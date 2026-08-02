"""Hyperliquid perpetual funding-rate loader (a second funding venue: a DEX perp with an HOURLY funding
cadence, vs Binance's 8h). Public POST API (no key), paginated 500/call, parquet cache. History starts ~2023.

Cache directory resolved at call time (``cache_dir`` -> env ``QUANT_FUNDING_CACHE`` -> ``<cwd>/data/raw``),
so research + fund repos share the same cache.
"""

from __future__ import annotations

import json
import os
import urllib.request
from pathlib import Path

import pandas as pd

_INFO = "https://api.hyperliquid.xyz/info"


def _cache_root(cache_dir: str | Path | None = None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    env = os.environ.get("QUANT_FUNDING_CACHE")
    return Path(env) if env else Path.cwd() / "data" / "raw"


def _parse_funding(records: list) -> pd.DataFrame:
    """Parse Hyperliquid fundingHistory records -> DataFrame[funding_rate] indexed by UTC datetime."""
    cols = ["funding_rate"]
    if not records:
        return pd.DataFrame(columns=cols)
    df = pd.DataFrame(records)
    df["t"] = pd.to_datetime(df["time"].astype("int64"), unit="ms")
    df["funding_rate"] = df["fundingRate"].astype(float)
    return df.set_index("t")[cols].sort_index()


def _daily_funding(df: pd.DataFrame) -> pd.Series:
    """Daily mean funding rate (Hyperliquid funding is paid hourly) -> daily series."""
    if df.empty:
        return pd.Series(dtype=float, name="funding_rate")
    s = df["funding_rate"].resample("D").mean().dropna()
    s.index = s.index.normalize()
    return s.rename("funding_rate")


def _fetch_pages(coin: str, start_ms: int, max_pages: int = 60) -> list:
    """Paginate the public fundingHistory endpoint (500/call) forward from ``start_ms``."""
    out: list = []
    cursor = start_ms
    for _ in range(max_pages):
        body = json.dumps({"type": "fundingHistory", "coin": coin, "startTime": cursor}).encode()
        req = urllib.request.Request(_INFO, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (trusted Hyperliquid host)
            page = json.load(r)
        if not page:
            break
        out.extend(page)
        if len(page) < 500:
            break
        nxt = int(page[-1]["time"]) + 1
        if nxt <= cursor:
            break
        cursor = nxt
    seen: set[int] = set()
    uniq: list = []
    for rec in out:
        t = int(rec["time"])
        if t not in seen:
            seen.add(t)
            uniq.append(rec)
    return uniq


def load_funding_rate(
    coin: str = "BTC", start: str | None = "2023-01-01", use_cache: bool = True,
    *, cache_dir: str | Path | None = None,
) -> pd.Series:
    """Daily Hyperliquid perpetual funding rate for ``coin``, cached to ``<cache_root>/hyperliquid_funding``."""
    safe = coin.upper()
    cache = _cache_root(cache_dir) / "hyperliquid_funding"
    path = cache / f"{safe}_funding_daily.parquet"
    if use_cache and path.exists():
        s = pd.read_parquet(path)["funding_rate"]
        s.index = pd.to_datetime(s.index)
        return s.rename("funding_rate")
    start_ms = int(pd.Timestamp(start).timestamp() * 1000) if start else 1672531200000
    s = _daily_funding(_parse_funding(_fetch_pages(safe, start_ms)))
    if len(s):
        cache.mkdir(parents=True, exist_ok=True)
        s.to_frame().to_parquet(path)
    return s
