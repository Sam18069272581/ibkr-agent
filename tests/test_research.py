import json

from ibkr_agent.research import parse_view

GOOD = {
    "bull_case": "b", "bear_case": "c", "key_risks": ["r"],
    "invalidation_conditions": ["i"], "adjustment": 0.10, "confidence": "high",
}


def test_adjustment_is_clamped():
    v = parse_view("X", json.dumps(GOOD), cap=0.03)
    assert v.adjustment == 0.03


def test_bad_output_means_no_view():
    assert parse_view("X", "not json", cap=0.03) is None
    assert parse_view("X", json.dumps({**GOOD, "confidence": "sure"}), cap=0.03) is None
    assert parse_view("X", json.dumps({"adjustment": 0.01}), cap=0.03) is None
