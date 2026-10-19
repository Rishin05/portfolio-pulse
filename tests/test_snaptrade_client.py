"""Step 6: mocked SnapTrade tests (no network, no real keys).

Covers the read-only client, the guarantee that no trading endpoint is reachable,
dry-run behaviour, masking and friendly errors (missing keys, rate limits,
broken connections).
"""
import re
from pathlib import Path

import pytest

import portfolio_pulse
from portfolio_pulse import cli, db, snaptrade_load, snaptrade_sync
from portfolio_pulse import snaptrade_readonly as sr

READ_GROUPS = {"authentication", "connections", "account_information"}
EXPECTED_READ_OPERATIONS = {
    "authentication.login_snap_trade_user",
    "connections.list_brokerage_authorizations",
    "account_information.list_all_user_accounts",
    "account_information.get_user_account_balance",
    "account_information.get_all_account_positions",
    "account_information.get_account_activities",
}


# --- test doubles -------------------------------------------------------------
class FakeApiException(Exception):
    """Looks like snaptrade_client.ApiException: status, reason, headers, a body."""

    def __init__(self, status, headers=None):
        super().__init__(f"HTTP {status} body: SECRET-BODY")
        self.status, self.reason, self.headers = status, "fake", headers or {}


class FakeResponse:
    def __init__(self, body):
        self.body = body


class FakeSDK:
    """Stands in for snaptrade_client.SnapTrade. Touching any non-read group fails the test."""

    def __init__(self, handlers=None):
        self.calls = []
        self.handlers = handlers or {}

    def __getattr__(self, group):
        if group.startswith("_"):
            raise AttributeError(group)
        if group not in READ_GROUPS:
            raise AssertionError(f"SDK group {group!r} must never be touched")
        return _FakeGroup(self, group)


class _FakeGroup:
    def __init__(self, sdk, group):
        self._sdk, self._group = sdk, group

    def __getattr__(self, method):
        if method.startswith("_"):
            raise AttributeError(method)
        op = f"{self._group}.{method}"

        def call(**kwargs):
            self._sdk.calls.append((op, kwargs))
            handler = self._sdk.handlers.get(op, {})
            result = handler(**kwargs) if callable(handler) else handler
            if isinstance(result, Exception):
                raise result
            return FakeResponse(result)

        return call


def make_client(handlers=None):
    sdk = FakeSDK(handlers)
    return sr.ReadOnlySnapTrade(sdk_client=sdk), sdk


# --- friendly errors ----------------------------------------------------------
def test_missing_keys_names_only_the_missing_variable():
    with pytest.raises(sr.SnapTradeConfigError) as exc:
        sr.ReadOnlySnapTrade(env={"SNAPTRADE_CLIENT_ID": "cid-SECRET"})
    msg = str(exc.value)
    assert "SNAPTRADE_CONSUMER_KEY" in msg
    assert "SNAPTRADE_CLIENT_ID" not in msg and "cid-SECRET" not in msg


def test_401_becomes_auth_error_without_leaking_body():
    c, _ = make_client({"connections.list_brokerage_authorizations": FakeApiException(401)})
    with pytest.raises(sr.SnapTradeAuthError) as exc:
        c.list_connections()
    assert ".env" in str(exc.value) and "SECRET-BODY" not in str(exc.value)
    assert exc.value.__suppress_context__   # the raw exception (with its body) is hidden


def test_429_becomes_rate_limit_error_with_retry_after():
    c, _ = make_client({"account_information.list_all_user_accounts":
                        FakeApiException(429, {"Retry-After": "7"})})
    with pytest.raises(sr.SnapTradeRateLimitError) as exc:
        c.list_accounts()
    assert exc.value.retry_after == 7 and "7 seconds" in str(exc.value)


def test_network_failure_is_friendly():
    c, _ = make_client({"connections.list_brokerage_authorizations": ConnectionError("dns")})
    with pytest.raises(sr.SnapTradeApiError, match="internet"):
        c.list_connections()


def test_other_http_errors_report_status_only():
    c, _ = make_client({"connections.list_brokerage_authorizations": FakeApiException(500)})
    with pytest.raises(sr.SnapTradeApiError, match="HTTP 500") as exc:
        c.list_connections()
    assert "SECRET-BODY" not in str(exc.value)


# --- read-only portal and pagination -------------------------------------------
def test_portal_link_requests_read_only_access():
    c, sdk = make_client({"authentication.login_snap_trade_user":
                          {"redirectURI": "https://example.com/portal"}})
    assert c.connection_portal_url() == "https://example.com/portal"
    assert sdk.calls == [("authentication.login_snap_trade_user", {"connection_type": "read"})]


def test_portal_without_link_is_a_friendly_error():
    c, _ = make_client({"authentication.login_snap_trade_user": {}})
    with pytest.raises(sr.SnapTradeApiError, match="Connection Portal"):
        c.connection_portal_url()


def test_activities_follow_pagination(monkeypatch):
    monkeypatch.setattr(sr, "PAGE_SIZE", 2)
    pages = {0: [{"id": "a"}, {"id": "b"}], 2: [{"id": "c"}]}

    def handler(account_id, offset, limit):
        return {"data": pages[offset], "pagination": {"total": 3}}

    c, sdk = make_client({"account_information.get_account_activities": handler})
    assert [i["id"] for i in c.get_activities("acct-1")] == ["a", "b", "c"]
    assert [kw["offset"] for _, kw in sdk.calls] == [0, 2]


