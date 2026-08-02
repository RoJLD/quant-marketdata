import math

from quant_marketdata.paper_execution import (
    leg_roundtrip_bps,
    simulate_carry_friction,
    slippage_bps,
    walk_book,
)


def test_walk_book_fills_within_first_level():
    vwap, filled, unfilled = walk_book([(100.0, 10.0), (101.0, 5.0)], 4.0)
    assert vwap == 100.0 and filled == 4.0 and unfilled == 0.0


def test_walk_book_walks_multiple_levels_vwap():
    vwap, filled, unfilled = walk_book([(100.0, 2.0), (110.0, 2.0)], 4.0)
    assert filled == 4.0 and unfilled == 0.0
    assert vwap == (100.0 * 2 + 110.0 * 2) / 4  # 105.0


def test_walk_book_shallow_leaves_unfilled():
    vwap, filled, unfilled = walk_book([(100.0, 1.0)], 3.0)
    assert filled == 1.0 and unfilled == 2.0


def test_slippage_buy_and_sell_signs():
    assert slippage_bps(101.0, 100.0, side="buy") == 100.0  # (101-100)/100*1e4
    assert slippage_bps(99.0, 100.0, side="sell") == 100.0  # (100-99)/100*1e4
    assert math.isnan(slippage_bps(101.0, 0.0, side="buy"))


def _book(best_bid, best_ask, qty=1e9):
    # deep book at the touch -> fills at the touch, no walk
    return {"bids": [(best_bid, qty)], "asks": [(best_ask, qty)]}


def test_leg_roundtrip_equals_floor_when_size_fits_touch():
    # mid=100, half-spread = 0.5/100*1e4 = 50 bps; roundtrip slippage = 2*50; +2*fee
    book = _book(99.5, 100.5)
    r = leg_roundtrip_bps(book, notional=100.0, taker_fee_bps=4.0)
    assert r["unfilled_notional"] == 0.0
    assert math.isclose(r["roundtrip_bps"], 2 * 50.0 + 2 * 4.0, rel_tol=1e-9)


def test_leg_roundtrip_exceeds_floor_when_walking_deeper():
    book = {"bids": [(99.5, 0.1), (98.5, 100.0)], "asks": [(100.5, 0.1), (101.5, 100.0)]}
    # notional 100 @ mid 100 -> size 1.0 > 0.1 top -> walks to deeper level (worse)
    r = leg_roundtrip_bps(book, notional=100.0, taker_fee_bps=4.0)
    assert r["roundtrip_bps"] > 2 * 50.0 + 2 * 4.0


def test_simulate_modeled_ge_floor_and_turnover_scales():
    books = {"btc_spot": _book(99.5, 100.5), "btc_perp": _book(99.5, 100.5)}
    notionals = {"btc_spot": 100.0, "btc_perp": 100.0}
    out = simulate_carry_friction(books, notionals, taker_fee_bps=4.0, turnover=0.1)
    assert out["friction_bps_per_day"] >= out["floor_bps_per_day"] - 1e-9
    assert out["erosion_bps"] >= -1e-9
    # touch-fill -> modeled == floor
    assert math.isclose(out["friction_bps_per_day"], out["floor_bps_per_day"], rel_tol=1e-9)
