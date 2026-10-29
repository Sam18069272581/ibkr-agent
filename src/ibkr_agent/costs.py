"""交易成本模型：佣金 + 半个点差 + 滑点。全部以美元计。"""

from __future__ import annotations

import math

from .config import CostConfig


def commission(shares: int, price: float, cfg: CostConfig) -> float:
    """IBKR Pro 固定费率的近似：每股费用，单笔最低收费，封顶为成交额的一定比例。"""
    if shares <= 0:
        return 0.0
    value = shares * price
    fee = max(cfg.per_share * shares, cfg.min_per_order)
    return min(fee, cfg.max_pct_of_value * value)


def one_way_cost(shares: int, price: float, cfg: CostConfig) -> float:
    """单边总成本：佣金 + 半个点差 + 滑点。"""
    if shares <= 0:
        return 0.0
    value = shares * price
    market_impact = value * (cfg.spread_bps + cfg.slippage_bps) / 10_000
    return commission(shares, price, cfg) + market_impact


def round_trip_cost_rate(value: float, price: float, cfg: CostConfig) -> float:
    """买入再卖出这笔仓位的总成本占仓位金额的比例。"""
    if value <= 0 or price <= 0:
        return math.inf
    shares = int(value // price)
    if shares == 0:
        return math.inf
    actual = shares * price
    return 2 * one_way_cost(shares, price, cfg) / actual
