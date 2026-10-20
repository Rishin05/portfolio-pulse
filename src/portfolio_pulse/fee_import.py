"""Import sourced, dated FX fees and bank FX quotes from a CSV you fill in yourself.

Two kinds of file:
  * fee    -> fee_schedule    e.g. a percentage FX fee from a platform's published fee page
  * quote  -> fx_posted_rate  e.g. a bank's posted USD/CAD buy/sell rate on one date

Every row needs an https source_url and the date you checked it. This code never fills
in a number: if a value isn't in your CSV, the row is rejected, not guessed.
Educational analytics only - not advice.
"""
from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd

from . import db
from .csv_import import ImportConfigError

log = logging.getLogger(__name__)

KINDS = ("fee", "quote")
COMMON_COLUMNS = ["institution_id", "institution_display_name", "source_url", "date_checked", "is_demo"]
REQUIRED_COLUMNS = {
    "fee": COMMON_COLUMNS + ["fee_type", "applies_to", "rate_bps", "flat_amount", "flat_currency",
                             "effective_from", "effective_to"],
    "quote": COMMON_COLUMNS + ["base_currency", "quote_currency", "rate_date",
                               "customer_buy_rate", "customer_sell_rate"],
}
EXAMPLE_FILE = {"fee": "fee_schedule.example.csv", "quote": "fx_posted_rate.example.csv"}
# Optional columns: notes (both kinds), channel (quotes; default 'standard').

