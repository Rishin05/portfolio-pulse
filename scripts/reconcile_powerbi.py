"""Write SQL control totals for the Power BI Reconciliation page.

Reads the SQLite database directly (read-only) and writes check_totals.csv into the same
folder Power BI loads. The report then shows SQL value vs Power BI value for each check.

Run it right after an export, so both sides describe the same data:

    # demo
    python scripts\\reconcile_powerbi.py --db data\\demo\\pulse_demo.db --out data\\powerbi_demo
    # personal (default database)
    python scripts\\reconcile_powerbi.py --db data\\pulse.db --out data\\powerbi

Only totals and row counts are written: no account numbers, headlines or secrets.
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

CAD_TOLERANCE = 0.005        # half a cent
RETURN_TOLERANCE = 0.0000005  # 0.00005 percentage points
COUNT_TOLERANCE = 0.0

COLUMNS = ["check_id", "check_name", "unit", "sql_value", "tolerance", "generated_at_utc"]


def _scalar(conn: sqlite3.Connection, sql: str) -> float:
    value = conn.execute(sql).fetchone()[0]
    return float(value) if value is not None else 0.0


def _twr_cad(conn: sqlite3.Connection) -> float:
    """Dollar-weighted daily return across accounts, compounded: the same rule as the DAX measure."""
    rows = conn.execute(
        "SELECT as_of_date, SUM(investment_change_cad), SUM(start_value_cad + net_flow_cad) "
        "FROM perf_account_daily WHERE return_cad IS NOT NULL GROUP BY as_of_date ORDER BY as_of_date"
    ).fetchall()
    growth = 1.0
    for _day, gain, base in rows:
        if base:
            growth *= 1.0 + gain / base
    return growth - 1.0 if rows else 0.0


def build_checks(conn: sqlite3.Connection) -> list[dict]:
    checks = [
        ("perf_rows", "Performance rows (account-days)", "rows", COUNT_TOLERANCE,
         _scalar(conn, "SELECT COUNT(*) FROM perf_account_daily")),
        ("end_value_cad", "Portfolio value at end: each account's last day, summed", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(end_value_cad) FROM perf_account_daily p WHERE as_of_date = "
                       "(SELECT MAX(as_of_date) FROM perf_account_daily q WHERE q.account_id = p.account_id)")),
        ("net_flow_cad", "Net deposits and transfers", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(net_flow_cad) FROM perf_account_daily")),
        ("investment_change_cad", "Investment gain or loss", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(investment_change_cad) FROM perf_account_daily")),
        ("market_change_cad", "Market gain or loss", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(market_change_cad) FROM perf_account_daily")),
        ("fx_change_cad", "Currency gain or loss", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(fx_change_cad) FROM perf_account_daily")),
        ("twr_cad", "Time-weighted return (summed dollars per day, compounded)", "return", RETURN_TOLERANCE,
         _twr_cad(conn)),
        ("activity_rows", "Activity rows", "rows", COUNT_TOLERANCE,
         _scalar(conn, "SELECT COUNT(*) FROM v_activity_cad")),
        ("activity_amount_cad", "Activity amount, all types", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(amount_cad) FROM v_activity_cad")),
        ("deposits_cad", "Deposits and transfers in", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(amount_cad) FROM v_activity_cad "
                       "WHERE activity_type IN ('DEPOSIT', 'TRANSFER_IN')")),
        ("holding_rows", "Holding snapshot rows", "rows", COUNT_TOLERANCE,
         _scalar(conn, "SELECT COUNT(*) FROM v_holding_cad")),
        ("holding_value_latest_cad", "Holdings value at the latest holdings snapshot", "CAD", CAD_TOLERANCE,
         _scalar(conn, "SELECT SUM(market_value_cad) FROM v_holding_cad "
                       "WHERE as_of_date = (SELECT MAX(as_of_date) FROM v_holding_cad)")),
        ("news_articles", "News articles", "rows", COUNT_TOLERANCE,
         _scalar(conn, "SELECT COUNT(*) FROM news_article")),
        ("news_unique", "Unique news stories (not flagged as duplicates)", "rows", COUNT_TOLERANCE,
         _scalar(conn, "SELECT COUNT(*) FROM news_article WHERE duplicate_of IS NULL")),
    ]
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{"check_id": cid, "check_name": name, "unit": unit, "sql_value": f"{value:.10f}",
             "tolerance": f"{tol:.7f}", "generated_at_utc": stamp}
            for cid, name, unit, tol, value in checks]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, required=True, help="path to the SQLite database")
    parser.add_argument("--out", type=Path, required=True, help="folder Power BI reads (check_totals.csv is written here)")
    args = parser.parse_args(argv)
    if not args.db.is_file():
        print(f"Database not found: {args.db}")
        return 2
    if not args.out.is_dir():
        print(f"Output folder not found: {args.out}. Run the export first.")
        return 2
    conn = sqlite3.connect(f"file:{args.db.resolve().as_posix()}?mode=ro", uri=True)
    try:
        rows = build_checks(conn)
    finally:
        conn.close()
    target = args.out / "check_totals.csv"
    with target.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} control totals to {target}")
    for r in rows:
        print(f"  {r['check_id']:<26}{float(r['sql_value']):>16,.6f}  {r['unit']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
