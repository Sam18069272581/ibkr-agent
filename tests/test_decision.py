from ibkr_agent.config import CostConfig, DecisionConfig, RiskConfig
from ibkr_agent.decision import Candidate, plan, score_candidate
from ibkr_agent.research import ResearchView

D, C, R = DecisionConfig(), CostConfig(), RiskConfig()
NAV = 2200.0


def view(adj, conf):
    return ResearchView("X", adj, conf, "", "", (), (), "")


def cand(t, price, base, sector="s", v=None):
    return Candidate(t, price, sector, base, v)


def test_low_confidence_claude_view_is_ignored():
    s = score_candidate(cand("A", 50, 0.10, v=view(0.03, "low")), 350, D, C)
    assert s.score == 0.10


def test_high_confidence_view_counts_fully():
    s = score_candidate(cand("A", 50, 0.10, v=view(-0.03, "high")), 350, D, C)
    assert abs(s.score - 0.07) < 1e-12


def test_missing_fundamentals_is_not_scored():
    assert score_candidate(cand("A", 50, None), 350, D, C) is None


def test_default_is_no_trade_when_nothing_beats_baseline():
    orders, _ = plan([cand("A", 50, 0.08), cand("B", 50, 0.06)], {}, NAV, D, C, R)
    assert orders == []


def test_buys_top_candidates_equal_weight():
    cands = [cand(t, 50, 0.12 + i * 0.01, sector=t) for i, t in enumerate("ABCDEFGH")]
    orders, _ = plan(cands, {}, NAV, D, C, R)
    assert len(orders) == R.max_positions
    assert {o.ticker for o in orders} == set("CDEFGH")
    assert all(o.action == "BUY" and o.shares == 7 for o in orders)  # 2200*0.97/6=355 -> 7 股


def test_sector_limit_spreads_positions():
    cands = [cand(t, 50, 0.15, sector="tech") for t in "ABCDEF"]
    orders, _ = plan(cands, {}, NAV, D, C, R)
    # 每个仓位约 16%，行业上限 45% -> 最多 2 只
    assert len(orders) == 2


def test_unaffordable_stock_is_skipped():
    orders, _ = plan([cand("A", 900, 0.20)], {}, NAV, D, C, R)
    assert orders == []


def test_hold_band_prevents_churn():
    # 超额 1.5%：不够买入（需 2% + 成本），但高于持有线（2% - 1% = 1%），所以继续持有
    held = {"A": 7}
    orders, _ = plan([cand("A", 50, 0.085)], held, NAV, D, C, R)
    assert orders == []


def test_sell_when_below_hold_line():
    orders, _ = plan([cand("A", 50, 0.07)], {"A": 7}, NAV, D, C, R)
    assert [(o.action, o.ticker, o.shares) for o in orders] == [("SELL", "A", 7)]


def test_no_data_is_not_a_sell_signal():
    orders, _ = plan([cand("A", 50, None)], {"A": 7}, NAV, D, C, R)
    assert orders == []


def test_sells_come_before_buys():
    cands = [cand("A", 50, 0.05, sector="x"), cand("B", 50, 0.15, sector="y")]
    orders, _ = plan(cands, {"A": 7}, NAV, D, C, R)
    assert [o.action for o in orders] == ["SELL", "BUY"]
