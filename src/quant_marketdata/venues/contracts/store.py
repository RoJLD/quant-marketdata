"""Partitioned Parquet store for dated contracts: by day or month, never one file per contract (Paradex lists
10k options, Kalshi KXBTCD alone creates ~4,500 markets a day).

Layout under the shared root (``QUANT_MARKETDATA_ROOT``, same ``safe_name`` and atomic writes as
``VenueStore``)::

    registry/options/<venue>.parquet
    registry/binaries/<venue>/<SERIES>/<YYYY-MM>.parquet               (month of expiry)
    registry/universe_changes/<venue>.parquet
    venues/<venue>/option_chain/<UNDERLYING>/<YYYY-MM-DD>.parquet       (+ <YYYY-MM-DD>.parts/ during the day)
    venues/<venue>/binary_quotes/<SERIES>/<YYYY-MM-DD>.parquet          (+ .parts/)
    venues/<venue>/binary_candles/<SERIES>/<period>/<YYYY-MM>.parquet
    venues/<venue>/binary_trades/<SERIES>/<YYYY-MM>.parquet
    venues/<venue>/option_trades/<UNDERLYING>/<YYYY-MM>.parquet
    venues/<venue>/option_mark_candles/<UNDERLYING>/<resolution>/<YYYY-MM>.parquet
    venues/<venue>/settlements/<settlement_index_id>.parquet
    venues/<venue>/_snapshot_health.json                                 (heartbeat of the last pass)

Snapshots (chains, quotes) are a slow stream and reuse the stream recorder's mechanics: each pass writes
one part file per partition, parts are compacted into the day file once the day is over, reading merges both.
They are **append-only**: a pass never overwrites an earlier one (compaction only drops exact replays of a
``(ts_snapshot, venue_symbol)`` pair, which a crash between compaction and part removal would create).
Monthly partitions are merged on their key columns, newer rows winning. Needs pyarrow (``[store]`` extra).
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Sequence, Tuple

import pandas as pd

from quant_marketdata.venues.schema import Dataset
from quant_marketdata.venues.store import VenueStore, resolve_root, safe_name

SNAPSHOT_DATASETS = frozenset({Dataset.OPTION_CHAIN, Dataset.BINARY_QUOTES})
MONTHLY_DATASETS = frozenset({Dataset.BINARY_CANDLES, Dataset.BINARY_TRADES, Dataset.OPTION_TRADES,
                              Dataset.OPTION_MARK_CANDLES})
SNAPSHOT_KEY = ["ts_snapshot", "venue_symbol"]


def day_of(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m-%d")


def month_of(ts) -> str:
    return pd.Timestamp(ts).strftime("%Y-%m")


@dataclass
class ContractStore:
    root: Path
    _seq: Iterator[int] = field(default_factory=itertools.count, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def at(cls, root: str | Path | None = None) -> "ContractStore":
        return cls(resolve_root(root))

    # -- paths ---------------------------------------------------------------------------------------

    def option_registry_path(self, venue: str) -> Path:
        return self.root / "registry" / "options" / f"{safe_name(venue)}.parquet"

    def binary_registry_path(self, venue: str, series: str, month: str) -> Path:
        return self.root / "registry" / "binaries" / safe_name(venue) / safe_name(series) / f"{month}.parquet"

    def universe_changes_path(self, venue: str) -> Path:
        return self.root / "registry" / "universe_changes" / f"{safe_name(venue)}.parquet"

    def dataset_dir(self, venue: str, dataset: Dataset, key: str, resolution: Optional[str] = None) -> Path:
        base = self.root / "venues" / safe_name(venue) / dataset.value / safe_name(key)
        return base / safe_name(resolution) if resolution is not None else base

    def partition_path(self, venue: str, dataset: Dataset, key: str, period: str,
                       resolution: Optional[str] = None) -> Path:
        """``venues/<venue>/<dataset>/<key>/[<resolution>/]<period>.parquet``; ``period`` is ``YYYY-MM-DD``
        for snapshots and ``YYYY-MM`` for monthly datasets."""
        return self.dataset_dir(venue, dataset, key, resolution) / f"{period}.parquet"

    def parts_dir(self, venue: str, dataset: Dataset, key: str, day: str) -> Path:
        return self.dataset_dir(venue, dataset, key) / f"{day}.parts"

    def settlements_path(self, venue: str, settlement_index_id: str) -> Path:
        return self.root / "venues" / safe_name(venue) / Dataset.SETTLEMENTS.value / \
            f"{safe_name(settlement_index_id)}.parquet"

    def heartbeat_path(self, venue: str) -> Path:
        return self.root / "venues" / safe_name(venue) / "_snapshot_health.json"

    # -- plain files ---------------------------------------------------------------------------------

    read = staticmethod(VenueStore.read)
    write = staticmethod(VenueStore.write)

    def merge(self, path: Path, new: pd.DataFrame, keys: Sequence[str],
              sort: Sequence[str] = (), *, keep: str = "last") -> int:
        """Merge ``new`` into ``path`` on ``keys``, newer rows winning (``keep="first"``: stored rows win,
        e.g. settlements keep their ``first_seen_at``). Returns the row count."""
        existing = self.read(path)
        frames = [f for f in (existing, new) if not f.empty]
        if not frames:
            return 0
        merged = pd.concat(frames, ignore_index=True) if len(frames) > 1 else frames[0].reset_index(drop=True)
        merged = merged.drop_duplicates(list(keys), keep=keep)
        if sort:
            merged = merged.sort_values(list(sort), kind="stable")
        self.write(path, merged.reset_index(drop=True))
        return len(merged)

    def merge_monthly(self, venue: str, dataset: Dataset, key: str, frame: pd.DataFrame,
                      keys: Sequence[str], *, resolution: Optional[str] = None,
                      time_column: str = "ts") -> List[Tuple[Path, int]]:
        """Split ``frame`` by month of ``time_column`` and merge each month into its partition."""
        if dataset not in MONTHLY_DATASETS:
            raise ValueError(f"{dataset.value} is not a monthly dataset")
        written = []
        if frame.empty:
            return written
        months = pd.to_datetime(frame[time_column]).dt.strftime("%Y-%m")
        for month, rows in frame.groupby(months, sort=True):
            path = self.partition_path(venue, dataset, key, month, resolution)
            written.append((path, self.merge(path, rows, keys, sort=(time_column,))))
        return written

    def write_heartbeat(self, venue: str, payload: dict) -> Path:
        path = self.heartbeat_path(venue)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=1, default=str))
        tmp.replace(path)
        return path

    def append_universe_changes(self, venue: str, events: pd.DataFrame) -> int:
        if events.empty:
            return 0
        return self.merge(self.universe_changes_path(venue), events, ["ts", "venue", "change", "key"],
                          sort=("ts",))

    # -- snapshots (append-only parts + daily compaction) ---------------------------------------------

    def append_snapshot(self, venue: str, dataset: Dataset, key: str, frame: pd.DataFrame) -> List[Path]:
        """Write one part file per UTC day of ``ts_snapshot``. Never touches an existing file."""
        if dataset not in SNAPSHOT_DATASETS:
            raise ValueError(f"{dataset.value} is not a snapshot dataset")
        paths = []
        if frame.empty:
            return paths
        days = pd.to_datetime(frame["ts_snapshot"]).dt.strftime("%Y-%m-%d")
        for day, rows in frame.groupby(days, sort=True):
            directory = self.parts_dir(venue, dataset, key, day)
            name = f"{time.time_ns() // 1_000_000:013d}-{next(self._seq):08d}.parquet"
            with self._lock:
                directory.mkdir(parents=True, exist_ok=True)
                self.write(directory / name, rows.reset_index(drop=True))
            paths.append(directory / name)
        return paths

    @staticmethod
    def _parts(directory: Path) -> List[Path]:
        return sorted(directory.glob("*.parquet")) if directory.is_dir() else []

    def pending(self, venue: Optional[str] = None) -> List[Tuple[str, Dataset, str, str]]:
        """``(venue, dataset, key, day)`` of every snapshot partition with parts not compacted yet."""
        found = []
        venues = [safe_name(venue)] if venue else sorted(p.name for p in (self.root / "venues").glob("*"))
        for name in venues:
            for dataset in sorted(SNAPSHOT_DATASETS, key=lambda d: d.value):
                base = self.root / "venues" / name / dataset.value
                for directory in sorted(base.glob("*/*.parts")):
                    if self._parts(directory):
                        found.append((name, dataset, directory.parent.name, directory.name[: -len(".parts")]))
        return found

    def compact(self, venue: str, dataset: Dataset, key: str, day: str) -> int:
        """Merge a day's parts into its daily file (kept rows: all, minus exact replays). Returns rows."""
        with self._lock:
            path = self.partition_path(venue, dataset, key, day)
            directory = self.parts_dir(venue, dataset, key, day)
            parts = self._parts(directory)
            if not parts:
                return self.row_count(path)
            frames = [f for f in [self.read(path)] + [self.read(p) for p in parts] if not f.empty]
            frame = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
            if not frame.empty:
                frame = frame.drop_duplicates(SNAPSHOT_KEY, keep="first")
                frame = frame.sort_values(SNAPSHOT_KEY, kind="stable").reset_index(drop=True)
                self.write(path, frame)
            for part in parts:
                part.unlink()
            try:
                directory.rmdir()
            except OSError:  # a part was added meanwhile
                pass
            return len(frame)

    def compact_before(self, day: str, venue: Optional[str] = None) -> List[Tuple[str, str, str, str, int]]:
        """Compact every pending snapshot partition of a day strictly before ``day`` (UTC)."""
        done = []
        for name, dataset, key, partition_day in self.pending(venue):
            if partition_day < day:
                done.append((name, dataset.value, key, partition_day,
                             self.compact(name, dataset, key, partition_day)))
        return done

    @staticmethod
    def row_count(path: Path) -> int:
        return VenueStore.row_count(path)

    def snapshot_days(self, venue: str, dataset: Dataset, key: str) -> List[str]:
        directory = self.dataset_dir(venue, dataset, key)
        names = {p.name.split(".")[0] for p in directory.glob("*")} if directory.is_dir() else set()
        return sorted(n for n in names if len(n) == 10)

    def read_snapshots(self, venue: str, dataset: Dataset, key: str, start: Optional[str] = None,
                       end: Optional[str] = None) -> pd.DataFrame:
        """Snapshot rows between UTC days ``start`` and ``end`` (inclusive), daily files and parts merged."""
        frames = []
        for day in self.snapshot_days(venue, dataset, key):
            if (start is not None and day < start) or (end is not None and day > end):
                continue
            sources = [self.partition_path(venue, dataset, key, day)]
            sources += self._parts(self.parts_dir(venue, dataset, key, day))
            frames += [f for f in (self.read(p) for p in sources) if not f.empty]
        if not frames:
            return pd.DataFrame()
        frame = pd.concat(frames, ignore_index=True).drop_duplicates(SNAPSHOT_KEY, keep="first")
        return frame.sort_values(SNAPSHOT_KEY, kind="stable").reset_index(drop=True)

    def read_monthly(self, venue: str, dataset: Dataset, key: str, *, resolution: Optional[str] = None,
                     start: Optional[str] = None, end: Optional[str] = None) -> pd.DataFrame:
        """Rows of the monthly partitions between months ``start`` and ``end`` (``YYYY-MM``, inclusive)."""
        directory = self.dataset_dir(venue, dataset, key, resolution)
        frames = []
        for path in sorted(directory.glob("*.parquet")) if directory.is_dir() else []:
            month = path.stem
            if (start is None or month >= start) and (end is None or month <= end):
                frames.append(self.read(path))
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()

    def last_partition(self, venue: str, dataset: Dataset, key: str,
                       resolution: Optional[str] = None) -> Optional[str]:
        """Latest stored period (incremental downloads resume from it: it is re-read, maybe incomplete)."""
        directory = self.dataset_dir(venue, dataset, key, resolution)
        periods = sorted(p.stem for p in directory.glob("*.parquet")) if directory.is_dir() else []
        return periods[-1] if periods else None


__all__ = ["ContractStore", "MONTHLY_DATASETS", "SNAPSHOT_DATASETS", "day_of", "month_of"]
