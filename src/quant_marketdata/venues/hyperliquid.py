"""Hyperliquid adapter over the public ``info`` endpoint (POST JSON), without the Hyperliquid SDK.

API quirks this adapter absorbs (verified 2026-10-15):

- Every query is a POST to ``/info`` with a ``type``. ``metaAndAssetCtxs`` lists the main-dex perps
  (``universe``) and their live context; delisted perps stay listed with ``isDelisted``. There is no
  listing date: history is found by asking from an early start (empty pages are cheap). Builder-deployed
  (HIP-3) dexes are not covered.
- ``fundingHistory`` is hourly, ``time`` in ms with a few ms of jitter; ``fundingRate`` is a **signed
  fraction per hour** (positive = longs pay), as the v0.2 ``load_hl_funding`` loader reads it. It returns
  the **oldest** 500 events from ``startTime``.
- ``candleSnapshot`` only serves the **most recent 5000 candles** of each interval; older windows come
  back empty. ``v`` is base volume; there is no quote volume and no mark-price candle.
- Info requests weigh 20 out of 1200 per minute per IP, i.e. 1 req/s sustained: the client is paced at
  1 req/s at most.
- ``k`` prefixes mean x1000 (``kPEPE``); ``szDecimals`` is the size step.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.paging import forward_fetch, paged_fetch
from quant_marketdata.venues.schema import (
    CANDLE_COLUMNS,
    INSTRUMENT_COLUMNS,
    Dataset,
    empty_frame,
    finalize_funding,
)

BASE_URL = "https://api.hyperliquid.xyz"
MAX_RATE = 1.0
FUNDING_PAGE = 500
CANDLE_PAGE = 5000
FUNDING_INTERVAL_H = 1
RESOLUTION_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def _ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


class HyperliquidVenue:
    name = "hyperliquid"
    capabilities = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.CANDLES, Dataset.SPECS})
    resolutions = tuple(RESOLUTION_SECONDS)

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0) -> None:
        self.client = client or JsonClient(BASE_URL, rate=min(rate, MAX_RATE))

    def _info(self, payload: Dict[str, Any]) -> Any:
        return self.client.post("info", payload)

    def _universe(self) -> List[Dict[str, Any]]:
        meta = self._info({"type": "meta"})
        return meta.get("universe") or []

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        rows = []
        for index, m in enumerate(self._universe()):
            rows.append({
                "venue": self.name,
                "venue_symbol": m["name"],
                "venue_market_id": str(index),  # the asset index used by the exchange endpoint
                "settle_asset": "USDC",
                "chain": None,
                "contract_address": None,
                "funding_interval_h": float(FUNDING_INTERVAL_H),
                "listed_at": pd.NaT,
                "delisted_at": pd.NaT,
                "status": "inactive" if m.get("isDelisted") else "active",
            })
        columns = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"]
        return pd.DataFrame(rows).reindex(columns=columns)

    def specs(self) -> pd.DataFrame:
        require(self, Dataset.SPECS)
        rows = []
        for m in self._universe():
            row = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in m.items()}
            rows.append({"symbol": m["name"], **row})
        return pd.DataFrame(rows)

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.FUNDING)
        end_ms = _ms(end)

        def fetch_from(cursor: int) -> pd.DataFrame:
            return parse_funding_history(self._info({
                "type": "fundingHistory", "coin": symbol, "startTime": cursor, "endTime": end_ms,
            }) or [])

        raw = forward_fetch(fetch_from, _ms(start), end_ms, FUNDING_PAGE)
        if raw.empty:
            return finalize_funding(raw, FUNDING_INTERVAL_H)
        return finalize_funding(raw.loc[pd.Timestamp(start):pd.Timestamp(end)], FUNDING_INTERVAL_H)

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        if price != "last":
            require(self, Dataset.MARK_CANDLES)
        require(self, Dataset.CANDLES)
        if resolution not in RESOLUTION_SECONDS:
            raise ValueError(f"Unsupported resolution {resolution!r}; expected one of {self.resolutions}")
        step = RESOLUTION_SECONDS[resolution] * 1000

        def fetch(a: int, b: int) -> pd.DataFrame:
            return parse_candles(self._info({
                "type": "candleSnapshot",
                "req": {"coin": symbol, "interval": resolution, "startTime": a, "endTime": b},
            }) or [])

        frame = paged_fetch(fetch, _ms(start), _ms(end), step * CANDLE_PAGE)
        if frame.empty:
            return empty_frame(CANDLE_COLUMNS)
        return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        require(self, Dataset.DAILY_METRICS)  # no open-interest history in the public API
        raise AssertionError("unreachable")


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_funding_history(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Raw ``fundingHistory`` records -> ``rate_raw`` and signed fractional ``rate`` per hour."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["time"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "rate_raw": frame["fundingRate"].astype(str).to_numpy(),
        "direction_raw": None,
        "rate": frame["fundingRate"].astype(float).to_numpy(),
    }, index=index)
    return out.sort_index()


def parse_candles(records: List[Dict[str, Any]]) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["t"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "open": frame["o"].astype(float).to_numpy(),
        "high": frame["h"].astype(float).to_numpy(),
        "low": frame["l"].astype(float).to_numpy(),
        "close": frame["c"].astype(float).to_numpy(),
        "volume_base": frame["v"].astype(float).to_numpy(),
        "volume_quote": float("nan"),
        "price_type": "last",
        "trade_count": frame["n"].astype("int64").to_numpy(),
    }, index=index)
    return out.sort_index()


register_venue(HyperliquidVenue.name, HyperliquidVenue)

__all__ = ["HyperliquidVenue", "parse_candles", "parse_funding_history"]
