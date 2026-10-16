"""Reproducible SYNTHETIC portfolio, cash-flow and news data.

Everything produced here is labelled demo data (source_id='synthetic',
quality_flag='demo'). It is shaped to look plausible, not to be accurate.
"""
from __future__ import annotations

import logging
import random
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone

from . import db, synthetic_brokers

log = logging.getLogger(__name__)

DEMO_ACCOUNT = "DEMO-TFSA"


@dataclass
class DemoDay:
    as_of_date: date
    start_value: float | None
    external_flow: float
    true_return: float      # the return the generator applied (used by tests)
    end_value: float


def business_days(start: date, end: date) -> list[date]:
    """Mon-Fri only. Market holidays are NOT removed - a known simplification."""
    days, d = [], start
    while d <= end:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    return days


def generate_days(seed: int = 42, start: date = date(2026, 1, 2), end: date = date(2026, 9, 30),
                  opening_value: float = 10_000.0) -> tuple[list[DemoDay], list[dict]]:
    """Return (daily values, cash-flow events). Same seed -> identical output."""
    rng = random.Random(seed)
    days = business_days(start, end)
    flows: list[dict] = []

    # Scheduled demo cash flows: a deposit every 10th business day, plus a few one-offs.
    planned: dict[date, list[tuple[str, float]]] = {}
    for i, d in enumerate(days):
        if i > 0 and i % 10 == 0:
            planned.setdefault(d, []).append(("DEPOSIT", 250.0))
    planned.setdefault(days[60], []).append(("WITHDRAWAL", -1_500.0))
    planned.setdefault(days[95], []).append(("TRANSFER_IN", 3_000.0))
    planned.setdefault(days[131], []).append(("DIVIDEND", 42.17))   # internal: part of return
    planned.setdefault(days[153], []).append(("FEE", -9.99))        # internal: part of return

    out: list[DemoDay] = []
    value = opening_value
    for i, d in enumerate(days):
        if i == 0:
            out.append(DemoDay(d, None, 0.0, 0.0, round(value, 2)))
            continue
        start_value = out[-1].end_value
        ext = sum(a for t, a in planned.get(d, []) if t in {"DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT"})
        internal = sum(a for t, a in planned.get(d, []) if t in {"DIVIDEND", "FEE", "INTEREST"})
        # Daily market return: mostly calm, occasional shock days so the calendar has stories.
        r = rng.gauss(0.0004, 0.009)
        if rng.random() < 0.03:
            r += rng.choice([-1, 1]) * rng.uniform(0.02, 0.04)
        # Flows assumed at start of day (matches v_daily_performance).
        end_value = (start_value + ext) * (1 + r) + internal
        end_value = round(end_value, 2)
        true_r = (end_value - start_value - ext) / (start_value + ext)
        out.append(DemoDay(d, start_value, ext, true_r, end_value))
        for t, a in planned.get(d, []):
            flows.append({"flow_date": d.isoformat(), "flow_type": t, "amount": a})
    return out, flows


DEMO_WATCHLIST = [
    ("SHOP", "Shopify Inc.", "TSX/NASDAQ", "CAD", "Shopify"),
    ("RY", "Royal Bank of Canada", "TSX/NYSE", "CAD", "Royal Bank of Canada"),
    ("XEQT", "iShares Core Equity ETF Portfolio", "TSX", "CAD", "iShares XEQT"),
]

_HEADLINE_TEMPLATES = [
    "[DEMO] {name} publishes quarterly results",
    "[DEMO] Analysts discuss outlook for {name}",
    "[DEMO] {name} announces product update",
    "[DEMO] {name} mentioned in sector roundup",
]


def generate_news(seed: int, days: list[DemoDay]) -> list[dict]:
    rng = random.Random(seed + 1)
    rows = []
    for symbol, name, *_ in DEMO_WATCHLIST:
        for k in range(8):
            d = rng.choice(days).as_of_date
            ts = datetime.combine(d, time(rng.randint(11, 21), rng.choice([0, 15, 30, 45])), tzinfo=timezone.utc)
            rows.append({
                "symbol": symbol,
                "headline": rng.choice(_HEADLINE_TEMPLATES).format(name=name),
                # example.com is reserved for documentation, so these links can never
                # be mistaken for a real article.
                "url": f"https://example.com/demo-news/{symbol.lower()}/{d.isoformat()}-{k}",
                "publisher": "Demo Wire (synthetic)",
                "published_at_utc": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            })
    return rows


def load_demo(conn, seed: int = 42) -> dict:
    """Write synthetic data. Safe to re-run: existing rows are skipped, not duplicated."""
    days, flows = generate_days(seed)
    batch = db.start_batch(conn, "synthetic", f"demo seed={seed}")
    db.ensure_account(conn, DEMO_ACCOUNT, "Demo TFSA (synthetic)", "TFSA", "CAD", True)
    now = db.utc_now()
    ins = skip = 0
    for d in days:
        new = db.insert_ignore(conn, "daily_snapshot", {
            "account_id": DEMO_ACCOUNT, "as_of_date": d.as_of_date.isoformat(), "end_value": d.end_value,
            "currency": "CAD", "quality_flag": "demo", "source_id": "synthetic",
            "batch_id": batch, "loaded_at_utc": now})
        ins, skip = ins + new, skip + (not new)
    for f in flows:
        external = f["flow_type"] in {"DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT"}
        new = db.insert_ignore(conn, "cash_flow", {
            "cash_flow_id": db.stable_id("synthetic", DEMO_ACCOUNT, f["flow_date"], f["flow_type"], f["amount"]),
            "account_id": DEMO_ACCOUNT, "flow_date": f["flow_date"], "flow_type": f["flow_type"],
            "amount": f["amount"], "currency": "CAD", "is_external": int(external),
            "description": f"Synthetic {f['flow_type'].lower()}", "source_id": "synthetic",
            "batch_id": batch, "loaded_at_utc": now})
        ins, skip = ins + new, skip + (not new)
    for sym, name, exch, cur, query in DEMO_WATCHLIST:
        conn.execute("INSERT OR IGNORE INTO watchlist (symbol, company_name, exchange, currency, news_query, "
                     "added_at_utc) VALUES (?,?,?,?,?,?)", (sym, name, exch, cur, query, now))
    for n in generate_news(seed, days):
        new = db.insert_ignore(conn, "news_article", {
            "article_id": db.stable_id(n["url"].lower()), **n, "fetched_at_utc": now,
            "source_id": "synthetic", "batch_id": batch})
        ins, skip = ins + new, skip + (not new)
    mb_read, mb_ins, mb_skip = synthetic_brokers.load_multi_broker(
        conn, seed, days, flows, batch, now, DEMO_ACCOUNT)
    ins, skip = ins + mb_ins, skip + mb_skip
    total = len(days) + len(flows) + 3 * 8 + mb_read
    db.finish_batch(conn, batch, read=total, inserted=ins, skipped=skip, rejected=0)
    conn.commit()
    log.info("Demo data: %s new rows, %s already present (seed=%s).", ins, skip, seed)
    return {"inserted": ins, "skipped": skip}
