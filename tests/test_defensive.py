"""防御性检测（2026-11-05）：每个测试对应一个能让运行报错、静默出错或输出空结果的问题。"""

import logging

import pytest
from test_crypto import BOOKS, NOW, FakeExchange, settings

from ibkr_agent import cli
from ibkr_agent.config import GraduationConfig
from ibkr_agent.crypto.cycle import run_cycle
from ibkr_agent.crypto.exchange import CcxtExchange, OrderStatus
from ibkr_agent.crypto.lots import LotBook
from ibkr_agent.journal import Journal
from ibkr_agent.report import build, render


def events(j, kind):
    return [r[0] for r in j.db.execute("SELECT payload FROM events WHERE kind=?", (kind,)).fetchall()]


# ---------- OKX 买入手续费按收到的币扣 ----------

class FakeCcxt:
    def __init__(self, order):
        self._order = order

    def fetch_order(self, oid, symbol):
        return self._order


def test_ccxt_order_separates_fee_currencies():
    """OKX 现货买入的手续费是用 BTC 扣的（feeCcy=BTC），不能当成欧元。"""
    ex = CcxtExchange(client=FakeCcxt({"id": "1", "status": "closed", "filled": 0.001, "average": 60000.0,
                                       "symbol": "BTC/EUR", "fee": {"cost": 8e-7, "currency": "BTC"}}))
    st = ex.order("BTC/EUR", "1")
    assert st.fee == 0.0 and st.fee_base == pytest.approx(8e-7)

    ex = CcxtExchange(client=FakeCcxt({"id": "2", "status": "closed", "filled": 0.001, "average": 60000.0,
                                       "symbol": "BTC/EUR", "fees": [{"cost": 0.048, "currency": "EUR"}]}))
    st = ex.order("BTC/EUR", "2")
    assert st.fee == pytest.approx(0.048) and st.fee_base == 0.0


class BaseFeeExchange(FakeExchange):
    """像 OKX 一样：买入时手续费从买到的币里扣。"""

    FEE = 0.0008

    def place_post_only(self, symbol, side, amount, price):
        oid = super().place_post_only(symbol, side, amount, price)
        if side == "BUY":
            base = symbol.split("/")[0]
            self.bal[base] -= amount * self.FEE
        return oid

    def order(self, symbol, oid):
        _, side, amount, price = self.placed[int(oid[1:]) - 1]
        if side == "BUY":
            return OrderStatus(oid, "closed", amount, price, 0.0, fee_base=amount * self.FEE)
        return OrderStatus(oid, "closed", amount, price, amount * price * self.FEE)


def test_base_currency_buy_fee_keeps_lots_in_sync(tmp_path):
    s = settings(tmp_path, dry_run=False)
    ex = BaseFeeExchange({"EUR": 300.0}, BOOKS)
    j = Journal(":memory:")
    assert run_cycle(s, ex, j, out=lambda _: None, clock=lambda: NOW, sleep=lambda _: None) == "ok"
    assert events(j, "warning") == []
    lots = LotBook(j.db, "okx-demo", s.tax)
    assert lots.held("BTC") == pytest.approx(ex.bal["BTC"], rel=1e-12)
    # 成本里要算上用币付的手续费：每个币的成本 = 花掉的欧元 / 实际到手的币
    lot = lots.lots("BTC")[0]
    assert lot.unit_cost == pytest.approx(60000.0 / (1 - BaseFeeExchange.FEE))
    # 记到 fills 的手续费换算成欧元，不能是 0
    fee = j.db.execute("SELECT commission FROM fills WHERE ticker='BTC'").fetchone()[0]
    assert fee == pytest.approx(lot.qty / (1 - BaseFeeExchange.FEE) * BaseFeeExchange.FEE * 60000.0)


# ---------- 持有的币拿不到价格 ----------

class FlakyBookExchange(FakeExchange):
    def __init__(self, *a, broken=(), **kw):
        super().__init__(*a, **kw)
        self.broken = set(broken)

    def book(self, symbol):
        if symbol in self.broken:
            raise RuntimeError(f"{symbol} 盘口为空")
        return super().book(symbol)


def test_unpriced_holding_blocks_trading(tmp_path):
    """BTC 盘口拿不到时，净值少算了 BTC，ETH 看起来超配，会被错误卖出。"""
    s = settings(tmp_path, dry_run=False)
    ex = FlakyBookExchange({"EUR": 6.0, "BTC": 0.003, "ETH": 0.04}, BOOKS, broken={"BTC/EUR"})
    j = Journal(":memory:")
    lines = []
    status = run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    assert ex.placed == []
    assert status == "error"
    assert any("BTC" in line and "价格" in line for line in lines)
    nav = j.db.execute("SELECT net_liquidation_usd FROM runs").fetchone()[0]
    assert nav is None  # 不完整的净值不能写进成绩和回撤高点


# ---------- 下单时出错 ----------

class PlaceFailsExchange(FakeExchange):
    def __init__(self, *a, fail_symbol, **kw):
        super().__init__(*a, **kw)
        self.fail_symbol = fail_symbol

    def place_post_only(self, symbol, side, amount, price):
        if symbol == self.fail_symbol:
            raise RuntimeError("okx 50001 service temporarily unavailable")
        return super().place_post_only(symbol, side, amount, price)


