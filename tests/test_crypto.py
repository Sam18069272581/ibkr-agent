from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from ibkr_agent.crypto.config import CryptoRisk, CryptoSettings, CryptoTax, parse
from ibkr_agent.crypto.cycle import run_cycle
from ibkr_agent.crypto.exchange import Book, OrderStatus
from ibkr_agent.crypto.guard import Send, Skip, preflight, risk_check, shocked, tax_filter
from ibkr_agent.crypto.lots import LotBook, marginal_tax
from ibkr_agent.crypto.plan import OrderPlan, rebalance
from ibkr_agent.journal import Journal

NOW = 1_800_000_000.0
NOW_DT = datetime.fromtimestamp(NOW, timezone.utc)


def settings(tmp_path, **kw) -> CryptoSettings:
    risk = CryptoRisk(kill_switch_file=str(tmp_path / "KILL"))
    return replace(CryptoSettings(enabled=True, risk=risk), **kw)


def plan(side="BUY", notional=100.0, ref=100.0, band=0.005, expires=NOW + 900, ticker="BTC"):
    return OrderPlan(ticker, f"{ticker}/EUR", side, notional, ref, band, expires, "test")


# ---------- 再平衡 ----------

def test_all_cash_buys_targets_within_cash_buffer(tmp_path):
    s = settings(tmp_path, risk=CryptoRisk(max_buy_fraction_per_run=1.0, kill_switch_file=str(tmp_path / "KILL")))
    plans, _ = rebalance({"BTC": 60000.0, "ETH": 3000.0}, {}, 300.0, s, NOW)
    by = {p.ticker: p for p in plans}
    assert set(by) == {"BTC", "ETH"} and all(p.side == "BUY" for p in plans)
    assert sum(p.notional for p in plans) == pytest.approx((300 - 6) / 1.0008)  # 留 2% 现金和手续费
    assert by["BTC"].notional / by["ETH"].notional == pytest.approx(0.6 / 0.4)
    assert by["BTC"].expires_at == NOW + 15 * 60


def test_staged_entry_caps_each_run(tmp_path):
    s = settings(tmp_path)  # 默认每次运行最多买净值的 25%
    plans, notes = rebalance({"BTC": 60000.0, "ETH": 3000.0}, {}, 300.0, s, NOW)
    by = {p.ticker: p for p in plans}
    assert sum(p.notional for p in plans) == pytest.approx(75.0)
    assert by["BTC"].notional / by["ETH"].notional == pytest.approx(0.6 / 0.4)
    assert any("分批建仓" in n for n in notes)


def test_staged_entry_reaches_target_in_four_runs_without_stalling(tmp_path):
    s = settings(tmp_path)
    prices = {"BTC": 100.0, "ETH": 100.0}
    held, cash = {}, 300.0
    for _ in range(4):
        plans, _ = rebalance(prices, held, cash, s, NOW)
        assert plans and all(p.side == "BUY" for p in plans)
        for p in plans:
            held[p.ticker] = held.get(p.ticker, 0.0) + p.notional / prices[p.ticker]
            cash -= p.notional * (1 + s.costs.maker_fee)
    nav = cash + sum(q * prices[a] for a, q in held.items())
    assert held["BTC"] * 100 / nav == pytest.approx(0.6, abs=0.015)
    assert held["ETH"] * 100 / nav == pytest.approx(0.4, abs=0.015)
    assert cash / nav == pytest.approx(0.02, abs=0.005)
    assert rebalance(prices, held, cash, s, NOW)[0] == []          # 建满以后在带内就不动


