"""13F の保有を「資産の種類」に分け、持ち方の特徴を文章にする。

分類（画面の並び順）:
    stock      個別株
    idx_etf    株式インデックス ETF（S&P 500、ナスダック 100、全世界株など）
    bond_etf   債券 ETF（国債・社債・ハイイールド・物価連動など）
    gold_etf   金・商品 ETF（金鉱株 ETF を含む）
    crypto_etf 暗号資産 ETF
    other_etf  その他の ETF（業種・テーマ型など）
    debt       社債・転換社債・優先株など（13F で元本額 PRN 表記、または利率付きの銘柄）
    unknown    未分類（銘柄を特定していない分。大規模な運用会社の下位銘柄など）

オプション（プット・コール）は保有額に含めず、想定元本を「株式ロングに対する比率」で別に出す。

**13F には国債・現金・米国外の株・空売りが出ない。** ここでいう「債券」は ETF と
一部の社債・転換社債だけで、その人の本当の債券比率ではない。文章にも必ずそう書く。
"""

from __future__ import annotations

KINDS = ("stock", "idx_etf", "bond_etf", "gold_etf", "crypto_etf", "other_etf", "debt", "unknown")
KIND_JP = {
    "stock": "個別株", "idx_etf": "株式インデックスETF", "bond_etf": "債券ETF",
    "gold_etf": "金・商品ETF", "crypto_etf": "暗号資産ETF", "other_etf": "その他のETF（業種・テーマ）",
    "debt": "社債・転換社債など", "unknown": "未分類",
}
FUND_TYPES = {"ETP", "Mutual Fund", "Closed-End Fund", "Unit"}

BOND_TICKERS = set("""
TLT IEF SHY SHV BIL SGOV TLH GOVT VGSH VGIT VGLT SCHO SCHR SPTS SPTI SPTL TIP SCHP VTIP STIP
LQD VCIT VCSH VCLT IGIB IGSB SPIB SPSB HYG JNK USHY SHYG SJNK HYLB ANGL FALN BKLN SRLN AGG BND
IUSB SCHZ BNDX EMB VWOB PCY MUB VTEB MBB VMBS JPST MINT NEAR FLOT FLRN GSY ICSH PULS USFR TFLO
ZROZ EDV BIV BSV BLV IEI IGLB SPAB SPLB BOXX XBIL TBIL CLIP
""".split())
GOLD_TICKERS = set("""
GLD IAU GLDM SGOL AAAU BAR SLV SIVR PPLT PALL GDX GDXJ SIL SILJ RING USO BNO UNG DBC DBA PDBC GSG
COMT CPER URA URNM
""".split())
CRYPTO_TICKERS = set("IBIT FBTC GBTC BITB ARKB HODL BTCO BRRR EZBC BTC ETHA FETH ETHE ETH BITO BITX".split())
INDEX_TICKERS = set("""
SPY IVV VOO VTI ITOT SCHB SCHX QQQ QQQM ONEQ IWM IWB IWF IWD IWO IWN IWR IWS IWP IJH IJR MDY
SPLG RSP DIA VT ACWI VEA VWO EFA EEM IEMG IEFA VXUS IXUS VUG VTV VO VB VV MGK SCHG SCHD SCHA SPYG
SPYV IUSG IUSV EWJ EWZ FXI MCHI KWEB INDA EWT EWY EZU VGK EWG EWU OEF MTUM QUAL USMV
""".split())

_BOND_WORDS = ("TREAS", "BOND", " BD ", "CORP BD", "HIGH YIELD", "HI YLD", "TIPS", "MUNI",
               "AGGREGATE", "FIXED INC", "T-BILL", "TBILL", "FLOATING RATE", "0-3 MONTH")
_GOLD_WORDS = ("GOLD", "SILVER", "PLATINUM", "PALLADIUM", "OIL FUND", "COMMODITY", "URANIUM",
               "NATURAL GAS")
_CRYPTO_WORDS = ("BITCOIN", "ETHER", "CRYPTO")
_INDEX_WORDS = ("S&P 500", "S&P500", "S&P MIDCAP", "S&P SMALLCAP", "RUSSELL", "TOTAL STOCK",
                "TOTAL WORLD", "TOTAL MKT", "NASDAQ 100", "QQQ", "MSCI", "DOW JONES INDUS", "DJIA",
                "EMERGING MKT", "DEVELOPED MKT")


