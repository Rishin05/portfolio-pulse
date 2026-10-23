"""Watchlist news ingestion: headline, link, publisher and timestamp only.

Source order, per symbol (Phase 5):
  1. Finnhub company-news, if FINNHUB_API_KEY is set and symbol_map resolves a Finnhub symbol
     (TSX '.TO' symbols are HTTP 403 on the free plan; cross-listed companies map to the US symbol).
  2. Google News RSS (personal, non-commercial), only when you opt in: --source rss, or
     NEWS_RSS_FALLBACK=1 in .env for `auto` / run-daily. Uses the resolved RSS phrase.
  3. Otherwise nothing is fetched for that symbol; existing/demo news stays in place.
A source that fails or returns nothing hands over to the next. A 401 (bad key) switches
Finnhub off for the rest of the run. A failure is logged per symbol and never stops the job.
Articles are stored under the watchlist symbol, whatever symbol the source was asked for.
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from . import db, news_time, symbol_map

log = logging.getLogger(__name__)
TIMEOUT = 15
USER_AGENT = "portfolio-pulse/0.1 (personal educational project)"
FINNHUB, RSS = "finnhub", "google_news_rss"
RSS_WINDOW_DEFAULT, RSS_MAX_ITEMS_DEFAULT = 7, 25
# A phrase that already uses search syntax is sent as written; a plain phrase is quoted.
_RSS_SYNTAX = re.compile(r'["()]|\bOR\b|\bwhen:|\bsite:|(^|\s)-\S')
PACE_SECONDS = {FINNHUB: 1.1, RSS: 1.0}   # Finnhub free plan: 60 calls/minute
_sleep = time.sleep                        # module-level so tests can replace it


class Pacer:
    """Keeps at least `interval` seconds between calls to one source. Spacing only, never retries."""

    def __init__(self, interval: float, clock=time.monotonic, sleep=None):
        self.interval, self.clock, self.sleep, self._last = interval, clock, sleep, None

    def wait(self) -> None:
        now = self.clock()
        if self._last is not None:
            remaining = self.interval - (now - self._last)
            if remaining > 0:
                (self.sleep or _sleep)(remaining)
                now += remaining
        self._last = now

def build_rss_query(phrase: str, window_days: int = RSS_WINDOW_DEFAULT) -> str:
    """'VFV ETF' -> '"VFV ETF" when:7d'; '"VFV" OR "Vanguard S&P 500"' is kept as written."""
    phrase = phrase.strip()
    q = phrase if _RSS_SYNTAX.search(phrase) else f'"{phrase}"'
    return q if "when:" in q else f"{q} when:{window_days}d"


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not lo <= value <= hi:
        log.warning("%s=%r ignored (expected a whole number %s-%s); using %s.", name, raw, lo, hi, default)
        return default
    return value


class NewsSourceUnavailable(RuntimeError):
    pass


class FinnhubKeyRejected(NewsSourceUnavailable):
    """HTTP 401: the key itself is wrong, so every further Finnhub call would fail too."""


class FinnhubNotOnPlan(NewsSourceUnavailable):
    """HTTP 403: this symbol isn't available on your plan (e.g. TSX '.TO' on the free plan)."""

class SourceRateLimited(NewsSourceUnavailable):
    """HTTP 429 (or 503 from Google News): stop calling this source for the rest of the run."""

    def __init__(self, source: str, retry_after: int | None = None):
        self.source, self.retry_after = source, retry_after
        wait = f"retry after {retry_after}s" if retry_after is not None else "try again later"
        super().__init__(f"{source} rate limit reached ({wait}).")


def _retry_after(resp) -> int | None:
    """Retry-After as seconds (number or HTTP date); None if absent or unreadable."""
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    if not raw:
        return None
    raw = str(raw).strip()
    if raw.isdigit():
        return int(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))

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


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def fetch_finnhub(symbol: str, api_key: str, days: int = 7, session=requests) -> list[dict]:
    today = date.today()
    resp = session.get(
        "https://finnhub.io/api/v1/company-news",
        params={"symbol": symbol, "from": (today - timedelta(days=days)).isoformat(), "to": today.isoformat()},
        headers={"X-Finnhub-Token": api_key, "User-Agent": USER_AGENT},   # key in header, not in logged URLs
        timeout=TIMEOUT,
    )
    if resp.status_code == 401:
        raise FinnhubKeyRejected("Finnhub rejected the API key (HTTP 401). Check FINNHUB_API_KEY in your .env file.")
    if resp.status_code == 403:
        raise FinnhubNotOnPlan(f"Finnhub returned HTTP 403 for {symbol}: not available on your plan "
                               "(TSX '.TO' symbols are 403 on the free plan). Map a US listing or skip "
                               "Finnhub for it with `watchlist map`.")
    if resp.status_code == 429:
        raise SourceRateLimited(FINNHUB, _retry_after(resp))
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


