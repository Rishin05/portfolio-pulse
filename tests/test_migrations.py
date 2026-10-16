"""Schema versioning: migrations apply once, in order, and roll back on failure."""
import pytest

from portfolio_pulse import db


@pytest.fixture
def fresh_conn(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    yield conn
    conn.close()


def tables(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_fresh_database_gets_baseline(fresh_conn):
    db.init_db(fresh_conn)
    assert db.current_schema_version(fresh_conn) == 1
    assert {"source", "account", "daily_snapshot", "cash_flow", "schema_version"} <= tables(fresh_conn)


def test_rerun_applies_nothing(fresh_conn):
    db.init_db(fresh_conn)
    assert db.migrate(fresh_conn) == []
    db.init_db(fresh_conn)
    assert fresh_conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    assert fresh_conn.execute("SELECT COUNT(*) FROM source").fetchone()[0] == len(db.SOURCES)


def test_pre_versioning_database_is_upgraded_in_place(fresh_conn):
    # Simulate a database built by the old init_db (schema.sql run directly, no schema_version).
    fresh_conn.executescript((db.MIGRATIONS_DIR / "001_baseline.sql").read_text(encoding="utf-8"))
    db.ensure_account(fresh_conn, "demo-tfsa", "Demo TFSA", "TFSA", "CAD", True)
    fresh_conn.commit()
    db.init_db(fresh_conn)
    assert db.current_schema_version(fresh_conn) == 1
    assert fresh_conn.execute("SELECT COUNT(*) FROM account").fetchone()[0] == 1   # data kept


def test_failed_migration_rolls_back(tmp_path, fresh_conn):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_first.sql").write_text("CREATE TABLE t1 (x INTEGER);", encoding="utf-8")
    (mig / "002_broken.sql").write_text(
        "CREATE TABLE t2 (x INTEGER);\nINSERT INTO no_such_table VALUES (1);", encoding="utf-8")
    with pytest.raises(db.MigrationError, match="002_broken"):
        db.migrate(fresh_conn, mig)
    assert db.current_schema_version(fresh_conn) == 1
    assert "t1" in tables(fresh_conn)
    assert "t2" not in tables(fresh_conn)   # half-applied work was rolled back


def test_database_newer_than_code_is_refused(fresh_conn):
    db.init_db(fresh_conn)
    fresh_conn.execute("INSERT INTO schema_version VALUES (99, '099_future', '2030-01-01T00:00:00Z')")
    fresh_conn.commit()
    with pytest.raises(db.MigrationError, match="version 99"):
        db.migrate(fresh_conn)


def test_duplicate_version_numbers_are_rejected(tmp_path):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "001_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (mig / "001_b.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(db.MigrationError, match="share version 001"):
        db.list_migrations(mig)