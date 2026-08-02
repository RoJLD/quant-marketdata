import math

import pytest

from quant_marketdata.market_impact import (
    book_impact_curve,
    breakeven_size,
    feasible_operating_point,
    impact_beyond_book,
    leg_roundtrip_bps_impact,
    max_turnover_at_size,
)

# mid = 100. Asks 101/102/103 with sizes 10/20/30; bids symmetric. Displayed depth = 60 units.
BOOK = {
    "bids": [(99.0, 10.0), (98.0, 20.0), (97.0, 30.0)],
    "asks": [(101.0, 10.0), (102.0, 20.0), (103.0, 30.0)],
}


def test_curve_cumulative_points_and_boundary():
    curve = book_impact_curve(BOOK, "buy")
    assert len(curve) == 3
    # notionals cumulative at mid=100: 10*100, 30*100, 60*100
    assert [round(n, 2) for n, _ in curve] == [1000.0, 3000.0, 6000.0]
    # impact strictly increasing (VWAP walks deeper) and first level = full spread/2 = 100 bps
    imps = [imp for _, imp in curve]
    assert imps[0] == pytest.approx(100.0, abs=1e-6)
    assert imps[0] < imps[1] < imps[2]


def test_within_book_is_exact_vwap_walk():
    # at a cumulative-level notional the beyond-book value equals the curve point (no model)
    curve = book_impact_curve(BOOK, "buy")
    n_mid, imp_mid = curve[1]  # 3000 USD -> 10@101 + 20@102 VWAP slippage
    assert impact_beyond_book(BOOK, n_mid, "buy") == pytest.approx(imp_mid, abs=1e-9)


def test_continuity_at_boundary():
    curve = book_impact_curve(BOOK, "buy")
    n_disp, imp_disp = curve[-1]
    assert impact_beyond_book(BOOK, n_disp, "buy") == pytest.approx(imp_disp, abs=1e-9)


def test_beyond_book_grows_like_sqrt_and_slope_matches():
    curve = book_impact_curve(BOOK, "buy")
    n_disp, imp_disp = curve[-1]
    n_prev, imp_prev = curve[-2]
    slope = (imp_disp - imp_prev) / (n_disp - n_prev)
    k = 2.0 * n_disp * slope
    # analytic value at 2*N_disp
    expected = imp_disp + k * (math.sqrt(2.0) - 1.0)
    assert impact_beyond_book(BOOK, 2.0 * n_disp, "buy") == pytest.approx(expected, rel=1e-9)
    # boundary slope match: finite-diff of the model just past N_disp ~ observed last-segment slope
    eps = n_disp * 1e-4
    fd = (impact_beyond_book(BOOK, n_disp + eps, "buy") - imp_disp) / eps
    assert fd == pytest.approx(slope, rel=1e-3)


def test_beyond_book_monotone_increasing():
    ns = [6000.0, 9000.0, 12000.0, 24000.0]
    vals = [impact_beyond_book(BOOK, n, "buy") for n in ns]
    assert all(b > a for a, b in zip(vals, vals[1:], strict=False))


def test_sell_side_uses_bids():
    # symmetric book -> sell impact equals buy impact at the same notional
    assert impact_beyond_book(BOOK, 3000.0, "sell") == pytest.approx(
        impact_beyond_book(BOOK, 3000.0, "buy"), abs=1e-9
    )


def test_notional_guard_and_degenerate_book():
    with pytest.raises(ValueError):
        impact_beyond_book(BOOK, 0.0, "buy")
    single = {"bids": [(99.0, 5.0)], "asks": [(101.0, 5.0)]}  # one level -> no slope to calibrate
    # beyond its 500 USD depth, falls back to the half-spread floor (100 bps) without raising
    assert impact_beyond_book(single, 5000.0, "buy") == pytest.approx(100.0, abs=1e-6)


# TIGHT book: mid 100, half-spread 0.5 bp, levels 0.5 bp apart, sizes 1000/2000/3000.
TIGHT = {
    "bids": [(99.995, 1000.0), (99.99, 2000.0), (99.985, 3000.0)],
    "asks": [(100.005, 1000.0), (100.01, 2000.0), (100.015, 3000.0)],
}
# DEEP: same shape, 10x the size at each level -> larger capacity.
DEEP = {
    "bids": [(99.995, 10000.0), (99.99, 20000.0), (99.985, 30000.0)],
    "asks": [(100.005, 10000.0), (100.01, 20000.0), (100.015, 30000.0)],
}


def test_leg_roundtrip_impact_sums_legs_and_fees():
    out = leg_roundtrip_bps_impact(BOOK, 3000.0, taker_fee_bps=1.0)
    assert out["roundtrip_bps"] == pytest.approx(
        out["buy_impact_bps"] + out["sell_impact_bps"] + 2.0, abs=1e-9
    )


def test_breakeven_friction_hits_tripwire():
    # turnover 0.1, fee 0, tripwire 0.5: floor = 0.1*2legs*(2*0.5bp) = 0.2 bp < 0.5 -> size binds.
    books = {"spot": TIGHT, "perp": TIGHT}
    out = breakeven_size(books, taker_fee_bps=0.0, turnover=0.1, tripwire_bps=0.5)
    n = out["breakeven_notional_usd"]
    assert 0.0 < n < float("inf")
    assert out["friction_at_breakeven_bps"] == pytest.approx(0.5, abs=1e-3)

    def friction(gross):
        per = gross / 2
        return 0.1 * sum(
            leg_roundtrip_bps_impact(b, per, 0.0)["roundtrip_bps"] for b in books.values()
        )

    assert friction(n * 1.05) > 0.5
    assert friction(n * 0.95) < 0.5


