from datetime import date
from decimal import Decimal as D

import pytest

from portfolio_pulse import db, fx_rates

# Real BoC FXUSDCAD values from the Step 1a smoke test (Oct 12 = Thanksgiving, none).
PAYLOAD = {"observations": [
    {"d": "2026-10-08", "FXUSDCAD": {"v": "1.4240"}},
    {"d": "2026-10-09", "FXUSDCAD": {"v": "1.4271"}},
    {"d": "2026-10-13", "FXUSDCAD": {"v": "1.4222"}},
    {"d": "2026-10-14", "FXUSDCAD": {"v": "1.4263"}},
    {"d": "2026-10-15", "FXUSDCAD": {"v": "1.4196"}},
    {"d": "2026-10-16", "FXUSDCAD": {"v": "1.4154"}},
]}
START, END = date(2026, 10, 8), date(2026, 10, 16)


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_db(c)
    yield c
    c.close()


def fake_fetch(payload):
    calls = []
    def _fetch(url):
        calls.append(url)
        return payload
    _fetch.calls = calls
    return _fetch


def boc_count(conn):
    return conn.execute("SELECT COUNT(*) FROM fx_rate WHERE source_id = 'boc_valet'").fetchone()[0]


def test_sync_stores_and_rerun_adds_zero(conn):
    first = fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=fake_fetch(PAYLOAD))
    second = fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=fake_fetch(PAYLOAD))
    assert (first["inserted"], first["skipped"]) == (6, 0)
    assert (second["inserted"], second["skipped"]) == (0, 6)
    assert boc_count(conn) == 6
    row = conn.execute("SELECT rate, series_id, quality_flag FROM fx_rate "
                       "WHERE rate_date = '2026-10-16'").fetchone()
    assert (row["rate"], row["series_id"], row["quality_flag"]) == (1.4154, "FXUSDCAD", "reported")
    batches = conn.execute("SELECT COUNT(*) FROM import_batch WHERE source_id = 'boc_valet'").fetchone()[0]
    assert batches == 2


def test_cached_sync_makes_no_request_when_up_to_date(conn):
    fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=fake_fetch(PAYLOAD))
    def must_not_fetch(url):
        raise AssertionError("should not hit the network")
    result = fx_rates.sync_boc_rates(conn, end=END, fetch=must_not_fetch)
    assert result["status"] == "up_to_date"


def test_incremental_sync_starts_after_latest_stored_date(conn):
    fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=fake_fetch(PAYLOAD))
    fetch = fake_fetch({"observations": []})
    fx_rates.sync_boc_rates(conn, end=date(2026, 10, 19), fetch=fetch)
    assert fetch.calls[0].endswith("start_date=2026-10-17&end_date=2026-10-19")


def test_network_failure_never_raises_or_writes(conn):
    def offline(url):
        raise OSError("no network")
    result = fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=offline)
    assert result["status"] == "unavailable"
    assert boc_count(conn) == 0


def test_dry_run_writes_nothing(conn):
    result = fx_rates.sync_boc_rates(conn, start=START, end=END, dry_run=True,
                                     fetch=fake_fetch(PAYLOAD))
    assert result["fetched"] == 6
    assert boc_count(conn) == 0


def test_rate_for_weekend_and_holiday_from_db(conn):
    fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=fake_fetch(PAYLOAD))
    sat = fx_rates.rate_for(conn, "USD", "CAD", date(2026, 10, 17))
    hol = fx_rates.rate_for(conn, "USD", "CAD", date(2026, 10, 12))
    assert (sat.rate_date, sat.rate, sat.status, sat.gap_days) == (
        date(2026, 10, 16), D("1.4154"), "carried_forward", 1)
    assert (hol.rate_date, hol.rate, hol.status, hol.gap_days) == (
        date(2026, 10, 9), D("1.4271"), "carried_forward", 3)


def test_rate_for_never_looks_ahead_and_flags_stale(conn):
    fx_rates.sync_boc_rates(conn, start=START, end=END, fetch=fake_fetch(PAYLOAD))
    assert fx_rates.rate_for(conn, "USD", "CAD", date(2026, 10, 7)).status == "missing"
    stale = fx_rates.rate_for(conn, "USD", "CAD", date(2026, 10, 22))
    assert (stale.status, stale.gap_days) == ("stale", 6)


def test_rate_for_same_currency_is_identity(conn):
    r = fx_rates.rate_for(conn, "CAD", "CAD", date(2026, 10, 17))
    assert (r.rate, r.status) == (D("1"), "identity")