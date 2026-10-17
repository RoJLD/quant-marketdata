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

## Venue data layer (`quant_marketdata.venues`)

One `Venue` protocol, canonical schemas, an instrument registry and an incremental Parquet store, so every
venue's history lands in the same shape. Adapters, all over public JSON APIs without venue SDKs (see the
Quant-lab spec `2026-10-15-multi-venue-data-layer-design.md`):

| Venue | Native symbol | Funding | Datasets |
|---|---|---|---|
| `lighter` | `BTC` | 1h, unsigned % + `direction` | funding, candles, mark candles, daily OI/volume, specs |
| `hyperliquid` | `BTC`, `kPEPE` | 1h | funding, candles (last 5000 per interval), specs |
| `binance` (USD-M) | `BTCUSDT` | per symbol, 8h/4h/1h, changes over time | funding, candles, mark candles, specs |
| `extended` | `BTC-USD` | 1h | funding, candles, mark candles, specs |
| `aster` | `BTCUSDT` | per symbol, 8h/4h/2h/1h | funding, candles, mark candles, specs |

Each adapter's module docstring lists the API quirks it absorbs.

- **Funding normalized per hour**: one row per settlement with the native values kept (`rate_raw`,
  `direction_raw`), `rate` as a signed fraction over the interval (**positive = longs pay**), `interval_h`,
  `rate_1h` (the cross-venue comparison column) and `apr`. `funding_grid(events, "settlement" | "accrual")`
  gives the cash-flow or the level view on an hourly grid.
- **Registry**: canonical ids `<BASE>-<QUOTE>-PERP` (`1000BONK` -> `BONK-USD-PERP`, multiplier 1000),
  asset classes, point-in-time listing/delisting. Ambiguous symbols are reported as unresolved, never
  guessed; explicit decisions live in `venues/registry_overrides.py`.
- **Store**: root = `--root` -> `$QUANT_MARKETDATA_ROOT` -> `./data/marketdata`; re-running a download only
  fetches what is new.

```bash
pip install "quant-marketdata[store]"   # pyarrow for the Parquet store
qmd-download --venue lighter --symbols BTC ETH --resolutions 1h 1d --since 2026-01-01
qmd-download --venue binance --symbols BTCUSDT ETHUSDT --datasets funding
```

```python
from quant_marketdata.venues import load_funding, load_registry
btc = load_funding("BTC-USD-PERP")        # every venue's funding events, with a `venue` column
```

Live API tests are opt-in: `pytest -m live`.

### Live streams (`quant_marketdata.venues.stream`)

REST APIs keep no history of the order book, intraday open interest or mark price, so they are recorded
live from the venue websocket (Lighter for now) into `venues/<venue>/stream/<channel>/<SYMBOL>/<day>.parquet`.

```bash
pip install "quant-marketdata[stream]"
qmd-record run --venue lighter --symbols BTC ETH            # until Ctrl+C (--duration N for N seconds)
qmd-record check --venue lighter --symbols BTC ETH         # live book vs REST snapshots, files vs live book
```

```python
from quant_marketdata.venues.stream import book_at, read_stream
trades = read_stream("lighter", "trades", "BTC", start="2026-10-17")
liquidations = trades[trades["type"] == "liquidation"]
book = book_at(read_stream("lighter", "order_book", "BTC", start="2026-10-17"))   # .levels("bid", 10)
```

## Scope

Deliberately focused on the cost-realism primitives shared by research (realism experiments) and live
operation (a fund). Provenance: extracted from a multi-year crypto-carry research program.

*Research software — not financial advice.*
