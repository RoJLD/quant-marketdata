# Changelog

All notable changes to **quant-marketdata** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versioning: [SemVer](https://semver.org/).
The shared market- and funding-data layer of the platform; the cross-repo dependency map lives in the
Quant-lab repo's `docs/PLATFORM.md`.

## [Unreleased]

## [0.6.0] — 2026-10-29

Paradex option mark candles recorded daily by `qmd-options-daily`, bounded and failure-isolated, and a
resume fix for `download_mark_candles` after days off.

### Added
- **`qmd-options-daily` records Paradex option mark candles** (step `option_mark_candles`, per underlying):
  hourly mark candles of every live contract and the spot-oracle candles of the base, through
  `paradex_snapshot.download_mark_candles` (served only until expiry: a day missed was lost). Found by
  capability (`Dataset.OPTION_MARK_CANDLES`: Paradex only). Bounded: its own client at
  `--mark-candles-rate` (default 10 requests/s, ~1,000 requests per base and day) and a time budget per
  underlying, `--mark-candles-minutes` (default 3), after which no fetch starts; failure-isolated: a failing
  contract fails the step (exit code 1, heartbeat), the other contracts and steps are stored. `--no-mark-candles`
  skips it. `StepResult.deferred` (heartbeat `deferred`) counts the series a budget left to the next run.
  Real runs 2026-10-26 into a scratch store (`--venues paradex`): first run 386 s, 1.67M rows, 268 BTC and 96
  ETH long-dated contracts deferred; second run 286 s, nothing deferred; then 257 s (BTC 1,029 series in 123 s,
  ETH 931 in 107 s). Every contract covered hourly from its listing hour, no missing hour.

### Changed
- `paradex_snapshot.download_mark_candles`: a stored series resumes at its last candle, re-reading at least
  `lookback` (it re-read only the last `lookback`: a run after more than two days off left a hole); the
  underlying is fetched first, then the contracts by expiry, soonest first; new `budget` (and `clock`)
  arguments and a `deferred` count in the report.

## [0.5.0] — 2026-10-25

Live open-interest and price snapshots on the six perp venues, Binance Vision open-interest history (research
only), and two registry corrections: Extended settles in USDC, and Lighter's `SPX` is the SPX6900 memecoin.
Consumers moving from v0.3.0 also get v0.4.0's instrument-id change for restricted trading sessions.

### Added
- **`Dataset.OPEN_INTEREST`**: `Venue.open_interest(symbols=None)`, a live snapshot (not stored) on the six
  adapters, one row per symbol (`OPEN_INTEREST_COLUMNS`: base units, price and its kind, USD value, the
  venue's `oi_convention`, timestamp, per-symbol `error`), and `one_side_usd(snapshot)` to compare venues.
  Conventions, checked on live payloads 2026-10-19: Lighter's `orderBookDetails.open_interest` counts one
  side (half its both-sides `exchangeMetrics` series: 1.96 on BTC, 1.98 on ETH); Paradex's
  `markets/summary.open_interest` counts both sides (= `open_interest_long + open_interest_short`);
  Hyperliquid (`metaAndAssetCtxs`), Binance and Aster (`openInterest`, one request per symbol, mark price
  from `premiumIndex`) and Extended (`marketStats`, USD as published) count one side.
- **`Dataset.PRICES`**: `Venue.prices(symbols=None)`, a live snapshot of every perp's mark price, the index
  its funding premium is measured against (`index_kind`: Lighter, Binance, Aster, Extended `index`;
  Hyperliquid `oracle`; Paradex `underlying`) and the last trade (Hyperliquid: mid; Binance/Aster: none),
  one request per venue (`PRICES_COLUMNS`, `make_prices`). For the basis scan of Quant-lab (F2).
- **Binance Vision open-interest history** (`venues.binance_vision`, `qmd-binance-vision`): the daily USD-M
  `metrics` archives of data.binance.vision over plain HTTPS, each zip verified against its `.CHECKSUM`
  (SHA-256) before it is read, stored as `Dataset.OPEN_INTEREST_HISTORY` (`OPEN_INTEREST_HISTORY_COLUMNS`:
  5-minute open interest in base units and USD, one side, the long/short ratios, `source`); a run fetches
  only the days a series lacks, in both directions. The REST `openInterestHist` keeps 30 days only. Rows are
  stamped like that endpoint: an archive row labelled t is the snapshot REST stamps t + 5 min (identical
  values on every common stamp, 2026-10-24); the label stays in `create_time_raw`. **Research only**: the
  Binance Vision Dataset Terms v1.0 (2026-08-26) license the archives CC BY-NC-SA 4.0 for non-commercial,
  non-production research and exclude live trading and fund tooling, so `--root` has no default
  (no `QUANT_MARKETDATA_ROOT` fallback). Funding stays on REST (`BinanceVenue.funding`, full history).
- `JsonClient.get_bytes`: raw bodies under the same pacing and retries.

### Fixed
- **Extended `settle_asset` is `USDC`**, not the `USD` the API serves as `collateralAssetName`: Extended's one
  collateral asset is USDC (docs: "a single collateral balance (USDC)"; `info/assets` gives it the L1 id of
  the USDC ERC-20 on Ethereum). Ids do not change. Quant-lab's allocator blocked every Extended leg on the
  `USD` value, which maps to no settlement asset.
- **Lighter `SPX` is the SPX6900 memecoin** (`crypto`, no `underlying_ref`), not an equity index: it trades
  near 0.35, sits in Lighter's crypto insurance fund, and matches Hyperliquid `SPX` and Binance/Aster
  `SPXUSDT`; the S&P 500 perp is `US500`. Its id stays `SPX-USD-PERP`.

### Documented
- `registry`: `settle_asset` is the margin stablecoin, not the unit of a venue's price index. Hyperliquid's
  oracle tracks USDT prices while Extended's index and Paradex's underlying are in USD, so cross-venue price
  levels differ by the USDT/USD basis (9-13 bp on 2026-10-24); funding rates do not carry it. The USD1 books
  of Binance (3) and Aster (27) are genuinely USD1-margined (`marginAsset` and `quoteAsset` both `USD1`).

## [0.4.0] — 2026-10-23

Dated contracts and two more venues: Paradex (perps and options), Derive (perps and options), Deribit option
chains and Kalshi binaries, on shared contract schemas, with the scheduled tasks that record what only exists
live (`qmd-snapshot`, `qmd-options-daily`). **Instrument ids change** for restricted trading sessions (see
Changed): consumers keyed on `instrument_id` map the Extended stock perps when they move to this version.

### Added
- **Paradex adapter** (`paradex`, perps only) over the public REST API (no SDK): instruments, funding,
  last/mark candles (1m-1h), specs, order book. Paradex funding is continuous; each whole hour becomes a
  settlement-equivalent event `rate = (index_h - index_{h-1}) / price` from the funding index (the
  exact cash flow per unit of notional; positive = longs pay, as the index rises with a positive rate),
  with `rate_raw` = the quoted `funding_rate_8h` and the index kept in `funding_index_raw`. History before
  2024-03-12 08:00 UTC is not served: the venue's stored records swap index and premium before
  06:09:13 that day and the old index does not match the quoted rate. One request per hour of history,
  fetched by 16 threads under the client's rate limiter (~13-17 req/s with `--rate 20`).
- `session` instrument column: the trading session the venue declares (`24x7`, `24x5`, `no-overnight`,
  or none). Extended (`tradingHours`) and Lighter (`market_config.trading_hours`) declare it.
- Registry: Paradex rule (`<BASE>-USD-PERP`, `k` = x1000) and overrides for its TradFi perps; explicit
  "left unresolved" decisions with their reason (`registry_overrides.UNRESOLVED`); non-ASCII names are
  reported as such.
- **Dated contracts** (`quant_marketdata.venues.contracts`, spec Quant-lab
  `2026-10-18-options-prediction-markets-design.md`): canonical schemas of option and binary contracts,
  chain and quote snapshots, trades, candles and settlements, with canonical ids
  (`BTC-USD-20261023T0800-82000-C`, `BTC-USD-20261018T1000-GE81500`; the settlement index is a column,
  never part of an id); the `OptionsVenue` and `BinaryVenue` protocols; a point-in-time contract
  registry separate from the perp registry (underlying from structured fields, the name only
  cross-checked, `delisted` before expiry and `expired` after, settlements kept with
  `settlement_seen_at`, `universe_change` events); explicit tables for underlying overrides, Kalshi series,
  settlement indexes (`cfb:BRTI`, `deribit:btc_usd`, `paradex:<BASE>`...) with per-market fixing methods, and fee models
  (`documented` / `unconfirmed`); a store partitioned by day or month with append-only snapshot parts
  compacted daily (`ContractStore`).
- **Derive adapter** (`derive`) over the public API v3 (no SDK): perps on the `Venue` protocol
  (instruments, hourly funding as a signed fraction per hour, specs; the venue keeps ~12 days of funding
  history) and options on `OptionsVenue` (contracts from `get_all_instruments` structured fields, chains
  from `get_tickers` per expiry with the abbreviated keys mapped by an explicit table, greeks =
  undiscounted Black-76 on the forward, public trades without trader identifiers, settlement prices of the
  ~12 days of expiries). Registry rule `<BASE>-PERP` and the XAUT gold-token override.
- **Deribit option chains** (`deribit`, `OptionsVenue` only) over the direct public API, not OpenBB:
  inverse BTC/ETH contracts (linear USDC options opt-in), chains from one book summary per currency with
  inverse prices converted to USD by the pass's index price and IV from percent to fraction (raw values
  kept), delivery prices since 2016 (`deribit:btc_usd`, 30-min TWAP per the venue docs), archive trades
  from `history.deribit.com` with IV at the trade.
- **Kalshi adapter** (`kalshi`, `BinaryVenue`) for the declared series KXBTCD, KXBTC, KXETHD and KXETH:
  markets with canonical bounds (`greater` -> [floor + 0.01, inf), `between` -> [floor, cap + 0.01),
  `less` -> (-inf, cap)) and the event's `strike_date` as expiry (tickers, in New York time, are never
  read), outcomes from `result` (recomputed from `expiration_value` on every settled market of the
  fixtures and of a live day), the fixing method read per market (2025 trimmed mean, 2026 simple mean),
  live quotes without invented sides, per-event and per-market candles (1m/1h/1d) and trades from both
  data tiers (current and historical, split at `/historical/cutoff`) under one schema, the YES order book,
  settlements per event, and the quadratic fee formula (`contracts.fees.kalshi_fee`, `unconfirmed`).
