"""Unit tests for SnapTrade activity-type mapping (no network, no database)."""
import pytest

from portfolio_pulse.snaptrade_activity import (
    CATEGORY_BY_ACTIVITY_TYPE, REVIEW_TYPES, TYPE_MAP, local_date, map_type, normalize_activity,
)

# The CHECK list on activity.activity_type in sql/migrations/002_multi_broker.sql.
SCHEMA_TYPES = {"BUY", "SELL", "DIVIDEND", "INTEREST", "FEE", "DEPOSIT", "WITHDRAWAL",
                "TRANSFER_IN", "TRANSFER_OUT", "FX_CONVERSION", "TAX", "SPLIT", "OTHER"}


@pytest.mark.parametrize("raw,expected", sorted(TYPE_MAP.items()))
def test_known_types_map_without_review(raw, expected):
    m = map_type(raw)
    assert (m.activity_type, m.category) == expected
    assert not m.needs_review


@pytest.mark.parametrize("amount,units,expected", [
    (50.0, None, "TRANSFER_IN"),
    (-50.0, None, "TRANSFER_OUT"),
    (None, 3, "TRANSFER_IN"),
    (0, -2, "TRANSFER_OUT"),
])
def test_transfer_direction_from_sign(amount, units, expected):
    m = map_type("TRANSFER", amount, units)
    assert m.activity_type == expected
    assert m.category == "external"
    assert not m.needs_review


def test_transfer_without_direction_is_flagged():
    m = map_type("TRANSFER", None, 0)
    assert (m.activity_type, m.category, m.needs_review) == ("OTHER", "review", True)


@pytest.mark.parametrize("raw", sorted(REVIEW_TYPES))
def test_ambiguous_documented_types_are_flagged(raw):
    m = map_type(raw, amount=12.5)
    assert (m.activity_type, m.category, m.needs_review) == ("OTHER", "review", True)


def test_unknown_broker_label_is_flagged_not_guessed():
    m = map_type("WEIRD_BROKER_THING", amount=-10)
    assert (m.activity_type, m.needs_review) == ("OTHER", True)
    assert "WEIRD_BROKER_THING" in m.reason


def test_case_and_whitespace_are_normalized():
    assert map_type("  buy ").activity_type == "BUY"


def test_every_mapped_type_is_allowed_by_schema():
    produced = {t for t, _ in TYPE_MAP.values()} | {"TRANSFER_IN", "TRANSFER_OUT", "OTHER"}
    assert produced <= SCHEMA_TYPES


def test_category_covers_every_schema_type():
    assert set(CATEGORY_BY_ACTIVITY_TYPE) == SCHEMA_TYPES

    

USD = {"code": "USD"}


def _item(**overrides):
    base = {"id": "x1", "type": "BUY", "amount": -100.0, "currency": USD,
            "trade_date": "2026-10-16T00:00:00Z"}
    base.update(overrides)
    return base


def test_buy_units_positive_and_sell_units_negative():
    buy, _, _ = normalize_activity(_item(units=-2), "ACC", "reported")
    sell, _, _ = normalize_activity(_item(type="SELL", amount=100.0, units=2), "ACC", "reported")
    assert buy["quantity"] == 2 and sell["quantity"] == -2


def test_date_only_timestamps_are_not_shifted():
    assert local_date("2026-10-16T00:00:00Z") == "2026-10-16"


def test_intraday_utc_converts_to_toronto_date():
    assert local_date("2026-10-16T02:30:00Z") == "2026-10-15"


def test_missing_amount_is_rejected():
    row, problem, _ = normalize_activity(_item(type="DIVIDEND", amount=None), "ACC", "reported")
    assert row is None and "MISSING amount" in problem


def test_split_without_amount_loads_as_zero_cash():
    row, _, _ = normalize_activity(_item(type="SPLIT", amount=None, units=10), "ACC", "reported")
    assert row["amount"] == 0.0 and row["activity_type"] == "SPLIT"


def test_fee_is_positive_and_fx_rate_not_stored():
    row, _, _ = normalize_activity(_item(fee=-1.5, fx_rate=1.37), "ACC", "reported")
    assert row["fee_amount"] == 1.5 and row["fee_currency"] == "USD"
    assert row["reported_fx_rate"] is None


def test_missing_currency_is_rejected():
    row, problem, _ = normalize_activity(_item(currency=None), "ACC", "reported")
    assert row is None and "currency" in problem