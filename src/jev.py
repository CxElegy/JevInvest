"""Jev（TypeSafe AI System One）クライアント。

Jev は seed も temperature も持たず、同一入力に対してビット同一の出力を
保証していない。したがって再現性は自前で作る必要がある:

  1. モデルバージョンをピン留めする（jev-1.13 ではなく jev-latest だと
     ある日突然バックテスト結果が変わる）
  2. (model, state, questions) のハッシュをキーに全応答を SQLite に保存する

この 2 つで「バックテストを 3 ヶ月後に再実行しても同じ結果が出る」を担保する。
キャッシュはコスト削減よりも再現性のための仕組み。

オフラインモード（JEV_OFFLINE=1）では API を叩かず、ハッシュから決定論的に
擬似応答を組み立てる。API キーなしで全パイプラインの配線を確認できる。
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

API_URL = "https://api.typesafe.ai/v1/systemone"

# Jev の料金（2026-09 時点）。出力は無料。
USD_PER_1M_INPUT = 0.042


class JevError(RuntimeError):
    pass


@dataclass(frozen=True)
class Answer:
    """1 つの質問に対する Jev の回答。

    型ごとに埋まるフィールドが違う:
      noul   -> noul のみ（confidence は無い。確率自体が確度）
      choice -> choice, probabilities, confidence
      score  -> score, probabilities, confidence, legend
    """

    qid: str
    type: str
    noul: float | None = None
    choice: str | None = None
    score: float | None = None
    confidence: float | None = None
    probabilities: dict[str, float] | None = None
    legend: dict[str, str] | None = None

    @property
    def certainty(self) -> float:
        """型をまたいで「どれだけ自信があるか」を 0-1 で取る。

        注意: TypeSafe は choice(相対) と noul(絶対) の数値は比較できないと
        明示的に警告している。この値は「同じ型の中での振り分け」にのみ使い、
        型をまたいだしきい値の共有はしないこと。
        """
        if self.type == "noul":
            # noul は 0.5 が判断保留。0 と 1 の両端がどちらも「確信あり」。
            return abs((self.noul or 0.5) - 0.5) * 2.0
        return self.confidence or 0.0


@dataclass
class JevResponse:
    model: str
    answers: dict[str, Answer]
    input_tokens: int
    cached: bool

    @property
    def cost_usd(self) -> float:
        if self.cached:
            return 0.0
        return self.input_tokens / 1_000_000 * USD_PER_1M_INPUT


def _canonical(obj: Any) -> str:
    """ハッシュ用の正規化 JSON。キー順を固定しないとキャッシュが当たらない。"""
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _payload_hash(model: str, state: Any, questions: dict) -> str:
    raw = f"{model}\n{_canonical(state)}\n{_canonical(questions)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _parse_answers(raw: dict) -> dict[str, Answer]:
    out: dict[str, Answer] = {}
    for qid, a in (raw.get("answers") or {}).items():
        out[qid] = Answer(
            qid=qid,
            type=a.get("type", ""),
            noul=a.get("noul"),
            choice=a.get("choice"),
            score=a.get("score"),
            confidence=a.get("confidence"),
            probabilities=a.get("probabilities"),
            legend=a.get("legend"),
        )
    return out


class JevClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        cache_path: str | Path | None = None,
        offline: bool | None = None,
        timeout: float = 30.0,
    ):
        self.api_key = api_key or os.getenv("TYPESAFE_API_KEY", "")
        self.model = model or os.getenv("JEV_MODEL", "jev-1.13")
        self.offline = (
            offline if offline is not None else os.getenv("JEV_OFFLINE", "0") == "1"
        )
        self.timeout = timeout

        if not self.offline and not self.api_key:
            raise JevError(
                "TYPESAFE_API_KEY が未設定。.env に設定するか、"
                "配線確認だけなら JEV_OFFLINE=1 を指定してください。"
            )

        cache_path = cache_path or Path(__file__).resolve().parents[1] / "data" / "jev_cache.sqlite"
        Path(cache_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS responses (
                   hash TEXT PRIMARY KEY,
                   model TEXT NOT NULL,
                   response_json TEXT NOT NULL,
                   input_tokens INTEGER NOT NULL,
                   created_at REAL NOT NULL
               )"""
        )
        self._db.commit()
        self._client = httpx.Client(timeout=timeout)

        # 実行中の集計。バックテスト後に「いくらかかったか」を出すため。
        self.api_calls = 0
        self.cache_hits = 0
        self.total_input_tokens = 0

    # ------------------------------------------------------------ public

    def ask(self, state: Any, questions: dict) -> JevResponse:
        """state に対して questions をまとめて 1 回で評価する。

        questions は複数入れても state のトークン代を共有するので、
        戦略を増やしてもリクエスト数は増えない。
        """
        if not questions:
            raise JevError("questions が空です")

        h = _payload_hash(self.model, state, questions)

        cached = self._db.execute(
            "SELECT response_json, input_tokens FROM responses WHERE hash = ?", (h,)
        ).fetchone()
        if cached is not None:
            self.cache_hits += 1
            raw = json.loads(cached[0])
            return JevResponse(
                model=raw.get("model", self.model),
                answers=_parse_answers(raw),
                input_tokens=cached[1],
                cached=True,
            )

        raw = self._stub(h, questions) if self.offline else self._post(state, questions)

        input_tokens = int((raw.get("usage") or {}).get("input_tokens", 0))
        self._db.execute(
            "INSERT OR REPLACE INTO responses VALUES (?,?,?,?,?)",
            (h, self.model, json.dumps(raw, ensure_ascii=False), input_tokens, time.time()),
        )
        self._db.commit()

        self.api_calls += 1
        self.total_input_tokens += input_tokens
        return JevResponse(
            model=raw.get("model", self.model),
            answers=_parse_answers(raw),
            input_tokens=input_tokens,
            cached=False,
        )

    @property
    def spend_usd(self) -> float:
        return self.total_input_tokens / 1_000_000 * USD_PER_1M_INPUT

    def stats(self) -> dict:
        total = self.api_calls + self.cache_hits
        return {
            "api_calls": self.api_calls,
            "cache_hits": self.cache_hits,
            "cache_hit_rate": (self.cache_hits / total) if total else 0.0,
            "input_tokens": self.total_input_tokens,
            "spend_usd": round(self.spend_usd, 6),
        }

    def close(self) -> None:
        self._client.close()
        self._db.close()

    # ----------------------------------------------------------- internal

    def _post(self, state: Any, questions: dict) -> dict:
        body = {"model": self.model, "state": state, "questions": questions}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # レート制限が公開されていないので、429/5xx は指数バックオフで粘る。
        delay = 1.0
        last: Exception | None = None
        for attempt in range(5):
            try:
                r = self._client.post(API_URL, json=body, headers=headers)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    last = JevError(f"HTTP {r.status_code}: {r.text[:200]}")
                    retry_after = r.headers.get("retry-after")
                    time.sleep(float(retry_after) if retry_after else delay)
                    delay = min(delay * 2, 30.0)
                    continue
                raise JevError(f"HTTP {r.status_code}: {r.text[:500]}")
            except httpx.RequestError as e:
                last = e
                time.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise JevError(f"5 回リトライしても失敗: {last}")

    def _stub(self, h: str, questions: dict) -> dict:
        """オフライン用の決定論的な擬似応答。

        ハッシュから値を作るので、同じ入力なら常に同じ答えが返る。
        あくまで配線確認用で、判断内容に意味はない。
        """
        answers: dict[str, dict] = {}
        for i, (qid, q) in enumerate(sorted(questions.items())):
            seed = int(hashlib.sha256(f"{h}:{qid}".encode()).hexdigest()[:8], 16)
            frac = (seed % 10_000) / 10_000.0
            qtype = q["type"]

            if qtype == "noul":
                answers[qid] = {"type": "noul", "noul": round(frac, 4)}
            elif qtype == "choice":
                opts = list((q.get("criteria") or {}).keys()) or ["flat"]
                probs = [((seed >> (3 * k)) % 100) + 1 for k in range(len(opts))]
                total = sum(probs)
                pmap = {o: round(p / total, 4) for o, p in zip(opts, probs)}
                win = max(pmap, key=pmap.get)
                top = sorted(pmap.values(), reverse=True)
                conf = top[0] - (top[1] if len(top) > 1 else 0.0)
                answers[qid] = {
                    "type": "choice",
                    "choice": win,
                    "confidence": round(conf, 4),
                    "probabilities": pmap,
                }
            elif qtype == "score":
                levels = q.get("criteria") or []
                n = max(len(levels), 2)
                answers[qid] = {
                    "type": "score",
                    "score": round(frac * (n - 1), 4),
                    "confidence": round(0.5 + frac / 2, 4),
                    "legend": {str(k): str(v) for k, v in enumerate(levels)},
                    "probabilities": {str(k): round(1.0 / n, 4) for k in range(n)},
                }
            else:
                raise JevError(f"未知の質問タイプ: {qtype}")

            del i  # 使っていないが列挙の意図を残す

        # トークン数もおおよそ再現しておく（見積りツールと整合させるため）
        approx = len(_canonical(questions)) // 3
        return {
            "model": f"{self.model}-offline-stub",
            "answers": answers,
            "usage": {"input_tokens": approx, "output_tokens": 0},
        }
