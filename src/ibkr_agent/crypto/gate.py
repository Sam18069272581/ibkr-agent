"""快哨兵：每笔买入下单前，用便宜的快模型（默认 Haiku 5.5）回答三道固定的封闭问题。

- 只管开仓。卖出不经过它，它坏了也不会卡住止损或再平衡卖出。
- 只给概率，阈值写死在配置里：失效概率或异常概率超过阈值就不买。
- 没有答案（超预算、报错、拒答、格式不对）就不买，也不重问。
- 第三题（7 天后涨的概率）不参与决策，只用来每周校准：看它说 70% 的时候是不是真的大约七成会涨。

每次回答都记进 crypto_gate 表，report 里的校准表从这里读。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import CryptoSettings
from .envelope import Envelope
from .llm import Budget, Call, ask_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_gate (
    ts TEXT NOT NULL,
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    price REAL NOT NULL,
    p_invalidated REAL,
    p_abnormal REAL,
    p_up_7d REAL,
    verdict TEXT NOT NULL,      -- pass | block | no_answer
    model TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS crypto_prices (
    ts TEXT NOT NULL,
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    price REAL NOT NULL
);
"""

QUESTIONS = {
    "p_invalidated": "按给出的数据，本周边界里的任何一条失效条件已经成立、或很可能在 24 小时内成立的概率是多少？没有边界时，回答“这个币的长期持有逻辑本周被破坏”的概率。",
    "p_abnormal": "当前行情处于异常状态（闪崩、插针、价差异常扩大、流动性明显变差）的概率是多少？",
    "p_up_7d": "7 天后价格高于现在价格的概率是多少？",
}

SYSTEM_PROMPT = """你是交易系统开仓前的快速哨兵。你只回答固定的三道题，每题给一个 0 到 1 之间的概率。
只依据给出的数据；数据里列为 missing 的内容你看不到，不要猜。拿不准时给接近 0.5 的概率，不要编造信心。
你不决定买不买，也不决定买多少，阈值由系统的代码决定。

题目：
""" + "\n".join(f"- {k}：{q}" for k, q in QUESTIONS.items())

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {**{k: {"type": "number"} for k in QUESTIONS}, "note": {"type": "string"}},
    "required": [*QUESTIONS, "note"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class GateAnswer:
    p_invalidated: float
    p_abnormal: float
    p_up_7d: float
    note: str


@dataclass(frozen=True)
class GateResult:
    verdict: str                 # pass | block | no_answer
    why: str
    answer: GateAnswer | None


def parse(raw: dict | None) -> GateAnswer | None:
    if raw is None:
        return None
    try:
        ps = {k: float(raw[k]) for k in QUESTIONS}
    except (KeyError, TypeError, ValueError):
        return None
    if not all(0.0 <= p <= 1.0 for p in ps.values()):
        return None
    return GateAnswer(**ps, note=str(raw.get("note", "")))


def judge(a: GateAnswer | None, s: CryptoSettings) -> GateResult:
    if a is None:
        return GateResult("no_answer", "快哨兵没有给出答案，不买", None)
    if a.p_invalidated > s.ai.gate_max_p_invalidated:
        return GateResult("block", f"快哨兵认为失效概率 {a.p_invalidated:.0%}，超过 {s.ai.gate_max_p_invalidated:.0%}", a)
    if a.p_abnormal > s.ai.gate_max_p_abnormal:
        return GateResult("block", f"快哨兵认为行情异常概率 {a.p_abnormal:.0%}，超过 {s.ai.gate_max_p_abnormal:.0%}", a)
    return GateResult("pass", f"快哨兵放行（失效 {a.p_invalidated:.0%}，异常 {a.p_abnormal:.0%}，7 天上涨 {a.p_up_7d:.0%}）", a)


def dossier(asset: str, price: float, spread_bps: float, window: tuple[float, float, float], window_minutes: int,
            closes: list[float] | None, env: Envelope | None, notional: float, quote: str) -> dict:
    o, lo, hi = window

    def ret(days: int) -> float | None:
        return round(price / closes[-1 - days] - 1, 4) if closes and len(closes) > days and closes[-1 - days] > 0 else None

    return {
        "asset": asset,
        "planned_action": f"BUY 约 {notional:.2f} {quote}（只挂 maker 单，不追价）",
        "price": price,
        "spread_bps": round(spread_bps, 1),
        f"range_{window_minutes}m_pct": round((hi - lo) / o, 4) if o else None,
        "return_1d": ret(1),
        "return_7d": ret(7),
        "envelope": None if env is None else {
            "stance": env.stance,
            "buy_range": [round(env.buy_low, 2), round(env.buy_high, 2)],
            "invalidation": [{"kind": c.kind, "value": c.value, "why": c.why} for c in env.invalidation],
            "valid_until": env.valid_until,
        },
        "missing": (["日线"] if not closes else []) + ["新闻", "链上数据", "资金费率和持仓量", "其他交易所的价格"],
    }


class Gate:
    def __init__(self, client, budget: Budget, db: sqlite3.Connection, account: str, s: CryptoSettings):
        self.client = client
        self.budget = budget
        self.db = db
        self.account = account
        self.s = s
        db.executescript(SCHEMA)

    def ask(self, facts: dict, now: datetime) -> GateResult:
        raw = None
        if self.client is not None:
            raw = ask_json(self.client, self.budget, Call(
                purpose="gate",
                model=self.s.ai.gate_model,
                effort=self.s.ai.gate_effort,
                system=SYSTEM_PROMPT,
                user="数据（JSON）：\n" + json.dumps(facts, ensure_ascii=False),
                schema=OUTPUT_SCHEMA,
                estimate_usd=0.01,
                max_tokens=4000,
            ))
        result = judge(parse(raw), self.s)
        a = result.answer
        self.db.execute("INSERT INTO crypto_gate VALUES (?,?,?,?,?,?,?,?,?)",
                        (now.isoformat(), self.account, facts["asset"], facts["price"],
                         a and a.p_invalidated, a and a.p_abnormal, a and a.p_up_7d, result.verdict,
                         self.s.ai.gate_model))
        self.db.commit()
        return result


def record_prices(db: sqlite3.Connection, account: str, prices: dict[str, float], now: datetime) -> None:
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO crypto_prices VALUES (?,?,?,?)",
                   [(now.isoformat(), account, a, p) for a, p in prices.items()])
    db.commit()
