"""Resolve a watchlist symbol to what each news source should be asked for.

Finnhub (verified live 2026-10-22, free plan): TSX listings use the .TO suffix (search shows
VFV.TO, QQC.TO, SHOP.TO), but company-news returns HTTP 403 for .TO symbols. Cross-listed
companies work through their US symbol (SHOP on Nasdaq). So:
  finnhub: map row value (NULL = skip on purpose) -> USD watchlist currency: same symbol -> skip.
  rss:     map row rss_query -> watchlist.news_query -> company_name.
A bare ticker is never assumed to be a US listing (T is Telus on the TSX but AT&T on the NYSE).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from . import db
from .watchlist import SYMBOL_RE, WatchlistError, normalize_symbol

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
UNSET = object()   # "leave this field as it is" in set_mapping

_SQL = """
SELECT w.symbol, w.company_name, w.currency, w.news_query, w.active,
       m.symbol AS mapped, m.finnhub_symbol, m.rss_query
FROM watchlist w LEFT JOIN symbol_map m ON m.symbol = w.symbol
"""


@dataclass(frozen=True)
class Resolved:
    symbol: str
    finnhub_symbol: str | None
    finnhub_basis: str   # mapped | mapped_skip | default_usd | default_skip
    rss_query: str
    rss_basis: str       # mapped | watchlist | name


def _resolve_row(r) -> Resolved:
    if r["mapped"] is not None:
        fh = r["finnhub_symbol"]
        fh_basis = "mapped" if fh else "mapped_skip"
    elif (r["currency"] or "").upper() == "USD":
        fh, fh_basis = r["symbol"], "default_usd"
    else:
        fh, fh_basis = None, "default_skip"
    if r["rss_query"]:
        rss, rss_basis = r["rss_query"], "mapped"
    elif r["news_query"]:
        rss, rss_basis = r["news_query"], "watchlist"
    else:
        rss, rss_basis = r["company_name"], "name"
    return Resolved(r["symbol"], fh, fh_basis, rss, rss_basis)


def resolve(conn, symbol: str) -> Resolved:
    sym = normalize_symbol(symbol)
    row = conn.execute(_SQL + " WHERE w.symbol = ?", (sym,)).fetchone()
    if row is None:
        raise WatchlistError(f"{sym} is not on the watchlist. Add it first with `watchlist add {sym}`.")
    return _resolve_row(row)


def resolve_active(conn) -> list[Resolved]:
    return [_resolve_row(r) for r in conn.execute(_SQL + " WHERE w.active = 1 ORDER BY w.symbol")]


def set_mapping(conn, symbol: str, finnhub=UNSET, rss_query=UNSET, note=UNSET,
                checked_on: str | None = None) -> Resolved:
    """Create or update a mapping. finnhub=None means 'don't use Finnhub'; UNSET keeps the old value."""
    sym = normalize_symbol(symbol)
    if conn.execute("SELECT 1 FROM watchlist WHERE symbol = ?", (sym,)).fetchone() is None:
        raise WatchlistError(f"{sym} is not on the watchlist. Add it first with `watchlist add {sym}`.")
    if finnhub is not UNSET and finnhub is not None:
        finnhub = finnhub.strip().upper()
        if not SYMBOL_RE.match(finnhub):
            raise WatchlistError(f"{finnhub!r} doesn't look like a Finnhub symbol (e.g. SHOP, RY).")
    if rss_query is not UNSET and rss_query is not None:
        rss_query = rss_query.strip()
        if not rss_query:
            raise WatchlistError("The RSS phrase can't be blank (omit it to keep the fallback).")
    checked_on = checked_on or date.today().isoformat()
    if not DATE_RE.match(checked_on):
        raise WatchlistError(f"checked_on must look like 2026-10-22, got {checked_on!r}.")

    old = conn.execute("SELECT finnhub_symbol, rss_query, note FROM symbol_map WHERE symbol = ?",
                       (sym,)).fetchone()
    if old is None and finnhub is UNSET:
        current = resolve(conn, sym)            # first row: start from today's effective value
        finnhub = current.finnhub_symbol
    values = {
        "finnhub_symbol": old["finnhub_symbol"] if finnhub is UNSET else finnhub,
        "rss_query": (old["rss_query"] if old else None) if rss_query is UNSET else rss_query,
        "note": (old["note"] if old else None) if note is UNSET else note,
    }
    conn.execute(
        "INSERT INTO symbol_map (symbol, finnhub_symbol, rss_query, note, checked_on, updated_at_utc) "
        "VALUES (?,?,?,?,?,?) ON CONFLICT(symbol) DO UPDATE SET finnhub_symbol = excluded.finnhub_symbol, "
        "rss_query = excluded.rss_query, note = excluded.note, checked_on = excluded.checked_on, "
        "updated_at_utc = excluded.updated_at_utc",
        (sym, values["finnhub_symbol"], values["rss_query"], values["note"], checked_on, db.utc_now()))
    conn.commit()
    return resolve(conn, sym)


def clear_mapping(conn, symbol: str) -> bool:
    """Remove a mapping so the default rule applies again. Returns True if a row was removed."""
    cur = conn.execute("DELETE FROM symbol_map WHERE symbol = ?", (normalize_symbol(symbol),))
    conn.commit()
    return cur.rowcount == 1