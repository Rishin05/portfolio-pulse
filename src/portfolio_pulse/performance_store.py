"""Save performance engine v2 results (derived data) and summarize them.

perf_account_daily and perf_household_daily are rebuilt on every run inside one transaction:
readers see the old results or the new ones, never a mix. They hold computed values only, so
replacing them is safe; source tables keep the insert-only rule.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict

from . import db, performance


def _iso(d):
    return d.isoformat() if d else None


def _insert(conn, table: str, row: dict) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))


def save(conn, account_days, household_days, household_of) -> tuple[int, int]:
    """Replace stored results atomically. Any error rolls back to the previous results."""
    now = db.utc_now()
    with conn:   # commit on success, roll back on any exception
        conn.execute("DELETE FROM perf_account_daily")
        conn.execute("DELETE FROM perf_household_daily")
        for d in account_days:
            row = asdict(d)
            row.pop("flows_cad")
            row.update(as_of_date=_iso(d.as_of_date), prev_date=_iso(d.prev_date),
                       household_id=household_of(d.account_id), computed_at_utc=now)
            _insert(conn, "perf_account_daily", row)
        for h in household_days:
            row = asdict(h)
            row.update(as_of_date=_iso(h.as_of_date), computed_at_utc=now)
            _insert(conn, "perf_household_daily", row)
    return len(account_days), len(household_days)


def run_and_save(conn):
    """Compute engine v2 from the database and store it. Returns (account_days, household_days, pairs)."""
    snaps, flows, households = performance.load_inputs(conn)
    household_of = lambda account_id: households.get(account_id, "REAL")
    account_days = performance.compute_account_days(snaps, flows)
    pairs = performance.pair_transfers(flows, household_of)
    household_days = performance.compute_household_days(account_days, pairs, household_of)
    save(conn, account_days, household_days, household_of)
    return account_days, household_days, pairs


def _money(v) -> str:
    return "n/a" if v is None else f"${v:,.2f}"


def _pct(v) -> str:
    return "n/a" if v is None else f"{v:+.2%}"


def summarize(account_days, household_days) -> list[str]:
    """Plain-English lines: one per household, plus the latest day of each non-CAD account."""
    lines: list[str] = []
    by_household = defaultdict(list)
    for h in household_days:
        by_household[h.household_id].append(h)
    for hid in sorted(by_household):
        rows = sorted(by_household[hid], key=lambda h: h.as_of_date)
        with_ret = [h for h in rows if h.return_cad is not None]
        flagged = sum(1 for h in rows if h.fx_status not in ("identity", "reported"))
        line = (f"{hid}: {rows[0].as_of_date}..{rows[-1].as_of_date}, {len(rows)} day(s), "
                f"end {_money(rows[-1].end_value_cad)}, "
                f"TWR {_pct(performance.twr(h.return_cad for h in rows))}, "
                f"contributions {_money(sum(h.external_flow_cad or 0 for h in rows))}, "
                f"investment change {_money(sum(h.investment_change_cad or 0 for h in with_ret))} "
                f"(market {_money(sum(h.market_change_cad or 0 for h in with_ret))}, "
                f"FX {_money(sum(h.fx_change_cad or 0 for h in with_ret))})")
        if with_ret:
            best = max(with_ret, key=lambda h: h.return_cad)
            worst = min(with_ret, key=lambda h: h.return_cad)
            line += (f", best {best.as_of_date} {_pct(best.return_cad)}, "
                     f"worst {worst.as_of_date} {_pct(worst.return_cad)}")
        if flagged:
            line += f", {flagged} day(s) with carried-forward/stale/missing FX"
        lines.append(line)
    latest = {}
    for d in account_days:
        if d.currency != "CAD" and d.prev_date is not None:
            if d.account_id not in latest or d.as_of_date > latest[d.account_id].as_of_date:
                latest[d.account_id] = d
    for account_id in sorted(latest):
        d = latest[account_id]
        lines.append(f"{account_id} {d.as_of_date}: {d.currency} {_pct(d.return_native)} | "
                     f"CAD {_pct(d.return_cad)} | currency effect {_pct(d.currency_effect)} "
                     f"(market {_money(d.market_change_cad)}, FX {_money(d.fx_change_cad)}) [{d.fx_status}]")
    return lines