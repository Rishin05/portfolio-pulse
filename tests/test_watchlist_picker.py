"""Excel picker: dropdown labels, seeding, kept rows on regenerate, xlsx import round trip."""
import openpyxl
import pytest

from portfolio_pulse import cli
from portfolio_pulse import watchlist_file as wf
from test_universe_search import loaded  # noqa: F401  (universe: NVDA, AAPL, APP, RY, SHOP x2, VFV)


def seed(conn, tmp_path):
    f = tmp_path / "seed.csv"
    f.write_text("symbol,finnhub,rss,note\nRY,RY,,cross-listed\nVFV,-,VFV ETF,TSX-only\n", encoding="utf-8")
    wf.import_file(conn, f)


@pytest.mark.parametrize("cell, expected", [
    ("SHOP | TSX | SHOPIFY", ("SHOP", "TSX")), ("ry", ("ry", None)),
    ("SHOP|nasdaq|x", ("SHOP", "NASDAQ")), ("", ("", None))])
def test_split_pick(cell, expected):
    assert wf.split_pick(cell) == expected


def test_picker_seeds_manual_rows_and_builds_the_dropdown(loaded, tmp_path):
    seed(loaded, tmp_path)
    loaded.execute("INSERT INTO watchlist (symbol, company_name, added_at_utc) VALUES ('ZZZ', 'Demo', 'x')")
    out = tmp_path / "watchlist.xlsx"
    assert wf.write_picker(loaded, out) == {"rows": 2, "universe": 7, "kept": False}
    wb = openpyxl.load_workbook(out)
    ws = wb["Watchlist"]
    assert [c.value for c in ws[1]] == wf.WL_COLUMNS
    assert ws["A2"].value == "RY | TSX | ROYAL BANK OF CANADA" and ws["F2"].value == "RY"
    assert ws["A3"].value.startswith("VFV | TSX |") and ws["F3"].value == "-"
    dvs = ws.data_validations.dataValidation
    assert len(dvs) == 1 and dvs[0].formula1 == "Universe!$A$2:$A$8"
    assert wb["Universe"].max_row == 8 and "Help" in wb.sheetnames


def test_round_trip_seed_then_import_changes_nothing(loaded, tmp_path):
    seed(loaded, tmp_path)
    out = tmp_path / "watchlist.xlsx"
    wf.write_picker(loaded, out)
    res = wf.import_file(loaded, out)
    assert (res.added, res.updated, res.deactivated, sorted(res.unchanged)) == ([], [], [], ["RY", "VFV"])


def test_picked_values_import_with_their_exchange(loaded, tmp_path):
    out = tmp_path / "watchlist.xlsx"
    wf.write_picker(loaded, out)
    wb = openpyxl.load_workbook(out)
    wb["Watchlist"].append(["SHOP | NASDAQ | Shopify Inc", 1])
    wb["Watchlist"].append(["NVDA | NASDAQ | NVIDIA"])
    wb.save(out)
    res = wf.import_file(loaded, out)
    assert sorted(res.added) == ["NVDA", "SHOP"]
    shop = loaded.execute("SELECT exchange, currency, company_name FROM watchlist WHERE symbol = 'SHOP'").fetchone()
    assert tuple(shop) == ("NASDAQ", "USD", "Shopify Inc")


def test_regenerating_keeps_your_rows_and_refreshes_the_universe(loaded, tmp_path):
    out = tmp_path / "watchlist.xlsx"
    wf.write_picker(loaded, out)
    wb = openpyxl.load_workbook(out)
    wb["Watchlist"].append(["RY", 0, None, None, None, None, None, "my note"])
    wb.save(out)
    loaded.execute("DELETE FROM symbol_universe WHERE symbol = 'APP'")
    assert wf.write_picker(loaded, out) == {"rows": 1, "universe": 6, "kept": True}
    ws = openpyxl.load_workbook(out)["Watchlist"]
    assert (ws["A2"].value, ws["B2"].value, ws["H2"].value) == ("RY", 0, "my note")


def test_default_path_prefers_xlsx_and_warns_when_both(tmp_path):
    assert wf.default_path(tmp_path) == (None, None)
    (tmp_path / "watchlist.csv").write_text("symbol\n", encoding="utf-8")
    assert wf.default_path(tmp_path) == (tmp_path / "watchlist.csv", None)
    (tmp_path / "watchlist.xlsx").write_bytes(b"")
    path, note = wf.default_path(tmp_path)
    assert path.name == "watchlist.xlsx" and "Both" in note


def test_cli_picker_import_and_corrupt_file(loaded, tmp_path):
    db_path = str(tmp_path / "test.db")
    out = tmp_path / "wl.xlsx"
    assert cli.main(["--db", db_path, "watchlist", "picker", "--out", str(out)]) == 0
    assert cli.main(["--db", db_path, "watchlist", "import", str(out), "--dry-run"]) == 0
    bad = tmp_path / "bad.xlsx"
    bad.write_bytes(b"not a zip file")
    assert cli.main(["--db", db_path, "watchlist", "import", str(bad)]) == 2