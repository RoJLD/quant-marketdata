"""Pure cost-attribution for funding-carry execution (paper).

Estimates the carry's per-day execution friction (bps on gross) from observed half-spreads +
known taker fees at a given daily turnover, the breakeven turnover at which friction hits the
0.5 bps/day tripwire, and a persistence-gated tripwire over a friction series. PAPER: spreads
are observed but fills are modeled; this is the observable friction FLOOR (spread+fees) and is
necessary-not-sufficient. pandas-pure, NO network.
"""

from __future__ import annotations

import pandas as pd


def _per_unit_roundtrip_bps(half_spreads_bps: dict, taker_fee_bps: float) -> float:
    """Round-trip cost (bps) per unit of gross turned over: sum over legs of 2*(half_spread+fee)."""
    return float(sum(2.0 * (hs + taker_fee_bps) for hs in half_spreads_bps.values()))


def carry_friction_bps_per_day(
    half_spreads_bps: dict, taker_fee_bps: float, *, turnover: float
) -> float:
    """Per-day execution friction (bps on gross) at a given daily ``turnover`` fraction.

    friction = turnover * sum_legs(2*half_spread + 2*taker_fee). ``half_spreads_bps`` maps each
    leg (spot/perp x btc/eth) to its observed half-spread in bps.
    """
    if turnover < 0:
        raise ValueError(f"turnover must be >= 0, got {turnover}")
    return float(turnover * _per_unit_roundtrip_bps(half_spreads_bps, taker_fee_bps))


def breakeven_turnover(
    half_spreads_bps: dict, taker_fee_bps: float, *, tripwire_bps: float = 0.5
) -> float:
    """Daily turnover at which friction == ``tripwire_bps`` (inf if per-unit cost is 0).

    How much daily churn the carry can tolerate before breaching the tripwire — reported instead
    of fabricating a single realized-cost number (turnover is the unobservable lever).
    """
    per_unit = _per_unit_roundtrip_bps(half_spreads_bps, taker_fee_bps)
    return float(tripwire_bps / per_unit) if per_unit > 0 else float("inf")


def evaluate_tripwire(
    friction_series: pd.Series, *, tripwire_bps: float = 0.5, persistence: int = 3
) -> dict:
    """Persistence-gated breach: True iff the trailing ``persistence`` observations all exceed the tripwire.

    Counts observations (rows), not calendar days: under the intended once-per-day cron one row ==
    one day, so ``persistence`` consecutive observations == consecutive days. Avoids single-run
    alerts (an implicit sequential test / noise). Returns the trailing consecutive-breach count and
    the latest value.
    """
    s = friction_series.dropna()
    consecutive = 0
    for v in reversed(s.tolist()):
        if v > tripwire_bps:
            consecutive += 1
        else:
            break
    breached = len(s) >= persistence and consecutive >= persistence
    latest = float(s.iloc[-1]) if len(s) else float("nan")
    return {"breached": bool(breached), "consecutive": int(consecutive), "latest": latest}
