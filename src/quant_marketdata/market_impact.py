"""Beyond-displayed-book market impact + breakeven-in-size for carry execution (paper).

Extends paper_execution's within-displayed-book VWAP walk with (a) the observed impact CURVE and
(b) a beyond-book square-root impact law CALIBRATED to the book (continuity + boundary slope match),
and (c) breakeven_size: the largest gross notional whose impact-aware daily friction stays within the
tripwire. Necessary-not-sufficient (no real fills, no queue, no market reaction), consistent with the
rest of the cost-realism layer. math/stdlib-pure, NO network.
"""

from __future__ import annotations

import math

from quant_marketdata.paper_execution import (
    _half_spread_bps,
    _mid,
    slippage_bps,
    walk_book,
)


def _levels(book, side: str):
    return book.get("asks", []) if side == "buy" else book.get("bids", [])


def book_impact_curve(book, side: str) -> list[tuple[float, float]]:
    """Cumulative (notional_usd, avg_impact_bps) at each displayed level, filling from the touch.
    side='buy' walks asks, 'sell' walks bids. Impact = VWAP slippage vs mid. Last point is the
    displayed-depth boundary (N_disp, imp_disp)."""
    mid = _mid(book)
    levels = _levels(book, side)
    curve: list[tuple[float, float]] = []
    cum_qty = 0.0
    for _price, qty in levels:
        cum_qty += float(qty)
        vwap, _filled, _unf = walk_book(levels, cum_qty)
        imp = slippage_bps(vwap, mid, side=side)
        curve.append((cum_qty * mid, float(imp)))
    return curve


def impact_beyond_book(book, notional: float, side: str) -> float:
    """Average impact (bps) to fill ``notional`` USD. Within displayed depth: exact VWAP walk. Beyond:
    sqrt law imp_disp + k*(sqrt(N/N_disp) - 1), k = 2*N_disp*slope_obs (continuity + boundary slope
    match). Degenerate book (empty / zero mid / single level beyond its depth) -> half-spread floor.
    notional <= 0 -> ValueError."""
    if notional <= 0:
        raise ValueError(f"notional must be > 0, got {notional}")
    mid = _mid(book)
    curve = book_impact_curve(book, side)
    if mid <= 0 or not curve or math.isnan(curve[-1][1]):
        return float(_half_spread_bps(book))  # degenerate
    n_disp, imp_disp = curve[-1]
    if notional <= n_disp:
        vwap, _f, _u = walk_book(_levels(book, side), notional / mid)
        return float(slippage_bps(vwap, mid, side=side))
    if len(curve) < 2:
        return float(_half_spread_bps(book))  # single level -> no slope to calibrate
    n_prev, imp_prev = curve[-2]
    denom = n_disp - n_prev
    slope = (imp_disp - imp_prev) / denom if denom > 0 else 0.0
    if slope <= 0:
        return float(imp_disp)  # non-increasing observed impact -> flat at the boundary
    k = 2.0 * n_disp * slope
    return float(imp_disp + k * (math.sqrt(notional / n_disp) - 1.0))


def leg_roundtrip_bps_impact(book, notional: float, taker_fee_bps: float) -> dict:
    """Round-trip ``notional`` on one leg using impact_beyond_book (buy asks + sell bids), so friction
    is defined for ANY size (not depth-capped). Mirrors paper_execution.leg_roundtrip_bps."""
    buy = impact_beyond_book(book, notional, "buy")
    sell = impact_beyond_book(book, notional, "sell")
    return {
        "roundtrip_bps": float(buy + sell + 2.0 * taker_fee_bps),
        "buy_impact_bps": float(buy),
        "sell_impact_bps": float(sell),
    }


def _gross_friction(books, gross_notional: float, taker_fee_bps: float, turnover: float) -> float:
    per_leg = gross_notional / len(books)
    return turnover * sum(
        leg_roundtrip_bps_impact(books[leg], per_leg, taker_fee_bps)["roundtrip_bps"]
        for leg in books
    )


def _binding_leg(books, gross_notional: float, taker_fee_bps: float) -> str:
    per_leg = gross_notional / len(books)
    costs = {
        leg: leg_roundtrip_bps_impact(books[leg], per_leg, taker_fee_bps)["roundtrip_bps"]
        for leg in books
    }
    return max(costs, key=costs.get)


