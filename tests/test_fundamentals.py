from datetime import date

from ibkr_agent.fundamentals import parse_company_facts, sec_ticker


def _fy(val, start, end, form="10-K"):
    return {"val": val, "start": start, "end": end, "form": form}


FACTS = {
    "facts": {
        "us-gaap": {
            "Revenues": {"units": {"USD": [
                _fy(100, "2022-01-01", "2022-12-31"),
                _fy(110, "2023-01-01", "2023-12-31"),
                _fy(30, "2023-10-01", "2023-12-31", form="10-Q"),
            ]}},
            "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": [_fy(20, "2023-01-01", "2023-12-31")]}},
            "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": [_fy(5, "2023-01-01", "2023-12-31")]}},
            "StockholdersEquity": {"units": {"USD": [{"val": 50, "end": "2023-12-31", "form": "10-K"}]}},
            "LongTermDebt": {"units": {"USD": [{"val": 25, "end": "2023-12-31", "form": "10-K"}]}},
            "WeightedAverageNumberOfDilutedSharesOutstanding": {"units": {"shares": [_fy(10, "2023-01-01", "2023-12-31")]}},
        }
    }
}


def test_parse_company_facts():
    f = parse_company_facts("TEST", FACTS)
    assert f.fiscal_year_end == date(2023, 12, 31)
    assert f.revenue == 110
    assert abs(f.revenue_growth - 0.10) < 1e-9
    assert f.free_cash_flow == 15
    assert f.debt_to_equity == 0.5
    assert f.diluted_shares == 10


def test_missing_data_gives_none():
    f = parse_company_facts("EMPTY", {"facts": {}})
    assert f.revenue is None and f.free_cash_flow is None and f.revenue_growth is None


def test_sec_ticker():
    assert sec_ticker("BRK B") == "BRK-B"
