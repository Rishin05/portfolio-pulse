"""fact_fx_cost grid: date rule, skipped combinations, and the export files."""
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

from portfolio_pulse import db, export, fx_converter, fx_rates


def setup(conn):
    batch = db.start_batch(conn, "boc_valet", "test BoC rates")
    fx_rates.store_observations(conn, [
        SimpleNamespace(rate_date=date.fromisoformat(d), base="USD", quote="CAD", rate=D(r), series="FXUSDCAD")
        for d, r in (("2026-10-16", "1.3800"), ("2026-10-19", "1.3800"), ("2026-10-20", "1.4000"))], batch)
    for inst in ("test-broker", "test-bank"):
        db.insert_ignore(conn, "institution", {"institution_id": inst, "display_name": inst, "country": None,
                                               "website_url": None, "is_demo": 0})
    db.insert_ignore(conn, "fee_schedule", {
        "fee_schedule_id": "tb-fx", "institution_id": "test-broker", "fee_type": "fx_conversion",
        "applies_to": "CAD<->USD", "rate_bps": 150.0, "flat_amount": None, "flat_currency": None,
        "effective_from": None, "effective_to": None, "source_url": "https://fees.test/fx",
        "date_checked": "2026-10-19", "notes": None, "is_demo": 0})
    db.insert_ignore(conn, "fx_posted_rate", {                # buy side only: USD->CAD can't be computed
        "fx_posted_rate_id": "bank-2026-10-20", "institution_id": "test-bank", "base_currency": "USD",
        "quote_currency": "CAD", "rate_date": "2026-10-20", "channel": "standard",
        "customer_buy_rate": 1.44, "customer_sell_rate": None, "source_url": "https://bank.test/fx",
        "date_checked": "2026-10-20", "notes": None, "is_demo": 0})


def test_grid_starts_at_date_checked_and_skips_impossible_rows(conn):
    setup(conn)
    rows, skipped = fx_converter.cost_grid(conn)
    percent = [r for r in rows if r["model"] == "percent"]
    assert sorted({r["as_of_date"] for r in percent}) == ["2026-10-19", "2026-10-20"]   # not 2026-10-16
    assert len(percent) == 12 and all(r["cost_bps"] == 150.0 for r in percent)
    posted = [r for r in rows if r["model"] == "posted_rate"]
    assert len(posted) == 3 and {r["from_currency"] for r in posted} == {"CAD"}
    assert skipped == 3


def test_export_writes_the_three_fx_files(conn, tmp_path):
    setup(conn)
    counts = export.export_all(conn, tmp_path / "powerbi")
    assert (counts["fact_fx_cost"], counts["dim_fee_schedule"], counts["dim_fx_posted_rate"]) == (15, 1, 1)