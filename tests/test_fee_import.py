"""fees-import: sourced, dated fees and bank quotes - validation, rejects, idempotency."""
import pandas as pd
import pytest

from portfolio_pulse import fee_import
from portfolio_pulse.csv_import import ImportConfigError

FEE_HEADER = ("institution_id,institution_display_name,fee_type,applies_to,rate_bps,flat_amount,"
              "flat_currency,effective_from,effective_to,source_url,date_checked,notes,is_demo")
GOOD_FEE = ("demo-test-broker,Test Broker (DEMO),fx_conversion,CAD<->USD,150,,,,,"
            "https://example.com/fees/fx,2026-10-19,DEMO,1")
QUOTE_HEADER = ("institution_id,institution_display_name,base_currency,quote_currency,rate_date,"
                "channel,customer_buy_rate,customer_sell_rate,source_url,date_checked,notes,is_demo")
GOOD_QUOTE = ("demo-test-bank,Test Bank (DEMO),USD,CAD,2026-10-19,standard,1.4400,1.3600,"
              "https://example.com/rates/usd,2026-10-19,DEMO,1")


def write_csv(tmp_path, name, header, *lines):
    path = tmp_path / name
    path.write_text("\n".join([header, *lines]) + "\n", encoding="utf-8")
    return path


def count(conn, table, where="1=1"):
    return conn.execute(f"SELECT COUNT(*) FROM {table} WHERE {where}").fetchone()[0]


def test_fee_import_is_idempotent(conn, tmp_path):
    f = write_csv(tmp_path, "fees.csv", FEE_HEADER, GOOD_FEE)
    first = fee_import.import_fee_csv(conn, f, "fee")
    second = fee_import.import_fee_csv(conn, f, "fee")
    assert (first.inserted, first.institutions_created) == (1, 1)
    assert (second.inserted, second.skipped_duplicates) == (0, 1)
    assert count(conn, "fee_schedule") == 1


def test_recheck_refreshes_date_checked_without_a_new_row(conn, tmp_path):
    fee_import.import_fee_csv(conn, write_csv(tmp_path, "a.csv", FEE_HEADER, GOOD_FEE), "fee")
    rechecked = GOOD_FEE.replace(",150,", ",150.00,").replace("2026-10-19", "2026-10-20")
    res = fee_import.import_fee_csv(conn, write_csv(tmp_path, "b.csv", FEE_HEADER, rechecked), "fee")
    assert (res.inserted, res.refreshed) == (0, 1)
    assert conn.execute("SELECT date_checked FROM fee_schedule").fetchone()[0] == "2026-10-20"


def test_bad_fee_rows_go_to_rejects_with_reasons(conn, tmp_path):
    bad = [
        GOOD_FEE.replace("https://example.com/fees/fx", ""),   # no source
        GOOD_FEE.replace("https://", "http://"),               # not https
        GOOD_FEE.replace(",150,", ",1.5%,"),                   # percent sign instead of bps
        GOOD_FEE.replace("CAD<->USD", "US stocks"),            # not an FX pair
        GOOD_FEE.replace(",DEMO,1", ",real,0"),                # real row with a demo URL
        GOOD_FEE.replace("2026-10-19", "2999-01-01"),          # checked in the future
        GOOD_FEE.replace(",150,", ",,"),                       # no fee value at all
    ]
    f = write_csv(tmp_path, "bad.csv", FEE_HEADER, *bad)
    res = fee_import.import_fee_csv(conn, f, "fee", rejects_dir=tmp_path / "rejects")
    assert res.inserted == 0 and count(conn, "fee_schedule") == 0
    expected = ["source_url is missing", "https://", "not a number", "applies_to like",
                "example.com", "in the future", "give rate_bps"]
    reasons = [r["reason"] for r in res.rejected]
    assert len(reasons) == len(expected)
    for reason, fragment in zip(reasons, expected):
        assert fragment in reason
    saved = pd.read_csv(tmp_path / "rejects" / "bad_rejects.csv")
    assert list(saved["line"]) == [2, 3, 4, 5, 6, 7, 8]


def test_dry_run_writes_nothing(conn, tmp_path):
    f = write_csv(tmp_path, "fees.csv", FEE_HEADER, GOOD_FEE)
    res = fee_import.import_fee_csv(conn, f, "fee", dry_run=True)
    assert res.inserted == 0 and res.rejected == []
    assert count(conn, "fee_schedule") == 0
    assert count(conn, "institution", "institution_id = 'demo-test-broker'") == 0


def test_quotes_import_and_inverted_or_crossed_rates_are_rejected(conn, tmp_path):
    inverted = GOOD_QUOTE.replace("1.4400,1.3600", "0.7300,0.6900")   # USD per CAD by mistake
    crossed = GOOD_QUOTE.replace("1.4400,1.3600", "1.3600,1.4400")    # buy below sell
    f = write_csv(tmp_path, "q.csv", QUOTE_HEADER, GOOD_QUOTE, inverted, crossed)
    res = fee_import.import_fee_csv(conn, f, "quote")
    assert res.inserted == 1
    reasons = [r["reason"] for r in res.rejected]
    assert "looks wrong for USD/CAD" in reasons[0]
    assert "never pays more" in reasons[1]
    row = conn.execute("SELECT customer_buy_rate, customer_sell_rate, channel FROM fx_posted_rate").fetchone()
    assert tuple(row) == (1.44, 1.36, "standard")


def test_wrong_kind_explains_missing_columns(conn, tmp_path):
    f = write_csv(tmp_path, "fees.csv", FEE_HEADER, GOOD_FEE)
    with pytest.raises(ImportConfigError, match="missing column"):
        fee_import.import_fee_csv(conn, f, "quote")