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