def breakeven_size(
    books, taker_fee_bps: float, *, turnover: float, tripwire_bps: float = 0.5
) -> dict:
    """Largest gross notional N* whose impact-aware daily friction <= tripwire_bps at ``turnover``.
    friction(N) = turnover * sum_legs(roundtrip_bps(leg, N/n_legs)); N split equally across legs.
    Friction is monotone increasing in N -> bisection. Returns {breakeven_notional_usd, binding_leg,
    friction_at_breakeven_bps}. Special cases: no cost -> inf; size-independent floor (half-spread +
    fees at minimal size) already >= tripwire -> {..., floor_bound: True} with breakeven 0; a book too
    deep to ever breach within the bracket -> {..., bracket_capped: True} with inf."""
    if turnover < 0:
        raise ValueError(f"turnover must be >= 0, got {turnover}")

    def friction(gross: float) -> float:
        if gross <= 0.0:
            return 0.0
        return _gross_friction(books, gross, taker_fee_bps, turnover)

    probe = 1.0  # ~$1 gross -> friction here is the size-independent floor (half-spread + fees)
    floor = friction(probe)
    if floor <= 0.0:  # zero turnover / zero cost -> unbounded capacity
        return {"breakeven_notional_usd": float("inf"), "binding_leg": None,
                "friction_at_breakeven_bps": 0.0}
    if floor >= tripwire_bps:  # the floor alone breaches -> no size headroom at all
        return {"breakeven_notional_usd": 0.0,
                "binding_leg": _binding_leg(books, probe, taker_fee_bps),
                "friction_at_breakeven_bps": float(floor), "floor_bound": True}

    lo, hi = probe, probe
    max_bracket = 1e15
    while friction(hi) < tripwire_bps:
        lo, hi = hi, hi * 2.0
        if hi > max_bracket:
            return {"breakeven_notional_usd": float("inf"),
                    "binding_leg": _binding_leg(books, lo, taker_fee_bps),
                    "friction_at_breakeven_bps": float(friction(lo)), "bracket_capped": True}
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if friction(mid) < tripwire_bps:
            lo = mid
        else:
            hi = mid
    return {
        "breakeven_notional_usd": float(lo),
        "binding_leg": _binding_leg(books, lo, taker_fee_bps),
        "friction_at_breakeven_bps": float(friction(lo)),
    }


def max_turnover_at_size(
    books, taker_fee_bps: float, *, gross_notional: float, tripwire_bps: float = 0.5
) -> dict:
    """Largest daily turnover tau* whose impact-aware friction at fixed gross ``gross_notional`` stays
    within ``tripwire_bps``. friction(N, tau) = tau * sum_legs(roundtrip_bps(leg, N/n_legs)) is LINEAR in
    tau -> closed form tau* = tripwire / per_unit_roundtrip (no bisection). The orthogonal slice of
    breakeven_size (which fixes tau and solves for N). per-unit cost 0 (degenerate/zero-spread book) ->
    tau* = inf. ``gross_notional`` <= 0 -> ValueError."""
    if gross_notional <= 0:
        raise ValueError(f"gross_notional must be > 0, got {gross_notional}")
    per_unit = _gross_friction(books, gross_notional, taker_fee_bps, 1.0)  # friction at turnover=1
    if per_unit <= 0.0:
        return {"max_turnover": float("inf"),
                "binding_leg": _binding_leg(books, gross_notional, taker_fee_bps),
                "friction_at_max_bps": 0.0, "per_unit_roundtrip_bps": float(per_unit)}
    tau = tripwire_bps / per_unit
    return {
        "max_turnover": float(tau),
        "binding_leg": _binding_leg(books, gross_notional, taker_fee_bps),
        "friction_at_max_bps": float(_gross_friction(books, gross_notional, taker_fee_bps, tau)),
        "per_unit_roundtrip_bps": float(per_unit),
    }


def feasible_operating_point(
    books, taker_fee_bps: float, *, gross_notional: float, turnover: float, tripwire_bps: float = 0.5
) -> dict:
    """Verdict for a single operating point (N, tau): feasible iff friction(N, tau) <= tripwire, plus the
    headroom on each axis (size_headroom_usd = breakeven_size(tau) - N; turnover_headroom =
    max_turnover_at_size(N) - tau). ``gross_notional`` <= 0 or ``turnover`` < 0 -> ValueError."""
    if gross_notional <= 0:
        raise ValueError(f"gross_notional must be > 0, got {gross_notional}")
    if turnover < 0:
        raise ValueError(f"turnover must be >= 0, got {turnover}")
    friction = _gross_friction(books, gross_notional, taker_fee_bps, turnover)
    size_be = breakeven_size(books, taker_fee_bps, turnover=turnover, tripwire_bps=tripwire_bps)[
        "breakeven_notional_usd"
    ]
    turn_be = max_turnover_at_size(
        books, taker_fee_bps, gross_notional=gross_notional, tripwire_bps=tripwire_bps
    )["max_turnover"]
    return {
        "feasible": bool(friction <= tripwire_bps),
        "friction_bps": float(friction),
        "size_headroom_usd": float(size_be - gross_notional),
        "turnover_headroom": float(turn_be - turnover),
        "binding_leg": _binding_leg(books, gross_notional, taker_fee_bps),
    }
