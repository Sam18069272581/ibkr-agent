"""再平衡计划：纯函数。

评审链（以后包括 Opus 定的边界）只产出“带边界的计划”，不产出“现在以某价成交”：
标的、方向、金额上限、计划价、价格带、过期时间。真正的价格在下单前一秒由 guard.preflight 重新确认。
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CryptoSettings


@dataclass(frozen=True)
class OrderPlan:
    ticker: str          # 资产代码，例如 BTC（字段名沿用日志里的 ticker）
    symbol: str          # 交易对，例如 BTC/EUR
    side: str            # BUY | SELL
    notional: float      # 计价货币金额上限
    ref_price: float     # 做计划时的中间价
    band: float          # 允许的价格偏离
    expires_at: float    # 计划作废的时间（epoch 秒）
    reason: str

    @property
    def ceiling(self) -> float:
        return self.ref_price * (1 + self.band)

    @property
    def floor(self) -> float:
        return self.ref_price * (1 - self.band)


@dataclass(frozen=True)
class Weight:
    ticker: str
    value: float
    weight: float
    target: float

    @property
    def deviation(self) -> float:
        return self.weight - self.target


def weights(prices: dict[str, float], holdings: dict[str, float], cash: float,
            targets: dict[str, float]) -> tuple[float, list[Weight]]:
    """返回（净值, 每个资产的权重）。持有但不在目标里的资产，目标按 0 处理。"""
    assets = sorted(set(targets) | {a for a, q in holdings.items() if q > 0 and a in prices})
    values = {a: holdings.get(a, 0.0) * prices[a] for a in assets if a in prices}
    nav = cash + sum(values.values())
    if nav <= 0:
        return 0.0, []
    return nav, [Weight(a, values.get(a, 0.0), values.get(a, 0.0) / nav, targets.get(a, 0.0)) for a in assets if a in prices]


def rebalance(prices: dict[str, float], holdings: dict[str, float], cash: float,
              s: CryptoSettings, now: float) -> tuple[list[OrderPlan], list[str]]:
    """偏离目标超过再平衡带的资产调回目标。先卖后买。

    - 现金比缓冲多出 cash_deploy_band 以上时（从零建仓、入金、卖出之后），低于目标的币都补到目标，不看带。
      否则分批建仓到最后一步时，差几个百分点的币会卡在带内，现金一直闲着。
    - 买入总额受两个限制，取小的那个，按比例缩小：可用现金（扣缓冲和手续费），以及每次运行最多买净值的
      max_buy_fraction_per_run（分批建仓）。
    """
    nav, ws = weights(prices, holdings, cash, s.targets)
    notes: list[str] = []
    if nav <= 0:
        return [], ["净值为 0，没有可调配的资金"]
    expires = now + s.execution.plan_ttl_minutes * 60
    sells: list[OrderPlan] = []
    buys: list[OrderPlan] = []
    cash_heavy = cash / nav - s.risk.cash_buffer > s.cash_deploy_band
    for w in ws:
        inside = abs(w.deviation) < s.rebalance_band
        if inside and not (cash_heavy and w.deviation < 0):
            notes.append(f"{w.ticker} 占 {w.weight:.0%}，目标 {w.target:.0%}，偏离在 {s.rebalance_band:.0%} 带内，不动")
            continue
        amount = abs(w.deviation) * nav
        side = "SELL" if w.deviation > 0 else "BUY"
        if inside:
            reason = f"占 {w.weight:.0%}，目标 {w.target:.0%}，现金占 {cash / nav:.0%} 偏多，补到目标"
        else:
            reason = f"占 {w.weight:.0%}，目标 {w.target:.0%}，偏离 {w.deviation:+.0%} 超过 {s.rebalance_band:.0%} 带"
        plan = OrderPlan(w.ticker, s.symbol(w.ticker), side, amount, prices[w.ticker],
                         s.execution.price_band, expires, reason)
        (sells if side == "SELL" else buys).append(plan)

    spendable = max(0.0, (cash - s.risk.cash_buffer * nav) / (1 + s.costs.maker_fee))  # 手续费也要从现金里出
    per_run = s.risk.max_buy_fraction_per_run * nav
    want = sum(b.notional for b in buys)
    limit = min(spendable, per_run)
    if buys and want > limit:
        scale = limit / want
        if per_run < spendable:
            notes.append(f"分批建仓：这次最多买净值的 {s.risk.max_buy_fraction_per_run:.0%}（{per_run:.2f} {s.quote}），"
                         f"一共需要 {want:.2f}，剩下的下次运行再买")
        else:
            notes.append(f"买入需要 {want:.2f}，可用现金 {spendable:.2f}，按比例缩小（卖出所得下次运行再用）")
        buys = [OrderPlan(b.ticker, b.symbol, b.side, b.notional * scale, b.ref_price, b.band,
                          b.expires_at, b.reason) for b in buys]

    plans = []
    for p in sells + buys:
        if p.notional < s.risk.min_order_value:
            notes.append(f"{p.side} {p.ticker} 金额 {p.notional:.2f} 低于单笔下限 {s.risk.min_order_value:.0f}，不下")
            continue
        plans.append(p)
    return plans, notes
