"""风控和下单前检查：纯函数。

- tax_filter：卖出会产生短期税负时，只卖能免税的部分；超过单币硬上限时例外。
- risk_check：kill switch、回撤熔断、单币上限、现金、单笔下限、订单数。
- shocked：最近一段时间波动过大时不买。
- preflight：下单前一秒用新盘口再确认：计划没过期、报价新鲜、价差正常、价格还在价格带内。
  价格跑出带外就不追。卖出和买入一样检查价格带，但卖出不受回撤熔断和波动检查限制。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from .config import CryptoSettings
from .exchange import Book
from .lots import LotBook
from .plan import OrderPlan


@dataclass(frozen=True)
class Rejected:
    ticker: str
    side: str
    why: str


@dataclass(frozen=True)
class Send:
    price: float
    amount: float    # 币的数量


@dataclass(frozen=True)
class Skip:
    why: str


def tax_filter(plans: list[OrderPlan], lots: LotBook, weights: dict[str, float],
               s: CryptoSettings, now: datetime) -> tuple[list[OrderPlan], list[Rejected]]:
    kept: list[OrderPlan] = []
    held_back: list[Rejected] = []
    for p in plans:
        if p.side != "SELL":
            kept.append(p)
            continue
        if weights.get(p.ticker, 0.0) > s.risk.max_weight:
            kept.append(replace(p, reason=p.reason + f"；超过单币上限 {s.risk.max_weight:.0%}，即使有税也卖"))
            continue
        free_value = lots.tax_free_qty(p.ticker, p.ref_price, now) * p.ref_price
        if free_value >= p.notional:
            kept.append(p)
        elif free_value >= s.risk.min_order_value:
            kept.append(replace(p, notional=free_value, reason=p.reason + "；只卖不产生税的部分"))
        else:
            when = lots.next_tax_free_date(p.ticker, now)
            held_back.append(Rejected(p.ticker, p.side,
                                      f"可卖批次未满一年，现在卖要交税；最早 {when:%Y-%m-%d} 免税" if when else "现在卖要交税"))
    return kept, held_back


def risk_check(plans: list[OrderPlan], nav: float, high_water: float, cash: float,
               values: dict[str, float], s: CryptoSettings) -> tuple[list[OrderPlan], list[Rejected]]:
    r = s.risk
    if Path(r.kill_switch_file).exists():
        return [], [Rejected(p.ticker, p.side, "总开关已触发") for p in plans]
    ok: list[OrderPlan] = []
    bad: list[Rejected] = []
    halted = high_water > 0 and nav < high_water * (1 - r.drawdown_halt)
    cash_left = cash - r.cash_buffer * nav
    vals = dict(values)
    for p in plans:
        if len(ok) >= r.max_orders_per_run:
            bad.append(Rejected(p.ticker, p.side, "超过单次运行订单数上限"))
            continue
        if p.notional < r.min_order_value:
            bad.append(Rejected(p.ticker, p.side, f"金额 {p.notional:.2f} 低于下限"))
            continue
        if p.side == "SELL":
            vals[p.ticker] = max(0.0, vals.get(p.ticker, 0.0) - p.notional)
            ok.append(p)
            continue
        if halted:
            bad.append(Rejected(p.ticker, p.side, f"回撤熔断中（高点 {high_water:.2f}，现在 {nav:.2f}），不买"))
            continue
        cost = p.notional * (1 + s.costs.maker_fee)
        if cost > cash_left + 1e-9:
            bad.append(Rejected(p.ticker, p.side, f"现金不足（需要 {cost:.2f}，可用 {cash_left:.2f}）"))
            continue
        if (vals.get(p.ticker, 0.0) + p.notional) / nav > r.max_weight + 1e-9:
            bad.append(Rejected(p.ticker, p.side, f"买完会超过单币上限 {r.max_weight:.0%}"))
            continue
        cash_left -= cost
        vals[p.ticker] = vals.get(p.ticker, 0.0) + p.notional
        ok.append(p)
    return ok, bad


def shocked(open_: float, low: float, high: float, threshold: float) -> bool:
    """窗口内最高最低之差相对开盘价超过阈值。"""
    return open_ > 0 and (high - low) / open_ > threshold


def preflight(p: OrderPlan, book: Book, now: float, s: CryptoSettings) -> Send | Skip:
    ex = s.execution
    if now > p.expires_at:
        return Skip("计划已过期，等下一轮重新评估")
    if now - book.ts > ex.max_quote_age_seconds:
        return Skip(f"报价已经 {now - book.ts:.1f} 秒，超过 {ex.max_quote_age_seconds:.0f} 秒")
    if book.bid <= 0 or book.ask <= book.bid:
        return Skip("盘口无效")
    if book.spread_bps > s.risk.max_spread_bps:
        return Skip(f"价差 {book.spread_bps:.0f}bp 过大")
    if p.side == "BUY":
        if book.bid > p.ceiling:
            return Skip(f"价格已涨到 {book.bid:.2f}，超出计划价 {p.ref_price:.2f} 的 +{p.band:.1%}，不追")
        price = min(book.bid, p.ceiling)   # 挂在买一，做 maker；永远不超过价格带上沿
    else:
        if book.ask < p.floor:
            return Skip(f"价格已跌到 {book.ask:.2f}，超出计划价 {p.ref_price:.2f} 的 −{p.band:.1%}，不追")
        price = max(book.ask, p.floor)
    return Send(price, p.notional / price)