- **`qmd-snapshot`** (one scheduled pass: option chains of Derive and Deribit, Kalshi quotes, registry
  updates, `universe_change` events, a heartbeat per venue; a failing venue does not stop the others) and
  **`qmd-options-daily`** (registries with recent expiries, settlements -- Derive's ~12 days, Deribit's
  deliveries since 2016, Kalshi events of the last days --, settlement prices filled into expired
  contracts, Kalshi 1-minute candles, incremental option trades). Option and binary venues are discovered
  by capability, so Paradex options join without changing the command.
- Readers `load_chain`, `load_ladder`, `load_settlements`, `load_option_registry`, `load_binary_registry`
  and `settlement_comparison` (the daily index gap at the common 08:00 UTC fixing, in basis points).
- `download_venue` only lists perp instruments for a venue that declares `Dataset.INSTRUMENTS`.
- `Dataset` members for dated contracts (`OPTION_CONTRACTS`, `OPTION_CHAIN`, ..., `SETTLEMENTS`) and
  `PERP_DATASETS` / `CONTRACT_DATASETS`; Lighter now declares `PERP_DATASETS` instead of every dataset.
- **Paradex options** (`ParadexVenue` is now also an `OptionsVenue`, `venues/paradex_options.py`): the
  ~10k dated options of 14 underlyings in the canonical contract schemas, the name only cross-checked (day
  not zero-padded, decimal strikes written `712_5`, the expiry hour from `expiry_at`: 08:00 UTC for crypto,
  13:30-21:00 for the TradFi bases). Chains from one `markets/summary` call (Black-76 on
  `F = S exp(f T)`, greeks w.r.t. spot, a 365-day year: recomputed exactly in the tests), trades by base
  (expired contracts included), mark and underlying candles of live contracts, and fixings: the 30-min TWAP
  of the spot oracle served by `markets/settlement-price` for expiries from 2026-05-21, found through the
  registry or the `SETTLE_MARKET` trades (payoff kept as `settlement_payoff_raw`). Fee model
  `unconfirmed` (12.5 % premium cap documented, base of the rate inferred).
