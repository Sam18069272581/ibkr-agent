"""从日线收盘价算出的几个行情事实。Opus 的材料、快哨兵的材料和边界检查都用这里，口径一致。

收盘价按时间从旧到新排列，最后一根是今天还没收完的日线（ccxt 的 OHLCV 就是这样，日线按 UTC 零点切）。
所以 closes[-2] 是今天 UTC 零点的价格，closes[-2 - n] 是 n 天前 UTC 零点的价格，离现在 n 天多一点。
“n 天前的价格”取 closes[-2 - n]：保证至少过去了 n 天。用 closes[-1 - n] 的话其实只过去 n-1 天多，
“7 天跌幅”会漏掉 6 天多以前那次下跌。需要 n 天前的价格时，至少要取 n + 2 根日线。
"""

from __future__ import annotations

import math

WEEK_DAYS = 7          # “7 天涨跌”“7 天跌幅”里的 7 天
VOL_DAYS = 30          # 年化波动率用最近 30 个日收益
MIN_VOL_SAMPLES = 10   # 日收益少于这么多个时不算波动率
CRYPTO_DAYS_PER_YEAR = 365  # 币圈全年交易，年化用 365 天


def candles_for(days: int) -> int:
    """要算 days 天前的价格，至少要取多少根日线（今天没收完的一根 + 今天零点那根 + days 根）。"""
    return days + 2


def close_days_ago(closes: list[float] | None, days: int) -> float | None:
    """至少 days 天以前（UTC 零点）的收盘价；数据不够长或价格不是正数时返回 None。"""
    if not closes or len(closes) < candles_for(days):
        return None
    c = closes[-2 - days]
    return c if c > 0 else None


def period_return(closes: list[float] | None, price: float, days: int, digits: int = 4) -> float | None:
    """现价相对 days 天前收盘价的涨跌幅。"""
    past = close_days_ago(closes, days)
    return None if past is None else round(price / past - 1, digits)


def annualized_vol(closes: list[float] | None, days: int = VOL_DAYS, digits: int = 4) -> float | None:
    """最近 days 个日对数收益的年化波动率；样本太少时返回 None。"""
    if not closes:
        return None
    tail = closes[-(days + 1):]
    daily = [math.log(b / a) for a, b in zip(tail, tail[1:], strict=False) if a > 0 and b > 0]
    if len(daily) < MIN_VOL_SAMPLES:
        return None
    m = sum(daily) / len(daily)
    return round(math.sqrt(sum((x - m) ** 2 for x in daily) / (len(daily) - 1)) * math.sqrt(CRYPTO_DAYS_PER_YEAR), digits)


def drawdown_from_high(closes: list[float] | None, price: float, days: int, digits: int = 4) -> float | None:
    """现价相对最近 days 根日线最高收盘价的回撤（负数）。"""
    window = [c for c in (closes or [])[-days:] if c > 0]
    return round(price / max(window) - 1, digits) if window else None


def vs_average(closes: list[float] | None, price: float, days: int, digits: int = 4) -> float | None:
    """现价相对最近 days 根日线均价的偏离；数据不够 days 根时返回 None。"""
    if not closes or len(closes) < days:
        return None
    avg = sum(closes[-days:]) / days
    return round(price / avg - 1, digits) if avg > 0 else None
