"""News time and identity rules (Phase 5). Pure functions: no database, no network.

Times: every source timestamp is stored in UTC; grouping uses America/Toronto (zoneinfo + tzdata),
so the DST changes on 2026-03-08 and 2026-11-01 are handled.
  published_local_date = the Toronto calendar date the article was published.
  market_date          = the first weekday session the article could relate to: published on a
                         weekday before 16:00 Toronto -> that day; at/after 16:00 or on a weekend
                         -> the next weekday. Weekday calendar: market holidays are NOT removed.
  "Published around this date" for trading day D = articles whose market_date is D or the weekday
  before D. It is a time window only and never claims a headline caused a price move.
Identity: story_key = hash of the normalized headline with the publisher suffix removed. It spots
the same story arriving from two sources with different links (Google News links are redirects).
"""
from __future__ import annotations

import html
import re
import unicodedata
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from . import db

MARKET_TZ = ZoneInfo("America/Toronto")
MARKET_CLOSE = time(16, 0)
_PUNCT = str.maketrans({"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
                        "\u2013": "-", "\u2014": "-"})
_NON_ALNUM = re.compile(r"[^0-9a-z]+")


def parse_utc(value: str | None) -> datetime | None:
    """'2026-10-22T19:59:00Z' -> aware UTC datetime; None for blank or malformed values."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def local_date(value: str | None) -> str | None:
    dt = parse_utc(value)
    return dt.astimezone(MARKET_TZ).date().isoformat() if dt else None


def next_weekday(d: date) -> date:
    d += timedelta(days=1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def previous_weekday(d: date) -> date:
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def market_date(value: str | None) -> str | None:
    dt = parse_utc(value)
    if dt is None:
        return None
    local = dt.astimezone(MARKET_TZ)
    if local.weekday() < 5 and local.time() < MARKET_CLOSE:
        return local.date().isoformat()
    return next_weekday(local.date()).isoformat()


def in_window(article_market_date: str, day: str) -> bool:
    """True if an article belongs under 'Published around this date' for trading day `day`."""
    d = date.fromisoformat(day)
    return previous_weekday(d).isoformat() <= article_market_date <= d.isoformat()


def clean_headline(headline: str | None, publisher: str | None = None) -> str:
    """Unescape HTML and drop a trailing ' - <publisher>' (Google News adds it); nothing else."""
    text = html.unescape(headline or "").strip()
    if publisher:
        suffix = f" - {publisher.strip()}"
        if len(text) > len(suffix) and text.lower().endswith(suffix.lower()):
            text = text[: -len(suffix)].rstrip()
    return text


def normalize_headline(headline: str | None, publisher: str | None = None) -> str:
    text = unicodedata.normalize("NFKC", clean_headline(headline, publisher)).translate(_PUNCT).lower()
    return _NON_ALNUM.sub(" ", text).strip()


def story_key(headline: str | None, publisher: str | None = None) -> str | None:
    norm = normalize_headline(headline, publisher)
    return db.stable_id("story", norm) if norm else None