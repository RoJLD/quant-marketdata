"""Kalshi (CFTC-regulated prediction market) adapter for binary contracts over the public REST API
(``https://api.elections.kalshi.com/trade-api/v2``, GET, no key), without its SDK. ``BinaryVenue`` only;
data use only (trading from France is not established).

API quirks this adapter absorbs (verified 2026-10-18/19):

- Only the series of ``contracts.overrides.KALSHI_SERIES`` are served (KXBTCD, KXBTC, KXETHD, KXETH): the
  underlying and the settlement index come from that table, never from a series or ticker name. **Tickers
  are never parsed**: they encode New York local time (``KXBTCD-26OCT1806`` fixes at 10:00 UTC); the expiry
  is the event's ``strike_date``.
- KXBTCD mixes three cadences (``product_metadata.cadence``: hourly events on a $100 grid of 188
  thresholds, daily at 21:00 UTC on $250, weekly on Fridays on $500). An hourly event opens 1 h before its
  fixing; events older than the historical cutoff carry no ``product_metadata`` (cadence left empty).
- Markets: ``strike_type`` ``greater`` (YES iff value > ``floor_strike``), ``less`` (YES iff value <
  ``cap_strike``), ``between`` (YES iff ``floor_strike`` <= value <= ``cap_strike``), per the rules text.
  The fixing value is in cents, so the canonical bounds are ``greater`` -> [floor + 0.01, inf), ``less`` ->
  (-inf, cap), ``between`` -> [floor, cap + 0.01). Any other strike type is left unbounded (unresolved).
  Prices are dollars (``*_dollars``) on a 1-cent grid; sizes are fixed-point strings (``*_fp``), fractional.
  ``yes_bid`` 0.00 and ``yes_ask`` 1.00 (orders are 1-99 cents) mean **no quote** on that side: NaN, so
  ``p_mid`` is NaN (never 0.5).
- Settlement per market: ``expiration_value`` (the index value at the fixing, in cents) and ``result``
  (``yes``/``no``; ``""`` when not settled or voided; any other value -> NaN outcome and a warning). The
  fixing **rule changed**: a 2025-01-01 market averages the 60 s of BRTI excluding the top and bottom 20 %,
  2026 markets take the simple 60 s mean -> ``fixing_method`` is read per market (``rules_primary``).
- ``/events?series_ticker&status&with_nested_markets=true`` lists events (cursor pages of up to 200,
  **newest first**) with their markets nested in each event; it **ignores** ``min_close_ts``. ``GET
  /events/<e>`` answers for any event, with the markets at the top level -- empty for an event older than
  the **historical cutoff** (``/historical/cutoff`` ``market_settled_ts``, 2026-08-20 on 2026-10-19),
  whose markets come from ``/historical/markets?event_ticker`` (that endpoint ignores ``min_close_ts``
  too). Historical payloads use the same market fields.
- Candles: ``period_interval`` in {1, 60, 1440} only. Per event (current tier only:
  ``/series/<s>/events/<e>/candlesticks``) for every market at once, **truncated** at ~10,000 candles
  with ``adjusted_end_ts`` where the next request resumes; minutes without a change are absent. Per
  market: ``/series/<s>/markets/<t>/candlesticks`` (current) or ``/historical/markets/<t>/candlesticks``
  (before the cutoff, **unsuffixed** field names: ``close`` instead of ``close_dollars``, ``volume``
  instead of ``volume_fp``); the historical tier has no event-level or batch endpoint, so an old event
  costs one request per market. ``price`` is empty without trades.
- Trades: ``/markets/trades?ticker&min_ts&max_ts`` (current) and ``/historical/trades`` (before the
  cutoff), cursor pages; ``taker_side`` is ``yes``/``no``, ``yes_price_dollars`` the YES price.
- Book: ``/markets/<t>/orderbook`` serves two ladders of **bids** (``yes_dollars``, ``no_dollars``); the
  YES asks are ``1 - NO bids``.
- Fees: series ``fee_type=quadratic``, ``fee_multiplier=1``; the coefficient and rounding could not be read
  from the published schedule (``contracts.fees``: ``unconfirmed``, spec Q5).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.contracts.indexes import parse_fixing_method, settlement_index_info
from quant_marketdata.venues.contracts.overrides import KALSHI_SERIES
from quant_marketdata.venues.contracts.schema import (
    BINARY_CANDLE_COLUMNS,
    BINARY_QUOTE_COLUMNS,
    BINARY_SNAPSHOT_COLUMNS,
    BINARY_TRADE_COLUMNS,
    CADENCES,
    SETTLEMENT_COLUMNS,
    frame_of,
    outcome_from_bounds,
    p_mid,
    to_float,
    utc_naive,
)
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.schema import Dataset, make_book

logger = logging.getLogger(__name__)

BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
VENUE = "kalshi"
EVENT_PAGE = 200
MARKET_PAGE = 1000
PRICE_TICK = Decimal("0.01")
PERIODS: Dict[str, int] = {"1m": 1, "1h": 60, "1d": 1440}
_STATUSES = {"initialized": "scheduled", "unopened": "scheduled", "active": "active", "open": "active",
             "closed": "expired", "determined": "expired", "settled": "expired", "finalized": "expired",
             "disputed": "expired", "amended": "expired"}
_RESULTS = {"yes": 1.0, "no": 0.0, "": float("nan")}


def _seconds(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).timestamp())


def kalshi_bounds(strike_type: Optional[str], floor, cap) -> tuple[float, float]:
    """Canonical ``[k_low, k_high)`` of a Kalshi market, on the fixing's 1-cent resolution."""
    nan = float("nan")
    floor, cap = to_float(floor), to_float(cap)
    if strike_type == "greater" and floor == floor:
        return float(Decimal(str(floor)) + PRICE_TICK), nan
    if strike_type == "less" and cap == cap:
        return nan, cap
    if strike_type == "between" and floor == floor and cap == cap:
        return floor, float(Decimal(str(cap)) + PRICE_TICK)
    return nan, nan


