import dataclasses
import sqlite3

import pytest

from portfolio_pulse import cli, db, performance_store, synthetic


@pytest.fixture
def demo_conn(tmp_path):
    c = db.connect(tmp_path / "demo.db")
    db.init_db(c)
    synthetic.load_demo(c, 42)
    yield c
    c.close()


def counts(c):
    return (c.execute("SELECT COUNT(*) FROM perf_account_daily").fetchone()[0],
            c.execute("SELECT COUNT(*) FROM perf_household_daily").fetchone()[0])


def test_rebuild_replaces_never_duplicates(demo_conn):
    days, households, _ = performance_store.run_and_save(demo_conn)
    first = counts(demo_conn)
    performance_store.run_and_save(demo_conn)
    assert counts(demo_conn) == first == (len(days), len(households))
    assert first[0] > 0


def test_stored_demo_figures_match_demo_check(demo_conn):
    performance_store.run_and_save(demo_conn)
    best = demo_conn.execute("SELECT as_of_date, return_cad FROM perf_household_daily "
                             "WHERE household_id = 'DEMO' ORDER BY return_cad DESC LIMIT 1").fetchone()
    worst = demo_conn.execute("SELECT as_of_date, return_cad FROM perf_household_daily "
                              "WHERE household_id = 'DEMO' AND return_cad IS NOT NULL "
                              "ORDER BY return_cad ASC LIMIT 1").fetchone()
    assert (best["as_of_date"], round(best["return_cad"], 4)) == ("2026-03-30", 0.0459)
    assert (worst["as_of_date"], round(worst["return_cad"], 4)) == ("2026-01-16", -0.0218)


def test_failed_save_keeps_previous_results(demo_conn):
    days, households, _ = performance_store.run_and_save(demo_conn)
    before = counts(demo_conn)
    broken = [*days[:-1], dataclasses.replace(days[-1], fx_status="bogus")]   # violates CHECK
    with pytest.raises(sqlite3.IntegrityError):
        performance_store.save(demo_conn, broken, households, lambda a: "DEMO")
    assert counts(demo_conn) == before                                         # rolled back


def test_cli_performance_command(tmp_path):
    db_path = str(tmp_path / "t.db")
    assert cli.main(["--db", db_path, "demo"]) == 0
    assert cli.main(["--db", db_path, "performance"]) == 0
    with sqlite3.connect(db_path) as c:
        assert c.execute("SELECT COUNT(*) FROM perf_household_daily "
                         "WHERE household_id = 'DEMO'").fetchone()[0] > 0