"""Import a CSV you exported yourself (e.g. a Wealthsimple activity statement).

No export format is assumed. You describe your file's columns in a small JSON
mapping (see config/column_map.example.json), and every row is validated before
anything is written. Rejected rows go to data/rejects/ with a reason.

Two kinds of file are supported:
  * cashflows  - one row per transaction (deposit, withdrawal, dividend, ...)
  * snapshots  - one row per date with an end-of-day account value
Trades (buys/sells) move money between cash and holdings inside the account, so
they do not change total account value and are skipped by the cash-flow import.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from . import db

log = logging.getLogger(__name__)

EXTERNAL_TYPES = {"DEPOSIT", "WITHDRAWAL", "TRANSFER_IN", "TRANSFER_OUT"}
ALL_TYPES = EXTERNAL_TYPES | {"DIVIDEND", "INTEREST", "FEE", "OTHER"}
# Types whose amount must point a particular way once signed.
SIGN_RULES = {"DEPOSIT": 1, "TRANSFER_IN": 1, "DIVIDEND": 1, "INTEREST": 1,
              "WITHDRAWAL": -1, "TRANSFER_OUT": -1, "FEE": -1}
DATE_FORMATS = ["%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y", "%Y-%m-%d %H:%M:%S"]


class ImportConfigError(ValueError):
    """The mapping file doesn't match the CSV. Message is written for a human."""


@dataclass
class ImportResult:
    rows_read: int = 0
    inserted: int = 0
    skipped_duplicates: int = 0
    ignored_types: int = 0
    rejected: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def parse_date(raw: object, fmt: str | None) -> date:
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        raise ValueError("date is missing")
    for f in ([fmt] if fmt else DATE_FORMATS):
        try:
            return datetime.strptime(text, f).date()
        except ValueError:
            continue
    raise ValueError(f"date '{text}' doesn't match {fmt or 'any known format'}")


def parse_amount(raw: object) -> float:
    text = str(raw).strip()
    if not text or text.lower() == "nan":
        raise ValueError("amount is missing")
    negative = text.startswith("(") and text.endswith(")")   # accounting style (12.50)
    cleaned = re.sub(r"[,$\s()]|CAD|USD", "", text, flags=re.I)
    try:
        value = float(cleaned)
    except ValueError:
        raise ValueError(f"amount '{text}' is not a number") from None
    return -abs(value) if negative else value


def load_mapping(path: str | Path) -> dict:
    mapping = json.loads(Path(path).read_text(encoding="utf-8"))
    if mapping.get("kind") not in {"cashflows", "snapshots"}:
        raise ImportConfigError("Mapping must set \"kind\" to \"cashflows\" or \"snapshots\".")
    return mapping


def _required_columns(mapping: dict) -> list[str]:
    cols = mapping["columns"]
    need = ["date", "amount", "type"] if mapping["kind"] == "cashflows" else ["date", "value"]
    missing = [k for k in need if not cols.get(k)]
    if missing:
        raise ImportConfigError(f"Mapping is missing column names for: {', '.join(missing)}")
    return [cols[k] for k in need]


