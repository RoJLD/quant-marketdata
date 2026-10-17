"""Binance USD-M futures adapter (``fapi``) over the public REST API, without a client library.

API quirks this adapter absorbs (verified 2026-10-15):

- ``exchangeInfo`` lists perps (``PERPETUAL`` and ``TRADIFI_PERPETUAL``, the stock/commodity perps) next
  to quarterly futures, which are dropped. Delisted perps stay listed with ``status=SETTLING``; the asset
  class is declared in ``underlyingType`` (``COIN``, ``EQUITY``, ``COMMODITY``, ...). One base can have
  several books (``BTCUSDT``, ``BTCUSDC``): see the registry rules.
- The funding interval is **per symbol and changes over time**: ``fundingInfo`` gives the current
  ``fundingIntervalHours`` (8h, 4h; a symbol that hits its rate cap is moved to 1h, then back), and
  **only lists symbols with non-default settings** -- a missing symbol is on the default 8h. The interval
  of each event is therefore read from the gap to the previous settlement, capped at 8h; ``funding`` looks
  back one 8h interval before ``start`` so that the first event of a window is not given the current
  interval by default.
- ``fundingRate`` is a **signed fraction per interval** (positive = longs pay), returns the **oldest**
  1000 events from ``startTime`` and is limited to 500 requests / 5 min per IP (shared across symbols):
  the client is paced at 1.5 req/s at most. Settlement times carry a few ms of jitter (``...600002``).
- ``klines`` / ``markPriceKlines`` return the oldest ``limit`` (max 1500) candles from ``startTime``;
  mark candles carry no volume.

Aster (``aster.py``) serves the same API shape and reuses this adapter.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.paging import forward_fetch, paged_fetch, window_count
from quant_marketdata.venues.schema import (
    CANDLE_COLUMNS,
    INSTRUMENT_COLUMNS,
    Dataset,
    empty_frame,
    finalize_funding,
    make_book,
)

BASE_URL = "https://fapi.binance.com/fapi/v1"
# 500 fundingRate requests per 5 minutes per IP.
MAX_RATE = 1.5
FUNDING_PAGE = 1000
KLINE_PAGE = 1500
DEFAULT_INTERVAL_H = 8
MAX_INTERVAL_H = 8
PERP_TYPES = frozenset({"PERPETUAL", "TRADIFI_PERPETUAL"})
RESOLUTION_SECONDS: Dict[str, int] = {
    "1m": 60,
    "3m": 3 * 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "2h": 2 * 60 * 60,
    "4h": 4 * 60 * 60,
    "6h": 6 * 60 * 60,
    "8h": 8 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
}
ASSET_CLASSES: Dict[str, Optional[str]] = {
    "COIN": "crypto",
    "EQUITY": "equity",
    "HK_EQUITY": "equity",
    "KR_EQUITY": "equity",
    "CN_EQUITY": "equity",
    "COMMODITY": "commodity",
    "INDEX": "index",
    "FX": "fx",
    "PREMARKET": "private",
}
STATUSES = {"TRADING": "active", "SETTLING": "inactive", "PENDING_TRADING": "pending"}


def _ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


# Book depths the depth endpoint accepts.
DEPTH_LIMITS = (5, 10, 20, 50, 100, 500, 1000)


class BinanceVenue:
    name = "binance"
    capabilities = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.CANDLES, Dataset.MARK_CANDLES,
                              Dataset.SPECS, Dataset.BOOK})
    resolutions = tuple(RESOLUTION_SECONDS)
    base_url = BASE_URL
    max_rate: Optional[float] = MAX_RATE
    default_interval_h = DEFAULT_INTERVAL_H
    max_interval_h = MAX_INTERVAL_H
    perp_types = PERP_TYPES

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0) -> None:
        if client is None:
            pace = rate if self.max_rate is None else min(rate, self.max_rate)
            client = JsonClient(self.base_url, rate=pace)
        self.client = client
        self._intervals: Optional[Dict[str, int]] = None

    # -- markets -----------------------------------------------------------------------------------

    def _symbols(self) -> List[Dict[str, Any]]:
        payload = self.client.get("exchangeInfo")
        return [s for s in payload.get("symbols") or [] if s.get("contractType") in self.perp_types]

    def funding_intervals(self) -> Dict[str, int]:
        """Current funding interval in hours per symbol (symbols absent from ``fundingInfo``: default)."""
        if self._intervals is None:
            self._intervals = {f["symbol"]: int(f["fundingIntervalHours"])
                               for f in self.client.get("fundingInfo") or []
                               if f.get("fundingIntervalHours")}
        return self._intervals

    def funding_interval(self, symbol: str) -> int:
        return self.funding_intervals().get(symbol, self.default_interval_h)

    def asset_class(self, market: Dict[str, Any]) -> Optional[str]:
        return ASSET_CLASSES.get(market.get("underlyingType") or "")

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        rows = []
        for m in self._symbols():
            onboard = int(m["onboardDate"]) if m.get("onboardDate") else None
            asset_class = self.asset_class(m)
            rows.append({
                "venue": self.name,
                "venue_symbol": m["symbol"],
                "venue_market_id": m.get("pair") or m["symbol"],
                "settle_asset": m.get("marginAsset"),
                "asset_class": asset_class,
                "underlying_ref": m.get("baseAsset") if asset_class not in (None, "crypto") else None,
                "chain": None,
                "contract_address": None,
                "funding_interval_h": float(self.funding_interval(m["symbol"])),
                "listed_at": pd.Timestamp(onboard, unit="ms") if onboard else pd.NaT,
                "delisted_at": pd.NaT,
                "status": STATUSES.get(m.get("status"), str(m.get("status", "")).lower()),
            })
        columns = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"]
        return pd.DataFrame(rows).reindex(columns=columns)

    def specs(self) -> pd.DataFrame:
        require(self, Dataset.SPECS)
        intervals = self.funding_intervals()
        rows = []
        for m in self._symbols():
            row = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in m.items()}
            row["fundingIntervalHours"] = intervals.get(m["symbol"], self.default_interval_h)
            rows.append(row)
        return pd.DataFrame(rows)

    # -- time series -------------------------------------------------------------------------------

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.FUNDING)
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        lookback = start - pd.Timedelta(hours=self.max_interval_h)
        end_ms = _ms(end)

        def fetch_from(cursor: int) -> pd.DataFrame:
            return parse_funding_rates(self.client.get("fundingRate", {
                "symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": FUNDING_PAGE,
            }) or [])

        raw = forward_fetch(fetch_from, _ms(lookback), end_ms, FUNDING_PAGE)
        events = finalize_funding(raw, self.funding_interval(symbol), self.max_interval_h)
        return events.loc[start:end]

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        if price == "last":
            require(self, Dataset.CANDLES)
            endpoint = "klines"
        elif price == "mark":
            require(self, Dataset.MARK_CANDLES)
            endpoint = "markPriceKlines"
        else:
            raise ValueError(f"price must be 'last' or 'mark', got {price!r}")
        if resolution not in RESOLUTION_SECONDS:
            raise ValueError(f"Unsupported resolution {resolution!r}; expected one of {self.resolutions}")
        step = RESOLUTION_SECONDS[resolution] * 1000

        def fetch(a: int, b: int) -> pd.DataFrame:
            return parse_klines(self.client.get(endpoint, {
                "symbol": symbol, "interval": resolution, "startTime": a, "endTime": b,
                "limit": window_count(a, b, step, KLINE_PAGE),
            }) or [], price)

        frame = paged_fetch(fetch, _ms(start), _ms(end), step * KLINE_PAGE)
        if frame.empty:
            return empty_frame(CANDLE_COLUMNS)
        return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book from ``depth`` (Binance-compatible: Binance and Aster)."""
        require(self, Dataset.BOOK)
        limit = next((n for n in DEPTH_LIMITS if n >= depth), DEPTH_LIMITS[-1])
        payload = self.client.get("depth", {"symbol": symbol, "limit": limit})
        stamp = payload.get("T") or payload.get("E")
        return make_book(payload.get("bids") or [], payload.get("asks") or [], venue=self.name, symbol=symbol,
                         depth=depth, ts=pd.Timestamp(int(stamp), unit="ms") if stamp else None)

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        # openInterestHist only reaches 30 days back: no history worth storing.
        require(self, Dataset.DAILY_METRICS)
        raise AssertionError("unreachable")


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_funding_rates(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Raw ``fundingRate`` records -> ``rate_raw`` and signed fractional ``rate`` (already signed)."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["fundingTime"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "rate_raw": frame["fundingRate"].astype(str).to_numpy(),
        "direction_raw": None,
        "rate": frame["fundingRate"].astype(float).to_numpy(),
    }, index=index)
    return out.sort_index()


