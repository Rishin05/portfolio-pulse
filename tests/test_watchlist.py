"""Watchlist sync and manual edits (migration 007 + watchlist.py)."""
import pytest

from portfolio_pulse import db
from portfolio_pulse import watchlist as wl

D1, D2, D3 = "2026-10-01", "2026-10-02", "2026-10-05"


@pytest.fixture
def batch(conn):
    db.ensure_account(conn, "REAL-TFSA", "Real TFSA (test)", "TFSA", "CAD", False)
    db.ensure_account(conn, "DEMO-TFSA", "Demo TFSA (test)", "TFSA", "CAD", True)
    return db.start_batch(conn, "csv_import", "test holdings")


def hold(conn, batch, account, as_of, symbol, qty=10.0, asset_class="etf", ccy="CAD"):
    db.insert_ignore(conn, "holding_snapshot", {
        "account_id": account, "as_of_date": as_of, "symbol": symbol, "asset_class": asset_class,
        "quantity": qty, "price": None, "price_currency": None, "market_value": qty * 10,
        "market_value_currency": ccy, "book_value": None, "book_value_currency": None,
        "quality_flag": "demo" if account.startswith("DEMO") else "reported",
        "source_id": "csv_import", "batch_id": batch, "loaded_at_utc": db.utc_now()})


def row(conn, symbol):
    return conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()


def test_migration_defaults_unlabelled_rows_to_demo(conn):
    conn.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc) "
                 "VALUES ('RY', 'Royal Bank of Canada', '2026-01-01T00:00:00Z')")
    r = row(conn, "RY")
    assert (r["origin"], r["is_demo"], r["last_seen_in_holdings"]) == ("demo", 1, None)


def test_sync_adds_held_symbol_and_ignores_cash(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "VFV")
    hold(conn, batch, "REAL-TFSA", D1, "CASH.CAD", asset_class="cash")
    res = wl.sync_from_holdings(conn)
    assert res.added == ["VFV"] and res.held == 1
    r = row(conn, "VFV")
    assert (r["origin"], r["is_demo"], r["active"], r["company_name"], r["currency"],
            r["last_seen_in_holdings"]) == ("holding", 0, 1, "VFV", "CAD", D1)
    assert row(conn, "CASH.CAD") is None


def test_sync_uses_each_accounts_latest_snapshot_only(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "QQC")
    hold(conn, batch, "REAL-TFSA", D2, "VFV")      # QQC sold before D2
    assert wl.sync_from_holdings(conn).added == ["VFV"]


def test_zero_quantity_is_not_held(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "VFV", qty=0.0)
    hold(conn, batch, "REAL-TFSA", D1, "QQC")
    assert wl.sync_from_holdings(conn).added == ["QQC"]


def test_sold_holding_is_deactivated_kept_then_reactivated(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "VFV")
    wl.sync_from_holdings(conn)
    hold(conn, batch, "REAL-TFSA", D2, "QQC")      # VFV sold
    res = wl.sync_from_holdings(conn)
    assert res.deactivated == ["VFV"] and row(conn, "VFV")["active"] == 0
    hold(conn, batch, "REAL-TFSA", D3, "VFV")      # bought back
    res = wl.sync_from_holdings(conn)
    assert res.reactivated == ["VFV"] and row(conn, "VFV")["active"] == 1
    assert row(conn, "VFV")["last_seen_in_holdings"] == D3


def test_demo_and_manual_rows_are_never_toggled(conn, batch):
    conn.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc) "
                 "VALUES ('XEQT', 'iShares XEQT', '2026-01-01T00:00:00Z')")   # demo seed, not held
    wl.add_manual(conn, "SHOP")
    wl.remove(conn, "SHOP")                                                # you switched it off
    hold(conn, batch, "REAL-TFSA", D1, "SHOP")                              # ...but still hold it
    wl.sync_from_holdings(conn)
    assert row(conn, "XEQT")["active"] == 1
    assert (row(conn, "SHOP")["active"], row(conn, "SHOP")["origin"]) == (0, "manual")
    assert row(conn, "SHOP")["last_seen_in_holdings"] == D1


def test_demo_label_worst_wins_unless_a_real_account_holds_it(conn, batch):
    hold(conn, batch, "DEMO-TFSA", D1, "VTI", ccy="USD")
    hold(conn, batch, "DEMO-TFSA", D1, "VFV")
    hold(conn, batch, "REAL-TFSA", D1, "VFV")
    wl.sync_from_holdings(conn)
    assert row(conn, "VTI")["is_demo"] == 1
    assert row(conn, "VFV")["is_demo"] == 0


def test_rerun_is_idempotent(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "VFV")
    wl.sync_from_holdings(conn)
    before = [tuple(r) for r in conn.execute("SELECT * FROM watchlist ORDER BY symbol")]
    res = wl.sync_from_holdings(conn)
    assert (res.added, res.reactivated, res.deactivated, res.seen) == ([], [], [], ["VFV"])
    assert [tuple(r) for r in conn.execute("SELECT * FROM watchlist ORDER BY symbol")] == before


def test_dry_run_writes_nothing(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "VFV")
    res = wl.sync_from_holdings(conn, dry_run=True)
    assert res.added == ["VFV"]
    assert conn.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0


def test_no_holdings_deactivates_nothing(conn, batch):
    hold(conn, batch, "REAL-TFSA", D1, "VFV")
    wl.sync_from_holdings(conn)
    conn.execute("DELETE FROM holding_snapshot")    # e.g. a failed load left nothing
    res = wl.sync_from_holdings(conn)
    assert res.deactivated == [] and row(conn, "VFV")["active"] == 1


def test_add_manual_validates_and_updates_without_changing_demo_label(conn):
    with pytest.raises(wl.WatchlistError):
        wl.add_manual(conn, "bad sym!")
    with pytest.raises(wl.WatchlistError):
        wl.add_manual(conn, "VFV", currency="CA")
    assert wl.add_manual(conn, "vfv", name="Vanguard S&P 500 Index ETF") == "added"
    assert (row(conn, "VFV")["origin"], row(conn, "VFV")["is_demo"]) == ("manual", 0)
    conn.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc) "
                 "VALUES ('RY', 'Royal Bank of Canada', '2026-01-01T00:00:00Z')")
    assert wl.add_manual(conn, "RY", news_query="Royal Bank of Canada RBC") == "updated"
    r = row(conn, "RY")
    assert (r["origin"], r["is_demo"], r["company_name"], r["news_query"]) == \
           ("manual", 1, "Royal Bank of Canada", "Royal Bank of Canada RBC")


def test_remove_unknown_symbol_is_a_friendly_error(conn):
    with pytest.raises(wl.WatchlistError, match="not on the watchlist"):
        wl.remove(conn, "ZZZ")