def test_excess_cash_tops_up_underweight_coin_inside_band(tmp_path):
    s = settings(tmp_path)
    # BTC 60%，ETH 31%，现金 9%：ETH 只差 9 个百分点，在带内；但现金比 2% 缓冲多 7 个百分点，要补
    plans, _ = rebalance({"BTC": 100.0, "ETH": 100.0}, {"BTC": 1.8, "ETH": 0.93}, 27.0, s, NOW)
    assert [(p.side, p.ticker) for p in plans] == [("BUY", "ETH")]
    assert plans[0].notional == pytest.approx((27 - 6) / 1.0008)
    assert "现金" in plans[0].reason
    # 现金只比缓冲多 4 个百分点：不补
    assert rebalance({"BTC": 100.0, "ETH": 100.0}, {"BTC": 1.8, "ETH": 1.02}, 18.0, s, NOW)[0] == []


def test_inside_band_does_nothing(tmp_path):
    s = settings(tmp_path)
    # BTC 55%，ETH 42%，现金 3%：都在 10pp 带内
    plans, notes = rebalance({"BTC": 100.0, "ETH": 100.0}, {"BTC": 1.65, "ETH": 1.26}, 9.0, s, NOW)
    assert plans == [] and len(notes) == 2


def test_overweight_sells_back_to_target(tmp_path):
    s = settings(tmp_path)
    plans, _ = rebalance({"BTC": 100.0, "ETH": 100.0}, {"BTC": 2.4, "ETH": 0.6}, 0.0, s, NOW)
    sell = next(p for p in plans if p.side == "SELL")
    assert sell.ticker == "BTC" and sell.notional == pytest.approx(0.2 * 300)


def test_tiny_orders_dropped(tmp_path):
    s = settings(tmp_path, risk=CryptoRisk(min_order_value=500, kill_switch_file=str(tmp_path / "KILL")))
    plans, notes = rebalance({"BTC": 100.0, "ETH": 100.0}, {}, 300.0, s, NOW)
    assert plans == [] and any("低于单笔下限" in n for n in notes)


def test_targets_over_one_rejected():
    with pytest.raises(ValueError):
        parse({"targets": {"BTC": 0.7, "ETH": 0.5}})


# ---------- 税 ----------

def test_marginal_tax_freigrenze():
    cfg = CryptoTax(annual_threshold=1000, est_tax_rate=0.3)
    assert marginal_tax(0, 900, cfg) == 0
    assert marginal_tax(900, 200, cfg) == pytest.approx(1100 * 0.3)  # 越过阈值后全额计税
    assert marginal_tax(1100, 100, cfg) == pytest.approx(30)
    assert marginal_tax(1100, -100, cfg) == 0


def make_lots(tax=CryptoTax(annual_threshold=0)):
    return LotBook(Journal(":memory:").db, "test", tax)


def test_fifo_and_tax_free_qty():
    lots = make_lots()
    lots.add("BTC", NOW_DT - timedelta(days=400), 1.0, 100.0)   # 长期
    lots.add("BTC", NOW_DT - timedelta(days=30), 1.0, 100.0)    # 短期、盈利
    assert lots.tax_free_qty("BTC", 150.0, NOW_DT) == pytest.approx(1.0)
    assert lots.tax_free_qty("BTC", 90.0, NOW_DT) == pytest.approx(2.0)   # 短期批次亏损，可以卖
    short_gain = lots.dispose("BTC", 1.5, 150.0, NOW_DT)
    assert short_gain == pytest.approx(0.5 * 50)
    assert lots.held("BTC") == pytest.approx(0.5)
    assert lots.next_tax_free_date("BTC", NOW_DT).date() == (NOW_DT - timedelta(days=30) + timedelta(days=365)).date()


def test_tax_free_qty_uses_freigrenze_room():
    lots = make_lots(CryptoTax(annual_threshold=1000))
    lots.add("ETH", NOW_DT - timedelta(days=10), 10.0, 100.0)
    # 每个币赚 200，阈值 1000 → 最多卖 5 个
    assert lots.tax_free_qty("ETH", 300.0, NOW_DT) == pytest.approx(5.0)


