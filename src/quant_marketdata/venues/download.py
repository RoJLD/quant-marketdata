"""Incremental download of one venue's history into the store, and its command line.

    python -m quant_marketdata.venues.download --venue lighter --symbols BTC ETH --datasets funding candles
    python -m quant_marketdata.venues.download --venues bybit okx bitget gate --datasets funding --since 2026-10-27

Each series resumes from its last stored timestamp (that row is fetched again: the latest candle may have
been incomplete). Long ranges are fetched in chunks merged to disk one by one, so an interrupted run keeps
its progress. Delisted markets are included by default to avoid survivorship bias; a market delisted before
the start of its series is skipped (nothing to fetch). A venue serving its funding over a rolling window
(``funding_window``: OKX, Bitget) starts a first download at the window (minus ``WINDOW_MARGIN``) and cannot
fill a series that resumes before it: such a series is logged, and ``--raw-backfill`` first merges what the raw
archive holds (``venues.raw_archive``). Ids that are crypto on one venue and something else on another
(``registry.class_conflicts``) are logged after each registry update.
"""

from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, List, Optional, Sequence

import pandas as pd

from quant_marketdata.venues.base import WINDOW_MARGIN, Venue, get_venue
from quant_marketdata.venues.registry import class_conflicts, resolve, update_registry
from quant_marketdata.venues.schema import Dataset
from quant_marketdata.venues.store import VenueStore

logger = logging.getLogger(__name__)

# Points per chunk written to disk (20 API pages of 500).
CHUNK_POINTS = 10_000
FUNDING_STEP_S = 3600
DEFAULT_DATASETS = (Dataset.FUNDING, Dataset.CANDLES, Dataset.MARK_CANDLES, Dataset.DAILY_METRICS)


@dataclass
class DownloadResult:
    venue: str
    symbol: str
    dataset: str
    new_rows: int = 0
    total_rows: int = 0
    error: Optional[str] = None


@dataclass
class DownloadPlan:
    venue: str
    symbols: Optional[Sequence[str]] = None
    datasets: Sequence[Dataset] = DEFAULT_DATASETS
    resolutions: Optional[Sequence[str]] = None  # None: every resolution the venue supports
    mark_resolutions: Optional[Sequence[str]] = None  # None: same as ``resolutions``
    since: Optional[pd.Timestamp] = None
    active_only: bool = False
    specs: bool = True
    registry: bool = True
    raw_backfill: bool = False  # merge <root>/_raw/<venue>/funding first (venues.raw_archive)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def _chunks(start: pd.Timestamp, end: pd.Timestamp, span: pd.Timedelta):
    cursor = start
    while cursor < end:
        nxt = min(cursor + span, end)
        yield cursor, nxt
        cursor = nxt


def download_series(
    store: VenueStore,
    venue: Venue,
    symbol: str,
    dataset: Dataset,
    fetch: Callable[[datetime, datetime], pd.DataFrame],
    *,
    start: pd.Timestamp,
    end: pd.Timestamp,
    step_s: int,
    resolution: Optional[str] = None,
    served_from: Optional[pd.Timestamp] = None,
) -> DownloadResult:
    """Fetch ``[resume point, end]`` chunk by chunk and merge each chunk into the store.

    ``served_from``: the start of the venue's rolling window (a floor: the venue may serve a little more); a
    stored series that ends before it may have lost the span in between, which is logged as a warning. The
    start is not clamped: the venue's pager stops where its history does."""
    path = store.path(venue.name, dataset, symbol, resolution)
    label = dataset.value if resolution is None else f"{dataset.value}/{resolution}"
    before = store.row_count(path)
    last = store.last_timestamp(path)
    if last is not None:
        start = last
        if served_from is not None and last < served_from:
            logger.warning("%s %s %s: stored until %s, older than the venue's window (from about %s): what it "
                           "no longer serves is lost unless a raw archive holds it (--raw-backfill)",
                           venue.name, symbol, label, last, served_from)
    span = pd.Timedelta(seconds=step_s * CHUNK_POINTS)
    for a, b in _chunks(start, end, span):
        chunk = fetch(a, b)
        if not chunk.empty:
            store.merge(path, chunk)
    total = store.row_count(path)
    return DownloadResult(venue.name, symbol, label, total - before, total)


