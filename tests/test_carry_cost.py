import pandas as pd

from quant_marketdata.carry_cost import (
    breakeven_turnover,
    carry_friction_bps_per_day,
    evaluate_tripwire,
)


def test_friction_known_value():
    # 2 legs each half-spread 1 bps, taker fee 0.5 bps: per-unit round-trip = 2*(2*1+2*0.5)=6 bps.
    spreads = {"a": 1.0, "b": 1.0}
    assert abs(carry_friction_bps_per_day(spreads, 0.5, turnover=0.1) - 0.6) < 1e-12
    assert carry_friction_bps_per_day(spreads, 0.5, turnover=0.0) == 0.0


def test_friction_rejects_negative_turnover():
    try:
        carry_friction_bps_per_day({"a": 1.0}, 0.5, turnover=-0.1)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


def test_breakeven_turnover_value_and_zero_cost():
    spreads = {"a": 1.0, "b": 1.0}  # per-unit = 6 bps
    assert abs(breakeven_turnover(spreads, 0.5, tripwire_bps=0.5) - 0.5 / 6.0) < 1e-12
    # friction at the breakeven turnover equals the tripwire (round-trip consistency)
    be = breakeven_turnover(spreads, 0.5, tripwire_bps=0.5)
    assert abs(carry_friction_bps_per_day(spreads, 0.5, turnover=be) - 0.5) < 1e-12
    # zero per-unit cost -> infinite breakeven
    assert breakeven_turnover({"a": 0.0}, 0.0) == float("inf")


def test_evaluate_tripwire_persistence_gated():
    idx = pd.date_range("2026-01-01", periods=4, freq="D")
    # last 3 over 0.5 -> breached at persistence=3
    breached = evaluate_tripwire(pd.Series([0.3, 0.6, 0.6, 0.6], index=idx), persistence=3)
    assert (
        breached["breached"] is True and breached["consecutive"] == 3 and breached["latest"] == 0.6
    )
    # a gap in the run -> not breached
    gap = evaluate_tripwire(pd.Series([0.6, 0.3, 0.6], index=idx[:3]), persistence=3)
    assert gap["breached"] is False and gap["consecutive"] == 1
    # too few points -> not breached even if all over
    short = evaluate_tripwire(pd.Series([0.6, 0.6], index=idx[:2]), persistence=3)
    assert short["breached"] is False
