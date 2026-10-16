"""Synthetic data reproducibility + the core cash-flow vs performance rule."""
import pytest

from portfolio_pulse import db, synthetic


def perf(conn, account="DEMO-TFSA"):
    return conn.execute("SELECT * FROM v_daily_performance WHERE account_id=? ORDER BY as_of_date",
                        (account,)).fetchall()


def test_same_seed_same_data():
    a, fa = synthetic.generate_days(seed=7)
    b, fb = synthetic.generate_days(seed=7)
    c, _ = synthetic.generate_days(seed=8)
    assert [d.end_value for d in a] == [d.end_value for d in b]
    assert fa == fb
    assert [d.end_value for d in a] != [d.end_value for d in c]


def test_demo_load_is_idempotent(conn):
    first = synthetic.load_demo(conn)
    second = synthetic.load_demo(conn)
    assert first["inserted"] > 0
    assert second["inserted"] == 0 and second["skipped"] == first["inserted"]
    assert conn.execute("SELECT COUNT(*) FROM daily_snapshot").fetchone()[0] == len(synthetic.generate_days()[0])


def test_view_recovers_true_returns_and_excludes_deposits(conn):
    synthetic.load_demo(conn)
    days, flows = synthetic.generate_days()
    rows = perf(conn)
    assert len(rows) == len(days)
    for d, r in zip(days[1:], rows[1:]):
        assert r["investment_return"] == pytest.approx(d.true_return, abs=1e-9)
        assert r["net_external_flow"] == pytest.approx(d.external_flow)
    # A deposit day: total change includes the deposit, investment change does not.
    dep_day = next(r for r in rows if r["net_external_flow"] > 0)
    assert dep_day["total_change"] - dep_day["investment_change"] == pytest.approx(dep_day["net_external_flow"])


def test_dividend_and_fee_count_as_return_not_contribution(conn):
    synthetic.load_demo(conn)
    rows = {r["as_of_date"]: r for r in perf(conn)}
    div = conn.execute("SELECT flow_date FROM cash_flow WHERE flow_type='DIVIDEND'").fetchone()[0]
    assert rows[div]["net_external_flow"] == 0
    assert rows[div]["internal_flow"] == pytest.approx(42.17)


def test_weekend_deposit_rolls_into_next_trading_day(conn):
    b = db.start_batch(conn, "synthetic", "t")
    db.ensure_account(conn, "A", "A", None, "CAD", True)
    now = db.utc_now()
    for d, v in [("2026-07-03", 1000.0), ("2026-07-06", 1510.0)]:   # Fri, Mon
        db.insert_ignore(conn, "daily_snapshot", {"account_id": "A", "as_of_date": d, "end_value": v,
                         "currency": "CAD", "quality_flag": "demo", "source_id": "synthetic",
                         "batch_id": b, "loaded_at_utc": now})
    db.insert_ignore(conn, "cash_flow", {"cash_flow_id": "x", "account_id": "A", "flow_date": "2026-07-04",
                     "flow_type": "DEPOSIT", "amount": 500.0, "currency": "CAD", "is_external": 1,
                     "description": "", "source_id": "synthetic", "batch_id": b, "loaded_at_utc": now})
    mon = perf(conn, "A")[1]
    assert mon["net_external_flow"] == 500
    assert mon["investment_change"] == pytest.approx(10.0)          # not $510
    assert mon["investment_return"] == pytest.approx(10 / 1500)
    assert mon["days_since_prev"] == 3


def test_calendar_order_and_extremes(conn):
    synthetic.load_demo(conn)
    rows = perf(conn)
    dates = [r["as_of_date"] for r in rows]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    scored = [r for r in rows if r["investment_return"] is not None]
    best = max(scored, key=lambda r: r["investment_return"])
    worst = min(scored, key=lambda r: r["investment_return"])
    days = synthetic.generate_days()[0][1:]
    assert best["as_of_date"] == max(days, key=lambda d: d.true_return).as_of_date.isoformat()
    assert worst["as_of_date"] == min(days, key=lambda d: d.true_return).as_of_date.isoformat()
    assert best["investment_return"] > 0 > worst["investment_return"]