def parse_klines(rows: List[List[Any]], price: str) -> pd.DataFrame:
    """``[open_time, o, h, l, c, volume, close_time, quote_volume, trades, ...]`` rows -> candles."""
    if not rows:
        return pd.DataFrame()
    frame = pd.DataFrame([r[:9] for r in rows],
                         columns=["t", "o", "h", "l", "c", "v", "T", "q", "n"])
    index = pd.DatetimeIndex(pd.to_datetime(frame["t"].astype("int64"), unit="ms"), name="ts")
    mark = price != "last"  # mark candles report zero volume: not a volume
    out = pd.DataFrame({
        "open": frame["o"].astype(float).to_numpy(),
        "high": frame["h"].astype(float).to_numpy(),
        "low": frame["l"].astype(float).to_numpy(),
        "close": frame["c"].astype(float).to_numpy(),
        "volume_base": float("nan") if mark else frame["v"].astype(float).to_numpy(),
        "volume_quote": float("nan") if mark else frame["q"].astype(float).to_numpy(),
        "price_type": price,
    }, index=index)
    if not mark:
        out["trade_count"] = frame["n"].astype("int64").to_numpy()
    return out.sort_index()


register_venue(BinanceVenue.name, BinanceVenue)

__all__ = ["BinanceVenue", "parse_funding_rates", "parse_klines"]
