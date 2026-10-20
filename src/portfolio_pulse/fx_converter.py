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


def benchmark(conn, as_of: date) -> boc_valet.ResolvedRate:
    """BoC USD/CAD for `as_of` via the Phase 3 rule; stale or missing -> ConverterError."""
    r = fx_rates.rate_for(conn, "USD", "CAD", as_of)
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
    boc = benchmark(conn, as_of)
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
    if ref["is_demo"]:
        label = "DEMO"
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