"""Phase 5 gap sweep: undated items, malformed feeds, offset timestamps end-to-end, integer ages."""
import csv
from datetime import date

from portfolio_pulse import export, news, synthetic
from portfolio_pulse import watchlist as wl
from test_news_routing import RSS_ONE, Resp, Router, key  # noqa: F401  (key = Finnhub key fixture)


def test_finnhub_items_without_a_usable_time_are_kept_undated(conn, key):
    wl.add_manual(conn, "VTI", currency="USD")
    payload = [{"headline": "Zero time", "url": "https://ex.org/n", "source": "Wire", "datetime": 0},
               {"headline": "No time", "url": "https://ex.org/m", "source": "Wire"}]
    news.fetch_watchlist_news(conn, "auto", session=Router(finnhub=Resp(payload=payload)), rss_fallback=False)
    rows = conn.execute("SELECT published_at_utc, published_local_date, market_date FROM news_article").fetchall()
    assert [tuple(r) for r in rows] == [(None, None, None), (None, None, None)]
    q = export.build_quality(conn, today=date(2026, 10, 22))
    q = q[q["area"] == "news"].set_index("item")
    assert q.loc["VTI", "status"] == "STALE" and "2 without timestamp" in q.loc["VTI", "detail"]


def test_malformed_rss_for_one_symbol_does_not_stop_the_next(conn, key):
    for sym in ("AAA", "BBB"):
        wl.add_manual(conn, sym, currency="CAD")

    class Flaky(Router):
        def get(self, url, **kw):
            self.calls.append((url, kw))
            return Resp(text="<rss><channel><item>broken") if len(self.calls) == 1 else Resp(text=RSS_ONE)

    res = news.fetch_watchlist_news(conn, "rss", session=Flaky())
    assert res["errors"] == 1 and res["inserted"] == 1 and res["no_source"] == []


def test_rss_offset_timestamp_becomes_utc_and_toronto_dates(conn, key):
    wl.add_manual(conn, "VFV", currency="CAD")
    xml = ('<?xml version="1.0"?><rss><channel><item><title>VFV after the close</title>'
           '<link>https://example.org/late</link><pubDate>Thu, 22 Oct 2026 18:00:00 -0400</pubDate>'
           '</item></channel></rss>')
    news.fetch_watchlist_news(conn, "rss", session=Router(rss=Resp(text=xml)))
    r = conn.execute("SELECT published_at_utc, published_local_date, market_date FROM news_article").fetchone()
    assert tuple(r) == ("2026-10-22T22:00:00Z", "2026-10-22", "2026-10-23")   # 18:00 Toronto: after close


def test_age_days_is_a_whole_number_in_the_export(conn, tmp_path):
    synthetic.load_demo(conn)
    export.export_all(conn, tmp_path)
    with open(tmp_path / "data_quality.csv", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert rows and all(r["age_days"] == "" or r["age_days"].lstrip("-").isdigit() for r in rows)