def import_csv(conn, csv_path: str | Path, mapping: dict, *, dry_run: bool = False,
               rejects_dir: str | Path | None = None) -> ImportResult:
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Can't find {csv_path}. Put your export in data/inbox/ and try again.")
    content = csv_path.read_bytes()
    sha = hashlib.sha256(content).hexdigest()
    df = pd.read_csv(csv_path, dtype=str, keep_default_na=False, skip_blank_lines=True,
                     encoding=mapping.get("encoding", "utf-8-sig"))
    df.columns = [c.strip() for c in df.columns]

    needed = _required_columns(mapping)
    absent = [c for c in needed if c not in df.columns]
    if absent:
        raise ImportConfigError(
            f"These mapped columns aren't in the file: {absent}. The file has: {list(df.columns)}. "
            "Edit the 'columns' section of your mapping to match your export's headers.")

    cols = mapping["columns"]
    expected_ccy = mapping.get("expected_currency", "CAD").upper()
    account_id = mapping.get("account_id", "MY-ACCOUNT")
    type_map = {str(k).strip().upper(): v.upper() for k, v in mapping.get("type_map", {}).items()}
    ignore_types = {str(t).strip().upper() for t in mapping.get("ignore_types", [])}
    flip = bool(mapping.get("amounts_are_unsigned", False))
    result = ImportResult(rows_read=len(df))

    clean_rows: list[dict] = []
    seen_keys: set[tuple] = set()
    for idx, raw in df.iterrows():
        line = idx + 2   # +1 header, +1 human counting
        try:
            row_ccy = (raw.get(cols.get("currency", ""), "") or expected_ccy).strip().upper()
            if row_ccy != expected_ccy:
                raise ValueError(f"currency {row_ccy} != expected {expected_ccy} (no FX conversion in the MVP)")
            d = parse_date(raw[cols["date"]], mapping.get("date_format"))
            if d > date.today():
                raise ValueError(f"date {d} is in the future")
            if mapping["kind"] == "snapshots":
                value = parse_amount(raw[cols["value"]])
                if value < 0:
                    raise ValueError("account value can't be negative")
                key = (account_id, d.isoformat())
                rec = {"as_of_date": d.isoformat(), "end_value": round(value, 2)}
            else:
                raw_type = str(raw[cols["type"]]).strip().upper()
                if raw_type in ignore_types:
                    result.ignored_types += 1
                    continue
                ftype = type_map.get(raw_type, raw_type)
                if ftype not in ALL_TYPES:
                    raise ValueError(f"type '{raw_type}' isn't mapped - add it to type_map or ignore_types")
                amount = parse_amount(raw[cols["amount"]])
                if amount == 0:
                    raise ValueError("amount is zero")
                if flip and ftype in SIGN_RULES:
                    amount = abs(amount) * SIGN_RULES[ftype]
                if ftype in SIGN_RULES and (amount > 0) != (SIGN_RULES[ftype] > 0):
                    raise ValueError(f"{ftype} amount {amount} has the wrong sign "
                                     "(set amounts_are_unsigned=true if your export has no signs)")
                desc = str(raw.get(cols.get("description", ""), "")).strip()[:200]
                key = (account_id, d.isoformat(), ftype, round(amount, 2), desc)
                rec = {"flow_date": d.isoformat(), "flow_type": ftype, "amount": round(amount, 2),
                       "is_external": int(ftype in EXTERNAL_TYPES), "description": desc}
            if key in seen_keys:
                raise ValueError("duplicate of an earlier row in this file")
            seen_keys.add(key)
            rec["_key"] = key
            clean_rows.append(rec)
        except (ValueError, KeyError) as exc:
            result.rejected.append({"line": line, "reason": str(exc), **{k: raw[k] for k in df.columns}})

    if mapping["kind"] == "snapshots" and clean_rows:
        result.warnings += find_gaps([r["as_of_date"] for r in clean_rows])

    if rejects_dir and result.rejected:
        out = Path(rejects_dir)
        out.mkdir(parents=True, exist_ok=True)
        pd.DataFrame(result.rejected).to_csv(out / f"{csv_path.stem}_rejects.csv", index=False)

    if dry_run:
        log.info("DRY RUN - nothing written. %s valid, %s rejected, %s ignored.",
                 len(clean_rows), len(result.rejected), result.ignored_types)
        return result

    batch = db.start_batch(conn, "csv_import", csv_path.name, sha)
    db.ensure_account(conn, account_id, mapping.get("account_name", account_id),
                      mapping.get("account_type"), expected_ccy, False)
    now = db.utc_now()
    for rec in clean_rows:
        key = rec.pop("_key")
        if mapping["kind"] == "snapshots":
            row = {"account_id": account_id, **rec, "currency": expected_ccy,
                   "quality_flag": mapping.get("quality_flag", "reported"),
                   "source_id": "csv_import", "batch_id": batch, "loaded_at_utc": now}
            new = db.insert_ignore(conn, "daily_snapshot", row)
        else:
            row = {"cash_flow_id": db.stable_id("csv", *key), "account_id": account_id, **rec,
                   "currency": expected_ccy, "source_id": "csv_import", "batch_id": batch,
                   "loaded_at_utc": now}
            new = db.insert_ignore(conn, "cash_flow", row)
        result.inserted += new
        result.skipped_duplicates += (not new)
    db.finish_batch(conn, batch, read=result.rows_read, inserted=result.inserted,
                    skipped=result.skipped_duplicates, rejected=len(result.rejected))
    conn.commit()
    log.info("Imported %s: %s new, %s already loaded, %s rejected, %s ignored.", csv_path.name,
             result.inserted, result.skipped_duplicates, len(result.rejected), result.ignored_types)
    return result


def find_gaps(iso_dates: list[str]) -> list[str]:
    """Warn about missing weekdays between snapshots (holidays will show up too)."""
    ds = sorted(date.fromisoformat(x) for x in iso_dates)
    warnings = []
    for a, b in zip(ds, ds[1:]):
        missing = [a + timedelta(days=i) for i in range(1, (b - a).days)]
        weekdays = [m for m in missing if m.weekday() < 5]
        if weekdays:
            warnings.append(f"{len(weekdays)} weekday(s) missing between {a} and {b}; "
                            f"the change shown on {b} covers several days.")
    return warnings
