"""Snapshot passes for dated contracts: what the venues only serve live, recorded into the store.

    qmd-snapshot                      # one pass: option chains + binary quotes, registries, heartbeats
    qmd-options-daily                 # registries with recent expiries, settlements, candles, trades,
                                      # mark candles of the live options (Paradex)

A pass is one scheduled task that exits (no orphan process); a failing venue never stops the others and
the exit code is 1 if any failed. Each pass starts from one ``ts_snapshot`` (local UTC clock), updates the
contract registries (``universe_change`` events appended), appends the snapshot parts and writes a
heartbeat per venue (``venues/<venue>/_snapshot_health.json``, the freshness source of L0). Parts of
previous days are compacted at the start of every pass.

Defaults kept from the spec's open questions (§6, validated 2026-10-18):

- Q1 cadence and universe: BTC and ETH chains every 15 minutes (:00, :15, :30, :45) plus a pass at 07:58 UTC
  (last quotes and Paradex underlying before the 08:00 fixing); widen (SOL, HYPE, XAU...) only after the
  first week's disk measurement. Kalshi: the four declared series.
- Q2 host: a missed pass is lost for Derive and Paradex (live-only data); nothing tries to backfill it.
- Q6 Deribit: inverse BTC/ETH chains only; linear USDC options are not snapshotted.
- Q7 the 07:15-07:58 passes for the exact 08:00 pair are kept (they are the 15-minute cadence plus 07:58).
- Lighter options/binaries: not snapshotted until listed (spec §3.7.5); Paradex options join automatically
  once its adapter declares ``Dataset.OPTION_CHAIN`` (option venues are discovered by capability).

The daily task (before 08:00 UTC): option registries with their recent expiries (Derive ``expired=true``
reaches ~11 days back, Deribit only the last expiry) and settlement prices (Derive: ~12 days of expiries;
Deribit: deliveries since 2016, incremental), Kalshi markets settled over the last days (outcomes, the
settlement of each event and its 1-minute candles from the event endpoint), incremental option trades, and
the hourly mark candles of every live option of the venues serving them (Paradex, only until expiry:
``paradex_snapshot.download_mark_candles``). That step has its own client (``--mark-candles-rate``: about one
request per live contract and day, ~1,000 per base) and a time budget per underlying
(``--mark-candles-minutes``): soonest expiries first, the contracts left are fetched by the next run.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from typing import Callable, Dict, List, Optional, Sequence

import pandas as pd

import quant_marketdata.venues  # noqa: F401  (registers every adapter)
from quant_marketdata.venues.base import available_venues, get_venue
from quant_marketdata.venues.contracts.registry import (
    binary_settlements,
    resolve_binary_contracts,
    resolve_option_contracts,
    universe_changes,
    update_contract_registry,
)
from quant_marketdata.venues.contracts.store import ContractStore, day_of
from quant_marketdata.venues.paradex_snapshot import download_mark_candles
from quant_marketdata.venues.schema import Dataset

logger = logging.getLogger(__name__)

DEFAULT_UNDERLYINGS = ("BTC", "ETH")
DEFAULT_SERIES = ("KXBTCD", "KXBTC", "KXETHD", "KXETH")
DEFAULT_SINCE_DAYS = 2
SETTLEMENT_KEYS = ["ts", "settlement_index_id"]
CANDLE_BATCH_EVENTS = 24  # Kalshi events fetched between two writes of a monthly candle file
# Option mark candles: the task's other steps take 13-22 minutes of the host's 30 (2026-10-23..26).
MARK_CANDLES_BUDGET = pd.Timedelta(minutes=3)  # per underlying
MARK_CANDLES_RATE = 10.0  # requests per second (Paradex: 1,500 a minute per IP)


@dataclass
class StepResult:
    venue: str
    step: str
    key: str
    rows: int = 0
    unresolved: int = 0
    error: Optional[str] = None
    deferred: int = 0  # series left to the next run by a time budget


@dataclass
class VenueRun:
    venue: str
    steps: List[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.error is None for s in self.steps)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def venues_with(dataset: Dataset, names: Optional[Sequence[str]] = None) -> List[str]:
    """Registered venues declaring ``dataset`` (option chains: Derive, Deribit, later Paradex...)."""
    candidates = names if names is not None else available_venues()
    return [n for n in candidates if dataset in get_venue(n).capabilities]


def _run(run: VenueRun, step: str, key: str, job: Callable[[], StepResult]) -> Optional[StepResult]:
    try:
        result = job()
    except Exception as exc:  # one failing step must not stop the others
        result = StepResult(run.venue, step, key, error=f"{type(exc).__name__}: {exc}".splitlines()[0])
        logger.warning("%s %s %s failed: %s", run.venue, step, key, result.error)
    else:
        logger.info("%s %s %s: %d rows (%d unresolved)", run.venue, step, key, result.rows, result.unresolved)
    run.steps.append(result)
    return result


def _log_unresolved(venue: str, unresolved: pd.DataFrame) -> None:
    for row in unresolved.head(20).to_dict("records"):
        logger.warning("Unresolved %s %s: %s", venue, row["venue_symbol"], row["reason"])
    if len(unresolved) > 20:
        logger.warning("... and %d more unresolved %s contracts", len(unresolved) - 20, venue)


# -- registries --------------------------------------------------------------------------------------


def update_option_registry(store: ContractStore, venue, underlying: str, as_of: pd.Timestamp, *,
                           expired: bool = False):
    """List one underlying's contracts (live, plus recent expiries when ``expired``), resolve them and merge
    them into ``registry/options/<venue>.parquet``. Returns ``(build, merged registry)``."""
    listing = venue.option_contracts(underlying)
    if expired:
        listing = pd.concat([listing, venue.option_contracts(underlying, expired=True)], ignore_index=True)
        listing = listing.drop_duplicates("venue_symbol", keep="first")
    build = resolve_option_contracts(venue.name, listing)
    _log_unresolved(venue.name, build.unresolved)
    path = store.option_registry_path(venue.name)
    existing = store.read(path)
    merged = update_contract_registry(existing, build.contracts, as_of, scope=("venue_base", [underlying]))
    store.write(path, merged)
    store.append_universe_changes(venue.name, universe_changes(existing, merged, as_of))
    return build, merged


def update_binary_registry(store: ContractStore, venue_name: str, series: str, listing: pd.DataFrame,
                           as_of: pd.Timestamp):
    """Resolve a binary listing and merge it into the monthly registry partitions of its series."""
    build = resolve_binary_contracts(venue_name, listing)
    _log_unresolved(venue_name, build.unresolved)
    contracts = build.contracts
    if contracts.empty:
        return build
    for month, rows in contracts.groupby(contracts["expiry"].dt.strftime("%Y-%m"), sort=True):
        path = store.binary_registry_path(venue_name, series, month)
        existing = store.read(path)
        merged = update_contract_registry(existing, rows, as_of,
                                          scope=("venue_event_id", rows["venue_event_id"].unique()))
        store.write(path, merged)
        store.append_universe_changes(venue_name, universe_changes(existing, merged, as_of))
    return build


def _ids(contracts: pd.DataFrame) -> Dict[str, str]:
    return dict(zip(contracts["venue_symbol"], contracts["contract_id"], strict=True)) if not contracts.empty else {}


# -- one pass ----------------------------------------------------------------------------------------


def snapshot_options(store: ContractStore, venue, underlyings: Sequence[str], ts_snapshot: pd.Timestamp,
                     run: VenueRun) -> None:
    for underlying in underlyings:
        def job(u=underlying) -> StepResult:
            build, _ = update_option_registry(store, venue, u, ts_snapshot)
            ids = build.contracts["underlying_id"].dropna().unique() if not build.contracts.empty else []
            if len(ids) != 1:
                raise ValueError(f"{u}: underlying not resolved to one canonical id ({list(ids)})")
            chain = venue.option_chain(u)
            chain["ts_snapshot"] = ts_snapshot
            chain["contract_id"] = chain["venue_symbol"].map(_ids(build.contracts))
            store.append_snapshot(venue.name, Dataset.OPTION_CHAIN, ids[0], chain)
            return StepResult(venue.name, "option_chain", ids[0], len(chain), len(build.unresolved))

        _run(run, "option_chain", underlying, job)


def snapshot_binaries(store: ContractStore, venue, series_list: Sequence[str], ts_snapshot: pd.Timestamp,
                      run: VenueRun) -> None:
    for series in series_list:
        def job(s=series) -> StepResult:
            build = update_binary_registry(store, venue.name, s, venue.binary_contracts(s), ts_snapshot)
            quotes = venue.binary_quotes(s)
            quotes["ts_snapshot"] = ts_snapshot
            quotes["contract_id"] = quotes["venue_symbol"].map(_ids(build.contracts))
            store.append_snapshot(venue.name, Dataset.BINARY_QUOTES, s, quotes)
            return StepResult(venue.name, "binary_quotes", s, len(quotes), len(build.unresolved))

        _run(run, "binary_quotes", series, job)


def _heartbeat(store: ContractStore, run: VenueRun, ts_snapshot: pd.Timestamp, kind: str) -> None:
    store.write_heartbeat(run.venue, {
        "venue": run.venue,
        "kind": kind,
        "ts_snapshot": ts_snapshot.isoformat(),
        "finished_at": _now().isoformat(),
        "ok": run.ok,
        "steps": [asdict(s) for s in run.steps],
    })


def snapshot_pass(*, root=None, venues: Optional[Sequence[str]] = None,
                  underlyings: Sequence[str] = DEFAULT_UNDERLYINGS, series: Sequence[str] = DEFAULT_SERIES,
                  rate: float = 2.0, factories: Optional[Dict[str, Callable]] = None,
                  ts_snapshot: Optional[pd.Timestamp] = None) -> List[VenueRun]:
    """One ``qmd-snapshot`` pass over the option venues (chains) and binary venues (quotes)."""
    store = ContractStore.at(root)
    ts_snapshot = _now() if ts_snapshot is None else pd.Timestamp(ts_snapshot)
    for done in store.compact_before(day_of(ts_snapshot)):
        logger.info("Compacted %s %s %s %s: %d rows", *done)
    make = factories or {}
    names = list(venues) if venues is not None else sorted(
        set(venues_with(Dataset.OPTION_CHAIN)) | set(venues_with(Dataset.BINARY_QUOTES)))
    runs = []
    for name in names:
        venue = make[name]() if name in make else get_venue(name, rate=rate)
        run = VenueRun(name)
        if Dataset.OPTION_CHAIN in venue.capabilities:
            snapshot_options(store, venue, underlyings, ts_snapshot, run)
        if Dataset.BINARY_QUOTES in venue.capabilities:
            snapshot_binaries(store, venue, series, ts_snapshot, run)
        _heartbeat(store, run, ts_snapshot, "snapshot")
        runs.append(run)
    return runs


# -- daily task --------------------------------------------------------------------------------------


def store_settlements(store: ContractStore, venue_name: str, frame: pd.DataFrame,
                      seen_at: Optional[pd.Timestamp] = None) -> int:
    """Merge fixings into ``settlements/<index>.parquet`` (stored rows win: ``first_seen_at`` is kept);
    ``seen_at`` (the pass's clock) stamps the new ones."""
    if seen_at is not None and not frame.empty:
        frame = frame.assign(first_seen_at=pd.Timestamp(seen_at))
    rows = 0
    for index_id, group in frame.groupby("settlement_index_id", sort=True):
        rows += store.merge(store.settlements_path(venue_name, index_id), group, SETTLEMENT_KEYS,
                            sort=("ts",), keep="first")
    return rows


def fill_registry_settlements(store: ContractStore, venue_name: str, settlements: pd.DataFrame,
                              as_of: pd.Timestamp) -> int:
    """Complete expired option contracts with the fixing of their index at their expiry."""
    path = store.option_registry_path(venue_name)
    registry = store.read(path)
    if registry.empty or settlements.empty:
        return 0
    prices = settlements.set_index(["settlement_index_id", "ts"])["settlement_price"]
    keys = pd.MultiIndex.from_arrays([registry["settlement_index_id"], registry["expiry"]])
    found = pd.Series(prices.reindex(keys).to_numpy(), index=registry.index)
    fill = registry["settlement_price"].isna() & found.notna()
    if not fill.any():
        return 0
    registry["settlement_price"] = registry["settlement_price"].where(~fill, found)
    registry["settlement_source"] = registry["settlement_source"].astype("object").where(
        ~fill, settlements["source_endpoint"].iloc[0])
    registry["settlement_seen_at"] = registry["settlement_seen_at"].where(~fill, as_of)
    store.write(path, registry)
    return int(fill.sum())


def _settlement_indexes(contracts: pd.DataFrame) -> List[str]:
    if contracts.empty:
        return []
    return sorted(contracts["settlement_index_raw"].dropna().unique())


def _trades_since(store: ContractStore, venue_name: str, key: str, default: pd.Timestamp) -> pd.Timestamp:
    month = store.last_partition(venue_name, Dataset.OPTION_TRADES, key)
    if month is None:
        return default
    stored = store.read(store.partition_path(venue_name, Dataset.OPTION_TRADES, key, month))
    return stored["ts"].max() if not stored.empty else default


def daily_options(store: ContractStore, venue, underlyings: Sequence[str], as_of: pd.Timestamp, run: VenueRun,
                  *, since: pd.Timestamp, trades: bool = True) -> None:
    for underlying in underlyings:
        state: Dict[str, object] = {}

        def registry_job(u=underlying, state=state) -> StepResult:
            build, _ = update_option_registry(store, venue, u, as_of, expired=True)
            state["contracts"] = build.contracts
            return StepResult(venue.name, "option_registry", u, len(build.contracts), len(build.unresolved))

        if _run(run, "option_registry", underlying, registry_job).error:
            continue
        contracts: pd.DataFrame = state["contracts"]
        for index in _settlement_indexes(contracts):
            def settle_job(i=index, contracts=contracts) -> StepResult:
                path_id = contracts.loc[contracts["settlement_index_raw"] == i, "settlement_index_id"].iloc[0]
                stored = store.read(store.settlements_path(venue.name, path_id))
                start = stored["ts"].max() if not stored.empty else None
                frame = venue.settlements(i, start=start)
                rows = store_settlements(store, venue.name, frame, seen_at=as_of)
                filled = fill_registry_settlements(store, venue.name, store.read(
                    store.settlements_path(venue.name, path_id)), as_of)
                logger.info("%s %s: %d contracts completed with their settlement", venue.name, i, filled)
                return StepResult(venue.name, "settlements", i, rows)

            _run(run, "settlements", index, settle_job)
        underlying_ids = contracts["underlying_id"].dropna().unique() if not contracts.empty else []
        if trades and Dataset.OPTION_TRADES in venue.capabilities and len(underlying_ids) == 1:
            def trades_job(u=underlying, key=underlying_ids[0], contracts=contracts) -> StepResult:
                start = _trades_since(store, venue.name, key, since)
                frame = venue.option_trades(u, start, as_of)
                frame["contract_id"] = frame["venue_symbol"].map(_ids(contracts))
                store.merge_monthly(venue.name, Dataset.OPTION_TRADES, key, frame, ["trade_id"])
                return StepResult(venue.name, "option_trades", key, len(frame))

            _run(run, "option_trades", underlying, trades_job)


def daily_binaries(store: ContractStore, venue, series_list: Sequence[str], as_of: pd.Timestamp, run: VenueRun,
                   *, since: pd.Timestamp, candles: bool = True) -> None:
    for series in series_list:
        state: Dict[str, object] = {}

        def settled_job(s=series, state=state) -> StepResult:
            listing = venue.binary_contracts(s, status="settled", start=since, end=as_of)
            build = update_binary_registry(store, venue.name, s, listing, as_of)
            state["contracts"] = build.contracts
            frame = binary_settlements(build.contracts, source_endpoint=f"{venue.name}.binary_contracts")
            rows = store_settlements(store, venue.name, frame, seen_at=as_of) if not frame.empty else 0
            return StepResult(venue.name, "binary_settlements", s, rows, len(build.unresolved))

        if _run(run, "binary_settlements", series, settled_job).error or not candles:
            continue
        contracts: pd.DataFrame = state["contracts"]
        if contracts.empty:
            continue
        pending: List[pd.DataFrame] = []

        def write_job(s=series, pending=pending) -> StepResult:
            # One merge per batch of events: a monthly file is rewritten once, not once per event.
            frame = pd.concat(pending, ignore_index=True)
            pending.clear()
            store.merge_monthly(venue.name, Dataset.BINARY_CANDLES, s, frame, ["venue_symbol", "ts"],
                                resolution="1m")
            return StepResult(venue.name, "binary_candles_write", s, len(frame))

        for event, group in contracts.groupby("venue_event_id", sort=True):
            def candles_job(e=event, g=group, s=series, pending=pending) -> StepResult:
                frame = venue.binary_candles(e, "1m", g["trading_open"].min(), g["expiry"].max(), series=s)
                frame["contract_id"] = frame["venue_symbol"].map(_ids(g))
                if not frame.empty:
                    pending.append(frame)
                return StepResult(venue.name, "binary_candles", e, len(frame))

            _run(run, "binary_candles", event, candles_job)
            if len(pending) >= CANDLE_BATCH_EVENTS:
                _run(run, "binary_candles_write", series, write_job)
        if pending:
            _run(run, "binary_candles_write", series, write_job)


def daily_mark_candles(store: ContractStore, venue, underlyings: Sequence[str], as_of: pd.Timestamp,
                       run: VenueRun, *, budget: Optional[pd.Timedelta]) -> None:
    """Mark candles of the live options of each underlying, served only until expiry, within ``budget`` per
    underlying; a contract that fails is reported, the others are stored."""
    for underlying in underlyings:
        def job(u=underlying) -> StepResult:
            report = download_mark_candles(venue, store, u, as_of=as_of, budget=budget)
            errors, error = report["errors"], None
            if errors:
                symbol, message = next(iter(errors.items()))
                error = f"{len(errors)} of {report['contracts'] + 1} series failed, e.g. {symbol}: {message}"
                logger.warning("%s option_mark_candles %s: %s", venue.name, u, error)
            if report["deferred"]:
                logger.warning("%s option_mark_candles %s: budget spent, %d of %d series left to the next run",
                               venue.name, u, report["deferred"], report["contracts"] + 1)
            return StepResult(venue.name, "option_mark_candles", report["underlying"], report["rows"],
                              error=error, deferred=report["deferred"])

        _run(run, "option_mark_candles", underlying, job)


def daily_task(*, root=None, venues: Optional[Sequence[str]] = None,
               underlyings: Sequence[str] = DEFAULT_UNDERLYINGS, series: Sequence[str] = DEFAULT_SERIES,
               since_days: int = DEFAULT_SINCE_DAYS, trades: bool = True, candles: bool = True,
               mark_candles: bool = True, mark_candles_budget: Optional[pd.Timedelta] = MARK_CANDLES_BUDGET,
               mark_candles_rate: float = MARK_CANDLES_RATE, rate: float = 2.0,
               factories: Optional[Dict[str, Callable]] = None,
               as_of: Optional[pd.Timestamp] = None) -> List[VenueRun]:
    """The ``qmd-options-daily`` task: what the venues forget after a few days, plus Kalshi's recent past."""
    store = ContractStore.at(root)
    as_of = _now() if as_of is None else pd.Timestamp(as_of)
    since = as_of.normalize() - pd.Timedelta(days=since_days)
    for done in store.compact_before(day_of(as_of)):
        logger.info("Compacted %s %s %s %s: %d rows", *done)
    make = factories or {}
    names = list(venues) if venues is not None else sorted(
        set(venues_with(Dataset.OPTION_CONTRACTS)) | set(venues_with(Dataset.BINARY_CONTRACTS)))
    runs = []
    for name in names:
        venue = make[name]() if name in make else get_venue(name, rate=rate)
        run = VenueRun(name)
        if Dataset.OPTION_CONTRACTS in venue.capabilities:
            daily_options(store, venue, underlyings, as_of, run, since=since, trades=trades)
        if mark_candles and Dataset.OPTION_MARK_CANDLES in venue.capabilities:
            # Its own client: one request per live contract, paced at mark_candles_rate, not rate.
            fast = make[name]() if name in make else get_venue(name, rate=mark_candles_rate)
            daily_mark_candles(store, fast, underlyings, as_of, run, budget=mark_candles_budget)
        if Dataset.BINARY_CONTRACTS in venue.capabilities:
            daily_binaries(store, venue, series, as_of, run, since=since, candles=candles)
        _heartbeat(store, run, as_of, "daily")
        runs.append(run)
    return runs


# -- command line ------------------------------------------------------------------------------------


def _parser(description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--root", help="Store root (default: $QUANT_MARKETDATA_ROOT or ./data/marketdata).")
    parser.add_argument("--venues", nargs="+", help="Venues (default: every venue serving the datasets).")
    parser.add_argument("--underlyings", nargs="+", default=list(DEFAULT_UNDERLYINGS),
                        help="Native option bases (default: BTC ETH).")
    parser.add_argument("--series", nargs="+", default=list(DEFAULT_SERIES),
                        help="Binary series (default: KXBTCD KXBTC KXETHD KXETH).")
    parser.add_argument("--rate", type=float, default=2.0, help="Max requests per second per venue.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log retries.")
    return parser


def _report(runs: List[VenueRun], started: float) -> int:
    failed = [s for r in runs for s in r.steps if s.error]
    rows = sum(s.rows for r in runs for s in r.steps)
    print(f"Done in {time.monotonic() - started:.0f}s: {len(runs)} venues, {rows} rows, {len(failed)} failed steps")
    for s in failed:
        print(f"  FAILED {s.venue} {s.step} {s.key}: {s.error}")
    return 1 if failed else 0


def _logging(verbose: bool) -> None:
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser("One snapshot pass of option chains and binary quotes (qmd-snapshot).").parse_args(argv)
    _logging(args.verbose)
    started = time.monotonic()
    runs = snapshot_pass(root=args.root, venues=args.venues, underlyings=args.underlyings, series=args.series,
                         rate=args.rate)
    return _report(runs, started)


def daily_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = _parser("Daily task: registries with recent expiries, settlements, candles, trades.")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS,
                        help="Days of settled Kalshi events and of first option trades (default: 2).")
    parser.add_argument("--no-trades", action="store_true", help="Skip option trades.")
    parser.add_argument("--no-candles", action="store_true", help="Skip Kalshi candles.")
    parser.add_argument("--no-mark-candles", action="store_true", help="Skip option mark candles (Paradex).")
    parser.add_argument("--mark-candles-minutes", type=float,
                        default=MARK_CANDLES_BUDGET.total_seconds() / 60,
                        help="Time budget of the option mark candles per underlying (default: 3); the "
                             "contracts left are fetched by the next run.")
    parser.add_argument("--mark-candles-rate", type=float, default=MARK_CANDLES_RATE,
                        help="Max requests per second of the option mark candles (default: 10).")
    args = parser.parse_args(argv)
    _logging(args.verbose)
    started = time.monotonic()
    runs = daily_task(root=args.root, venues=args.venues, underlyings=args.underlyings, series=args.series,
                      since_days=args.since_days, trades=not args.no_trades, candles=not args.no_candles,
                      mark_candles=not args.no_mark_candles,
                      mark_candles_budget=pd.Timedelta(minutes=args.mark_candles_minutes),
                      mark_candles_rate=args.mark_candles_rate, rate=args.rate)
    return _report(runs, started)


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "StepResult",
    "VenueRun",
    "daily_main",
    "daily_mark_candles",
    "daily_task",
    "fill_registry_settlements",
    "main",
    "snapshot_pass",
    "store_settlements",
    "update_binary_registry",
    "update_option_registry",
    "venues_with",
]
