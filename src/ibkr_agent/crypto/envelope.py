"""慢思考：Opus 每周给每个币定一次“边界”。

Opus 只能输出这几样，而且都会被代码裁剪：
- stance：add（可以加仓）/ hold（按配置目标正常再平衡）/ reduce_only（本周不买）
- tilt：买入目标的倾斜，最多 ±crypto.ai.max_tilt；只有 stance=add 且信心不是 low 时才采用
- 买入价区间 [buy_low, buy_high]：最多离现价 ±crypto.ai.max_band；价格在区间外不买
- 失效条件：只接受代码能检查的两种：price_below（价格跌破）和 drop_7d_above（7 天跌幅超过）
  任何一条成立就停止买入并告警，等下次 Opus 重新评估。

Opus 不决定下单金额、不决定什么时候下单，也不能放宽风控。卖出完全不看边界：
卖不卖、卖多少只按配置目标、再平衡带和单币上限算，倾斜只改“买到多少为止”。
被拒、被裁剪、被丢掉的内容都记日志，`crypto envelope` 的输出里也会列出来。
"""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .config import CryptoSettings
from .llm import Budget, Call, ask_json, usd
from .market import WEEK_DAYS, annualized_vol, drawdown_from_high, period_return, vs_average

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS crypto_envelopes (
    ts TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    account TEXT NOT NULL,
    asset TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""

STANCES = ("add", "hold", "reduce_only")
CONFIDENCE = ("low", "medium", "high")
INVALIDATION_KINDS = ("price_below", "drop_7d_above")

SYSTEM_PROMPT = """你是一个小额加密货币组合的每周风险评审员。组合只做 BTC、ETH 现货，长期持有，按目标比例再平衡。
账户在德国，持有满一年卖出免税，所以系统偏好低换手。

你每周给每个币定一次“边界”，之后一周由代码和一个快速哨兵模型照边界执行。你不决定金额、不决定下单时机。

要求：
- 只依据给出的数据。数据里列为 missing 的内容你看不到，不要假装知道（例如新闻、链上数据）。
- 先写多方理由，再写同样认真的空方理由，最后给结论。
- stance：add = 可以按（倾斜后的）目标买入；hold = 正常再平衡；reduce_only = 本周不买入。
- tilt：对买入目标比例的倾斜（小数，0.05 = 5 个百分点），只在 stance 为 add 时采用。
  它只改变“买到多少为止”，不会让系统卖出：卖出永远按配置目标。没有充分理由时给 0。
- buy_low / buy_high：本周允许买入的价格区间（计价货币）。区间要包含现价，除非你认为现价不该买。
- invalidation：让本周判断失效的条件，只能用两种：
  price_below（价格跌破某个值）和 drop_7d_above（7 天跌幅超过某个比例，0.15 = 15%）。
  每条写清楚为什么。条件成立时系统会停止买入并请你重新评估。
- confidence：low / medium / high。low 时系统不会采用你的 tilt。"""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "assets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "asset": {"type": "string"},
                    "bull_case": {"type": "string"},
                    "bear_case": {"type": "string"},
                    "stance": {"type": "string", "enum": list(STANCES)},
                    "tilt": {"type": "number"},
                    "buy_low": {"type": "number"},
                    "buy_high": {"type": "number"},
                    "invalidation": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "kind": {"type": "string", "enum": list(INVALIDATION_KINDS)},
                                "value": {"type": "number"},
                                "why": {"type": "string"},
                            },
                            "required": ["kind", "value", "why"],
                            "additionalProperties": False,
                        },
                    },
                    "confidence": {"type": "string", "enum": list(CONFIDENCE)},
                },
                "required": ["asset", "bull_case", "bear_case", "stance", "tilt", "buy_low", "buy_high",
                             "invalidation", "confidence"],
                "additionalProperties": False,
            },
        },
        "summary": {"type": "string"},
    },
    "required": ["assets", "summary"],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class Invalidation:
    kind: str
    value: float
    why: str


@dataclass(frozen=True)
class Envelope:
    asset: str
    stance: str
    tilt: float
    buy_low: float
    buy_high: float
    invalidation: tuple[Invalidation, ...]
    confidence: str
    created: str
    valid_until: str
    rationale: str

    def effective_tilt(self) -> float:
        """实际采用的倾斜：只有 stance=add 且信心不是 low 时才用。"""
        return self.tilt if self.stance == "add" and self.confidence != "low" else 0.0


