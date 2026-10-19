"""Paradex (Starknet appchain perp DEX) adapter over the public REST API, without its SDK: perps here, the
dated options in ``paradex_options.py`` (mixed into ``ParadexVenue``).

API quirks this adapter absorbs (verified 2026-10-18):

- ``markets`` lists perps (``asset_kind=PERP``), dated options and a few spot markets, with no status: a
  delisted perp **disappears** from the listing (ARB, OP, TIA, WIF...), although ``funding/data`` still
  serves its history by name. The registry marks a vanished symbol delisted, from the first download on.
  ``tags`` say ``RWA`` for every TradFi perp without its class (overrides classify them); the other tags
  are crypto sectors. ``fee_config.api_fee.taker_fee.fee`` is the API taker fee (a fraction).
- **Funding is continuous.** ``funding/data`` serves one record every 5 s, newest first: ``funding_rate``
  per ``funding_period_hours`` (8), ``funding_rate_8h``, ``funding_premium`` (= rate x spot/USDC price)
  and ``funding_index``, which accrues ``premium x dt / period`` per tick (``dt`` treated as 0 after a gap
  of more than 30 s) in USDC per unit of the asset. A position's funding is ``-size x (index - cached
  index)``: a rising index (positive rate) makes **longs pay** (docs: risk/funding-mechanism). The funding
  multiplier (0.5 on TradFi perps) is already inside the published rate.
- Canonical events: the index is sampled at every whole hour (the newest record of the hour, one request
  with ``page_size=1``) and each hour gets a **settlement-equivalent event** ``rate = (index_h -
  index_{h-1}) / price``, ``price`` being the mean of ``premium / rate`` at both samples, i.e. the exact
  cash flow per unit of notional. Checked on full hours of records: the index increments equal the sum of
  ``premium x dt / period`` to 1e-6, and ``rate`` equals the time-average of ``funding_rate_8h`` / 8 to
  0.1-0.4 % (BTC, ETH, SOL, NVDA, XAU, kPEPE, 2024-2026). A missing hour (outage) merges into the next
  event, whose ``interval_h`` is then the real gap. ``rate_raw`` is ``funding_rate_8h`` at the sample and
  ``funding_index_raw`` the index, as served.
- Sign, cross-checked 2026-10-18 on all 66 perps: the 9 with a negative rate all had mark < spot, and the
  rate correlates with the mark-spot premium (0.76); Binance's sign agreed on 7 of those 9. Over
  2026-08-15..10-18, the daily mean ``rate_1h`` of BTC (ETH) correlates positively with each of the five
  other venues: 0.32-0.80 (0.25-0.45).
- **History before 2024-03-12 08:00 UTC is not served.** Until 06:09:13 that day (BTC, ETH, SOL, STRK)
  the stored records have ``funding_index`` and ``funding_premium`` swapped, 06:09:13-06:15:53 holds
  duplicate records in both orders, and once un-swapped the old index accrues at exactly one third of
  what ``funding_rate_8h`` implies: which of the two was paid is ambiguous. The first event is the first
  hour whose two samples are both consistent records (06:59 and 07:59).
- One request per hour of funding history, each taking ~0.9 s: the samples are fetched by 16 threads
  sharing the client's rate limiter, paced at 20 req/s at most (public limit 1500 requests per minute per
  IP). Measured: ~13 req/s with ``--rate 20``, i.e. ~11 min per market-year; ~1 req/s sequentially.
- ``markets/klines`` takes ``start_at``/``end_at`` in ms (end exclusive), at most **1440 bars** per
  request, resolutions 1, 3, 5, 15, 30 and 60 minutes, ``price_kind`` last or mark; rows are
  ``[t, o, h, l, c, volume]`` oldest first with the volume in USD, and hours without trades are absent.
- ``orderbook/<market>`` serves aggregated levels, ``depth`` at most 100, without RPI orders.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Any, Dict, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.paging import paged_fetch
from quant_marketdata.venues.paradex_options import OPTION_DATASETS, ParadexOptions
from quant_marketdata.venues.schema import (
    CANDLE_COLUMNS,
    FUNDING_COLUMNS,
    HOURS_PER_YEAR,
    INSTRUMENT_COLUMNS,
    Dataset,
    empty_frame,
    make_book,
)

BASE_URL = "https://api.prod.paradex.trade/v1"
MAX_RATE = 20.0
FUNDING_WORKERS = 16  # requests are latency-bound; the shared limiter keeps the pace
FUNDING_INTERVAL_H = 1  # one settlement-equivalent event per hour
# Records from this time on are consistent (see the module docstring); the first event needs two samples.
INDEX_CONSISTENT_FROM = pd.Timestamp("2024-03-12 06:16")
FIRST_EVENT = pd.Timestamp("2024-03-12 08:00")
KLINE_PAGE = 1000  # bars per request, under the API's 1440
MAX_BOOK_DEPTH = 100
RESOLUTION_MINUTES: Dict[str, int] = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60}
CRYPTO_TAGS = frozenset({"LAYER-1", "LAYER-2", "DEFI", "AI", "MEME"})
HOUR = pd.Timedelta(hours=1)


def _ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


def asset_class(market: Dict[str, Any]) -> Optional[str]:
    tags = set(market.get("tags") or [])
    if tags and tags <= CRYPTO_TAGS:
        return "crypto"
    return None  # RWA (classified by overrides) or untagged (the registry defaults it, flagged)


class ParadexVenue(ParadexOptions):
    name = "paradex"
    capabilities = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.CANDLES, Dataset.MARK_CANDLES,
                              Dataset.SPECS, Dataset.BOOK}) | OPTION_DATASETS
    resolutions = tuple(RESOLUTION_MINUTES)

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0,
                 workers: int = FUNDING_WORKERS) -> None:
        self.client = client or JsonClient(BASE_URL, rate=min(rate, MAX_RATE))
        self.workers = workers

    def _markets(self) -> List[Dict[str, Any]]:
        markets = self.client.get("markets").get("results") or []
        return [m for m in markets if m.get("asset_kind") == "PERP"]

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
        rows = []
        for m in self._markets():
            opened = pd.Timestamp(int(m["open_at"]), unit="ms") if m.get("open_at") else pd.NaT
            rows.append({
                "venue": self.name,
                "venue_symbol": m["symbol"],
                "venue_market_id": m["symbol"],
                "settle_asset": m.get("settlement_currency"),
                "asset_class": asset_class(m),
                "chain": None,
                "contract_address": None,
                "funding_interval_h": float(FUNDING_INTERVAL_H),
                "listed_at": opened,
                "delisted_at": pd.NaT,
                "status": "pending" if pd.notna(opened) and opened > now else "active",
                "session": None,  # not declared; the TradFi perps trade at weekends too
            })
        columns = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"]
        return pd.DataFrame(rows).reindex(columns=columns)

    def specs(self) -> pd.DataFrame:
        require(self, Dataset.SPECS)
        rows = []
        for m in self._markets():
            row = {k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in m.items()}
            rows.append({"symbol": m["symbol"], **row})
        return pd.DataFrame(rows)

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Hourly settlement-equivalent events in ``[start, end]``, from the funding index sampled at every
        whole hour (one request per hour, plus the sample an hour before ``start``)."""
        require(self, Dataset.FUNDING)
        first = max(pd.Timestamp(start).ceil("h"), FIRST_EVENT)
        last = pd.Timestamp(end).floor("h")
        if first > last:
            return empty_frame(FUNDING_COLUMNS)
        def sample(hour: pd.Timestamp) -> List[Dict[str, Any]]:
            boundary = _ms(hour)
            payload = self.client.get("funding/data", {
                "market": symbol, "start_at": boundary - 3_600_000 + 1, "end_at": boundary, "page_size": 1,
            })
            return payload.get("results") or []

        hours = pd.date_range(first - HOUR, last, freq="h")
        with ThreadPoolExecutor(max_workers=max(1, min(self.workers, len(hours)))) as pool:
            samples = [record for page in pool.map(sample, hours) for record in page]
        events = parse_funding_samples(samples)
        return events.loc[first:last] if not events.empty else events

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        if price == "last":
            require(self, Dataset.CANDLES)
        elif price == "mark":
            require(self, Dataset.MARK_CANDLES)
        else:
            raise ValueError(f"price must be 'last' or 'mark', got {price!r}")
        return self._klines(symbol, resolution, start, end, price)

    def _klines(self, symbol: str, resolution: str, start: datetime, end: datetime,
                price_kind: str) -> pd.DataFrame:
        if resolution not in RESOLUTION_MINUTES:
            raise ValueError(f"Unsupported resolution {resolution!r}; expected one of {self.resolutions}")
        step = RESOLUTION_MINUTES[resolution] * 60_000

        def fetch(a: int, b: int) -> pd.DataFrame:
            payload = self.client.get("markets/klines", {
                "symbol": symbol, "resolution": RESOLUTION_MINUTES[resolution], "start_at": a,
                "end_at": b + 1, "price_kind": price_kind,  # end is exclusive: + 1 ms keeps the bar at b
            })
            return parse_klines(payload.get("results") or [], price_kind)

        frame = paged_fetch(fetch, _ms(start), _ms(end), step * KLINE_PAGE)
        if frame.empty:
            return empty_frame(CANDLE_COLUMNS)
        return frame.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book from ``orderbook/<market>`` (aggregated levels, at most 100 a side)."""
        require(self, Dataset.BOOK)
        payload = self.client.get(f"orderbook/{symbol}", {"depth": min(depth, MAX_BOOK_DEPTH)})
        ts = pd.Timestamp(int(payload["last_updated_at"]), unit="ms") if payload.get("last_updated_at") else None
        return make_book(payload.get("bids") or [], payload.get("asks") or [],
                         venue=self.name, symbol=symbol, ts=ts, depth=depth)

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        require(self, Dataset.DAILY_METRICS)  # no open-interest history in the public API
        raise AssertionError("unreachable")


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_funding_samples(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Funding-index samples (the newest ``funding/data`` record of each hour) -> one settlement-equivalent
    event per sampled hour, stamped at the hour, with the canonical funding columns plus
    ``funding_index_raw``. The first sample only serves as the starting point; records older than
    ``INDEX_CONSISTENT_FROM`` are ignored."""
    if not records:
        return empty_frame(FUNDING_COLUMNS)
    frame = pd.DataFrame(records)
    created = pd.to_datetime(frame["created_at"].astype("int64"), unit="ms")
    frame = frame.assign(hour=created.dt.ceil("h"), created=created)
    frame = frame[frame["created"] >= INDEX_CONSISTENT_FROM]
    frame = frame.sort_values("created").drop_duplicates("hour", keep="last")
    if len(frame) < 2:
        return empty_frame(FUNDING_COLUMNS)
    index = frame["funding_index"].astype(float)
    rate = frame["funding_rate"].astype(float)
    price = (frame["funding_premium"].astype(float) / rate).where(rate != 0)
    mean_price = pd.concat([price, price.shift()], axis=1).mean(axis=1)
    step = index.diff()
    fraction = (step / mean_price).where(step != 0, 0.0)
    interval = frame["hour"].diff().dt.total_seconds() / 3600
    out = pd.DataFrame({
        "rate_raw": frame["funding_rate_8h"].astype(str).to_numpy(),
        "direction_raw": None,
        "interval_h": interval.to_numpy(),
        "rate": fraction.to_numpy(),
        "funding_index_raw": frame["funding_index"].astype(str).to_numpy(),
    }, index=pd.DatetimeIndex(frame["hour"], name="ts"))
    out = out.iloc[1:].dropna(subset=["rate"])
    out["rate_1h"] = out["rate"] / out["interval_h"]
    out["apr"] = out["rate_1h"] * HOURS_PER_YEAR
    return out[FUNDING_COLUMNS + ["funding_index_raw"]]


def parse_klines(records: List[List[Any]], price: str) -> pd.DataFrame:
    """``[t, o, h, l, c, volume_usd]`` rows -> candles; mark candles carry no volume."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records, columns=["t", "o", "h", "l", "c", "v"])
    index = pd.DatetimeIndex(pd.to_datetime(frame["t"].astype("int64"), unit="ms"), name="ts")
    out = pd.DataFrame({
        "open": frame["o"].astype(float).to_numpy(),
        "high": frame["h"].astype(float).to_numpy(),
        "low": frame["l"].astype(float).to_numpy(),
        "close": frame["c"].astype(float).to_numpy(),
        "volume_base": float("nan"),
        "volume_quote": frame["v"].astype(float).to_numpy() if price == "last" else float("nan"),
        "price_type": price,
    }, index=index)
    return out.sort_index()


register_venue(ParadexVenue.name, ParadexVenue)

__all__ = ["FIRST_EVENT", "INDEX_CONSISTENT_FROM", "ParadexVenue", "asset_class", "parse_funding_samples", "parse_klines"]
