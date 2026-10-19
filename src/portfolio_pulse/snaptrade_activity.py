"""Map SnapTrade activity types onto the Phase 1 activity ledger.

Categories (used by the performance engine in Phase 3):
  external - money or assets moving in/out (deposits, withdrawals, transfers): NOT return
  internal - income and costs that ARE part of return (dividends, interest, fees, tax)
  trade    - swaps inside the account (buys, sells, splits, FX): ignored by the return formula
  review   - not classified: stored as OTHER and flagged. We never guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from . import db

MARKET_TZ = ZoneInfo("America/Toronto")
NO_CASH_TYPES = frozenset({"SPLIT"})   # may legitimately have no cash amount

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



def local_date(value: Any) -> str | None:
    """Broker timestamp -> YYYY-MM-DD.

    Exactly midnight means the broker gave a date only: keep it as-is (converting
    2026-10-16T00:00Z to Toronto would wrongly give the 15th). Real intraday times
    convert to the Toronto calendar date.
    """
    if not value:
        return None
    text = str(value)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", text) else None
    if dt.tzinfo is None or dt.time() == time(0, 0):
        return dt.date().isoformat()
    return dt.astimezone(MARKET_TZ).date().isoformat()


def normalize_activity(item: dict, account_id: str, quality_flag: str
                       ) -> tuple[dict | None, str | None, TypeMapping]:
    """One raw SnapTrade activity -> (activity row, None, mapping) or (None, problem, mapping).

    source_id, batch_id and loaded_at_utc are added by the loader.
    """
    raw_type = str(item.get("type") or "").strip()
    mapping = map_type(raw_type, item.get("amount"), item.get("units"))
    provider_id = item.get("id")
    if not provider_id:
        return None, "MISSING provider id", mapping
    if not raw_type:
        return None, "MISSING type", mapping
    code = str((item.get("currency") or {}).get("code") or "").upper()
    if len(code) != 3:
        return None, f"{raw_type}: MISSING fiat currency (may be crypto-denominated)", mapping
    trade_date = local_date(item.get("trade_date"))
    if not trade_date:
        return None, f"{raw_type}: MISSING trade_date", mapping

    amount = _num(item.get("amount"))
    if amount is None:
        if mapping.activity_type in NO_CASH_TYPES:
            amount = 0.0
        else:
            return None, f"{raw_type} on {trade_date}: MISSING amount", mapping

    units = _num(item.get("units"))
    if units is not None and mapping.activity_type == "BUY":
        units = abs(units)
    elif units is not None and mapping.activity_type == "SELL":
        units = -abs(units)

    price = _num(item.get("price"))
    if price is not None and price < 0:
        price = None
    fee = _num(item.get("fee"))
    fee = abs(fee) if fee else None
    symbol = ((item.get("symbol") or {}).get("symbol")
              or (item.get("option_symbol") or {}).get("ticker"))

    row = {
        "activity_id": db.stable_id("snaptrade", account_id, provider_id),
        "account_id": account_id,
        "activity_type": mapping.activity_type,
        "raw_type": raw_type,
        "trade_date": trade_date,
        "settlement_date": local_date(item.get("settlement_date")),
        "symbol": symbol,
        "description": item.get("description"),
        "quantity": units,
        "price": price,
        "price_currency": code if price is not None else None,
        "amount": amount,
        "currency": code,
        "fee_amount": fee,
        "fee_currency": code if fee else None,
        "counter_amount": None,
        "counter_currency": None,
        "reported_fx_rate": None,   # SnapTrade fx_rate direction unverified: raw JSON only
        "provider_activity_id": str(provider_id),
        "quality_flag": quality_flag,
    }
    return row, None, mapping