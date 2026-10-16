from datetime import date

import pytest
import requests

from portfolio_pulse import db, export, news, synthetic

RSS = """<?xml version="1.0"?><rss><channel>
<item><title>Shopify expands payments</title><link>https://example.org/a?utm_source=x</link>
<pubDate>Tue, 13 Oct 2026 14:05:00 GMT</pubDate><source url="https://example.org">Example Daily</source></item>
<item><title>Shopify expands payments (dup)</title><link>https://EXAMPLE.org/a</link>
<pubDate>Tue, 13 Oct 2026 14:05:00 GMT</pubDate></item>
<item><title>No date story</title><link>https://example.org/b</link></item>
<item><title></title><link>https://example.org/c</link></item>
</channel></rss>"""


class FakeResp:
    def __init__(self, status=200, payload=None, text=""):
        self.status_code, self._payload, self.text = status, payload, text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, resp=None, exc=None):
        self.resp, self.exc, self.calls = resp, exc, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        if self.exc:
            raise self.exc
        return self.resp


def test_rss_parsing_timestamps_and_dedupe(conn):
    items = news.parse_rss(RSS)
    assert len(items) == 3                                   # empty title dropped
    assert items[0]["published_at_utc"] == "2026-10-13T14:05:00Z"
    assert items[2]["published_at_utc"] is None              # missing date kept, flagged later
    synthetic.load_demo(conn)
    b = db.start_batch(conn, "google_news_rss", "t")
    ins, skip = news.store_articles(conn, "SHOP", items, "google_news_rss", b)
    assert (ins, skip) == (2, 1)                              # utm + case variant deduped
    ins, skip = news.store_articles(conn, "SHOP", items, "google_news_rss", b)
    assert (ins, skip) == (0, 3)


def test_finnhub_mapping_and_key_in_header(conn, monkeypatch):
    payload = [{"headline": "RBC news", "url": "https://example.org/rbc", "source": "Wire", "datetime": 1760364300},
               {"headline": "", "url": "https://example.org/x"}]
    s = FakeSession(FakeResp(payload=payload))
    out = news.fetch_finnhub("RY", "secret-key", session=s)
    assert out == [{"headline": "RBC news", "url": "https://example.org/rbc", "publisher": "Wire",
                    "published_at_utc": "2025-10-13T14:05:00Z"}]
    url, kw = s.calls[0]
    assert "secret-key" not in url and "token" not in kw["params"]
    assert kw["headers"]["X-Finnhub-Token"] == "secret-key"


def test_bad_key_and_outage_do_not_crash(conn, monkeypatch):
    synthetic.load_demo(conn)
    monkeypatch.setenv("FINNHUB_API_KEY", "bad")
    res = news.fetch_watchlist_news(conn, "auto", session=FakeSession(FakeResp(status=401)))
    assert res["errors"] == 3 and res["inserted"] == 0
    res = news.fetch_watchlist_news(conn, "auto", session=FakeSession(exc=requests.ConnectionError()))
    assert res["errors"] == 3


def test_missing_key_falls_back_quietly(conn, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    assert news.fetch_watchlist_news(conn, "auto")["source"] == "none"
    with pytest.raises(news.NewsSourceUnavailable, match="FINNHUB_API_KEY"):
        news.fetch_watchlist_news(conn, "finnhub")


def test_quality_flags(conn):
    synthetic.load_demo(conn)
    db.ensure_account(conn, "REAL", "Real", None, "CAD", False)
    conn.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc) VALUES ('ZZZ','No News Co','x')")
    q = export.build_quality(conn, today=date(2026, 10, 15)).set_index("item")
    assert q.loc["DEMO-TFSA", "status"] == "DEMO"
    assert q.loc["REAL", "status"] == "MISSING"
    assert q.loc["ZZZ", "status"] == "MISSING"
    assert q.loc["SHOP", "status"] == "DEMO"


def test_export_writes_all_tables(conn, tmp_path):
    synthetic.load_demo(conn)
    counts = export.export_all(conn, tmp_path)
    for name in ["fact_daily_performance", "fact_cash_flow", "fact_news", "dim_date", "data_quality"]:
        assert (tmp_path / f"{name}.csv").exists() and counts[name] > 0
