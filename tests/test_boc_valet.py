from datetime import date
from decimal import Decimal

import pytest

from portfolio_pulse.boc_valet import (
    build_url,
    parse_observations,
    resolve_rate,
    series_to_pair,
)

# Test values only (not real BoC rates).
PAYLOAD = {
    "observations": [
        {"d": "2026-10-16", "FXUSDCAD": {"v": "1.3750"}, "FXEURCAD": {"v": "1.6010"}},
        {"d": "2026-10-15", "FXUSDCAD": {"v": "1.3800"}},
    ]
}


def test_series_to_pair():
    assert series_to_pair("FXUSDCAD") == ("USD", "CAD")


def test_series_to_pair_rejects_bad_code():
    with pytest.raises(ValueError):
        series_to_pair("USDCAD")


def test_build_url():
    url = build_url(["FXUSDCAD", "FXEURCAD"], date(2026, 10, 8), date(2026, 10, 16))
    assert url == (
        "https://www.bankofcanada.ca/valet/observations/FXUSDCAD,FXEURCAD/json"
        "?start_date=2026-10-08&end_date=2026-10-16"
    )


def test_parse_reads_values_as_exact_decimals_sorted():
    obs, warnings = parse_observations(PAYLOAD, ["FXUSDCAD"])
    assert warnings == []
    assert [(o.rate_date, o.rate) for o in obs] == [
        (date(2026, 10, 15), Decimal("1.3800")),
        (date(2026, 10, 16), Decimal("1.3750")),
    ]
    assert (obs[0].base, obs[0].quote) == ("USD", "CAD")


def test_parse_multiple_series_skips_absent_values():
    obs, _ = parse_observations(PAYLOAD, ["FXUSDCAD", "FXEURCAD"])
    eur = [o for o in obs if o.series == "FXEURCAD"]
    assert len(obs) == 3  # EUR has no 10-15 value: skipped, not an error
    assert eur[0].rate == Decimal("1.6010")


def test_parse_warns_on_bad_values_and_skips_blank():
    payload = {
        "observations": [
            {"d": "2026-10-15", "FXUSDCAD": {"v": ""}},      # blank: silent skip
            {"d": "2026-10-16", "FXUSDCAD": {"v": "abc"}},   # warn
            {"d": "not-a-date", "FXUSDCAD": {"v": "1.37"}},  # warn
            {"d": "2026-10-14", "FXUSDCAD": {"v": "-1"}},    # warn
        ]
    }
    obs, warnings = parse_observations(payload, ["FXUSDCAD"])
    assert obs == []
    assert len(warnings) == 3


def test_resolve_exact_date_is_reported():
    r = resolve_rate({date(2026, 10, 16): Decimal("1.3750")}, date(2026, 10, 16))
    assert (r.rate, r.status, r.gap_days) == (Decimal("1.3750"), "reported", 0)


def test_resolve_weekend_uses_friday():
    rates = {date(2026, 10, 16): Decimal("1.3750")}  # Friday
    sat = resolve_rate(rates, date(2026, 10, 17))
    sun = resolve_rate(rates, date(2026, 10, 18))
    assert (sat.rate_date, sat.rate, sat.status, sat.gap_days) == (
        date(2026, 10, 16), Decimal("1.3750"), "carried_forward", 1)
    assert (sun.status, sun.gap_days) == ("carried_forward", 2)


def test_resolve_holiday_monday_uses_previous_friday():
    # Thanksgiving Monday 2026-10-12: BoC publishes nothing.
    rates = {date(2026, 10, 9): Decimal("1.3810"), date(2026, 10, 13): Decimal("1.3790")}
    r = resolve_rate(rates, date(2026, 10, 12))
    assert (r.rate_date, r.rate, r.status, r.gap_days) == (
        date(2026, 10, 9), Decimal("1.3810"), "carried_forward", 3)


def test_resolve_gap_beyond_window_is_stale():
    r = resolve_rate({date(2026, 10, 1): Decimal("1.3600")}, date(2026, 10, 9))
    assert (r.status, r.gap_days, r.rate) == ("stale", 8, Decimal("1.3600"))


def test_resolve_never_looks_ahead():
    r = resolve_rate({date(2026, 10, 20): Decimal("1.3700")}, date(2026, 10, 19))
    assert (r.status, r.rate, r.rate_date) == ("missing", None, None)


def test_resolve_empty_is_missing():
    assert resolve_rate({}, date(2026, 10, 19)).status == "missing"