def test_order_error_still_runs_remaining_orders_and_reconciles(tmp_path):
    s = settings(tmp_path, dry_run=False)
    ex = PlaceFailsExchange({"EUR": 300.0}, BOOKS, fail_symbol="BTC/EUR")
    j = Journal(":memory:")
    status = run_cycle(s, ex, j, out=lambda _: None, clock=lambda: NOW, sleep=lambda _: None)
    assert status == "error"
    assert [p[0] for p in ex.placed] == ["ETH/EUR"]
    assert any("50001" in e for e in events(j, "order_error"))
    # 对账和快照照常做
    assert j.db.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] > 0
    assert j.db.execute("SELECT status FROM runs").fetchone()[0] == "error"


# ---------- 拿不到日线时要留痕 ----------

class NoClosesExchange(FakeExchange):
    def daily_closes(self, symbol, days):
        raise RuntimeError("okx 日线超时")


def test_missing_daily_closes_is_logged_and_marked_missing(tmp_path, caplog):
    from test_crypto_ai import FakeClient

    from ibkr_agent.crypto.config import CryptoAI

    s = settings(tmp_path, dry_run=True, ai=CryptoAI(gate_enabled=True))
    client = FakeClient({"p_invalidated": 0.05, "p_abnormal": 0.05, "p_up_7d": 0.5, "note": ""})
    j = Journal(":memory:")
    with caplog.at_level(logging.WARNING):
        run_cycle(s, NoClosesExchange({"EUR": 300.0}, BOOKS), j, out=lambda _: None, clock=lambda: NOW,
                  sleep=lambda _: None, ai_client=client)
    assert "日线超时" in caplog.text
    assert client.calls and "日线" in client.calls[0]["messages"][0]["content"]


# ---------- 报告：只有演练运行时不能显示 0 → 0 ----------

def test_report_explains_when_only_dry_runs():
    j = Journal(":memory:")
    for _ in range(2):
        rid = j.start_run("paper", True)
        j.set_account(rid, 3468.0, 3468.0, 500.0, False)
        j.finish_run(rid, "ok")
    text = render(build(j.db, "paper", GraduationConfig()))
    assert "0 → 0" not in text
    assert "演练 2 次" in text and "dry_run" in text


def test_report_empty_journal_says_no_runs():
    text = render(build(Journal(":memory:").db, "paper", GraduationConfig()))
    assert "0 → 0" not in text and "还没有" in text


# ---------- 交易所连不上时要告警 ----------

def test_crypto_startup_failure_sends_alert(tmp_path, monkeypatch):
    cfg = tmp_path / "settings.yaml"
    cfg.write_text(f"research:\n  journal_path: {tmp_path / 'j.sqlite'}\ncrypto:\n  enabled: true\n", encoding="utf-8")
    sent = []
    monkeypatch.setattr(cli.alerts, "send", lambda title, body: sent.append((title, body)))

    def boom(*a, **kw):
        raise RuntimeError("缺少环境变量：OKX_API_KEY")

    monkeypatch.setattr("ibkr_agent.crypto.exchange.CcxtExchange", boom)
    assert cli.crypto(str(cfg), "run") == 1
    assert sent and "OKX_API_KEY" in sent[0][1]


# ---------- 美股：财报全部拿不到 ----------

class FakeBroker:
    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def account_state(self, hwm):
        from ibkr_agent.risk import AccountState
        return AccountState(3468.0, 3468.0, 3468.0, {})

    def last_prices(self, tickers):
        return {t: 100.0 for t in tickers}

    def holdings(self):
        return {}

    def quotes(self, tickers):
        return {}

    def snapshot(self):
        return []


class BrokenEdgar:
    def fundamentals(self, ticker):
        raise RuntimeError("sec.gov 403 Forbidden")


def test_all_fundamentals_failing_is_an_error_not_ok(monkeypatch):
    """以前每只股票都只记一行日志，整次运行仍算成功（ok），只是一直“本次不交易”，没人会发现。"""
    from ibkr_agent.config import Settings

    monkeypatch.setattr(cli, "Broker", FakeBroker)
    monkeypatch.setattr(cli, "EdgarClient", BrokenEdgar)
    j = Journal(":memory:")
    rid = j.start_run("paper", True)
    with pytest.raises(RuntimeError, match="报表"):
        cli._cycle(Settings(universe=("KO", "PG")), j, rid, skip_research=True)
    assert len(events(j, "warning")) == 2 and "403" in events(j, "warning")[0]


class HalfEdgar:
    def fundamentals(self, ticker):
        if ticker == "KO":
            raise RuntimeError("KO 没有 FCF 数据")
        from ibkr_agent.fundamentals import Fundamentals
        return Fundamentals(ticker, None, 1e9, 0.03, 1e8, 1e8, 0.5, 1e7)


def test_single_fundamentals_failure_is_recorded_in_journal(monkeypatch):
    from ibkr_agent.config import Settings

    monkeypatch.setattr(cli, "Broker", FakeBroker)
    monkeypatch.setattr(cli, "EdgarClient", HalfEdgar)
    from ibkr_agent.valuation import Valuation
    monkeypatch.setattr(cli, "value", lambda f, cap: Valuation(f.ticker, 0.05, 0.03, 0.0))
    j = Journal(":memory:")
    rid = j.start_run("paper", True)
    assert cli._cycle(Settings(universe=("KO", "PG")), j, rid, skip_research=True) == "ok"
    warnings = events(j, "warning")
    assert len(warnings) == 1 and "KO" in warnings[0]
