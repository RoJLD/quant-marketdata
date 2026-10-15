"""Incremental Parquet store, one file per venue, dataset and native symbol.

Layout under the root::

    registry/instruments.parquet
    venues/<venue>/funding/<symbol>.parquet
    venues/<venue>/candles/<resolution>/<symbol>.parquet
    venues/<venue>/mark_candles/<resolution>/<symbol>.parquet
    venues/<venue>/daily_metrics/<symbol>.parquet
    venues/<venue>/specs.parquet

The root is resolved at call time: ``root`` argument -> env ``QUANT_MARKETDATA_ROOT`` ->
``<cwd>/data/marketdata``, so the research lab and the fund can share one store.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd

from quant_marketdata.venues.schema import Dataset

ROOT_ENV = "QUANT_MARKETDATA_ROOT"


def resolve_root(root: str | Path | None = None) -> Path:
    if root is not None:
        return Path(root)
    env = os.environ.get(ROOT_ENV)
    return Path(env) if env else Path.cwd() / "data" / "marketdata"


def safe_name(symbol: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "_", symbol)


@dataclass
class VenueStore:
    root: Path

    @classmethod
    def at(cls, root: str | Path | None = None) -> "VenueStore":
        return cls(resolve_root(root))

    def path(self, venue: str, dataset: Dataset, symbol: str = "",
             resolution: Optional[str] = None) -> Path:
        base = self.root / "venues" / venue / dataset.value
        if dataset is Dataset.SPECS:
            return base.with_suffix(".parquet")
        if resolution is not None:
            base = base / resolution
        return base / f"{safe_name(symbol)}.parquet"

    def registry_path(self) -> Path:
        return self.root / "registry" / "instruments.parquet"

    @staticmethod
    def read(path: Path, columns: Optional[list[str]] = None) -> pd.DataFrame:
        return pd.read_parquet(path, columns=columns) if path.exists() else pd.DataFrame()

    @staticmethod
    def last_timestamp(path: Path) -> Optional[pd.Timestamp]:
        if not path.exists():
            return None
        index = pd.read_parquet(path, columns=[]).index
        return index.max() if len(index) else None

    @staticmethod
    def row_count(path: Path) -> int:
        return len(pd.read_parquet(path, columns=[]).index) if path.exists() else 0

    @staticmethod
    def write(path: Path, frame: pd.DataFrame) -> None:
        """Atomic write: a crash mid-write never leaves a truncated file behind."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".parquet.tmp")
        frame.to_parquet(tmp)
        tmp.replace(path)

    @classmethod
    def merge(cls, path: Path, new: pd.DataFrame) -> int:
        """Merge ``new`` into ``path`` on the time index, newer rows winning. Returns the row count."""
        existing = cls.read(path)
        frames = [f for f in (existing, new) if not f.empty]
        if not frames:
            return 0
        merged = pd.concat(frames) if len(frames) > 1 else frames[0]
        merged = merged[~merged.index.duplicated(keep="last")].sort_index()
        cls.write(path, merged)
        return len(merged)


__all__ = ["ROOT_ENV", "VenueStore", "resolve_root", "safe_name"]
