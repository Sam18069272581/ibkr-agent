"""从 SEC EDGAR 的 XBRL companyfacts 取年报数据。

财务数字一律从这里由代码取得和计算，不让模型生成数字。
文档：https://www.sec.gov/search-filings/edgar-application-programming-interfaces
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date

import httpx

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"

REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "SalesRevenueNet",
)
OPERATING_CF_TAGS = ("NetCashProvidedByUsedInOperatingActivities",)
CAPEX_TAGS = ("PaymentsToAcquirePropertyPlantAndEquipment",)
NET_INCOME_TAGS = ("NetIncomeLoss",)
EQUITY_TAGS = ("StockholdersEquity",)
DEBT_TAGS = ("LongTermDebt", "LongTermDebtNoncurrent")
SHARES_TAGS = ("WeightedAverageNumberOfDilutedSharesOutstanding",)


@dataclass(frozen=True)
class Fundamentals:
    ticker: str
    fiscal_year_end: date | None
    revenue: float | None
    revenue_growth: float | None
    net_income: float | None
    free_cash_flow: float | None
    debt_to_equity: float | None
    diluted_shares: float | None

    def as_dict(self) -> dict:
        return {
            "ticker": self.ticker,
            "fiscal_year_end": self.fiscal_year_end.isoformat() if self.fiscal_year_end else None,
            "revenue_usd": self.revenue,
            "revenue_growth_yoy": self.revenue_growth,
            "net_income_usd": self.net_income,
            "free_cash_flow_usd": self.free_cash_flow,
            "debt_to_equity": self.debt_to_equity,
            "diluted_shares": self.diluted_shares,
        }


def _annual_series(facts: dict, tags: tuple[str, ...], unit: str = "USD") -> list[tuple[date, float]]:
    """10-K 中时长约一年的数值，按期末日期去重并升序排列。取第一个有数据的标签。"""
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        entries = gaap.get(tag, {}).get("units", {}).get(unit, [])
        by_end: dict[date, float] = {}
        for e in entries:
            if e.get("form") != "10-K" or "start" not in e:
                continue
            start, end = date.fromisoformat(e["start"]), date.fromisoformat(e["end"])
            if 350 <= (end - start).days <= 380:
                by_end[end] = float(e["val"])  # 后出现的（更新的申报）覆盖先前的
        if by_end:
            return sorted(by_end.items())
    return []


def _latest_instant(facts: dict, tags: tuple[str, ...], unit: str = "USD") -> float | None:
    gaap = facts.get("facts", {}).get("us-gaap", {})
    for tag in tags:
        entries = [
            e
            for e in gaap.get(tag, {}).get("units", {}).get(unit, [])
            if e.get("form") in ("10-K", "10-Q") and "start" not in e
        ]
        if entries:
            return float(max(entries, key=lambda e: e["end"])["val"])
    return None


def parse_company_facts(ticker: str, facts: dict) -> Fundamentals:
    revenue = _annual_series(facts, REVENUE_TAGS)
    ocf = dict(_annual_series(facts, OPERATING_CF_TAGS))
    capex = dict(_annual_series(facts, CAPEX_TAGS))
    net_income = _annual_series(facts, NET_INCOME_TAGS)
    shares = _annual_series(facts, SHARES_TAGS, unit="shares")

    latest_end = revenue[-1][0] if revenue else None
    growth = None
    if len(revenue) >= 2 and revenue[-2][1] > 0:
        growth = revenue[-1][1] / revenue[-2][1] - 1

    fcf = None
    if latest_end and latest_end in ocf:
        fcf = ocf[latest_end] - capex.get(latest_end, 0.0)

    equity = _latest_instant(facts, EQUITY_TAGS)
    debt = _latest_instant(facts, DEBT_TAGS)
    d2e = debt / equity if debt is not None and equity and equity > 0 else None

    return Fundamentals(
        ticker=ticker,
        fiscal_year_end=latest_end,
        revenue=revenue[-1][1] if revenue else None,
        revenue_growth=growth,
        net_income=net_income[-1][1] if net_income else None,
        free_cash_flow=fcf,
        debt_to_equity=d2e,
        diluted_shares=shares[-1][1] if shares else None,
    )


def sec_ticker(ticker: str) -> str:
    """IBKR 用 'BRK B'，SEC 用 'BRK-B'。"""
    return ticker.replace(" ", "-").upper()


class EdgarClient:
    def __init__(self, user_agent: str | None = None):
        ua = user_agent or os.environ.get("SEC_USER_AGENT")
        if not ua:
            raise RuntimeError("SEC 要求设置 User-Agent：请在 .env 中设置 SEC_USER_AGENT")
        self._http = httpx.Client(headers={"User-Agent": ua}, timeout=30)
        self._cik: dict[str, int] | None = None

    def cik(self, ticker: str) -> int:
        if self._cik is None:
            rows = self._http.get(TICKERS_URL).raise_for_status().json().values()
            self._cik = {r["ticker"].upper(): int(r["cik_str"]) for r in rows}
        return self._cik[sec_ticker(ticker)]

    def fundamentals(self, ticker: str) -> Fundamentals:
        facts = self._http.get(FACTS_URL.format(cik=self.cik(ticker))).raise_for_status().json()
        return parse_company_facts(ticker, facts)
