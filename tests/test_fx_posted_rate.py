"""fx_posted_rate (migration 006): the database itself rejects bad bank quotes."""
import sqlite3

import pytest

from portfolio_pulse import db

DROP = object()   # sentinel: remove this key from the row entirely

INSTITUTION = {
    "institution_id": "test-bank", "display_name": "Test Bank (test)", "country": "CA",
    "website_url": "https://example.com/test-bank", "is_demo": 1,
}


def row(**overrides):
    r = {
        "fx_posted_rate_id": "test-bank-USDCAD-2026-10-19-standard",
        "institution_id": "test-bank", "base_currency": "USD", "quote_currency": "CAD",
        "rate_date": "2026-10-19", "channel": "standard",
        "customer_buy_rate": 1.4400, "customer_sell_rate": 1.3900,
        "source_url": "https://example.com/test-bank/fx", "date_checked": "2026-10-19",
        "notes": "TEST quote", "is_demo": 1,
    }
    for key, value in overrides.items():
        if value is DROP:
            r.pop(key)
        else:
            r[key] = value
    return r


@pytest.fixture
def bank(conn):
    db.insert_ignore(conn, "institution", INSTITUTION)
    return conn


def n_rows(c):
    return c.execute("SELECT COUNT(*) FROM fx_posted_rate").fetchone()[0]


def test_valid_quote_inserts_and_reimport_adds_zero(bank):
    db.insert_ignore(bank, "fx_posted_rate", row())
    db.insert_ignore(bank, "fx_posted_rate", row())                          # same primary key
    db.insert_ignore(bank, "fx_posted_rate", row(fx_posted_rate_id="dup"))   # same natural key
    assert n_rows(bank) == 1


def test_one_sided_quote_is_allowed(bank):
    db.insert_ignore(bank, "fx_posted_rate",
                     row(fx_posted_rate_id="buy-only", rate_date="2026-10-16", customer_sell_rate=None))
    assert n_rows(bank) == 1


@pytest.mark.parametrize("overrides", [
    {"source_url": DROP},                                       # must say where it came from
    {"source_url": "   "},
    {"date_checked": DROP},                                     # must say when it was checked
    {"customer_buy_rate": None, "customer_sell_rate": None},    # a quote needs at least one rate
    {"customer_buy_rate": 1.38, "customer_sell_rate": 1.39},    # a bank never pays more than it charges
    {"customer_buy_rate": 0},
    {"customer_sell_rate": -1.39},
    {"quote_currency": "USD"},                                  # USD/USD is not a pair
    {"base_currency": "usd"},                                   # ISO codes are upper case
    {"rate_date": "2026/10/19"},
    {"date_checked": "2026-10-18"},                             # checked before the rate existed
    {"channel": "  "},
    {"is_demo": 2},
], ids=lambda o: ",".join(o))
def test_bad_quotes_are_rejected(bank, overrides):
    with pytest.raises(sqlite3.IntegrityError):
        db.insert_ignore(bank, "fx_posted_rate", row(**overrides))
    assert n_rows(bank) == 0