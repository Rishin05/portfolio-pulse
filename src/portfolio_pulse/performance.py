"""Portfolio Pulse engine v2: per-account and household performance in native currency and CAD.

Educational analytics only, not advice.

Confirmed rules (Project Memory):
  investment change = V(t) - V(t-1) - F;  return = change / (V(t-1) + F)
  F = external flows (deposits, withdrawals, transfers in/out) dated after the previous
  snapshot up to and including this one; flows land at the START of the period.
  Dividends, interest, fees and tax are part of return. Trades and FX conversions are not
  flows, so a conversion cost shows up as an investment loss, never as a withdrawal.
  TWR = product(1 + r) - 1. Household figures sum dollars, then divide.

Phase 3 additions (2026-10-19):
  * CAD basis: V(t) converts at that day's rate R1; V(t-1) and the period's flows convert at
    the previous snapshot's rate R0. Then (1 + r_cad) = (1 + r_native) * (R1 / R0), so the
    currency effect is exactly R1 / R0 - 1.
  * Dollar split: fx_change_cad = (V(t-1) + F_native) * (R1 - R0); market_change_cad = rest.
  * Transfers between your own accounts are paired and kept out of household external flows.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable

from .snaptrade_activity import CATEGORY_BY_ACTIVITY_TYPE

STATUS_RANK = {"identity": 0, "reported": 1, "carried_forward": 2, "stale": 3, "missing": 4}
TRANSFER_MAX_DAYS = 7
TRANSFER_TOLERANCE = 0.01


@dataclass(frozen=True)
class Snapshot:
    account_id: str
    as_of_date: date
    end_value: float
    currency: str
    fx_rate: float | None   # CAD per 1 unit of currency (1.0 for CAD); None = missing
    fx_status: str


@dataclass(frozen=True)
class Flow:
    activity_id: str
    account_id: str
    trade_date: date
    activity_type: str
    amount: float
    currency: str


@dataclass(frozen=True)
class AccountDay:
    account_id: str
    as_of_date: date
    prev_date: date | None
    currency: str
    start_value: float | None
    end_value: float
    net_flow: float | None
    investment_change: float | None
    return_native: float | None
    start_value_cad: float | None
    end_value_cad: float | None
    net_flow_cad: float | None
    investment_change_cad: float | None
    return_cad: float | None
    market_change_cad: float | None
    fx_change_cad: float | None
    currency_effect: float | None
    fx_status: str
    flows_cad: tuple[tuple[str, float | None], ...] = ()   # (activity_id, CAD value)


@dataclass(frozen=True)
class HouseholdDay:
    household_id: str
    as_of_date: date
    accounts: int
    accounts_with_return: int
    end_value_cad: float | None
    external_flow_cad: float | None     # contributions/withdrawals, own-account transfers excluded
    internal_transfer_cad: float | None # gross amount moved between your own accounts
    investment_change_cad: float | None
    market_change_cad: float | None
    fx_change_cad: float | None
    return_cad: float | None
    fx_status: str


def is_external(activity_type: str) -> bool:
    return CATEGORY_BY_ACTIVITY_TYPE.get(activity_type) == "external"


def worst_status(*statuses: str) -> str:
    return max(statuses, key=lambda s: STATUS_RANK[s])


def _sum(values: Iterable[float | None]) -> float | None:
    values = list(values)
    return None if any(v is None for v in values) else sum(values)


def household_for(is_demo: bool, source_id: str | None) -> str:
    if not is_demo:
        return "REAL"
    return "DEMO" if source_id in (None, "synthetic") else "PAPER"


def _flow_values(flow: Flow, account_ccy: str, rate_start: float | None):
    """(native, CAD) value of one flow, both at the period-start rate."""
    if flow.currency == account_ccy:
        return flow.amount, (None if rate_start is None else flow.amount * rate_start)
    if flow.currency == "CAD":
        return (None if rate_start is None else flow.amount / rate_start), flow.amount
    raise ValueError(f"{flow.activity_id}: a {flow.currency} flow into a {account_ccy} account "
                     "is not supported (only CAD or the account's own currency).")


def compute_account_days(snapshots: Iterable[Snapshot], flows: Iterable[Flow]) -> list[AccountDay]:
    snaps_by: dict[str, list[Snapshot]] = defaultdict(list)
    for s in snapshots:
        snaps_by[s.account_id].append(s)
    flows_by: dict[str, list[Flow]] = defaultdict(list)
    for f in flows:
        if is_external(f.activity_type):   # trades, FX conversions, income: not flows
            flows_by[f.account_id].append(f)

    out: list[AccountDay] = []
    for account_id in sorted(snaps_by):
        snaps = sorted(snaps_by[account_id], key=lambda s: s.as_of_date)
        account_flows = sorted(flows_by.get(account_id, []), key=lambda f: (f.trade_date, f.activity_id))
        prev: Snapshot | None = None
        for s in snaps:
            if prev is not None and s.currency != prev.currency:
                raise ValueError(f"{account_id}: snapshot currency changed {prev.currency} -> {s.currency}")
            period = [f for f in account_flows
                      if f.trade_date <= s.as_of_date and (prev is None or f.trade_date > prev.as_of_date)]
            rate_start = prev.fx_rate if prev else s.fx_rate
            values = [_flow_values(f, s.currency, rate_start) for f in period]
            net_flow = _sum(v[0] for v in values)
            net_flow_cad = _sum(v[1] for v in values)
            end_cad = None if s.fx_rate is None else s.end_value * s.fx_rate
            status = s.fx_status if prev is None else worst_status(prev.fx_status, s.fx_status)

            start = inv = r = None
            start_cad = inv_cad = r_cad = market = fx_change = effect = None
            if prev is not None:
                start = prev.end_value
                if net_flow is not None:
                    capital = start + net_flow
                    inv = s.end_value - start - net_flow
                    r = inv / capital if capital > 0 else None
                if prev.fx_rate is not None and end_cad is not None and net_flow_cad is not None:
                    start_cad = start * prev.fx_rate
                    capital_cad = start_cad + net_flow_cad
                    inv_cad = end_cad - start_cad - net_flow_cad
                    r_cad = inv_cad / capital_cad if capital_cad > 0 else None
                    if net_flow is not None:
                        fx_change = (start + net_flow) * (s.fx_rate - prev.fx_rate)
                        market = inv_cad - fx_change
                if r is not None and r_cad is not None and r != -1:
                    effect = (1 + r_cad) / (1 + r) - 1

            out.append(AccountDay(
                account_id=account_id, as_of_date=s.as_of_date,
                prev_date=prev.as_of_date if prev else None, currency=s.currency,
                start_value=start, end_value=s.end_value, net_flow=net_flow,
                investment_change=inv, return_native=r,
                start_value_cad=start_cad, end_value_cad=end_cad, net_flow_cad=net_flow_cad,
                investment_change_cad=inv_cad, return_cad=r_cad,
                market_change_cad=market, fx_change_cad=fx_change, currency_effect=effect,
                fx_status=status,
                flows_cad=tuple((f.activity_id, v[1]) for f, v in zip(period, values)),
            ))
            prev = s
    return out


def pair_transfers(flows: Iterable[Flow], household_of: Callable[[str], str],
                   max_days: int = TRANSFER_MAX_DAYS,
                   tolerance: float = TRANSFER_TOLERANCE) -> dict[str, str]:
    """Match TRANSFER_OUT to TRANSFER_IN between your own accounts. Returns {id: partner_id}.

    Same household, different accounts, same currency, amounts within tolerance, dates within
    max_days. Closest date wins; each leg is used once. Unmatched transfers stay external.
    """
    flows = list(flows)
    outs = sorted((f for f in flows if f.activity_type == "TRANSFER_OUT"),
                  key=lambda f: (f.trade_date, f.activity_id))
    ins = [f for f in flows if f.activity_type == "TRANSFER_IN"]
    used: set[str] = set()
    pairs: dict[str, str] = {}
    for o in outs:
        candidates = [
            i for i in ins
            if i.activity_id not in used
            and i.account_id != o.account_id
            and household_of(i.account_id) == household_of(o.account_id)
            and i.currency == o.currency
            and abs(abs(i.amount) - abs(o.amount)) <= tolerance
            and abs((i.trade_date - o.trade_date).days) <= max_days
        ]
        if candidates:
            best = min(candidates, key=lambda i: (abs((i.trade_date - o.trade_date).days), i.activity_id))
            used.add(best.activity_id)
            pairs[o.activity_id] = best.activity_id
            pairs[best.activity_id] = o.activity_id
    return pairs


def compute_household_days(account_days: Iterable[AccountDay], pairs: dict[str, str],
                           household_of: Callable[[str], str]) -> list[HouseholdDay]:
    """Sum account dollars per household and date, then divide (never average percentages).

    The return is None if any account that had a previous snapshot lacks a CAD return that
    day (e.g. a missing FX rate): a partial household figure would mislead.
    """
    groups: dict[tuple[str, date], list[AccountDay]] = defaultdict(list)
    for d in account_days:
        groups[(household_of(d.account_id), d.as_of_date)].append(d)

    out: list[HouseholdDay] = []
    for (household_id, day), rows in sorted(groups.items()):
        with_prev = [r for r in rows if r.prev_date is not None]
        with_ret = [r for r in with_prev if r.return_cad is not None]
        complete = bool(with_prev) and len(with_ret) == len(with_prev)
        flows = [(aid, v) for r in rows for aid, v in r.flows_cad]
        inv = sum(r.investment_change_cad for r in with_ret) if complete else None
        capital = sum(r.start_value_cad + r.net_flow_cad for r in with_ret) if complete else None
        out.append(HouseholdDay(
            household_id=household_id, as_of_date=day, accounts=len(rows),
            accounts_with_return=len(with_ret),
            end_value_cad=_sum(r.end_value_cad for r in rows),
            external_flow_cad=_sum(v for aid, v in flows if aid not in pairs),
            internal_transfer_cad=_sum(v for aid, v in flows if aid in pairs and (v is None or v > 0)),
            investment_change_cad=inv,
            market_change_cad=_sum(r.market_change_cad for r in with_ret) if complete else None,
            fx_change_cad=_sum(r.fx_change_cad for r in with_ret) if complete else None,
            return_cad=inv / capital if complete and capital > 0 else None,
            fx_status=worst_status(*(r.fx_status for r in rows)),
        ))
    return out


def twr(returns: Iterable[float | None]) -> float | None:
    """Time-weighted return by chaining: product(1 + r) - 1. Skips None (first day, missing FX)."""
    growth, seen = 1.0, False
    for r in returns:
        if r is not None:
            growth *= 1 + r
            seen = True
    return growth - 1 if seen else None


def load_inputs(conn) -> tuple[list[Snapshot], list[Flow], dict[str, str]]:
    """Read snapshots (with CAD rates), external activity, and each account's household."""
    households = {
        r["account_id"]: household_for(bool(r["is_demo"]), r["source_id"])
        for r in conn.execute(
            "SELECT a.account_id, a.is_demo, c.source_id FROM account a "
            "LEFT JOIN connection c ON c.connection_id = a.connection_id")
    }
    snaps = [
        Snapshot(r["account_id"], date.fromisoformat(r["as_of_date"]), r["end_value"],
                 r["currency"], r["fx_rate"], r["fx_status"])
        for r in conn.execute(
            "SELECT account_id, as_of_date, end_value, currency, fx_rate, fx_status FROM v_snapshot_cad")
    ]
    external = sorted(t for t, c in CATEGORY_BY_ACTIVITY_TYPE.items() if c == "external")
    marks = ",".join("?" * len(external))
    flows = [
        Flow(r["activity_id"], r["account_id"], date.fromisoformat(r["trade_date"]),
             r["activity_type"], r["amount"], r["currency"])
        for r in conn.execute(
            f"SELECT activity_id, account_id, trade_date, activity_type, amount, currency "
            f"FROM activity WHERE activity_type IN ({marks})", external)
    ]
    return snaps, flows, households


def run(conn) -> tuple[list[AccountDay], list[HouseholdDay], dict[str, str]]:
    snaps, flows, households = load_inputs(conn)
    household_of = lambda account_id: households.get(account_id, "REAL")
    account_days = compute_account_days(snaps, flows)
    pairs = pair_transfers(flows, household_of)
    return account_days, compute_household_days(account_days, pairs, household_of), pairs