def _outcome(result: object, ticker: str) -> float:
    if result is None:
        return float("nan")
    value = _RESULTS.get(str(result))
    if value is None:
        logger.warning("Kalshi %s: unexpected result %r (outcome left NaN)", ticker, result)
        return float("nan")
    return value


def _cadence(event: Dict[str, Any]) -> Optional[str]:
    cadence = (event.get("product_metadata") or {}).get("cadence")
    if cadence is None:
        return None
    return cadence if cadence in CADENCES else "other"


def _bid(value) -> float:
    price = to_float(value)
    return price if price > 0 else float("nan")


def _ask(value) -> float:
    price = to_float(value)
    return price if 0 < price < 1 else float("nan")


class KalshiVenue:
    name = VENUE
    capabilities = frozenset({Dataset.BINARY_CONTRACTS, Dataset.BINARY_QUOTES, Dataset.BINARY_CANDLES,
                              Dataset.BINARY_TRADES, Dataset.SETTLEMENTS, Dataset.BOOK})
    resolutions = tuple(PERIODS)

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0,
                 series_table: Optional[Dict[str, Dict[str, object]]] = None) -> None:
        self.client = client or JsonClient(BASE_URL, rate=rate)
        self.series_table = KALSHI_SERIES if series_table is None else series_table
        self._series_info: Dict[str, Dict[str, Any]] = {}
        self._cutoff: Optional[pd.Timestamp] = None

    def _declared(self, series: str) -> Dict[str, object]:
        try:
            return self.series_table[series]
        except KeyError:
            raise ValueError(f"Kalshi series {series!r} is not in the series table: not downloaded") from None

    def series(self, series: str) -> Dict[str, Any]:
        """The venue's description of a declared series (fee type and multiplier...), cached."""
        self._declared(series)
        if series not in self._series_info:
            self._series_info[series] = self.client.get(f"series/{series}").get("series") or {}
        return self._series_info[series]

    def historical_cutoff(self) -> pd.Timestamp:
        """Markets settled before this instant live in the historical tier."""
        if self._cutoff is None:
            self._cutoff = utc_naive(self.client.get("historical/cutoff")["market_settled_ts"])
        return self._cutoff

    # -- listings ------------------------------------------------------------------------------------

    def events(self, series: str, status: str = "open", *, nested: bool = True,
               since: Optional[datetime] = None, until: Optional[datetime] = None) -> List[Dict[str, Any]]:
        """Events of a declared series, newest first, with their markets when ``nested``; ``since`` /
        ``until`` bound the ``strike_date`` (paging stops once a page is older than ``since``)."""
        self._declared(series)
        since = pd.Timestamp(since) if since is not None else None
        until = pd.Timestamp(until) if until is not None else None
        found, cursor, seen = [], None, set()
        while True:
            payload = self.client.get("events", {"series_ticker": series, "status": status, "limit": EVENT_PAGE,
                                                 "with_nested_markets": nested, "cursor": cursor})
            page = payload.get("events") or []
            for event in page:
                strike = utc_naive(event.get("strike_date"))
                if (since is None or strike >= since) and (until is None or strike <= until):
                    found.append(event)
            cursor = payload.get("cursor")
            oldest = min((utc_naive(e.get("strike_date")) for e in page), default=None)
            if not cursor or not page or cursor in seen or (since is not None and oldest < since):
                return found
            seen.add(cursor)

    def event(self, event: str) -> Dict[str, Any]:
        """One event with its markets, from either tier."""
        payload = self.client.get(f"events/{event}")
        info = dict(payload.get("event") or {})
        markets = payload.get("markets") or info.pop("markets", None) or []
        info["tier"] = "current"
        if not markets:
            markets = self._paged("historical/markets", {"event_ticker": event}, "markets")
            info["tier"] = "historical"
        info["markets"] = markets
        return info

    def _paged(self, path: str, params: Dict[str, Any], key: str) -> List[Dict[str, Any]]:
        records, cursor, seen = [], None, set()
        while True:
            payload = self.client.get(path, {**params, "limit": params.get("limit", MARKET_PAGE),
                                             "cursor": cursor})
            records += payload.get(key) or []
            cursor = payload.get("cursor")
            if not cursor or not payload.get(key) or cursor in seen:
                return records
            seen.add(cursor)

    # -- BinaryVenue ---------------------------------------------------------------------------------

    def binary_contracts(self, series: str, *, status: str = "open", start: Optional[datetime] = None,
                         end: Optional[datetime] = None) -> pd.DataFrame:
        """Markets of a declared series; ``status`` is Kalshi's event filter (``open``, ``settled``...),
        ``start``/``end`` bound the expiry. Events past the historical cutoff are completed from the
        historical tier (one request per event)."""
        require(self, Dataset.BINARY_CONTRACTS)
        info = self.series(series)
        frames = []
        for event in self.events(series, status, since=start, until=end):
            markets = event.get("markets") or []
            if not markets:
                markets = self._paged("historical/markets", {"event_ticker": event["event_ticker"]}, "markets")
            frames.append(parse_markets(markets, event, info))
        frames = [f for f in frames if not f.empty]
        return pd.concat(frames, ignore_index=True) if frames else frame_of([], BINARY_SNAPSHOT_COLUMNS)

    def binary_quotes(self, series: str) -> pd.DataFrame:
        """Live quotes of the series' open markets (one listing request per 1000 markets)."""
        require(self, Dataset.BINARY_QUOTES)
        self._declared(series)
        markets = self._paged("markets", {"series_ticker": series, "status": "open"}, "markets")
        return parse_market_quotes(markets, received_at=pd.Timestamp.now(tz="UTC").tz_localize(None))

    def binary_candles(self, event: str, period: str, start: datetime, end: datetime, *,
                       series: Optional[str] = None) -> pd.DataFrame:
        """Candles of every market of an event (``period`` 1m, 1h or 1d). Current tier: the event endpoint,
        resumed from ``adjusted_end_ts``; historical tier: one request per market."""
        require(self, Dataset.BINARY_CANDLES)
        interval = _interval(period)
        info = self.event(event)
        series = series or info.get("series_ticker")
        self._declared(series)
        markets = info.get("markets") or []
        if info["tier"] == "historical":
            frames = [self.market_candles(m["ticker"], period, start, end, series=series, historical=True)
                      for m in markets]
        else:
            frames, cursor, last = [], _seconds(start), _seconds(end)
            while cursor < last:
                payload = self.client.get(f"series/{series}/events/{event}/candlesticks", {
                    "start_ts": cursor, "end_ts": last, "period_interval": interval})
                for ticker, candles in zip(payload.get("market_tickers") or [],
                                           payload.get("market_candlesticks") or [], strict=True):
                    frames.append(parse_candles(candles, ticker, period))
                adjusted = int(payload.get("adjusted_end_ts") or last)
                if adjusted >= last or adjusted <= cursor:
                    break
                cursor = adjusted
        frames = [f for f in frames if not f.empty]
        if not frames:
            return frame_of([], BINARY_CANDLE_COLUMNS)
        frame = pd.concat(frames, ignore_index=True).drop_duplicates(["venue_symbol", "ts"], keep="last")
        return frame.sort_values(["ts", "venue_symbol"], kind="stable").reset_index(drop=True)

    def market_candles(self, ticker: str, period: str, start: datetime, end: datetime, *, series: str,
                       historical: Optional[bool] = None) -> pd.DataFrame:
        """Candles of one market; ``historical`` picks the tier (default: by the cutoff vs ``end``)."""
        require(self, Dataset.BINARY_CANDLES)
        self._declared(series)
        if historical is None:
            historical = pd.Timestamp(end) < self.historical_cutoff()
        path = f"historical/markets/{ticker}/candlesticks" if historical else \
            f"series/{series}/markets/{ticker}/candlesticks"
        payload = self.client.get(path, {"start_ts": _seconds(start), "end_ts": _seconds(end),
                                         "period_interval": _interval(period)})
        return parse_candles(payload.get("candlesticks") or [], ticker, period)

    def binary_trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.BINARY_TRADES)
        cutoff = self.historical_cutoff()
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        records = []
        if start < cutoff:
            records += self._paged("historical/trades", {"ticker": symbol, "min_ts": _seconds(start),
                                                         "max_ts": _seconds(min(end, cutoff))}, "trades")
        if end >= cutoff:
            records += self._paged("markets/trades", {"ticker": symbol, "min_ts": _seconds(max(start, cutoff)),
                                                      "max_ts": _seconds(end)}, "trades")
        frame = parse_trades(records)
        frame = frame[(frame["ts"] >= start) & (frame["ts"] <= end)].drop_duplicates("trade_id")
        return frame.sort_values(["ts", "trade_id"], kind="stable").reset_index(drop=True)

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book in YES probability: bids = YES bids, asks = 1 - NO bids."""
        require(self, Dataset.BOOK)
        payload = self.client.get(f"markets/{symbol}/orderbook")
        return parse_orderbook(payload, symbol, depth)

    def settlements(self, series: str, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> pd.DataFrame:
        """One fixing per settled event of a declared series (``expiration_value``), with its method."""
        require(self, Dataset.SETTLEMENTS)
        contracts = self.binary_contracts(series, status="settled", start=start, end=end)
        return settlements_from_contracts(contracts, self._declared(series)["settlement_index_id"])


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def _interval(period: str) -> int:
    try:
        return PERIODS[period]
    except KeyError:
        raise ValueError(f"Unsupported Kalshi period {period!r}; expected one of {tuple(PERIODS)}") from None


def parse_markets(markets: Iterable[Dict[str, Any]], event: Dict[str, Any],
                  series_info: Optional[Dict[str, Any]] = None) -> pd.DataFrame:
    """Markets of one event -> ``BINARY_SNAPSHOT_COLUMNS``; the expiry is the event's ``strike_date``."""
    series_info = series_info or {}
    expiry = utc_naive(event.get("strike_date"))
    rows = []
    for m in markets:
        ticker = m["ticker"]
        strike_type = m.get("strike_type")
        k_low, k_high = kalshi_bounds(strike_type, m.get("floor_strike"), m.get("cap_strike"))
        method, window = parse_fixing_method(m.get("rules_primary"))
        value = to_float(m.get("expiration_value"))
        result = m.get("result")
        steps = [to_float(r.get("step")) for r in m.get("price_ranges") or []]
        rows.append({
            "venue": VENUE,
            "venue_symbol": ticker,
            "venue_event_id": m.get("event_ticker") or event.get("event_ticker"),
            "venue_series": event.get("series_ticker"),
            "cadence": _cadence(event),
            "strike_type_raw": strike_type,
            "floor_strike_raw": to_float(m.get("floor_strike")),
            "cap_strike_raw": to_float(m.get("cap_strike")),
            "k_low": k_low,
            "k_high": k_high,
            "expiry": expiry,
            "trading_open": utc_naive(m.get("open_time")),
            "trading_close": utc_naive(m.get("close_time")),
            "expected_settlement_at": utc_naive(m.get("expected_expiration_time")),
            "latest_settlement_at": utc_naive(m.get("latest_expiration_time")),
            "fixing_method": method,
            "fixing_window_s": window,
            "rules_text": m.get("rules_primary"),
            "payout": to_float(m.get("notional_value_dollars")),
            "price_tick": min(steps) if steps else float("nan"),
            "fee_type_raw": series_info.get("fee_type"),
            "fee_multiplier_raw": to_float(series_info.get("fee_multiplier")),
            "outcome": _outcome(result, ticker),
            "result_raw": result,
            "settlement_value": value,
            "settled_at": utc_naive(m.get("settlement_ts")),
            "settlement_seen_at": pd.NaT,
            "listed_at": utc_naive(m.get("created_time")),
            "delisted_at": pd.NaT,
            "status": _STATUSES.get(m.get("status")),
            "status_raw": m.get("status"),
            "raw_market_type": m.get("market_type"),
            "raw_can_close_early": m.get("can_close_early"),
            "raw_price_ranges": json.dumps(m.get("price_ranges")),
            "raw_settlement_timer_seconds": to_float(m.get("settlement_timer_seconds")),
            "raw_settlement_value_dollars": to_float(m.get("settlement_value_dollars")),
        })
    return frame_of(rows, BINARY_SNAPSHOT_COLUMNS)


