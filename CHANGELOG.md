# Changelog

All notable changes to **quant-marketdata** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versioning: [SemVer](https://semver.org/).
The shared market- and funding-data layer of the platform; the cross-repo dependency map lives in the
Quant-lab repo's `docs/PLATFORM.md`.

## [Unreleased]

### Added
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
- The v0.2 loaders and `funding_diff`.

## [0.3.0] — 2026-10-15

### Added
- **`quant_marketdata.venues`**, a venue-agnostic data layer: the `Venue` protocol with declared
  capabilities, canonical schemas (instruments, funding normalized per hour, last/mark candles, daily
  OI/volume, specs snapshots), an instrument registry with explicit overrides and point-in-time
  listing/delisting, an incremental Parquet store (`QUANT_MARKETDATA_ROOT`), a rate-limited stdlib JSON
  client with backoff, and the `qmd-download` command.
- **Lighter adapter** over the public REST API (no SDK): perps, candles (1m-1d), mark candles, hourly
  funding, daily open interest and volume, market specs.
- Optional `[store]` extra (pyarrow).

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