FEE_TYPES = {"fx_conversion", "commission", "account", "other"}
SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,40}$")
CURRENCY = re.compile(r"^[A-Z]{3}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FX_PAIR = re.compile(r"^([A-Z]{3})(->|<->)([A-Z]{3})$")
DEMO_URL_PREFIX = "https://example.com/"
USDCAD_SANE = (Decimal("0.9"), Decimal("2.0"))   # CAD per 1 USD; catches rates typed upside down


@dataclass
class FeeImportResult:
    kind: str
    rows_read: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    refreshed: int = 0              # duplicates whose date_checked moved forward
    institutions_created: int = 0
    rejected: list[dict] = field(default_factory=list)


def _text(raw, col: str) -> str:
    return str(raw.get(col, "")).strip()


def _date(text: str, col: str, *, required: bool = True) -> date | None:
    if not text:
        if required:
            raise ValueError(f"{col} is missing")
        return None
    if not ISO_DATE.match(text):
        raise ValueError(f"{col} '{text}' must be YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise ValueError(f"{col} '{text}' is not a real date") from None


def _number(text: str, col: str, hint: str = "") -> Decimal | None:
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{col} '{text}' is not a number{hint}") from None
    if not value.is_finite():
        raise ValueError(f"{col} '{text}' is not a finite number")
    return value


def _key_part(value: Decimal | None) -> str:
    """Canonical text for IDs, so '150', '150.0' and '150.00' give the same ID."""
    return "" if value is None else format(value.normalize(), "f")


def _validate_common(raw) -> dict:
    inst = _text(raw, "institution_id")
    if not SLUG.match(inst):
        raise ValueError(f"institution_id '{inst}' must be a lower-case slug like 'wealthsimple'")
    name = _text(raw, "institution_display_name")
    if not name:
        raise ValueError("institution_display_name is missing")
    url = _text(raw, "source_url")
    if not url:
        raise ValueError("source_url is missing (where did this number come from?)")
    if not url.startswith("https://"):
        raise ValueError(f"source_url must start with https:// (got '{url}')")
    demo = _text(raw, "is_demo")
    if demo not in {"0", "1"}:
        raise ValueError("is_demo must be 0 (real) or 1 (demo)")
    if demo == "1" and not url.startswith(DEMO_URL_PREFIX):
        raise ValueError("demo rows must use an https://example.com/ source_url")
    if demo == "0" and "example.com" in url:
        raise ValueError("real rows (is_demo=0) can't use an example.com source_url")
    checked = _date(_text(raw, "date_checked"), "date_checked")
    if checked > date.today():
        raise ValueError(f"date_checked {checked} is in the future")
    return {"institution_id": inst, "display_name": name, "source_url": url, "date_checked": checked,
            "is_demo": int(demo), "notes": _text(raw, "notes")[:300] or None}


def _validate_fee(raw, common: dict) -> tuple[tuple, dict]:
    fee_type = _text(raw, "fee_type").lower()
    if fee_type not in FEE_TYPES:
        raise ValueError(f"fee_type '{fee_type}' must be one of {sorted(FEE_TYPES)}")
    applies_to = _text(raw, "applies_to") or None
    bps = _number(_text(raw, "rate_bps"), "rate_bps", " (use basis points: 150 = 1.50%)")
    flat = _number(_text(raw, "flat_amount"), "flat_amount")
    flat_ccy = _text(raw, "flat_currency").upper() or None
    if bps is None and flat is None:
        raise ValueError("give rate_bps or flat_amount (never left blank or guessed)")
    if bps is not None and not (0 <= bps < 10000):
        raise ValueError(f"rate_bps {bps} must be between 0 and 9999 (150 = 1.50%)")
    if flat is not None and flat < 0:
        raise ValueError("flat_amount can't be negative")
    if (flat is None) != (flat_ccy is None):
        raise ValueError("flat_amount and flat_currency go together")
    if flat_ccy and not CURRENCY.match(flat_ccy):
        raise ValueError(f"flat_currency '{flat_ccy}' must be a 3-letter code like CAD")
    if fee_type == "fx_conversion":
        m = FX_PAIR.match(applies_to or "")
        if not m or m.group(1) == m.group(3):
            raise ValueError("fx_conversion needs applies_to like CAD->USD, USD->CAD or CAD<->USD")
        if bps is None:
            raise ValueError("fx_conversion needs rate_bps (the percentage fee)")
    eff_from = _date(_text(raw, "effective_from"), "effective_from", required=False)
    eff_to = _date(_text(raw, "effective_to"), "effective_to", required=False)
    if eff_from and eff_to and eff_to < eff_from:
        raise ValueError("effective_to is before effective_from")
    key = (common["institution_id"], fee_type, applies_to or "",
           eff_from.isoformat() if eff_from else "", _key_part(bps), _key_part(flat), flat_ccy or "")
    row = {
        "fee_schedule_id": db.stable_id("fee", *key), "institution_id": common["institution_id"],
        "fee_type": fee_type, "applies_to": applies_to,
        "rate_bps": float(bps) if bps is not None else None,
        "flat_amount": float(flat) if flat is not None else None, "flat_currency": flat_ccy,
        "effective_from": eff_from.isoformat() if eff_from else None,
        "effective_to": eff_to.isoformat() if eff_to else None,
        "source_url": common["source_url"], "date_checked": common["date_checked"].isoformat(),
        "notes": common["notes"], "is_demo": common["is_demo"],
    }
    return key, row


def _validate_quote(raw, common: dict) -> tuple[tuple, dict]:
    base = _text(raw, "base_currency").upper()
    quote = _text(raw, "quote_currency").upper()
    for col, ccy in (("base_currency", base), ("quote_currency", quote)):
        if not CURRENCY.match(ccy):
            raise ValueError(f"{col} '{ccy}' must be a 3-letter code like USD")
    if base == quote:
        raise ValueError(f"{base}/{quote} is not a currency pair")
    rate_date = _date(_text(raw, "rate_date"), "rate_date")
    if common["date_checked"] < rate_date:
        raise ValueError("date_checked is before rate_date")
    channel = _text(raw, "channel") or "standard"
    buy = _number(_text(raw, "customer_buy_rate"), "customer_buy_rate")
    sell = _number(_text(raw, "customer_sell_rate"), "customer_sell_rate")
    if buy is None and sell is None:
        raise ValueError("give customer_buy_rate and/or customer_sell_rate")
    for col, v in (("customer_buy_rate", buy), ("customer_sell_rate", sell)):
        if v is not None and v <= 0:
            raise ValueError(f"{col} must be > 0")
    if buy is not None and sell is not None and buy < sell:
        raise ValueError("customer_buy_rate is below customer_sell_rate: a bank never pays more than it charges")
    if (base, quote) == ("USD", "CAD"):
        lo, hi = USDCAD_SANE
        for col, v in (("customer_buy_rate", buy), ("customer_sell_rate", sell)):
            if v is not None and not (lo <= v <= hi):
                raise ValueError(f"{col} {v} looks wrong for USD/CAD: enter CAD per 1 USD "
                                 "(Bank of Canada style), not USD per 1 CAD")
    key = (common["institution_id"], base, quote, rate_date.isoformat(), channel)
    row = {
        "fx_posted_rate_id": db.stable_id("quote", *key), "institution_id": common["institution_id"],
        "base_currency": base, "quote_currency": quote, "rate_date": rate_date.isoformat(),
        "channel": channel,
        "customer_buy_rate": float(buy) if buy is not None else None,
        "customer_sell_rate": float(sell) if sell is not None else None,
        "source_url": common["source_url"], "date_checked": common["date_checked"].isoformat(),
        "notes": common["notes"], "is_demo": common["is_demo"],
    }
    return key, row


def import_fee_csv(conn, csv_path: str | Path, kind: str, *, dry_run: bool = False,
                   rejects_dir: str | Path | None = None) -> FeeImportResult:
    if kind not in KINDS:
        raise ImportConfigError(f"--kind must be one of {KINDS}, got {kind!r}")
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Can't find {csv_path}. Copy examples/fees/{EXAMPLE_FILE[kind]} "
                                "to data/fees/ and fill it in from the live fee page.")
    sha = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, skip_blank_lines=True, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    missing = [c for c in REQUIRED_COLUMNS[kind] if c not in df.columns]
    if missing:
        raise ImportConfigError(f"{csv_path.name} is missing column(s) {missing} for --kind {kind}. "
                                f"Start from examples/fees/{EXAMPLE_FILE[kind]}.")

    validate = _validate_fee if kind == "fee" else _validate_quote
    result = FeeImportResult(kind=kind, rows_read=len(df))
    clean: list[tuple[dict, dict]] = []
    seen: set[tuple] = set()
    for idx, raw in df.iterrows():
        line = idx + 2   # +1 header, +1 human counting
        try:
            common = _validate_common(raw)
            key, row = validate(raw, common)
            if key in seen:
                raise ValueError("duplicate of an earlier row in this file")
            seen.add(key)
            clean.append((common, row))
        except ValueError as exc:
            result.rejected.append({"line": line, "reason": str(exc), **{c: raw[c] for c in df.columns}})

    if rejects_dir and result.rejected:
        out = Path(rejects_dir)
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result.rejected).to_csv(out / f"{csv_path.stem}_rejects.csv", index=False)

    if dry_run:
        log.info("DRY RUN - nothing written. %s valid %s row(s), %s rejected.",
                 len(clean), kind, len(result.rejected))
        return result

    table, id_col = (("fee_schedule", "fee_schedule_id") if kind == "fee"
                     else ("fx_posted_rate", "fx_posted_rate_id"))
    batch = db.start_batch(conn, "csv_import", csv_path.name, sha)
    for common, row in clean:
        result.institutions_created += db.insert_ignore(conn, "institution", {
            "institution_id": common["institution_id"], "display_name": common["display_name"],
            "country": None, "website_url": None, "is_demo": common["is_demo"]})
        if db.insert_ignore(conn, table, row):
            result.inserted += 1
        else:
            result.skipped_duplicates += 1
            cur = conn.execute(f"UPDATE {table} SET date_checked = ? WHERE {id_col} = ? AND date_checked < ?",
                               (row["date_checked"], row[id_col], row["date_checked"]))
            result.refreshed += cur.rowcount
    db.finish_batch(conn, batch, read=result.rows_read, inserted=result.inserted,
                    skipped=result.skipped_duplicates, rejected=len(result.rejected))
    conn.commit()
    log.info("Imported %s (%s): %s new, %s already loaded (%s re-checked), %s rejected, %s new institution(s).",
             csv_path.name, kind, result.inserted, result.skipped_duplicates, result.refreshed,
             len(result.rejected), result.institutions_created)
    return result