# --- no trading endpoint is reachable ----------------------------------------------
def test_allowlist_is_exactly_the_six_read_operations():
    assert sr.ALLOWED_OPERATIONS == EXPECTED_READ_OPERATIONS
    assert {op.split(".")[0] for op in sr.ALLOWED_OPERATIONS} <= READ_GROUPS


@pytest.mark.parametrize("operation", [
    "trading.place_order",
    "trading.cancel_order",
    "trading.place_force_order",
    "experimental_endpoints.place_simple_order",
    "connections.delete_connection",
])
def test_non_read_operations_are_blocked_before_reaching_the_sdk(operation):
    c, sdk = make_client()
    with pytest.raises(sr.SnapTradeError, match="Blocked"):
        c._call(operation)
    assert sdk.calls == []


def test_public_api_has_no_trading_methods():
    public = {n for n in dir(sr.ReadOnlySnapTrade) if not n.startswith("_")}
    assert public == {"connection_portal_url", "list_connections", "list_accounts",
                      "get_balances", "get_positions", "get_activities"}


def test_every_public_method_stays_inside_read_groups():
    c, sdk = make_client({
        "authentication.login_snap_trade_user": {"redirectURI": "https://example.com"},
        "account_information.get_account_activities": {"data": [], "pagination": {"total": 0}},
    })
    c.connection_portal_url()
    c.list_connections()
    c.list_accounts()
    c.get_balances("a")
    c.get_positions("a")
    c.get_activities("a")
    assert {op for op, _ in sdk.calls} == sr.ALLOWED_OPERATIONS


FORBIDDEN = re.compile(r"\.trading\b|place_\w*order|cancel_order|replace_order|"
                       r"experimental_endpoints|delete_connection|disable_brokerage|"
                       r"refresh_brokerage")


def test_source_code_never_references_trading_or_write_apis():
    pkg = Path(portfolio_pulse.__file__).parent
    for name in ["snaptrade_readonly.py", "snaptrade_sync.py", "snaptrade_load.py",
                 "snaptrade_activity.py", "cli.py"]:
        text = (pkg / name).read_text(encoding="utf-8")
        assert not FORBIDDEN.search(text), f"{name} references a trading/write API"


# --- pull: dry run, masking, fault isolation, broken connection --------------------
ACCOUNTS = {"results": [
    {"id": "acct-A-0001", "connection_id": "conn-1", "masked_account_number": "Q6542138443"},
    {"id": "acct-B-0002", "connection_id": "conn-1", "masked_account_number": "Z0000009999"},
]}
CONNECTIONS = [{"id": "conn-1", "disabled": True, "meta": {"identifier": "rishi@example.com"}}]


def pull_handlers(fail_account=None):
    def balances(account_id):
        if account_id == fail_account:
            return FakeApiException(500)
        return [{"cash": 1.0, "currency": {"code": "USD"}}]

    return {
        "connections.list_brokerage_authorizations": CONNECTIONS,
        "account_information.list_all_user_accounts": ACCOUNTS,
        "account_information.get_user_account_balance": balances,
        "account_information.get_all_account_positions": {"results": []},
        "account_information.get_account_activities": {"data": [], "pagination": {"total": 0}},
    }


def test_pull_dry_run_writes_nothing(tmp_path):
    c, _ = make_client(pull_handlers())
    res = snaptrade_sync.pull_raw(c, tmp_path / "raw", dry_run=True)
    assert res.folder is None and res.accounts == 2
    assert not (tmp_path / "raw").exists()


def test_pull_masks_identifiers_before_writing(tmp_path):
    c, _ = make_client(pull_handlers())
    res = snaptrade_sync.pull_raw(c, tmp_path / "raw")
    saved = " ".join(p.read_text(encoding="utf-8") for p in res.folder.glob("*.json"))
    assert "Q6542138443" not in saved and "****8443" in saved
    assert "rishi@example.com" not in saved


def test_one_failing_account_does_not_stop_the_others(tmp_path):
    c, _ = make_client(pull_handlers(fail_account="acct-A-0001"))
    res = snaptrade_sync.pull_raw(c, tmp_path / "raw")
    assert len(res.errors) == 1 and "acct-A-0" in res.errors[0]
    assert "balances_acct-B-0.json" in res.files
    assert "balances_acct-A-0.json" not in res.files


def test_disabled_connection_is_recorded_and_warned(tmp_path):
    c, _ = make_client(pull_handlers())
    res = snaptrade_sync.pull_raw(c, tmp_path / "raw")
    conn = db.connect(tmp_path / "t.db")
    db.init_db(conn)
    loaded = snaptrade_load.load_folder(conn, res.folder)
    status = conn.execute("SELECT status FROM connection").fetchone()[0]
    conn.close()
    assert status == "disabled"
    assert any("disabled" in w and "snaptrade-connect" in w for w in loaded.warnings)


# --- CLI ------------------------------------------------------------------------
def test_cli_missing_keys_exits_2_with_friendly_message(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv("SNAPTRADE_CLIENT_ID", "")      # load_dotenv won't override these
    monkeypatch.setenv("SNAPTRADE_CONSUMER_KEY", "")
    code = cli.main(["--db", str(tmp_path / "t.db"), "snaptrade-connect"])
    assert code == 2
    assert "SnapTrade keys missing" in caplog.text