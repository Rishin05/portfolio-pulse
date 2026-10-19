from decimal import Decimal as D

import pytest

from portfolio_pulse.fx_cost import estimate_buy_foreign, estimate_sell_foreign

USD = D("100000.00")   # Alpaca Paper DEMO snapshot, 2026-10-16
BOC = D("1.4154")      # BoC FXUSDCAD, 2026-10-16


def test_fund_usd_via_wealthsimple_1_5_pct():
    e = estimate_buy_foreign(USD, BOC, D("0.015"))
    assert e.cad_at_benchmark == D("141540.00")
    assert e.cad_with_fee == D("143695.43")
    assert e.fee_cad == D("2155.43")
    assert e.fee_bps == D("152.28")   # gross-up costs more than 150 bps


def test_sell_usd_back_to_cad_1_5_pct():
    e = estimate_sell_foreign(USD, BOC, D("0.015"))
    assert e.cad_with_fee == D("139416.90")
    assert e.fee_cad == D("2123.10")
    assert e.fee_bps == D("150.00")


def test_all_in_assumption_2_5_pct_scenario():
    e = estimate_buy_foreign(USD, BOC, D("0.025"))
    assert e.cad_with_fee == D("145169.23")
    assert e.fee_cad == D("3629.23")
    assert e.fee_bps == D("256.41")


def test_matches_wealthsimple_published_example():
    # Fee schedule example: 1.3 / (1 - 0.015) = 1.3198; 1.3 * (1 - 0.015) = 1.2805
    buy = estimate_buy_foreign(D("10000"), D("1.3"), D("0.015"))
    sell = estimate_sell_foreign(D("10000"), D("1.3"), D("0.015"))
    assert buy.cad_with_fee == D("13197.97")
    assert sell.cad_with_fee == D("12805.00")


def test_zero_fee_equals_benchmark():
    e = estimate_buy_foreign(USD, BOC, D("0"))
    assert e.cad_with_fee == e.cad_at_benchmark == D("141540.00")
    assert e.fee_cad == D("0.00")


def test_rejects_bad_inputs():
    with pytest.raises(ValueError):
        estimate_buy_foreign(D("-1"), BOC, D("0.015"))
    with pytest.raises(ValueError):
        estimate_buy_foreign(USD, D("0"), D("0.015"))
    with pytest.raises(ValueError):
        estimate_sell_foreign(USD, BOC, D("1"))