import pytest

from portfolio_pulse import db

APPROX = dict(abs=0.005)   # half a cent
BOC = [("2026-10-09", 1.4271), ("2026-10-13", 1.4222),
       ("2026-10-15", 1.4196), ("2026-10-16", 1.4154)]   # real BoC FXUSDCAD; Oct 12 = holiday


@pytest.fixture
def env(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    now = db.utc_now()
    b = {"boc": db.start_batch(c, "boc_valet", "test rates"),
         "st": db.start_batch(c, "snaptrade", "test rows"),
         "syn": db.start_batch(c, "synthetic", "test demo rows")}
    for d, r in BOC:
        db.insert_ignore(c, "fx_rate", {
            "rate_date": d, "base_currency": "USD", "quote_currency": "CAD", "rate": r,
            "series_id": "FXUSDCAD", "quality_flag": "reported", "source_id": "boc_valet",
            "batch_id": b["boc"], "loaded_at_utc": now})
    db.insert_ignore(c, "fx_rate", {   # demo rate deliberately far from BoC
        "rate_date": "2026-10-16", "base_currency": "USD", "quote_currency": "CAD", "rate": 1.3000,
        "series_id": "DEMO-USDCAD", "quality_flag": "demo", "source_id": "synthetic",
        "batch_id": b["syn"], "loaded_at_utc": now})
    db.ensure_account(c, "T-USD", "Test USD", "TFSA", "USD", False)
    db.ensure_account(c, "T-DEMO", "Test demo", "TFSA", "USD", True)
    c.commit()
    yield c, b
    c.close()


def add_activity(c, b, aid, atype, d, amount, cur, **extra):
    db.insert_ignore(c, "activity", {
        "activity_id": aid, "account_id": "T-USD", "activity_type": atype, "raw_type": atype,
        "trade_date": d, "amount": amount, "currency": cur, "quality_flag": "reported",
        "source_id": "snaptrade", "batch_id": b["st"], "loaded_at_utc": db.utc_now(), **extra})


def activity(c, aid):
    return c.execute("SELECT * FROM v_activity_cad WHERE activity_id = ?", (aid,)).fetchone()


def test_schema_has_fx_cad_views(env):
    c, _ = env
    assert db.current_schema_version(c) >= 3   # later migrations must not break this


def test_usd_snapshot_on_rate_day_is_reported(env):
    c, b = env
    db.insert_ignore(c, "daily_snapshot", {
        "account_id": "T-USD", "as_of_date": "2026-10-16", "end_value": 100000.00, "currency": "USD",
        "quality_flag": "reported", "source_id": "snaptrade", "batch_id": b["st"],
        "loaded_at_utc": db.utc_now()})
    r = c.execute("SELECT * FROM v_snapshot_cad WHERE account_id = 'T-USD'").fetchone()
    assert r["end_value"] == 100000.00                      # native value kept
    assert r["end_value_cad"] == pytest.approx(141540.00, **APPROX)   # 100,000 x 1.4154
    assert (r["fx_rate_date"], r["fx_gap_days"], r["fx_status"]) == ("2026-10-16", 0, "reported")


def test_cad_amount_is_identity(env):
    c, b = env
    add_activity(c, b, "cad-dep", "DEPOSIT", "2026-10-16", 250.00, "CAD")
    r = activity(c, "cad-dep")
    assert (r["amount_cad"], r["fx_rate"], r["fx_status"]) == (250.00, 1.0, "identity")


def test_weekend_usd_deposit_uses_friday_rate(env):
    c, b = env
    add_activity(c, b, "usd-dep", "DEPOSIT", "2026-10-18", 1000.00, "USD")   # Sunday
    r = activity(c, "usd-dep")
    assert r["amount_cad"] == pytest.approx(1415.40, **APPROX)               # 1,000 x 1.4154
    assert (r["fx_rate_date"], r["fx_gap_days"], r["fx_status"]) == ("2026-10-16", 2, "carried_forward")


def test_holiday_holding_uses_previous_business_day(env):
    c, b = env
    db.insert_ignore(c, "holding_snapshot", {
        "account_id": "T-USD", "as_of_date": "2026-10-12", "symbol": "CASH.USD", "asset_class": "cash",
        "quantity": 500, "market_value": 500.00, "market_value_currency": "USD",
        "quality_flag": "reported", "source_id": "snaptrade", "batch_id": b["st"],
        "loaded_at_utc": db.utc_now()})
    r = c.execute("SELECT * FROM v_holding_cad").fetchone()
    assert r["market_value_cad"] == pytest.approx(713.55, **APPROX)          # 500 x 1.4271
    assert (r["fx_rate_date"], r["fx_gap_days"], r["fx_status"]) == ("2026-10-09", 3, "carried_forward")


def test_stale_rate_is_converted_but_flagged(env):
    c, b = env
    add_activity(c, b, "late", "DIVIDEND", "2026-10-22", 200.00, "USD")
    r = activity(c, "late")
    assert r["amount_cad"] == pytest.approx(283.08, **APPROX)                # 200 x 1.4154
    assert (r["fx_gap_days"], r["fx_status"]) == (6, "stale")


def test_no_earlier_rate_is_missing_never_a_later_one(env):
    c, b = env
    add_activity(c, b, "early", "DIVIDEND", "2026-10-01", 50.00, "USD")      # before first rate
    r = activity(c, "early")
    assert (r["amount_cad"], r["fx_rate_date"], r["fx_status"]) == (None, None, "missing")


def test_cad_to_usd_conversion_converts_both_sides_and_shows_cost(env):
    c, b = env
    # Wealthsimple-style: 1,000 USD costs 1,000 x 1.4154 / 0.985 = 1,436.95 CAD
    add_activity(c, b, "fx", "FX_CONVERSION", "2026-10-16", -1436.95, "CAD",
                 counter_amount=1000.00, counter_currency="USD", reported_fx_rate=1.43695)
    r = activity(c, "fx")
    assert r["amount_cad"] == pytest.approx(-1436.95, **APPROX)
    assert r["counter_amount_cad"] == pytest.approx(1415.40, **APPROX)
    assert r["counter_fx_status"] == "reported"
    assert r["fx_conversion_cost_cad"] == pytest.approx(-21.55, **APPROX)   # loss, not a withdrawal


def test_demo_rows_use_demo_rates_only(env):
    c, b = env
    db.insert_ignore(c, "daily_snapshot", {
        "account_id": "T-DEMO", "as_of_date": "2026-10-16", "end_value": 1000.00, "currency": "USD",
        "quality_flag": "demo", "source_id": "synthetic", "batch_id": b["syn"],
        "loaded_at_utc": db.utc_now()})
    r = c.execute("SELECT * FROM v_snapshot_cad WHERE account_id = 'T-DEMO'").fetchone()
    assert r["end_value_cad"] == pytest.approx(1300.00, **APPROX)            # demo 1.3000, not BoC 1.4154
    assert r["fx_rate"] == 1.3
