"""Per-symbol news routing (Phase 5): Finnhub via symbol_map, opt-in RSS fallback, error handling."""
import pytest
import requests

from portfolio_pulse import news, symbol_map
from portfolio_pulse import watchlist as wl

RSS_ONE = """<?xml version="1.0"?><rss><channel><item><title>VFV story</title>
<link>https://example.org/vfv</link><pubDate>Wed, 21 Oct 2026 14:00:00 GMT</pubDate></item></channel></rss>"""
FH_ONE = [{"headline": "Shopify story", "url": "https://example.org/shop", "source": "Wire",
           "datetime": 1792850400}]


class Resp:
    def __init__(self, status=200, payload=None, text="", headers=None):
        self.status_code, self._payload, self.text = status, payload, text
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


class Router:
    """Answers Finnhub and Google News RSS differently and records every call."""
    def __init__(self, finnhub=None, rss=None):
        self.finnhub, self.rss, self.calls = finnhub, rss, []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.finnhub if "finnhub.io" in url else self.rss

    def hosts(self):
        return ["finnhub" if "finnhub.io" in u else "rss" for u, _ in self.calls]


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("FINNHUB_API_KEY", "test-key")


def stored(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT symbol, source_id, url FROM news_article ORDER BY symbol, url")]


def test_finnhub_uses_mapped_symbol_but_stores_under_watchlist_symbol(conn, key):
    wl.add_manual(conn, "SHOP.TO", name="Shopify Inc.", currency="CAD")
    symbol_map.set_mapping(conn, "SHOP.TO", finnhub="SHOP")
    s = Router(finnhub=Resp(payload=FH_ONE))
    res = news.fetch_watchlist_news(conn, "auto", session=s, rss_fallback=False)
    assert s.calls[0][1]["params"]["symbol"] == "SHOP"
    assert stored(conn) == [("SHOP.TO", "finnhub", "https://example.org/shop")]
    assert res["by_source"]["finnhub"]["inserted"] == 1


def test_unmapped_cad_symbol_has_no_source_without_rss_opt_in(conn, key):
    wl.add_manual(conn, "VFV", name="Vanguard S&P 500 Index ETF", currency="CAD")
    s = Router()
    res = news.fetch_watchlist_news(conn, "auto", session=s, rss_fallback=False)
    assert s.calls == [] and res["no_source"] == ["VFV"]


def test_rss_fallback_uses_mapped_phrase(conn, key):
    wl.add_manual(conn, "VFV", name="Vanguard S&P 500 Index ETF", currency="CAD")
    symbol_map.set_mapping(conn, "VFV", finnhub=None, rss_query="VFV ETF")
    s = Router(rss=Resp(text=RSS_ONE))
    res = news.fetch_watchlist_news(conn, "auto", session=s, rss_fallback=True)
    assert s.hosts() == ["rss"] and "VFV+ETF" in s.calls[0][0]
    assert stored(conn) == [("VFV", "google_news_rss", "https://example.org/vfv")]
    assert res["no_source"] == []


def test_403_is_not_a_key_error_and_hands_over_to_rss(conn, key, caplog):
    wl.add_manual(conn, "VFV", name="Vanguard S&P 500 Index ETF", currency="CAD")
    wl.add_manual(conn, "QQC", currency="CAD")
    symbol_map.set_mapping(conn, "VFV", finnhub="VFV.TO")
    symbol_map.set_mapping(conn, "QQC", finnhub="QQC.TO")
    s = Router(finnhub=Resp(status=403), rss=Resp(text=RSS_ONE))
    res = news.fetch_watchlist_news(conn, "auto", session=s, rss_fallback=True)
    assert s.hosts() == ["finnhub", "rss", "finnhub", "rss"]      # 403 doesn't switch Finnhub off
    assert res["errors"] == 2
    assert "not available on your plan" in caplog.text and "rejected the API key" not in caplog.text


def test_zero_finnhub_articles_tries_rss_next(conn, key):
    wl.add_manual(conn, "VTI", currency="USD")                        # default_usd -> Finnhub first
    s = Router(finnhub=Resp(payload=[]), rss=Resp(text=RSS_ONE))
    news.fetch_watchlist_news(conn, "auto", session=s, rss_fallback=True)
    assert s.hosts() == ["finnhub", "rss"]
    assert stored(conn)[0][1] == "google_news_rss"


def test_source_rss_never_calls_finnhub(conn, key):
    wl.add_manual(conn, "VTI", currency="USD")
    s = Router(rss=Resp(text=RSS_ONE))
    news.fetch_watchlist_news(conn, "rss", session=s)
    assert s.hosts() == ["rss"]


def test_dry_run_writes_no_rows_and_no_batch(conn, key):
    wl.add_manual(conn, "VTI", currency="USD")
    batches = conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0]
    res = news.fetch_watchlist_news(conn, "auto", dry_run=True, session=Router(finnhub=Resp(payload=FH_ONE)),
                                    rss_fallback=False)
    assert res["fetched"] == 1 and res["inserted"] == 0
    assert conn.execute("SELECT COUNT(*) FROM news_article").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == batches

    

