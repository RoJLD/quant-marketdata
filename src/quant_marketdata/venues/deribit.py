"""Deribit option chains over the direct public API (``https://www.deribit.com/api/v2/public``), stdlib only.

Decision (spec §3.7.4): a direct adapter, not OpenBB -- its ``deribit`` provider would pull pydantic and
FastAPI into the core, and everything it serves without a key is covered here. OpenBB stays possible on the
Quant-lab side (``[openbb]`` extra) as a one-off cross-check of a chain snapshot. Deribit funding and DVOL
(today in ``quant_lab.data.deribit``) are not ported yet: this adapter is ``OptionsVenue`` only.

API quirks this adapter absorbs (verified 2026-10-18):

- ``get_instruments?currency=BTC&kind=option``: 928 BTC options, all expiring at **08:00 UTC**, with
  ``instrument_type=reversed``: **inverse** contracts, quoted and settled in BTC (ETH likewise), strike in
  USD (``counter_currency``), index ``price_index=btc_usd``, contract size 1. ``currency=USDC`` lists the
  **linear** options (SOL, BTC, ETH, HYPE, XRP, AVAX, TRX), quoted and settled in USDC, names like
  ``AVAX_USDC-19OCT26-8d5-C`` (``d`` = decimal point). ``expired=true`` only lists the **last** expiry.
- ``get_book_summary_by_currency``: one call for every option of a currency: bid/ask/mark/last **in BTC**
  for inverse options, ``mark_iv`` **in percent**, ``underlying_price`` (the expiry's future, synthetic
  when none is listed: ``underlying_index``), ``open_interest`` and ``volume`` in base currency, a missing
  bid served as ``null``. **No greeks, no bid/ask IV, no sizes**: those are per-instrument ``ticker``
  calls, not taken by a chain snapshot (greeks are recomputable exactly from ``mark_iv`` and
  ``underlying_price``: ``ticker`` greeks are **Black-76 on ``underlying_price``** in USD, vega per vol
  point, theta per day, checked on ``BTC-23OCT26-82000-C``).
- Inverse prices are converted to USD with the ``index_price`` of the **same pass**
  (``get_index_price``; ``estimated_delivery_price`` turns into the running delivery TWAP before 08:00 and
  is kept raw only); native prices stay in ``*_raw`` columns.
- ``get_delivery_prices?index_name=btc_usd``: every delivery since **2016-03-18** (2,642 on 2026-10-18),
  newest first, **100 per request** at most (``offset``/``count``). Delivery = TWAP of the index over the
  30 minutes before 08:00 UTC per Deribit's documentation (``venue_doc``, to reconfirm: spec Q6).
- Trades: ``www.deribit.com`` keeps recent trades only; ``history.deribit.com`` serves the archive
  (``get_last_trades_by_currency_and_time``, ``sorting=asc``, up to 1000 per request, ``has_more``) with
  ``iv`` (percent), ``mark_price`` and ``index_price`` at each trade; ``direction`` is the taker side.

Spec Q6 default: chains of the liquid **inverse** BTC and ETH options; linear USDC options are parsed
(``DeribitVenue(linear=True)``) but not snapshotted by default.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.contracts.indexes import settlement_index_info, venue_index_id
from quant_marketdata.venues.contracts.registry import register_name_parser
from quant_marketdata.venues.contracts.schema import (
    OPTION_QUOTE_COLUMNS,
    OPTION_SNAPSHOT_COLUMNS,
    OPTION_TRADE_COLUMNS,
    SETTLEMENT_COLUMNS,
    frame_of,
    to_float,
    utc_naive,
)
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.schema import Dataset

BASE_URL = "https://www.deribit.com/api/v2/public"
HISTORY_URL = "https://history.deribit.com/api/v2/public"
VENUE = "deribit"
INVERSE_CURRENCIES = ("BTC", "ETH")
DELIVERY_PAGE = 100
TRADE_PAGE = 1000
DELIVERY_HOUR = pd.Timedelta(hours=8)

_NAME = re.compile(r"^([A-Z0-9]+)(?:_(USDC|USDT))?-(\d{1,2}[A-Z]{3}\d{2})-([0-9]+(?:d[0-9]+)?)-([CP])$")
_STATES = {"open": "active", "created": "scheduled", "settlement": "expired", "delivered": "expired",
           "closed": "expired", "inactive": "delisted"}


def _ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


def _result(payload: Dict[str, Any]) -> Any:
    if "error" in payload:
        raise ValueError(f"Deribit API error: {payload['error']}")
    return payload.get("result")


def parse_option_name(symbol: str) -> Optional[Dict[str, object]]:
    """Name fields for the registry cross-check: ``BTC-23OCT26-82000-C``, ``AVAX_USDC-19OCT26-8d5-C``."""
    match = _NAME.match(symbol)
    if not match:
        return None
    base, _, date, strike, kind = match.groups()
    try:
        expiry_date = pd.Timestamp(datetime.strptime(date.title(), "%d%b%y"))
    except ValueError:
        return None
    return {"base": base, "expiry_date": expiry_date, "strike": float(strike.replace("d", ".")),
            "option_type": kind}


class DeribitVenue:
    name = VENUE
    capabilities = frozenset({Dataset.OPTION_CONTRACTS, Dataset.OPTION_CHAIN, Dataset.OPTION_TRADES,
                              Dataset.SETTLEMENTS})
    resolutions: tuple = ()

    def __init__(self, client: Optional[JsonClient] = None, *, history_client: Optional[JsonClient] = None,
                 rate: float = 2.0, linear: bool = False) -> None:
        self.client = client or JsonClient(BASE_URL, rate=rate)
        self.history_client = history_client or (client if client is not None else JsonClient(HISTORY_URL, rate=rate))
        self.linear = linear

    def _get(self, path: str, params: Dict[str, Any], *, history: bool = False) -> Any:
        return _result((self.history_client if history else self.client).get(path, params))

    def index_price(self, index_name: str) -> float:
        return float(self._get("get_index_price", {"index_name": index_name})["index_price"])

    # -- options (OptionsVenue) ----------------------------------------------------------------------

    def option_contracts(self, underlying: Optional[str] = None, *, expired: bool = False) -> pd.DataFrame:
        require(self, Dataset.OPTION_CONTRACTS)
        records: List[Dict[str, Any]] = []
        for currency in [underlying] if underlying else list(INVERSE_CURRENCIES):
            if currency in INVERSE_CURRENCIES:
                records += self._get("get_instruments", {"currency": currency, "kind": "option",
                                                         "expired": expired}) or []
        if self.linear:
            linear = self._get("get_instruments", {"currency": "USDC", "kind": "option", "expired": expired})
            records += [r for r in linear or [] if underlying is None or r.get("base_currency") == underlying]
        return parse_instruments(records)

    def option_chain(self, underlying: str, expiry: Optional[datetime] = None) -> pd.DataFrame:
        require(self, Dataset.OPTION_CHAIN)
        frames = []
        if underlying in INVERSE_CURRENCIES:
            index = self.index_price(f"{underlying.lower()}_usd")
            summary = self._get("get_book_summary_by_currency", {"currency": underlying, "kind": "option"})
            frames.append(parse_book_summary(summary or [], index))
        if self.linear:
            summary = self._get("get_book_summary_by_currency", {"currency": "USDC", "kind": "option"})
            rows = [r for r in summary or [] if r.get("base_currency") == underlying]
            if rows:
                frames.append(parse_book_summary(rows, self.index_price(f"{underlying.lower()}_usdc")))
        frames = [f for f in frames if not f.empty]
        chain = pd.concat(frames, ignore_index=True) if frames else frame_of([], OPTION_QUOTE_COLUMNS)
        if expiry is not None and not chain.empty:
            contracts = self.option_contracts(underlying)
            keep = contracts.loc[contracts["expiry"] == pd.Timestamp(expiry), "venue_symbol"]
            chain = chain[chain["venue_symbol"].isin(keep)].reset_index(drop=True)
        return chain

    def option_trades(self, underlying: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Archive trades of a currency's options (``history.deribit.com``) in ``[start, end]``."""
        require(self, Dataset.OPTION_TRADES)
        frames, cursor, last = [], _ms(start), _ms(end)
        while cursor <= last:
            result = self._get("get_last_trades_by_currency_and_time", {
                "currency": underlying, "kind": "option", "start_timestamp": cursor, "end_timestamp": last,
                "count": TRADE_PAGE, "sorting": "asc",
            }, history=True)
            trades = result.get("trades") or []
            frames.append(parse_trades(trades, inverse=underlying in INVERSE_CURRENCIES))
            if not result.get("has_more") or not trades:
                break
            newest = max(int(t["timestamp"]) for t in trades)
            # Resume at the newest timestamp (trades sharing it are de-duplicated by id); a full page of
            # one millisecond would not advance, so step past it then.
            cursor = newest if newest > cursor else cursor + 1
        frames = [f for f in frames if not f.empty]
        if not frames:
            return frame_of([], OPTION_TRADE_COLUMNS)
        frame = pd.concat(frames, ignore_index=True).drop_duplicates("trade_id", keep="last")
        return frame.sort_values(["ts", "trade_id"], kind="stable").reset_index(drop=True)

    def option_mark_candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                            *, price: str = "mark") -> pd.DataFrame:
        require(self, Dataset.OPTION_MARK_CANDLES)  # get_tradingview_chart_data serves trades only
        raise AssertionError("unreachable")

    def settlements(self, index: str, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> pd.DataFrame:
        """Deliveries of an index (``btc_usd``), newest pages first until ``start`` is passed."""
        require(self, Dataset.SETTLEMENTS)
        records, offset = [], 0
        while True:
            result = self._get("get_delivery_prices", {"index_name": index, "offset": offset,
                                                       "count": DELIVERY_PAGE})
            page = result.get("data") or []
            records += page
            offset += len(page)
            total = int(result.get("records_total") or 0)
            oldest = pd.Timestamp(page[-1]["date"]) if page else None
            if not page or offset >= total or (start is not None and oldest < pd.Timestamp(start).normalize()):
                break
        frame = parse_delivery_prices(records, index)
        if start is not None:
            frame = frame[frame["ts"] >= pd.Timestamp(start)]
        if end is not None:
            frame = frame[frame["ts"] <= pd.Timestamp(end)]
        return frame.reset_index(drop=True)


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_instruments(records: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in records:
        if r.get("kind") != "option":
            continue
        inverse = r.get("instrument_type") == "reversed"
        kind = {"call": "C", "put": "P"}.get(r.get("option_type"))
        state = r.get("state")
        rows.append({
            "venue": VENUE,
            "venue_symbol": r["instrument_name"],
            "venue_market_id": str(r.get("instrument_id")) if r.get("instrument_id") is not None else None,
            "venue_base": r.get("base_currency"),
            "venue_quote": r.get("counter_currency"),
            "settle_asset": r.get("settlement_currency"),
            "expiry": utc_naive(r.get("expiration_timestamp"), "ms"),
            "option_type": kind,
            "strike": to_float(r.get("strike")),
            "style": "european",
            "exercise": "cash",
            "contract_size": to_float(r.get("contract_size")),
            "price_unit": "base" if inverse else "quote",
            "settlement_index_raw": r.get("price_index"),
            "tick_size": to_float(r.get("tick_size")),
            "min_size": to_float(r.get("min_trade_amount")),
            "maker_fee_raw": to_float(r.get("maker_commission")),
            "taker_fee_raw": to_float(r.get("taker_commission")),
            "fee_extra_raw": json.dumps({"block_trade_commission": r.get("block_trade_commission"),
                                         "tick_size_steps": r.get("tick_size_steps")}),
            "listed_at": utc_naive(r.get("creation_timestamp"), "ms"),
            "delisted_at": pd.NaT,
            "status": _STATES.get(state, "delisted" if not r.get("is_active") else "active"),
            "status_raw": state,
            "settlement_price": float("nan"),
            "settlement_source": None,
            "settlement_seen_at": pd.NaT,
            "raw_instrument_type": r.get("instrument_type"),
            "raw_settlement_period": r.get("settlement_period"),
        })
    return frame_of(rows, OPTION_SNAPSHOT_COLUMNS)


def parse_book_summary(records: Iterable[Dict[str, Any]], index_price: float) -> pd.DataFrame:
    """``get_book_summary_by_currency`` (options) -> ``OPTION_QUOTE_COLUMNS``; inverse prices x the pass's
    ``index_price``, IV / 100. No greeks, sizes nor bid/ask IV in this endpoint."""
    rows = []
    for r in records:
        inverse = r.get("quote_currency") == r.get("base_currency")
        scale = index_price if inverse else 1.0
        bid, ask, mark, last = (to_float(r.get(k)) for k in ("bid_price", "ask_price", "mark_price", "last"))
        iv = to_float(r.get("mark_iv"))
        underlying = to_float(r.get("underlying_price"))
        rows.append({
            "ts": utc_naive(r.get("creation_timestamp"), "ms"),
            "venue": VENUE,
            "venue_symbol": r["instrument_name"],
            "bid": bid * scale, "ask": ask * scale, "mark": mark * scale, "last": last * scale,
            "bid_size": float("nan"), "ask_size": float("nan"),
            "iv_bid": float("nan"), "iv_ask": float("nan"), "iv_mark": iv / 100, "iv_source": "venue",
            "delta": float("nan"), "gamma": float("nan"), "vega": float("nan"), "theta": float("nan"),
            "rho": float("nan"), "vanna": float("nan"), "volga": float("nan"),
            "greeks_reference": "forward", "greeks_source": "none",
            "underlying_price": underlying, "underlying_kind": "future",
            "index_price": float(index_price), "forward_price": underlying, "forward_rate": float("nan"),
            "discount_factor": float("nan"),
            "open_interest": to_float(r.get("open_interest")),
            "volume_24h": to_float(r.get("volume")),
            "bid_raw": bid, "ask_raw": ask, "mark_raw": mark, "last_raw": last,
            "iv_bid_raw": float("nan"), "iv_ask_raw": float("nan"), "iv_mark_raw": iv,
            "raw_underlying_index": r.get("underlying_index"),
            "raw_estimated_delivery_price": to_float(r.get("estimated_delivery_price")),
            "raw_interest_rate": to_float(r.get("interest_rate")),
            "raw_volume_usd": to_float(r.get("volume_usd")),
            "raw_mid_price": to_float(r.get("mid_price")),
            "raw_price_change": to_float(r.get("price_change")),
            "raw_high": to_float(r.get("high")),
            "raw_low": to_float(r.get("low")),
            "raw_price_currency": r.get("quote_currency"),
        })
    return frame_of(rows, OPTION_QUOTE_COLUMNS)


def parse_trades(records: Iterable[Dict[str, Any]], *, inverse: bool) -> pd.DataFrame:
    rows = []
    for r in records:
        index = to_float(r.get("index_price"))
        scale = index if inverse else 1.0
        price, mark, iv = to_float(r.get("price")), to_float(r.get("mark_price")), to_float(r.get("iv"))
        trade_type = "block" if r.get("block_trade_id") else "liquidation" if r.get("liquidation") else "orderbook"
        rows.append({
            "ts": utc_naive(r.get("timestamp"), "ms"),
            "venue": VENUE,
            "venue_symbol": r.get("instrument_name"),
            "trade_id": str(r.get("trade_id")),
            "price": price * scale,
            "size": to_float(r.get("amount")),
            "side": r.get("direction"),
            "trade_type": trade_type,
            "mark_at_trade": mark * scale,
            "index_at_trade": index,
            "iv_at_trade": iv / 100,
            "price_raw": price,
            "mark_at_trade_raw": mark,
            "iv_at_trade_raw": iv,
        })
    return frame_of(rows, OPTION_TRADE_COLUMNS)


def parse_delivery_prices(records: Iterable[Dict[str, Any]], index: str,
                          seen_at: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    seen_at = pd.Timestamp.now(tz="UTC").tz_localize(None) if seen_at is None else pd.Timestamp(seen_at)
    index_id = venue_index_id(VENUE, index)
    info = settlement_index_info(index_id)
    rows = [{
        "ts": pd.Timestamp(r["date"]) + DELIVERY_HOUR,
        "venue": VENUE,
        "settlement_index_id": index_id,
        "settlement_price": to_float(r.get("delivery_price")),
        "method": info["method"],
        "method_source": info["method_source"],
        "source_endpoint": "get_delivery_prices",
        "venue_ref": r["date"],
        "first_seen_at": seen_at,
    } for r in records]
    frame = frame_of(rows, SETTLEMENT_COLUMNS)
    return frame.drop_duplicates("ts", keep="first").sort_values("ts").reset_index(drop=True)


register_venue(DeribitVenue.name, DeribitVenue)
register_name_parser(VENUE, parse_option_name)

__all__ = [
    "DeribitVenue",
    "parse_book_summary",
    "parse_delivery_prices",
    "parse_instruments",
    "parse_option_name",
    "parse_trades",
]
