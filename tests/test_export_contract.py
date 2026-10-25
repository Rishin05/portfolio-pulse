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
                  "published_date_utc", "fetched_at_utc", "source_id", "is_demo",
                  "published_local_date", "market_date", "story_key", "duplicate_of"],
    "bridge_news_symbol": ["article_id", "symbol", "source_id", "first_seen_utc"],
    "dim_account": ["account_id", "display_name", "account_type", "base_currency", "is_demo",
                    "institution_id", "connection_id"],
    "dim_watchlist": ["symbol", "company_name", "exchange", "currency", "active", "origin", "is_demo",
                      "last_seen_in_holdings"],
    "dim_source": ["source_id", "display_name", "kind", "is_demo", "usage_note"],
    "dim_date": ["date", "year", "month_num", "month", "year_month", "week_of_year",
                 "weekday_num", "weekday", "is_weekday"],
    "data_quality": ["area", "item", "status", "last_date", "age_days", "detail"],
    "export_info": ["exported_at_utc", "note", "contract_version"],
    "dim_fee_schedule": ["fee_schedule_id", "institution_id", "fee_type", "applies_to", "rate_bps",
                         "flat_amount", "flat_currency", "effective_from", "effective_to", "source_url",
                         "date_checked", "notes", "is_demo"],
    "dim_fx_posted_rate": ["fx_posted_rate_id", "institution_id", "base_currency", "quote_currency",
                           "rate_date", "channel", "customer_buy_rate", "customer_sell_rate", "source_url",
                           "date_checked", "notes", "is_demo"],
    "dim_symbol_universe": ["listing_key", "symbol", "exchange", "currency", "asset_type", "name", "sector",
                            "lists", "holdings_as_of", "is_demo"],
    "bridge_universe_list": ["list_id", "listing_key", "weight_pct", "holdings_as_of", "fund_name", "is_demo"],
    "fact_fx_cost": ["as_of_date", "institution_id", "model", "channel", "from_currency", "to_currency",
                     "amount", "boc_rate", "boc_rate_date", "boc_status", "platform_rate",
                     "received_at_benchmark", "received_via_platform", "notional_cad", "cost_cad", "cost_bps",
                     "quality_label", "source_ref", "source_url", "date_checked", "is_demo"],
    # --- contract 1.6 ---
    "fact_perf_account_daily": ["account_id", "household_id", "as_of_date", "prev_date", "currency",
                                "start_value", "end_value", "net_flow", "investment_change", "return_native",
                                "start_value_cad", "end_value_cad", "net_flow_cad", "investment_change_cad",
                                "return_cad", "market_change_cad", "fx_change_cad", "currency_effect",
                                "fx_status", "computed_at_utc", "quality_label", "is_demo"],
    "fact_perf_household_daily": ["household_id", "as_of_date", "accounts", "accounts_with_return",
                                  "end_value_cad", "external_flow_cad", "internal_transfer_cad",
                                  "investment_change_cad", "market_change_cad", "fx_change_cad", "return_cad",
                                  "fx_status", "computed_at_utc", "quality_label", "is_demo"],
    "fact_activity": ["activity_id", "account_id", "activity_type", "raw_type", "trade_date", "symbol",
                      "quantity", "amount", "currency", "fx_rate", "fx_rate_date", "fx_gap_days", "fx_status",
                      "amount_cad", "counter_amount", "counter_currency", "counter_amount_cad",
                      "counter_fx_status", "reported_fx_rate", "fx_conversion_cost_cad", "quality_flag",
                      "source_id"],
    "fact_holding_snapshot": ["account_id", "as_of_date", "symbol", "asset_class", "quantity", "market_value",
                              "currency", "fx_rate", "fx_rate_date", "fx_gap_days", "fx_status",
                              "market_value_cad", "quality_flag", "source_id"],
    "dim_connection": ["connection_id", "institution_id", "source_id", "display_name", "access_mode", "status",
                       "created_at_utc", "last_synced_at_utc", "is_demo"],
    "dim_institution": ["institution_id", "display_name", "country", "website_url", "is_demo"],
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



def test_dim_watchlist_labels_demo_rows(exported):
    """Contract 1.3: demo-seeded symbols reach Power BI labelled origin=demo, is_demo=1."""
    with open(exported / "dim_watchlist.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [r["symbol"] for r in rows] == ["RY", "SHOP", "XEQT"]
    assert {(r["origin"], r["is_demo"]) for r in rows} == {("demo", "1")}


def test_universe_export_is_one_row_per_listing_plus_bridge(conn, tmp_path):
    """Contract 1.4: dim = one row per SYMBOL|EXCHANGE with sorted lists; bridge = one row per membership."""
    from portfolio_pulse import universe
    synthetic.load_demo(conn)
    a = tmp_path / "a.csv"
    a.write_text("symbol,name,exchange\nSHOP,Shopify Inc,NASDAQ\nVFV,Vanguard S&P 500 Index ETF,TSX\n",
                 encoding="utf-8")
    b = tmp_path / "b.csv"
    b.write_text("symbol,name,exchange\nVFV,Vanguard S&P 500 Index ETF,TSX\n", encoding="utf-8")
    universe.import_list(conn, b, "ZLIST")
    universe.import_list(conn, a, "MANUAL")
    out = tmp_path / "pbi"
    export.export_all(conn, out)
    with open(out / "dim_symbol_universe.csv", newline="", encoding="utf-8") as f:
        dim = list(csv.DictReader(f))
    with open(out / "bridge_universe_list.csv", newline="", encoding="utf-8") as f:
        bridge = list(csv.DictReader(f))
    assert [(r["listing_key"], r["lists"]) for r in dim] == [("SHOP|NASDAQ", "MANUAL"), ("VFV|TSX", "MANUAL,ZLIST")]
    assert [(r["list_id"], r["listing_key"]) for r in bridge] == [
        ("MANUAL", "SHOP|NASDAQ"), ("MANUAL", "VFV|TSX"), ("ZLIST", "VFV|TSX")]