def test_sync_records_unknown_and_external():
    lots = make_lots()
    assert "多" in lots.sync("BTC", 0.5, 100.0, NOW_DT)
    assert lots.sync("BTC", 0.5, 100.0, NOW_DT) is None
    assert "系统外" in lots.sync("BTC", 0.2, 100.0, NOW_DT)
    assert lots.held("BTC") == pytest.approx(0.2)


def test_tax_filter_holds_back_short_term_gain(tmp_path):
    s = settings(tmp_path, tax=CryptoTax(annual_threshold=0))
    lots = LotBook(Journal(":memory:").db, "t", s.tax)
    lots.add("BTC", NOW_DT - timedelta(days=100), 1.0, 50.0)
    kept, held = tax_filter([plan("SELL", 50.0)], lots, {"BTC": 0.6}, s, NOW_DT)
    assert kept == [] and "未满一年" in held[0].why
    kept, held = tax_filter([plan("SELL", 50.0)], lots, {"BTC": 0.8}, s, NOW_DT)  # 超过 70% 硬上限
    assert len(kept) == 1 and held == []


# ---------- 风控 ----------

def test_kill_switch(tmp_path):
    s = settings(tmp_path)
    (tmp_path / "KILL").write_text("")
    ok, bad = risk_check([plan()], 300, 300, 300, {}, s)
    assert ok == [] and bad[0].why == "总开关已触发"


def test_drawdown_blocks_buys_not_sells(tmp_path):
    s = settings(tmp_path)
    ok, bad = risk_check([plan("SELL", 50), plan("BUY", 50, ticker="ETH")], 190, 300, 200, {"BTC": 100}, s)
    assert [p.side for p in ok] == ["SELL"] and "回撤熔断" in bad[0].why


def test_cash_and_weight_limits(tmp_path):
    s = settings(tmp_path)
    ok, bad = risk_check([plan("BUY", 250)], 300, 0, 300, {}, s)
    assert ok == [] and "上限" in bad[0].why
    ok, bad = risk_check([plan("BUY", 150)], 300, 0, 100, {}, s)
    assert ok == [] and "现金不足" in bad[0].why


def test_risk_check_caps_total_buys_per_run(tmp_path):
    s = settings(tmp_path)
    ok, bad = risk_check([plan("BUY", 50), plan("BUY", 50, ticker="ETH")], 300, 0, 300, {}, s)
    assert [p.ticker for p in ok] == ["BTC"] and "单次买入上限" in bad[0].why
    with pytest.raises(ValueError):
        parse({"risk": {"max_buy_fraction_per_run": 0}})


def test_shocked():
    assert shocked(100, 98, 102, 0.03)
    assert not shocked(100, 99, 101, 0.03)


# ---------- 下单前重取价 ----------

def test_preflight_buys_at_bid_inside_band(tmp_path):
    s = settings(tmp_path)
    d = preflight(plan(), Book("BTC/EUR", 100.2, 100.3, NOW), NOW + 1, s)
    assert isinstance(d, Send) and d.price == 100.2 and d.amount == pytest.approx(100 / 100.2)


def test_preflight_does_not_chase(tmp_path):
    s = settings(tmp_path)
    d = preflight(plan(), Book("BTC/EUR", 100.6, 100.7, NOW), NOW, s)
    assert isinstance(d, Skip) and "不追" in d.why
    d = preflight(plan("SELL"), Book("BTC/EUR", 99.3, 99.4, NOW), NOW, s)
    assert isinstance(d, Skip) and "不追" in d.why


def test_preflight_expired_and_stale(tmp_path):
    s = settings(tmp_path)
    assert "过期" in preflight(plan(expires=NOW - 1), Book("BTC/EUR", 100, 100.1, NOW), NOW, s).why
    assert "秒" in preflight(plan(), Book("BTC/EUR", 100, 100.1, NOW - 5), NOW, s).why


# ---------- 整个周期 ----------

