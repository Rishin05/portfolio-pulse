"""FX cost estimates: what a conversion costs versus the BoC benchmark.

Educational estimates only, never advice. Brokers apply their fee to their
own corporate rate, which can differ from the BoC rate, so real all-in
costs can be higher than these figures.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ONE = Decimal("1")


@dataclass(frozen=True)
class FxCostEstimate:
    direction: str            # buy_foreign (CAD -> USD) | sell_foreign (USD -> CAD)
    foreign_amount: Decimal   # e.g. USD
    benchmark_rate: Decimal   # CAD per 1 foreign unit (BoC)
    fee_rate: Decimal         # 0.015 = 1.5%
    cad_at_benchmark: Decimal
    cad_with_fee: Decimal     # CAD paid (buy) or received (sell)
    fee_cad: Decimal          # always >= 0
    fee_bps: Decimal          # fee_cad / cad_at_benchmark * 10,000


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _validate(foreign_amount: Decimal, benchmark_rate: Decimal, fee_rate: Decimal) -> None:
    if foreign_amount < 0:
        raise ValueError("foreign_amount must be >= 0")
    if benchmark_rate <= 0:
        raise ValueError("benchmark_rate must be > 0")
    if not (Decimal(0) <= fee_rate < ONE):
        raise ValueError("fee_rate must be in [0, 1)")


def _build(direction, foreign_amount, benchmark_rate, fee_rate, at_benchmark, with_fee, fee):
    bps = fee / at_benchmark * 10000 if at_benchmark else Decimal(0)
    return FxCostEstimate(
        direction=direction,
        foreign_amount=foreign_amount,
        benchmark_rate=benchmark_rate,
        fee_rate=fee_rate,
        cad_at_benchmark=_cents(at_benchmark),
        cad_with_fee=_cents(with_fee),
        fee_cad=_cents(fee),
        fee_bps=bps.quantize(CENT, rounding=ROUND_HALF_UP),
    )


def estimate_buy_foreign(
    foreign_amount: Decimal, benchmark_rate: Decimal, fee_rate: Decimal
) -> FxCostEstimate:
    """CAD needed to obtain `foreign_amount` (CAD -> USD): rate / (1 - fee)."""
    _validate(foreign_amount, benchmark_rate, fee_rate)
    at_benchmark = foreign_amount * benchmark_rate
    with_fee = at_benchmark / (ONE - fee_rate)
    return _build("buy_foreign", foreign_amount, benchmark_rate, fee_rate,
                  at_benchmark, with_fee, with_fee - at_benchmark)


def estimate_sell_foreign(
    foreign_amount: Decimal, benchmark_rate: Decimal, fee_rate: Decimal
) -> FxCostEstimate:
    """CAD received for `foreign_amount` (USD -> CAD): rate * (1 - fee)."""
    _validate(foreign_amount, benchmark_rate, fee_rate)
    at_benchmark = foreign_amount * benchmark_rate
    with_fee = at_benchmark * (ONE - fee_rate)
    return _build("sell_foreign", foreign_amount, benchmark_rate, fee_rate,
                  at_benchmark, with_fee, at_benchmark - with_fee)