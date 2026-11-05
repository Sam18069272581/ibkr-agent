"""快哨兵：每笔买入下单前，用便宜的快模型（默认 Haiku 5.5）回答三道固定的封闭问题。

- 只管开仓。卖出不经过它，它坏了也不会卡住止损或再平衡卖出。
- 只给概率，阈值写在配置里：失效概率或异常概率超过阈值就不买。
- 没有答案（没有客户端、超预算、报错、超时、拒答、格式不对）就不买，也不重问。不买的原因写明是哪一种。
- 第三题（7 天后涨的概率）不参与决策，只用来每周校准：看它说 70% 的时候是不是真的大约七成会涨。

每次回答都记进 crypto_gate 表，report 里的校准表从这里读。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime

from .config import CryptoSettings
from .envelope import Envelope
from .llm import Budget, Call, ask_json
from .market import WEEK_DAYS, period_return

log = logging.getLogger(__name__)

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

# 前两题超过配置的阈值（默认 30%）就不买，所以提示词不能让模型“拿不准就答一半”：
# 看不到的数据不是出事的证据，数据里没有迹象时，这两件事的概率本来就低。第三题只用来校准，可以答一半。
QUESTIONS = {
    "p_invalidated": "按给出的数据，本周边界里的任何一条失效条件已经成立、或很可能在 24 小时内成立的概率是多少？"
                     "没有边界时，回答“价格正处在急跌或明显的下跌趋势中”的概率（看 return_1d、return_7d 和近期波动）。"
                     "数据里看不到这类迹象时给低概率。",
    "p_abnormal": "当前行情处于异常状态（闪崩、插针、价差异常扩大、流动性明显变差）的概率是多少？"
                  "看给出的价差和近期波动范围；都正常时给低概率。",
    "p_up_7d": "7 天后价格高于现在价格的概率是多少？这题本来就接近抛硬币，没有把握就给 0.5 左右，不要编造信心。",
}

SYSTEM_PROMPT = """你是交易系统开仓前的快速哨兵。你只回答固定的三道题，每题给一个 0 到 1 之间的概率。
只依据给出的数据；数据里列为 missing 的内容你看不到，不要猜，也不要因为看不到就把风险往高里报。
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


def problem(raw: dict) -> str | None:
    """回答哪里不能用；能用时返回 None。"""
    for k in QUESTIONS:
        if k not in raw:
            return f"缺少 {k}"
        try:
            p = float(raw[k])
        except (TypeError, ValueError):
            return f"{k} 不是数字：{raw[k]!r}"
        if not (math.isfinite(p) and 0.0 <= p <= 1.0):
            return f"{k}={p:g} 不在 0 到 1 之间"
    return None


def parse(raw: dict | None) -> GateAnswer | None:
    """把回答转成 GateAnswer；不能用时记日志并返回 None。"""
    if raw is None:
        return None
    if why := problem(raw):
        log.warning("快哨兵的回答不能用：%s", why)
        return None
    return GateAnswer(**{k: float(raw[k]) for k in QUESTIONS}, note=str(raw.get("note", "")))


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
    return {
        "asset": asset,
        "planned_action": f"BUY 约 {notional:.2f} {quote}（只挂 maker 单，不追价）",
        "price": price,
        "spread_bps": round(spread_bps, 1),
        f"range_{window_minutes}m_pct": round((hi - lo) / o, 4) if o else None,
        "return_1d": period_return(closes, price, 1),
        "return_7d": period_return(closes, price, WEEK_DAYS),
        "envelope": None if env is None else {
            "stance": env.stance,
            "buy_range": [round(env.buy_low, 2), round(env.buy_high, 2)],
            "invalidation": [{"kind": c.kind, "value": c.value, "why": c.why} for c in env.invalidation],
            "valid_until": env.valid_until,
        },
        "missing": ["新闻", "链上数据", "资金费率和持仓量", "其他交易所的价格"],
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
        result = self._ask(facts, now)
        a = result.answer
        self.db.execute("INSERT INTO crypto_gate VALUES (?,?,?,?,?,?,?,?,?)",
                        (now.isoformat(), self.account, facts["asset"], facts["price"],
                         a and a.p_invalidated, a and a.p_abnormal, a and a.p_up_7d, result.verdict,
                         self.s.ai.gate_model))
        self.db.commit()
        return result

    def _ask(self, facts: dict, now: datetime) -> GateResult:
        ai = self.s.ai
        if self.client is None:
            log.warning("快哨兵已开启但没有 Claude 客户端，%s 不买", facts["asset"])
            return GateResult("no_answer", "快哨兵不可用（没有 Claude 客户端，看上面的日志），不买", None)
        answer = ask_json(self.client, self.budget, Call(
            purpose="gate",
            model=ai.gate_model,
            effort=ai.gate_effort,
            system=SYSTEM_PROMPT,
            user="数据（JSON）：\n" + json.dumps(facts, ensure_ascii=False),
            schema=OUTPUT_SCHEMA,
            max_tokens=ai.gate_max_tokens,
            timeout_s=ai.gate_timeout_seconds,
            max_retries=ai.gate_max_retries,
        ), now)
        if answer.data is None:
            return GateResult("no_answer", f"快哨兵没有回答（{answer.error}），不买", None)
        if why := problem(answer.data):
            log.warning("快哨兵对 %s 的回答不能用：%s", facts["asset"], why)
            return GateResult("no_answer", f"快哨兵的回答不能用（{why}），不买", None)
        return judge(parse(answer.data), self.s)


def record_prices(db: sqlite3.Connection, account: str, prices: dict[str, float], now: datetime) -> None:
    db.executescript(SCHEMA)
    db.executemany("INSERT INTO crypto_prices VALUES (?,?,?,?)",
                   [(now.isoformat(), account, a, p) for a, p in prices.items()])
    db.commit()
