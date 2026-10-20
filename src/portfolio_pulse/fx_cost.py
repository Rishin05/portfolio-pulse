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


# --- Phase 4: "I convert this amount" mode --------------------------------------------
# The amount is in the FROM currency ("I convert 1,000 CAD to USD").
#   cost_cad = CAD value, at the BoC rate, of what you'd get - CAD value of what you actually get
#   cost_bps = cost_cad / CAD notional at the BoC rate * 10,000   (a 1.5% fee -> 150.00 both ways)
# The functions above answer a different question ("I need exactly X USD": 1.5% -> 152.28 bps).
# Rates are CAD per 1 USD, like BoC FXUSDCAD. A negative cost means better than the BoC rate.
# The BoC rate is an indicative reference rate, not a rate anyone can trade at.

SUPPORTED_PAIRS = {("CAD", "USD"), ("USD", "CAD")}
BPS = Decimal("10000")
RATE_PLACES = Decimal("0.000001")


@dataclass(frozen=True)
class ConversionEstimate:
    from_currency: str
    to_currency: str
    amount: Decimal                  # in from_currency
    model: str                       # percent | posted_rate
    benchmark_rate: Decimal          # BoC, CAD per 1 USD
    platform_rate: Decimal           # effective CAD per 1 USD applied by the platform
    received_at_benchmark: Decimal   # in to_currency
    received_via_platform: Decimal   # in to_currency
    notional_cad: Decimal
    cost_cad: Decimal
    cost_bps: Decimal


def _check_pair(from_currency: str, to_currency: str) -> None:
    if (from_currency, to_currency) not in SUPPORTED_PAIRS:
        raise ValueError(f"{from_currency}->{to_currency} isn't supported yet (CAD<->USD only)")


def estimate_conversion(amount: Decimal, from_currency: str, to_currency: str,
                        benchmark_rate: Decimal, platform_rate: Decimal, model: str) -> ConversionEstimate:
    """Compare converting `amount` at the platform's rate with the BoC reference rate."""
    _check_pair(from_currency, to_currency)
    if amount <= 0:
        raise ValueError("amount must be greater than 0")
    if benchmark_rate <= 0 or platform_rate <= 0:
        raise ValueError("rates must be greater than 0")
    if from_currency == "CAD":                       # buying USD
        at_benchmark = amount / benchmark_rate
        via_platform = amount / platform_rate
        notional = amount
        cost = amount - via_platform * benchmark_rate
    else:                                            # selling USD
        at_benchmark = amount * benchmark_rate
        via_platform = amount * platform_rate
        notional = at_benchmark
        cost = at_benchmark - via_platform
    return ConversionEstimate(
        from_currency=from_currency, to_currency=to_currency, amount=amount, model=model,
        benchmark_rate=benchmark_rate,
        platform_rate=platform_rate.quantize(RATE_PLACES, rounding=ROUND_HALF_UP),
        received_at_benchmark=_cents(at_benchmark), received_via_platform=_cents(via_platform),
        notional_cad=_cents(notional), cost_cad=_cents(cost),
        cost_bps=(cost / notional * BPS).quantize(CENT, rounding=ROUND_HALF_UP),
    )


def estimate_percent(amount: Decimal, from_currency: str, to_currency: str,
                     benchmark_rate: Decimal, fee_rate: Decimal) -> ConversionEstimate:
    """Percentage fee applied to the rate (Wealthsimple-style): rate/(1-f) buying USD, rate*(1-f) selling."""
    _check_pair(from_currency, to_currency)
    _validate(Decimal(0), benchmark_rate, fee_rate)
    platform = benchmark_rate / (ONE - fee_rate) if from_currency == "CAD" else benchmark_rate * (ONE - fee_rate)
    return estimate_conversion(amount, from_currency, to_currency, benchmark_rate, platform, "percent")


def estimate_posted(amount: Decimal, from_currency: str, to_currency: str, benchmark_rate: Decimal,
                    customer_buy_rate: Decimal | None, customer_sell_rate: Decimal | None) -> ConversionEstimate:
    """Bank posted rate: you buy USD at customer_buy_rate, sell USD at customer_sell_rate."""
    _check_pair(from_currency, to_currency)
    side, rate = (("buy", customer_buy_rate) if from_currency == "CAD" else ("sell", customer_sell_rate))
    if rate is None:
        raise ValueError(f"the posted quote has no customer_{side}_rate, which {from_currency}->{to_currency} needs")
    return estimate_conversion(amount, from_currency, to_currency, benchmark_rate, rate, "posted_rate")