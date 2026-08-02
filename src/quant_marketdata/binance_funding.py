"""Binance perpetual funding-rate loader.

Funding rate = the periodic payment between perpetual longs and shorts (positioning + leverage-churn proxy).
Public REST API (no key), paginated, parquet cache. Full history (BTCUSDT perp from 2019-09).

The parquet cache directory is resolved at call time: an explicit ``cache_dir``, else the env var
``QUANT_FUNDING_CACHE``, else ``<cwd>/data/raw`` -- so a research repo and a fund repo can point at the SAME
shared 2019+ cache (load-bearing for the carry's history) by setting the same cache root.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pandas as pd

_FAPI = "https://fapi.binance.com/fapi/v1/fundingRate"


def _cache_root(cache_dir: str | Path | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    env = os.environ.get("QUANT_FUNDING_CACHE")
    return Path(env) if env else Path.cwd() / "data" / "raw"


def _parse_funding(records: list) -> pd.DataFrame:
    """Parse Binance fundingRate records -> DataFrame[funding_rate] indexed by UTC datetime."""
    cols = ["funding_rate"]
    if not records:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(records)
    df["t"] = pd.to_datetime(df["fundingTime"].astype("int64"), unit="ms")
    df["funding_rate"] = df["fundingRate"].astype(float)
    return df.set_index("t")[cols].sort_index()


def _daily_funding(df: pd.DataFrame) -> pd.Series:
    """Daily mean funding rate (the 8h funding is paid ~3x/day) -> daily series."""
    if df.empty:
        return pd.Series(dtype=float, name="funding_rate")
    s = df["funding_rate"].resample("D").mean().dropna()
    s.index = s.index.normalize()
    return s.rename("funding_rate")


def _fetch_pages(symbol: str, start_ms: int, max_pages: int = 60) -> list:
    """Paginate the public fundingRate endpoint (1000/call) forward from ``start_ms``."""
    out: list = []
    cursor = start_ms
    for _ in range(max_pages):
        url = f"{_FAPI}?symbol={symbol}&startTime={cursor}&limit=1000"
        with urllib.request.urlopen(url, timeout=30) as r:  # noqa: S310 (trusted Binance host)
            page = json.load(r)
        if not page:
            break
        out.extend(page)
        if len(page) < 1000:
            break
        cursor = int(page[-1]["fundingTime"]) + 1
    return out


def load_funding_rate(
    symbol: str = "BTCUSDT", start: str | None = "2019-01-01", use_cache: bool = True,
    *, cache_dir: str | Path | None = None,
) -> pd.Series:
    """Daily Binance perpetual funding rate for ``symbol``, cached to ``<cache_root>/binance_funding``."""
    safe = symbol.replace("/", "").replace(":", "")
    cache = _cache_root(cache_dir) / "binance_funding"
    path = cache / f"{safe}_funding_daily.parquet"
    if use_cache and path.exists():
        s = pd.read_parquet(path)["funding_rate"]
        s.index = pd.to_datetime(s.index)
        return s.rename("funding_rate")
    start_ms = int(pd.Timestamp(start).timestamp() * 1000) if start else 1567036800000
    s = _daily_funding(_parse_funding(_fetch_pages(safe, start_ms)))
    if len(s):
        cache.mkdir(parents=True, exist_ok=True)
        s.to_frame().to_parquet(path)
    return s
