"""symbol_map (migration 008): explicit mappings, safe defaults, provenance of every value."""
import pytest

from portfolio_pulse import symbol_map as sm
from portfolio_pulse import watchlist as wl


@pytest.fixture
def listed(conn):
    wl.add_manual(conn, "VFV", name="Vanguard S&P 500 Index ETF", currency="CAD")
    wl.add_manual(conn, "SHOP", name="Shopify Inc.", news_query="Shopify", currency="CAD")
    wl.add_manual(conn, "VTI", currency="USD")
    wl.add_manual(conn, "XYZ")                       # no currency known
    return conn


def test_defaults_never_guess_a_us_listing(listed):
    assert (sm.resolve(listed, "VTI").finnhub_symbol, sm.resolve(listed, "VTI").finnhub_basis) == ("VTI", "default_usd")
    for sym in ("VFV", "SHOP", "XYZ"):
        r = sm.resolve(listed, sym)
        assert (r.finnhub_symbol, r.finnhub_basis) == (None, "default_skip")


def test_rss_fallback_order(listed):
    assert (sm.resolve(listed, "SHOP").rss_query, sm.resolve(listed, "SHOP").rss_basis) == ("Shopify", "watchlist")
    assert (sm.resolve(listed, "VFV").rss_query, sm.resolve(listed, "VFV").rss_basis) == \
           ("Vanguard S&P 500 Index ETF", "name")
    r = sm.set_mapping(listed, "VFV", rss_query="VFV ETF")
    assert (r.rss_query, r.rss_basis) == ("VFV ETF", "mapped")


def test_cross_listed_company_maps_to_us_symbol(listed):
    r = sm.set_mapping(listed, "shop", finnhub="shop", note="TSX+Nasdaq SHOP; .TO is 403 on free plan",
                       checked_on="2026-10-22")
    assert (r.finnhub_symbol, r.finnhub_basis) == ("SHOP", "mapped")


def test_explicit_skip_is_recorded_differently_from_default(listed):
    r = sm.set_mapping(listed, "VTI", finnhub=None, note="ETF: company-news returns 0")
    assert (r.finnhub_symbol, r.finnhub_basis) == (None, "mapped_skip")
    assert sm.clear_mapping(listed, "VTI") is True
    assert sm.resolve(listed, "VTI").finnhub_basis == "default_usd"


def test_partial_update_keeps_other_fields(listed):
    sm.set_mapping(listed, "SHOP", finnhub="SHOP", rss_query="Shopify stock", note="cross-listed")
    r = sm.set_mapping(listed, "SHOP", rss_query="Shopify Inc")
    assert (r.finnhub_symbol, r.rss_query) == ("SHOP", "Shopify Inc")
    assert listed.execute("SELECT note FROM symbol_map WHERE symbol='SHOP'").fetchone()[0] == "cross-listed"


def test_first_mapping_without_finnhub_keeps_current_effective_value(listed):
    assert sm.set_mapping(listed, "VTI", rss_query="Vanguard Total Stock Market ETF").finnhub_symbol == "VTI"
    assert sm.set_mapping(listed, "VFV", rss_query="VFV ETF").finnhub_basis == "mapped_skip"


def test_validation_errors_are_friendly(listed):
    with pytest.raises(wl.WatchlistError, match="not on the watchlist"):
        sm.set_mapping(listed, "ZZZ", finnhub="ZZZ")
    with pytest.raises(wl.WatchlistError):
        sm.set_mapping(listed, "SHOP", finnhub="bad sym!")
    with pytest.raises(wl.WatchlistError):
        sm.set_mapping(listed, "SHOP", rss_query="   ")
    with pytest.raises(wl.WatchlistError):
        sm.set_mapping(listed, "SHOP", finnhub="SHOP", checked_on="22/10/2026")


def test_resolve_active_skips_inactive_symbols(listed):
    wl.remove(listed, "XYZ")
    assert [r.symbol for r in sm.resolve_active(listed)] == ["SHOP", "VFV", "VTI"]