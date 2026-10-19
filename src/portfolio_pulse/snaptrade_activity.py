"""Map SnapTrade activity types onto the Phase 1 activity ledger.

Categories (used by the performance engine in Phase 3):
  external - money or assets moving in/out (deposits, withdrawals, transfers): NOT return
  internal - income and costs that ARE part of return (dividends, interest, fees, tax)
  trade    - swaps inside the account (buys, sells, splits, FX): ignored by the return formula
  review   - not classified: stored as OTHER and flagged. We never guess.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# SnapTrade type -> (our activity_type, category).
# Source: SnapTrade API reference, getAccountActivities (checked 2026-10-15).
TYPE_MAP: dict[str, tuple[str, str]] = {
    "BUY": ("BUY", "trade"),
    "SELL": ("SELL", "trade"),
    "SPLIT": ("SPLIT", "trade"),
    "DIVIDEND": ("DIVIDEND", "internal"),
    "SUBSTITUTE_DIVIDEND": ("DIVIDEND", "internal"),
    "INTEREST": ("INTEREST", "internal"),
    "FEE": ("FEE", "internal"),
    "TAX": ("TAX", "internal"),   # withholding counts as return (assumption: open question 7)
    "CONTRIBUTION": ("DEPOSIT", "external"),
    "WITHDRAWAL": ("WITHDRAWAL", "external"),
    "EXTERNAL_ASSET_TRANSFER_IN": ("TRANSFER_IN", "external"),
    "EXTERNAL_ASSET_TRANSFER_OUT": ("TRANSFER_OUT", "external"),
}

# Documented SnapTrade types whose effect on return is ambiguous: OTHER + flagged.
REVIEW_TYPES: frozenset[str] = frozenset({
    "REI", "STOCK_DIVIDEND", "OPTIONEXPIRATION", "OPTIONASSIGNMENT",
    "OPTIONEXERCISE", "ADJUSTMENT",
})

# Category for every activity_type allowed by the schema (002_multi_broker.sql).
CATEGORY_BY_ACTIVITY_TYPE: dict[str, str] = {
    "BUY": "trade", "SELL": "trade", "SPLIT": "trade", "FX_CONVERSION": "trade",
    "DIVIDEND": "internal", "INTEREST": "internal", "FEE": "internal", "TAX": "internal",
    "DEPOSIT": "external", "WITHDRAWAL": "external",
    "TRANSFER_IN": "external", "TRANSFER_OUT": "external",
    "OTHER": "review",
}


@dataclass(frozen=True)
class TypeMapping:
    activity_type: str
    category: str
    needs_review: bool
    reason: str | None = None


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_type(raw_type: Any, amount: Any = None, units: Any = None) -> TypeMapping:
    """Classify one SnapTrade activity. Unknown or ambiguous types are flagged, never guessed."""
    key = str(raw_type or "").strip().upper()
    if key in TYPE_MAP:
        activity_type, category = TYPE_MAP[key]
        return TypeMapping(activity_type, category, False)
    if key == "TRANSFER":
        amt = _num(amount)
        signal = amt if amt not in (None, 0.0) else _num(units)
        if signal is not None and signal > 0:
            return TypeMapping("TRANSFER_IN", "external", False)
        if signal is not None and signal < 0:
            return TypeMapping("TRANSFER_OUT", "external", False)
        return TypeMapping("OTHER", "review", True,
                           "TRANSFER with no direction (amount and units empty or zero)")
    if key in REVIEW_TYPES:
        return TypeMapping("OTHER", "review", True,
                           f"{key}: documented SnapTrade type, return treatment not decided yet")
    return TypeMapping("OTHER", "review", True, f"Unknown type {str(raw_type)!r} (broker's own label)")