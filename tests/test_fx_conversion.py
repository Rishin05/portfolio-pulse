"""Phase 4 conversion math vs hand-worked examples (test BoC rate 1.3800 is invented)."""
from decimal import Decimal as D

import pytest

from portfolio_pulse import fx_cost

BOC = D("1.3800")


@pytest.mark.parametrize("case, est, at_boc, via, cost, bps", [
    ("A: 150 bps, 1000 CAD->USD", lambda: fx_cost.estimate_percent(D("1000"), "CAD", "USD", BOC, D("0.015")),
     "724.64", "713.77", "15.00", "150.00"),
    ("B: 150 bps, 500 USD->CAD", lambda: fx_cost.estimate_percent(D("500"), "USD", "CAD", BOC, D("0.015")),
     "690.00", "679.65", "10.35", "150.00"),
    ("C: posted buy 1.44, 1000 CAD->USD", lambda: fx_cost.estimate_posted(D("1000"), "CAD", "USD", BOC, D("1.44"), D("1.36")),
     "724.64", "694.44", "41.67", "416.67"),
    ("D: posted sell 1.36, 500 USD->CAD", lambda: fx_cost.estimate_posted(D("500"), "USD", "CAD", BOC, D("1.44"), D("1.36")),
     "690.00", "680.00", "10.00", "144.93"),
], ids=lambda v: v if isinstance(v, str) and ":" in v else "")
def test_hand_worked_examples(case, est, at_boc, via, cost, bps):
    e = est()
    assert (e.received_at_benchmark, e.received_via_platform, e.cost_cad, e.cost_bps) == \
           (D(at_boc), D(via), D(cost), D(bps))


def test_posted_rate_better_than_boc_shows_negative_cost():
    e = fx_cost.estimate_posted(D("500"), "USD", "CAD", BOC, None, D("1.3900"))
    assert (e.cost_cad, e.cost_bps) == (D("-5.00"), D("-72.46"))


@pytest.mark.parametrize("call, message", [
    (lambda: fx_cost.estimate_percent(D("0"), "CAD", "USD", BOC, D("0.015")), "greater than 0"),
    (lambda: fx_cost.estimate_percent(D("100"), "EUR", "CAD", BOC, D("0.015")), "CAD<->USD only"),
    (lambda: fx_cost.estimate_posted(D("100"), "CAD", "USD", BOC, None, D("1.36")), "no customer_buy_rate"),
], ids=["zero amount", "unsupported pair", "quote missing needed side"])
def test_bad_inputs_explain_themselves_never_zero(call, message):
    with pytest.raises(ValueError, match=message):
        call()