def parse_market_quotes(markets: Iterable[Dict[str, Any]], received_at: pd.Timestamp) -> pd.DataFrame:
    """Market listing -> ``BINARY_QUOTE_COLUMNS``; ``ts`` is the receive time (the listing has no quote
    timestamp; ``updated_time`` is kept raw)."""
    rows = []
    for m in markets:
        bid, ask = _bid(m.get("yes_bid_dollars")), _ask(m.get("yes_ask_dollars"))
        last = to_float(m.get("last_price_dollars"))
        rows.append({
            "ts": pd.Timestamp(received_at),
            "venue": VENUE,
            "venue_symbol": m["ticker"],
            "venue_event_id": m.get("event_ticker"),
            "yes_bid": bid,
            "yes_ask": ask,
            "yes_bid_size": to_float(m.get("yes_bid_size_fp")) if bid == bid else float("nan"),
            "yes_ask_size": to_float(m.get("yes_ask_size_fp")) if ask == ask else float("nan"),
            "p_mid": p_mid(bid, ask),
            "last": last if last > 0 else float("nan"),
            "volume": to_float(m.get("volume_fp")),
            "volume_24h": to_float(m.get("volume_24h_fp")),
            "open_interest": to_float(m.get("open_interest_fp")),
            "no_bid_raw": to_float(m.get("no_bid_dollars")),
            "no_ask_raw": to_float(m.get("no_ask_dollars")),
            "quote_source": "snapshot",
            "raw_yes_bid": to_float(m.get("yes_bid_dollars")),
            "raw_yes_ask": to_float(m.get("yes_ask_dollars")),
            "raw_previous_yes_bid": to_float(m.get("previous_yes_bid_dollars")),
            "raw_previous_yes_ask": to_float(m.get("previous_yes_ask_dollars")),
            "raw_previous_price": to_float(m.get("previous_price_dollars")),
            "raw_updated_time": utc_naive(m.get("updated_time")),
        })
    return frame_of(rows, BINARY_QUOTE_COLUMNS)


