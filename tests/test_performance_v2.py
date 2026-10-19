from datetime import date

import pytest

from portfolio_pulse import db, performance, synthetic
from portfolio_pulse.performance import Flow, Snapshot

D = date.fromisoformat
NEAR = dict(abs=1e-6)
REAL = lambda account_id: "REAL"


def snap(acct, d, value, ccy="CAD", rate=1.0, status="identity"):
    return Snapshot(acct, D(d), value, ccy, rate, status)


def flow(aid, acct, d, typ, amount, ccy="CAD"):
    return Flow(aid, acct, D(d), typ, amount, ccy)


def test_usd_deposit_day():
    # Thu 10,000 USD @1.4196; Fri deposit 1,000 USD; Fri close 11,100 USD @1.4154
    days = performance.compute_account_days(
        [snap("U", "2026-10-15", 10000, "USD", 1.4196, "reported"),
         snap("U", "2026-10-16", 11100, "USD", 1.4154, "reported")],
        [flow("dep", "U", "2026-10-16", "DEPOSIT", 1000.00, "USD")])
    d = days[1]
    assert (d.net_flow, d.investment_change) == pytest.approx((1000.00, 100.00), **NEAR)
    assert d.return_native == pytest.approx(100 / 11000)                  # +0.9091% in USD
    assert d.start_value_cad == pytest.approx(14196.00, **NEAR)           # 10,000 x 1.4196
    assert d.net_flow_cad == pytest.approx(1419.60, **NEAR)               # 1,000 x 1.4196 (start rate)
    assert d.end_value_cad == pytest.approx(15710.94, **NEAR)             # 11,100 x 1.4154
    assert d.investment_change_cad == pytest.approx(95.34, **NEAR)
    assert d.return_cad == pytest.approx(95.34 / 15615.60)                # +0.6105% in CAD
    assert d.currency_effect == pytest.approx(1.4154 / 1.4196 - 1)        # -0.2959%: USD fell
    assert d.fx_change_cad == pytest.approx(-46.20, **NEAR)               # 11,000 x (1.4154 - 1.4196)
    assert d.market_change_cad == pytest.approx(141.54, **NEAR)           # 100 USD x 1.4154


def test_cad_to_usd_conversion_is_a_loss_not_a_withdrawal():
    # 1,436.95 CAD -> 1,000 USD worth 1,415.40 CAD at BoC: value drops 21.55
    days = performance.compute_account_days(
        [snap("C", "2026-10-15", 10000.00), snap("C", "2026-10-16", 9978.45)],
        [flow("fx", "C", "2026-10-16", "FX_CONVERSION", -1436.95)])
    d = days[1]
    assert d.net_flow == 0
    assert d.investment_change == pytest.approx(-21.55, **NEAR)
    assert d.return_native == pytest.approx(-0.002155)
    assert d.currency_effect == pytest.approx(0.0)


def test_transfer_between_own_accounts_nets_to_zero_for_household():
    snaps = [snap("A", "2026-10-15", 10000), snap("A", "2026-10-16", 8050),
             snap("B", "2026-10-15", 5000), snap("B", "2026-10-16", 7030)]
    flows = [flow("out", "A", "2026-10-16", "TRANSFER_OUT", -2000.00),
             flow("in", "B", "2026-10-16", "TRANSFER_IN", 2000.00)]
    days = performance.compute_account_days(snaps, flows)
    pairs = performance.pair_transfers(flows, REAL)
    assert pairs == {"out": "in", "in": "out"}
    a, b = [d for d in days if d.as_of_date == D("2026-10-16")]
    assert (a.investment_change, a.return_native) == pytest.approx((50, 50 / 8000))
    assert (b.investment_change, b.return_native) == pytest.approx((30, 30 / 7000))
    hh = performance.compute_household_days(days, pairs, REAL)[-1]
    assert hh.external_flow_cad == 0                      # not a contribution
    assert hh.internal_transfer_cad == pytest.approx(2000.00)
    assert hh.investment_change_cad == pytest.approx(80)
    assert hh.return_cad == pytest.approx(80 / 15000)     # capital 8,000 + 7,000
    assert hh.end_value_cad == pytest.approx(15080)


def test_transfer_pairing_rules():
    household = {"A": "REAL", "B": "REAL", "P": "PAPER"}.get
    out = flow("out", "A", "2026-10-16", "TRANSFER_OUT", -2000.00)
    near_misses = [
        flow("far", "B", "2026-10-01", "TRANSFER_IN", 2000.00),          # 15 days apart
        flow("paper", "P", "2026-10-16", "TRANSFER_IN", 2000.00),        # other household
        flow("amt", "B", "2026-10-16", "TRANSFER_IN", 1999.00),          # amount differs
        flow("usd", "B", "2026-10-16", "TRANSFER_IN", 2000.00, "USD"),   # other currency
        flow("self", "A", "2026-10-16", "TRANSFER_IN", 2000.00),         # same account
    ]
    assert performance.pair_transfers([out, *near_misses], household) == {}
    ok = flow("ok", "B", "2026-10-18", "TRANSFER_IN", 2000.00)           # 2 days later
    assert performance.pair_transfers([out, *near_misses, ok], household) == {"out": "ok", "ok": "out"}


