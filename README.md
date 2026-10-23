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
| `paradex` (perps + options) | `BTC-USD-PERP`, `kPEPE-USD-PERP` | continuous; hourly events from the funding index | funding (from 2024-03-12), candles (1m-1h), mark candles, specs; options (see below) |
| `derive` (perps + options) | `BTC-PERP` | continuous; hourly rate, ~12 days of history | funding, specs; options (see below) |

Each adapter's module docstring lists the API quirks it absorbs. Paradex accrues funding continuously:
its funding index is sampled at every whole hour and each hour becomes one settlement-equivalent event,
`rate = Δindex / price` (one request per hour of history, ~11 min per market-year with `--rate 20`).

- **Funding normalized per hour**: one row per settlement with the native values kept (`rate_raw`,
  `direction_raw`), `rate` as a signed fraction over the interval (**positive = longs pay**), `interval_h`,
  `rate_1h` (the cross-venue comparison column) and `apr`. `funding_grid(events, "settlement" | "accrual")`
  gives the cash-flow or the level view on an hourly grid.
- **Registry**: canonical ids `<BASE>-<QUOTE>-PERP` (`1000BONK` -> `BONK-USD-PERP`, multiplier 1000),
  asset classes, point-in-time listing/delisting. A contract on a restricted trading session, as the
  venue declares it, is its own instrument (`ADBE-USD-PERP-24X5`, `ADSK-USD-PERP-NOOVERNIGHT`).
  Ambiguous symbols are reported as unresolved with a reason, never guessed; explicit decisions live in
  `venues/registry_overrides.py`.
- **Store**: root = `--root` -> `$QUANT_MARKETDATA_ROOT` -> `./data/marketdata`; re-running a download only
  fetches what is new.

```bash
pip install "quant-marketdata[store]"   # pyarrow for the Parquet store
qmd-download --venue lighter --symbols BTC ETH --resolutions 1h 1d --since 2026-01-01
qmd-download --venue binance --symbols BTCUSDT ETHUSDT --datasets funding
qmd-download --venue paradex --symbols BTC-USD-PERP --datasets funding --since 2026-01-01 --rate 20
```

```python
from quant_marketdata.venues import load_funding, load_registry
btc = load_funding("BTC-USD-PERP")        # every venue's funding events, with a `venue` column
```

Live API tests are opt-in: `pytest -m live`.

### Dated contracts: options and prediction markets (`quant_marketdata.venues.contracts`)

Options and binary contracts have a life cycle (listing, expiry, settlement), structured attributes and
volumes that a one-file-per-symbol perp layout cannot hold (Kalshi KXBTCD alone creates ~4,500 markets a
day). They get their own schemas, registry and partitioned store (Quant-lab spec
`2026-10-18-options-prediction-markets-design.md`). Data only: implied probabilities, distributions and
hourly vol are estimated in Quant-lab.

| Venue | Protocol | Contracts | Live (snapshot) | History |
|---|---|---|---|---|
| `derive` | `OptionsVenue` (+ perps) | `get_all_instruments`, ~11 days of expiries | chains per expiry, venue greeks (undiscounted Black-76 on the forward) | trades since 2024-01 (no trader ids); settlements of about two weeks of expiries |
| `deribit` | `OptionsVenue` | inverse BTC/ETH (linear USDC opt-in) | one book summary per currency: USD prices via the pass's index, IV / 100, no greeks | deliveries since 2016; archive trades with IV |
| `kalshi` | `BinaryVenue` | KXBTCD, KXBTC, KXETHD, KXETH (series table only) | quotes of open markets, YES book | markets, outcomes, 1m/1h/1d candles and trades from both data tiers |

Paradex options join the same schemas in the Paradex adapter (they are discovered by capability).

- **Canonical ids** (the settlement index is never part of an id): options
  `BTC-USD-20261023T0800-82000-C` (strike in minimal decimals), binaries `BTC-USD-20261018T1000-GE81500`
  (`GE<k>`, `LT<k>`, `IN<lo>_<hi>`, bounds `k_low <= S_T < k_high`).
- **Canonical units**: option prices in USD per unit of underlying (Deribit's BTC prices x the index of
  the same pass, raw kept), IV as a fraction, `delta`/`gamma` per unit of the declared
  `greeks_reference`, vega per vol point, theta per calendar day; binary quotes as YES probabilities with
  `p_mid` NaN when a side is missing. `settlement_price` / `settlement_value` = the index value at the
  fixing, never a payoff.
- **Nothing read from names**: underlyings come from structured fields, an explicit override
  (`contracts/overrides.py`) or the crypto-ticker rule; names are only cross-checked (a mismatch is
  reported `unresolved`). Kalshi tickers (New York time) are never parsed: the expiry is the event's
  `strike_date`, the underlying and index come from the series table.
