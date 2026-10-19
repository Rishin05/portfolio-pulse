import functools
import sqlite3

import pytest

from portfolio_pulse import cli, fx_rates

PAYLOAD = {"observations": [
    {"d": "2026-10-15", "FXUSDCAD": {"v": "1.4196"}},
    {"d": "2026-10-16", "FXUSDCAD": {"v": "1.4154"}},
]}
REAL_SYNC = fx_rates.sync_boc_rates


def use_fetch(monkeypatch, fetch):
    monkeypatch.setattr(cli.fx_rates, "sync_boc_rates", functools.partial(REAL_SYNC, fetch=fetch))


def boc_rows(db_path):
    with sqlite3.connect(db_path) as c:
        return c.execute("SELECT COUNT(*) FROM fx_rate WHERE source_id='boc_valet'").fetchone()[0]


def test_fetch_fx_stores_rates_and_exits_0(tmp_path, monkeypatch):
    use_fetch(monkeypatch, lambda url: PAYLOAD)
    db_path = tmp_path / "t.db"
    code = cli.main(["--db", str(db_path), "fetch-fx", "--start", "2026-10-15", "--end", "2026-10-16"])
    assert code == 0
    assert boc_rows(db_path) == 2


def test_fetch_fx_offline_exits_2_and_writes_nothing(tmp_path, monkeypatch):
    def offline(url):
        raise OSError("no network")
    use_fetch(monkeypatch, offline)
    db_path = tmp_path / "t.db"
    assert cli.main(["--db", str(db_path), "fetch-fx", "--start", "2026-10-15",
                     "--end", "2026-10-16"]) == 2
    assert boc_rows(db_path) == 0


def test_fetch_fx_rejects_bad_date():
    with pytest.raises(SystemExit) as exc:
        cli.main(["fetch-fx", "--start", "19/10/2026"])
    assert exc.value.code == 2