RSS_THREE = """<?xml version="1.0"?><rss><channel>
<item><title>Old</title><link>https://example.org/1</link><pubDate>Mon, 19 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Newest</title><link>https://example.org/2</link><pubDate>Wed, 21 Oct 2026 10:00:00 GMT</pubDate></item>
<item><title>Undated</title><link>https://example.org/3</link></item>
</channel></rss>"""


@pytest.mark.parametrize("phrase, expected", [
    ("VFV ETF", '"VFV ETF" when:7d'),
    ('"VFV" OR "Vanguard S&P 500 Index ETF"', '"VFV" OR "Vanguard S&P 500 Index ETF" when:7d'),
    ("XEQT -crypto", "XEQT -crypto when:7d"),
    ('"Shopify" when:3d', '"Shopify" when:3d'),
])
def test_build_rss_query(phrase, expected):
    assert news.build_rss_query(phrase) == expected


def test_rss_cap_keeps_newest_first():
    s = Router(rss=Resp(text=RSS_THREE))
    items = news.fetch_google_news_rss("x", session=s, max_items=2)
    assert [i["headline"] for i in items] == ["Newest", "Old"]


def test_rss_window_and_cap_come_from_env(conn, key, monkeypatch):
    wl.add_manual(conn, "VFV", currency="CAD")
    monkeypatch.setenv("NEWS_RSS_WINDOW_DAYS", "3")
    monkeypatch.setenv("NEWS_RSS_MAX_ITEMS", "1")
    s = Router(rss=Resp(text=RSS_THREE))
    res = news.fetch_watchlist_news(conn, "rss", session=s)
    assert "when%3A3d" in s.calls[0][0] and res["fetched"] == 1


def test_bad_env_values_fall_back_to_defaults(conn, key, monkeypatch, caplog):
    wl.add_manual(conn, "VFV", currency="CAD")
    monkeypatch.setenv("NEWS_RSS_WINDOW_DAYS", "90")
    monkeypatch.setenv("NEWS_RSS_MAX_ITEMS", "lots")
    s = Router(rss=Resp(text=RSS_THREE))
    news.fetch_watchlist_news(conn, "rss", session=s)
    assert "when%3A7d" in s.calls[0][0]
    assert "NEWS_RSS_WINDOW_DAYS" in caplog.text and "NEWS_RSS_MAX_ITEMS" in caplog.text
    

def test_finnhub_429_switches_finnhub_off_and_reports_retry_after(conn, key, caplog):
    for sym in ("AAA", "BBB"):
        wl.add_manual(conn, sym, currency="USD")
    s = Router(finnhub=Resp(status=429, headers={"Retry-After": "30"}), rss=Resp(text=RSS_ONE))
    res = news.fetch_watchlist_news(conn, "auto", session=s, rss_fallback=True)
    assert s.hosts() == ["finnhub", "rss", "rss"]           # Finnhub asked once, then RSS fallback only
    assert res["rate_limited"] == {"finnhub": 30} and res["errors"] == 1
    assert "retry after 30s" in caplog.text and "no automatic retry" in caplog.text


def test_rss_503_switches_rss_off_for_the_run(conn, key):
    for sym in ("AAA", "BBB"):
        wl.add_manual(conn, sym, currency="CAD")
    s = Router(rss=Resp(status=503))
    res = news.fetch_watchlist_news(conn, "rss", session=s)
    assert s.hosts() == ["rss"]
    assert res["rate_limited"] == {"google_news_rss": None} and res["no_source"] == ["BBB"]


def test_retry_after_seconds_http_date_and_garbage():
    class R:
        headers = {"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}
    assert news._retry_after(R) == 0                         # a date in the past -> 0 seconds
    R.headers = {"Retry-After": "soon"}
    assert news._retry_after(R) is None
    R.headers = {}
    assert news._retry_after(R) is None


def test_pacer_spaces_calls_without_retrying():
    t, slept = [100.0], []
    p = news.Pacer(1.1, clock=lambda: t[0], sleep=slept.append)
    p.wait()
    t[0] += 0.5
    p.wait()                                                  # too soon: waits the remaining 0.6 s
    t[0] += 5
    p.wait()                                                  # long enough apart: no wait
    assert slept == [pytest.approx(0.6)]


def test_fetch_paces_each_source(conn, key, monkeypatch):
    slept = []
    monkeypatch.setattr(news, "_sleep", slept.append)
    for sym in ("AAA", "BBB", "CCC"):
        wl.add_manual(conn, sym, currency="USD")
    news.fetch_watchlist_news(conn, "auto", session=Router(finnhub=Resp(payload=FH_ONE)), rss_fallback=False)
    assert len(slept) == 2                                    # 3 instant Finnhub calls -> 2 waits