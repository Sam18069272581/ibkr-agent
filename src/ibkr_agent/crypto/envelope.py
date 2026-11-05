"""慢思考：Opus 每周给每个币定一次“边界”。

Opus 只能输出这几样，而且都会被代码裁剪：
- stance：add（可以加仓）/ hold（按配置目标正常再平衡）/ reduce_only（本周不买）
- tilt：目标比例的倾斜，最多 ±crypto.ai.max_tilt；信心 low 时按 0 处理
- 买入价区间 [buy_low, buy_high]：最多离现价 ±crypto.ai.max_band；价格在区间外不买
- 失效条件：只接受代码能检查的两种：price_below（价格跌破）和 drop_7d_above（7 天跌幅超过）
  任何一条成立就停止买入并告警，等下次 Opus 重新评估。

Opus 不决定下单金额、不决定什么时候下单，也不能放宽风控。卖出完全不看边界。
"""

from __future__ import annotations

import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta

from .config import CryptoSettings
from .llm import Budget, Call, ask_json, usd

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
- tilt：对配置目标比例的倾斜（小数，0.05 = 5 个百分点）。没有充分理由时给 0。
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
        return 0.0 if self.confidence == "low" else self.tilt


# ---------- 给 Opus 的事实材料 ----------

def facts(asset: str, closes: list[float], price: float, weight: float, target: float) -> dict:
    """只放能从行情算出来的事实；看不到的东西列进 missing。"""

    def ret(days: int) -> float | None:
        return round(price / closes[-1 - days] - 1, 4) if len(closes) > days and closes[-1 - days] > 0 else None

    daily = [math.log(b / a) for a, b in zip(closes[-31:-1], closes[-30:]) if a > 0 and b > 0]
    vol = None
    if len(daily) >= 10:
        m = sum(daily) / len(daily)
        vol = round(math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1)) * math.sqrt(365), 4)
    hi90 = max(closes[-90:]) if closes else None
    ma50 = sum(closes[-50:]) / len(closes[-50:]) if len(closes) >= 50 else None
    return {
        "asset": asset,
        "price": price,
        "return_7d": ret(7),
        "return_30d": ret(30),
        "return_90d": ret(90),
        "annualized_vol_30d": vol,
        "drawdown_from_90d_high": round(price / hi90 - 1, 4) if hi90 else None,
        "price_vs_50d_average": round(price / ma50 - 1, 4) if ma50 else None,
        "current_weight": round(weight, 4),
        "configured_target": target,
        "missing": ["新闻", "链上数据", "资金费率和持仓量", "宏观事件日历"],
    }


# ---------- 解析和裁剪 ----------

def clamp(raw: dict, price: float, s: CryptoSettings, now: datetime) -> Envelope | None:
    try:
        stance = raw["stance"]
        confidence = raw["confidence"]
        if stance not in STANCES or confidence not in CONFIDENCE:
            return None
        tilt = max(-s.ai.max_tilt, min(s.ai.max_tilt, float(raw["tilt"])))
        lo_cap, hi_cap = price * (1 - s.ai.max_band), price * (1 + s.ai.max_band)
        lo = max(lo_cap, min(hi_cap, float(raw["buy_low"])))
        hi = max(lo_cap, min(hi_cap, float(raw["buy_high"])))
        if lo > hi:
            return None
        inv = []
        for c in raw.get("invalidation") or []:
            kind, value = c["kind"], float(c["value"])
            if kind == "price_below" and value > 0:
                inv.append(Invalidation(kind, value, str(c.get("why", ""))))
            elif kind == "drop_7d_above" and 0 < value < 1:
                inv.append(Invalidation(kind, value, str(c.get("why", ""))))
        rationale = f"多：{raw.get('bull_case', '')}\n空：{raw.get('bear_case', '')}"
    except (KeyError, TypeError, ValueError):
        return None
    return Envelope(str(raw["asset"]).upper(), stance, tilt, lo, hi, tuple(inv), confidence, now.isoformat(),
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
            "ORDER BY ts DESC LIMIT 1", (self.account, asset, now.isoformat())).fetchone()
        if not row:
            return None
        d = json.loads(row[0])
        d["invalidation"] = tuple(Invalidation(**i) for i in d["invalidation"])
        return Envelope(**d)


# ---------- 执行时用到的纯函数 ----------

