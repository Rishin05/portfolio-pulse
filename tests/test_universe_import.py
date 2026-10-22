"""symbol_universe loading (migration 009): diff refresh, stale-file guard, wrong-fund guard."""
import pytest

from portfolio_pulse import cli
from portfolio_pulse import universe as u
from test_universe_parse import US_HEADER, titles, workbook

NVDA = ["NVDA", "NVIDIA", "Information Technology", "Equity", "1", "8.0", "NASDAQ", "USD"]
AAPL = ["AAPL", "APPLE", "Information Technology", "Equity", "1", "7.5", "NASDAQ", "USD"]
MSFT = ["MSFT", "MICROSOFT", "Information Technology", "Equity", "1", "5.9", "NASDAQ", "USD"]


def ivv(tmp_path, rows, as_of="Oct 21, 2026", name="iShares Core S&P 500 ETF", fname="IVV_holdings.xls"):
    p = tmp_path / fname
    p.write_bytes(workbook(titles(name, as_of) + [US_HEADER] + rows))
    return p


def rows(conn, lid="IVV"):
    return {r["symbol"]: dict(r) for r in conn.execute("SELECT * FROM symbol_universe WHERE list_id = ?", (lid,))}


def test_first_import_loads_rows_with_provenance(conn, tmp_path):
    res = u.import_list(conn, ivv(tmp_path, [NVDA, AAPL]), "ivv")
    assert (res.added, res.updated, res.removed, res.rejected) == (2, 0, 0, 0)
    r = rows(conn)["NVDA"]
    assert (r["list_id"], r["currency"], r["holdings_as_of"], r["fund_name"], r["source_file"],
            r["source_id"], r["is_demo"]) == ("IVV", "USD", "2026-10-21", "iShares Core S&P 500 ETF",
                                              "IVV_holdings.xls", "ishares_holdings", 0)


def test_same_file_again_changes_nothing(conn, tmp_path):
    f = ivv(tmp_path, [NVDA, AAPL])
    u.import_list(conn, f, "IVV")
    batches = conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0]
    res = u.import_list(conn, f, "IVV")
    assert (res.added, res.updated, res.removed, res.unchanged) == (0, 0, 0, 2)
    assert conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == batches


def test_newer_file_adds_updates_and_removes(conn, tmp_path):
    u.import_list(conn, ivv(tmp_path, [NVDA, AAPL]), "IVV")
    newer = ivv(tmp_path, [["NVDA", "NVIDIA", "Information Technology", "Equity", "1", "8.1", "NASDAQ", "USD"],
                           MSFT], as_of="Oct 22, 2026", fname="IVV_new.xls")
    res = u.import_list(conn, newer, "IVV")
    assert (res.added, res.updated, res.removed) == (1, 1, 1)
    r = rows(conn)
    assert sorted(r) == ["MSFT", "NVDA"] and r["NVDA"]["weight_pct"] == pytest.approx(8.1)
    assert r["NVDA"]["holdings_as_of"] == "2026-10-22"


def test_older_file_is_refused(conn, tmp_path):
    u.import_list(conn, ivv(tmp_path, [NVDA], as_of="Oct 22, 2026"), "IVV")
    older = ivv(tmp_path, [NVDA, AAPL], as_of="Oct 21, 2026", fname="IVV_old.xls")
    with pytest.raises(u.UniverseError, match="older"):
        u.import_list(conn, older, "IVV")
    assert sorted(rows(conn)) == ["NVDA"]


def test_wrong_fund_for_a_known_list_is_refused(conn, tmp_path):
    f = ivv(tmp_path, [NVDA], name="iShares Russell 2000 ETF", fname="IWB_holdings.xls")
    with pytest.raises(u.UniverseError, match="Russell 1000"):
        u.import_list(conn, f, "IWB")
    assert rows(conn, "IWB") == {}
    g = ivv(tmp_path, [NVDA], name="iShares Russell 1000 ETF", fname="IWM_holdings.xls")
    with pytest.raises(u.UniverseError, match="Russell 2000"):
        u.import_list(conn, g, "IWM")
    assert rows(conn, "IWM") == {}


def test_dry_run_writes_no_rows_and_no_batch(conn, tmp_path):
    res = u.import_list(conn, ivv(tmp_path, [NVDA, AAPL]), "IVV", dry_run=True)
    assert res.added == 2 and rows(conn) == {}
    assert conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0


def test_manual_csv_list_with_demo_label(conn, tmp_path):
    f = tmp_path / "manual_etfs.csv"
    f.write_text("symbol,name,exchange\nVFV,Vanguard S&P 500 Index ETF,TSX\nVTI,Vanguard Total Stock Market ETF,NYSE_ARCA\n",
                 encoding="utf-8")
    res = u.import_list(conn, f, "MANUAL", is_demo=True)
    assert res.added == 2
    r = rows(conn, "MANUAL")
    assert (r["VFV"]["currency"], r["VFV"]["asset_type"], r["VFV"]["is_demo"], r["VFV"]["source_id"],
            r["VFV"]["holdings_as_of"]) == ("CAD", "etf", 1, "csv_import", None)


def test_rejects_file_lists_reasons(conn, tmp_path):
    f = ivv(tmp_path, [NVDA, ["--", "UNLISTED CO", "Health Care", "Equity", "1", "0", "NO MARKET", "USD"]])
    res = u.import_list(conn, f, "IVV", rejects_dir=tmp_path / "rejects")
    assert res.rejected == 1
    written = list((tmp_path / "rejects").glob("universe_IVV_*.csv"))
    assert len(written) == 1 and "exchange not covered: NO MARKET" in written[0].read_text(encoding="utf-8")


def test_bad_list_id_is_a_friendly_error(conn, tmp_path):
    with pytest.raises(u.UniverseError, match="--list"):
        u.import_list(conn, ivv(tmp_path, [NVDA]), "S&P 500")


def test_cli_universe_import_exit_codes(tmp_path):
    db_path = str(tmp_path / "cli.db")
    good = ivv(tmp_path, [NVDA])
    bad = ivv(tmp_path, [NVDA], name="iShares Russell 2000 ETF", fname="IWB_holdings.xls")
    assert cli.main(["--db", db_path, "universe-import", str(good), "--list", "IVV", "--dry-run"]) == 0
    assert cli.main(["--db", db_path, "universe-import", str(bad), "--list", "IWB"]) == 2

    

def test_rejects_written_only_when_the_list_changes(conn, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(u, "_write_rejects", lambda rejects, d, lid: calls.append(lid))
    f = ivv(tmp_path, [NVDA, ["--", "UNLISTED CO", "Health Care", "Equity", "1", "0", "NO MARKET", "USD"]])
    u.import_list(conn, f, "IVV", rejects_dir=tmp_path / "rejects")
    u.import_list(conn, f, "IVV", rejects_dir=tmp_path / "rejects")      # unchanged: no new rejects file
    assert calls == ["IVV"]