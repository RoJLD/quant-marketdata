# Changelog

All notable changes to **quant-marketdata** are documented here.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) · Versioning: [SemVer](https://semver.org/).
The shared market- and funding-data layer of the platform; the cross-repo dependency map lives in the
Quant-lab repo's `docs/PLATFORM.md`.

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

[0.2.0]: https://github.com/RoJLD/quant-marketdata/releases/tag/v0.2.0
[0.1.0]: https://github.com/RoJLD/quant-marketdata/releases/tag/v0.1.0