def test_deeper_book_has_larger_breakeven():
    shallow = breakeven_size({"spot": TIGHT, "perp": TIGHT}, 0.0, turnover=0.1)["breakeven_notional_usd"]
    deep = breakeven_size({"spot": DEEP, "perp": DEEP}, 0.0, turnover=0.1)["breakeven_notional_usd"]
    assert deep > shallow


def test_binding_leg_is_the_shallower_book():
    out = breakeven_size({"spot": DEEP, "perp": TIGHT}, 0.0, turnover=0.1)
    assert out["binding_leg"] == "perp"  # TIGHT is 10x shallower than DEEP


def test_breakeven_infinite_when_no_cost():
    out = breakeven_size({"spot": TIGHT, "perp": TIGHT}, 0.0, turnover=0.0)
    assert out["breakeven_notional_usd"] == float("inf")


def test_breakeven_floor_bound_returns_zero():
    # turnover 1.0 on TIGHT: floor = 1.0*2legs*(2*0.5bp) = 2.0 bp >= 0.5 tripwire -> no size headroom.
    out = breakeven_size({"spot": TIGHT, "perp": TIGHT}, 0.0, turnover=1.0, tripwire_bps=0.5)
    assert out["breakeven_notional_usd"] == 0.0
    assert out["floor_bound"] is True


def test_breakeven_turnover_guard():
    with pytest.raises(ValueError):
        breakeven_size({"spot": TIGHT}, 0.0, turnover=-1.0)


def test_max_turnover_decreasing_in_size():
    books = {"spot": TIGHT, "perp": TIGHT}
    small = max_turnover_at_size(books, 0.0, gross_notional=1_000.0)["max_turnover"]
    big = max_turnover_at_size(books, 0.0, gross_notional=1_000_000.0)["max_turnover"]
    assert 0.0 < big < small < float("inf")


def test_max_turnover_is_reciprocal_of_breakeven_size():
    books = {"spot": TIGHT, "perp": TIGHT}
    turnover0 = 0.1
    n_star = breakeven_size(books, 0.0, turnover=turnover0, tripwire_bps=0.5)["breakeven_notional_usd"]
    back = max_turnover_at_size(books, 0.0, gross_notional=n_star, tripwire_bps=0.5)["max_turnover"]
    assert back == pytest.approx(turnover0, rel=1e-3)


def test_max_turnover_infinite_when_no_cost():
    flat = {"bids": [(100.0, 1e9)], "asks": [(100.0, 1e9)]}
    out = max_turnover_at_size({"spot": flat, "perp": flat}, 0.0, gross_notional=1_000.0)
    assert out["max_turnover"] == float("inf")


def test_max_turnover_size_guard():
    with pytest.raises(ValueError):
        max_turnover_at_size({"spot": TIGHT}, 0.0, gross_notional=0.0)


def test_feasible_point_below_frontier_has_both_headrooms():
    books = {"spot": TIGHT, "perp": TIGHT}
    n_star = breakeven_size(books, 0.0, turnover=0.1, tripwire_bps=0.5)["breakeven_notional_usd"]
    out = feasible_operating_point(books, 0.0, gross_notional=n_star * 0.5, turnover=0.05, tripwire_bps=0.5)
    assert out["feasible"] is True
    assert out["friction_bps"] < 0.5
    assert out["size_headroom_usd"] > 0.0
    assert out["turnover_headroom"] > 0.0


def test_feasible_point_above_frontier_is_infeasible():
    books = {"spot": TIGHT, "perp": TIGHT}
    n_star = breakeven_size(books, 0.0, turnover=0.1, tripwire_bps=0.5)["breakeven_notional_usd"]
    out = feasible_operating_point(books, 0.0, gross_notional=n_star * 2.0, turnover=0.1, tripwire_bps=0.5)
    assert out["feasible"] is False
    assert out["friction_bps"] > 0.5
    assert out["size_headroom_usd"] < 0.0


def test_feasible_point_guards():
    with pytest.raises(ValueError):
        feasible_operating_point({"spot": TIGHT}, 0.0, gross_notional=0.0, turnover=0.1)
    with pytest.raises(ValueError):
        feasible_operating_point({"spot": TIGHT}, 0.0, gross_notional=1_000.0, turnover=-0.1)


def test_max_turnover_reconciles_with_fee_breakeven_turnover():
    from quant_marketdata.carry_cost import breakeven_turnover
    from quant_marketdata.paper_execution import _half_spread_bps

    books = {"btc_spot": TIGHT, "btc_perp": TIGHT, "eth_spot": DEEP, "eth_perp": DEEP}
    intercept = max_turnover_at_size(books, 0.0, gross_notional=1.0, tripwire_bps=0.5)["max_turnover"]
    half_spreads = {leg: _half_spread_bps(b) for leg, b in books.items()}
    fee_ceiling = breakeven_turnover(half_spreads, 0.0, tripwire_bps=0.5)
    assert intercept == pytest.approx(fee_ceiling, rel=1e-6)
