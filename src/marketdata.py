"""市場データ（yfinance）の取得と SQLite キャッシュ。

無料データソースの扱いで実際に踏む問題を、ここで全部吸収する:

  1. 429 のとき yfinance は例外を投げず、空の DataFrame を返す。
     → 空応答を「失敗」として警告に残す。黙って 0 円扱いにしない。
  2. 銘柄ごとのループは IP 単位のレート制限に当たりやすい。
     → 一括取得（1 回の download に全銘柄）にする。
  3. 数日だけ 10 倍ずれる配信ミスが混ざる（例: 1306.T の 382.7 → 37.6 → 389.2）。
     → 1 日 ±50% を超える動きが数営業日以内に元の水準へ戻るなら配信ミスとして除外し、
       戻らないものは本物のイベントの可能性があるので直さず警告だけ出す。
  4. 取得のたびに叩くと制限に当たる。
     → SQLite に保存し、価格は 15 分、履歴は 12 時間だけ再利用する。

評価額には実際の価格（close）、リターン・ボラ・相関には配当再投資後（adj_close）を使うため、
両方を保存する。
"""

from __future__ import annotations

import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "market.sqlite"

QUOTE_TTL_SEC = 15 * 60
HISTORY_TTL_SEC = 12 * 3600

# 異常バー判定
JUMP_THRESHOLD = 0.5        # 直前の正常な終値から ±50% を超えたら候補
RECOVERY_WINDOW = 5         # この営業日数以内に戻れば配信ミスとみなす
RECOVERY_TOLERANCE = 0.3    # 「戻った」とみなす直前水準からの乖離

BAR_COLUMNS = ("open", "high", "low", "close", "adj_close", "volume")
_FIELD_MAP = {
    "open": "open", "high": "high", "low": "low", "close": "close",
    "adj close": "adj_close", "adj_close": "adj_close", "volume": "volume",
}


# ------------------------------------------------------------ 整形・検査

def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """yfinance の出力を、列名・index を揃えた日足に整える。"""
    d = df.copy()
    d.columns = [_FIELD_MAP.get(str(c).strip().lower(), str(c).strip().lower())
                 for c in d.columns]
    if "close" not in d.columns:
        return pd.DataFrame(columns=BAR_COLUMNS)

    for col in BAR_COLUMNS:
        if col not in d.columns:
            d[col] = np.nan
    d = d[list(BAR_COLUMNS)].apply(pd.to_numeric, errors="coerce")
    d = d.dropna(subset=["close"])
    if d.empty:
        return pd.DataFrame(columns=BAR_COLUMNS)

    d["adj_close"] = d["adj_close"].fillna(d["close"])
    for col in ("open", "high", "low"):
        d[col] = d[col].fillna(d["close"])
    d["volume"] = d["volume"].fillna(0.0)

    idx = pd.to_datetime(d.index)
    if idx.tz is not None:
        idx = idx.tz_localize(None)
    d.index = idx.normalize()
    d.index.name = "date"
    return d[~d.index.duplicated(keep="last")].sort_index()


def _split_download(raw: pd.DataFrame | None, symbols: list[str]) -> dict[str, pd.DataFrame]:
    """download の戻りを銘柄ごとの日足に分ける。銘柄が 1 つでも多段列で返る版がある。"""
    if raw is None or raw.empty:
        return {}

    out: dict[str, pd.DataFrame] = {}
    if isinstance(raw.columns, pd.MultiIndex):
        level0 = set(raw.columns.get_level_values(0))
        level1 = set(raw.columns.get_level_values(1))
        for s in symbols:
            if s in level0:
                sub = raw[s]
            elif s in level1:
                sub = raw.xs(s, axis=1, level=1)
            else:
                continue
            n = _normalize(sub)
            if not n.empty:
                out[s] = n
    elif len(symbols) == 1:
        n = _normalize(raw)
        if not n.empty:
            out[symbols[0]] = n
    return out