# ---------- 给 Opus 的事实材料 ----------

def facts(asset: str, closes: list[float], price: float, weight: float, target: float) -> dict:
    """只放能从行情算出来的事实；数据不够算的项是 null，看不到的东西列进 missing。"""
    return {
        "asset": asset,
        "price": price,
        "return_7d": period_return(closes, price, WEEK_DAYS),
        "return_30d": period_return(closes, price, 30),
        "return_90d": period_return(closes, price, 90),
        "annualized_vol_30d": annualized_vol(closes, 30),
        "drawdown_from_90d_high": drawdown_from_high(closes, price, 90),
        "price_vs_50d_average": vs_average(closes, price, 50),
        "current_weight": round(weight, 4),
        "configured_target": target,
        "missing": ["新闻", "链上数据", "资金费率和持仓量", "宏观事件日历"],
    }


# ---------- 解析和裁剪 ----------

def _number(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _valid_condition(kind, value: float | None) -> bool:
    if value is None:
        return False
    return (kind == "price_below" and value > 0) or (kind == "drop_7d_above" and 0 < value < 1)


def clamp(raw: dict, price: float, s: CryptoSettings, now: datetime, notes: list[str] | None = None) -> Envelope | None:
    """把 Opus 对一个币的回答裁剪成能执行的边界；整条不能用时返回 None。

    整条被拒、倾斜或区间被裁剪、失效条件被丢掉，都会记日志，并把一句给人看的说明加进 notes。"""
    notes = [] if notes is None else notes
    asset = str(raw.get("asset", "")).upper() or "（没写币种）"

    def reject(why: str) -> None:
        log.warning("Opus 给 %s 的边界不能用：%s", asset, why)
        notes.append(f"{asset} 的边界不能用：{why}")

    stance, confidence = raw.get("stance"), raw.get("confidence")
    if stance not in STANCES:
        return reject(f"stance={stance!r} 不是 {'/'.join(STANCES)} 之一")
    if confidence not in CONFIDENCE:
        return reject(f"confidence={confidence!r} 不是 {'/'.join(CONFIDENCE)} 之一")
    tilt_raw, lo_raw, hi_raw = (_number(raw.get(k)) for k in ("tilt", "buy_low", "buy_high"))
    if tilt_raw is None or lo_raw is None or hi_raw is None:
        return reject(f"tilt、buy_low、buy_high 要是数字，收到 {raw.get('tilt')!r}、{raw.get('buy_low')!r}、{raw.get('buy_high')!r}")
    if lo_raw > hi_raw:
        return reject(f"买入区间 {lo_raw:,.2f}–{hi_raw:,.2f} 上下颠倒")

    tilt = max(-s.ai.max_tilt, min(s.ai.max_tilt, tilt_raw))
    if tilt != tilt_raw:
        log.warning("Opus 给 %s 的倾斜 %+.2f 超过上限 ±%.2f，按 %+.2f", asset, tilt_raw, s.ai.max_tilt, tilt)
        notes.append(f"{asset} 倾斜 {tilt_raw:+.0%} 超过上限 ±{s.ai.max_tilt:.0%}，按 {tilt:+.0%}")
    lo_cap, hi_cap = price * (1 - s.ai.max_band), price * (1 + s.ai.max_band)
    lo, hi = max(lo_cap, min(hi_cap, lo_raw)), max(lo_cap, min(hi_cap, hi_raw))
    if (lo, hi) != (lo_raw, hi_raw):
        log.warning("Opus 给 %s 的买入区间 %.2f–%.2f 超出现价 ±%.0f%%，按 %.2f–%.2f",
                    asset, lo_raw, hi_raw, s.ai.max_band * 100, lo, hi)
        notes.append(f"{asset} 买入区间 {lo_raw:,.2f}–{hi_raw:,.2f} 超出现价 ±{s.ai.max_band:.0%}，按 {lo:,.2f}–{hi:,.2f}")

    inv = []
    for c in raw.get("invalidation") or []:
        kind, value = c.get("kind"), _number(c.get("value"))
        if _valid_condition(kind, value):
            inv.append(Invalidation(kind, value, str(c.get("why", ""))))
        else:
            log.warning("Opus 给 %s 的失效条件 %s %r 无效，不采用", asset, kind, c.get("value"))
            notes.append(f"{asset} 的失效条件 {kind} {c.get('value')} 无效，不采用")
    rationale = f"多：{raw.get('bull_case', '')}\n空：{raw.get('bear_case', '')}"
    return Envelope(asset, stance, tilt, lo, hi, tuple(inv), confidence, now.isoformat(),
                    (now + timedelta(days=s.ai.envelope_valid_days)).isoformat(), rationale)


# ---------- 存取 ----------

class EnvelopeStore:
    def __init__(self, db: sqlite3.Connection, account: str):
        self.db = db
        self.account = account
        db.executescript(SCHEMA)

    def save(self, e: Envelope) -> None:
        self.db.execute("INSERT INTO crypto_envelopes VALUES (?,?,?,?,?)",
                        (e.created, e.valid_until, self.account, e.asset, json.dumps(asdict(e), ensure_ascii=False)))
        self.db.commit()

    def current(self, asset: str, now: datetime) -> Envelope | None:
        row = self.db.execute(
            "SELECT payload FROM crypto_envelopes WHERE account = ? AND asset = ? AND valid_until > ? "
            "ORDER BY ts DESC, rowid DESC LIMIT 1", (self.account, asset, now.isoformat())).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        d["invalidation"] = tuple(Invalidation(**i) for i in d["invalidation"])
        return Envelope(**d)


# ---------- 执行时用到的纯函数 ----------

def tilted_targets(base: dict[str, float], envelopes: dict[str, Envelope], max_weight: float,
                   max_tilt: float) -> dict[str, float]:
    """买入用的目标：配置目标加上 Opus 采用的倾斜，限制在 [0, 单币上限]。

    只用于决定“买到多少为止”，卖出始终按配置目标。没有边界的币保持配置目标，不被别的币的倾斜挤占；
    总和可以略超过 1，买入金额本来就受可用现金和每次买入上限约束。
    倾斜按现在的 max_tilt 再裁一次：调小 max_tilt 对已经存下的边界立即生效。"""

    def tilt(a: str) -> float:
        return max(-max_tilt, min(max_tilt, envelopes[a].effective_tilt())) if a in envelopes else 0.0

    return {a: max(0.0, min(max_weight, w + tilt(a))) for a, w in base.items()}


def triggered(e: Envelope, price: float, price_7d_ago: float | None) -> list[Invalidation]:
    hit = []
    for c in e.invalidation:
        if c.kind == "price_below" and price < c.value:
            hit.append(c)
        elif c.kind == "drop_7d_above" and price_7d_ago and (price_7d_ago - price) / price_7d_ago > c.value:
            hit.append(c)
    return hit


def buy_block(e: Envelope, price: float, price_7d_ago: float | None) -> str | None:
    """返回不能买的原因；None 表示边界允许买。检查不了的条件按不买处理。"""
    if e.stance == "reduce_only":
        return "Opus 本周定为只减不买"
    if price_7d_ago is None and any(c.kind == "drop_7d_above" for c in e.invalidation):
        return "拿不到 7 天前的价格，没法检查“7 天跌幅”失效条件，不买"
    if hits := triggered(e, price, price_7d_ago):
        return "失效条件已触发：" + "；".join(f"{h.kind} {h.value:g}（{h.why}）" for h in hits)
    if not e.buy_low <= price <= e.buy_high:
        return f"价格 {price:.2f} 不在本周买入区间 {e.buy_low:.2f}–{e.buy_high:.2f}"
    return None


# ---------- 调用 Opus ----------

def review(client, budget: Budget, s: CryptoSettings, dossiers: list[dict],
           now: datetime) -> tuple[list[Envelope], str, list[str]]:
    """一次调用评估所有币（共享上下文更便宜）。返回（边界, 总结, 给人看的说明）。

    说明里写清楚：没拿到答案的原因、被忽略的币、被裁剪或丢掉的内容、哪些币这次按配置目标。"""
    ai = s.ai
    answer = ask_json(client, budget, Call(
        purpose="envelope",
        model=ai.envelope_model,
        effort=ai.envelope_effort,
        system=SYSTEM_PROMPT,
        user="本周数据（JSON，价格单位 " + s.quote + "）：\n" + json.dumps(dossiers, ensure_ascii=False),
        schema=OUTPUT_SCHEMA,
        max_tokens=ai.envelope_max_tokens,
        timeout_s=ai.envelope_timeout_seconds,
        max_retries=ai.envelope_max_retries,
    ), now)
    if answer.data is None:
        return [], "", [f"Opus 没有给出边界：{answer.error}"]
    prices = {d["asset"]: d["price"] for d in dossiers}
    notes: list[str] = []
    envs: dict[str, Envelope] = {}
    for raw in answer.data.get("assets") or []:
        asset = str(raw.get("asset", "")).upper() if isinstance(raw, dict) else ""
        if asset not in prices:
            log.warning("Opus 返回了不在目标里的币 %r，忽略", raw.get("asset") if isinstance(raw, dict) else raw)
            notes.append(f"Opus 返回了不在目标里的 {asset or '（没写币种）'}，忽略")
        elif asset in envs:
            log.warning("Opus 给 %s 返回了不止一条边界，只用第一条", asset)
            notes.append(f"Opus 给 {asset} 返回了不止一条边界，只用第一条")
        elif e := clamp(raw, prices[asset], s, now, notes):
            envs[asset] = e
    notes += [f"{a} 这次没有拿到可用的边界，运行时按配置目标" for a in prices if a not in envs]
    return list(envs.values()), str(answer.data.get("summary", "")), notes


def weekly(s: CryptoSettings, ex, db: sqlite3.Connection, account: str, client, now: datetime,
           out=print) -> list[Envelope]:
    """`ibkr-agent crypto envelope`：取数据 → 问 Opus → 裁剪 → 存起来。每周跑一次。

    取数据失败会抛出（命令以失败结束、记日志），上周的边界在过期前照常有效。"""
    from .plan import weights

    if not s.targets:
        out("crypto.targets 是空的，没有要评估的币。")
        return []
    days = s.ai.envelope_history_days
    out(f"正在取 {', '.join(s.targets)} 的余额、盘口和最近 {days} 天日线…")
    bal = ex.balances()
    closes, prices = {}, {}
    for a in s.targets:
        closes[a] = ex.daily_closes(s.symbol(a), days)
        prices[a] = ex.book(s.symbol(a)).mid
    holdings = {a: bal.get(a, 0.0) for a in s.targets}
    _, ws = weights(prices, holdings, bal.get(s.quote, 0.0), s.targets)
    w = {x.ticker: x.weight for x in ws}
    dossiers = [facts(a, closes[a], prices[a], w.get(a, 0.0), s.targets[a]) for a in s.targets]
    budget = Budget(db, s.ai.monthly_budget_usd, s.ai.prices)
    out(f"正在请 {s.ai.envelope_model} 评估（通常要一两分钟）…")
    envs, summary, notes = review(client, budget, s, dossiers, now)
    store = EnvelopeStore(db, account)
    for e in envs:
        store.save(e)
        if e.effective_tilt() == e.tilt:
            tilt = f"买入目标倾斜 {e.tilt:+.0%}"
        else:
            tilt = f"倾斜 {e.tilt:+.0%} 不采用（{'信心低' if e.confidence == 'low' else '立场不是 add'}）"
        out(f"{e.asset}：{e.stance}，{tilt}，信心 {e.confidence}，"
            f"买入区间 {e.buy_low:.2f}–{e.buy_high:.2f} {s.quote}，有效到 {e.valid_until[:10]}")
        for c in e.invalidation:
            out(f"  失效条件：{c.kind} {c.value:g}（{c.why}）")
        out("  " + e.rationale.replace("\n", "\n  "))
    for n in notes:
        out("· " + n)
    if summary:
        out("总结：" + summary)
    if not envs:
        out("这次没有存下任何边界。执行层按配置目标运行（上周的边界在过期前仍然有效）。")
    out(f"本月 API 花费 {usd(budget.spent_this_month(now))} / {usd(budget.cap)}")
    return envs
