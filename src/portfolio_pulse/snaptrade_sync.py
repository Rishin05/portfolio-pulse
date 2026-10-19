"""Pull raw SnapTrade data (read-only) and save it as masked JSON.

Raw files land in data/raw/snaptrade/<UTC timestamp>/ (git-ignored). Account
numbers are masked before anything is written. Normalizing into the Phase 1
tables happens in a separate step, so it can be re-run from raw files.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .snaptrade_readonly import ReadOnlySnapTrade, SnapTradeError

log = logging.getLogger(__name__)

# Keys whose values identify a real brokerage account. Masked everywhere.
SENSITIVE_KEYS = frozenset({"number", "account_number", "institution_account_id",
                            "masked_account_number", "identifier"})


def mask_identifier(value: Any) -> Any:
    """'Q6542138443' -> '****8443'. Short values are fully masked."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return text
    return "****" + (text[-4:] if len(text) > 4 else "")


def mask_sensitive(value: Any) -> Any:
    """Return a copy with every SENSITIVE_KEYS value masked, at any depth."""
    if isinstance(value, dict):
        return {
            k: (mask_identifier(v) if k in SENSITIVE_KEYS and isinstance(v, (str, int)) else mask_sensitive(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [mask_sensitive(v) for v in value]
    return value


def shape_of(value: Any) -> Any:
    """Keys and value types only (no values), safe to share when debugging."""
    if isinstance(value, dict):
        return {k: shape_of(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [shape_of(value[0])] if value else []
    return type(value).__name__


def items_of(body: Any) -> list[dict]:
    """Accept a bare list or a {results|data|accounts: [...]} wrapper."""
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if isinstance(body, dict):
        for key in ("results", "data", "accounts"):
            if isinstance(body.get(key), list):
                return [x for x in body[key] if isinstance(x, dict)]
    return []


@dataclass
class PullResult:
    folder: Path | None = None
    accounts: int = 0
    positions: int = 0
    activities: int = 0
    files: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    shapes: dict[str, Any] = field(default_factory=dict)


def pull_raw(client: ReadOnlySnapTrade, raw_root: Path, *, dry_run: bool = False,
             now: datetime | None = None) -> PullResult:
    """Fetch connections, accounts, balances, positions and activities."""
    result = PullResult()
    payloads: dict[str, Any] = {}

    payloads["connections"] = client.list_connections()
    payloads["accounts"] = client.list_accounts()
    result.shapes["connections"] = shape_of(payloads["connections"])
    result.shapes["accounts"] = shape_of(payloads["accounts"])

    accounts = items_of(payloads["accounts"])
    result.accounts = len(accounts)
    for acct in accounts:
        acct_id = acct.get("id")
        if not acct_id:
            result.errors.append("An account without an id was skipped.")
            continue
        short = str(acct_id)[:8]   # opaque SnapTrade UUID prefix, not an account number
        try:
            balances = client.get_balances(acct_id)
            positions = client.get_positions(acct_id)
            activities = client.get_activities(acct_id)
        except SnapTradeError as exc:   # one failing account must not stop the rest
            result.errors.append(f"Account {short}: {exc}")
            log.warning("SnapTrade account %s skipped: %s", short, exc)
            continue
        payloads[f"balances_{short}"] = balances
        payloads[f"positions_{short}"] = positions
        payloads[f"activities_{short}"] = activities
        result.positions += len(items_of(positions))
        result.activities += len(activities)
        result.shapes.setdefault("balances", shape_of(balances))
        result.shapes.setdefault("positions", shape_of(positions))
        result.shapes.setdefault("activities", shape_of(activities))

    if dry_run:
        return result

    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    folder = raw_root / stamp
    folder.mkdir(parents=True, exist_ok=False)
    for name, body in payloads.items():
        path = folder / f"{name}.json"
        path.write_text(json.dumps(mask_sensitive(body), indent=2, sort_keys=True),
                        encoding="utf-8")
        result.files.append(path.name)
    result.folder = folder
    return result