"""Universe search and lookup, plus the CLI wiring for search and add --from-universe."""
import pytest

from portfolio_pulse import cli
from portfolio_pulse import universe as u
from portfolio_pulse import watchlist as wl
from test_universe_parse import CA_HEADER, US_HEADER, titles, workbook


@pytest.fixture
def loaded(conn, tmp_path):
    us = tmp_path / "IVV_holdings.xls"
    us.write_bytes(workbook(titles("iShares Core S&P 500 ETF", "Oct 21, 2026") + [US_HEADER,
        ["NVDA", "NVIDIA", "Information Technology", "Equity", "1", "8.0", "NASDAQ", "USD"],
        ["AAPL", "APPLE", "Information Technology", "Equity", "1", "7.5", "NASDAQ", "USD"],
        ["APP", "APPLOVIN CORP", "Information Technology", "Equity", "1", "0.4", "NASDAQ", "USD"]]))
    xeqt = tmp_path / "XEQT_holdings.xls"
    xeqt.write_bytes(workbook(titles("iShares Core Equity ETF Portfolio", "Oct 21, 2026") + [CA_HEADER,
        ["NVDA", "NVIDIA", "Information Technology", "3.2", "NASDAQ", "CAD"],
        ["RY", "ROYAL BANK OF CANADA", "Financials", "2.0", "Toronto Stock Exchange", "CAD"],
        ["SHOP", "SHOPIFY SUBORDINATE VOTI", "Information Technology", "1.5", "Toronto Stock Exchange", "CAD"]]))
    manual = tmp_path / "manual.csv"
    manual.write_text("symbol,name,exchange,asset_type\nVFV,Vanguard S&P 500 Index ETF,TSX,etf\n"
                      "SHOP,Shopify Inc,NASDAQ,equity\n", encoding="utf-8")
    for f, lid in ((us, "IVV"), (xeqt, "XEQT"), (manual, "MANUAL")):
        u.import_list(conn, f, lid)
    return conn


def test_exact_ticker_ranks_before_name_matches(loaded):
    assert [h["symbol"] for h in u.search(loaded, "app")] == ["APP", "AAPL"]


def test_name_search_is_case_insensitive(loaded):
    assert [(h["symbol"], h["exchange"]) for h in u.search(loaded, "royal bank")] == [("RY", "TSX")]


def test_one_row_per_listing_with_all_its_lists(loaded):
    hits = u.search(loaded, "NVDA")
    assert len(hits) == 1 and hits[0]["lists"] == "IVV,XEQT" and hits[0]["currency"] == "USD"


def test_cross_listed_symbol_shows_each_exchange_and_watchlist_status(loaded):
    wl.add_manual(loaded, "SHOP")
    hits = {h["exchange"]: h for h in u.search(loaded, "shop")}
    assert set(hits) == {"TSX", "NASDAQ"}
    assert hits["TSX"]["watchlist"] == "active" and hits["TSX"]["lists"] == "XEQT"
    assert u.search(loaded, "VFV")[0]["watchlist"] == "-"


def test_wildcards_are_literal_and_limit_applies(loaded):
    assert u.search(loaded, "%") == [] and u.search(loaded, "_") == []
    assert len(u.search(loaded, "a", limit=2)) == 2
    with pytest.raises(u.UniverseError):
        u.search(loaded, "   ")


def test_lookup_unique_ambiguous_filtered_and_unknown(loaded):
    assert u.lookup(loaded, "ry")["exchange"] == "TSX"
    with pytest.raises(u.UniverseError, match="--exchange"):
        u.lookup(loaded, "SHOP")
    assert u.lookup(loaded, "SHOP", "nasdaq")["currency"] == "USD"
    with pytest.raises(u.UniverseError, match="not in the symbol universe"):
        u.lookup(loaded, "ZZZ")


def test_cli_search_and_add_from_universe(loaded, tmp_path, capsys):
    path = str(tmp_path / "test.db")                       # same file the conn fixture uses
    assert cli.main(["--db", path, "watchlist", "search", "royal"]) == 0
    out = capsys.readouterr().out
    assert "RY" in out and "TSX" in out and "XEQT" in out
    assert cli.main(["--db", path, "watchlist", "add", "SHOP", "--from-universe"]) == 2      # ambiguous
    assert cli.main(["--db", path, "watchlist", "add", "SHOP", "--from-universe", "--exchange", "TSX"]) == 0
    r = loaded.execute("SELECT company_name, exchange, currency, origin FROM watchlist WHERE symbol = 'SHOP'").fetchone()
    assert tuple(r) == ("SHOPIFY SUBORDINATE VOTI", "TSX", "CAD", "manual")



def test_large_cap_lists_rank_before_small_cap_weight(conn, tmp_path):
    ivv = tmp_path / "IVV_holdings.xls"
    ivv.write_bytes(workbook(titles("iShares Core S&P 500 ETF", "Oct 21, 2026") + [US_HEADER,
        ["BNY", "BANK OF NEW YORK MELLON CORP", "Financials", "Equity", "1", "0.2", "NYSE", "USD"]]))
    iwm = tmp_path / "IWM_holdings.xls"
    iwm.write_bytes(workbook(titles("iShares Russell 2000 ETF", "Oct 21, 2026") + [US_HEADER,
        ["UBSI", "UNITED BANKSHARES", "Financials", "Equity", "1", "0.3", "NASDAQ", "USD"]]))
    u.import_list(conn, ivv, "IVV")
    u.import_list(conn, iwm, "IWM")
    assert [h["symbol"] for h in u.search(conn, "bank")] == ["BNY", "UBSI"]