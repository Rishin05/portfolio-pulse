"""Bank of Canada Valet FX rates: fetch, parse and resolve.

Pure functions only; database writes live elsewhere.
Rates are quote per 1 base, e.g. FXUSDCAD = CAD per 1 USD
(same direction as fx_rate.rate).
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

VALET_BASE = "https://www.bankofcanada.ca/valet/observations"
DEFAULT_SERIES: tuple[str, ...] = ("FXUSDCAD",)
MAX_CARRY_FORWARD_DAYS = 5  # proposed rule; confirm before Step 1b
TIMEOUT_SECONDS = 20


@dataclass(frozen=True)
class FxObservation:
    series: str       # e.g. "FXUSDCAD"
    base: str         # "USD"
    quote: str        # "CAD"
    rate_date: date
    rate: Decimal     # quote per 1 base


@dataclass(frozen=True)
class ResolvedRate:
    target_date: date
    rate_date: date | None
    rate: Decimal | None
    status: str       # reported | carried_forward | stale | missing
    gap_days: int | None


def series_to_pair(series: str) -> tuple[str, str]:
    """'FXUSDCAD' -> ('USD', 'CAD')."""
    if len(series) != 8 or not series.startswith("FX") or not series[2:].isalpha():
        raise ValueError(f"Not a BoC FX series code: {series!r}")
    return series[2:5], series[5:8]


def build_url(series: tuple[str, ...] | list[str], start: date, end: date) -> str:
    for code in series:
        series_to_pair(code)  # validate early
    query = urllib.parse.urlencode(
        {"start_date": start.isoformat(), "end_date": end.isoformat()}
    )
    return f"{VALET_BASE}/{','.join(series)}/json?{query}"


def fetch_json(url: str, timeout: int = TIMEOUT_SECONDS) -> dict:
    """GET a public Valet URL. No key, no credentials."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "portfolio-pulse (educational analytics)"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)


def parse_observations(
    payload: dict, series: tuple[str, ...] | list[str]
) -> tuple[list[FxObservation], list[str]]:
    """Turn a Valet payload into observations. Returns (observations, warnings).

    A missing or blank value for a series on a date is skipped silently
    (the BoC didn't publish it). Non-numeric or non-positive values are
    skipped with a warning.
    """
    observations: list[FxObservation] = []
    warnings: list[str] = []
    for row in payload.get("observations", []):
        raw_date = row.get("d")
        try:
            rate_date = date.fromisoformat(raw_date)
        except (TypeError, ValueError):
            warnings.append(f"Skipped row with bad date {raw_date!r}")
            continue
        for code in series:
            cell = row.get(code) or {}
            raw_value = cell.get("v")
            if raw_value in (None, ""):
                continue
            try:
                rate = Decimal(str(raw_value))
            except InvalidOperation:
                warnings.append(f"{code} {rate_date}: non-numeric value {raw_value!r}")
                continue
            if rate <= 0:
                warnings.append(f"{code} {rate_date}: non-positive rate {raw_value!r}")
                continue
            base, quote = series_to_pair(code)
            observations.append(FxObservation(code, base, quote, rate_date, rate))
    observations.sort(key=lambda o: (o.series, o.rate_date))
    return observations, warnings


def resolve_rate(
    rates_by_date: dict[date, Decimal],
    target: date,
    max_carry_days: int = MAX_CARRY_FORWARD_DAYS,
) -> ResolvedRate:
    """Pick the rate for `target` from one series' observations.

    Exact date -> reported. Otherwise the latest EARLIER rate:
    within max_carry_days -> carried_forward, beyond -> stale.
    Never uses a later rate (no look-ahead). Nothing earlier -> missing.
    """
    if target in rates_by_date:
        return ResolvedRate(target, target, rates_by_date[target], "reported", 0)
    earlier = [d for d in rates_by_date if d < target]
    if not earlier:
        return ResolvedRate(target, None, None, "missing", None)
    rate_date = max(earlier)
    gap = (target - rate_date).days
    status = "carried_forward" if gap <= max_carry_days else "stale"
    return ResolvedRate(target, rate_date, rates_by_date[rate_date], status, gap)