def clean_bars(symbol: str, df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """配信ミスの異常バーを除外する。除外したものと、除外しなかった疑いは必ず警告で返す。

    - 直前の正常な終値から ±50% を超えて動き、5 営業日以内に元の水準（±30%）へ戻る
      → データ異常。該当バーを落とす。
    - 戻らない → 未調整の分割や本物の暴落の可能性がある。勝手に直さず警告だけ出す。
    """
    warnings: list[str] = []
    if len(df) < 3:
        return df, warnings

    close = df["close"].to_numpy(dtype=float)
    n = len(close)
    drop = np.zeros(n, dtype=bool)
    last_good = 0
    i = 1
    while i < n:
        base = close[last_good]
        if base > 0 and abs(close[i] / base - 1.0) > JUMP_THRESHOLD:
            recovered = None
            for j in range(i + 1, min(n, i + 1 + RECOVERY_WINDOW)):
                if abs(close[j] / base - 1.0) < RECOVERY_TOLERANCE:
                    recovered = j
                    break
            if recovered is not None:
                drop[i:recovered] = True
                d0, d1 = df.index[i].date(), df.index[recovered - 1].date()
                warnings.append(
                    f"{symbol}: {d0}〜{d1} の {recovered - i} 営業日を配信ミスと判断して除外"
                    f"（{base:,.2f} → {close[i]:,.2f} → {close[recovered]:,.2f}）"
                )
                last_good = recovered
                i = recovered + 1
                continue
            warnings.append(
                f"{symbol}: {df.index[i].date()} に {close[i] / base - 1.0:+.0%} の変動"
                f"（{base:,.2f} → {close[i]:,.2f}）。戻らないため除外していません。"
                f"未調整の分割や実際の急変の可能性があります"
            )
        last_good = i
        i += 1

    return (df[~drop] if drop.any() else df), warnings


# ------------------------------------------------------------ 本体

class MarketData:
    def __init__(self, db_path: str | Path | None = None):
        path = Path(db_path) if db_path else DEFAULT_DB
        path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS quotes (
                symbol TEXT PRIMARY KEY, price REAL NOT NULL, fetched_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS bars (
                symbol TEXT NOT NULL, date TEXT NOT NULL,
                open REAL, high REAL, low REAL, close REAL, adj_close REAL, volume REAL,
                PRIMARY KEY (symbol, date)
            );
            CREATE TABLE IF NOT EXISTS history_meta (
                symbol TEXT PRIMARY KEY, years REAL NOT NULL,
                fetched_at REAL NOT NULL, notes TEXT
            );
            """
        )
        self._db.commit()
        # レポート冒頭に出す警告。取得失敗・古いキャッシュ・異常バー除外がここに溜まる。
        self.fetch_errors: list[str] = []

    # ---------------------------------------------------------- 取得（差し替え点）

    def _download(self, symbols: list[str], start: str | None = None,
                  period: str | None = None) -> dict[str, pd.DataFrame]:
        """yfinance で一括取得する。テストではここを差し替える。"""
        try:
            import yfinance as yf
        except ImportError:
            self.fetch_errors.append("yfinance が入っていません（pip install yfinance）")
            return {}

        kwargs = {"start": start} if start else {"period": period or "5d"}
        try:
            raw = yf.download(
                list(symbols), interval="1d", auto_adjust=False, group_by="ticker",
                progress=False, threads=False, **kwargs,
            )
        except Exception as e:  # noqa: BLE001 — 取得失敗は警告にして続行する
            self.fetch_errors.append(f"価格取得に失敗: {type(e).__name__}: {e}")
            return {}
        return _split_download(raw, list(symbols))

    # ---------------------------------------------------------- 価格・為替

    def get_quotes(self, symbols, force: bool = False) -> dict[str, float]:
        """直近の終値（配当調整なし）。評価額に使う。"""
        syms = sorted(set(symbols))
        now = time.time()
        out: dict[str, float] = {}
        todo: list[str] = []

        for s in syms:
            row = self._db.execute(
                "SELECT price, fetched_at FROM quotes WHERE symbol=?", (s,)
            ).fetchone()
            if row and not force and now - row[1] < QUOTE_TTL_SEC:
                out[s] = float(row[0])
            else:
                todo.append(s)

        if not todo:
            return out

        data = self._download(todo, period="5d")
        if not data:
            self.fetch_errors.append(
                "価格の取得結果が空です。レート制限（429）かネットワーク制限の可能性があります"
            )

        for s in todo:
            df = data.get(s)
            if df is not None and not df.empty:
                price = float(df["close"].iloc[-1])
                self._db.execute(
                    "INSERT OR REPLACE INTO quotes VALUES (?,?,?)", (s, price, now)
                )
                out[s] = price
                continue

            stale = self._db.execute(
                "SELECT price, fetched_at FROM quotes WHERE symbol=?", (s,)
            ).fetchone()
            if stale:
                when = datetime.fromtimestamp(stale[1]).strftime("%Y-%m-%d %H:%M")
                self.fetch_errors.append(
                    f"{s}: 取得に失敗したため古い価格を使用しています（{when} 時点）"
                )
                out[s] = float(stale[0])
            else:
                self.fetch_errors.append(
                    f"{s}: 価格を取得できませんでした（銘柄コード誤り・レート制限の可能性）"
                )
        self._db.commit()
        return out

    def get_fx(self, currencies, base: str) -> dict[str, float]:
        """「その通貨 1 単位 = 基準通貨いくら」。取れなかった通貨は含めない。"""
        out = {base: 1.0}
        pairs = {f"{c}{base}=X": c for c in sorted(set(currencies)) if c != base}
        if pairs:
            quotes = self.get_quotes(list(pairs))
            for sym, ccy in pairs.items():
                if sym in quotes:
                    out[ccy] = quotes[sym]
        return out

    # ---------------------------------------------------------- 履歴

    def get_history(self, symbols, years: float = 3.0) -> dict[str, pd.DataFrame]:
        """日足（open/high/low/close/adj_close/volume）。異常バーは除外済み。"""
        syms = sorted(set(symbols))
        now = time.time()
        since = pd.Timestamp(date.today() - timedelta(days=int(years * 365.25)))
        result: dict[str, pd.DataFrame] = {}
        todo: list[str] = []

        for s in syms:
            meta = self._db.execute(
                "SELECT years, fetched_at, notes FROM history_meta WHERE symbol=?", (s,)
            ).fetchone()
            if meta and meta[0] >= years - 1e-9 and now - meta[1] < HISTORY_TTL_SEC:
                df = self._load_bars(s, since)
                if not df.empty:
                    result[s] = df
                    # キャッシュ経由でも、除外した異常バーの警告は毎回出す
                    self.fetch_errors.extend(n for n in (meta[2] or "").split("\n") if n)
                    continue
            todo.append(s)

        if not todo:
            return result

        start = (date.today() - timedelta(days=int(years * 365.25) + 7)).isoformat()
        data = self._download(todo, start=start)
        if not data:
            self.fetch_errors.append(
                "履歴の取得結果が空です。レート制限（429）かネットワーク制限の可能性があります"
            )

        for s in todo:
            df = data.get(s)
            if df is None or df.empty:
                stale = self._load_bars(s, since)
                if not stale.empty:
                    self.fetch_errors.append(
                        f"{s}: 履歴の取得に失敗したため古いキャッシュを使用しています"
                    )
                    result[s] = stale
                else:
                    self.fetch_errors.append(f"{s}: 履歴を取得できませんでした")
                continue

            df, notes = clean_bars(s, df)
            self.fetch_errors.extend(notes)
            self._store_bars(s, df, years, now, notes)
            result[s] = df[df.index >= since]

        return result

    # ---------------------------------------------------------- キャッシュ

    def _store_bars(self, symbol: str, df: pd.DataFrame, years: float, now: float,
                    notes: list[str]) -> None:
        self._db.execute("DELETE FROM bars WHERE symbol=?", (symbol,))
        rows = [
            (symbol, idx.date().isoformat(), float(r.open), float(r.high), float(r.low),
             float(r.close), float(r.adj_close), float(r.volume))
            for idx, r in df.iterrows()
        ]
        self._db.executemany("INSERT INTO bars VALUES (?,?,?,?,?,?,?,?)", rows)
        self._db.execute(
            "INSERT OR REPLACE INTO history_meta VALUES (?,?,?,?)",
            (symbol, years, now, "\n".join(notes)),
        )
        self._db.commit()

    def _load_bars(self, symbol: str, since: pd.Timestamp) -> pd.DataFrame:
        rows = self._db.execute(
            "SELECT date, open, high, low, close, adj_close, volume FROM bars"
            " WHERE symbol=? AND date>=? ORDER BY date",
            (symbol, since.date().isoformat()),
        ).fetchall()
        if not rows:
            return pd.DataFrame(columns=BAR_COLUMNS)
        df = pd.DataFrame(rows, columns=("date", *BAR_COLUMNS))
        df["date"] = pd.to_datetime(df["date"])
        return df.set_index("date")

    def close(self) -> None:
        self._db.close()
