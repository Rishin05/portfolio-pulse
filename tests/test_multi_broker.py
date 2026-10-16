"""Phase 1 multi-broker model: idempotent demo, reconciliation, FX conversions, constraints."""
import sqlite3

import pytest

from portfolio_pulse import db, synthetic

DATA_TABLES = ["account", "institution", "connection", "daily_snapshot", "cash_flow", "watchlist",
               "news_article", "activity", "holding_snapshot", "fx_rate", "fee_schedule"]

# Project rule: every money column has a currency column beside it.
MONEY_COLUMNS = {
    "activity": [("price", "price_currency"), ("amount", "currency"),
                 ("fee_amount", "fee_currency"), ("counter_amount", "counter_currency")],
    "holding_snapshot": [("price", "price_currency"), ("market_value", "market_value_currency"),
                         ("book_value", "book_value_currency")],
    "fee_schedule": [("flat_amount", "flat_currency")],
    "daily_snapshot": [("end_value", "currency")],
    "cash_flow": [("amount", "currency")],
}


def counts(conn):
    return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in DATA_TABLES}


@pytest.fixture
def demo(conn):
    synthetic.load_demo(conn)
    return conn


def test_rerunning_demo_adds_zero_rows_anywhere(demo):
    before = counts(demo)
    assert before["activity"] == 33 and before["holding_snapshot"] == 45
    assert before["fx_rate"] == 194 and before["fee_schedule"] == 4
    result = synthetic.load_demo(demo)
    assert result["inserted"] == 0
    assert counts(demo) == before


def test_original_demo_figures_unchanged(demo):
    rows = demo.execute("SELECT as_of_date, investment_return FROM v_daily_performance "
                        "WHERE account_id='DEMO-TFSA' AND investment_return IS NOT NULL").fetchall()
    best = max(rows, key=lambda r: r[1])
    worst = min(rows, key=lambda r: r[1])
    assert best[0] == "2026-03-30" and best[1] == pytest.approx(0.0459, abs=5e-5)
    assert worst[0] == "2026-01-16" and worst[1] == pytest.approx(-0.0218, abs=5e-5)
    contrib, gain = demo.execute("SELECT SUM(net_external_flow), SUM(investment_change) "
                                 "FROM v_daily_performance WHERE account_id='DEMO-TFSA'").fetchone()
    assert contrib == pytest.approx(6250, abs=0.01)
    assert gain == pytest.approx(3672, abs=1)


def test_tfsa_holdings_add_up_to_daily_snapshot(demo):
    rows = demo.execute(
        "SELECT h.as_of_date, SUM(h.market_value), s.end_value FROM holding_snapshot h "
        "JOIN daily_snapshot s ON s.account_id = h.account_id AND s.as_of_date = h.as_of_date "
        "WHERE h.account_id = 'DEMO-TFSA' GROUP BY h.as_of_date").fetchall()
    assert len(rows) == 9                       # one per month-end, Jan-Sep
    for _, total, end_value in rows:
        assert total == pytest.approx(end_value, abs=0.005)


def test_usd_holdings_reconcile_to_activity_ledger(demo):
    last = demo.execute("SELECT MAX(as_of_date) FROM holding_snapshot "
                        "WHERE account_id='DEMO-USD'").fetchone()[0]
    for cur in ("USD", "CAD"):
        ledger = demo.execute(
            "SELECT COALESCE(SUM(CASE WHEN currency = ? THEN amount END), 0) "
            "     + COALESCE(SUM(CASE WHEN counter_currency = ? THEN counter_amount END), 0) "
            "FROM activity WHERE account_id='DEMO-USD' AND trade_date <= ?", (cur, cur, last)).fetchone()[0]
        held = demo.execute(
            "SELECT COALESCE(SUM(market_value), 0) FROM holding_snapshot "
            "WHERE account_id='DEMO-USD' AND as_of_date=? AND symbol=?", (last, f"CASH.{cur}")).fetchone()[0]
        assert held == pytest.approx(ledger, abs=0.01)
    qty = dict(demo.execute("SELECT symbol, quantity FROM holding_snapshot WHERE account_id='DEMO-USD' "
                            "AND as_of_date=? AND asset_class='etf'", (last,)).fetchall())
    assert qty == {"VTI": 4, "VXUS": 15}       # bought 6 + 15, sold 2


def test_tfsa_activity_mirrors_cash_flow(demo):
    flows = demo.execute("SELECT flow_date, flow_type, amount FROM cash_flow "
                         "WHERE account_id='DEMO-TFSA' ORDER BY 1, 2, 3").fetchall()
    acts = demo.execute("SELECT trade_date, activity_type, amount FROM activity "
                        "WHERE account_id='DEMO-TFSA' ORDER BY 1, 2, 3").fetchall()
    assert len(flows) == 23
    assert [tuple(r) for r in acts] == [tuple(r) for r in flows]