- **Settlement indexes** (`contracts/indexes.py`): `cfb:BRTI`/`cfb:ERTI` (Kalshi; fixing method read per
  market: 2025 trimmed mean, 2026 simple 60 s mean), `deribit:btc_usd` (30-min TWAP, venue doc),
  `paradex:<BASE>` (30-min TWAP of the spot oracle, venue doc), `derive:BTC-USD` (unknown). Index gaps are
  columns, never corrected: `settlement_comparison(load_settlements())` lines up each fixing with the gap in
  basis points (2026-10-18 08:00 UTC: Deribit +1.8 bp, Derive +2.2 bp over Kalshi's BRTI fixing).
- **Fees** (`contracts/fees.py`): raw venue rates in the registry, the composition rule declared per
  venue with `fee_model_status` (`unconfirmed` for Derive, Deribit and Kalshi's quadratic formula until
  the published schedules are read).
- **Registry** (`registry/options/<venue>.parquet`, `registry/binaries/<venue>/<SERIES>/<YYYY-MM>.parquet`):
  point-in-time; a contract missing before its expiry is `delisted`, after it `expired`, then completed
  with its settlement (`settlement_seen_at`); nothing is deleted; `first_seen_at` is the first pass that
  saw it; `universe_change` events (new underlying, expiry, series) in `registry/universe_changes/`.
- **Store**: chains and quotes are append-only snapshot parts (`venues/<venue>/option_chain/<UNDERLYING>/
  <day>.parts/`) compacted into daily files; trades and candles in monthly partitions; settlements per index.

```bash
qmd-snapshot                 # one pass: BTC/ETH chains (Derive, Deribit) + Kalshi quotes, ~25 s, ~0.7 MB
qmd-options-daily            # before 08:00 UTC: recent expiries, settlements, Kalshi candles, trades
```

Schedule `qmd-snapshot` every 15 minutes (:00, :15, :30, :45) plus once at 07:58 UTC, and
`qmd-options-daily` once a day before 08:00 UTC (each run exits; a failing venue does not stop the others;
`venues/<venue>/_snapshot_health.json` is the heartbeat). Derive keeps ~11 days of expired contracts and ~12
days of settlement prices, Paradex no quotes nor candles after expiry: a day not recorded is lost.

```python
from quant_marketdata.venues.contracts import load_chain, load_ladder, load_settlements, settlement_comparison
chain = load_chain("BTC-USD")                                  # last snapshot per venue + registry columns
ladder = load_ladder("KXBTCD", "KXBTCD-26OCT1904", at="2026-10-19 07:30")
gaps = settlement_comparison(load_settlements())               # one row per fixing, gap_bp:<index>
```

Decisions kept from the spec's open questions (validated 2026-10-18 with their defaults):

1. Cadence and universe: BTC and ETH every 15 minutes plus 07:58 UTC; widen after a week of disk use.
2. Host: a missed pass is not backfilled (Derive and Paradex quotes are live-only).
3. Paradex settlement: answered since (30-min TWAP of the spot oracle, served after expiry): `twap_30min`.
4. Derive: index settlement method `unknown`; abbreviated ticker keys mapped by a tested table (deduced from
   values); fee composition `unconfirmed`.
5. Kalshi fees: quadratic formula with an `unconfirmed` coefficient; data use only.
6. Deribit: delivery = 30-min TWAP per the venue docs (to reconfirm); inverse BTC/ETH chains only.
7. F4 estimator and caps belong to the scanner's YAML; the 07:15-07:58 passes are kept.
8. Lighter binaries: semantics not assumed; nothing recorded until `/events` is live on mainnet.

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

### Dated options: Paradex (`quant_marketdata.venues.paradex_snapshot`)

Option listings, chains (quotes, IV, greeks) and mark candles exist only while a contract lives: they are
recorded by passes into the dated-contract store (`venues.contracts.ContractStore`), the fixings of expired
contracts are fetched afterwards by name.

```python
from quant_marketdata.venues import get_venue
from quant_marketdata.venues.contracts import ContractStore
from quant_marketdata.venues.paradex_snapshot import download_mark_candles, settle_expired, snapshot_pass

venue, store = get_venue("paradex", rate=20), ContractStore.at()   # same root as the perps store
snapshot_pass(venue, store, underlyings=("BTC", "ETH"))   # every 15 min: registry + chain snapshots
settle_expired(venue, store)                              # daily: fixings (30-min TWAP of the spot oracle)
download_mark_candles(venue, store, "BTC")                # daily: mark candles of the live contracts
```

## Scope

Deliberately focused on the cost-realism primitives shared by research (realism experiments) and live
operation (a fund). Provenance: extracted from a multi-year crypto-carry research program.

*Research software — not financial advice.*