- `venues/paradex_snapshot.py`: `snapshot_pass` (full listing into the contract registry, append-only chain
  snapshots per underlying, heartbeat; ~9 s for BTC + ETH), `settle_expired` (one request per base and
  expiry) and `download_mark_candles` (monthly partitions), each step reported, none stopping the others.

### Changed
- Registry: a restricted session is part of the instrument id (`ADBE-USD-PERP-24X5`,
  `ADSK-USD-PERP-NOOVERNIGHT`); continuous and undeclared sessions add nothing. On Extended this changes
  the id of the 46 `NO_OVERNIGHT` stock perps (`ADSK-USD` was `ADSK-USD-PERP`) and resolves the 67
  `<TICKER>_24_5-USD` markets: the name is a listing batch, the declared session decides (`MSFT_24_5-USD`
  is continuous, hence `MSFT-USD-PERP`; `ADBE_24_5-USD` trades on weekdays, hence `ADBE-USD-PERP-24X5`).
  An unknown declared session leaves the market unresolved.
- Registry: Binance and Aster `BTCU` / `ETHU` (quote and margin asset `U`, a stablecoin) resolve to
  `BTC-U-PERP` / `ETH-U-PERP`, like the other second stablecoin books.
- Extended `underlying_ref` drops the `_24_5` listing suffix (`AAPL`, not `AAPL_24_5`).