def tilted_targets(base: dict[str, float], envelopes: dict[str, Envelope], max_weight: float) -> dict[str, float]:
    """目标比例加上 Opus 的倾斜，限制在 [0, 单币上限]，总和超过 1 时按比例缩小。"""
    t = {a: max(0.0, min(max_weight, w + (envelopes[a].effective_tilt() if a in envelopes else 0.0)))
         for a, w in base.items()}
    total = sum(t.values())
    return {a: w / total for a, w in t.items()} if total > 1 else t


def triggered(e: Envelope, price: float, price_7d_ago: float | None) -> list[Invalidation]:
    hit = []
    for c in e.invalidation:
        if c.kind == "price_below" and price < c.value:
            hit.append(c)
        elif c.kind == "drop_7d_above" and price_7d_ago and (price_7d_ago - price) / price_7d_ago > c.value:
            hit.append(c)
    return hit


def buy_block(e: Envelope, price: float, price_7d_ago: float | None) -> str | None:
    """返回不能买的原因；None 表示边界允许买。"""
    if e.stance == "reduce_only":
        return "Opus 本周定为只减不买"
    if hits := triggered(e, price, price_7d_ago):
        return "失效条件已触发：" + "；".join(f"{h.kind} {h.value:g}（{h.why}）" for h in hits)
    if not e.buy_low <= price <= e.buy_high:
        return f"价格 {price:.2f} 不在本周买入区间 {e.buy_low:.2f}–{e.buy_high:.2f}"
    return None


# ---------- 调用 Opus ----------

def review(client, budget: Budget, s: CryptoSettings, dossiers: list[dict], now: datetime) -> tuple[list[Envelope], str]:
    """一次调用评估所有币（共享上下文更便宜）。失败返回空列表，执行层按配置目标运行。"""
    data = ask_json(client, budget, Call(
        purpose="envelope",
        model=s.ai.envelope_model,
        effort=s.ai.envelope_effort,
        system=SYSTEM_PROMPT,
        user="本周数据（JSON，价格单位 " + s.quote + "）：\n" + json.dumps(dossiers, ensure_ascii=False),
        schema=OUTPUT_SCHEMA,
        estimate_usd=0.40,
    ))
    if data is None:
        return [], ""
    prices = {d["asset"]: d["price"] for d in dossiers}
    out = []
    for raw in data.get("assets") or []:
        asset = str(raw.get("asset", "")).upper()
        if asset in prices and (e := clamp(raw, prices[asset], s, now)):
            out.append(e)
    return out, str(data.get("summary", ""))


def weekly(s: CryptoSettings, ex, db: sqlite3.Connection, account: str, client, now: datetime,
           out=print) -> list[Envelope]:
    """`ibkr-agent crypto envelope`：取数据 → 问 Opus → 裁剪 → 存起来。每周跑一次。"""
    from .plan import weights

    bal = ex.balances()
    closes, prices = {}, {}
    for a in s.targets:
        closes[a] = ex.daily_closes(s.symbol(a), 100)
        prices[a] = ex.book(s.symbol(a)).mid
    holdings = {a: bal.get(a, 0.0) for a in s.targets}
    _, ws = weights(prices, holdings, bal.get(s.quote, 0.0), s.targets)
    w = {x.ticker: x.weight for x in ws}
    dossiers = [facts(a, closes[a], prices[a], w.get(a, 0.0), s.targets[a]) for a in s.targets]
    budget = Budget(db, s.ai.monthly_budget_usd)
    envs, summary = review(client, budget, s, dossiers, now)
    if not envs:
        out("Opus 没有给出可用的边界（可能是超预算、调用失败或格式不对）。执行层继续按配置目标运行。")
        return []
    store = EnvelopeStore(db, account)
    for e in envs:
        store.save(e)
        out(f"{e.asset}：{e.stance}，倾斜 {e.tilt:+.0%}（信心 {e.confidence}"
            + ("，信心低不采用倾斜" if e.confidence == "low" else "") + "），"
            f"买入区间 {e.buy_low:.2f}–{e.buy_high:.2f} {s.quote}，有效到 {e.valid_until[:10]}")
        for c in e.invalidation:
            out(f"  失效条件：{c.kind} {c.value:g}（{c.why}）")
        out("  " + e.rationale.replace("\n", "\n  "))
    if summary:
        out("总结：" + summary)
    out(f"本月 API 花费 {usd(budget.spent_this_month(now))} / {usd(budget.cap)}")
    return envs
