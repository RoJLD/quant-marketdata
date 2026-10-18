"""Lighter (zkLighter perp DEX) adapter over the public REST API, without the ``lighter-sdk`` package.

API quirks this adapter absorbs (verified 2026-10-15):

- ``orderBookDetails`` only lists perps with ``filter=perp`` (``perps`` is rejected); delisted markets stay
  listed with ``status=inactive``.
- ``candles`` / ``markPriceCandles`` return at most 500 points, the **most recent** of the window, and
  **extend the window backwards until it holds ``count_back`` points** -- so ``count_back`` must equal the
  window size or the requested start is not honoured. Candle times are in ms, query bounds in seconds.
- Mark price history only starts around 2025-08-25: the first day is located with a coarse probe instead
  of paging through months of empty windows.
- ``fundings`` is hourly, ``timestamp`` in seconds; ``rate`` is **unsigned, in percent per hour**, with
  the sign in ``direction``: ``long`` = longs pay. Confirmed 2026-10-17: the websocket ``market_stats``
  ``funding_rate`` is signed and equals the REST rate with sign + for ``long``, - for ``short`` on 7/7
  markets checked (and every ``short`` market had a negative mark-index premium). The rate is
  rounded to 4 decimals of a percent, i.e. a 1e-6/h (~0.88 %/yr) grid.
- ``market_config.trading_hours`` is empty (continuous) on every perp (checked 2026-10-18); any other
  value is passed on as the session, which the registry reports as unresolved until it is mapped.
- ``exchangeMetrics`` open interest is the notional of **both sides**; series are padded with zeros before
  listing, and an unknown symbol yields zeros rather than an error.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.paging import first_non_empty, paged_fetch, window_count
from quant_marketdata.venues.schema import (
    CANDLE_COLUMNS,
    DAILY_METRICS_COLUMNS,
    INSTRUMENT_COLUMNS,
    Dataset,
    empty_frame,
    finalize_funding,
    make_book,
)

BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"
MARKET_FILTER = "perp"
MAX_POINTS = 500
FUNDING_INTERVAL_H = 1
RESOLUTION_SECONDS: Dict[str, int] = {
    "1m": 60,
    "5m": 5 * 60,
    "15m": 15 * 60,
    "30m": 30 * 60,
    "1h": 60 * 60,
    "4h": 4 * 60 * 60,
    "12h": 12 * 60 * 60,
    "1d": 24 * 60 * 60,
}


def _seconds(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).timestamp())


def _session(market: Dict[str, Any]) -> Optional[str]:
    config = market.get("market_config")
    if not isinstance(config, dict) or "trading_hours" not in config:
        return None
    hours = config["trading_hours"]
    return "24x7" if hours == "" else str(hours).lower()


def _check(payload: Dict[str, Any]) -> Dict[str, Any]:
    code = payload.get("code", 200)
    if code != 200:
        raise ValueError(f"Lighter API error {code}: {payload.get('message', '')}")
    return payload


class LighterVenue:
    name = "lighter"
    capabilities = frozenset(Dataset)
    resolutions = tuple(RESOLUTION_SECONDS)

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0) -> None:
        self.client = client or JsonClient(BASE_URL, rate=rate)
        self._market_ids: Optional[Dict[str, int]] = None

    # -- markets -----------------------------------------------------------------------------------

    def _markets(self) -> List[Dict[str, Any]]:
        payload = _check(self.client.get("orderBookDetails", {"filter": MARKET_FILTER}))
        markets = payload.get("order_book_details") or []
        self._market_ids = {m["symbol"]: int(m["market_id"]) for m in markets}
        return markets

    def market_id(self, symbol: str) -> int:
        if self._market_ids is None:
            self._markets()
        try:
            return self._market_ids[symbol]
        except KeyError:
            raise ValueError(f"Unknown Lighter market {symbol!r}") from None

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        rows = []
        for m in self._markets():
            created = int(m["created_at"]) if m.get("created_at") else None
            rows.append({
                "venue": self.name,
                "venue_symbol": m["symbol"],
                "venue_market_id": str(m["market_id"]),
                "settle_asset": "USDC",
                "chain": None,
                "contract_address": None,
                "funding_interval_h": float(FUNDING_INTERVAL_H),
                "listed_at": pd.Timestamp(created, unit="ms") if created else pd.NaT,
                "delisted_at": pd.NaT,
                "status": m.get("status"),
                "session": _session(m),
            })
        columns = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"]
        return pd.DataFrame(rows).reindex(columns=columns)

    def specs(self) -> pd.DataFrame:
        require(self, Dataset.SPECS)
        rows = []
        for m in self._markets():
            # Nested fields (daily chart, market config) are kept as JSON text.
            rows.append({k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in m.items()})
        return pd.DataFrame(rows)

    # -- time series -------------------------------------------------------------------------------

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.FUNDING)
        market_id = self.market_id(symbol)
        step = FUNDING_INTERVAL_H * 3600

        def fetch(a: int, b: int) -> pd.DataFrame:
            payload = _check(self.client.get("fundings", {
                "market_id": market_id, "resolution": "1h", "start_timestamp": a,
                "end_timestamp": b, "count_back": window_count(a, b, step, MAX_POINTS),
            }))
            return parse_fundings(payload.get("fundings") or [])

        raw = paged_fetch(fetch, _seconds(start), _seconds(end), step * MAX_POINTS)
        if raw.empty:
            return finalize_funding(raw, FUNDING_INTERVAL_H)
        raw = raw.loc[pd.Timestamp(start):pd.Timestamp(end)]
        return finalize_funding(raw, FUNDING_INTERVAL_H)

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        if price == "last":
            require(self, Dataset.CANDLES)
            endpoint = "candles"
        elif price == "mark":
            require(self, Dataset.MARK_CANDLES)
            endpoint = "markPriceCandles"
        else:
            raise ValueError(f"price must be 'last' or 'mark', got {price!r}")
        if resolution not in RESOLUTION_SECONDS:
            raise ValueError(f"Unsupported resolution {resolution!r}; expected one of {self.resolutions}")
        market_id = self.market_id(symbol)

        def fetch_at(res: str):
            step = RESOLUTION_SECONDS[res]

            def fetch(a: int, b: int) -> pd.DataFrame:
                payload = _check(self.client.get(endpoint, {
                    "market_id": market_id, "resolution": res, "start_timestamp": a,
                    "end_timestamp": b, "count_back": window_count(a, b, step, MAX_POINTS),
                }))
                return parse_candles(payload.get("c") or [], price)

            return fetch

        first, last = _seconds(start), _seconds(end)
        day = RESOLUTION_SECONDS["1d"]
        # The probe only pays off over long ranges; on a sub-day range the API rejects a 1d window
        # (it floors both bounds to the same day), and incremental updates are always short.
        if price == "mark" and last - first > 2 * day:
            found = first_non_empty(fetch_at("1d"), first - first % day, last, day * MAX_POINTS)
            if found is None:
                return empty_frame(CANDLE_COLUMNS)
            first = max(first, _seconds(found))
        frame = paged_fetch(fetch_at(resolution), first, last, RESOLUTION_SECONDS[resolution] * MAX_POINTS)
        if frame.empty:
            return empty_frame(CANDLE_COLUMNS)
        return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book from ``orderBookOrders`` (individual orders, up to 250 per side, aggregated by price)."""
        require(self, Dataset.BOOK)
        payload = _check(self.client.get("orderBookOrders", {"market_id": self.market_id(symbol), "limit": 250}))

        def orders(side):
            return [(o["price"], o["remaining_base_amount"]) for o in payload.get(side) or []]

        return make_book(orders("bids"), orders("asks"), venue=self.name, symbol=symbol, depth=depth)

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        require(self, Dataset.DAILY_METRICS)
        series = {}
        for kind, column in (("open_interest", "open_interest_usd"), ("volume", "volume_usd")):
            payload = _check(self.client.get("exchangeMetrics", {
                "period": "all", "kind": kind, "filter": "byMarket", "value": symbol,
            }))
            series[column] = pd.Series(
                {pd.Timestamp(int(m["timestamp"]), unit="s"): float(m["data"])
                 for m in payload.get("metrics") or []},
                dtype="float64",
            )
        return parse_daily_metrics(series["open_interest_usd"], series["volume_usd"])


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_fundings(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Raw Lighter funding records -> ``rate_raw``, ``direction_raw`` and signed fractional ``rate``."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["timestamp"].astype("int64"), unit="s"), name="ts")
    sign = frame["direction"].map({"long": 1.0, "short": -1.0})
    if sign.isna().any():
        unknown = sorted(set(frame.loc[sign.isna(), "direction"].astype(str)))
        raise ValueError(f"Unexpected Lighter funding direction(s): {unknown}")
    out = pd.DataFrame({
        "rate_raw": frame["rate"].astype(str).to_numpy(),
        "direction_raw": frame["direction"].astype(str).to_numpy(),
        "rate": (sign * frame["rate"].astype(float) / 100).to_numpy(),
    }, index=index)
    return out.sort_index()


def parse_candles(records: List[Dict[str, Any]], price: str) -> pd.DataFrame:
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    index = pd.DatetimeIndex(pd.to_datetime(frame["t"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "open": frame["o"].astype(float).to_numpy(),
        "high": frame["h"].astype(float).to_numpy(),
        "low": frame["l"].astype(float).to_numpy(),
        "close": frame["c"].astype(float).to_numpy(),
        "volume_base": frame["v"].astype(float).to_numpy() if "v" in frame else float("nan"),
        "volume_quote": frame["V"].astype(float).to_numpy() if "V" in frame else float("nan"),
        "price_type": price,
    }, index=index)
    if "i" in frame:
        out["last_trade_id"] = frame["i"].astype("int64").to_numpy()
    if "sc" in frame:
        out["sample_count"] = frame["sc"].astype("int64").to_numpy()
    return out.sort_index()


def parse_daily_metrics(open_interest: pd.Series, volume: pd.Series) -> pd.DataFrame:
    """Align the two daily series and drop the zero padding the API adds before listing."""
    frame = pd.DataFrame({"open_interest_usd": open_interest, "volume_usd": volume}).sort_index()
    frame.index.name = "ts"
    active = frame.fillna(0).ne(0).any(axis=1)
    if not active.any():
        return empty_frame(DAILY_METRICS_COLUMNS)
    frame = frame.loc[active.idxmax():].copy()
    frame["oi_convention"] = "both_sides"
    return frame[DAILY_METRICS_COLUMNS]


register_venue(LighterVenue.name, LighterVenue)

__all__ = ["LighterVenue", "parse_candles", "parse_daily_metrics", "parse_fundings"]
