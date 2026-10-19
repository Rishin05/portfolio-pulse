"""Loader tests: fake raw SnapTrade folders -> temporary database (no network)."""
import json

import pytest

from portfolio_pulse import db, snaptrade_load

ACCT = "acct-uuid-0001"
USD = {"code": "USD"}


def _put(folder, name, body):
    (folder / name).write_text(json.dumps(body), encoding="utf-8")


@pytest.fixture
def raw_folder(tmp_path):
    folder = tmp_path / "raw" / "20261020T210000Z"   # Tue 17:00 Toronto
    folder.mkdir(parents=True)
    _put(folder, "connections.json", [{
        "id": "conn-1", "disabled": False, "created_date": "2026-10-18T03:00:00Z",
        "brokerage": {"slug": "ALPACA-PAPER", "display_name": "Alpaca Paper",
                      "url": "https://example.com"},
    }])
    _put(folder, "accounts.json", {"results": [{
        "id": ACCT, "connection_id": "conn-1", "display_name": "Paper account",
        "is_paper": True, "net_value": {"amount": 1000.0, "currency": "USD"},
    }]})
    short = ACCT[:8]
    _put(folder, f"balances_{short}.json", [{"cash": 250.0, "currency": USD}])
    _put(folder, f"activities_{short}.json", [
        {"id": "a1", "type": "CONTRIBUTION", "amount": 1000.0, "currency": USD,
         "trade_date": "2026-10-19T00:00:00Z"},
        {"id": "a2", "type": "BUY", "amount": -750.0, "units": 3, "price": 250.0,
         "currency": USD, "symbol": {"symbol": "VTI"}, "trade_date": "2026-10-19T14:30:00Z"},
        {"id": "a3", "type": "REI", "amount": 0.0, "units": 0.01, "currency": USD,
         "trade_date": "2026-10-19T00:00:00Z"},
        {"id": "a4", "type": "WEIRD_THING", "amount": -1.0, "currency": USD,
         "trade_date": "2026-10-19T00:00:00Z"},
        {"id": "a5", "type": "DIVIDEND", "amount": None, "currency": USD,
         "trade_date": "2026-10-19T00:00:00Z"},
    ])
    return folder


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.init_db(c)
    yield c
    c.close()


def test_load_counts_and_rerun_adds_nothing(conn, raw_folder):
    first = snaptrade_load.load_folder(conn, raw_folder)
    assert first.as_of_date == "2026-10-20"
    # 5 account-level rows + 4 activities written, 1 activity rejected (missing amount)
    assert (first.read, first.inserted, first.skipped, first.rejected) == (10, 9, 0, 1)
    second = snaptrade_load.load_folder(conn, raw_folder)
    assert (second.read, second.inserted, second.skipped, second.rejected) == (10, 0, 9, 1)


def test_unknown_and_ambiguous_types_are_stored_as_other(conn, raw_folder):
    res = snaptrade_load.load_folder(conn, raw_folder)
    types = {r[0]: r[1] for r in conn.execute("SELECT raw_type, activity_type FROM activity")}
    assert types == {"CONTRIBUTION": "DEPOSIT", "BUY": "BUY",
                     "REI": "OTHER", "WEIRD_THING": "OTHER"}
    text = " ".join(res.warnings)
    assert "'REI'" in text and "'WEIRD_THING'" in text


def test_dry_run_writes_nothing(conn, raw_folder):
    res = snaptrade_load.load_folder(conn, raw_folder, dry_run=True)
    assert res.inserted == 9
    assert conn.execute("SELECT COUNT(*) FROM activity").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0