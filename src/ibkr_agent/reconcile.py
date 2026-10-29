"""对账：本地记录的持仓变化必须和 IBKR 实际持仓一致。纯函数。"""

from __future__ import annotations

from .journal import FillRecord


def apply_fills(before: dict[str, float], fills: list[FillRecord]) -> dict[str, float]:
    after = dict(before)
    for f in fills:
        sign = 1 if f.side == "BUY" else -1
        after[f.ticker] = after.get(f.ticker, 0.0) + sign * f.shares
    return {t: q for t, q in after.items() if q != 0}


def differences(expected: dict[str, float], actual: dict[str, float]) -> dict[str, tuple[float, float]]:
    """返回 {股票: (预期股数, 实际股数)}，只列出不一致的。"""
    tickers = set(expected) | set(actual)
    return {
        t: (expected.get(t, 0.0), actual.get(t, 0.0))
        for t in sorted(tickers)
        if abs(expected.get(t, 0.0) - actual.get(t, 0.0)) > 1e-9
    }
