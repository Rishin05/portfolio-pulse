import csv

import pytest

from portfolio_pulse import db, demo_gate, export, performance_store, synthetic

NEW_TABLES = {
    "fact_perf_account_daily": "v_perf_account_daily",
    "fact_perf_household_daily": "v_perf_household_daily",
    "fact_activity": "v_activity_cad",
    "fact_holding_snapshot": "v_holding_cad",
    "dim_connection": "connection",
    "dim_institution": "institution",
}


def _rows(path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def demo_export(tmp_path_factory):
    base = tmp_path_factory.mktemp("demo16")
    conn = db.connect(str(base / "demo.db"))
    db.init_db(conn)
    synthetic.load_demo(conn, 42)
    performance_store.run_and_save(conn)
    out = base / "powerbi_demo"
    counts = export.export_all(conn, out)
    yield conn, out, counts
    conn.close()


def test_new_tables_match_their_sources(demo_export):
    conn, _, counts = demo_export
    for table, source in NEW_TABLES.items():
        expected = conn.execute(f"SELECT COUNT(*) FROM {source}").fetchone()[0]
        assert counts[table] == expected, table
    assert counts["fact_perf_household_daily"] > 0


def test_dim_connection_never_exports_provider_ref(demo_export):
    _, out, _ = demo_export
    header = (out / "dim_connection.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert "provider_ref" not in header.split(",")


def test_dim_date_covers_every_fact_date(demo_export):
    _, out, _ = demo_export
    dates = {r["date"] for r in _rows(out / "dim_date.csv")}
    for table, cols in export.DATE_COLUMNS.items():
        path = out / f"{table}.csv"
        if not path.exists():
            continue
        for r in _rows(path):
            for col in cols:
                v = (r.get(col) or "")[:10]
                if v:
                    assert v in dates, f"{table}.{col} {v} missing from dim_date"


def test_export_info_states_the_contract_version(demo_export):
    _, out, _ = demo_export
    assert _rows(out / "export_info.csv")[0]["contract_version"] == export.EXPORT_CONTRACT_VERSION == "1.6"


def test_fresh_demo_export_passes_the_privacy_gate(demo_export):
    _, out, _ = demo_export
    assert demo_gate.check_export(out) == []