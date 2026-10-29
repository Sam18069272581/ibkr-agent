"""基于报表的基线预期收益。

刻意保持简单、可解释：长期年化收益 ≈ 自由现金流收益率 + 可持续增长率 − 杠杆惩罚。
这是一个可以被替换的起点，不是结论；替换时务必先回测。
"""

from __future__ import annotations

from dataclasses import dataclass

from .fundamentals import Fundamentals

GROWTH_FLOOR = -0.05
GROWTH_CAP = 0.08
HIGH_LEVERAGE = 2.0
LEVERAGE_PENALTY = 0.01


@dataclass(frozen=True)
class Valuation:
    ticker: str
    fcf_yield: float
    growth: float
    leverage_penalty: float

    @property
    def expected_return(self) -> float:
        return self.fcf_yield + self.growth - self.leverage_penalty


def value(f: Fundamentals, market_cap: float) -> Valuation | None:
    """数据不全时返回 None，宁可不交易也不猜。"""
    if market_cap <= 0 or f.free_cash_flow is None or f.revenue_growth is None:
        return None
    fcf_yield = f.free_cash_flow / market_cap
    growth = min(max(f.revenue_growth, GROWTH_FLOOR), GROWTH_CAP)
    penalty = (
        LEVERAGE_PENALTY
        if f.debt_to_equity is not None and f.debt_to_equity > HIGH_LEVERAGE
        else 0.0
    )
    return Valuation(f.ticker, fcf_yield, growth, penalty)
