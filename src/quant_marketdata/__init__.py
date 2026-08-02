"""quant-marketdata -- market-data & execution-simulation primitives for crypto perp/spot carry.

The honest cost-realism layer between a backtest and real fills (models, not real fills):
- carry_cost: spread+fee friction floor, breakeven turnover, persistence-gated tripwire,
- paper_execution: book-walking VWAP fills, slippage vs mid, modeled-realized carry friction,
- market_impact: sqrt-law beyond-book impact + capacity (breakeven size / max turnover / feasibility),
- live_quotes: observed books/spreads via ccxt (lazy; the ``live`` extra) + HL urllib fallback.
pandas core; ccxt only for live_quotes.
"""
from quant_marketdata.carry_cost import (
    breakeven_turnover,
    carry_friction_bps_per_day,
    evaluate_tripwire,
)
from quant_marketdata.live_quotes import fetch_leg_books, fetch_leg_spreads
from quant_marketdata.market_impact import (
    book_impact_curve,
    breakeven_size,
    feasible_operating_point,
    impact_beyond_book,
    leg_roundtrip_bps_impact,
    max_turnover_at_size,
)
from quant_marketdata.paper_execution import (
    leg_roundtrip_bps,
    simulate_carry_friction,
    slippage_bps,
    walk_book,
)

__version__ = "0.1.0"

__all__ = [
    # carry_cost
    "carry_friction_bps_per_day",
    "breakeven_turnover",
    "evaluate_tripwire",
    # paper_execution
    "walk_book",
    "slippage_bps",
    "leg_roundtrip_bps",
    "simulate_carry_friction",
    # market_impact
    "book_impact_curve",
    "impact_beyond_book",
    "leg_roundtrip_bps_impact",
    "breakeven_size",
    "max_turnover_at_size",
    "feasible_operating_point",
    # live_quotes (needs the `live` extra: ccxt)
    "fetch_leg_spreads",
    "fetch_leg_books",
]