def fetch_google_news_rss(query: str, session=requests, window_days: int = RSS_WINDOW_DEFAULT,
                          max_items: int | None = RSS_MAX_ITEMS_DEFAULT) -> list[dict]:
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": build_rss_query(query, window_days), "hl": "en-CA", "gl": "CA", "ceid": "CA:en"})
    resp = session.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
    if resp.status_code in (429, 503):
        raise SourceRateLimited(RSS, _retry_after(resp))
    resp.raise_for_status()
    items = parse_rss(resp.text)
    if max_items:   # keep the newest; undated items sort last
        items = sorted(items, key=lambda a: a["published_at_utc"] or "", reverse=True)[:max_items]
    return items


_DUPLICATE_SQL = """
SELECT n.article_id FROM news_article n
JOIN news_article_symbol s ON s.article_id = n.article_id
WHERE s.symbol = ? AND n.story_key = ? AND n.duplicate_of IS NULL AND n.article_id <> ?
  AND n.published_local_date BETWEEN ? AND ?
ORDER BY n.published_at_utc LIMIT 1
"""


def _find_duplicate(conn, symbol: str, key: str | None, local: str | None, article_id: str) -> str | None:
    """Same story for the same symbol within +/-1 Toronto day, from a different link."""
    if not key or not local:
        return None
    d = date.fromisoformat(local)
    row = conn.execute(_DUPLICATE_SQL, (symbol, key, article_id, (d - timedelta(days=1)).isoformat(),
                                        (d + timedelta(days=1)).isoformat())).fetchone()
    return row["article_id"] if row else None


def store_articles(conn, symbol: str, articles: list[dict], source_id: str, batch_id: str,
                   stats: dict | None = None) -> tuple[int, int]:
    """Insert new articles and link each to `symbol`. Returns (new articles, exact repeats).

    stats (optional) accumulates 'duplicates' (new rows flagged duplicate_of) and 'linked'
    (an existing article newly linked to this symbol).
    """
    now = db.utc_now()
    ins = skip = 0
    for a in articles:
        aid = db.stable_id(normalize_url(a["url"]))
        exists = conn.execute("SELECT 1 FROM news_article WHERE article_id = ?", (aid,)).fetchone()
        if exists is None:
            published = a.get("published_at_utc")
            key = news_time.story_key(a["headline"], a.get("publisher"))
            local = news_time.local_date(published)
            dup = _find_duplicate(conn, symbol, key, local, aid)
            db.insert_ignore(conn, "news_article", {
                "article_id": aid, "symbol": symbol, "headline": a["headline"][:300], "url": a["url"],
                "publisher": a.get("publisher"), "published_at_utc": published, "fetched_at_utc": now,
                "source_id": source_id, "batch_id": batch_id, "published_local_date": local,
                "market_date": news_time.market_date(published), "story_key": key, "duplicate_of": dup})
            ins += 1
            if dup and stats is not None:
                stats["duplicates"] = stats.get("duplicates", 0) + 1
        linked = db.insert_ignore(conn, "news_article_symbol", {
            "article_id": aid, "symbol": symbol, "source_id": source_id, "first_seen_utc": now})
        if exists is not None:
            if linked:
                if stats is not None:
                    stats["linked"] = stats.get("linked", 0) + 1
            else:
                skip += 1
    return ins, skip


def refresh_derived(conn) -> dict:
    """Fill Toronto dates and story keys, and link any article not yet in news_article_symbol.

    Idempotent. Needed because SQLite can't do time-zone conversion, and because older rows
    (and the demo generator) write news_article only.
    """
    rows = conn.execute(
        "SELECT article_id, headline, publisher, published_at_utc FROM news_article "
        "WHERE story_key IS NULL OR (published_at_utc IS NOT NULL AND published_local_date IS NULL)").fetchall()
    for r in rows:
        conn.execute("UPDATE news_article SET published_local_date = ?, market_date = ?, story_key = ? "
                     "WHERE article_id = ?",
                     (news_time.local_date(r["published_at_utc"]), news_time.market_date(r["published_at_utc"]),
                      news_time.story_key(r["headline"], r["publisher"]), r["article_id"]))
    linked = conn.execute(
        "INSERT INTO news_article_symbol (article_id, symbol, source_id, first_seen_utc) "
        "SELECT n.article_id, n.symbol, n.source_id, n.fetched_at_utc FROM news_article n "
        "WHERE NOT EXISTS (SELECT 1 FROM news_article_symbol s "
        "                  WHERE s.article_id = n.article_id AND s.symbol = n.symbol)").rowcount
    if rows or linked:
        conn.commit()
    return {"filled": len(rows), "linked": linked}


def _empty_totals(source: str) -> dict:
    return {"source": source, "inserted": 0, "skipped": 0, "errors": 0, "fetched": 0,
            "duplicates": 0, "linked": 0, "rate_limited": {}, "by_source": {}, "no_source": []}


