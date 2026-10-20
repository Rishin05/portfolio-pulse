"""FX Fee Converter against a real (temporary) database: rates, fees, quotes, labels, errors."""
from datetime import date
from decimal import Decimal as D
from types import SimpleNamespace

import pytest

from portfolio_pulse import db, fx_converter, fx_rates


def add_boc(conn, *pairs):
    batch = db.start_batch(conn, "boc_valet", "test BoC rates")
    obs = [SimpleNamespace(rate_date=date.fromisoformat(d), base="USD", quote="CAD", rate=D(r), series="FXUSDCAD")
           for d, r in pairs]
    fx_rates.store_observations(conn, obs, batch)


def add_inst(conn, inst, demo=0):
    db.insert_ignore(conn, "institution", {"institution_id": inst, "display_name": inst, "country": None,
                                           "website_url": None, "is_demo": demo})


def add_fee(conn, inst="test-broker", bps=150.0, demo=0):
    add_inst(conn, inst, demo)
    db.insert_ignore(conn, "fee_schedule", {
        "fee_schedule_id": f"{inst}-fx", "institution_id": inst, "fee_type": "fx_conversion",
        "applies_to": "CAD<->USD", "rate_bps": bps, "flat_amount": None, "flat_currency": None,
        "effective_from": None, "effective_to": None,
        "source_url": "https://example.com/fees" if demo else "https://fees.test/fx",
        "date_checked": "2026-10-19", "notes": None, "is_demo": demo})


def add_quote(conn, rate_date, buy, sell, inst="test-bank"):
    add_inst(conn, inst)
    db.insert_ignore(conn, "fx_posted_rate", {
        "fx_posted_rate_id": f"{inst}-{rate_date}", "institution_id": inst, "base_currency": "USD",
        "quote_currency": "CAD", "rate_date": rate_date, "channel": "standard",
        "customer_buy_rate": buy, "customer_sell_rate": sell, "source_url": "https://bank.test/fx",
        "date_checked": rate_date, "notes": None, "is_demo": 0})


def convert(conn, amount, frm, to, inst, day):
    return fx_converter.convert(conn, D(amount), frm, to, inst, date.fromisoformat(day))


def test_percent_fee_same_day_rate_matches_example_a(conn):
    add_boc(conn, ("2026-10-19", "1.3800"))
    add_fee(conn)
    res = convert(conn, "1000", "CAD", "USD", "test-broker", "2026-10-19")
    assert (res.estimate.cost_cad, res.estimate.cost_bps) == (D("15.00"), D("150.00"))
    assert (res.boc_status, res.quality_label) == ("reported", "estimated")


def test_weekend_uses_prior_rate_labelled_stale(conn):
    add_boc(conn, ("2026-10-16", "1.3800"))
    add_fee(conn)
    res = convert(conn, "1000", "CAD", "USD", "test-broker", "2026-10-18")   # Sunday
    assert (res.boc_status, res.boc_rate_date, res.quality_label) == ("carried_forward", date(2026, 10, 16), "STALE")
    assert any("2026-10-16" in w for w in res.warnings)


def test_rate_older_than_limit_is_an_error(conn):
    add_boc(conn, ("2026-10-09", "1.3800"))
    add_fee(conn)
    with pytest.raises(fx_converter.ConverterError, match="fetch-fx"):
        convert(conn, "1000", "CAD", "USD", "test-broker", "2026-10-19")


def test_no_boc_rate_is_an_error(conn):
    add_fee(conn)
    with pytest.raises(fx_converter.ConverterError, match="No Bank of Canada"):
        convert(conn, "1000", "CAD", "USD", "test-broker", "2026-10-19")


def test_missing_fee_is_an_error_not_zero(conn):
    add_boc(conn, ("2026-10-19", "1.3800"))
    add_inst(conn, "test-broker")
    with pytest.raises(fx_converter.ConverterError, match="fees-import"):
        convert(conn, "1000", "CAD", "USD", "test-broker", "2026-10-19")


def test_posted_quote_matches_example_d(conn):
    add_boc(conn, ("2026-10-19", "1.3800"))
    add_quote(conn, "2026-10-19", 1.44, 1.36)
    res = convert(conn, "500", "USD", "CAD", "test-bank", "2026-10-19")
    assert (res.estimate.cost_cad, res.estimate.cost_bps) == (D("10.00"), D("144.93"))
    assert res.quality_label == "reported"


def test_posted_quote_is_never_reused_on_another_day(conn):
    add_boc(conn, ("2026-10-16", "1.3800"), ("2026-10-19", "1.3800"))
    add_quote(conn, "2026-10-19", 1.44, 1.36)
    with pytest.raises(fx_converter.ConverterError, match="never reused"):
        convert(conn, "500", "USD", "CAD", "test-bank", "2026-10-16")


def test_demo_fee_is_labelled_demo(conn):
    add_boc(conn, ("2026-10-19", "1.3800"))
    add_fee(conn, inst="demo-broker", demo=1)
    res = convert(conn, "1000", "CAD", "USD", "demo-broker", "2026-10-19")
    assert res.quality_label == "DEMO"
    assert "[DEMO]" in fx_converter.describe(res)[0]


def test_unknown_platform_is_an_error(conn):
    add_boc(conn, ("2026-10-19", "1.3800"))
    with pytest.raises(fx_converter.ConverterError, match="Unknown platform"):
        convert(conn, "1000", "CAD", "USD", "nobody", "2026-10-19")