def _field(block: Optional[Dict[str, Any]], name: str, suffix: str = "_dollars") -> float:
    """A candle field under its current-tier (``close_dollars``) or historical (``close``) name."""
    block = block or {}
    return to_float(block.get(f"{name}{suffix}", block.get(name)))


def parse_candles(candles: Iterable[Dict[str, Any]], ticker: str, period: str) -> pd.DataFrame:
    """Candles of one market (either tier) -> ``BINARY_CANDLE_COLUMNS``, stamped at the end of the period.
    Bid 0.00 / ask 1.00 (no quote) become NaN."""
    rows = []
    for c in candles:
        bid, ask, price = c.get("yes_bid"), c.get("yes_ask"), c.get("price")
        row = {"ts": utc_naive(c.get("end_period_ts"), "s"), "venue": VENUE, "venue_symbol": ticker,
               "period": period}
        for side, block, clean in (("yes_bid", bid, _bid), ("yes_ask", ask, _ask)):
            for name in ("open", "high", "low", "close"):
                row[f"{side}_{name}"] = clean(_field(block, name))
        for name in ("open", "high", "low", "close", "mean", "previous"):
            row[f"price_{name}"] = _field(price, name)
        row["volume"] = to_float(c.get("volume_fp", c.get("volume")))
        row["open_interest"] = to_float(c.get("open_interest_fp", c.get("open_interest")))
        row["raw_yes_bid_close"] = _field(bid, "close")
        row["raw_yes_ask_close"] = _field(ask, "close")
        rows.append(row)
    return frame_of(rows, BINARY_CANDLE_COLUMNS)


