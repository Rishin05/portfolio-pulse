"""SYNTHETIC multi-broker demo data: institutions, connections, a USD account, activity
(trades, dividend, withholding tax, fees, FX conversions), month-end holdings, demo FX
rates and demo fee schedules.

Everything here is DEMO data (source_id='synthetic', quality_flag='demo', is_demo=1).
The brokers are fictional, fee URLs point to example.com, and the FX rates are NOT
Bank of Canada data. Uses its own random generator (seed + 2) so the original TFSA
demo numbers never change.
"""
from __future__ import annotations

import random
from datetime import date

from . import db

USD_ACCOUNT = "DEMO-USD"
FEES_CHECKED = "2026-09-30"          # fixed date so re-runs produce identical rows

INSTITUTIONS = [
    ("demo-maple-broker", "Maple Demo Brokerage (synthetic)", "CA", "https://example.com/demo-maple"),
    ("demo-northern-invest", "Northern Demo Invest (synthetic)", "CA", "https://example.com/demo-northern"),
]
CONNECTIONS = [
    ("demo-maple-conn", "demo-maple-broker", "Maple demo connection (synthetic)"),
    ("demo-northern-conn", "demo-northern-invest", "Northern demo connection (synthetic)"),
]
# (fee_schedule_id, institution_id, fee_type, applies_to, rate_bps, flat_amount, flat_currency, notes)
FEE_SCHEDULES = [
    ("demo-maple-fx", "demo-maple-broker", "fx_conversion", "CAD<->USD", 150.0, None, None,
     "DEMO: 1.50% spread on currency conversion"),
    ("demo-maple-commission", "demo-maple-broker", "commission", "all trades", None, 0.0, "CAD",
     "DEMO: commission-free trading"),
    ("demo-northern-fx", "demo-northern-invest", "fx_conversion", "CAD<->USD", 50.0, None, None,
     "DEMO: 0.50% spread on currency conversion"),
    ("demo-northern-commission", "demo-northern-invest", "commission", "US equities", None, 4.95, "USD",
     "DEMO: flat commission per trade"),
]
NORTHERN_FX_SPREAD = 0.005           # matches demo-northern-fx (50 bps)
COMMISSION_USD = 4.95
RAW_TYPES = {"DEPOSIT": "Deposit", "WITHDRAWAL": "Withdrawal", "TRANSFER_IN": "Transfer in",
             "TRANSFER_OUT": "Transfer out", "DIVIDEND": "Dividend", "FEE": "Fee", "INTEREST": "Interest"}


def _walk(rng: random.Random, dates: list[date], start: float, drift: float, vol: float,
          ndigits: int) -> dict[date, float]:
    """Seeded random walk: one value per date."""
    out, v = {}, start
    for d in dates:
        v *= 1 + rng.gauss(drift, vol)
        out[d] = round(v, ndigits)
    return out


def month_ends(dates: list[date]) -> list[date]:
    """Last business day of each month in the list."""
    return [a for a, b in zip(dates, dates[1:] + [None]) if b is None or b.month != a.month]


def _activity(account: str, d: date, activity_type: str, raw_type: str, amount: float,
              currency: str, **extra) -> dict:
    row = {"account_id": account, "trade_date": d.isoformat(), "activity_type": activity_type,
           "raw_type": raw_type, "amount": round(amount, 2), "currency": currency, **extra}
    row["activity_id"] = db.stable_id("synthetic", account, row["trade_date"], activity_type,
                                      extra.get("symbol"), row["amount"], currency)
    return row


def _holding(account: str, d: date, symbol: str, asset_class: str, quantity: float,
             market_value: float, mv_currency: str, **extra) -> dict:
    return {"account_id": account, "as_of_date": d.isoformat(), "symbol": symbol,
            "asset_class": asset_class, "quantity": quantity, "market_value": market_value,
            "market_value_currency": mv_currency, **extra}


def tfsa_activity(account: str, flows: list[dict]) -> list[dict]:
    """Mirror the original TFSA cash flows into the activity ledger."""
    rows = []
    for f in flows:
        t = f["flow_type"]
        rows.append(_activity(account, date.fromisoformat(f["flow_date"]), t, RAW_TYPES[t],
                              f["amount"], "CAD", symbol="XEQT" if t == "DIVIDEND" else None,
                              description=f"Synthetic {t.lower()}"))
    return rows


