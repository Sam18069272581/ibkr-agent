"""币圈部分调用 Claude 的唯一入口：先查月度预算，再调用，按 usage 记账。

任何失败（超预算、报错、超时、拒答、截断、格式不对）都返回没有 data 的 Answer，error 里写明原因，
同时记一条日志。调用方必须把它当成“没有答案”：边界没有就按配置目标走；快哨兵没答就不买。
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import anthropic

from .config import CryptoAI

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_usage (
    ts TEXT NOT NULL,
    purpose TEXT NOT NULL,          -- envelope | gate
    model TEXT NOT NULL,            -- 服务端换过模型时写成“原模型→兜底模型”
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL
);
"""

# 支持服务端拒答兜底（被拒时由服务端换一个模型重答）的模型。其他模型（例如 Haiku）加这个参数会报错。
SERVER_FALLBACK_MODELS = frozenset({"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"})
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# 估算输入 token 的上限：中文大约一个字一个 token，英文和 JSON 更省。按每个字符 2 个 token 算，只会高估。
INPUT_TOKENS_PER_CHAR_UPPER = 2

Prices = dict[str, tuple[float, float, float, float]]


def usd(x: float) -> str:
    """花费的显示：小于 1 美分时显示到四位小数，不然 Haiku 的调用全是 $0.00。"""
    return f"${x:.4f}" if 0 < x < 0.01 else f"${x:.2f}"


def uses_fallback(model: str) -> bool:
    return model in SERVER_FALLBACK_MODELS


def max_prices(prices: Prices) -> tuple[float, float, float, float]:
    """价目表里每一列的最高价。"""
    i, o, w, r = (max(p[k] for p in prices.values()) for k in range(4))
    return i, o, w, r


def price_of(model: str, prices: Prices) -> tuple[float, float, float, float]:
    """模型的价格。带日期后缀的型号（claude-haiku-5-5-2026…）按最长的前缀匹配。
    价目表里没有的模型按表里每一列的最高价估算并告警：宁可高估，让预算上限更早生效。"""
    if model in prices:
        return prices[model]
    prefix = max((k for k in prices if model.startswith(k + "-")), key=len, default=None)
    if prefix:
        return prices[prefix]
    log.warning("价目表（crypto.ai.prices）里没有 %s，按表里最贵的价格估算花费", model)
    return max_prices(prices)


