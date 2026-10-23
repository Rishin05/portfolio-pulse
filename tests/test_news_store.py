"""Migration 010 behaviour: article-symbol links, cross-source duplicate flags, backfill."""
import pytest

from portfolio_pulse import db, export, news, synthetic
from portfolio_pulse import watchlist as wl

T0 = "2026-10-22T14:00:00Z"   # Thu 10:00 Toronto


@pytest.fixture
def ready(conn):
    for sym in ("SHOP", "RY"):
        wl.add_manual(conn, sym)
    return conn, db.start_batch(conn, "finnhub", "test")


def art(url, headline, published=T0, publisher=None):
    return {"url": url, "headline": headline, "publisher": publisher, "published_at_utc": published}


def row(conn, url):
    return conn.execute("SELECT * FROM news_article WHERE article_id = ?",
                        (db.stable_id(news.normalize_url(url)),)).fetchone()


def links(conn):
    return sorted(tuple(r) for r in conn.execute("SELECT symbol, article_id FROM news_article_symbol"))


def test_new_article_gets_dates_story_key_and_link(ready):
    conn, b = ready
    assert news.store_articles(conn, "SHOP", [art("https://ex.org/a", "Shopify lifts outlook")], "finnhub", b) == (1, 0)
    r = row(conn, "https://ex.org/a")
    assert (r["published_local_date"], r["market_date"], r["duplicate_of"]) == ("2026-10-22", "2026-10-22", None)
    assert r["story_key"] and [s for s, _ in links(conn)] == ["SHOP"]


def test_same_url_for_a_second_symbol_is_linked_not_copied(ready):
    conn, b = ready
    a = art("https://ex.org/bank", "Canadian banks and tech rally")
    news.store_articles(conn, "SHOP", [a], "finnhub", b)
    stats = {}
    assert news.store_articles(conn, "RY", [a], "finnhub", b, stats=stats) == (0, 0)
    assert stats == {"linked": 1}
    assert conn.execute("SELECT COUNT(*) FROM news_article").fetchone()[0] == 1
    assert [s for s, _ in links(conn)] == ["RY", "SHOP"]
    assert news.store_articles(conn, "RY", [a], "finnhub", b) == (0, 1)       # exact repeat


def test_same_story_from_another_source_is_flagged_and_kept(ready):
    conn, b = ready
    news.store_articles(conn, "SHOP", [art("https://finnhub.example/x", "Shopify's AI Checkout Push Lifts Shares")],
                        "finnhub", b)
    rb = db.start_batch(conn, "google_news_rss", "test")
    stats = {}
    google = art("https://news.google.com/rss/articles/abc", "Shopify\u2019s AI checkout push lifts shares - Yahoo Finance",
                 published="2026-10-22T17:00:00Z", publisher="Yahoo Finance")
    assert news.store_articles(conn, "SHOP", [google], "google_news_rss", rb, stats=stats) == (1, 0)
    assert stats == {"duplicates": 1}
    first = row(conn, "https://finnhub.example/x")["article_id"]
    assert row(conn, "https://news.google.com/rss/articles/abc")["duplicate_of"] == first


@pytest.mark.parametrize("symbol, published", [
    ("SHOP", "2026-10-25T14:00:00Z"),     # same symbol, 3 days later: a new story
    ("RY", "2026-10-22T15:00:00Z"),       # same headline under another symbol: not a duplicate here
    ("SHOP", None),                       # no timestamp: never flagged
])
def test_not_flagged_outside_window_other_symbol_or_without_time(ready, symbol, published):
    conn, b = ready
    news.store_articles(conn, "SHOP", [art("https://ex.org/1", "Shopify lifts outlook")], "finnhub", b)
    news.store_articles(conn, symbol, [art("https://ex.org/2", "Shopify lifts outlook", published=published)],
                        "finnhub", b)
    assert row(conn, "https://ex.org/2")["duplicate_of"] is None


def test_refresh_backfills_legacy_and_demo_rows_idempotently(conn):
    synthetic.load_demo(conn)                       # writes news_article only (24 rows, no links)
    first = news.refresh_derived(conn)
    assert first["linked"] == 24 and first["filled"] == 24
    assert conn.execute("SELECT COUNT(*) FROM news_article WHERE published_local_date IS NULL").fetchone()[0] == 0
    assert news.refresh_derived(conn) == {"filled": 0, "linked": 0}


def test_export_runs_the_refresh_first(conn, tmp_path):
    synthetic.load_demo(conn)
    export.export_all(conn, tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM news_article_symbol").fetchone()[0] == 24