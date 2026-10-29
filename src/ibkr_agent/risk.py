"""风控层：最后一道关。纯函数，任何一条不通过就拒单，并给出原因。

这一层不读模型输出，只看订单、账户和报价。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import CostConfig, RiskConfig
from .costs import commission
from .decision import PlannedOrder


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        return (self.ask - self.bid) / self.mid * 10_000


@dataclass(frozen=True)
class AccountState:
    net_liquidation: float  # 美元
    usd_cash: float  # 可用美元现金（不允许用融资买入）
    high_water_mark: float
    position_values: dict[str, float]  # 美元市值


@dataclass(frozen=True)
class ApprovedOrder:
    ticker: str
    action: str
    shares: int
    limit_price: float
    reason: str


@dataclass(frozen=True)
class Rejection:
    ticker: str
    action: str
    why: str


def limit_price(action: str, q: Quote, offset_bps: float) -> float:
    off = q.mid * offset_bps / 10_000
    if action == "BUY":
        return round(min(q.ask, q.mid + off), 2)
    return round(max(q.bid, q.mid - off), 2)


def check(
    orders: list[PlannedOrder],
    quotes: dict[str, Quote],
    account: AccountState,
    sectors: dict[str, str],
    rcfg: RiskConfig,
    ccfg: CostConfig,
) -> tuple[list[ApprovedOrder], list[Rejection]]:
    if Path(rcfg.kill_switch_file).exists():
        return [], [Rejection(o.ticker, o.action, "总开关已触发") for o in orders]

    approved: list[ApprovedOrder] = []
    rejected: list[Rejection] = []
    nl = account.net_liquidation
    halted = nl < account.high_water_mark * (1 - rcfg.drawdown_halt)
    cash_left = account.usd_cash - nl * rcfg.cash_buffer
    values = dict(account.position_values)

    def sector_value(sector: str) -> float:
        return sum(v for t, v in values.items() if sectors.get(t, "unknown") == sector)

    for o in orders:
        if len(approved) >= rcfg.max_orders_per_run:
            rejected.append(Rejection(o.ticker, o.action, "超过单次运行订单数上限"))
            continue
        q = quotes.get(o.ticker)
        if q is None or q.bid <= 0 or q.ask <= q.bid:
            rejected.append(Rejection(o.ticker, o.action, "没有有效报价"))
            continue
        if q.spread_bps > rcfg.max_quote_spread_bps:
            rejected.append(Rejection(o.ticker, o.action, f"买卖价差 {q.spread_bps:.0f}bp 过大"))
            continue

        px = limit_price(o.action, q, rcfg.limit_offset_bps)
        value = o.shares * px

        if o.action == "SELL":
            values[o.ticker] = max(0.0, values.get(o.ticker, 0.0) - value)
            approved.append(ApprovedOrder(o.ticker, o.action, o.shares, px, o.reason))
            continue  # 卖出所得要等结算，不计入本次可用现金

        if halted:
            rejected.append(Rejection(o.ticker, o.action, "回撤熔断中，禁止开新仓"))
            continue
        if value < rcfg.min_order_value:
            rejected.append(Rejection(o.ticker, o.action, f"订单金额 {value:.0f} 低于下限"))
            continue
        cost = value + commission(o.shares, px, ccfg)
        if cost > cash_left:
            rejected.append(Rejection(o.ticker, o.action, f"美元现金不足（需要 {cost:.0f}，可用 {cash_left:.0f}）"))
            continue
        if (values.get(o.ticker, 0.0) + value) / nl > rcfg.max_weight:
            rejected.append(Rejection(o.ticker, o.action, "超过单只股票仓位上限"))
            continue
        sector = sectors.get(o.ticker, "unknown")
        if (sector_value(sector) + value) / nl > rcfg.max_sector_weight:
            rejected.append(Rejection(o.ticker, o.action, f"超过行业 {sector} 仓位上限"))
            continue

        cash_left -= cost
        values[o.ticker] = values.get(o.ticker, 0.0) + value
        approved.append(ApprovedOrder(o.ticker, o.action, o.shares, px, o.reason))

    return approved, rejected