## [0.3.0] — 2026-10-17

The multi-venue data layer: one `Venue` protocol for Lighter, Hyperliquid, Binance USD-M, Extended and Aster,
live order book snapshots and a websocket recorder. Additive only: v0.2 consumers keep working unchanged.

### Added
- **`quant_marketdata.venues`**, a venue-agnostic data layer: the `Venue` protocol with declared
  capabilities, canonical schemas (instruments, funding normalized per hour, last/mark candles, daily
  OI/volume, specs snapshots), an instrument registry with explicit overrides and point-in-time
  listing/delisting, an incremental Parquet store (`QUANT_MARKETDATA_ROOT`), a rate-limited stdlib JSON
  client with backoff, and the `qmd-download` command.
- **Lighter adapter** over the public REST API (no SDK): perps, candles (1m-1d), mark candles, hourly
  funding, daily open interest and volume, market specs.
- Optional `[store]` extra (pyarrow).
- **Venue adapters for Hyperliquid, Binance (USD-M futures), Extended and Aster**, on the Lighter
  template (stdlib JSON client, no SDK, quirks in each module docstring, parsers tested on recorded
  payloads): funding normalized to the canonical schema, last/mark candles, specs snapshots.
- Per-symbol funding intervals for Binance and Aster (`fundingInfo`), with each event's interval read from
  the gap to the previous settlement (capped at 8h) so that a symbol moved between 8h, 4h and 1h keeps a
  correct `rate_1h`.
- Registry rules for the four venues (`USDT` books collapse to `USD`, second stablecoin books keep their
  quote, `1000`/`1000000`/`1M`/`k` multipliers) and venue-declared asset classes (Binance
  `underlyingType`, Aster `underlyingSubType`, Extended `category`), plus overrides for gold tokens and
  Extended's mixed ETF/Index markets.
- `finalize_funding(..., max_interval_h=)` and `paging.forward_fetch` (cursor pagination).
- An equivalence test: the Binance and Hyperliquid adapters reproduce the daily means of
  `load_binance_funding` / `load_hl_funding` on a recorded window.

- **`quant_marketdata.venues.stream`**: continuous websocket recording of the order book (snapshot, deltas,
  hourly and daily checkpoints), trades (with `type`: trade, liquidation, deleverage, market-settlement) and
  market stats, into the shared store (`venues/<venue>/stream/…`), with the canonical `instrument_id` on every
  row. Venue-agnostic recorder (order book continuity by nonce chain, resubscription on gaps, reconnection
  with backoff, trade de-duplication, part files compacted daily, a `_health.json` freshness file) and a
  Lighter protocol, without the Lighter SDK. Ported from the Quant_Lighter prototype. `qmd-record run` and
  `qmd-record check` (live book vs REST snapshots, replayed files vs live book). New `[stream]` extra
  (`websockets`, `pyarrow`); the core does not import it.

- **`Dataset.BOOK`**: `order_book(symbol, depth)` on Lighter, Hyperliquid, Binance, Aster and Extended — a
  live snapshot `{"bids", "asks", "ts", "venue", "symbol"}` (best first, orders aggregated by price) that
  feeds `paper_execution` / `market_impact` directly; built by `schema.make_book`.

### Confirmed
- Lighter funding sign: `direction == "long"` means longs pay (websocket `funding_rate` is signed and
  matches the REST rate on every market checked).

### Unchanged
- The v0.2 loaders (`load_binance_funding`, `load_hl_funding`) and `funding_diff`: consumers keep working
  without re-pinning.

## [0.2.0] — 2026-08-02

### Added
- **Funding-data layer**: `binance_funding` + `hyperliquid_funding` daily loaders and `funding_diff`
  (`annualized_diff`, `cross_venue_diff`, `differential_carry_frame`) — the cross-venue funding differential
  shared by the research realism experiments and the live fund.
- Cache directory is resolved **at call time** (`cache_dir` arg → env `QUANT_FUNDING_CACHE` → `<cwd>/data/raw`),
  so the research lab and the fund share one 2019+ funding cache.

## [0.1.0] — 2026-08-02

Initial public release. The shared market-data & execution primitives, extracted from the Quant-lab program
so the realism experiments and the live fund depend on one source of truth.

### Added
- `market_impact`, `live_quotes`, `paper_execution`, `carry_cost` — the live-primitive layer.
- Optional `[live]` extra (ccxt) for the exchange-touching paths; the core depends only on pandas.

[0.3.0]: https://github.com/RoJLD/quant-marketdata/releases/tag/v0.3.0
[0.2.0]: https://github.com/RoJLD/quant-marketdata/releases/tag/v0.2.0
[0.1.0]: https://github.com/RoJLD/quant-marketdata/releases/tag/v0.1.0