def fetch_watchlist_news(conn, source: str = "auto", dry_run: bool = False, session=requests,
                         rss_fallback: bool | None = None) -> dict:
    """source: auto | finnhub | rss | none. rss_fallback=None reads NEWS_RSS_FALLBACK from the environment."""
    api_key = os.getenv("FINNHUB_API_KEY", "").strip()
    if rss_fallback is None:
        rss_fallback = _env_flag("NEWS_RSS_FALLBACK")
    if source == "finnhub" and not api_key:
        raise NewsSourceUnavailable("--source finnhub needs FINNHUB_API_KEY in your .env file.")
    use_finnhub = source in ("auto", "finnhub") and bool(api_key)
    use_rss = source == "rss" or (source == "auto" and rss_fallback)
    rss_window = _env_int("NEWS_RSS_WINDOW_DAYS", RSS_WINDOW_DEFAULT, 1, 30)
    rss_max = _env_int("NEWS_RSS_MAX_ITEMS", RSS_MAX_ITEMS_DEFAULT, 1, 100)
    if source == "none" or not (use_finnhub or use_rss):
        log.info("No news source enabled - keeping existing/demo news. Add FINNHUB_API_KEY to .env, "
                 "set NEWS_RSS_FALLBACK=1, or run with --source rss.")
        return _empty_totals("none")

    totals = _empty_totals(source)
    if not dry_run:
        refresh_derived(conn)
    batches: dict[str, str] = {}
    disabled: set[str] = set()                  # sources switched off for the rest of this run
    pacers = {s: Pacer(PACE_SECONDS[s]) for s in (FINNHUB, RSS)}
    for r in symbol_map.resolve_active(conn):
        plan = ([FINNHUB] if use_finnhub and r.finnhub_symbol else []) + ([RSS] if use_rss else [])
        attempted = False
        for src in plan:
            if src in disabled:
                continue
            attempted = True
            pacers[src].wait()
            try:
                if src == FINNHUB:
                    articles = fetch_finnhub(r.finnhub_symbol, api_key, session=session)
                else:
                    articles = fetch_google_news_rss(r.rss_query, session=session,
                                                     window_days=rss_window, max_items=rss_max)
            except FinnhubKeyRejected as exc:
                log.warning("%s Finnhub is off for the rest of this run.", exc)
                disabled.add(FINNHUB)
                totals["errors"] += 1
                continue
            except SourceRateLimited as exc:
                log.warning("%s %s is off for the rest of this run; no automatic retry.", exc, src)
                disabled.add(src)
                totals["rate_limited"][src] = exc.retry_after
                totals["errors"] += 1
                continue
            except NewsSourceUnavailable as exc:
                log.warning("%s (%s): %s", r.symbol, src, exc)
                totals["errors"] += 1
                continue
            except (requests.RequestException, ET.ParseError, ValueError) as exc:
                log.warning("%s (%s): couldn't fetch news (%s).", r.symbol, src, type(exc).__name__)
                totals["errors"] += 1
                continue
            per = totals["by_source"].setdefault(src, {"fetched": 0, "inserted": 0, "skipped": 0})
            per["fetched"] += len(articles)
            totals["fetched"] += len(articles)
            if not articles:
                log.info("%s (%s): no recent articles found.", r.symbol, src)
                continue                                    # try the next source
            log.info("%s (%s): %s article(s).", r.symbol, src, len(articles))                                # try the next source
            if not dry_run:
                if src not in batches:
                    batches[src] = db.start_batch(conn, src, f"news via {src}")
                ins, skip = store_articles(conn, r.symbol, articles, src, batches[src], stats=totals)
                per["inserted"] += ins
                per["skipped"] += skip
                totals["inserted"] += ins
                totals["skipped"] += skip
            break                                           # got articles: no need for a fallback
        if not attempted:
            totals["no_source"].append(r.symbol)
            log.info("%s: no news source (Finnhub: %s; RSS fallback %s).", r.symbol, r.finnhub_basis,
                     "on" if use_rss else "off")

    for src, batch in batches.items():
        per = totals["by_source"][src]
        db.finish_batch(conn, batch, read=per["fetched"], inserted=per["inserted"],
                        skipped=per["skipped"], rejected=0)
    if batches:
        conn.commit()
    log.info("News (%s%s): %s fetched, %s new (%s flagged as cross-source duplicates), %s repeats, "
             "%s linked to another symbol, %s errors; by source %s; no source: %s%s.",
             source, ", dry run" if dry_run else "", totals["fetched"], totals["inserted"],
             totals["duplicates"], totals["skipped"], totals["linked"], totals["errors"],
             {k: v["fetched"] for k, v in totals["by_source"].items()} or "none",
             totals["no_source"] or "none",
             f"; rate-limited: {totals['rate_limited']}" if totals["rate_limited"] else "")
    return totals