class FakeExchange:
    def __init__(self, balances, books, move_after_plan=None):
        self.bal = dict(balances)
        self.books = dict(books)
        self.calls = {}
        self.placed = []
        self.move = move_after_plan or {}

    def balances(self):
        return dict(self.bal)

    def book(self, symbol):
        n = self.calls[symbol] = self.calls.get(symbol, 0) + 1
        bid, ask = self.move.get(symbol, self.books[symbol]) if n > 1 else self.books[symbol]
        return Book(symbol, bid, ask, NOW)

    def recent_range(self, symbol, minutes):
        b = self.books[symbol][0]
        return b, b * 0.995, b * 1.005

    def daily_closes(self, symbol, days):
        return [self.books[symbol][0]] * days

    def amount_to_precision(self, symbol, amount):
        return round(amount, 8)

    def min_amount(self, symbol):
        return 0.00001

    def place_post_only(self, symbol, side, amount, price):
        base, quote = symbol.split("/")
        sign = 1 if side == "BUY" else -1
        self.bal[base] = self.bal.get(base, 0) + sign * amount
        self.bal[quote] = self.bal.get(quote, 0) - sign * amount * price
        self.placed.append((symbol, side, amount, price))
        return f"o{len(self.placed)}"

    def order(self, symbol, oid):
        _, _, amount, price = self.placed[int(oid[1:]) - 1]
        return OrderStatus(oid, "closed", amount, price, 0.0)

    def cancel(self, symbol, oid):
        pass


BOOKS = {"BTC/EUR": (60000.0, 60010.0), "ETH/EUR": (3000.0, 3001.0)}


def test_dry_run_places_nothing(tmp_path):
    s = settings(tmp_path)
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    j = Journal(":memory:")
    lines = []
    assert run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None) == "ok"
    assert ex.placed == []
    assert any("[演练] BUY BTC" in l for l in lines)


def test_live_cycle_buys_and_records_lots(tmp_path):
    s = settings(tmp_path, dry_run=False)
    ex = FakeExchange({"EUR": 300.0}, BOOKS)
    j = Journal(":memory:")
    assert run_cycle(s, ex, j, out=lambda _: None, clock=lambda: NOW, sleep=lambda _: None) == "ok"
    assert [p[1] for p in ex.placed] == ["BUY", "BUY"]
    assert all(p[3] == BOOKS[p[0]][0] for p in ex.placed)  # 挂在买一
    lots = LotBook(j.db, "okx-demo", s.tax)
    assert lots.held("BTC") == pytest.approx(ex.bal["BTC"])
    assert j.db.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 2
    # 快照要带批次成本，否则报告会把所有持仓都算成浮盈
    btc_cost = j.db.execute("SELECT avg_cost FROM snapshots WHERE ticker='BTC'").fetchone()[0]
    assert btc_cost == pytest.approx(lots.lots("BTC")[0].unit_cost) and btc_cost > 0


def test_price_runs_away_between_plan_and_send(tmp_path):
    s = settings(tmp_path, dry_run=False)
    ex = FakeExchange({"EUR": 300.0}, BOOKS, move_after_plan={"BTC/EUR": (61000.0, 61010.0)})
    j = Journal(":memory:")
    lines = []
    run_cycle(s, ex, j, out=lines.append, clock=lambda: NOW, sleep=lambda _: None)
    assert [p[0] for p in ex.placed] == ["ETH/EUR"]
    assert any("不追" in l for l in lines)


def test_crypto_high_water_mark_separate_from_stocks():
    j = Journal(":memory:")
    a = j.start_run("paper", False); j.set_account(a, 3000, 3500, None, False); j.finish_run(a, "ok")
    b = j.start_run("crypto-okx-demo", False); j.set_account(b, 300, 320, None, False); j.finish_run(b, "ok")
    assert j.high_water_mark() == 3500
    assert j.high_water_mark("crypto-okx-demo") == 320
