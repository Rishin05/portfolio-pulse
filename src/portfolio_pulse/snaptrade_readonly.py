"""Read-only SnapTrade access for Portfolio Pulse.

Design rules (see Project Memory):
- Personal API key auth: no userId / userSecret, no user registration.
- Read-only: this module exposes data-retrieval methods only. Every SDK call
  goes through _call(), which refuses anything outside ALLOWED_OPERATIONS.
  The SDK's trading and order APIs are never referenced.
- Secrets come from environment variables (.env) and never appear in logs,
  repr() output or error messages.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from datetime import date
from decimal import Decimal
from typing import Any

PAGE_SIZE = 1000  # SnapTrade's documented maximum per activities page

# The only SDK operations this module may call ("group.method").
# Step 6 adds a test that proves nothing outside this set is reachable.
ALLOWED_OPERATIONS: frozenset[str] = frozenset(
    {
        "authentication.login_snap_trade_user",
        "connections.list_brokerage_authorizations",
        "account_information.list_all_user_accounts",
        "account_information.get_user_account_balance",
        "account_information.get_all_account_positions",
        "account_information.get_account_activities",
    }
)

KEY_PAGE = "https://dashboard.snaptrade.com/api-key"


class SnapTradeError(RuntimeError):
    """Base class for friendly SnapTrade errors."""


class SnapTradeConfigError(SnapTradeError):
    """Keys are missing from the environment."""


class SnapTradeAuthError(SnapTradeError):
    """SnapTrade rejected the key (HTTP 401/403)."""


class SnapTradeRateLimitError(SnapTradeError):
    """SnapTrade returned HTTP 429."""

    def __init__(self, message: str, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SnapTradeApiError(SnapTradeError):
    """Any other API or network failure."""


def load_credentials(env: Mapping[str, str] | None = None) -> tuple[str, str]:
    """Return (client_id, consumer_key) or raise a friendly config error."""
    env = os.environ if env is None else env
    client_id = env.get("SNAPTRADE_CLIENT_ID", "").strip()
    consumer_key = env.get("SNAPTRADE_CONSUMER_KEY", "").strip()
    missing = [
        name
        for name, value in (
            ("SNAPTRADE_CLIENT_ID", client_id),
            ("SNAPTRADE_CONSUMER_KEY", consumer_key),
        )
        if not value
    ]
    if missing:
        raise SnapTradeConfigError(
            "SnapTrade keys missing: " + ", ".join(missing)
            + ". Add them to .env (see .env.example). Personal keys: " + KEY_PAGE
        )
    return client_id, consumer_key


def to_plain(value: Any) -> Any:
    """Convert SDK response objects into plain JSON-safe Python values.

    Decimals become strings so raw JSON keeps exact money values.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {str(k): to_plain(v) for k, v in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        return [to_plain(v) for v in value]
    if hasattr(value, "is_none_oapg") and value.is_none_oapg():
        return None
    if hasattr(value, "is_true_oapg"):
        return bool(value.is_true_oapg())
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _retry_after(headers: Any) -> int | None:
    if not headers:
        return None
    try:
        value = headers.get("Retry-After") or headers.get("retry-after")
        return int(value)
    except (AttributeError, TypeError, ValueError):
        return None


def _translate(exc: Exception, operation: str) -> SnapTradeError:
    """Map SDK/network exceptions to friendly errors without leaking bodies."""
    status = getattr(exc, "status", None)
    if status is None:
        return SnapTradeApiError(
            f"{operation} failed ({type(exc).__name__}). "
            "Check your internet connection and try again."
        )
    if status in (401, 403):
        return SnapTradeAuthError(
            f"SnapTrade rejected the API key (HTTP {status}). Check "
            "SNAPTRADE_CLIENT_ID / SNAPTRADE_CONSUMER_KEY in .env, or rotate "
            "the key at " + KEY_PAGE
        )
    if status == 429:
        wait = _retry_after(getattr(exc, "headers", None))
        hint = f" Try again in {wait} seconds." if wait else " Try again in a minute."
        return SnapTradeRateLimitError(
            "SnapTrade rate limit reached (HTTP 429)." + hint, retry_after=wait
        )
    reason = getattr(exc, "reason", "") or "error"
    return SnapTradeApiError(f"{operation} failed with HTTP {status} ({reason}).")


class ReadOnlySnapTrade:
    """Read-only SnapTrade client using a Personal API key."""

    def __init__(self, sdk_client: Any | None = None,
                 env: Mapping[str, str] | None = None) -> None:
        if sdk_client is None:
            client_id, consumer_key = load_credentials(env)
            from snaptrade_client import SnapTrade, SnapTradeAuth

            sdk_client = SnapTrade(
                auth=SnapTradeAuth.personal_api_key(
                    client_id=client_id, consumer_key=consumer_key
                )
            )
        self._sdk = sdk_client

    def __repr__(self) -> str:
        return "ReadOnlySnapTrade(personal_api_key=***)"

    # --- the single choke point for every SDK call -------------------------
    def _call(self, operation: str, **kwargs: Any) -> Any:
        if operation not in ALLOWED_OPERATIONS:
            raise SnapTradeError(f"Blocked non-read-only operation: {operation}")
        group, method = operation.split(".")
        func = getattr(getattr(self._sdk, group), method)
        try:
            response = func(**kwargs)
        except Exception as exc:  # noqa: BLE001 - translated below
            raise _translate(exc, operation) from None  # hide raw body/headers
        return to_plain(response.body)

    # --- public read-only API ----------------------------------------------
    def connection_portal_url(self, broker: str | None = None) -> str:
        """Return a one-time Connection Portal link (expires in ~5 minutes)."""
        kwargs: dict[str, Any] = {"connection_type": "read"}
        if broker:
            kwargs["broker"] = broker
        body = self._call("authentication.login_snap_trade_user", **kwargs)
        url = body.get("redirectURI") if isinstance(body, dict) else None
        if not url:
            raise SnapTradeApiError("SnapTrade did not return a Connection Portal link.")
        return url

    def list_connections(self) -> Any:
        return self._call("connections.list_brokerage_authorizations")

    def list_accounts(self) -> Any:
        return self._call("account_information.list_all_user_accounts")

    def get_balances(self, account_id: str) -> Any:
        return self._call("account_information.get_user_account_balance",
                          account_id=account_id)

    def get_positions(self, account_id: str) -> Any:
        return self._call("account_information.get_all_account_positions",
                          account_id=account_id)

    def get_activities(self, account_id: str, start_date: date | None = None,
                       end_date: date | None = None) -> list[Any]:
        """Return every activity for the account, following pagination."""
        items: list[Any] = []
        offset = 0
        while True:
            kwargs: dict[str, Any] = {"account_id": account_id,
                                      "offset": offset, "limit": PAGE_SIZE}
            if start_date:
                kwargs["start_date"] = start_date.isoformat()
            if end_date:
                kwargs["end_date"] = end_date.isoformat()
            body = self._call("account_information.get_account_activities", **kwargs)
            page = (body.get("data") if isinstance(body, dict) else None) or []
            items.extend(page)
            offset += len(page)
            total = (body.get("pagination") or {}).get("total") if isinstance(body, dict) else None
            if not page or len(page) < PAGE_SIZE:
                break
            if total is not None and offset >= int(Decimal(str(total))):
                break
        return items