def generate_usd_activity(dates: list[date], fx: dict[date, float],
                          prices: dict[str, dict[date, float]]) -> list[dict]:
    """Scripted events for the USD account. fx[d] = demo mid rate, CAD per 1 USD."""
    d, rows = dates, []
    # Deposit CAD, then convert it to USD at the broker's rate (mid + spread).
    rows.append(_activity(USD_ACCOUNT, d[5], "DEPOSIT", "Deposit", 5000.00, "CAD",
                          description="Demo deposit"))
    usd_in = round(5000.00 / (fx[d[6]] * (1 + NORTHERN_FX_SPREAD)), 2)
    rows.append(_activity(USD_ACCOUNT, d[6], "FX_CONVERSION", "FX conversion", -5000.00, "CAD",
                          counter_amount=usd_in, counter_currency="USD",
                          reported_fx_rate=round(5000.00 / usd_in, 6), description="Demo CAD->USD"))
    # Two buys, each with a flat commission included in amount.
    for sym, qty in (("VTI", 6), ("VXUS", 15)):
        p = prices[sym][d[7]]
        rows.append(_activity(USD_ACCOUNT, d[7], "BUY", "Buy", -(qty * p + COMMISSION_USD), "USD",
                              symbol=sym, quantity=qty, price=p, price_currency="USD",
                              fee_amount=COMMISSION_USD, fee_currency="USD"))
    # Dividend with 15% US withholding tax.
    div = round(6 * 0.95, 2)
    rows.append(_activity(USD_ACCOUNT, d[60], "DIVIDEND", "Dividend", div, "USD", symbol="VTI"))
    rows.append(_activity(USD_ACCOUNT, d[60], "TAX", "Non-resident tax withheld",
                          -round(div * 0.15, 2), "USD", symbol="VTI"))
    # Partial sell.
    p = prices["VTI"][d[120]]
    rows.append(_activity(USD_ACCOUNT, d[120], "SELL", "Sell", 2 * p - COMMISSION_USD, "USD",
                          symbol="VTI", quantity=-2, price=p, price_currency="USD",
                          fee_amount=COMMISSION_USD, fee_currency="USD"))
    # Convert USD back to CAD (mid - spread), then withdraw the CAD.
    cad_in = round(1000.00 * fx[d[121]] * (1 - NORTHERN_FX_SPREAD), 2)
    rows.append(_activity(USD_ACCOUNT, d[121], "FX_CONVERSION", "FX conversion", -1000.00, "USD",
                          counter_amount=cad_in, counter_currency="CAD",
                          reported_fx_rate=round(1000.00 / cad_in, 6), description="Demo USD->CAD"))
    rows.append(_activity(USD_ACCOUNT, d[125], "WITHDRAWAL", "Withdrawal", -cad_in, "CAD",
                          description="Demo withdrawal"))
    # A standalone account fee.
    rows.append(_activity(USD_ACCOUNT, d[150], "FEE", "Admin fee", -25.00, "USD",
                          description="Demo account fee"))
    return rows


def usd_holdings(dates: list[date], activity: list[dict],
                 prices: dict[str, dict[date, float]]) -> list[dict]:
    """Month-end holdings rebuilt from the activity ledger, so they always reconcile."""
    snaps: list[dict] = []
    qty: dict[str, float] = {}
    cash: dict[str, float] = {}
    book: dict[str, float] = {}
    events = sorted(activity, key=lambda r: r["trade_date"])
    i = 0
    for me in month_ends(dates):
        while i < len(events) and events[i]["trade_date"] <= me.isoformat():
            e = events[i]
            i += 1
            cash[e["currency"]] = cash.get(e["currency"], 0.0) + e["amount"]
            if e.get("counter_currency"):
                cash[e["counter_currency"]] = cash.get(e["counter_currency"], 0.0) + e["counter_amount"]
            sym = e.get("symbol")
            if e["activity_type"] == "BUY":
                qty[sym] = qty.get(sym, 0) + e["quantity"]
                book[sym] = book.get(sym, 0.0) - e["amount"]            # cost incl. commission
            elif e["activity_type"] == "SELL":
                held = qty[sym]
                book[sym] -= book[sym] * (-e["quantity"] / held)         # average-cost reduction
                qty[sym] = held + e["quantity"]
        for sym, q in sorted(qty.items()):
            if q:
                p = prices[sym][me]
                snaps.append(_holding(USD_ACCOUNT, me, sym, "etf", q, round(q * p, 2), "USD",
                                      price=p, price_currency="USD",
                                      book_value=round(book[sym], 2), book_value_currency="USD"))
        for cur, amt in sorted(cash.items()):
            if abs(amt) >= 0.005:
                snaps.append(_holding(USD_ACCOUNT, me, f"CASH.{cur}", "cash",
                                      round(amt, 2), round(amt, 2), cur))
    return snaps


