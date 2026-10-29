from ibkr_agent.config import CostConfig, RiskConfig
from ibkr_agent.decision import PlannedOrder
from ibkr_agent.risk import AccountState, Quote, check, limit_price

C = CostConfig()
Q = {"A": Quote(49.99, 50.01), "B": Quote(29.99, 30.01)}


def acct(cash=2000.0, nav=2200.0, hwm=2200.0, pos=None):
    return AccountState(nav, cash, hwm, pos or {})


def rcfg(tmp_path, **kw):
    return RiskConfig(kill_switch_file=str(tmp_path / "KILL"), **kw)


def buy(t="A", n=7, px=50.0):
    return PlannedOrder(t, "BUY", n, px, "test")


def test_limit_price_never_crosses_the_far_side():
    q = Quote(49.99, 50.01)
    assert limit_price("BUY", q, 10) == 50.01
    assert limit_price("SELL", q, 10) == 49.99


def test_approves_normal_buy(tmp_path):
    ok, bad = check([buy()], Q, acct(), {}, rcfg(tmp_path), C)
    assert len(ok) == 1 and not bad


def test_kill_switch_blocks_everything(tmp_path):
    (tmp_path / "KILL").touch()
    ok, bad = check([buy()], Q, acct(), {}, rcfg(tmp_path), C)
    assert not ok and bad[0].why == "总开关已触发"


def test_no_margin_buying(tmp_path):
    ok, bad = check([buy()], Q, acct(cash=300), {}, rcfg(tmp_path), C)
    assert not ok and "现金不足" in bad[0].why


def test_drawdown_halts_buys_but_allows_sells(tmp_path):
    sell = PlannedOrder("B", "SELL", 5, 30.0, "test")
    ok, bad = check([sell, buy()], Q, acct(nav=1700, hwm=2200), {}, rcfg(tmp_path), C)
    assert [o.action for o in ok] == ["SELL"]
    assert "回撤熔断" in bad[0].why


def test_wide_spread_is_rejected(tmp_path):
    q = {"A": Quote(49.0, 51.0)}
    ok, bad = check([buy()], q, acct(), {}, rcfg(tmp_path), C)
    assert not ok and "价差" in bad[0].why


def test_missing_quote_is_rejected(tmp_path):
    ok, bad = check([buy("Z")], Q, acct(), {}, rcfg(tmp_path), C)
    assert not ok and bad[0].why == "没有有效报价"


def test_position_cap(tmp_path):
    ok, bad = check([buy(n=11)], Q, acct(), {}, rcfg(tmp_path), C)  # 550/2200 = 25%+
    assert not ok and "单只股票" in bad[0].why


def test_sector_cap(tmp_path):
    sectors = {"A": "tech", "X": "tech"}
    ok, bad = check([buy()], Q, acct(pos={"X": 700}), sectors, rcfg(tmp_path), C)
    assert not ok and "行业" in bad[0].why


def test_small_orders_rejected(tmp_path):
    ok, bad = check([buy(n=2)], Q, acct(), {}, rcfg(tmp_path), C)
    assert not ok and "低于下限" in bad[0].why


def test_order_count_limit(tmp_path):
    orders = [buy("A", 4), buy("B", 7)]
    ok, bad = check(orders, Q, acct(), {}, rcfg(tmp_path, max_orders_per_run=1), C)
    assert len(ok) == 1 and "上限" in bad[0].why
