"""Watchlist news ingestion: headline, link, publisher and timestamp only.

Source order (decided at run time):
  1. Finnhub company-news API, if FINNHUB_API_KEY is set in your .env
  2. Google News RSS, only if you pass --source rss (personal, non-commercial use)
  3. Otherwise nothing is fetched and the synthetic demo news stays in place.
A failed source never crashes the daily job; it logs a friendly message instead.
"""
from __future__ import annotations

import logging
import os
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from . import db

log = logging.getLogger(__name__)
TIMEOUT = 15
USER_AGENT = "portfolio-pulse/0.1 (personal educational project)"


class NewsSourceUnavailable(RuntimeError):
    pass


def normalize_url(url: str) -> str:
    """Lower-case scheme/host and drop tracking parameters so the same article dedupes."""
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query) if not k.lower().startswith("utm_")]
    return urllib.parse.urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path,
                                    urllib.parse.urlencode(query), ""))


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_finnhub(symbol: str, api_key: str, days: int = 7, session=requests) -> list[dict]:
    today = date.today()
    resp = session.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": symbol, "from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat()},
        headers={"X-Finnhub-Token": api_key, "User-Agent": USER_AGENT},   # key in header, not in logged URLs
        timeout=TIMEOUT,
    )
    if resp.status_code in (401, 403):
        raise NewsSourceUnavailable("Finnhub rejected the API key. Check FINNHUB_API_KEY in your .env file.")
    if resp.status_code == 429:
        raise NewsSourceUnavailable("Finnhub rate limit reached. Try again in a minute.")
    resp.raise_for_status()
    items = resp.json()
    if not isinstance(items, list):
        raise NewsSourceUnavailable(f"Unexpected Finnhub response for {symbol}.")
    out = []
    for it in items:
        if not it.get("url") or not it.get("headline"):
            continue
        ts = it.get("datetime")
        published = datetime.fromtimestamp(ts, tz=timezone.utc) if isinstance(ts, (int, float)) and ts > 0 else None
        out.append({"headline": it["headline"].strip(), "url": it["url"], "publisher": it.get("source"),
                    "published_at_utc": _iso(published)})
    return out


def parse_rss(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    out = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not title or not link:
            continue
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub) if pub else None
        except (TypeError, ValueError):
            published = None
        src = item.find("source")
        out.append({"headline": title, "url": link,
                    "publisher": src.text.strip() if src is not None and src.text else None,
                    "published_at_utc": _iso(published)})
    return out


def fetch_google_news_rss(query: str, session=requests) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": f'"{query}" when:7d', "hl": "en-CA", "gl": "CA", "ceid": "CA:en"})
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    resp.raise_for_status()
    return parse_rss(resp.text)


def store_articles(conn, symbol: str, articles: list[dict], source_id: str, batch_id: str) -> tuple[int, int]:
    now = db.utc_now()
    ins = skip = 0
    for a in articles:
        new = db.insert_ignore(conn, "news_article", {
            "article_id": db.stable_id(normalize_url(a["url"])), "symbol": symbol,
            "headline": a["headline"][:300], "url": a["url"], "publisher": a.get("publisher"),
            "published_at_utc": a.get("published_at_utc"), "fetched_at_utc": now,
            "source_id": source_id, "batch_id": batch_id})
        ins += new
        skip += not new
    return ins, skip


def fetch_watchlist_news(conn, source: str = "auto", dry_run: bool = False, session=requests) -> dict:
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if source == "auto":
        source = "finnhub" if api_key else "none"
    if source == "none":
        log.info("No FINNHUB_API_KEY found and RSS not requested - keeping existing/demo news. "
                 "Add a key to .env or run with --source rss.")
        return {"source": "none", "inserted": 0, "skipped": 0, "errors": 0}
    if source == "finnhub" and not api_key:
        raise NewsSourceUnavailable("--source finnhub needs FINNHUB_API_KEY in your .env file.")

    source_id = "finnhub" if source == "finnhub" else "google_news_rss"
    symbols = conn.execute("SELECT symbol, company_name, news_query FROM watchlist WHERE active = 1").fetchall()
    batch = None if dry_run else db.start_batch(conn, source_id, f"news via {source}")
    totals = {"source": source, "inserted": 0, "skipped": 0, "errors": 0, "fetched": 0}
    for row in symbols:
        try:
            if source == "finnhub":
                articles = fetch_finnhub(row["symbol"], api_key, session=session)
            else:
                articles = fetch_google_news_rss(row["news_query"] or row["company_name"], session=session)
        except NewsSourceUnavailable as exc:
            log.warning("%s: %s", row["symbol"], exc)
            totals["errors"] += 1
            continue
        except (requests.RequestException, ET.ParseError, ValueError) as exc:
            log.warning("%s: couldn't fetch news (%s). Skipping this symbol.", row["symbol"], type(exc).__name__)
            totals["errors"] += 1
            continue
        totals["fetched"] += len(articles)
        if not articles:
            log.info("%s: no recent articles found.", row["symbol"])
        if dry_run:
            continue
        ins, skip = store_articles(conn, row["symbol"], articles, source_id, batch)
        totals["inserted"] += ins
        totals["skipped"] += skip
    if batch:
        db.finish_batch(conn, batch, read=totals["fetched"], inserted=totals["inserted"],
                        skipped=totals["skipped"], rejected=0)
        conn.commit()
    log.info("News (%s): %s fetched, %s new, %s duplicates, %s symbol errors.", source,
             totals["fetched"], totals["inserted"], totals["skipped"], totals["errors"])
    return totals