def snapshot_specs(store: VenueStore, venue: Venue, as_of: pd.Timestamp) -> DownloadResult:
    """Keep one specs row per instrument and UTC day, so fee or margin changes can be replayed."""
    path = store.path(venue.name, Dataset.SPECS)
    specs = venue.specs()
    specs.insert(0, "snapshot_date", as_of.normalize())
    existing = store.read(path)
    before = len(existing)
    key = "symbol" if "symbol" in specs.columns else specs.columns[1]
    merged = pd.concat([existing, specs], ignore_index=True) if before else specs
    merged = merged.drop_duplicates(["snapshot_date", key], keep="last")
    merged = merged.sort_values(["snapshot_date", key]).reset_index(drop=True)
    store.write(path, merged)
    return DownloadResult(venue.name, "*", Dataset.SPECS.value, len(merged) - before, len(merged))


def download_venue(plan: DownloadPlan, *, root=None, rate: float = 2.0,
             venue: Optional[Venue] = None) -> List[DownloadResult]:
    venue = venue or get_venue(plan.venue, rate=rate)
    store = VenueStore.at(root)
    end = _now()
    results: List[DownloadResult] = []

    if Dataset.INSTRUMENTS not in venue.capabilities:
        logger.info("%s lists no perp instruments: nothing for qmd-download (dated contracts: qmd-snapshot)",
                    venue.name)
        return results
    instruments = venue.instruments()
    if plan.registry:
        build = resolve(venue.name, instruments)
        registry = update_registry(store.read(store.registry_path()), build, venue.name, end)
        store.write(store.registry_path(), registry)
        logger.info("Registry: %d %s instruments, %d unresolved", len(build.instruments), venue.name,
                    len(build.unresolved))
        for row in build.unresolved.to_dict("records"):
            logger.warning("Unresolved %s %s: %s", row["venue"], row["venue_symbol"], row["reason"])
        conflicts = class_conflicts(registry)
        touched = conflicts[conflicts["instrument_id"].isin(
            conflicts.loc[conflicts["venue"] == venue.name, "instrument_id"])]
        for instrument, group in touched.groupby("instrument_id"):
            logger.warning("Id %s carries several asset classes: %s", instrument, ", ".join(
                f"{r.venue} {r.venue_symbol} {r.asset_class}" for r in group.itertuples()))
    if plan.specs and Dataset.SPECS in venue.capabilities:
        results.append(snapshot_specs(store, venue, end))
    if plan.raw_backfill and Dataset.FUNDING in plan.datasets:
        from quant_marketdata.venues.raw_archive import backfill_funding  # imports this module

        results += backfill_funding(venue, root=root, symbols=plan.symbols)

    selected = instruments
    if plan.symbols:
        unknown = sorted(set(plan.symbols) - set(instruments["venue_symbol"]))
        if unknown:
            raise ValueError(f"Unknown {venue.name} symbols: {', '.join(unknown)}")
        selected = instruments[instruments["venue_symbol"].isin(plan.symbols)]
    if plan.active_only:
        selected = selected[selected["status"] == "active"]

    resolutions = list(plan.resolutions or venue.resolutions)
    mark_resolutions = resolutions if plan.mark_resolutions is None else list(plan.mark_resolutions)
    window = getattr(venue, "funding_window", None)
    served_from = end - window if window is not None else None
    jobs, gone = [], 0
    for row in selected.to_dict("records"):
        symbol = row["venue_symbol"]
        listed = row["listed_at"] if pd.notna(row["listed_at"]) else pd.Timestamp("2015-01-01")
        start = max(listed, plan.since) if plan.since is not None else listed
        delisted = row.get("delisted_at")
        if delisted is not None and pd.notna(delisted) and start > delisted:
            gone += 1
            continue
        for dataset in plan.datasets:
            if dataset not in venue.capabilities:
                continue
            if dataset is Dataset.FUNDING:
                first = start if served_from is None else max(start, served_from - WINDOW_MARGIN)
                jobs.append((symbol, dataset.value, lambda s=symbol, t=first: download_series(
                    store, venue, s, Dataset.FUNDING, lambda a, b, s=s: venue.funding(s, a, b),
                    start=t, end=end, step_s=FUNDING_STEP_S, served_from=served_from)))
            elif dataset in (Dataset.CANDLES, Dataset.MARK_CANDLES):
                price = "last" if dataset is Dataset.CANDLES else "mark"
                for res in resolutions if dataset is Dataset.CANDLES else mark_resolutions:
                    step = _resolution_seconds(res)
                    jobs.append((symbol, f"{dataset.value}/{res}",
                                 lambda s=symbol, t=start, r=res, p=price, d=dataset, st=step: download_series(
                                     store, venue, s, d,
                                     lambda a, b, s=s, r=r, p=p: venue.candles(s, r, a, b, price=p),
                                     start=t, end=end, step_s=st, resolution=r)))
            elif dataset is Dataset.DAILY_METRICS:
                jobs.append((symbol, dataset.value, lambda s=symbol: _download_metrics(store, venue, s)))

    logger.info("%s: %d instruments, %d series (%d delisted before their start, skipped)", venue.name,
                len(selected), len(jobs), gone)
    for index, (symbol, label, job) in enumerate(jobs, 1):
        try:
            result = job()
        except Exception as exc:  # one failing series must not stop the others
            result = DownloadResult(venue.name, symbol, label,
                                    error=f"{type(exc).__name__}: {exc}".splitlines()[0])
            logger.warning("[%d/%d] %s %s failed: %s", index, len(jobs), symbol, label, result.error)
        else:
            logger.info("[%d/%d] %s %s: +%d rows (%d total)", index, len(jobs), symbol, label,
                        result.new_rows, result.total_rows)
        results.append(result)
    return results


