"""决策引擎：纯函数，不调用任何外部服务。

规则：
1. 打分 = 报表基线预期收益 + Claude 调整 × 信心权重。
2. 新买入要求：打分 − 大盘基线 − 摊薄后的来回成本 ≥ min_excess_return。
3. 已持有的只在打分跌破“持有线”时卖出（比买入线低 rebalance_min_gain），避免来回折腾。
4. 选中的股票等权分配。估计误差很大时，等权比按分数加权更稳。
5. 金额太小、买不起一股、或调整幅度不值手续费的订单一律不下。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CostConfig, DecisionConfig, RiskConfig
from .costs import round_trip_cost_rate
from .research import ResearchView


@dataclass(frozen=True)
class Candidate:
    ticker: str
    price: float
    sector: str
    baseline_return: float | None  # None = 报表数据不全
    view: ResearchView | None  # None = Claude 没有给出可用的提案


@dataclass(frozen=True)
class Scored:
    ticker: str
    score: float
    excess: float
    amortized_cost: float
    net_excess: float
    reason: str


@dataclass(frozen=True)
class PlannedOrder:
    ticker: str
    action: str  # BUY | SELL
    shares: int
    ref_price: float
    reason: str

    @property
    def value(self) -> float:
        return self.shares * self.ref_price


def claude_weight(confidence: str, cfg: DecisionConfig) -> float:
    return {
        "low": cfg.claude_weight_low,
        "medium": cfg.claude_weight_medium,
        "high": cfg.claude_weight_high,
    }[confidence]


def score_candidate(c: Candidate, position_value: float, dcfg: DecisionConfig, ccfg: CostConfig) -> Scored | None:
    if c.baseline_return is None:
        return None
    adj = c.view.adjustment * claude_weight(c.view.confidence, dcfg) if c.view else 0.0
    score = c.baseline_return + adj
    excess = score - dcfg.baseline_annual_return
    amortized = round_trip_cost_rate(position_value, c.price, ccfg) / dcfg.holding_years
    reason = f"基线 {c.baseline_return:.2%} + Claude 调整 {adj:+.2%}（{c.view.confidence if c.view else '无'}）"
    return Scored(c.ticker, score, excess, amortized, excess - amortized, reason)


def plan(
    candidates: list[Candidate],
    holdings: dict[str, int],
    net_liquidation: float,
    dcfg: DecisionConfig,
    ccfg: CostConfig,
    rcfg: RiskConfig,
) -> tuple[list[PlannedOrder], list[Scored]]:
    """返回计划订单和全部打分（用于记录与复盘）。"""
    investable = net_liquidation * (1 - rcfg.cash_buffer)
    n_slots = rcfg.max_positions
    while n_slots > 1 and investable / n_slots < rcfg.min_order_value:
        n_slots -= 1
    slot_value = min(investable / n_slots, net_liquidation * rcfg.max_weight)

    by_ticker = {c.ticker: c for c in candidates}
    scored = [s for c in candidates if (s := score_candidate(c, slot_value, dcfg, ccfg)) is not None]
    scored.sort(key=lambda s: s.net_excess, reverse=True)
    score_of = {s.ticker: s for s in scored}

    hold_line = dcfg.min_excess_return - dcfg.rebalance_min_gain
    keep = [
        t
        for t, qty in holdings.items()
        if qty > 0 and (t not in score_of or score_of[t].excess >= hold_line)  # 没数据不等于卖出信号
    ]
    new = [s.ticker for s in scored if s.ticker not in keep and s.net_excess >= dcfg.min_excess_return]

    selected: list[str] = []
    sector_slots: dict[str, int] = {}
    max_per_sector = max(1, int(rcfg.max_sector_weight / (slot_value / net_liquidation)))
    for t in keep + new:
        if len(selected) >= n_slots:
            break
        sector = by_ticker[t].sector if t in by_ticker else "unknown"
        if t not in keep and sector_slots.get(sector, 0) >= max_per_sector:
            continue
        if t not in keep and int(slot_value // by_ticker[t].price) == 0:
            continue  # 一股都买不起
        selected.append(t)
        sector_slots[sector] = sector_slots.get(sector, 0) + 1

    orders: list[PlannedOrder] = []
    for t, qty in holdings.items():
        if qty > 0 and t not in selected:
            price = by_ticker[t].price if t in by_ticker else 0.0
            why = score_of[t].reason if t in score_of else "超出持仓数量上限"
            orders.append(PlannedOrder(t, "SELL", qty, price, f"跌破持有线：{why}"))

    for t in selected:
        c = by_ticker.get(t)
        if c is None:
            continue  # 持有但不在候选池里：不动
        target = int(slot_value // c.price)
        delta = target - holdings.get(t, 0)
        if delta == 0 or abs(delta) * c.price < rcfg.min_order_value:
            continue
        action = "BUY" if delta > 0 else "SELL"
        why = score_of[t].reason if t in score_of else "调整到目标仓位"
        orders.append(PlannedOrder(t, action, abs(delta), c.price, why))

    orders.sort(key=lambda o: o.action != "SELL")  # 先卖后买，释放现金
    return orders, scored