def tfsa_holdings(account: str, days: list, xeqt: dict[date, float]) -> list[dict]:
    """Month-end TFSA holdings that add up exactly to that day's daily_snapshot end_value."""
    end_value = {d.as_of_date: d.end_value for d in days}
    snaps = []
    for me in month_ends(list(end_value)):
        p = xeqt[me]
        q = round(0.97 * end_value[me] / p, 4)
        mv = round(q * p, 2)
        cash = round(end_value[me] - mv, 2)
        snaps.append(_holding(account, me, "XEQT", "etf", q, mv, "CAD", price=p, price_currency="CAD"))
        snaps.append(_holding(account, me, "CASH.CAD", "cash", cash, cash, "CAD"))
    return snaps


def load_multi_broker(conn, seed: int, days: list, flows: list[dict], batch: str, now: str,
                      tfsa_account: str) -> tuple[int, int, int]:
    """Write the multi-broker demo rows. Returns (read, inserted, skipped). Safe to re-run."""
    rng = random.Random(seed + 2)      # own generator: never disturbs the original TFSA numbers
    dates = [d.as_of_date for d in days]
    fx = _walk(rng, dates, 1.3600, 0.0, 0.003, 4)
    prices = {"VTI": _walk(rng, dates, 300.0, 0.0004, 0.010, 2),
              "VXUS": _walk(rng, dates, 65.0, 0.0003, 0.009, 2)}
    xeqt = _walk(rng, dates, 32.0, 0.0004, 0.008, 2)
    read = ins = 0

    def put(table: str, row: dict) -> None:
        nonlocal read, ins
        read += 1
        ins += db.insert_ignore(conn, table, row)

    for inst_id, name, country, url in INSTITUTIONS:
        put("institution", {"institution_id": inst_id, "display_name": name, "country": country,
                            "website_url": url, "is_demo": 1})
    for conn_id, inst_id, name in CONNECTIONS:
        put("connection", {"connection_id": conn_id, "institution_id": inst_id, "source_id": "synthetic",
                           "display_name": name, "created_at_utc": now, "is_demo": 1})
    db.ensure_account(conn, USD_ACCOUNT, "Demo USD account (synthetic)", "non-registered", "USD", True)
    for account, inst_id, conn_id in ((tfsa_account, "demo-maple-broker", "demo-maple-conn"),
                                      (USD_ACCOUNT, "demo-northern-invest", "demo-northern-conn")):
        # COALESCE: fills the link once, never overwrites one that is already set.
        conn.execute("UPDATE account SET institution_id = COALESCE(institution_id, ?), "
                     "connection_id = COALESCE(connection_id, ?) WHERE account_id = ?",
                     (inst_id, conn_id, account))
    for fid, inst_id, ftype, applies, bps, flat, flat_cur, notes in FEE_SCHEDULES:
        put("fee_schedule", {"fee_schedule_id": fid, "institution_id": inst_id, "fee_type": ftype,
                             "applies_to": applies, "rate_bps": bps, "flat_amount": flat,
                             "flat_currency": flat_cur,
                             "source_url": f"https://example.com/demo-fees/{fid}",
                             "date_checked": FEES_CHECKED, "notes": notes, "is_demo": 1})
    stamp = {"quality_flag": "demo", "source_id": "synthetic", "batch_id": batch, "loaded_at_utc": now}
    for d in dates:
        put("fx_rate", {"rate_date": d.isoformat(), "base_currency": "USD", "quote_currency": "CAD",
                        "rate": fx[d], "series_id": "DEMO-USDCAD", **stamp})
    usd_activity = generate_usd_activity(dates, fx, prices)
    for row in tfsa_activity(tfsa_account, flows) + usd_activity:
        put("activity", {**row, **stamp})
    for row in tfsa_holdings(tfsa_account, days, xeqt) + usd_holdings(dates, usd_activity, prices):
        put("holding_snapshot", {**row, **stamp})
    return read, ins, read - ins