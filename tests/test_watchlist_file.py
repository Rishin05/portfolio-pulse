"""Editable watchlist file: plan-then-apply, universe fill, mapping columns, idempotence, rejects."""
import pytest

from portfolio_pulse import cli, symbol_map
from portfolio_pulse import watchlist_file as wf
from portfolio_pulse.watchlist import WatchlistError
from test_universe_search import loaded  # noqa: F401  (fixture: small universe incl. RY, SHOP x2, VFV)


def write(tmp_path, text, name="watchlist.csv"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def wrow(conn, sym):
    return conn.execute("SELECT * FROM watchlist WHERE symbol = ?", (sym,)).fetchone()


def test_new_row_is_filled_from_the_universe(loaded, tmp_path):
    res = wf.import_file(loaded, write(tmp_path, "symbol\nry\n"))
    assert res.added == ["RY"]
    r = wrow(loaded, "RY")
    assert (r["company_name"], r["exchange"], r["currency"], r["origin"], r["active"]) == \
           ("ROYAL BANK OF CANADA", "TSX", "CAD", "manual", 1)


def test_same_file_again_changes_nothing(loaded, tmp_path):
    f = write(tmp_path, "symbol,finnhub,rss\nRY,RY,Royal Bank of Canada RBC\n")
    wf.import_file(loaded, f)
    before = [tuple(r) for r in loaded.execute("SELECT * FROM watchlist")] + \
             [tuple(r) for r in loaded.execute("SELECT * FROM symbol_map")]
    res = wf.import_file(loaded, f)
    assert (res.added, res.updated, res.unchanged) == ([], [], ["RY"])
    after = [tuple(r) for r in loaded.execute("SELECT * FROM watchlist")] + \
            [tuple(r) for r in loaded.execute("SELECT * FROM symbol_map")]
    assert after == before


def test_mapping_columns_set_news_routing(loaded, tmp_path):
    wf.import_file(loaded, write(tmp_path, 'symbol,finnhub,rss,note\nVFV,-,"""VFV ETF"" OR ""Vanguard S&P 500""",TSX-only\n'
                                           "SHOP,SHOP,Shopify,cross-listed\n".replace("SHOP,SHOP", "SHOP,SHOP")))
    vfv = symbol_map.resolve(loaded, "VFV")
    assert (vfv.finnhub_basis, vfv.rss_query) == ("mapped_skip", '"VFV ETF" OR "Vanguard S&P 500"')
    assert loaded.execute("SELECT note FROM symbol_map WHERE symbol='VFV'").fetchone()[0] == "TSX-only"


def test_active_zero_switches_off_and_pins_a_holding_row(loaded, tmp_path):
    loaded.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc, origin, is_demo) "
                   "VALUES ('NVDA', 'NVDA', 'x', 'holding', 0)")
    res = wf.import_file(loaded, write(tmp_path, "symbol,active\nNVDA,0\n"))
    assert res.deactivated == ["NVDA"]
    assert (wrow(loaded, "NVDA")["active"], wrow(loaded, "NVDA")["origin"]) == (0, "manual")


def test_symbols_not_in_the_file_are_left_alone(loaded, tmp_path):
    loaded.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc) VALUES ('ZZZ', 'Demo', 'x')")
    wf.import_file(loaded, write(tmp_path, "symbol\nRY\n"))
    assert (wrow(loaded, "ZZZ")["active"], wrow(loaded, "ZZZ")["origin"]) == (1, "demo")


def test_bad_rows_are_rejected_and_the_rest_applied(loaded, tmp_path):
    text = "symbol,active,currency,finnhub\nRY,1,,\nbad sym!,1,,\nAPP,maybe,,\nAAPL,1,CA,\nNVDA,1,,x y\nRY,0,,\n"
    res = wf.import_file(loaded, write(tmp_path, text), rejects_dir=tmp_path / "rejects")
    assert res.added == ["RY"]
    assert [r["line"] for r in res.rejects] == [3, 4, 5, 6, 7]
    assert "duplicate" in res.rejects[-1]["reason"]
    assert len(list((tmp_path / "rejects").glob("watchlist_*.csv"))) == 1


def test_dry_run_changes_nothing(loaded, tmp_path):
    res = wf.import_file(loaded, write(tmp_path, "symbol,finnhub\nRY,RY\n"), dry_run=True)
    assert res.added == ["RY"] and wrow(loaded, "RY") is None
    assert loaded.execute("SELECT COUNT(*) FROM symbol_map").fetchone()[0] == 0


def test_ambiguous_universe_listing_warns_unless_exchange_given(loaded, tmp_path):
    res = wf.import_file(loaded, write(tmp_path, "symbol\nSHOP\n"))
    assert any("--exchange" in w for w in res.warnings) and wrow(loaded, "SHOP")["company_name"] == "SHOP"
    wf.import_file(loaded, write(tmp_path, "symbol,exchange\nSHOP,TSX\n", name="w2.csv"))
    assert (wrow(loaded, "SHOP")["company_name"], wrow(loaded, "SHOP")["currency"]) == \
           ("SHOPIFY SUBORDINATE VOTI", "CAD")


def test_missing_symbol_column_and_soft_limit(loaded, tmp_path, monkeypatch):
    with pytest.raises(WatchlistError, match="symbol"):
        wf.import_file(loaded, write(tmp_path, "ticker\nRY\n"))
    monkeypatch.setattr(wf, "SOFT_LIMIT", 1)
    res = wf.import_file(loaded, write(tmp_path, "symbol\nRY\nNVDA\n", name="w3.csv"))
    assert any("active watchlist symbols" in w for w in res.warnings)


def test_cli_watchlist_import(loaded, tmp_path):
    path = str(tmp_path / "test.db")
    f = write(tmp_path, "symbol\nRY\n")
    assert cli.main(["--db", path, "watchlist", "import", str(f), "--dry-run"]) == 0
    assert cli.main(["--db", path, "watchlist", "import", str(tmp_path / "missing.csv")]) == 2