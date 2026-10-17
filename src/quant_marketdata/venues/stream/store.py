"""Stream store: daily Parquet files plus pending part files, per venue, channel and native symbol.

    <root>/venues/<venue>/stream/<channel>/<SYMBOL>/<YYYY-MM-DD>.parquet
    <root>/venues/<venue>/stream/<channel>/<SYMBOL>/<YYYY-MM-DD>.parts/<n>.parquet

Same root and symbol naming as the REST ``VenueStore``. Rows are flushed into small part files every few
seconds, so a crash loses at most one flush interval; parts are compacted into the daily file once the day is
over, and for every day when the recorder stops (or at the next start after a crash). Reading merges both, so
the current day is readable while recording.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from quant_marketdata.venues.store import resolve_root, safe_name
from quant_marketdata.venues.stream.schema import (
    CHANNELS,
    DEDUPE_KEYS,
    PARQUET_COMPRESSION,
    SCHEMAS,
)


@dataclass
class StreamStore:
    root: Path  # the venue's stream directory
    _seq: Iterator[int] = field(default_factory=itertools.count, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @classmethod
    def at(cls, venue: str, root: str | Path | None = None) -> "StreamStore":
        return cls(resolve_root(root) / "venues" / venue / "stream")

    def day_path(self, channel: str, symbol: str, day: str) -> Path:
        return self.root / channel / safe_name(symbol) / f"{day}.parquet"

    def parts_dir(self, channel: str, symbol: str, day: str) -> Path:
        return self.root / channel / safe_name(symbol) / f"{day}.parts"

    def write_part(self, channel: str, symbol: str, day: str, table: pa.Table) -> Path:
        directory = self.parts_dir(channel, symbol, day)
        name = f"{time.time_ns() // 1_000_000:013d}-{next(self._seq):08d}.parquet"
        with self._lock:
            directory.mkdir(parents=True, exist_ok=True)
            tmp = directory / f"{name}.tmp"
            pq.write_table(table, tmp, compression=PARQUET_COMPRESSION)
            tmp.replace(directory / name)
        return directory / name

    @staticmethod
    def _parts(directory: Path) -> List[Path]:
        return sorted(directory.glob("*.parquet")) if directory.is_dir() else []

    def pending(self) -> List[Tuple[str, str, str]]:
        """``(channel, symbol, day)`` of every partition with parts not yet compacted."""
        found = []
        for directory in sorted(self.root.glob("*/*/*.parts")):
            if self._parts(directory):
                channel, symbol = directory.parent.parent.name, directory.parent.name
                found.append((channel, symbol, directory.name[: -len(".parts")]))
        return found

    def compact(self, channel: str, symbol: str, day: str) -> int:
        """Merge the parts of a day into its daily file. Returns the number of rows."""
        with self._lock:
            path = self.day_path(channel, symbol, day)
            directory = self.parts_dir(channel, symbol, day)
            parts = self._parts(directory)
            if not parts:
                return 0
            sources = ([path] if path.exists() else []) + parts
            schema = SCHEMAS[channel]
            tmp = path.with_suffix(".parquet.tmp")
            rows = 0
            if channel in DEDUPE_KEYS:
                table = pa.concat_tables(pq.read_table(p, schema=schema) for p in sources)
                frame = table.to_pandas()
                frame = frame.drop_duplicates(DEDUPE_KEYS[channel], keep="first")
                frame = frame.sort_values(["timestamp"] + DEDUPE_KEYS[channel], kind="stable")
                table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
                pq.write_table(table, tmp, compression=PARQUET_COMPRESSION)
                rows = table.num_rows
            else:
                with pq.ParquetWriter(tmp, schema, compression=PARQUET_COMPRESSION) as writer:
                    for source in sources:
                        for batch in pq.ParquetFile(source).iter_batches(batch_size=500_000):
                            writer.write_batch(batch)
                            rows += batch.num_rows
            tmp.replace(path)
            for part in parts:
                part.unlink()
            try:
                directory.rmdir()
            except OSError:  # a part was added meanwhile
                pass
            return rows

    def read(self, channel: str, symbol: str, day: str) -> pa.Table:
        path = self.day_path(channel, symbol, day)
        sources = ([path] if path.exists() else []) + self._parts(self.parts_dir(channel, symbol, day))
        schema = SCHEMAS[channel]
        if not sources:
            return schema.empty_table()
        return pa.concat_tables(pq.read_table(p, schema=schema) for p in sources)

    def days(self, channel: str, symbol: str) -> List[str]:
        directory = self.root / channel / safe_name(symbol)
        names = {p.name.split(".")[0] for p in directory.glob("*")} if directory.is_dir() else set()
        return sorted(n for n in names if len(n) == 10)


def read_stream(
    venue: str,
    channel: str,
    symbol: str,
    start: Optional[str] = None,
    end: Optional[str] = None,
    *,
    root: str | Path | None = None,
) -> pd.DataFrame:
    """Recorded rows of ``channel`` for a venue's native ``symbol`` between UTC days ``start`` and ``end``
    (inclusive). Rows of the current day still in part files are included."""
    if channel not in SCHEMAS:
        raise ValueError(f"Unknown channel {channel!r}; expected one of {', '.join(CHANNELS)}")
    store = StreamStore.at(venue, root)
    days = [d for d in store.days(channel, symbol) if (start is None or d >= start) and (end is None or d <= end)]
    tables = [store.read(channel, symbol, d) for d in days]
    table = pa.concat_tables(tables) if tables else SCHEMAS[channel].empty_table()
    return table.to_pandas()


__all__ = ["StreamStore", "read_stream"]
