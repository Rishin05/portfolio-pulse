"""Fault-isolated step runner for `run-daily`.

Every step runs on its own. If one raises, the error is logged, the
database connection is rolled back, and the next step still runs. The
run ends with a plain-English summary, a JSON run log, and an exit code
that Windows Task Scheduler shows as "Last Run Result".
"""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

OK, SKIPPED, FAILED = "OK", "SKIPPED", "FAILED"

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_CRITICAL = 2
EXIT_LOCKED = 3
EXIT_MEANING = {
    EXIT_OK: "all steps OK or skipped",
    EXIT_PARTIAL: "some steps failed; the Power BI export still ran",
    EXIT_CRITICAL: "a critical step failed (the Power BI files were not refreshed)",
    EXIT_LOCKED: "another run-daily was already running; nothing was done",
}
DETAIL_MAX = 300


class StepSkipped(Exception):
    """Raise inside a step to record SKIPPED (nothing to do, or an optional source isn't set up)."""


class StepFailed(Exception):
    """Raise inside a step to record an expected, explained FAILED result (no traceback logged)."""


class RunLocked(Exception):
    """Another run holds the lock file."""


@dataclass(frozen=True)
class Step:
    name: str
    func: Callable[[], str | None]   # returns an optional one-line detail for the summary
    critical: bool = False           # a critical failure means exit code 2


@dataclass
class StepResult:
    name: str
    status: str
    seconds: float
    detail: str = ""
    critical: bool = False


@dataclass
class RunReport:
    started_utc: str
    finished_utc: str = ""
    results: list[StepResult] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        failed = [r for r in self.results if r.status == FAILED]
        if any(r.critical for r in failed):
            return EXIT_CRITICAL
        return EXIT_PARTIAL if failed else EXIT_OK


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_steps(steps: Iterable[Step], on_failure: Callable[[], None] | None = None) -> RunReport:
    """Run every step in order.

    Only Exception subclasses are caught, so Ctrl+C (KeyboardInterrupt) still stops the run.
    `on_failure` (e.g. conn.rollback) runs after each failed step so a half-open
    transaction can't break the next one.
    """
    report = RunReport(started_utc=_utc_now())
    for step in steps:
        t0 = time.perf_counter()
        try:
            status, detail = OK, step.func() or ""
        except StepSkipped as exc:
            status, detail = SKIPPED, str(exc)
            log.info("Step %s skipped: %s", step.name, exc)
        except StepFailed as exc:
            status, detail = FAILED, str(exc)
            log.error("Step %s failed: %s", step.name, exc)
        except Exception as exc:  # noqa: BLE001 - orchestration boundary: one step must not stop the rest
            status, detail = FAILED, f"{type(exc).__name__}: {exc}"
            log.exception("Step %s failed unexpectedly; continuing with the next step.", step.name)
        if status == FAILED and on_failure is not None:
            try:
                on_failure()
            except Exception:  # noqa: BLE001
                log.exception("Cleanup after step %s also failed.", step.name)
        report.results.append(StepResult(step.name, status, round(time.perf_counter() - t0, 2),
                                         detail[:DETAIL_MAX], step.critical))
    report.finished_utc = _utc_now()
    return report


def summary_lines(report: RunReport) -> list[str]:
    lines = [f"run-daily summary ({len(report.results)} steps):"]
    for r in report.results:
        lines.append(f"  {r.status:<8}{r.name:<18}{r.seconds:>7.2f}s  {r.detail}".rstrip())
    lines.append(f"Exit code {report.exit_code}: {EXIT_MEANING[report.exit_code]}.")
    return lines


def write_run_log(report: RunReport, folder: Path, keep: int = 90) -> Path:
    """Write one JSON file per run (UTC-stamped, sortable) and keep only the newest `keep`."""
    folder.mkdir(parents=True, exist_ok=True)
    stamp = report.started_utc.replace("-", "").replace(":", "")
    path = folder / f"run-daily-{stamp}.json"
    payload = {
        "started_utc": report.started_utc,
        "finished_utc": report.finished_utc,
        "exit_code": report.exit_code,
        "exit_meaning": EXIT_MEANING[report.exit_code],
        "steps": [asdict(r) for r in report.results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    for old in sorted(folder.glob("run-daily-*.json"))[:-keep]:
        old.unlink(missing_ok=True)
    return path


class RunLock:
    """Lock file so a scheduled run and a manual run can't overlap.

    Created atomically (O_EXCL). A lock older than `stale_after_s` is assumed to be
    left over from a crash or power cut and is replaced, with a warning.
    """

    def __init__(self, path: Path, stale_after_s: int = 6 * 3600):
        self.path = path
        self.stale_after_s = stale_after_s

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and time.time() - self.path.stat().st_mtime > self.stale_after_s:
            log.warning("Removing stale lock %s (older than %s h).", self.path.name, self.stale_after_s // 3600)
            self.path.unlink(missing_ok=True)
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise RunLocked(f"Another run-daily appears to be running ({self.path.name} exists). "
                            "If it isn't, delete that file and try again.") from exc
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(f"pid={os.getpid()} started={_utc_now()}\n")
        return self

    def __exit__(self, *exc_info) -> bool:
        self.path.unlink(missing_ok=True)
        return False