def test_unmatched_transfer_stays_external():
    days = performance.compute_account_days(
        [snap("A", "2026-10-15", 10000), snap("A", "2026-10-16", 12000)],
        [flow("in", "A", "2026-10-16", "TRANSFER_IN", 2000.00)])
    hh = performance.compute_household_days(days, {}, REAL)[-1]
    assert hh.external_flow_cad == pytest.approx(2000.00)
    assert hh.internal_transfer_cad == 0
    assert hh.investment_change_cad == pytest.approx(0)


def test_weekend_deposit_uses_friday_rate():
    # Fri 10,000 USD @1.4154; Sat deposit 1,000 USD rolls into Mon; Mon 11,050 USD @1.4097
    days = performance.compute_account_days(
        [snap("U", "2026-10-16", 10000, "USD", 1.4154, "reported"),
         snap("U", "2026-10-19", 11050, "USD", 1.4097, "reported")],
        [flow("sat", "U", "2026-10-17", "DEPOSIT", 1000.00, "USD")])
    d = days[1]
    assert d.return_native == pytest.approx(50 / 11000)                   # +0.4545% in USD
    assert d.net_flow_cad == pytest.approx(1415.40, **NEAR)               # Saturday = Friday's 1.4154
    assert d.end_value_cad == pytest.approx(15577.185, **NEAR)            # 11,050 x 1.4097
    assert d.investment_change_cad == pytest.approx(7.785, **NEAR)
    assert d.return_cad == pytest.approx(7.785 / 15569.40)                # +0.0500% in CAD
    assert d.currency_effect == pytest.approx(1.4097 / 1.4154 - 1)        # -0.4027%
    assert d.fx_change_cad == pytest.approx(-62.70, **NEAR)               # 11,000 x -0.0057
    assert d.market_change_cad == pytest.approx(70.485, **NEAR)           # 50 USD x 1.4097


def test_holiday_status_carries_and_missing_rate_blocks_cad_only():
    days = performance.compute_account_days(
        [snap("U", "2026-10-09", 10000, "USD", 1.4271, "reported"),
         snap("U", "2026-10-12", 10100, "USD", 1.4271, "carried_forward"),   # Thanksgiving
         snap("U", "2026-10-13", 10200, "USD", None, "missing")], [])
    hol, miss = days[1], days[2]
    assert (hol.return_native, hol.return_cad) == pytest.approx((0.01, 0.01))
    assert hol.currency_effect == pytest.approx(0.0)
    assert hol.fx_status == "carried_forward"
    assert miss.return_native == pytest.approx(100 / 10100)               # USD return still known
    assert (miss.return_cad, miss.end_value_cad, miss.fx_status) == (None, None, "missing")
    hh = performance.compute_household_days(days, {}, REAL)[-1]
    assert hh.return_cad is None                                          # no partial household


def test_twr_chains_returns():
    assert performance.twr([0.01, None, -0.02, 0.03]) == pytest.approx(1.01 * 0.98 * 1.03 - 1)  # 1.9494%
    assert performance.twr([]) is None


@pytest.fixture
def demo_conn(tmp_path):
    c = db.connect(tmp_path / "demo.db")
    db.init_db(c)
    synthetic.load_demo(c, 42)
    yield c
    c.close()


def _same(a, b):
    return (a is None and b is None) or (a is not None and b is not None and a == pytest.approx(b, rel=1e-9, abs=1e-9))


def test_demo_v2_matches_v1_row_for_row(demo_conn):
    days, households, pairs = performance.run(demo_conn)
    v2 = {(d.account_id, d.as_of_date.isoformat()): d for d in days}
    v1 = demo_conn.execute("SELECT * FROM v_daily_performance").fetchall()
    assert len(v1) == len(v2) > 0
    for row in v1:
        d = v2[(row["account_id"], row["as_of_date"])]
        assert _same(d.net_flow_cad, row["net_external_flow"]), row["as_of_date"]
        assert _same(d.investment_change_cad, row["investment_change"]), row["as_of_date"]
        assert _same(d.return_cad, row["investment_return"]), row["as_of_date"]
    assert pairs == {}                                     # no own-account transfers in the demo
    v1_flow = sum(r["net_external_flow"] for r in v1)
    assert sum(h.external_flow_cad for h in households) == pytest.approx(v1_flow)


def test_demo_best_and_worst_days_unchanged(demo_conn):
    _, households, _ = performance.run(demo_conn)
    demo = [h for h in households if h.household_id == "DEMO" and h.return_cad is not None]
    best = max(demo, key=lambda h: h.return_cad)
    worst = min(demo, key=lambda h: h.return_cad)
    assert (best.as_of_date.isoformat(), round(best.return_cad, 4)) == ("2026-03-30", 0.0459)
    assert (worst.as_of_date.isoformat(), round(worst.return_cad, 4)) == ("2026-01-16", -0.0218)