def _tokens(u) -> tuple[int, int, int, int]:
    """usage 里的四种 token 数：输入、输出、写缓存、读缓存。"""
    i, o, w, r = (int(getattr(u, k, 0) or 0)
                  for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return i, o, w, r


def cost_usd(prices: Prices, model: str, input_tokens: int, output_tokens: int,
             cache_write: int = 0, cache_read: int = 0) -> float:
    i, o, w, r = price_of(model, prices)
    return (input_tokens * i + output_tokens * o + cache_write * w + cache_read * r) / 1_000_000


class Budget:
    def __init__(self, db: sqlite3.Connection, monthly_cap_usd: float, prices: Prices | None = None):
        """prices 不给时用默认价目表（只看花费、不调用时可以不给）。"""
        self.db = db
        self.cap = monthly_cap_usd
        self.prices = prices if prices is not None else CryptoAI().prices
        db.executescript(SCHEMA)

    def spent_this_month(self, now: datetime | None = None) -> float:
        now = now or datetime.now(timezone.utc)
        row = self.db.execute("SELECT SUM(cost_usd) FROM api_usage WHERE substr(ts, 1, 7) = ?",
                              (now.strftime("%Y-%m"),)).fetchone()
        return row[0] or 0.0

    def allows(self, estimate: float, now: datetime | None = None) -> bool:
        return self.spent_this_month(now) + estimate <= self.cap

    def record(self, purpose: str, model: str, usage, now: datetime | None = None) -> float:
        """记一次调用的花费，返回美元数。

        服务端换模型重答时，usage 顶层只是最后那次的用量；被拒的那次也要付钱。
        所以有 usage.iterations 时按每一段各自的模型和用量加总。"""
        parts = list(getattr(usage, "iterations", None) or []) or [usage]
        models: list[str] = []
        totals = [0, 0, 0, 0]
        cost = 0.0
        for part in parts:
            m = getattr(part, "model", None) or model
            t = _tokens(part)
            cost += cost_usd(self.prices, m, *t)
            totals = [a + b for a, b in zip(totals, t, strict=True)]
            if m not in models:
                models.append(m)
        self.db.execute("INSERT INTO api_usage VALUES (?,?,?,?,?,?,?,?)",
                        ((now or datetime.now(timezone.utc)).isoformat(), purpose, "→".join(models), *totals, cost))
        self.db.commit()
        return cost

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
    max_tokens: int
    timeout_s: float        # 单次请求最多等多久
    max_retries: int        # SDK 对超时、限流、5xx 的自动重试次数


@dataclass(frozen=True)
class Answer:
    data: dict | None
    error: str = ""         # 没有 data 时的原因，给人看


def worst_case_usd(call: Call, prices: Prices) -> float:
    """调用前查预算用的上限：输入按每字符 2 个 token、按写缓存和输入里贵的价算；输出按 max_tokens 算满。
    开了服务端兜底时，兜底那次按价目表里最贵的价再算一遍。"""
    chars = len(call.system) + len(call.user) + len(json.dumps(call.schema, ensure_ascii=False))
    tokens_in = chars * INPUT_TOKENS_PER_CHAR_UPPER

    def one(p: tuple[float, float, float, float]) -> float:
        return (tokens_in * max(p[0], p[2]) + call.max_tokens * p[1]) / 1_000_000

    worst = one(price_of(call.model, prices))
    if uses_fallback(call.model):
        worst += one(max_prices(prices))
    return worst


def _no_answer(call: Call, why: str) -> Answer:
    log.warning("%s（%s）没有答案：%s", call.purpose, call.model, why)
    return Answer(None, why)


def ask_json(client, budget: Budget, call: Call, now: datetime | None = None) -> Answer:
    estimate = worst_case_usd(call, budget.prices)
    if not budget.allows(estimate, now):
        return _no_answer(call, f"本月 API 已花 {usd(budget.spent_this_month(now))}，这次最多 {usd(estimate)}，"
                                f"会超过上限 {usd(budget.cap)}，不调用")
    kwargs: dict = dict(
        model=call.model,
        max_tokens=call.max_tokens,
        output_config={"effort": call.effort, "format": {"type": "json_schema", "schema": call.schema}},
        system=[{"type": "text", "text": call.system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": call.user}],
    )
    if uses_fallback(call.model):
        kwargs.update(betas=[FALLBACK_BETA], fallbacks="default")
    try:
        response = (client.with_options(timeout=call.timeout_s, max_retries=call.max_retries)
                    .beta.messages.create(**kwargs))
    except anthropic.APIError as e:  # 包括超时、限流、5xx（SDK 已按 max_retries 重试过）
        return _no_answer(call, f"调用失败：{e}")
    except Exception as e:  # 例如没配 key、SDK 太旧不认识参数。只能让它“不回答”，不能让整次运行崩掉、连卖出都做不了
        log.exception("%s 调用出现意外错误", call.purpose)
        return Answer(None, f"意外错误：{e!r}")
    budget.record(call.purpose, getattr(response, "model", None) or call.model, response.usage, now)
    if response.stop_reason != "end_turn":
        return _no_answer(call, f"没有正常结束（stop_reason={response.stop_reason}）")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except ValueError:
        return _no_answer(call, f"输出不是 JSON：{text[:80]!r}")
    if not isinstance(data, dict):
        return _no_answer(call, f"输出是 {type(data).__name__}，不是 JSON 对象")
    return Answer(data)
