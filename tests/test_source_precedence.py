import pytest

from portfolio_pulse import db, performance_store, synthetic

BOC = [("2026-10-09", 1.4271), ("2026-10-13", 1.4222),
       ("2026-10-15", 1.4196), ("2026-10-16", 1.4154)]   # real BoC FXUSDCAD


@pytest.fixture
def env(tmp_path):
    c = db.connect(tmp_path / "t.db")
    db.init_db(c)
    b = {s: db.start_batch(c, s, "test") for s in ("boc_valet", "snaptrade", "csv_import", "alpha_vantage")}
    for d, r in BOC:
        db.insert_ignore(c, "fx_rate", {
            "rate_date": d, "base_currency": "USD", "quote_currency": "CAD", "rate": r,
            "series_id": "FXUSDCAD", "quality_flag": "reported", "source_id": "boc_valet",
            "batch_id": b["boc_valet"], "loaded_at_utc": db.utc_now()})
    db.ensure_account(c, "R-USD", "Real USD", "TFSA", "USD", False)
    db.ensure_account(c, "R-CAD", "Real CAD", "TFSA", "CAD", False)
    c.commit()
    yield c, b
    c.close()


def reported(c, b, acct, d, value, ccy="USD", source="snaptrade", quality="reported"):
    db.insert_ignore(c, "daily_snapshot", {
        "account_id": acct, "as_of_date": d, "end_value": value, "currency": ccy,
        "quality_flag": quality, "source_id": source, "batch_id": b[source],
        "loaded_at_utc": db.utc_now()})


def rebuilt(c, b, acct, d, value, ccy="USD", share=1.0):
    db.insert_ignore(c, "reconstructed_snapshot", {
        "account_id": acct, "as_of_date": d, "end_value": value, "currency": ccy,
        "priced_share": share, "quality_flag": "estimated", "source_id": "alpha_vantage",
        "batch_id": b["alpha_vantage"], "loaded_at_utc": db.utc_now()})


def best(c, acct, d):
    return c.execute("SELECT * FROM v_snapshot_cad WHERE account_id = ? AND as_of_date = ?",
                     (acct, d)).fetchone()


def test_reported_beats_reconstructed_and_reconstruction_fills_gaps(env):
    c, b = env
    rebuilt(c, b, "R-USD", "2026-10-15", 10000.00)
    rebuilt(c, b, "R-USD", "2026-10-16", 10050.00)
    reported(c, b, "R-USD", "2026-10-16", 10040.00)
    gap_day, both = best(c, "R-USD", "2026-10-15"), best(c, "R-USD", "2026-10-16")
    assert (gap_day["method"], gap_day["end_value"], gap_day["quality_label"]) == (
        "reconstructed", 10000.00, "estimated")
    assert (both["method"], both["source_id"], both["end_value"], both["precedence"],
            both["candidates"], both["reconciliation"]) == (
        "reported", "snaptrade", 10040.00, 1, 2, "ok")            # 0.10% apart: within 0.5%
    assert both["quality_label"] == "reported"


def test_difference_over_half_percent_is_flagged_but_reported_wins(env):
    c, b = env
    reported(c, b, "R-USD", "2026-10-16", 10000.00)
    rebuilt(c, b, "R-USD", "2026-10-16", 10100.00)                 # 1.00% apart
    r = best(c, "R-USD", "2026-10-16")
    assert (r["end_value"], r["reconciliation"]) == (10000.00, "mismatch")


def test_csv_beats_reconstruction(env):
    c, b = env
    reported(c, b, "R-CAD", "2026-10-16", 5000.00, "CAD", source="csv_import")
    rebuilt(c, b, "R-CAD", "2026-10-16", 4990.00, "CAD")
    r = best(c, "R-CAD", "2026-10-16")
    assert (r["source_id"], r["precedence"], r["end_value"], r["reconciliation"]) == (
        "csv_import", 2, 5000.00, "ok")                            # 0.20% apart


def test_incomplete_reconstruction_is_never_used(env):
    c, b = env
    rebuilt(c, b, "R-USD", "2026-10-15", 9000.00, share=0.9)       # 10% of value unpriced
    assert best(c, "R-USD", "2026-10-15") is None
    assert c.execute("SELECT COUNT(*) FROM reconstructed_snapshot").fetchone()[0] == 1   # kept for review


def test_quality_labels(env):
    c, b = env
    reported(c, b, "R-USD", "2026-10-16", 100.0)                   # exact BoC day
    reported(c, b, "R-USD", "2026-10-12", 100.0)                   # Thanksgiving: carried forward 3 days
    reported(c, b, "R-USD", "2026-10-22", 100.0)                   # 6 days after last rate
    reported(c, b, "R-USD", "2026-10-01", 100.0)                   # before the first rate
    reported(c, b, "R-CAD", "2026-10-16", 100.0, "CAD")
    db.ensure_account(c, "D-USD", "Paper", "TFSA", "USD", True)
    reported(c, b, "D-USD", "2026-10-16", 100.0, quality="demo")
    labels = {(r["account_id"], r["as_of_date"]): r["quality_label"]
              for r in c.execute("SELECT account_id, as_of_date, quality_label FROM v_snapshot_cad")}
    assert labels == {
        ("R-USD", "2026-10-16"): "reported",
        ("R-USD", "2026-10-12"): "estimated",
        ("R-USD", "2026-10-22"): "STALE",
        ("R-USD", "2026-10-01"): "MISSING",
        ("R-CAD", "2026-10-16"): "reported",
        ("D-USD", "2026-10-16"): "DEMO",
    }


def test_engine_uses_reconstructed_history_then_reported(env):
    c, b = env
    rebuilt(c, b, "R-USD", "2026-10-15", 10000.00)                 # before connecting
    reported(c, b, "R-USD", "2026-10-16", 10100.00)                # first broker value
    c.commit()
    performance_store.run_and_save(c)
    r = c.execute("SELECT * FROM v_perf_account_daily WHERE as_of_date = '2026-10-16'").fetchone()
    assert r["return_native"] == pytest.approx(0.01)
    assert r["return_cad"] == pytest.approx(10100 * 1.4154 / (10000 * 1.4196) - 1)   # +0.7012%
    assert r["quality_label"] == "estimated"                       # start value was reconstructed
    h = c.execute("SELECT * FROM v_perf_household_daily WHERE as_of_date = '2026-10-16'").fetchone()
    assert (h["household_id"], h["quality_label"]) == ("REAL", "estimated")


def test_demo_household_labelled_demo_and_snapshots_unchanged(tmp_path):
    c = db.connect(tmp_path / "demo.db")
    db.init_db(c)
    synthetic.load_demo(c, 42)
    performance_store.run_and_save(c)
    assert {r[0] for r in c.execute("SELECT DISTINCT quality_label FROM v_perf_household_daily")} == {"DEMO"}
    n_raw = c.execute("SELECT COUNT(*) FROM daily_snapshot").fetchone()[0]
    n_best = c.execute("SELECT COUNT(*) FROM v_snapshot_cad").fetchone()[0]
    assert n_raw == n_best > 0
    c.close()