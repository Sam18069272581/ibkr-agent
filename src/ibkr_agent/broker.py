"""IBKR 连接：账户、报价、下单。基于 ib_async，连接 IB Gateway。

账户基础货币可能是欧元，这里统一换算成美元，因为交易的是美股。
"""

from __future__ import annotations

import logging
import math

from ib_async import IB, LimitOrder, Stock

from .config import IbkrConfig
from .risk import AccountState, ApprovedOrder, Quote

log = logging.getLogger(__name__)

DELAYED_FROZEN = 4  # 没有实时行情订阅时退回延迟行情


def _finite(x: float | None) -> float:
    return x if x is not None and not math.isnan(x) else 0.0


class Broker:
    def __init__(self, cfg: IbkrConfig, readonly: bool):
        self._cfg = cfg
        self._readonly = readonly
        self.ib = IB()

    def __enter__(self) -> "Broker":
        self.ib.connect(
            self._cfg.host,
            self._cfg.port,
            clientId=self._cfg.client_id,
            readonly=self._readonly,
            account=self._cfg.account,
        )
        self.ib.reqMarketDataType(DELAYED_FROZEN)
        return self

    def __exit__(self, *exc) -> None:
        self.ib.disconnect()

    @property
    def account(self) -> str:
        return self._cfg.account or self.ib.managedAccounts()[0]

    def holdings(self) -> dict[str, int]:
        return {
            p.contract.symbol: int(p.position)
            for p in self.ib.portfolio(self.account)
            if p.contract.secType == "STK" and p.contract.currency == "USD"
        }

    def account_state(self, high_water_mark: float) -> AccountState:
        values = self.ib.accountValues(self.account)
        base_nl = next(float(v.value) for v in values if v.tag == "NetLiquidation" and v.currency != "")
        usd_rate = next(
            (float(v.value) for v in values if v.tag == "ExchangeRate" and v.currency == "USD"), 1.0
        )  # 1 美元 = usd_rate 个基础货币
        usd_cash = next(
            (float(v.value) for v in values if v.tag == "CashBalance" and v.currency == "USD"), 0.0
        )
        nl_usd = base_nl / usd_rate
        positions = {
            p.contract.symbol: float(p.marketValue)
            for p in self.ib.portfolio(self.account)
            if p.contract.secType == "STK" and p.contract.currency == "USD"
        }
        return AccountState(nl_usd, usd_cash, max(high_water_mark, nl_usd), positions)

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        contracts = self.ib.qualifyContracts(*[Stock(t, "SMART", "USD") for t in tickers])
        out: dict[str, Quote] = {}
        for tk in self.ib.reqTickers(*contracts):
            out[tk.contract.symbol] = Quote(_finite(tk.bid), _finite(tk.ask))
        return out

    def last_prices(self, tickers: list[str]) -> dict[str, float]:
        """给决策引擎用的参考价：中间价，没有就用收盘价。"""
        contracts = self.ib.qualifyContracts(*[Stock(t, "SMART", "USD") for t in tickers])
        out: dict[str, float] = {}
        for tk in self.ib.reqTickers(*contracts):
            bid, ask = _finite(tk.bid), _finite(tk.ask)
            price = (bid + ask) / 2 if bid > 0 and ask > 0 else _finite(tk.close)
            if price > 0:
                out[tk.contract.symbol] = price
        return out

    def place(self, order: ApprovedOrder) -> None:
        if self._readonly:
            raise RuntimeError("只读连接不能下单")
        [contract] = self.ib.qualifyContracts(Stock(order.ticker, "SMART", "USD"))
        lmt = LimitOrder(order.action, order.shares, order.limit_price, tif="DAY", account=self.account)
        trade = self.ib.placeOrder(contract, lmt)
        log.info("已提交 %s %s %d @ %.2f（订单号 %s）", order.action, order.ticker, order.shares,
                 order.limit_price, trade.order.orderId)
