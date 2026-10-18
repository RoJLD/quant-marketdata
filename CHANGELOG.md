# Changelog

All notable changes to **quant-marketdata** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versioning: [SemVer](https://semver.org/).
The shared market- and funding-data layer of the platform; the cross-repo dependency map lives in the
Quant-lab repo's `docs/PLATFORM.md`.

## [Unreleased]

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
  last 12 expiries). Registry rule `<BASE>-PERP` and the XAUT gold-token override.
- `Dataset` members for dated contracts (`OPTION_CONTRACTS`, `OPTION_CHAIN`, ..., `SETTLEMENTS`) and
  `PERP_DATASETS` / `CONTRACT_DATASETS`; Lighter now declares `PERP_DATASETS` instead of every dataset.

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
