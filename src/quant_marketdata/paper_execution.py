"""Carry paper-execution simulator: model fills by walking the observed order book.

Replaces the carry cost-monitor's fill-at-the-touch assumption (the spread FLOOR) with a fill
modeled against the observed book DEPTH (VWAP across consumed levels), yielding a depth-aware
modeled-realized friction at the carry's order size. HONEST: it is a MODEL -- it assumes the
displayed book is the available liquidity and the order consumes it without market reaction,
hidden liquidity, or queue dynamics; it is NOT real fills (the tripwire's real-cost question
still needs testnet/live). Modeled-realized >= floor always (the floor is the zero-walk limit).
math/pandas-pure, NO network.
"""

from __future__ import annotations

import math

from quant_marketdata.carry_cost import carry_friction_bps_per_day


def walk_book(levels, size: float):
    """Consume (price, qty) levels in order until ``size`` is filled. Return (vwap, filled, unfilled)."""
    if size <= 0:
        return (float("nan"), 0.0, 0.0)
    remaining = float(size)
    cost = 0.0
    filled = 0.0
    for price, qty in levels:
        take = min(float(qty), remaining)
        cost += float(price) * take
        filled += take
        remaining -= take
        if remaining <= 0:
            break
    vwap = cost / filled if filled > 0 else float("nan")
    return (vwap, filled, max(0.0, remaining))


def slippage_bps(vwap: float, mid: float, *, side: str) -> float:
    """Adverse deviation of the VWAP fill from mid, in bps. NaN if mid<=0 or vwap is NaN."""
    if mid <= 0 or math.isnan(vwap):
        return float("nan")
    return (vwap - mid) / mid * 1e4 if side == "buy" else (mid - vwap) / mid * 1e4


def _mid(book) -> float:
    best_bid = book["bids"][0][0] if book.get("bids") else float("nan")
    best_ask = book["asks"][0][0] if book.get("asks") else float("nan")
    return 0.5 * (best_bid + best_ask)


def leg_roundtrip_bps(book, notional: float, taker_fee_bps: float) -> dict:
    """Round-trip a ``notional``-sized position on one leg by walking the book (buy asks, sell bids)."""
    mid = _mid(book)
    size = notional / mid if mid > 0 else 0.0
    vwap_buy, _, unf_buy = walk_book(book.get("asks", []), size)
    vwap_sell, _, unf_sell = walk_book(book.get("bids", []), size)
    buy_slip = slippage_bps(vwap_buy, mid, side="buy")
    sell_slip = slippage_bps(vwap_sell, mid, side="sell")
    return {
        "roundtrip_bps": buy_slip + sell_slip + 2.0 * taker_fee_bps,
        "buy_slippage_bps": buy_slip,
        "sell_slippage_bps": sell_slip,
        "unfilled_notional": max(unf_buy, unf_sell) * mid,
    }


def _half_spread_bps(book) -> float:
    mid = _mid(book)
    if mid <= 0:
        return float("nan")
    best_bid, best_ask = book["bids"][0][0], book["asks"][0][0]
    return (best_ask - best_bid) / 2.0 / mid * 1e4


def simulate_carry_friction(books, notionals, taker_fee_bps: float, *, turnover: float) -> dict:
    """Modeled-realized carry friction (bps/day) from book-walking, vs the spread-floor."""
    per_leg = {leg: leg_roundtrip_bps(books[leg], notionals[leg], taker_fee_bps) for leg in books}
    modeled = turnover * sum(v["roundtrip_bps"] for v in per_leg.values())
    floor = carry_friction_bps_per_day(
        {leg: _half_spread_bps(books[leg]) for leg in books}, taker_fee_bps, turnover=turnover
    )
    return {
        "friction_bps_per_day": float(modeled),
        "floor_bps_per_day": float(floor),
        "erosion_bps": float(modeled - floor),
        "unfilled_notional": float(sum(v["unfilled_notional"] for v in per_leg.values())),
        "per_leg": per_leg,
    }
