"""Editable watchlist file (config/watchlist.csv): you curate it, `watchlist import` applies it.

Columns (only `symbol` is required; a blank cell means "leave as is" or "fill from the universe"):
  symbol, active (1/0 or yes/no; blank = 1), name, exchange, currency,
  finnhub (symbol to ask Finnhub for, or '-' = never use Finnhub), rss (Google News phrase or
  search syntax), note (why; stored with the news mapping)
Rules:
  * Every row becomes origin='manual', so your file wins over the holdings sync.
  * Symbols NOT in the file are left alone. Switch one off with active=0; don't delete the line.
  * Blank name/exchange/currency are filled from the symbol universe when the ticker has exactly
    one listing there (or one on the exchange you give).
  * Rows are planned and validated first, then only real differences are applied, so the same
    file again changes nothing. Bad rows go to data/rejects/ and are not applied.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import symbol_map, universe, watchlist
from .symbol_map import UNSET
from .watchlist import SYMBOL_RE, WatchlistError

log = logging.getLogger(__name__)

SOFT_LIMIT = 50                     # more active symbols than this makes daily news runs slow
COLUMNS = {"symbol", "active", "name", "exchange", "currency", "finnhub", "rss", "note"}
TRUE, FALSE = {"", "1", "yes", "y", "true"}, {"0", "no", "n", "false"}
SKIP_FINNHUB = {"-", "none"}


@dataclass
class FileResult:
    read: int = 0
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    deactivated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    rejects: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False


def _parse(text: str) -> tuple[list[tuple[int, dict]], set[str]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")))
    fields = {(f or "").strip().lower() for f in (reader.fieldnames or [])} - {""}
    if "symbol" not in fields:
        raise WatchlistError("The watchlist file needs a 'symbol' column (see examples/watchlist.example.csv).")
    rows = []
    for n, raw in enumerate(reader, start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in raw.items()
               if k is not None and not isinstance(v, list)}
        if any(row.values()):
            rows.append((n, row))
    return rows, fields - COLUMNS


def _plan(conn, row: dict) -> tuple[dict, str | None]:
    """Validate one row and work out the desired state. Raises WatchlistError for a bad row."""
    sym = watchlist.normalize_symbol(row.get("symbol", ""))
    flag = row.get("active", "").lower()
    if flag not in TRUE | FALSE:
        raise WatchlistError(f"active must be 1/0 or yes/no, got {row.get('active')!r}.")
    name = row.get("name") or None
    exch = (row.get("exchange") or "").upper() or None
    cur = (row.get("currency") or "").upper() or None
    if cur and len(cur) != 3:
        raise WatchlistError(f"currency must be a 3-letter code like CAD, got {cur!r}.")
    warn = None
    if not (name and exch and cur):
        try:
            info = universe.lookup(conn, sym, exch)
            name, exch, cur = name or info["name"], exch or info["exchange"], cur or info["currency"]
        except universe.UniverseError as exc:
            warn = f"{sym}: details not filled from the universe ({exc})"
    fh = row.get("finnhub", "")
    if fh == "":
        finnhub = UNSET
    elif fh.lower() in SKIP_FINNHUB:
        finnhub = None
    else:
        finnhub = fh.upper()
        if not SYMBOL_RE.match(finnhub):
            raise WatchlistError(f"finnhub must be a ticker like SHOP or '-', got {fh!r}.")
    return {"symbol": sym, "active": int(flag in TRUE), "name": name, "exchange": exch, "currency": cur,
            "finnhub": finnhub, "rss": row.get("rss") or UNSET, "note": row.get("note") or UNSET}, warn


def _map_changes(conn, p: dict) -> bool:
    if p["finnhub"] is UNSET and p["rss"] is UNSET and p["note"] is UNSET:
        return False
    m = conn.execute("SELECT finnhub_symbol, rss_query, note FROM symbol_map WHERE symbol = ?",
                     (p["symbol"],)).fetchone()
    if m is None:
        return True
    return ((p["finnhub"] is not UNSET and p["finnhub"] != m["finnhub_symbol"])
            or (p["rss"] is not UNSET and p["rss"] != m["rss_query"])
            or (p["note"] is not UNSET and p["note"] != m["note"]))


def _classify(conn, p: dict) -> str:
    w = conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (p["symbol"],)).fetchone()
    if w is None:
        return "added" if p["active"] else "deactivated"
    row_change = (w["origin"] != "manual" or w["active"] != p["active"]
                  or any(p[k] is not None and p[k] != w[c]
                         for k, c in (("name", "company_name"), ("exchange", "exchange"), ("currency", "currency"))))
    if not (row_change or _map_changes(conn, p)):
        return "unchanged"
    return "deactivated" if w["active"] and not p["active"] else "updated"


def _apply(conn, p: dict) -> None:
    map_change = _map_changes(conn, p)                     # check before the watchlist row changes
    watchlist.add_manual(conn, p["symbol"], p["name"], None, p["exchange"], p["currency"])
    if not p["active"]:
        watchlist.remove(conn, p["symbol"])
    if map_change:
        symbol_map.set_mapping(conn, p["symbol"], finnhub=p["finnhub"], rss_query=p["rss"], note=p["note"])


def _write_rejects(rejects: list[dict], rejects_dir: Path) -> Path:
    rejects_dir.mkdir(parents=True, exist_ok=True)
    path = rejects_dir / f"watchlist_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.csv"
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["line", "symbol", "reason"])
        w.writeheader()
        w.writerows(rejects)
    return path


def import_file(conn, path: str | Path, dry_run: bool = False, rejects_dir: Path | None = None) -> FileResult:
    rows, unknown = _parse(Path(path).read_text(encoding="utf-8-sig"))
    res = FileResult(read=len(rows), dry_run=dry_run)
    if unknown:
        res.warnings.append(f"Ignored unknown column(s): {sorted(unknown)}")
    plans, seen = [], set()
    for n, row in rows:
        try:
            p, warn = _plan(conn, row)
        except WatchlistError as exc:
            res.rejects.append({"line": n, "symbol": row.get("symbol", ""), "reason": str(exc)})
            continue
        if p["symbol"] in seen:
            res.rejects.append({"line": n, "symbol": p["symbol"], "reason": "duplicate symbol in file"})
            continue
        seen.add(p["symbol"])
        if warn:
            res.warnings.append(warn)
        plans.append(p)
    for p in plans:
        kind = _classify(conn, p)
        getattr(res, kind).append(p["symbol"])
        if kind != "unchanged" and not dry_run:
            _apply(conn, p)
    if res.rejects and rejects_dir is not None and not dry_run:
        _write_rejects(res.rejects, rejects_dir)
    active = conn.execute("SELECT COUNT(*) FROM watchlist WHERE active = 1").fetchone()[0]
    if active > SOFT_LIMIT:
        res.warnings.append(f"{active} active watchlist symbols (> {SOFT_LIMIT}): daily news runs will be slow "
                            "and use more of the free API limits.")
    for w in res.warnings:
        log.warning(w)
    log.info("Watchlist file%s: %s row(s); added %s, updated %s, deactivated %s, unchanged %s, rejected %s%s.",
             " (dry run, nothing written)" if dry_run else "", res.read, res.added or "none",
             res.updated or "none", res.deactivated or "none", len(res.unchanged), len(res.rejects),
             " (see data/rejects/)" if res.rejects and rejects_dir is not None and not dry_run else "")
    return res