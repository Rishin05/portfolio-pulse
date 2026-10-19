"""Normalize masked raw SnapTrade JSON into the Phase 1 tables.

Phase 2 scope: institution, connection, account, daily total-value snapshot,
cash holdings (CASH.<code>) and activities. Positions follow in Step 5d.
Paper accounts are DEMO: they must never be presented as real money.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from . import db
from .snaptrade_activity import normalize_activity
from .snaptrade_sync import items_of

log = logging.getLogger(__name__)

SOURCE_ID = "snaptrade"
MARKET_TZ = ZoneInfo("America/Toronto")
FOLDER_STAMP = re.compile(r"^\d{8}T\d{6}Z$")


class RawDataError(RuntimeError):
    """The raw folder is missing or incomplete."""


@dataclass
class LoadResult:
    folder: Path
    as_of_date: str
    batch_id: str | None = None
    read: int = 0
    inserted: int = 0
    skipped: int = 0
    rejected: int = 0
    warnings: list[str] = field(default_factory=list)


def latest_raw_folder(raw_root: Path) -> Path:
    folders = sorted(p for p in raw_root.glob("*") if p.is_dir() and FOLDER_STAMP.match(p.name))
    if not folders:
        raise RawDataError(f"No raw SnapTrade pulls in {raw_root}. Run snaptrade-pull first.")
    return folders[-1]


def snapshot_date(folder_name: str) -> str:
    """UTC pull stamp -> Toronto calendar date; Sat/Sun roll back to Friday."""
    pulled = datetime.strptime(folder_name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    local = pulled.astimezone(MARKET_TZ).date()
    while local.weekday() >= 5:
        local -= timedelta(days=1)
    return local.isoformat()


def slugify(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-") or "unknown"


def to_utc_text(value: Any) -> str | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_account_id(snaptrade_account_id: str, institution_slug: str) -> str:
    """Our own stable label. Never a real account number."""
    return f"ST-{institution_slug}-{db.stable_id(SOURCE_ID, 'account', snaptrade_account_id)[:8]}"


def local_connection_id(snaptrade_connection_id: str) -> str:
    return f"ST-{db.stable_id(SOURCE_ID, 'connection', snaptrade_connection_id)[:12]}"


def _money(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _read_json(path: Path) -> Any:
    if not path.exists():
        raise RawDataError(f"Missing raw file: {path.name} in {path.parent.name}")
    return json.loads(path.read_text(encoding="utf-8"))


def _write(res: LoadResult, conn: sqlite3.Connection, table: str, row: dict) -> bool:
    """Insert-or-skip one row and count it. Rule violations are rejected, not hidden."""
    res.read += 1
    try:
        written = db.insert_ignore(conn, table, row)
    except sqlite3.IntegrityError as exc:
        res.rejected += 1
        res.warnings.append(f"Rejected {table} row: {exc}")
        return False
    if written:
        res.inserted += 1
    else:
        res.skipped += 1
    return written


def _reject(res: LoadResult, message: str) -> None:
    res.read += 1
    res.rejected += 1
    res.warnings.append(message)


def _warn_if_rekeyed(res: LoadResult, conn: sqlite3.Connection, row: dict) -> None:
    """Warn when a NEW activity id matches an existing row's natural key.

    SnapTrade documents that activity ids can change if it reprocesses data. We still
    insert (never silently drop) but flag it for review.
    """
    if conn.execute("SELECT 1 FROM activity WHERE activity_id=?",
                    (row["activity_id"],)).fetchone():
        return
    hit = conn.execute(
        "SELECT 1 FROM activity WHERE account_id=? AND trade_date=? AND raw_type=? "
        "AND amount=? AND COALESCE(symbol,'')=COALESCE(?,'') LIMIT 1",
        (row["account_id"], row["trade_date"], row["raw_type"], row["amount"], row["symbol"]),
    ).fetchone()
    if hit:
        res.warnings.append(
            f"{row['account_id']}: possible re-keyed duplicate {row['raw_type']} on "
            f"{row['trade_date']} ({row['amount']} {row['currency']}); inserted, please review.")


def _load_activities(res: LoadResult, conn: sqlite3.Connection, folder: Path,
                     st_acct: str, account_id: str, flag: str, now: str) -> None:
    path = folder / f"activities_{str(st_acct)[:8]}.json"
    if not path.exists():
        res.warnings.append(f"{account_id}: no activities file; activities not recorded.")
        return
    review: dict[str, int] = {}
    fx_count = 0
    for item in items_of(_read_json(path)):
        row, problem, mapping = normalize_activity(item, account_id, flag)
        if row is None:
            _reject(res, f"{account_id}: activity skipped: {problem}")
            continue
        row.update(source_id=SOURCE_ID, batch_id=res.batch_id, loaded_at_utc=now)
        if mapping.needs_review:
            review[row["raw_type"]] = review.get(row["raw_type"], 0) + 1
        if item.get("fx_rate") not in (None, "", 0):
            fx_count += 1
        _warn_if_rekeyed(res, conn, row)
        _write(res, conn, "activity", row)
    for raw, n in sorted(review.items()):
        res.warnings.append(f"{account_id}: {n} activity row(s) of type {raw!r} stored as "
                            "OTHER for review (not guessed).")
    if fx_count:
        res.warnings.append(f"{account_id}: {fx_count} activity row(s) carry fx_rate; kept in "
                            "raw JSON only until its direction is verified (Phase 4).")


def load_folder(conn: sqlite3.Connection, folder: Path, *, dry_run: bool = False) -> LoadResult:
    as_of = snapshot_date(folder.name)
    res = LoadResult(folder=folder, as_of_date=as_of)
    connections = items_of(_read_json(folder / "connections.json"))
    accounts = items_of(_read_json(folder / "accounts.json"))
    now = db.utc_now()

    try:
        res.batch_id = db.start_batch(conn, SOURCE_ID, f"snaptrade raw {folder.name}",
                                      dry_run=dry_run)

        # A connection is DEMO if any of its accounts is a paper account.
        paper_by_conn: dict[str, bool] = {}
        for a in accounts:
            cid = a.get("connection_id")
            if cid:
                paper_by_conn[cid] = paper_by_conn.get(cid, False) or bool(a.get("is_paper"))

        conn_map: dict[str, tuple[str, str]] = {}   # SnapTrade conn id -> (our id, slug)
        for c in connections:
            st_id = c.get("id")
            brokerage = c.get("brokerage") or {}
            if not st_id:
                _reject(res, "A connection without an id was skipped.")
                continue
            slug = slugify(brokerage.get("slug") or brokerage.get("name") or "unknown")
            broker_name = brokerage.get("display_name") or brokerage.get("name") or slug
            is_demo = int(paper_by_conn.get(st_id, False))
            _write(res, conn, "institution", {
                "institution_id": slug, "display_name": broker_name, "country": None,
                "website_url": brokerage.get("url"), "is_demo": is_demo,
            })
            connection_id = local_connection_id(st_id)
            status = "disabled" if c.get("disabled") else "active"
            _write(res, conn, "connection", {
                "connection_id": connection_id, "institution_id": slug,
                "source_id": SOURCE_ID, "display_name": f"{broker_name} via SnapTrade",
                "access_mode": "read_only", "status": status, "provider_ref": st_id,
                "created_at_utc": to_utc_text(c.get("created_date")) or now,
                "last_synced_at_utc": now, "is_demo": is_demo,
            })
            # Status is metadata, not a fact: keep it current on every run.
            conn.execute("UPDATE connection SET status=?, last_synced_at_utc=? "
                         "WHERE connection_id=?", (status, now, connection_id))
            if status == "disabled":
                res.warnings.append(
                    f"{broker_name} connection is disabled in SnapTrade; data is the last "
                    "cached state. Reconnect with: python -m portfolio_pulse snaptrade-connect")
            conn_map[st_id] = (connection_id, slug)

        for a in accounts:
            st_acct, cid = a.get("id"), a.get("connection_id")
            if not st_acct or cid not in conn_map:
                _reject(res, "An account without an id or known connection was skipped.")
                continue
            connection_id, slug = conn_map[cid]
            is_demo = bool(a.get("is_paper"))
            flag = "demo" if is_demo else "reported"
            net_value = a.get("net_value") or {}
            currency = str(net_value.get("currency") or "").upper()
            account_id = local_account_id(st_acct, slug)
            if len(currency) != 3:
                _reject(res, f"{account_id}: MISSING account currency; account skipped.")
                continue

            _write(res, conn, "account", {
                "account_id": account_id,
                "display_name": a.get("display_name") or f"{slug} account",
                "account_type": None, "base_currency": currency, "is_demo": int(is_demo),
                "institution_id": slug, "connection_id": connection_id,
            })
            conn.execute("UPDATE account SET institution_id=?, connection_id=? "
                         "WHERE account_id=?", (slug, connection_id, account_id))

            value = _money(net_value.get("amount"))
            if value is None or value < 0:
                _reject(res, f"{account_id}: MISSING or negative total value; no snapshot for {as_of}.")
            else:
                _write(res, conn, "daily_snapshot", {
                    "account_id": account_id, "as_of_date": as_of, "end_value": value,
                    "currency": currency, "quality_flag": flag, "source_id": SOURCE_ID,
                    "batch_id": res.batch_id, "loaded_at_utc": now,
                })

            balances_path = folder / f"balances_{str(st_acct)[:8]}.json"
            balances = items_of(_read_json(balances_path)) if balances_path.exists() else []
            if not balances_path.exists():
                res.warnings.append(f"{account_id}: no balances file; cash not recorded.")
            for b in balances:
                code = str((b.get("currency") or {}).get("code") or "").upper()
                cash = _money(b.get("cash"))
                if len(code) != 3 or cash is None:
                    _reject(res, f"{account_id}: MISSING cash currency or amount; row skipped.")
                    continue
                _write(res, conn, "holding_snapshot", {
                    "account_id": account_id, "as_of_date": as_of, "symbol": f"CASH.{code}",
                    "asset_class": "cash", "quantity": cash, "price": None,
                    "price_currency": None, "market_value": cash,
                    "market_value_currency": code, "book_value": None,
                    "book_value_currency": None, "quality_flag": flag,
                    "source_id": SOURCE_ID, "batch_id": res.batch_id, "loaded_at_utc": now,
                })

            _load_activities(res, conn, folder, st_acct, account_id, flag, now)

        db.finish_batch(conn, res.batch_id, read=res.read, inserted=res.inserted,
                        skipped=res.skipped, rejected=res.rejected)
    except Exception:
        conn.rollback()
        raise
    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    return res