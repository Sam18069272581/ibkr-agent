from types import SimpleNamespace

from ibkr_agent.broker import Broker
from ibkr_agent.config import IbkrConfig

NAN = float("nan")


class FakeIB:
    def __init__(self, tickers, bars):
        self._tickers, self._bars = tickers, bars

    def qualifyContracts(self, *cs):
        return list(cs)

    def reqTickers(self, *cs):
        return self._tickers

    def reqHistoricalData(self, contract, **kw):
        return self._bars.get(contract.symbol, [])


def ticker(sym, bid=NAN, ask=NAN, last=NAN, close=NAN):
    return SimpleNamespace(contract=SimpleNamespace(symbol=sym), bid=bid, ask=ask, last=last, close=close)


def test_last_prices_falls_back_to_historical_close():
    b = Broker(IbkrConfig(), readonly=True)
    b.ib = FakeIB(
        [ticker("AAPL", bid=100, ask=102), ticker("KO")],
        {"KO": [SimpleNamespace(close=60.0), SimpleNamespace(close=61.5)]},
    )
    assert b.last_prices(["AAPL", "KO", "PG"]) == {"AAPL": 101.0, "KO": 61.5}