def _download_metrics(store: VenueStore, venue: Venue, symbol: str) -> DownloadResult:
    path = store.path(venue.name, Dataset.DAILY_METRICS, symbol)
    before = store.row_count(path)
    total = store.merge(path, venue.daily_metrics(symbol))
    return DownloadResult(venue.name, symbol, Dataset.DAILY_METRICS.value, total - before, total)


def _resolution_seconds(resolution: str) -> int:
    return int(pd.Timedelta(resolution.replace("m", "min")).total_seconds())


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Download venue history into the quant-marketdata store.")
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--venue", help="Venue name, e.g. lighter.")
    which.add_argument("--venues", nargs="+",
                       help="Several venues, one after the other; a failing venue does not stop the others.")
    parser.add_argument("--root", help="Store root (default: $QUANT_MARKETDATA_ROOT or ./data/marketdata).")
    parser.add_argument("--symbols", nargs="+", help="Native symbols (default: every instrument).")
    parser.add_argument("--datasets", nargs="+", default=[d.value for d in DEFAULT_DATASETS],
                        choices=[d.value for d in DEFAULT_DATASETS], help="Time series to download.")
    parser.add_argument("--resolutions", nargs="+", help="Candle resolutions (default: all supported).")
    parser.add_argument("--mark-resolutions", nargs="*",
                        help="Mark candle resolutions (default: same as --resolutions).")
    parser.add_argument("--since", type=pd.Timestamp, help="Earliest UTC date for a first download.")
    parser.add_argument("--active-only", action="store_true", help="Skip delisted instruments.")
    parser.add_argument("--no-specs", action="store_true", help="Skip the daily specs snapshot.")
    parser.add_argument("--raw-backfill", action="store_true",
                        help="First merge the raw funding archive (<root>/_raw/<venue>/funding) into the store.")
    parser.add_argument("--rate", type=float, default=2.0, help="Max requests per second (default: 2).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log retries.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    results: List[DownloadResult] = []
    for name in args.venues or [args.venue]:
        plan = DownloadPlan(
            venue=name,
            symbols=args.symbols,
            datasets=[Dataset(d) for d in args.datasets],
            resolutions=args.resolutions,
            mark_resolutions=args.mark_resolutions,
            since=args.since,
            active_only=args.active_only,
            specs=not args.no_specs,
            raw_backfill=args.raw_backfill,
        )
        try:
            results += download_venue(plan, root=args.root, rate=args.rate)
        except Exception as exc:  # one failing venue must not stop the others
            results.append(DownloadResult(name, "*", "venue", error=f"{type(exc).__name__}: {exc}".splitlines()[0]))
            logger.warning("%s failed: %s", name, results[-1].error)
    failed = [r for r in results if r.error]
    print(f"Done: {len(results)} series, {sum(r.new_rows for r in results)} new rows, {len(failed)} failed")
    for r in failed:
        print(f"  FAILED {r.venue} {r.symbol} {r.dataset}: {r.error}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
