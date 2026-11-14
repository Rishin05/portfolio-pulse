"""FX Fee Converter (Phase 4): amount + currency pair + platform -> cost vs the BoC rate.

The Bank of Canada rate is an indicative reference rate (published once per business day),
not a rate anyone can trade at. Platform costs are estimates built only from fees and quotes
you imported with a source URL and a check date. Educational analytics only - not advice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from . import boc_valet, fx_cost, fx_rates

MODELS = ("auto", "percent", "posted")
QUALITY_RANK = {"reported": 1, "estimated": 2, "STALE": 3, "MISSING": 4, "DEMO": 5}
FEE_RECHECK_DAYS = 90


class ConverterError(ValueError):
    """Why no estimate could be made. Written for a human; never replaced by a zero."""


@dataclass(frozen=True)
class ConverterResult:
    estimate: fx_cost.ConversionEstimate
    institution_id: str
    as_of_date: date
    boc_rate_date: date
    boc_status: str            # reported | carried_forward
    quality_label: str         # reported | estimated | STALE | DEMO (worst wins)
    source_ref: str            # fee_schedule_id or fx_posted_rate_id
    source_url: str
    date_checked: str
    is_demo: bool
    warnings: list[str] = field(default_factory=list)


def _worst(*labels: str) -> str:
    return max(labels, key=QUALITY_RANK.__getitem__)


def _dec(value) -> Decimal | None:
    return None if value is None else Decimal(str(value))


DEMO_RATE_SOURCE = "synthetic"


def rate_source(conn, institution_id: str) -> str:
    """Which stored USD/CAD series is the benchmark for this platform.

    Real institutions always use the Bank of Canada. A DEMO institution also does when Bank of
    Canada rates are stored; in a demo-only database (none stored) it falls back to the
    synthetic demo series so the public demo still has cost rows. Those rows are labelled DEMO.
    """
    row = conn.execute("SELECT is_demo FROM institution WHERE institution_id = ?", (institution_id,)).fetchone()
    if row is None or not row["is_demo"]:
        return fx_rates.SOURCE_ID
    has_boc = conn.execute("SELECT 1 FROM fx_rate WHERE base_currency = 'USD' AND quote_currency = 'CAD' "
                           "AND source_id = ? LIMIT 1", (fx_rates.SOURCE_ID,)).fetchone()
    return fx_rates.SOURCE_ID if has_boc else DEMO_RATE_SOURCE


def benchmark(conn, as_of: date, source_id: str = fx_rates.SOURCE_ID) -> boc_valet.ResolvedRate:
    """USD/CAD reference rate for `as_of` via the Phase 3 rule; stale or missing -> ConverterError."""
    r = fx_rates.rate_for(conn, "USD", "CAD", as_of, source_id)
    if r.status == "missing" and source_id == DEMO_RATE_SOURCE:
        raise ConverterError(f"No synthetic demo USD/CAD rate on or before {as_of}. "
                             "Run: python -m portfolio_pulse demo")
    if r.status == "missing":
        raise ConverterError(f"No Bank of Canada USD/CAD rate on or before {as_of}. "
                             "Run: python -m portfolio_pulse fetch-fx")
    if r.status == "stale":
        raise ConverterError(f"The latest Bank of Canada rate is from {r.rate_date}, {r.gap_days} days before "
                             f"{as_of} (limit {boc_valet.MAX_CARRY_FORWARD_DAYS}). Run fetch-fx or pick another date.")
    return r


def _fee_row(conn, institution_id: str, from_ccy: str, to_ccy: str, as_of: date):
    rows = conn.execute(
        "SELECT * FROM fee_schedule WHERE institution_id = ? AND fee_type = 'fx_conversion' "
        "AND rate_bps IS NOT NULL "
        "AND (effective_from IS NULL OR effective_from <= ?) AND (effective_to IS NULL OR effective_to >= ?) "
        "ORDER BY COALESCE(effective_from, '') DESC, date_checked DESC",
        (institution_id, as_of.isoformat(), as_of.isoformat())).fetchall()
    wanted = {f"{from_ccy}->{to_ccy}", f"{from_ccy}<->{to_ccy}", f"{to_ccy}<->{from_ccy}"}
    return next((r for r in rows if (r["applies_to"] or "") in wanted), None)


def _quotes(conn, institution_id: str, as_of: date, channel: str | None) -> list:
    sql = ("SELECT * FROM fx_posted_rate WHERE institution_id = ? AND base_currency = 'USD' "
           "AND quote_currency = 'CAD' AND rate_date = ?")
    params = [institution_id, as_of.isoformat()]
    if channel:
        sql += " AND channel = ?"
        params.append(channel)
    return conn.execute(sql + " ORDER BY channel", params).fetchall()


def _nothing_found(conn, institution_id: str, from_ccy: str, to_ccy: str, as_of: date, model: str) -> str:
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT rate_date FROM fx_posted_rate WHERE institution_id = ? ORDER BY rate_date DESC LIMIT 3",
        (institution_id,))]
    if dates and model != "percent":
        return (f"{institution_id}'s posted quotes are for {', '.join(dates)} only; a posted rate is never "
                f"reused on another day. Add a quote for {as_of} (fees-import --kind quote) or use one of those dates.")
    return (f"No FX fee for {institution_id} covering {from_ccy}->{to_ccy} on {as_of}. Add a sourced, dated row: "
            "python -m portfolio_pulse fees-import data/fees/fee_schedule.csv --kind fee")


def convert(conn, amount: Decimal, from_currency: str, to_currency: str, institution_id: str,
            as_of: date, model: str = "auto", channel: str | None = None) -> ConverterResult:
    from_currency, to_currency = from_currency.upper(), to_currency.upper()
    if model not in MODELS:
        raise ConverterError(f"model must be one of {MODELS}")
    if conn.execute("SELECT 1 FROM institution WHERE institution_id = ?", (institution_id,)).fetchone() is None:
        raise ConverterError(f"Unknown platform '{institution_id}'. Import its fee or quote with fees-import first.")
    source_id = rate_source(conn, institution_id)
    boc = benchmark(conn, as_of, source_id)
    fee = _fee_row(conn, institution_id, from_currency, to_currency, as_of) if model in ("auto", "percent") else None
    quotes = _quotes(conn, institution_id, as_of, channel) if model in ("auto", "posted") else []
    if model == "auto" and fee is not None and quotes:
        raise ConverterError(f"{institution_id} has both a percentage fee and a posted quote for {as_of}; "
                             "choose --model percent or --model posted.")
    warnings: list[str] = []
    try:
        if fee is not None:
            est = fx_cost.estimate_percent(amount, from_currency, to_currency, boc.rate,
                                           _dec(fee["rate_bps"]) / fx_cost.BPS)
            label, ref, ref_id = "estimated", fee, fee["fee_schedule_id"]
            warnings.append("Estimated: the platform applies its fee to its own exchange rate, not the "
                            "Bank of Canada rate, so the real cost can differ.")
        elif quotes:
            if len(quotes) > 1:
                raise ConverterError(f"{institution_id} has several quotes for {as_of} "
                                     f"({', '.join(q['channel'] for q in quotes)}); choose one with --channel.")
            ref = quotes[0]
            est = fx_cost.estimate_posted(amount, from_currency, to_currency, boc.rate,
                                          _dec(ref["customer_buy_rate"]), _dec(ref["customer_sell_rate"]))
            label, ref_id = "reported", ref["fx_posted_rate_id"]
            warnings.append("Bank posted rates can change during the day; this compares one posted quote "
                            "with the Bank of Canada daily reference rate.")
        else:
            raise ConverterError(_nothing_found(conn, institution_id, from_currency, to_currency, as_of, model))
    except ConverterError:
        raise
    except ValueError as exc:                     # e.g. zero amount, unsupported pair
        raise ConverterError(str(exc)) from None

    if boc.status == "carried_forward":
        label = _worst(label, "STALE")
        warnings.append(f"No Bank of Canada rate on {as_of}; using {boc.rate_date} "
                        f"({boc.gap_days} day(s) earlier).")
    if ref["is_demo"] or source_id == DEMO_RATE_SOURCE:
        label = "DEMO"
    if source_id == DEMO_RATE_SOURCE:
        warnings.append("DEMO: the reference rate is a synthetic demo rate, not a Bank of Canada rate.")
    age = (as_of - date.fromisoformat(ref["date_checked"])).days
    if fee is not None and age > FEE_RECHECK_DAYS:
        warnings.append(f"Fee last checked {age} days before {as_of}; re-check {ref['source_url']}.")
    return ConverterResult(est, institution_id, as_of, boc.rate_date, boc.status, label, ref_id,
                           ref["source_url"], ref["date_checked"], bool(ref["is_demo"]), warnings)


def describe(res: ConverterResult) -> list[str]:
    """Plain-English lines for the CLI."""
    e = res.estimate
    tag = " [DEMO]" if res.quality_label == "DEMO" else ""
    return [
        f"FX cost estimate{tag} - educational analytics, not advice",
        f"  Convert {e.amount:,.2f} {e.from_currency} -> {e.to_currency} via {res.institution_id} on {res.as_of_date}",
        f"  Bank of Canada indicative reference rate: {e.benchmark_rate} CAD per USD "
        f"({res.boc_rate_date}, {res.boc_status})",
        f"  At the reference rate:  {e.received_at_benchmark:,.2f} {e.to_currency}",
        f"  Via {res.institution_id}:  {e.received_via_platform:,.2f} {e.to_currency} "
        f"(effective rate {e.platform_rate} CAD per USD)",
        f"  Cost vs reference: {e.cost_cad:,.2f} CAD ({e.cost_bps} bps)  quality: {res.quality_label}",
        f"  Source: {res.source_url} (checked {res.date_checked})",
        *[f"  Note: {w}" for w in res.warnings],
    ]


# --- Export grid (Power BI fact_fx_cost) -----------------------------------------------
# Every row comes from convert(), the same path as the CLI, so Power BI and the CLI agree.
# Dates: posted quotes only on their own rate_date; percent fees on every BoC date from
# effective_from (or date_checked if blank) onward - a fee is never applied to dates before
# you confirmed it. Rows that can't be computed are skipped and counted, never zero-filled.

GRID_AMOUNTS = (Decimal("100"), Decimal("1000"), Decimal("10000"))
DIRECTIONS = (("CAD", "USD"), ("USD", "CAD"))
COST_COLUMNS = ["as_of_date", "institution_id", "model", "channel", "from_currency", "to_currency",
                "amount", "boc_rate", "boc_rate_date", "boc_status", "platform_rate",
                "received_at_benchmark", "received_via_platform", "notional_cad", "cost_cad", "cost_bps",
                "quality_label", "source_ref", "source_url", "date_checked", "is_demo"]


def _boc_dates(conn, start: str, end: str | None, source_id: str = fx_rates.SOURCE_ID) -> list[date]:
    rows = conn.execute(
        "SELECT rate_date FROM fx_rate WHERE base_currency = 'USD' AND quote_currency = 'CAD' "
        "AND source_id = ? AND rate_date >= ? AND (? IS NULL OR rate_date <= ?) ORDER BY rate_date",
        (source_id, start, end, end)).fetchall()
    return [date.fromisoformat(r[0]) for r in rows]


def _grid_row(res: ConverterResult, channel: str | None) -> dict:
    e = res.estimate
    return {
        "as_of_date": res.as_of_date.isoformat(), "institution_id": res.institution_id, "model": e.model,
        "channel": channel, "from_currency": e.from_currency, "to_currency": e.to_currency,
        "amount": float(e.amount), "boc_rate": float(e.benchmark_rate),
        "boc_rate_date": res.boc_rate_date.isoformat(), "boc_status": res.boc_status,
        "platform_rate": float(e.platform_rate), "received_at_benchmark": float(e.received_at_benchmark),
        "received_via_platform": float(e.received_via_platform), "notional_cad": float(e.notional_cad),
        "cost_cad": float(e.cost_cad), "cost_bps": float(e.cost_bps), "quality_label": res.quality_label,
        "source_ref": res.source_ref, "source_url": res.source_url, "date_checked": res.date_checked,
        "is_demo": int(res.is_demo),
    }


def cost_grid(conn, amounts=GRID_AMOUNTS) -> tuple[list[dict], int]:
    """(rows for fact_fx_cost, number of combinations skipped because they can't be computed)."""
    jobs: set[tuple] = set()
    for r in conn.execute("SELECT institution_id, COALESCE(effective_from, date_checked) AS start, effective_to "
                          "FROM fee_schedule WHERE fee_type = 'fx_conversion' AND rate_bps IS NOT NULL").fetchall():
        for d in _boc_dates(conn, r["start"], r["effective_to"], rate_source(conn, r["institution_id"])):
            jobs.add((r["institution_id"], d, "percent", None))
    for r in conn.execute("SELECT DISTINCT institution_id, rate_date, channel FROM fx_posted_rate").fetchall():
        jobs.add((r["institution_id"], date.fromisoformat(r["rate_date"]), "posted", r["channel"]))
    rows, skipped = [], 0
    for inst, day, model, channel in sorted(jobs):
        for frm, to in DIRECTIONS:
            for amount in amounts:
                try:
                    res = convert(conn, amount, frm, to, inst, day, model, channel)
                except ConverterError:
                    skipped += 1
                    continue
                rows.append(_grid_row(res, channel))
    return rows, skipped