def test_fx_conversions_carry_the_demo_spread(demo):
    rows = demo.execute(
        "SELECT a.amount, a.currency, a.counter_amount, a.reported_fx_rate, r.rate "
        "FROM activity a JOIN fx_rate r ON r.rate_date = a.trade_date "
        " AND r.base_currency = 'USD' AND r.quote_currency = 'CAD' "
        "WHERE a.activity_type = 'FX_CONVERSION' ORDER BY a.trade_date").fetchall()
    assert len(rows) == 2
    for amount, cur, counter, reported, mid in rows:
        assert reported == pytest.approx(abs(amount) / abs(counter), rel=1e-5)
        cad_per_usd = abs(amount) / counter if cur == "CAD" else counter / abs(amount)
        if cur == "CAD":
            assert cad_per_usd > mid            # buying USD: you pay more than mid
        else:
            assert cad_per_usd < mid            # selling USD: you receive less than mid
        assert abs(cad_per_usd / mid - 1) * 10_000 == pytest.approx(50, abs=1)   # 50 bps


@pytest.mark.parametrize("table,pairs", MONEY_COLUMNS.items())
def test_every_money_column_has_a_currency(demo, table, pairs):
    cols = {r[1] for r in demo.execute(f"PRAGMA table_info({table})")}
    for money, cur in pairs:
        assert money in cols and cur in cols
        missing = demo.execute(f"SELECT COUNT(*) FROM {table} "
                               f"WHERE {money} IS NOT NULL AND {cur} IS NULL").fetchone()[0]
        assert missing == 0


def test_constraints_reject_bad_rows(demo):
    batch = db.start_batch(demo, "synthetic", "constraint test")
    now = db.utc_now()
    good = {"activity_id": "t1", "account_id": "DEMO-USD", "activity_type": "FX_CONVERSION",
            "raw_type": "FX conversion", "trade_date": "2026-10-01", "amount": -100.0, "currency": "CAD",
            "counter_amount": 73.0, "counter_currency": "USD", "quality_flag": "demo",
            "source_id": "synthetic", "batch_id": batch, "loaded_at_utc": now}
    assert db.insert_ignore(demo, "activity", good) is True
    assert db.insert_ignore(demo, "activity", good) is False          # duplicate key: skipped, no error
    bad_activity = [
        {**good, "activity_id": "t2", "counter_amount": None, "counter_currency": None},  # FX needs a counter leg
        {**good, "activity_id": "t3", "counter_currency": "CAD"},                          # same currency both sides
        {**good, "activity_id": "t4", "fee_amount": 1.0},                                  # fee without its currency
        {**good, "activity_id": "t5", "activity_type": "SWAP"},                            # unknown type
    ]
    for row in bad_activity:
        with pytest.raises(sqlite3.IntegrityError):
            db.insert_ignore(demo, "activity", row)
    with pytest.raises(sqlite3.IntegrityError):                       # connections are read-only, always
        db.insert_ignore(demo, "connection", {
            "connection_id": "rw", "institution_id": "demo-maple-broker", "source_id": "snaptrade",
            "display_name": "x", "access_mode": "read_write", "created_at_utc": now, "is_demo": 1})
    with pytest.raises(sqlite3.IntegrityError):                       # fees must say where they came from
        db.insert_ignore(demo, "fee_schedule", {
            "fee_schedule_id": "nosrc", "institution_id": "demo-maple-broker", "fee_type": "commission",
            "rate_bps": 10.0, "date_checked": "2026-10-15", "is_demo": 1})


def test_everything_synthetic_is_labelled_demo(demo):
    for t in ("activity", "holding_snapshot", "fx_rate"):
        assert {r[0] for r in demo.execute(f"SELECT DISTINCT quality_flag FROM {t}")} == {"demo"}
    for t in ("institution", "connection", "fee_schedule"):
        assert {r[0] for r in demo.execute(f"SELECT DISTINCT is_demo FROM {t}")} == {1}
    assert all(r[0].startswith("https://example.com/")
               for r in demo.execute("SELECT source_url FROM fee_schedule"))
    assert demo.execute("SELECT COUNT(*) FROM connection WHERE provider_ref IS NOT NULL").fetchone()[0] == 0
    assert demo.execute("SELECT COUNT(*) FROM fx_rate WHERE source_id = 'boc_valet'").fetchone()[0] == 0