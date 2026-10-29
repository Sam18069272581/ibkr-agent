"""IBKR 连接：账户、报价、下单、等成交、持仓快照。基于 ib_async，连接 IB Gateway。

账户基础货币可能是欧元，这里统一换算成美元，因为交易的是美股。
"""

from __future__ import annotations

import logging
import math
import time

from ib_async import IB, LimitOrder, Stock, Trade

from .config import CostConfig, IbkrConfig
from .costs import one_way_cost
from .journal import FillRecord, PositionSnapshot
from .risk import AccountState, ApprovedOrder, Quote

log = logging.getLogger(__name__)

UNSET_DOUBLE = 1e300
CONNECT_TIMEOUT_S = 20  # IBKR 用极大值表示“没有值”，例如开仓成交的已实现盈亏


def _finite(x: float | None) -> float:
    return x if x is not None and not math.isnan(x) else 0.0


class Broker:
    def __init__(self, cfg: IbkrConfig, readonly: bool, market_data_type: int = 3):
        self._cfg = cfg
        self._market_data_type = market_data_type
        self._readonly = readonly
        self.ib = IB()

    def __enter__(self) -> "Broker":
        try:
            self.ib.connect(
                self._cfg.host,
                self._cfg.port,
                clientId=self._cfg.client_id,
                timeout=CONNECT_TIMEOUT_S,
                readonly=self._readonly,
                account=self._cfg.account,
            )
        except (TimeoutError, ConnectionRefusedError) as e:
            raise ConnectionError(
                f"连不上 IB Gateway（{self._cfg.host}:{self._cfg.port}）。"
                "请检查：网关是否已登录完成（docker compose logs ib-gateway）、"
                "手机上的登录确认是否已同意、用的是否是模拟盘自己的账号、"
                "同一账户是否在 TWS 或网页上同时登录。"
            ) from e
        self.ib.reqMarketDataType(self._market_data_type)
        return self

    def __exit__(self, *exc) -> None:
        self.ib.disconnect()

    @property
    def account(self) -> str:
        return self._cfg.account or self.ib.managedAccounts()[0]

    def _usd_stocks(self):
        return [
            p
            for p in self.ib.portfolio(self.account)
            if p.contract.secType == "STK" and p.contract.currency == "USD" and p.position != 0
        ]

    def holdings(self) -> dict[str, int]:
        return {p.contract.symbol: int(p.position) for p in self._usd_stocks()}

    def snapshot(self) -> list[PositionSnapshot]:
        return [
            PositionSnapshot(p.contract.symbol, float(p.position), float(p.marketValue), float(p.averageCost))
            for p in self._usd_stocks()
        ]

    def refresh_positions(self) -> dict[str, float]:
        """成交后重新向 IBKR 要持仓，用于对账。"""
        return {
            p.contract.symbol: float(p.position)
            for p in self.ib.reqPositions()
            if p.account == self.account
            and p.contract.secType == "STK"
            and p.contract.currency == "USD"
            and p.position != 0
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
        positions = {p.contract.symbol: float(p.marketValue) for p in self._usd_stocks()}
        return AccountState(nl_usd, usd_cash, max(high_water_mark, nl_usd), positions)

    def quotes(self, tickers: list[str]) -> dict[str, Quote]:
        contracts = self.ib.qualifyContracts(*[Stock(t, "SMART", "USD") for t in tickers])
        out: dict[str, Quote] = {}
        for tk in self.ib.reqTickers(*contracts):
            out[tk.contract.symbol] = Quote(_finite(tk.bid), _finite(tk.ask))
        return out

    def last_prices(self, tickers: list[str]) -> dict[str, float]:
        """参考价：中间价，没有就用收盘价。"""
        contracts = self.ib.qualifyContracts(*[Stock(t, "SMART", "USD") for t in tickers])
        out: dict[str, float] = {}
        for tk in self.ib.reqTickers(*contracts):
            bid, ask = _finite(tk.bid), _finite(tk.ask)
            price = (bid + ask) / 2 if bid > 0 and ask > 0 else _finite(tk.close)
            if price > 0:
                out[tk.contract.symbol] = price
        return out

    def execute(
        self, orders: list[ApprovedOrder], mids: dict[str, float], costs: CostConfig, timeout_s: int
    ) -> list[FillRecord]:
        """下限价单，等成交到超时为止，没成交的撤掉，返回全部成交记录。"""
        if self._readonly:
            raise RuntimeError("只读连接不能下单")
        trades: list[tuple[ApprovedOrder, Trade]] = []
        for o in orders:
            [contract] = self.ib.qualifyContracts(Stock(o.ticker, "SMART", "USD"))
            lmt = LimitOrder(o.action, o.shares, o.limit_price, tif="DAY", account=self.account)
            trades.append((o, self.ib.placeOrder(contract, lmt)))
            log.info("已提交 %s %s %d @ %.2f", o.action, o.ticker, o.shares, o.limit_price)

        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline and not all(t.isDone() for _, t in trades):
            self.ib.sleep(1)
        for o, t in trades:
            if not t.isDone():
                log.warning("%s %s 超时未完全成交，撤单", o.action, o.ticker)
                self.ib.cancelOrder(t.order)
        self.ib.sleep(3)  # 等佣金回报到达

        records: list[FillRecord] = []
        for o, t in trades:
            mid = mids[o.ticker]
            for f in t.fills:
                shares = float(f.execution.shares)
                cr = f.commissionReport
                pnl = cr.realizedPNL if cr and abs(cr.realizedPNL) < UNSET_DOUBLE else None
                records.append(
                    FillRecord(
                        exec_id=f.execution.execId,
                        ts=f.time.isoformat(),
                        ticker=o.ticker,
                        side=o.action,
                        shares=shares,
                        price=float(f.execution.price),
                        commission=float(cr.commission) if cr else 0.0,
                        realized_pnl=pnl,
                        ref_mid=mid,
                        modelled_cost=one_way_cost(int(shares), mid, costs),
                    )
                )
        return records
