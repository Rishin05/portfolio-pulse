"""CLI wiring for `watchlist` (logic itself is covered in test_watchlist.py)."""
from portfolio_pulse import cli, db


def test_watchlist_add_list_remove_and_friendly_error(tmp_path, capsys):
    path = str(tmp_path / "cli.db")
    assert cli.main(["--db", path, "watchlist", "add", "vfv", "--name", "Vanguard S&P 500 Index ETF"]) == 0
    assert cli.main(["--db", path, "watchlist", "list"]) == 0
    out = capsys.readouterr().out
    assert "VFV" in out and "manual" in out
    assert cli.main(["--db", path, "watchlist", "remove", "VFV"]) == 0
    assert cli.main(["--db", path, "watchlist", "remove", "ZZZ"]) == 2      # friendly error, exit 2
    c = db.connect(path)
    assert tuple(c.execute("SELECT active, origin FROM watchlist WHERE symbol = 'VFV'").fetchone()) == (0, "manual")
    c.close()


def test_watchlist_sync_dry_run_on_empty_db(tmp_path):
    assert cli.main(["--db", str(tmp_path / "e.db"), "watchlist", "sync", "--dry-run"]) == 0



def test_watchlist_map_sources_unmap(tmp_path, capsys):
    import pytest
    path = str(tmp_path / "m.db")
    assert cli.main(["--db", path, "watchlist", "add", "SHOP", "--currency", "CAD"]) == 0
    assert cli.main(["--db", path, "watchlist", "map", "shop", "--finnhub", "shop", "--rss", "Shopify",
                     "--note", "cross-listed", "--checked-on", "2026-10-22"]) == 0
    assert cli.main(["--db", path, "watchlist", "sources"]) == 0
    out = capsys.readouterr().out
    assert "SHOP" in out and "mapped" in out and '"Shopify"' in out
    with pytest.raises(SystemExit):                      # --finnhub and --no-finnhub together
        cli.main(["--db", path, "watchlist", "map", "SHOP", "--finnhub", "SHOP", "--no-finnhub"])
    assert cli.main(["--db", path, "watchlist", "map", "ZZZ", "--no-finnhub"]) == 2   # friendly error
    assert cli.main(["--db", path, "watchlist", "unmap", "SHOP"]) == 0
    capsys.readouterr()
    assert cli.main(["--db", path, "watchlist", "sources"]) == 0
    assert "default_skip" in capsys.readouterr().out