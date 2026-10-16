"""Export contract: the CSVs Power BI receives and their exact columns, in order.
If this fails, update docs/export_contract.md (and bump its version) in the same commit."""
import csv

import pytest

from portfolio_pulse import export, synthetic

CONTRACT = {
    "fact_daily_performance": ["account_id", "as_of_date", "prev_date", "start_value", "end_value",
                               "currency", "net_external_flow", "internal_flow", "flow_count",
                               "total_change", "investment_change", "investment_return",
                               "days_since_prev", "quality_flag", "source_id", "source_is_demo"],
    "fact_cash_flow": ["cash_flow_id", "account_id", "flow_date", "flow_type", "amount", "currency",
                       "is_external", "description", "source_id"],
    "fact_news": ["article_id", "symbol", "headline", "url", "publisher", "published_at_utc",
                  "published_date_utc", "fetched_at_utc", "source_id", "is_demo"],
    "dim_account": ["account_id", "display_name", "account_type", "base_currency", "is_demo",
                    "institution_id", "connection_id"],
    "dim_watchlist": ["symbol", "company_name", "exchange", "currency", "active"],
    "dim_source": ["source_id", "display_name", "kind", "is_demo", "usage_note"],
    "dim_date": ["date", "year", "month_num", "month", "year_month", "week_of_year",
                 "weekday_num", "weekday", "is_weekday"],
    "data_quality": ["area", "item", "status", "last_date", "age_days", "detail"],
    "export_info": ["exported_at_utc", "note"],
}


@pytest.fixture
def exported(conn, tmp_path):
    synthetic.load_demo(conn)
    out = tmp_path / "powerbi"
    export.export_all(conn, out)
    return out


def test_exactly_the_contract_files(exported):
    assert sorted(p.stem for p in exported.glob("*.csv")) == sorted(CONTRACT)


@pytest.mark.parametrize("name", sorted(CONTRACT))
def test_csv_columns_match_contract(exported, name):
    with open(exported / f"{name}.csv", newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert header == CONTRACT[name]