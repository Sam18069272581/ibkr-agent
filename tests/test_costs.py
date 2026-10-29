import math

from ibkr_agent.config import CostConfig
from ibkr_agent.costs import commission, one_way_cost, round_trip_cost_rate

CFG = CostConfig()


def test_commission_minimum_applies_to_small_orders():
    assert commission(10, 30.0, CFG) == 1.0


def test_commission_capped_at_one_percent_of_value():
    assert commission(1, 50.0, CFG) == 0.5


def test_commission_per_share_for_large_orders():
    assert commission(1000, 50.0, CFG) == 5.0


def test_one_way_cost_includes_spread_and_slippage():
    # 300 美元，10bp 冲击 = 0.30，加最低佣金 1
    assert math.isclose(one_way_cost(10, 30.0, CFG), 1.30)


def test_round_trip_rate_infinite_when_cannot_buy_one_share():
    assert round_trip_cost_rate(300, 500.0, CFG) == math.inf


def test_round_trip_rate_small_position_is_expensive():
    rate = round_trip_cost_rate(300, 30.0, CFG)
    assert math.isclose(rate, 2 * 1.30 / 300)
