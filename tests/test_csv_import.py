import json
from pathlib import Path

import pytest

from portfolio_pulse import csv_import

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "sample_inputs" / "example_activity_export.csv"
MAP = json.loads((ROOT / "config" / "column_map.example.json").read_text())


def reasons(res):
    return " | ".join(r["reason"] for r in res.rejected)


def test_sample_file_validation(conn, tmp_path):
    res = csv_import.import_csv(conn, SAMPLE, MAP, rejects_dir=tmp_path)
    assert res.rows_read == 11
    assert res.ignored_types == 1                      # BUY row
    assert res.inserted == 4                           # CONT, DIV, WD, FEE
    r = reasons(res)
    for expected in ["duplicate", "not a number", "USD", "doesn't match", "isn't mapped", "missing"]:
        assert expected in r, expected
    assert (tmp_path / "example_activity_export_rejects.csv").exists()


def test_reimport_creates_no_duplicates(conn):
    csv_import.import_csv(conn, SAMPLE, MAP)
    again = csv_import.import_csv(conn, SAMPLE, MAP)
    assert again.inserted == 0 and again.skipped_duplicates == 4
    assert conn.execute("SELECT COUNT(*) FROM cash_flow").fetchone()[0] == 4


def test_dry_run_writes_nothing(conn):
    res = csv_import.import_csv(conn, SAMPLE, MAP, dry_run=True)
    assert res.inserted == 0
    assert conn.execute("SELECT COUNT(*) FROM cash_flow").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM import_batch").fetchone()[0] == 0


def test_signs_and_external_flags(conn):
    csv_import.import_csv(conn, SAMPLE, MAP)
    rows = {r["flow_type"]: r for r in conn.execute("SELECT * FROM cash_flow")}
    assert rows["WITHDRAWAL"]["amount"] == -200.0      # accounting-style (200.00)
    assert rows["DEPOSIT"]["is_external"] == 1
    assert rows["DIVIDEND"]["is_external"] == 0 and rows["FEE"]["is_external"] == 0


def test_wrong_column_names_give_friendly_error(conn):
    bad = {**MAP, "columns": {**MAP["columns"], "amount": "Net Amount"}}
    with pytest.raises(csv_import.ImportConfigError, match="Net Amount"):
        csv_import.import_csv(conn, SAMPLE, bad)


def test_unsigned_amounts_option(conn, tmp_path):
    f = tmp_path / "unsigned.csv"
    f.write_text("date,transaction,description,amount,currency\n2026-07-02,WD,out,200,CAD\n")
    assert csv_import.import_csv(conn, f, MAP).rejected                       # wrong sign rejected
    res = csv_import.import_csv(conn, f, {**MAP, "amounts_are_unsigned": True})
    assert res.inserted == 1
    assert conn.execute("SELECT amount FROM cash_flow").fetchone()[0] == -200


def test_snapshot_import_and_gap_warning(conn):
    smap = json.loads((ROOT / "config" / "snapshot_map.example.json").read_text())
    res = csv_import.import_csv(conn, ROOT / "sample_inputs" / "example_snapshots.csv", smap)
    assert res.inserted == 4
    assert any("missing between 2026-07-06 and 2026-07-10" in w for w in res.warnings)
    row = conn.execute("SELECT * FROM v_daily_performance WHERE as_of_date='2026-07-10'").fetchone()
    assert row["days_since_prev"] == 4


def test_missing_file_message(conn):
    with pytest.raises(FileNotFoundError, match="data/inbox"):
        csv_import.import_csv(conn, "nope.csv", MAP)
