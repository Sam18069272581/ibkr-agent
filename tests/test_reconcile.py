from ibkr_agent.journal import FillRecord
from ibkr_agent.reconcile import apply_fills, differences


def fill(t, side, n):
    return FillRecord("x", "t", t, side, n, 50.0, 1.0, None, 50.0, 1.0)


def test_apply_fills():
    after = apply_fills({"A": 5}, [fill("A", "SELL", 5), fill("B", "BUY", 7)])
    assert after == {"B": 7}


def test_differences_only_lists_mismatches():
    assert differences({"A": 5, "B": 7}, {"A": 5, "B": 6}) == {"B": (7, 6)}
    assert differences({"A": 5}, {"A": 5.0}) == {}
