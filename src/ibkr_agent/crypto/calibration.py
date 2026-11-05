"""快哨兵校准表：它说“7 天后上涨的概率是 70%”的那些时候，后来真的大约七成涨了吗？

只读 crypto_gate 和 crypto_prices 两张表。每次运行都会记价格，
所以一次回答的“结果”取它之后 6.5 到 10 天之间最早记下的价格。没有这个价格的回答先不计入。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta

from .gate import SCHEMA

BUCKETS = ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0001))


@dataclass(frozen=True)
class Bucket:
    low: float
    high: float
    n: int
    mean_p: float
    hit_rate: float


@dataclass(frozen=True)
class Calibration:
    scored: int          # 已有结果的回答数
    pending: int         # 还没到 7 天、或者没有对应价格的回答数
    brier: float | None  # 越低越好；永远答 0.5 是 0.25
    buckets: list[Bucket]
    verdicts: dict[str, int]


def build(db: sqlite3.Connection, account: str, now: datetime) -> Calibration:
    db.executescript(SCHEMA)
    rows = db.execute("SELECT ts, asset, price, p_up_7d FROM crypto_gate WHERE account = ? AND p_up_7d IS NOT NULL",
                      (account,)).fetchall()
    verdicts = dict(db.execute("SELECT verdict, COUNT(*) FROM crypto_gate WHERE account = ? GROUP BY verdict",
                               (account,)).fetchall())
    scored: list[tuple[float, int]] = []
    pending = 0
    for ts, asset, price, p in rows:
        t = datetime.fromisoformat(ts)
        lo, hi = t + timedelta(days=6.5), t + timedelta(days=10)
        later = None
        if lo <= now:
            later = db.execute("SELECT price FROM crypto_prices WHERE account = ? AND asset = ? AND ts >= ? AND ts <= ? "
                               "ORDER BY ts LIMIT 1", (account, asset, lo.isoformat(), hi.isoformat())).fetchone()
        if later is None:
            pending += 1
            continue
        scored.append((p, int(later[0] > price)))
    buckets = []
    for lo_p, hi_p in BUCKETS:
        xs = [(p, y) for p, y in scored if lo_p <= p < hi_p]
        if xs:
            buckets.append(Bucket(lo_p, min(hi_p, 1.0), len(xs), sum(p for p, _ in xs) / len(xs),
                                  sum(y for _, y in xs) / len(xs)))
    brier = sum((p - y) ** 2 for p, y in scored) / len(scored) if scored else None
    return Calibration(len(scored), pending, brier, buckets, verdicts)


def render(c: Calibration) -> str:
    v = c.verdicts
    lines = [f"快哨兵：放行 {v.get('pass', 0)} 次，拦下 {v.get('block', 0)} 次，没回答 {v.get('no_answer', 0)} 次",
             f"校准（7 天后是否上涨）：已出结果 {c.scored} 次，等待中 {c.pending} 次"]
    if c.brier is None:
        lines.append("  还没有可以打分的回答。")
        return "\n".join(lines)
    lines.append(f"  Brier 分数 {c.brier:.3f}（越低越好；一直答 50% 是 0.250）")
    lines.append("  它说的概率      次数   平均说   实际上涨")
    for b in c.buckets:
        lines.append(f"  {b.low:4.0%} – {b.high:4.0%}   {b.n:5d}   {b.mean_p:6.0%}   {b.hit_rate:7.0%}")
    if c.scored < 30:
        lines.append("  样本少于 30 次，只能看个大概。")
    return "\n".join(lines)
