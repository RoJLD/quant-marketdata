"""Extended (extended.exchange, Starknet perp DEX) adapter over the public REST API, without its SDK.

API quirks this adapter absorbs (verified 2026-10-15):

- Responses are wrapped as ``{"status": "OK", "data": ...}``. ``info/markets`` lists every market with a
  ``createdAt`` in ms; delisted markets stay listed (``status=DELISTED``), announced ones are
  ``PRELISTED``. The few ``SPOT`` markets (``BTCSPOT-USD``) are dropped.
- The asset class is declared by ``category`` / ``subCategory`` (``Crypto``, ``RWA`` / ``Equity``,
  ``Commodity``, ``FX``, ``ETF/Index``, ``Pre-market``). ``ETF/Index`` mixes both classes and is left to
  the registry overrides.
- ``collateralAssetName`` is ``USD`` for every market; it is kept as the settle asset as served.
- ``tradingHours`` declares the session (``CONTINUOUS``, ``WEEKDAYS``, ``NO_OVERNIGHT``; checked
  2026-10-18), which the registry keys restricted-session ids on. ``<TICKER>_24_5-USD`` names are a
  listing batch, not a session: most active ones are ``CONTINUOUS``. Delisted markets are renamed in
  ``uiName`` (``NA26-USD``) but keep their ``name``.
- ``info/<MARKET>/funding?startTime&endTime`` (ms) is hourly, ``f`` a **signed fraction per hour**
  (positive = longs pay), returned **newest first** and capped at the **1000 most recent** events of the
  window: windows are kept to 500 hours. History starts at the market's ``createdAt`` (BTC-USD:
  2025-07-18, after the move to Starknet).
- ``info/candles/<MARKET>/{trades,mark-prices}?interval=PT1H&limit&endTime`` ignores any start time and
  returns the ``limit`` most recent candles up to ``endTime``, newest first: the limit is sized to the
  window. Intervals are ISO-8601 durations; ``T`` is the open time; mark candles have no volume.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.paging import paged_fetch, window_count
from quant_marketdata.venues.schema import (
    CANDLE_COLUMNS,
    INSTRUMENT_COLUMNS,
    Dataset,
    empty_frame,
    finalize_funding,
    make_book,
)

BASE_URL = "https://api.starknet.extended.exchange/api/v1"
FUNDING_INTERVAL_H = 1
FUNDING_WINDOW_H = 500
CANDLE_PAGE = 1000
RESOLUTIONS: Dict[str, str] = {
    "1m": "PT1M",
    "5m": "PT5M",
    "15m": "PT15M",
    "30m": "PT30M",
    "1h": "PT1H",
    "2h": "PT2H",
    "4h": "PT4H",
    "8h": "PT8H",
    "12h": "PT12H",
    "1d": "P1D",
}
RESOLUTION_SECONDS: Dict[str, int] = {
    "1m": 60,
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
CATEGORY_CLASSES: Dict[str, Optional[str]] = {
    "Equity": "equity",
    "Commodity": "commodity",
    "FX": "fx",
    "Pre-market": "private",
}
STATUSES = {"ACTIVE": "active", "DELISTED": "inactive", "PRELISTED": "pending"}
SESSIONS = {"CONTINUOUS": "24x7", "WEEKDAYS": "24x5", "NO_OVERNIGHT": "no-overnight"}
LISTING_BATCH = "_24_5"


def session(market: Dict[str, Any]) -> Optional[str]:
    """Declared session; an unknown ``tradingHours`` value is passed on lower-cased (the registry then
    reports the market as unresolved)."""
    hours = market.get("tradingHours")
    if not hours:
        return None
    return SESSIONS.get(hours, str(hours).lower())


def _ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


def _data(payload: Dict[str, Any]) -> Any:
    if payload.get("status") != "OK":
        raise ValueError(f"Extended API error: {json.dumps(payload)[:200]}")
    return payload.get("data")


def asset_class(market: Dict[str, Any]) -> Optional[str]:
    if market.get("category") != "RWA":
        return "crypto"  # Crypto (gold tokens included), and the odd L1/L2/Infra category of old markets
    return CATEGORY_CLASSES.get(market.get("subCategory") or "")


class ExtendedVenue:
    name = "extended"
    capabilities = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.CANDLES, Dataset.MARK_CANDLES,
                              Dataset.SPECS, Dataset.BOOK})
    resolutions = tuple(RESOLUTIONS)

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0) -> None:
        self.client = client or JsonClient(BASE_URL, rate=rate)

    def _markets(self) -> List[Dict[str, Any]]:
        markets = _data(self.client.get("info/markets")) or []
        return [m for m in markets if m.get("type") == "PERPETUAL"]

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        rows = []
        for m in self._markets():
            created = int(m["createdAt"]) if m.get("createdAt") else None
            cls = asset_class(m)
            asset = (m.get("assetName") or "").removesuffix(LISTING_BATCH) or None
            rows.append({
                "venue": self.name,
                "venue_symbol": m["name"],
                "venue_market_id": m["name"],
                "settle_asset": m.get("collateralAssetName"),
                "asset_class": cls,
                "underlying_ref": asset if cls not in (None, "crypto") else None,
                "chain": None,
                "contract_address": None,
                "funding_interval_h": float(FUNDING_INTERVAL_H),
                "listed_at": pd.Timestamp(created, unit="ms") if created else pd.NaT,
                "delisted_at": pd.NaT,
                "status": STATUSES.get(m.get("status"), str(m.get("status", "")).lower()),
                "session": session(m),
            })
        columns = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"]
        return pd.DataFrame(rows).reindex(columns=columns)

    def specs(self) -> pd.DataFrame:
        require(self, Dataset.SPECS)
        rows = []
        for m in self._markets():
            m = {k: v for k, v in m.items() if k != "marketStats"}  # live prices, not specs
            row = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in m.items()}
            rows.append({"symbol": m["name"], **row})
        return pd.DataFrame(rows)

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.FUNDING)

        def fetch(a: int, b: int) -> pd.DataFrame:
            return parse_funding(_data(self.client.get(f"info/{symbol}/funding", {
                "startTime": a, "endTime": b,
            })) or [])

        raw = paged_fetch(fetch, _ms(start), _ms(end), FUNDING_WINDOW_H * 3600 * 1000)
        if raw.empty:
            return finalize_funding(raw, FUNDING_INTERVAL_H)
        return finalize_funding(raw.loc[pd.Timestamp(start):pd.Timestamp(end)], FUNDING_INTERVAL_H)

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        if price == "last":
            require(self, Dataset.CANDLES)
            kind = "trades"
        elif price == "mark":
            require(self, Dataset.MARK_CANDLES)
            kind = "mark-prices"
        else:
            raise ValueError(f"price must be 'last' or 'mark', got {price!r}")
        if resolution not in RESOLUTIONS:
            raise ValueError(f"Unsupported resolution {resolution!r}; expected one of {self.resolutions}")
        step = RESOLUTION_SECONDS[resolution] * 1000

        def fetch(a: int, b: int) -> pd.DataFrame:
            return parse_candles(_data(self.client.get(f"info/candles/{symbol}/{kind}", {
                "interval": RESOLUTIONS[resolution], "endTime": b,
                "limit": window_count(a, b, step, CANDLE_PAGE),
            })) or [], price)

        frame = paged_fetch(fetch, _ms(start), _ms(end), step * CANDLE_PAGE)
        if frame.empty:
            return empty_frame(CANDLE_COLUMNS)
        return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book from ``info/markets/<market>/orderbook`` (aggregated levels)."""
        require(self, Dataset.BOOK)
        data = self.client.get(f"info/markets/{symbol}/orderbook").get("data") or {}
        return make_book([(lv["price"], lv["qty"]) for lv in data.get("bid") or []],
                         [(lv["price"], lv["qty"]) for lv in data.get("ask") or []],
                         venue=self.name, symbol=symbol, depth=depth)

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        require(self, Dataset.DAILY_METRICS)  # no open-interest history in the public API
        raise AssertionError("unreachable")


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_funding(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Raw funding records ``{m, f, T}`` -> ``rate_raw`` and signed fractional ``rate`` per hour."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["T"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "rate_raw": frame["f"].astype(str).to_numpy(),
        "direction_raw": None,
        "rate": frame["f"].astype(float).to_numpy(),
    }, index=index)
    return out.sort_index()


def parse_candles(records: List[Dict[str, Any]], price: str) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["T"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "open": frame["o"].astype(float).to_numpy(),
        "high": frame["h"].astype(float).to_numpy(),
        "low": frame["l"].astype(float).to_numpy(),
        "close": frame["c"].astype(float).to_numpy(),
        "volume_base": frame["v"].astype(float).to_numpy() if "v" in frame else float("nan"),
        "volume_quote": float("nan"),
        "price_type": price,
    }, index=index)
    return out.sort_index()


register_venue(ExtendedVenue.name, ExtendedVenue)

__all__ = ["ExtendedVenue", "asset_class", "parse_candles", "parse_funding", "session"]
