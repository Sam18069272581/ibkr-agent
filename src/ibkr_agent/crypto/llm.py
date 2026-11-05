"""币圈部分调用 Claude 的唯一入口：先查月度预算，再调用，按 usage 记账。

任何失败（超预算、报错、拒答、截断、格式不对）都返回 None。调用方必须把 None 当成“没有答案”：
边界没有就按配置目标走；快哨兵没答就不买。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_usage (
    ts TEXT NOT NULL,
    purpose TEXT NOT NULL,          -- envelope | gate
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
"""

# 美元 / 百万 token：输入、输出、写缓存、读缓存。没列出的模型按最贵的档估，宁可高估。
PRICES = {
    "claude-opus-5-5": (4.0, 20.0, 5.0, 0.20),
    "claude-sonnet-5-5": (2.0, 10.0, 2.5, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.125, 0.01),
}
UNKNOWN_PRICE = (10.0, 50.0, 12.5, 1.0)


def usd(x: float) -> str:
    """花费的显示：小于 1 美分时显示到四位小数，不然 Haiku 的调用全是 $0.00。"""
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_write: int = 0, cache_read: int = 0) -> float:
    i, o, w, r = PRICES.get(model, UNKNOWN_PRICE)
    return (input_tokens * i + output_tokens * o + cache_write * w + cache_read * r) / 1_000_000


class Budget:
    def __init__(self, db: sqlite3.Connection, monthly_cap_usd: float):
        self.db = db
        self.cap = monthly_cap_usd
        db.executescript(SCHEMA)

    def spent_this_month(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        row = self.db.execute("SELECT SUM(cost_usd) FROM api_usage WHERE substr(ts, 1, 7) = ?",
                              (now.strftime("%Y-%m"),)).fetchone()
        return row[0] or 0.0

    def allows(self, estimate: float, now: datetime | None = None) -> bool:
        return self.spent_this_month(now) + estimate <= self.cap

    def record(self, purpose: str, model: str, usage, now: datetime | None = None) -> float:
        i = int(getattr(usage, "input_tokens", 0) or 0)
        o = int(getattr(usage, "output_tokens", 0) or 0)
        w = int(getattr(usage, "cache_creation_input_tokens", 0) or 0)
        r = int(getattr(usage, "cache_read_input_tokens", 0) or 0)
        c = cost_usd(model, i, o, w, r)
        self.db.execute("INSERT INTO api_usage VALUES (?,?,?,?,?,?,?,?)",
                        ((now or datetime.now(timezone.utc)).isoformat(), purpose, model, i, o, w, r, c))
        self.db.commit()
        return c

    def by_purpose(self, now: datetime | None = None) -> dict[str, tuple[int, float]]:
        now = now or datetime.now(timezone.utc)
        rows = self.db.execute("SELECT purpose, COUNT(*), SUM(cost_usd) FROM api_usage WHERE substr(ts, 1, 7) = ? "
                               "GROUP BY purpose", (now.strftime("%Y-%m"),)).fetchall()
        return {p: (n, c or 0.0) for p, n, c in rows}


@dataclass(frozen=True)
class Call:
    purpose: str
    model: str
    effort: str
    system: str
    user: str
    schema: dict
    estimate_usd: float     # 调用前用来查预算的估计花费
    max_tokens: int = 16000


def ask_json(client, budget: Budget, call: Call) -> dict | None:
    if not budget.allows(call.estimate_usd):
        log.warning("本月 API 花费已到上限 %.2f 美元，%s 不调用", budget.cap, call.purpose)
        return None
    kwargs: dict = dict(
        model=call.model,
        max_tokens=call.max_tokens,
        output_config={"effort": call.effort, "format": {"type": "json_schema", "schema": call.schema}},
        system=[{"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": call.user}],
    )
    if "haiku" not in call.model:  # Haiku 没有服务端拒答兜底
        kwargs.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        response = client.beta.messages.create(**kwargs)
    except anthropic.APIError as e:
        log.warning("%s 调用失败：%s", call.purpose, e)
        return None
    except Exception:  # 例如 SDK 版本太旧不认识某个参数。模型出错只能让它“不回答”，不能让整次运行崩掉、连卖出都做不了
        log.exception("%s 调用出现意外错误，视为没有答案", call.purpose)
        return None
    budget.record(call.purpose, getattr(response, "model", None) or call.model, response.usage)
    if response.stop_reason != "end_turn":
        log.warning("%s 未正常结束（%s），视为没有答案", call.purpose, response.stop_reason)
        return None
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("%s 输出不是 JSON，视为没有答案", call.purpose)
        return None
    return data if isinstance(data, dict) else None
