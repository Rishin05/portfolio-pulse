import json
from types import SimpleNamespace

import pytest

from portfolio_pulse import cli, pipeline

STEP_FUNCS = ["_step_snaptrade", "_step_fx", "_step_watchlist_file", "_step_watchlist_sync",
              "_step_news", "_step_inbox", "_step_performance", "_step_export"]


class FakeConn:
    def __init__(self):
        self.rollbacks = 0

    def rollback(self):
        self.rollbacks += 1


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    return tmp_path


def _stub_steps(monkeypatch, calls, fail=()):
    for name in STEP_FUNCS:
        def fake(*args, _name=name):
            calls.append(_name)
            if _name in fail:
                raise RuntimeError(f"{_name} broke")
            return "ok"
        monkeypatch.setattr(cli, name, fake)


def test_one_failure_still_runs_every_step_and_exits_1(root, monkeypatch):
    calls = []
    _stub_steps(monkeypatch, calls, fail={"_step_snaptrade"})
    conn = FakeConn()
    code = cli.run_daily(conn, root / "column_map.json", "auto")
    assert calls == STEP_FUNCS
    assert code == pipeline.EXIT_PARTIAL
    assert conn.rollbacks == 1
    logs = list((root / "logs" / "run-daily").glob("run-daily-*.json"))
    assert len(logs) == 1
    data = json.loads(logs[0].read_text(encoding="utf-8"))
    assert [s["status"] for s in data["steps"]] == ["FAILED"] + ["OK"] * 7
    assert not (root / "logs" / "run-daily.lock").exists()


def test_failed_export_exits_2(root, monkeypatch):
    _stub_steps(monkeypatch, [], fail={"_step_export"})
    assert cli.run_daily(FakeConn(), root / "column_map.json", "auto") == pipeline.EXIT_CRITICAL


def test_held_lock_exits_3_and_runs_nothing(root, monkeypatch):
    calls = []
    _stub_steps(monkeypatch, calls)
    lock = root / "logs" / "run-daily.lock"
    lock.parent.mkdir(parents=True)
    lock.write_text("pid=999\n", encoding="utf-8")
    assert cli.run_daily(FakeConn(), root / "column_map.json", "auto") == pipeline.EXIT_LOCKED
    assert calls == []
    assert lock.exists()


def test_snaptrade_without_keys_is_skipped(root, monkeypatch):
    def no_keys():
        raise cli.snaptrade_readonly.SnapTradeConfigError("missing")
    monkeypatch.setattr(cli.snaptrade_readonly, "load_credentials", no_keys)
    with pytest.raises(pipeline.StepSkipped, match="no SnapTrade keys"):
        cli._step_snaptrade(FakeConn())


def test_boc_unreachable_is_failed(root, monkeypatch):
    monkeypatch.setattr(cli.fx_rates, "sync_boc_rates", lambda conn, **kw: {"status": "unavailable"})
    with pytest.raises(pipeline.StepFailed, match="Bank of Canada"):
        cli._step_fx(FakeConn())


def test_empty_inbox_is_skipped(root):
    with pytest.raises(pipeline.StepSkipped):
        cli._step_inbox(FakeConn(), root / "column_map.json")


def test_inbox_without_column_map_is_failed(root):
    inbox = root / "data" / "inbox"
    inbox.mkdir(parents=True)
    (inbox / "a.csv").write_text("x", encoding="utf-8")
    with pytest.raises(pipeline.StepFailed, match="column_map"):
        cli._step_inbox(FakeConn(), root / "column_map.json")


def test_bad_inbox_file_does_not_stop_the_next(root, monkeypatch):
    inbox = root / "data" / "inbox"
    inbox.mkdir(parents=True)
    for name in ("a.csv", "b.csv"):
        (inbox / name).write_text("x", encoding="utf-8")
    map_path = root / "column_map.json"
    map_path.write_text("{}", encoding="utf-8")
    imported = []

    def fake_import(conn, path, mapping, rejects_dir):
        if path.name == "a.csv":
            raise ValueError("bad header")
        imported.append(path.name)
        return SimpleNamespace(warnings=[], rejected=[])

    monkeypatch.setattr(cli.csv_import, "load_mapping", lambda p: {})
    monkeypatch.setattr(cli.csv_import, "import_csv", fake_import)
    conn = FakeConn()
    with pytest.raises(pipeline.StepFailed, match="1 of 2 file"):
        cli._step_inbox(conn, map_path)
    assert imported == ["b.csv"]
    assert conn.rollbacks == 1