"""News time/identity rules: Toronto dates across DST, market-date roll, window, story keys."""
import pytest

from portfolio_pulse import news_time as nt


@pytest.mark.parametrize("utc, expected", [
    ("2026-10-22T03:30:00Z", "2026-10-21"),      # EDT: 23:30 the day before
    ("2026-11-01T04:30:00Z", "2026-11-01"),      # still EDT (DST ends 06:00 UTC): 00:30
    ("2026-11-02T04:30:00Z", "2026-11-01"),      # EST now: 23:30 on the 1st
    ("2026-03-08T04:30:00Z", "2026-03-07"),      # EST before DST starts (07:00 UTC)
    ("2026-03-09T04:30:00Z", "2026-03-09"),      # EDT after: 00:30
    (None, None),
    ("not a date", None),
])
def test_local_date_across_dst(utc, expected):
    assert nt.local_date(utc) == expected


@pytest.mark.parametrize("utc, expected", [
    ("2026-10-22T19:59:00Z", "2026-10-22"),      # Thu 15:59 EDT: same session
    ("2026-10-22T20:00:00Z", "2026-10-23"),      # Thu 16:00 exactly: next weekday
    ("2026-10-23T20:30:00Z", "2026-10-26"),      # Fri after close: Monday
    ("2026-10-24T15:00:00Z", "2026-10-26"),      # Saturday: Monday
    ("2026-10-26T03:59:00Z", "2026-10-26"),      # Monday in UTC, still Sunday in Toronto: Monday
    ("2026-10-26T05:00:00Z", "2026-10-26"),      # Mon 01:00: Monday
    ("2026-11-02T20:30:00Z", "2026-11-02"),      # Mon 15:30 EST (would be 16:30 under EDT)
])
def test_market_date_roll(utc, expected):
    assert nt.market_date(utc) == expected


def test_published_around_this_date_window():
    assert nt.in_window("2026-10-26", "2026-10-26")
    assert nt.in_window("2026-10-23", "2026-10-26")       # Friday is the weekday before Monday
    assert not nt.in_window("2026-10-22", "2026-10-26")
    assert not nt.in_window("2026-10-27", "2026-10-26")


@pytest.mark.parametrize("headline, publisher, expected", [
    ("Shopify Rises - Yahoo Finance", "Yahoo Finance", "Shopify Rises"),
    ("Shopify 3.1% but Still Overvalued -- GF Score - GuruFocus", "GuruFocus",
     "Shopify 3.1% but Still Overvalued -- GF Score"),
    ("RBC posts results", None, "RBC posts results"),
    ("Earnings - Reuters", "Bloomberg", "Earnings - Reuters"),
    ("S&amp;P 500 hits record - CNBC", "cnbc", "S&P 500 hits record"),
])
def test_clean_headline(headline, publisher, expected):
    assert nt.clean_headline(headline, publisher) == expected


def test_story_key_matches_the_same_story_across_sources():
    google = nt.story_key("Shopify\u2019s AI checkout push lifts shares - Yahoo Finance", "Yahoo Finance")
    finnhub = nt.story_key("Shopify's AI Checkout Push Lifts Shares", "Benzinga")
    assert google is not None and google == finnhub
    assert nt.story_key("Shopify cuts jobs", None) != google
    assert nt.story_key(" - ", None) is None