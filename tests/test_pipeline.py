import json
import os
import time

import pytest

from portfolio_pulse import pipeline as p


def _boom():
    raise RuntimeError("network down")


def test_failed_step_does_not_block_later_steps():
    calls = []
    steps = [p.Step("snaptrade", _boom),
             p.Step("fx", lambda: calls.append("fx") or "12 rates"),
             p.Step("export", lambda: calls.append("export"), critical=True)]
    report = p.run_steps(steps)
    assert calls == ["fx", "export"]
    assert [r.status for r in report.results] == ["FAILED", "OK", "OK"]
    assert report.results[0].detail == "RuntimeError: network down"
    assert report.results[1].detail == "12 rates"
    assert report.exit_code == p.EXIT_PARTIAL


def test_skipped_is_not_a_failure():
    def no_keys():
        raise p.StepSkipped("no SnapTrade keys in .env")
    report = p.run_steps([p.Step("snaptrade", no_keys), p.Step("export", lambda: None, critical=True)])
    assert [r.status for r in report.results] == ["SKIPPED", "OK"]
    assert report.results[0].detail == "no SnapTrade keys in .env"
    assert report.exit_code == p.EXIT_OK


def test_critical_failure_sets_exit_code_2():
    report = p.run_steps([p.Step("news", _boom), p.Step("export", _boom, critical=True)])
    assert report.exit_code == p.EXIT_CRITICAL


def test_expected_failure_is_logged_without_traceback(caplog):
    def offline():
        raise p.StepFailed("Bank of Canada unreachable")
    report = p.run_steps([p.Step("fx", offline)])
    assert report.results[0].status == "FAILED"
    assert report.results[0].detail == "Bank of Canada unreachable"
    assert not any(rec.exc_info for rec in caplog.records)


def test_rollback_hook_runs_after_each_failure_and_cannot_stop_the_run():
    rollbacks = []

    def rollback():
        rollbacks.append(1)
        raise RuntimeError("rollback failed too")

    steps = [p.Step("a", _boom), p.Step("b", lambda: None), p.Step("c", _boom)]
    report = p.run_steps(steps, on_failure=rollback)
    assert len(rollbacks) == 2
    assert [r.status for r in report.results] == ["FAILED", "OK", "FAILED"]


def test_ctrl_c_is_not_swallowed():
    def interrupt():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        p.run_steps([p.Step("a", interrupt), p.Step("b", lambda: None)])


def test_long_detail_is_truncated():
    report = p.run_steps([p.Step("a", lambda: "x" * 1000)])
    assert len(report.results[0].detail) == p.DETAIL_MAX


def test_summary_shows_each_step_and_the_exit_code():
    report = p.run_steps([p.Step("fx", _boom), p.Step("export", lambda: "15 tables", critical=True)])
    text = "\n".join(p.summary_lines(report))
    assert "FAILED  fx" in text and "OK      export" in text and "15 tables" in text
    assert "Exit code 1:" in text


def test_run_log_is_written_and_old_logs_pruned(tmp_path):
    for i in range(3):
        (tmp_path / f"run-daily-20260101T00000{i}Z.json").write_text("{}", encoding="utf-8")
    report = p.run_steps([p.Step("fx", lambda: "ok")])
    path = p.write_run_log(report, tmp_path, keep=2)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["exit_code"] == 0 and data["steps"][0]["name"] == "fx"
    remaining = sorted(x.name for x in tmp_path.glob("run-daily-*.json"))
    assert len(remaining) == 2 and path.name in remaining


def test_lock_blocks_a_second_run(tmp_path):
    lock = tmp_path / "run-daily.lock"
    with p.RunLock(lock):
        assert lock.exists()
        with pytest.raises(p.RunLocked):
            with p.RunLock(lock):
                pass
        assert lock.exists()          # the failed second attempt must not remove the first run's lock
    assert not lock.exists()


def test_stale_lock_is_replaced(tmp_path):
    lock = tmp_path / "run-daily.lock"
    lock.write_text("pid=1 started=old\n", encoding="utf-8")
    old = time.time() - 7 * 3600
    os.utime(lock, (old, old))
    with p.RunLock(lock, stale_after_s=6 * 3600):
        assert f"pid={os.getpid()}" in lock.read_text(encoding="utf-8")
    assert not lock.exists()


def test_lock_is_released_when_the_run_raises(tmp_path):
    lock = tmp_path / "run-daily.lock"
    with pytest.raises(RuntimeError):
        with p.RunLock(lock):
            raise RuntimeError("crash mid-run")
    assert not lock.exists()