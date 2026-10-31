"""Backfill the canonical funding store from a raw archive of venue payloads.

OKX (~94 days) and Bitget (90 days) serve their funding history over a rolling window only: every day not
collected is lost. Quant-lab's ``scripts/collect_window_funding.py`` (scheduled task ``quant-window-collect``,
work item ``wi-venue-window-collect``) archives the rows as served before they leave the window. The archive's
contract, which this module relies on:

    <root>/_raw/<venue>/funding/<instrument>.parquet

one file per venue instrument id (the native symbol: ``BTC-USDT-SWAP``, ``BTCUSDT``), the venue's fields as
served, as text, plus ``funding_time`` and ``fetched_at`` (UTC, ms); append-only.

``backfill_funding`` parses the archived rows with the venue adapter's own parser (``venue.parse_funding``), so
the canonical rows are what the API path produces, merges them with the stored series (a stored row wins over an
archived one at the same time) and recomputes ``interval_h`` / ``rate_1h`` / ``apr`` over the union, as a full
download would. It reads the archive and never writes it; it makes no request. The nominal interval of a series
(its first event) comes from the store's registry.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Sequence

import pandas as pd

from quant_marketdata.venues.base import Venue
from quant_marketdata.venues.download import DownloadResult
from quant_marketdata.venues.schema import Dataset, finalize_funding
from quant_marketdata.venues.store import VenueStore

logger = logging.getLogger(__name__)

RAW_DIR = "_raw"
ARCHIVE_COLUMNS = ("funding_time", "fetched_at")
RAW_EVENT_COLUMNS = ["rate_raw", "direction_raw", "rate"]
LABEL = "funding/raw_archive"


def archive_dir(root, venue: str) -> Path:
    return VenueStore.at(root).root / RAW_DIR / venue / "funding"


def archived_symbols(root, venue: str) -> List[str]:
    folder = archive_dir(root, venue)
    return sorted(p.stem for p in folder.glob("*.parquet")) if folder.is_dir() else []


def read_archived(path: Path) -> List[dict]:
    """One archive file -> the venue's raw records (archive columns dropped)."""
    frame = pd.read_parquet(path)
    frame = frame.drop(columns=[c for c in ARCHIVE_COLUMNS if c in frame.columns])
    return frame.astype(object).where(frame.notna(), None).to_dict("records")


def backfill_funding(venue: Venue, *, root=None, symbols: Optional[Sequence[str]] = None
                     ) -> List[DownloadResult]:
    """Merge every archived funding series of ``venue`` (or only ``symbols``) into the store."""
    parse = getattr(venue, "parse_funding", None)
    if parse is None:
        raise ValueError(f"{venue.name} has no raw funding parser: its archive cannot be read")
    store = VenueStore.at(root)
    registry = store.read(store.registry_path())
    intervals = {}
    if not registry.empty:
        rows = registry[registry["venue"] == venue.name]
        intervals = dict(zip(rows["venue_symbol"], rows["funding_interval_h"], strict=True))
    wanted = archived_symbols(root, venue.name)
    if symbols is not None:
        wanted = [s for s in wanted if s in set(symbols)]
    results = []
    for symbol in wanted:
        try:
            results.append(_backfill_one(store, venue, symbol, parse, intervals.get(symbol)))
        except Exception as exc:  # one bad file must not stop the others
            results.append(DownloadResult(venue.name, symbol, LABEL,
                                          error=f"{type(exc).__name__}: {exc}".splitlines()[0]))
            logger.warning("%s %s %s failed: %s", venue.name, symbol, LABEL, results[-1].error)
    added = sum(r.new_rows for r in results)
    logger.info("%s raw archive: %d series, +%d rows", venue.name, len(results), added)
    return results


def _backfill_one(store: VenueStore, venue: Venue, symbol: str, parse, nominal) -> DownloadResult:
    archived = parse(read_archived(archive_dir(store.root, venue.name) / f"{symbol}.parquet"))
    target = store.path(venue.name, Dataset.FUNDING, symbol)
    stored = store.read(target)
    parts = [f[RAW_EVENT_COLUMNS] for f in (stored, archived) if not f.empty]
    if not parts:
        return DownloadResult(venue.name, symbol, LABEL, 0, 0)
    union = pd.concat(parts)
    union = union[~union.index.duplicated(keep="first")]
    default_h = float(getattr(venue, "default_interval_h", 8))
    nominal_h = float(nominal) if nominal is not None and pd.notna(nominal) else default_h
    events = finalize_funding(union, nominal_h, getattr(venue, "max_interval_h", nominal_h))
    store.write(target, events)
    return DownloadResult(venue.name, symbol, LABEL, len(events) - len(stored), len(events))


__all__ = ["ARCHIVE_COLUMNS", "RAW_DIR", "archive_dir", "archived_symbols", "backfill_funding", "read_archived"]