def classify(ticker, name, sec_type, share_type) -> str:
    """13F の 1 行を資産の種類に分ける。"""
    if share_type == "PRN":
        return "debt"
    t = (ticker or "").split()[0].upper() if isinstance(ticker, str) else ""
    if isinstance(ticker, str) and " " in ticker.strip():
        return "debt"                       # 利率・満期付きの表記（社債・優先株）
    if not t:
        return "unknown"
    n = f" {name.upper()} " if isinstance(name, str) else " "
    if sec_type in FUND_TYPES or t in BOND_TICKERS | GOLD_TICKERS | CRYPTO_TICKERS | INDEX_TICKERS:
        if t in BOND_TICKERS or any(w in n for w in _BOND_WORDS):
            return "bond_etf"
        if t in CRYPTO_TICKERS or any(w in n for w in _CRYPTO_WORDS):
            return "crypto_etf"
        if t in GOLD_TICKERS or any(w in n for w in _GOLD_WORDS):
            return "gold_etf"
        if t in INDEX_TICKERS or any(w in n for w in _INDEX_WORDS):
            return "idx_etf"
        return "other_etf"
    return "stock"


def mix_weights(values_by_kind: dict[str, float]) -> dict[str, float]:
    tot = sum(values_by_kind.values())
    if tot <= 0:
        return {}
    return {k: values_by_kind.get(k, 0.0) / tot * 100.0 for k in KINDS if values_by_kind.get(k, 0.0) > 0}


def _pct(v: float) -> str:
    return f"{v:.1f}%"


def insights(f: dict, group_sector_avg: dict[str, float] | None = None) -> list[str]:
    """投資家 1 人の「持ち方」の特徴を、数字の根拠つきで短い文にする。

    f に必要なもの: mix, mix_prev, top10_pct, eff_n, n, puts_pct, calls_pct, put_names,
    call_names, sectors, selective, comparable, n_new, n_exit, unknown_note
    """
    out: list[str] = []
    mix = f["mix"]

    # 1. 集中度
    top10, eff = f["top10_pct"], f["eff_n"]
    if top10 >= 70:
        kind = "少数の銘柄に絞った集中投資"
    elif top10 >= 40:
        kind = "中程度の分散"
    else:
        kind = "幅広い分散（指数に近い形）"
    out.append(f"上位 10 銘柄で {_pct(top10)}、実質 {eff:.1f} 銘柄分の分散。{kind}です。")

    # 2. インデックス ETF
    idx = mix.get("idx_etf", 0.0)
    if idx >= 1:
        out.append(f"株式インデックス ETF が {_pct(idx)}。個別株とは別に、市場全体への投資"
                   "（または一時的な資金の置き場）を持っています。")
    elif f["selective"]:
        out.append("株式インデックス ETF はほぼ持たず、個別株で勝負しています。")

    # 3. 債券
    bond, debt = mix.get("bond_etf", 0.0), mix.get("debt", 0.0)
    if bond + debt >= 0.5:
        parts = []
        if bond >= 0.1:
            parts.append(f"債券 ETF {_pct(bond)}")
        if debt >= 0.1:
            parts.append(f"社債・転換社債など {_pct(debt)}")
        out.append("、".join(parts) + "。13F に出る範囲の債券で、国債や現金は含みません。")

    # 4. 金・商品、暗号資産
    if mix.get("gold_etf", 0.0) >= 0.5:
        out.append(f"金・商品 ETF が {_pct(mix['gold_etf'])}。インフレや通貨の価値低下への備えとみられます。")
    if mix.get("crypto_etf", 0.0) >= 0.5:
        out.append(f"暗号資産 ETF が {_pct(mix['crypto_etf'])}。")

    # 5. オプション
    if f["puts_pct"] >= 5:
        who = "、".join(f["put_names"][:3])
        out.append(f"プットの想定元本が株式ロングの {_pct(f['puts_pct'])}（主な対象: {who}）。"
                   "下落への備え（ヘッジ）か、下落を見込んだ賭けです。13F だけではどちらか区別できません。")
    if f["calls_pct"] >= 5:
        who = "、".join(f["call_names"][:3])
        out.append(f"コールの想定元本が株式ロングの {_pct(f['calls_pct'])}（主な対象: {who}）。"
                   "少ない資金で上昇に賭けています。")

    # 6. 分野の偏り
    if f["sectors"]:
        top = f["sectors"][0]
        line = f"最も大きい分野は「{top['s']}」で {_pct(top['w'])}"
        if group_sector_avg:
            diffs = [(s["w"] - group_sector_avg.get(s["s"], 0.0), s["s"]) for s in f["sectors"]]
            d, s = max(diffs)
            if d >= 5:
                line += f"。集中投資型の平均と比べて「{s}」が {d:+.1f}pp 多い"
        out.append(line + "。")

    # 7. 前期からの変化
    if f["comparable"] and f["mix_prev"]:
        for k, label in (("idx_etf", "株式インデックス ETF"), ("bond_etf", "債券 ETF"),
                         ("gold_etf", "金・商品 ETF")):
            d = mix.get(k, 0.0) - f["mix_prev"].get(k, 0.0)
            if abs(d) >= 2:
                out.append(f"{label}の比率を前期から {d:+.1f}pp {'増やしました' if d > 0 else '減らしました'}。")
        if f["selective"]:
            out.append(f"前期からの入れ替え: 新規 {f['n_new']} 銘柄、全売却 {f['n_exit']} 銘柄"
                       f"（保有 {f['n']} 銘柄中）。")

    if f.get("unknown_note"):
        out.append(f["unknown_note"])
    return out


