"""Open question 16: DEMO institutions use the synthetic demo USD/CAD series, so a demo-only
database (no Bank of Canada rows) still fills fact_fx_cost. Real institutions never do."""
from datetime import date
from decimal import Decimal as D

import pytest

from portfolio_pulse import db, demo_gate, export, fx_converter, synthetic


def test_demo_grid_has_rows_for_the_whole_demo_period_all_labelled_demo(conn):
    synthetic.load_demo(conn)
    assert conn.execute("SELECT COUNT(*) FROM fx_rate WHERE source_id = 'boc_valet'").fetchone()[0] == 0
    rows, skipped = fx_converter.cost_grid(conn)
    days = conn.execute("SELECT COUNT(*), MIN(rate_date), MAX(rate_date) FROM fx_rate "
                        "WHERE source_id = 'synthetic'").fetchone()
    assert skipped == 0
    assert len(rows) == days[0] * 2 * 2 * 3            # days x institutions x directions x amounts
    assert {r["quality_label"] for r in rows} == {"DEMO"} and {r["is_demo"] for r in rows} == {1}
    assert (min(r["as_of_date"] for r in rows), max(r["as_of_date"] for r in rows)) == (days[1], days[2])
    bps = {r["institution_id"]: round(r["cost_bps"], 6) for r in rows}
    assert bps == {"demo-maple-broker": 150.0, "demo-northern-invest": 50.0}


def test_demo_estimate_says_the_reference_rate_is_synthetic(conn):
    synthetic.load_demo(conn)
    res = fx_converter.convert(conn, D("1000"), "CAD", "USD", "demo-maple-broker", date(2026, 9, 30))
    assert res.quality_label == "DEMO"
    assert any("synthetic demo rate" in w for w in res.warnings)


def test_real_institution_never_uses_synthetic_rates(conn):
    synthetic.load_demo(conn)                           # synthetic rates exist, Bank of Canada rates do not
    db.insert_ignore(conn, "institution", {"institution_id": "real-broker", "display_name": "Real broker",
                                           "country": "CA", "website_url": None, "is_demo": 0})
    db.insert_ignore(conn, "fee_schedule", {
        "fee_schedule_id": "real-fx", "institution_id": "real-broker", "fee_type": "fx_conversion",
        "applies_to": "CAD<->USD", "rate_bps": 150.0, "flat_amount": None, "flat_currency": None,
        "effective_from": "2026-01-02", "effective_to": None, "source_url": "https://fees.test/fx",
        "date_checked": "2026-09-30", "notes": None, "is_demo": 0})
    assert fx_converter.rate_source(conn, "real-broker") == "boc_valet"
    with pytest.raises(fx_converter.ConverterError, match="Bank of Canada"):
        fx_converter.convert(conn, D("1000"), "CAD", "USD", "real-broker", date(2026, 9, 30))
    rows, _ = fx_converter.cost_grid(conn)
    assert "real-broker" not in {r["institution_id"] for r in rows}


def test_demo_export_fills_fact_fx_cost_and_passes_the_privacy_gate(conn, tmp_path):
    synthetic.load_demo(conn)
    counts = export.export_all(conn, tmp_path / "powerbi_demo")
    assert counts["fact_fx_cost"] > 0
    assert demo_gate.check_export(tmp_path / "powerbi_demo") == []
