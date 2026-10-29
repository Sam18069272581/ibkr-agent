from datetime import datetime, timezone

from ibkr_agent.cli import in_trading_window

W = ("09:45", "15:45")


def test_trading_window_new_york_time():
    # 2026-10-28 是周三；14:30 UTC = 10:30 美东（夏令时已结束前为 EDT，UTC-4）
    assert in_trading_window(datetime(2026, 10, 28, 14, 30, tzinfo=timezone.utc), W)
    assert not in_trading_window(datetime(2026, 10, 28, 13, 0, tzinfo=timezone.utc), W)
    assert not in_trading_window(datetime(2026, 10, 28, 20, 0, tzinfo=timezone.utc), W)


def test_weekend_is_closed():
    assert not in_trading_window(datetime(2026, 10, 31, 15, 0, tzinfo=timezone.utc), W)