def parse_trades(records: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    rows = [{
        "ts": utc_naive(r.get("created_time")),
        "venue": VENUE,
        "venue_symbol": r.get("ticker"),
        "trade_id": r.get("trade_id"),
        "price": to_float(r.get("yes_price_dollars", r.get("yes_price"))),
        "size": to_float(r.get("count_fp", r.get("count"))),
        "side": r.get("taker_side"),
        "trade_type": "block" if r.get("is_block_trade") else "orderbook",
        "no_price_raw": to_float(r.get("no_price_dollars", r.get("no_price"))),
    } for r in records]
    return frame_of(rows, BINARY_TRADE_COLUMNS)


def parse_orderbook(payload: Dict[str, Any], symbol: str, depth: int = 50) -> dict:
    book = payload.get("orderbook_fp") or {}
    bids = [(float(p), float(s)) for p, s in book.get("yes_dollars") or []]
    asks = [(round(1.0 - float(p), 4), float(s)) for p, s in book.get("no_dollars") or []]
    return make_book(bids, asks, venue=VENUE, symbol=symbol, depth=depth)


def settlements_from_contracts(contracts: pd.DataFrame, index_id: str,
                               seen_at: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """One ``Settlement`` row per settled event: its ``expiration_value`` and its markets' fixing method
    (``mixed`` if they disagree)."""
    seen_at = pd.Timestamp.now(tz="UTC").tz_localize(None) if seen_at is None else pd.Timestamp(seen_at)
    settled = contracts[contracts["settlement_value"].notna()] if not contracts.empty else contracts
    rows = []
    for event, group in settled.groupby("venue_event_id", sort=False):
        values = group["settlement_value"].unique()
        methods = group["fixing_method"].dropna().unique()
        if len(values) != 1:
            logger.warning("Kalshi %s: markets disagree on expiration_value %s", event, sorted(values))
            continue
        rows.append({
            "ts": group["expiry"].iloc[0],
            "venue": VENUE,
            "settlement_index_id": index_id,
            "settlement_price": float(values[0]),
            "method": methods[0] if len(methods) == 1 else "mixed",
            "method_source": settlement_index_info(index_id)["method_source"],
            "source_endpoint": "markets.expiration_value",
            "venue_ref": event,
            "first_seen_at": seen_at,
        })
    frame = frame_of(rows, SETTLEMENT_COLUMNS)
    return frame.sort_values("ts").reset_index(drop=True) if not frame.empty else frame


def recomputed_outcomes(contracts: pd.DataFrame) -> pd.Series:
    """Outcome recomputed from ``settlement_value`` and the canonical bounds (the check against
    ``result``)."""
    return pd.Series([outcome_from_bounds(v, lo, hi) for v, lo, hi in
                      contracts[["settlement_value", "k_low", "k_high"]].itertuples(index=False)],
                     index=contracts.index, dtype="float64")


register_venue(KalshiVenue.name, KalshiVenue)

__all__ = [
    "KalshiVenue",
    "kalshi_bounds",
    "parse_candles",
    "parse_market_quotes",
    "parse_markets",
    "parse_orderbook",
    "parse_trades",
    "recomputed_outcomes",
    "settlements_from_contracts",
]
