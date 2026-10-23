"""News quality per symbol (Phase 5): real news first, then DEMO, then MISSING, with reasons."""
from datetime import date

import pytest

from portfolio_pulse import db, export, news, symbol_map, synthetic
from portfolio_pulse import watchlist as wl

TODAY = date(2026, 10, 22)


@pytest.fixture
def setup(conn, monkeypatch):
    monkeypatch.delenv("NEWS_RSS_FALLBACK", raising=False)    # CLI tests may have loaded your .env
    wl.add_manual(conn, "SHOP", currency="CAD")
    symbol_map.set_mapping(conn, "SHOP", finnhub="SHOP")
    wl.add_manual(conn, "VFV", currency="CAD")
    symbol_map.set_mapping(conn, "VFV", finnhub=None, rss_query="VFV ETF")
    return conn


def store(conn, sym, url, headline, published, source="finnhub"):
    b = db.start_batch(conn, source, "test")
    news.store_articles(conn, sym, [{"url": url, "headline": headline, "publisher": "Wire",
                                     "published_at_utc": published}], source, b)


def quality(conn):
    q = export.build_quality(conn, today=TODAY)
    return q[q["area"] == "news"].set_index("item")


def test_recent_real_news_is_ok_with_routing_detail(setup):
    store(setup, "SHOP", "https://ex.org/a", "Shopify lifts outlook", "2026-10-21T15:00:00Z")
    q = quality(setup)
    assert (q.loc["SHOP", "status"], q.loc["SHOP", "age_days"], q.loc["SHOP", "last_date"]) == ("OK", 1, "2026-10-21")
    assert "Finnhub: SHOP" in q.loc["SHOP", "detail"] and "RSS fallback: off" in q.loc["SHOP", "detail"]


def test_old_real_news_is_stale(setup):
    store(setup, "SHOP", "https://ex.org/a", "Shopify lifts outlook", "2026-10-10T15:00:00Z")
    q = quality(setup)
    assert (q.loc["SHOP", "status"], q.loc["SHOP", "age_days"]) == ("STALE", 12)


def test_real_news_wins_over_demo(setup):
    store(setup, "SHOP", "https://example.com/demo-news/shop/1", "[DEMO] Shopify update",
          "2026-10-21T15:00:00Z", source="synthetic")
    assert quality(setup).loc["SHOP", "status"] == "DEMO"
    store(setup, "SHOP", "https://ex.org/real", "Shopify lifts outlook", "2026-10-20T15:00:00Z")
    assert quality(setup).loc["SHOP", "status"] == "OK"


def test_no_articles_is_missing_with_the_reason(setup, monkeypatch):
    q = quality(setup)
    assert q.loc["VFV", "status"] == "MISSING"
    assert "Finnhub: skipped (mapped_skip)" in q.loc["VFV", "detail"]
    monkeypatch.setenv("NEWS_RSS_FALLBACK", "1")
    assert "RSS fallback: on" in quality(setup).loc["VFV", "detail"]


def test_duplicates_dont_count_and_links_count_for_each_symbol(setup):
    store(setup, "SHOP", "https://finnhub.example/x", "Shopify's AI Checkout Push Lifts Shares", "2026-10-21T15:00:00Z")
    store(setup, "SHOP", "https://news.google.com/rss/articles/y", "Shopify\u2019s AI checkout push lifts shares",
          "2026-10-21T18:00:00Z", source="google_news_rss")
    store(setup, "VFV", "https://finnhub.example/x", "Shopify's AI Checkout Push Lifts Shares", "2026-10-21T15:00:00Z")
    q = quality(setup)
    assert q.loc["SHOP", "detail"].startswith("1 real, 0 demo, 1 cross-source duplicates")
    assert q.loc["VFV", "status"] == "OK"                     # same article, linked to VFV too


def test_export_fact_news_has_toronto_dates_and_bridge(conn, tmp_path):
    synthetic.load_demo(conn)
    export.export_all(conn, tmp_path)
    import csv
    with open(tmp_path / "fact_news.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows and all(r["published_local_date"] and r["market_date"] for r in rows)
    with open(tmp_path / "bridge_news_symbol.csv", newline="", encoding="utf-8") as f:
        assert len(list(csv.DictReader(f))) == 24