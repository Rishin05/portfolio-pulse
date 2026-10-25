"""Privacy gate for the public demo export.

Run before building or publishing the demo .pbix, and in CI:
    python -m portfolio_pulse.demo_gate data/powerbi_demo
Exit 0 = safe to publish; 1 = at least one rule failed. Messages show counts, IDs
and link hosts only, never headlines or amounts.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from urllib.parse import urlparse

FLAG_COLUMNS = ("is_demo", "source_is_demo")
CATALOG_TABLES = {"dim_source"}   # its is_demo marks which *source* is synthetic; rows are not data
FORBIDDEN_COLUMNS = {"provider_ref", "user_secret", "consumer_key", "client_id", "api_key",
                     "password", "account_number", "institution_account_id"}
URL_COLUMNS = {"url", "source_url", "link"}
DEMO_HOSTS = ("example.com",)
TRUE_VALUES = {"1", "true"}
MAX_EXAMPLES = 3


def _read(path: Path) -> tuple[list[str], list[dict]]:
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def _is_true(value: str | None) -> bool:
    return (value or "").strip().lower() in TRUE_VALUES


def _is_demo_host(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in DEMO_HOSTS)


def check_export(folder: Path) -> list[str]:
    """Return a list of problems; an empty list means the folder is safe to publish."""
    files = sorted(folder.glob("*.csv"))
    if not files:
        return [f"No CSV files in {folder}"]
    tables = {f.stem: _read(f) for f in files}
    problems: list[str] = []

    # Standalone DEMO accounts only: no SnapTrade connection, not even a paper account.
    demo_accounts: set[str] = set()
    if "dim_account" not in tables:
        problems.append("dim_account.csv is missing")
    for r in tables.get("dim_account", ([], []))[1]:
        acc = r.get("account_id", "")
        if _is_true(r.get("is_demo")) and not (r.get("connection_id") or "").strip() and not acc.startswith("ST-"):
            demo_accounts.add(acc)
        else:
            problems.append(f"dim_account: {acc} is not a standalone DEMO account")

    for name, (cols, rows) in tables.items():
        bad_cols = sorted(FORBIDDEN_COLUMNS.intersection(c.lower() for c in cols))
        if bad_cols:
            problems.append(f"{name}: forbidden column(s) {bad_cols}")
        flag = next((c for c in FLAG_COLUMNS if c in cols), None)
        if flag and name not in CATALOG_TABLES:
            bad = sum(1 for r in rows if not _is_true(r.get(flag)))
            if bad:
                problems.append(f"{name}: {bad} row(s) not flagged DEMO")
        if "account_id" in cols and name != "dim_account":
            unknown = sorted({r["account_id"] for r in rows if r.get("account_id")} - demo_accounts)
            if unknown:
                problems.append(f"{name}: {len(unknown)} account(s) not in the DEMO accounts, "
                                f"e.g. {unknown[:MAX_EXAMPLES]}")
        for col in (c for c in cols if c.lower() in URL_COLUMNS):
            hosts = sorted({urlparse(r[col]).hostname or "?" for r in rows
                            if (r.get(col) or "").strip() and not _is_demo_host(r[col])})
            if hosts:
                problems.append(f"{name}.{col}: {len(hosts)} link host(s) outside example.com, "
                                f"e.g. {hosts[:MAX_EXAMPLES]}")
    return problems


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    folder = Path(args[0]) if args else Path("data") / "powerbi_demo"
    problems = check_export(folder)
    if problems:
        print(f"DEMO GATE FAILED for {folder} ({len(problems)} problem(s)):")
        for p in problems:
            print(f"  - {p}")
        return 1
    print(f"DEMO GATE PASSED for {folder}: {len(list(folder.glob('*.csv')))} file(s); "
          "standalone DEMO accounts and example.com links only.")
    return 0


if __name__ == "__main__":
    sys.exit(main())