_INTL_TICKERS = set("""
VEA VWO EFA EEM IEMG IEFA VXUS IXUS VGK EZU EWJ EWZ EWY EWT EWG EWU FXI MCHI KWEB INDA ACWI VT
""".split())
_INTL_WORDS = ("EMERGING", "EAFE", "EUROPE", "JAPAN", "CHINA", "INDIA", "BRAZIL", "KOREA", "TAIWAN",
               "GERMANY", "UNITED KINGDOM", "ACWI", "TOTAL WORLD", "INTL", "INTERNATIONAL", "DEVELOPED",
               "EX-US", "EX US", "FTSE DEV")


def etf_subtype(ticker: str | None, name: str | None, kind: str) -> str:
    """ETF をもう一段細かく分ける（画面の「ETF の種類」用）。"""
    t = (ticker or "").upper()
    n = f" {(name or '').upper()} "
    if kind == "idx_etf":
        if t in _INTL_TICKERS or any(w in n for w in _INTL_WORDS):
            return "海外・新興国株の指数"
        return "米国株の指数"
    if kind == "bond_etf":
        if t in {"BIL", "SGOV", "SHV", "TBIL", "XBIL", "BOXX", "CLIP", "USFR", "TFLO", "GBIL"} or \
                any(w in n for w in ("T-BILL", "TBILL", "0-3 MONTH", "1-3M", "ULTRA SHORT", "TREASURY FLOAT")):
            return "短期国債（現金の置き場）"
        if t in {"HYG", "JNK", "USHY", "SHYG", "SJNK", "HYLB", "ANGL", "FALN"} or \
                any(w in n for w in ("HIGH YIELD", "HI YLD", "HIGH YLD")):
            return "ハイイールド債"
        if t in {"TLT", "IEF", "TLH", "GOVT", "VGLT", "VGIT", "SPTL", "SPTI", "ZROZ", "EDV", "IEI", "SHY",
                 "VGSH", "SCHO", "SCHR", "SPTS"} or "TREAS" in n:
            return "米国債"
        if t in {"TIP", "SCHP", "VTIP", "STIP"} or "TIPS" in n:
            return "物価連動債"
        if t in {"EMB", "VWOB", "PCY", "BNDX"}:
            return "海外・新興国債"
        return "社債・総合債券"
    if kind == "gold_etf":
        if "MINER" in n or t in {"GDX", "GDXJ", "SIL", "SILJ", "RING"}:
            return "金鉱・銀鉱株"
        if "GOLD" in n or t in {"GLD", "IAU", "GLDM", "SGOL", "AAAU", "BAR"}:
            return "金"
        if "SILVER" in n or t in {"SLV", "SIVR"}:
            return "銀"
        return "原油・その他の商品"
    if kind == "crypto_etf":
        return "イーサリアム" if "ETHER" in n or t.startswith("ETH") or t == "FETH" else "ビットコイン"
    if kind == "other_etf":
        if "SELECT SECTOR" in n or (len(t) == 3 and t.startswith("XL")):
            return "米国の業種別"
        return "業種・テーマ・その他"
    return KIND_JP.get(kind, kind)
