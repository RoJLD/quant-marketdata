# quant-marketdata

**Market-data & execution-simulation primitives for crypto perp/spot carry** — the honest cost-realism
layer between a backtest and real fills. numpy/pandas, MIT.

When a delta-neutral carry looks profitable on paper, the question is whether it survives *execution*: the
spread you cross, the depth you consume, the impact of your size, and the funding cost of holding. This
package bundles the primitives that answer that, honestly (they are **models** — displayed-book, no queue or
market reaction — a necessary-not-sufficient floor, not a substitute for real fills):

- **`carry_cost`** — spread+fee friction floor (`carry_friction_bps_per_day`), the breakeven daily turnover
  before a tripwire (`breakeven_turnover`), and a persistence-gated tripwire (`evaluate_tripwire`).
- **`paper_execution`** — model fills by walking the observed book: VWAP across consumed levels
  (`walk_book`), slippage vs mid (`slippage_bps`), per-leg round-trip (`leg_roundtrip_bps`), and
  depth-aware modeled-realized carry friction vs the floor (`simulate_carry_friction`).
- **`market_impact`** — beyond-displayed-book square-root impact calibrated to the book
  (`impact_beyond_book`, `book_impact_curve`), and capacity: the largest size within a tripwire
  (`breakeven_size`), the largest turnover at a size (`max_turnover_at_size`), and a single-point verdict
  (`feasible_operating_point`).
- **`live_quotes`** — observed top-of-book / L2 books via ccxt (public, no key), with a urllib Hyperliquid
  fallback (`fetch_leg_spreads`, `fetch_leg_books`). Requires the `live` extra (`pip install
  quant-marketdata[live]`); ccxt is imported lazily, so the rest of the package needs only pandas.

## Install

```bash
pip install "quant-marketdata @ git+https://github.com/RoJLD/quant-marketdata.git@v0.1.0"
# with the live ccxt adapter:
pip install "quant-marketdata[live] @ git+https://github.com/RoJLD/quant-marketdata.git@v0.1.0"
```

## Example

```python
from quant_marketdata import walk_book, slippage_bps, impact_beyond_book

book = {"bids": [(100.0, 5.0), (99.9, 20.0)], "asks": [(100.1, 5.0), (100.2, 20.0)]}
vwap, filled, unfilled = walk_book(book["asks"], size=8.0)   # consume 8 units up the asks
print(slippage_bps(vwap, mid=100.05, side="buy"))            # adverse deviation vs mid, in bps
print(impact_beyond_book(book, notional=1_000_000, side="buy"))  # sqrt-law impact beyond displayed depth
```

## Scope

Deliberately focused on the cost-realism primitives shared by research (realism experiments) and live
operation (a fund). Provenance: extracted from a multi-year crypto-carry research program.

*Research software — not financial advice.*
