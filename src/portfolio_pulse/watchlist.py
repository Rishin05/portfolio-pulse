"""Watchlist maintenance: sync symbols from the latest holdings, plus manual additions.

Rules (see sql/migrations/007_watchlist_origin.sql for the origin values):
  * Source = each account's LATEST as_of_date in holding_snapshot, so sold positions drop out.
    Cash (asset_class 'cash' or CASH.*) and zero quantities are ignored.
  * A newly held symbol is added with origin='holding'. company_name is the symbol as a
    placeholder because holdings carry no names; `watchlist add --name` or the mapping fixes it.
  * A 'holding' row that is no longer held is set inactive, never deleted.
  * 'demo' and 'manual' rows are never switched on or off by a sync.
  * `add` and `remove` set origin='manual', so your choice wins over future syncs.
  * is_demo = 0 only if a non-demo account holds the symbol (worst label wins otherwise).
  * If there are no holdings at all, nothing is deactivated (a failed load must not wipe the list).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from . import db

log = logging.getLogger(__name__)

SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")   # e.g. SHOP, RY.TO, BRK.B, ABC-U

LATEST_HOLDINGS_SQL = """
SELECT h.symbol,
       MIN(CASE WHEN a.is_demo = 1 OR h.quality_flag = 'demo' THEN 1 ELSE 0 END) AS is_demo,
       MAX(h.as_of_date)            AS as_of_date,
       MAX(h.market_value_currency) AS currency
FROM holding_snapshot h
JOIN account a ON a.account_id = h.account_id
JOIN (SELECT account_id, MAX(as_of_date) AS latest
      FROM holding_snapshot GROUP BY account_id) l
  ON l.account_id = h.account_id AND l.latest = h.as_of_date
WHERE h.asset_class <> 'cash'
  AND h.symbol NOT LIKE 'CASH.%'
  AND h.quantity <> 0
GROUP BY h.symbol
"""


class WatchlistError(ValueError):
    """Bad input for a watchlist command (friendly message, never a stack trace)."""


@dataclass
class SyncResult:
    held: int = 0
    added: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    seen: list[str] = field(default_factory=list)   # already listed; last_seen_in_holdings refreshed


def normalize_symbol(symbol: str) -> str:
    sym = (symbol or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise WatchlistError(f"{symbol!r} doesn't look like a ticker (letters, digits, '.' or '-').")
    return sym


def sync_from_holdings(conn, dry_run: bool = False) -> SyncResult:
    res = SyncResult()
    held = {r["symbol"]: r for r in conn.execute(LATEST_HOLDINGS_SQL)}
    res.held = len(held)
    existing = {r["symbol"]: r for r in conn.execute("SELECT symbol, origin, active FROM watchlist")}
    now = db.utc_now()

    for sym, h in sorted(held.items()):
        row = existing.get(sym)
        if row is None:
            res.added.append(sym)
            if not dry_run:
                db.insert_ignore(conn, "watchlist", {
                    "symbol": sym, "company_name": sym, "exchange": None, "currency": h["currency"],
                    "news_query": None, "active": 1, "added_at_utc": now, "origin": "holding",
                    "is_demo": h["is_demo"], "last_seen_in_holdings": h["as_of_date"]})
            continue
        if row["origin"] == "holding" and not row["active"]:
            res.reactivated.append(sym)
        else:
            res.seen.append(sym)
        if not dry_run:
            conn.execute(
                "UPDATE watchlist SET last_seen_in_holdings = ?, "
                " active  = CASE WHEN origin = 'holding' THEN 1 ELSE active  END, "
                " is_demo = CASE WHEN origin = 'holding' THEN ? ELSE is_demo END "
                "WHERE symbol = ?", (h["as_of_date"], h["is_demo"], sym))

    if not held:
        log.warning("Watchlist sync: no holdings found, so nothing was deactivated.")
    else:
        for sym, row in sorted(existing.items()):
            if row["origin"] == "holding" and row["active"] and sym not in held:
                res.deactivated.append(sym)
                if not dry_run:
                    conn.execute("UPDATE watchlist SET active = 0 WHERE symbol = ?", (sym,))

    if not dry_run:
        conn.commit()
    log.info("Watchlist sync%s: %s held symbol(s); added %s, reactivated %s, deactivated %s, "
             "already listed %s.", " (dry run, nothing written)" if dry_run else "", res.held,
             res.added or "none", res.reactivated or "none", res.deactivated or "none", len(res.seen))
    return res


def add_manual(conn, symbol: str, name: str | None = None, news_query: str | None = None,
               exchange: str | None = None, currency: str | None = None) -> str:
    """Add or update a symbol you chose yourself. Returns 'added' or 'updated'.

    Updating an existing row keeps its is_demo label: a demo row stays demo (worst label wins).
    """
    sym = normalize_symbol(symbol)
    if currency is not None:
        currency = currency.strip().upper()
        if len(currency) != 3:
            raise WatchlistError(f"Currency must be a 3-letter code like CAD, got {currency!r}.")
    found = conn.execute("SELECT 1 FROM watchlist WHERE symbol = ?", (sym,)).fetchone()
    if found is None:
        db.insert_ignore(conn, "watchlist", {
            "symbol": sym, "company_name": (name or sym).strip(), "exchange": exchange,
            "currency": currency, "news_query": news_query, "active": 1,
            "added_at_utc": db.utc_now(), "origin": "manual", "is_demo": 0,
            "last_seen_in_holdings": None})
        action = "added"
    else:
        conn.execute(
            "UPDATE watchlist SET origin = 'manual', active = 1, "
            " company_name = COALESCE(?, company_name), news_query = COALESCE(?, news_query), "
            " exchange = COALESCE(?, exchange), currency = COALESCE(?, currency) "
            "WHERE symbol = ?", (name, news_query, exchange, currency, sym))
        action = "updated"
    conn.commit()
    log.info("Watchlist: %s %s (manual).", action, sym)
    return action


def remove(conn, symbol: str) -> None:
    """Switch a symbol off and pin it as manual, so a sync won't switch it back on."""
    sym = normalize_symbol(symbol)
    cur = conn.execute("UPDATE watchlist SET active = 0, origin = 'manual' WHERE symbol = ?", (sym,))
    if cur.rowcount == 0:
        raise WatchlistError(f"{sym} is not on the watchlist.")
    conn.commit()
    log.info("Watchlist: removed %s (kept with its news history, marked manual).", sym)


def list_rows(conn) -> list:
    return conn.execute(
        "SELECT symbol, company_name, origin, is_demo, active, last_seen_in_holdings, news_query "
        "FROM watchlist ORDER BY active DESC, symbol").fetchall()