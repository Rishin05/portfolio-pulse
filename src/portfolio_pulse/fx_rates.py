"""FX rates in the database: sync from the Bank of Canada, then look up a rate for a date.

Only real observations are stored. Weekend/holiday handling happens at lookup time
(boc_valet.resolve_rate: carry forward up to 5 days, never use a later rate).
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from . import boc_valet, db

log = logging.getLogger(__name__)

SOURCE_ID = "boc_valet"
DEFAULT_BACKFILL_DAYS = 400   # first run with an empty table; override with start=


def store_observations(conn, observations, batch_id: str) -> tuple[int, int]:
    """Insert observations into fx_rate. Returns (inserted, skipped_duplicates)."""
    now = db.utc_now()
    inserted = skipped = 0
    for o in observations:
        new = db.insert_ignore(conn, "fx_rate", {
            "rate_date": o.rate_date.isoformat(),
            "base_currency": o.base,
            "quote_currency": o.quote,
            "rate": float(o.rate),
            "series_id": o.series,
            "quality_flag": "reported",
            "source_id": SOURCE_ID,
            "batch_id": batch_id,
            "loaded_at_utc": now,
        })
        inserted += new
        skipped += not new
    return inserted, skipped


def latest_rate_date(conn, base: str, quote: str, source_id: str = SOURCE_ID) -> date | None:
    row = conn.execute(
        "SELECT MAX(rate_date) FROM fx_rate "
        "WHERE base_currency = ? AND quote_currency = ? AND source_id = ?",
        (base, quote, source_id),
    ).fetchone()
    return date.fromisoformat(row[0]) if row[0] else None


def sync_boc_rates(conn, series=boc_valet.DEFAULT_SERIES, start: date | None = None,
                   end: date | None = None, dry_run: bool = False,
                   fetch=boc_valet.fetch_json) -> dict:
    """Fetch BoC rates not yet stored and save them. Never raises on network trouble.

    With no start: resume the day after the latest stored rate (cached), or backfill
    DEFAULT_BACKFILL_DAYS if nothing is stored yet.
    """
    end = end or date.today()
    if start is None:
        latest = [latest_rate_date(conn, *boc_valet.series_to_pair(s)) for s in series]
        if all(latest):
            start = min(latest) + timedelta(days=1)
        else:
            start = end - timedelta(days=DEFAULT_BACKFILL_DAYS)

    result = {"start": start.isoformat(), "end": end.isoformat(), "status": "ok",
              "fetched": 0, "inserted": 0, "skipped": 0, "warnings": []}
    if start > end:
        result["status"] = "up_to_date"
        log.info("BoC rates already up to date through %s.", end)
        return result

    url = boc_valet.build_url(series, start, end)
    try:
        payload = fetch(url)
    except (OSError, ValueError) as exc:   # URLError/timeout are OSError; bad JSON is ValueError
        result["status"] = "unavailable"
        log.warning("Bank of Canada rates unavailable (%s). Existing rates kept; "
                    "the next run will retry.", type(exc).__name__)
        return result

    observations, warnings = boc_valet.parse_observations(payload, series)
    result["fetched"] = len(observations)
    result["warnings"] = warnings
    for w in warnings:
        log.warning("BoC: %s", w)
    if dry_run:
        log.info("Dry run: %s BoC rates fetched for %s..%s, nothing written.",
                 len(observations), start, end)
        return result

    batch = db.start_batch(conn, SOURCE_ID, f"BoC Valet {','.join(series)} {start}..{end}")
    inserted, skipped = store_observations(conn, observations, batch)
    db.finish_batch(conn, batch, read=len(observations), inserted=inserted,
                    skipped=skipped, rejected=len(warnings))
    conn.commit()
    result["inserted"], result["skipped"] = inserted, skipped
    log.info("BoC rates %s..%s: %s fetched, %s new, %s duplicates.",
             start, end, len(observations), inserted, skipped)
    return result


def rate_for(conn, base: str, quote: str, target: date,
             source_id: str = SOURCE_ID) -> boc_valet.ResolvedRate:
    """Rate (quote per 1 base) to use on `target`, with its status.

    Same currency -> rate 1, status 'identity'. Otherwise the latest stored rate on or
    before target, judged by the 5-day carry-forward rule. Never a later rate.
    """
    if base == quote:
        return boc_valet.ResolvedRate(target, target, Decimal(1), "identity", 0)
    row = conn.execute(
        "SELECT rate_date, rate FROM fx_rate "
        "WHERE base_currency = ? AND quote_currency = ? AND source_id = ? AND rate_date <= ? "
        "ORDER BY rate_date DESC LIMIT 1",
        (base, quote, source_id, target.isoformat()),
    ).fetchone()
    rates = {} if row is None else {date.fromisoformat(row["rate_date"]): Decimal(str(row["rate"]))}
    return boc_valet.resolve_rate(rates, target)