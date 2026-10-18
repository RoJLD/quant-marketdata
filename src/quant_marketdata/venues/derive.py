"""Derive (ex-Lyra, options and perps DEX) adapter over the public API v3, without its SDK: perps on the
``Venue`` protocol, options on ``OptionsVenue``.

API quirks this adapter absorbs (verified 2026-10-18):

- Every method is a POST of JSON to ``https://api.derive.xyz/v3/public/<method>``; errors come back as
  ``{"error": {"code", "message", "data"}}``. The legacy host ``api.lyra.finance`` answers 503. In v3,
  ``get_option_settlement_history``, ``get_spot_feed_history(_candles)``, ``get_index_price`` and
  ``get_instruments`` answer 404: there is no option or index candle endpoint.
- ``get_all_live_instruments`` **ignores its filters** (it returns the ~3,900 names of every type and
  currency, names only): instruments come from ``get_all_instruments`` instead, paged (``page``,
  ``page_size`` up to 1000, ``pagination.num_pages``), which lists scheduled contracts too
  (``is_active=false`` with a future ``scheduled_activation``). With ``expired=true`` it only reaches back
  **~11 days**, every expired option carrying ``option_details.settlement_price``.
- Option names are ``<CUR>-<YYYYMMDD>-<STRIKE>-<C|P>`` with ``_`` as the decimal point (``ADA-...-1_7-C``);
  the adapter reads ``option_details`` (``index``, ``expiry`` in s, ``strike``, ``option_type``) and only
  cross-checks the name. Expiries are at 08:00 UTC. Options are European and cash-settled in USDC.
- ``get_tickers`` requires ``expiry_date`` for options: a chain is one request per currency and expiry.
  Tickers use **abbreviated keys**, whose meaning was **deduced from the values** (``DERIVE_TICKER_KEYS``,
  tested on a recorded payload): ``b``/``B`` best bid price/amount, ``a``/``A`` ask, ``M`` mark, ``I``
  index, ``f`` funding (perps; null on options), ``minp``/``maxp`` price bands; ``option_pricing``: ``d``
  delta, ``g`` gamma, ``v`` vega, ``t`` theta, ``r`` rho, ``i`` mark IV, ``bi``/``ai`` bid/ask IV, ``f``
  forward, ``m`` model mark, ``df`` discount factor; ``stats``: ``c`` contracts traded (24h), ``v`` USD
  notional (= ``c`` x index), ``pr`` USD premium volume, ``n`` trades, ``oi`` open interest, ``h``/``l``,
  ``p`` change. A missing side is served as ``"0"`` (price, amount and IV): kept as NaN.
- Greeks are **undiscounted Black-76 on the forward ``f``** (the mark includes ``df``), ``vega`` per vol
  point and ``theta`` per calendar day: recomputed to 1e-4 on the recorded chain (BTC 23 OCT 82000 C and
  79000 P), ``greeks_reference = "forward"``.
- ``get_option_settlement_prices`` serves the settlement price of the **last 12 expiries only**: the daily
  task must keep them. The settlement method of the ``BTC-USD`` index is not documented (spec Q4):
  ``derive:BTC-USD`` has method ``unknown``.
- ``get_trade_history`` (``currency``, ``instrument_type``, ``from_timestamp``/``to_timestamp`` in ms,
  ``page``/``page_size`` up to 1000, newest first) serves option trades back to at least 2024-01 with
  ``mark_price`` and ``index_price`` at the trade. Every trade appears twice, once per side
  (``liquidity_role`` maker and taker): the taker row is kept, its ``direction`` is the taker side.
  ``wallet``, ``subaccount_id``, ``tx_hash``, PnL and fee columns are dropped at parsing (no trader
  identifier is stored). RFQ trades carry ``rfq_id``/``quote_id`` (``trade_type = "rfq"``).
- Perps: ``<CUR>-PERP``, USDC-margined, ``perp_details.index``. ``get_funding_rate_history``
  (``instrument_name``, ``start_timestamp``/``end_timestamp`` in ms, ``period=3600``) serves hourly
  candles of the funding rate stamped at the **start** of the hour (the newest one still forming) and
  only reaches back **~12 days**: incremental downloads must run at least weekly. ``funding_rate`` is the
  candle's close, a **signed fraction per hour**; funding accrues continuously, and the hourly event
  (stamped at the end of its hour) takes the close as the hour's rate, an approximation of the time
  average the venue does not serve (BTC 2026-10-06..18: 86 % of the hours had open = close). Sign:
  positive = longs pay; consistent with 2026-10-18's tickers, where the rate sat at the 0.00125 %/h base on
  14 perps and fell below it only on BTC, whose mark was below the index.
- Fees are served per instrument (``maker_fee_rate`` 0.0001, ``taker_fee_rate`` 0.0003, ``base_fee``,
  ``mark_price_fee_rate_cap`` 0.125); the composition rule is ``unconfirmed`` (``contracts.fees``).
"""

from __future__ import annotations

import json
import math
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
from quant_marketdata.venues.schema import (
    FUNDING_COLUMNS,
    INSTRUMENT_COLUMNS,
    Dataset,
    empty_frame,
    finalize_funding,
)

BASE_URL = "https://api.derive.xyz/v3/public"
VENUE = "derive"
PAGE_SIZE = 1000
FUNDING_INTERVAL_H = 1
TRADE_WINDOW = pd.Timedelta(days=1)
HOUR = pd.Timedelta(hours=1)

# Abbreviated ticker keys (deduced from the values, see the module docstring) -> meaning.
DERIVE_TICKER_KEYS: Dict[str, str] = {
    "t": "timestamp_ms", "b": "best_bid_price", "B": "best_bid_amount", "a": "best_ask_price",
    "A": "best_ask_amount", "M": "mark_price", "I": "index_price", "f": "funding_rate",
    "minp": "min_price", "maxp": "max_price",
    "option_pricing.d": "delta", "option_pricing.g": "gamma", "option_pricing.v": "vega",
    "option_pricing.t": "theta", "option_pricing.r": "rho", "option_pricing.i": "iv",
    "option_pricing.bi": "bid_iv", "option_pricing.ai": "ask_iv", "option_pricing.f": "forward_price",
    "option_pricing.m": "model_mark_price", "option_pricing.df": "discount_factor",
    "stats.c": "contracts_traded_24h", "stats.v": "notional_volume_usd_24h",
    "stats.pr": "premium_volume_usd_24h", "stats.n": "trade_count_24h", "stats.oi": "open_interest",
    "stats.h": "high_24h", "stats.l": "low_24h", "stats.p": "price_change_24h",
}

_NAME = re.compile(r"^([A-Z0-9]+)-(\d{8})-([0-9_]+)-([CP])$")


def _ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


def _check(payload: Dict[str, Any]) -> Any:
    if "error" in payload:
        error = payload["error"] or {}
        raise ValueError(f"Derive API error {error.get('code')}: {error.get('message')} {error.get('data') or ''}")
    return payload.get("result")


def parse_option_name(symbol: str) -> Optional[Dict[str, object]]:
    """Name fields for the registry cross-check: ``BTC-20261023-82000-C``, ``ADA-20261225-1_7-C``."""
    match = _NAME.match(symbol)
    if not match:
        return None
    base, date, strike, kind = match.groups()
    return {"base": base, "expiry_date": pd.Timestamp(date), "strike": float(strike.replace("_", ".")),
            "option_type": kind}


class DeriveVenue:
    name = VENUE
    capabilities = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.SPECS, Dataset.OPTION_CONTRACTS,
                              Dataset.OPTION_CHAIN, Dataset.OPTION_TRADES, Dataset.SETTLEMENTS})
    resolutions: tuple = ()  # no candle endpoint in v3

    def __init__(self, client: Optional[JsonClient] = None, *, rate: float = 2.0) -> None:
        self.client = client or JsonClient(BASE_URL, rate=rate)

    def _call(self, method: str, payload: Optional[Dict[str, Any]] = None) -> Any:
        return _check(self.client.post(method, payload or {}))

    def _instruments(self, instrument_type: str, currency: Optional[str] = None,
                     expired: bool = False) -> List[Dict[str, Any]]:
        records, page = [], 1
        while True:
            payload = {"instrument_type": instrument_type, "expired": expired, "page": page,
                       "page_size": PAGE_SIZE}
            if currency is not None:
                payload["currency"] = currency
            result = self._call("get_all_instruments", payload)
            records += result.get("instruments") or []
            pages = int((result.get("pagination") or {}).get("num_pages") or 1)
            if page >= pages:
                return records
            page += 1

    def option_currencies(self) -> List[str]:
        """Currencies with option markets (``get_all_currencies``)."""
        return sorted(c["currency"] for c in self._call("get_all_currencies") or [] if c.get("option"))

    # -- perps (Venue) -------------------------------------------------------------------------------

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        return parse_perp_instruments(self._instruments("perp"))

    def specs(self) -> pd.DataFrame:
        require(self, Dataset.SPECS)
        rows = [{k: json.dumps(v) if isinstance(v, (dict, list)) else v for k, v in record.items()}
                for record in self._instruments("perp")]
        return pd.DataFrame(rows).rename(columns={"instrument_name": "symbol"})

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.FUNDING)
        # Candles are stamped at the start of their hour; the event closes the hour.
        result = self._call("get_funding_rate_history", {
            "instrument_name": symbol, "start_timestamp": _ms(pd.Timestamp(start) - HOUR),
            "end_timestamp": _ms(end), "period": 3600,
        })
        now = pd.Timestamp.now(tz="UTC").tz_localize(None)
        raw = parse_funding(result.get("funding_rate_history") or [], complete_before=now)
        if raw.empty:
            return empty_frame(FUNDING_COLUMNS)
        out = finalize_funding(raw, FUNDING_INTERVAL_H)
        out = out.join(raw[[c for c in raw.columns if c.startswith("raw_")]])
        return out.loc[pd.Timestamp(start):pd.Timestamp(end)]

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        require(self, Dataset.CANDLES)  # no candle endpoint in v3
        raise AssertionError("unreachable")

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        require(self, Dataset.DAILY_METRICS)
        raise AssertionError("unreachable")

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        require(self, Dataset.BOOK)  # public book endpoint not probed yet
        raise AssertionError("unreachable")

    # -- options (OptionsVenue) ----------------------------------------------------------------------

    def option_contracts(self, underlying: Optional[str] = None, *, expired: bool = False) -> pd.DataFrame:
        require(self, Dataset.OPTION_CONTRACTS)
        currencies = [underlying] if underlying else self.option_currencies()
        records = [r for c in currencies for r in self._instruments("option", c, expired)]
        return parse_option_instruments(records)

    def option_chain(self, underlying: str, expiry: Optional[datetime] = None) -> pd.DataFrame:
        require(self, Dataset.OPTION_CHAIN)
        contracts = self.option_contracts(underlying)
        live = contracts[contracts["status"] == "active"]
        expiries = sorted(live["expiry"].dropna().unique())
        if expiry is not None:
            expiries = [e for e in expiries if pd.Timestamp(e) == pd.Timestamp(expiry)]
        frames = []
        for stamp in expiries:
            result = self._call("get_tickers", {"instrument_type": "option", "currency": underlying,
                                                "expiry_date": pd.Timestamp(stamp).strftime("%Y%m%d")})
            frames.append(parse_option_tickers(result.get("tickers") or {}))
        frames = [f for f in frames if not f.empty]
        if not frames:
            return frame_of([], OPTION_QUOTE_COLUMNS)
        return pd.concat(frames, ignore_index=True)

    def option_trades(self, underlying: str, start: datetime, end: datetime) -> pd.DataFrame:
        require(self, Dataset.OPTION_TRADES)
        frames, cursor, end = [], pd.Timestamp(start), pd.Timestamp(end)
        while cursor < end:
            window_end = min(cursor + TRADE_WINDOW, end)
            page = 1
            while True:
                result = self._call("get_trade_history", {
                    "currency": underlying, "instrument_type": "option", "from_timestamp": _ms(cursor),
                    "to_timestamp": _ms(window_end), "page": page, "page_size": PAGE_SIZE,
                })
                frames.append(parse_trades(result.get("trades") or []))
                pages = int((result.get("pagination") or {}).get("num_pages") or 1)
                if page >= pages:
                    break
                page += 1
            cursor = window_end
        frames = [f for f in frames if not f.empty]
        if not frames:
            return frame_of([], OPTION_TRADE_COLUMNS)
        frame = pd.concat(frames, ignore_index=True).drop_duplicates("trade_id", keep="last")
        frame = frame[(frame["ts"] >= pd.Timestamp(start)) & (frame["ts"] <= end)]
        return frame.sort_values(["ts", "trade_id"], kind="stable").reset_index(drop=True)

    def option_mark_candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                            *, price: str = "mark") -> pd.DataFrame:
        require(self, Dataset.OPTION_MARK_CANDLES)  # not served in v3
        raise AssertionError("unreachable")

    def settlements(self, index: str, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> pd.DataFrame:
        """Fixings of an option index (``BTC-USD`` as in ``option_details.index``, or the currency):
        the last 12 expiries only."""
        require(self, Dataset.SETTLEMENTS)
        currency = index.split("-")[0]
        result = self._call("get_option_settlement_prices", {"currency": currency})
        frame = parse_settlement_prices(result.get("expiries") or [], f"{currency}-USD")
        if start is not None:
            frame = frame[frame["ts"] >= pd.Timestamp(start)]
        if end is not None:
            frame = frame[frame["ts"] <= pd.Timestamp(end)]
        return frame.reset_index(drop=True)


# -- parsers (pure, unit-tested on recorded payloads) ----------------------------------------------


def parse_perp_instruments(records: Iterable[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for r in records:
        if r.get("instrument_type") != "perp":
            continue
        rows.append({
            "venue": VENUE,
            "venue_symbol": r["instrument_name"],
            "venue_market_id": r.get("base_asset_address"),
            "settle_asset": r.get("quote_currency"),
            "chain": None,
            "contract_address": r.get("base_asset_address"),
            "funding_interval_h": float(FUNDING_INTERVAL_H),
            "listed_at": utc_naive(r.get("scheduled_activation"), "s"),
            "delisted_at": pd.NaT,
            "status": "active" if r.get("is_active") else "inactive",
            "session": None,
        })
    columns = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"]
    return pd.DataFrame(rows).reindex(columns=columns)


def parse_funding(records: List[Dict[str, Any]], complete_before: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """Hourly funding-rate candles -> one event per **complete** hour, stamped at its end; ``rate`` is the
    candle's ``funding_rate`` (its close), a signed fraction per hour."""
    if not records:
        return pd.DataFrame()
    frame = pd.DataFrame(records)
    opened = pd.to_datetime(frame["timestamp"].astype("int64"), unit="ms")
    index = pd.DatetimeIndex(opened + HOUR, name="ts")
    out = pd.DataFrame({
        "rate_raw": frame["funding_rate"].astype(str).to_numpy(),
        "direction_raw": None,
        "rate": frame["funding_rate"].astype(float).to_numpy(),
        "raw_open": frame["open"].astype(float).to_numpy(),
        "raw_high": frame["high"].astype(float).to_numpy(),
        "raw_low": frame["low"].astype(float).to_numpy(),
    }, index=index)
    if complete_before is not None:
        out = out[out.index <= pd.Timestamp(complete_before)]
    return out[~out.index.duplicated(keep="last")].sort_index()


def _status(record: Dict[str, Any], expiry: pd.Timestamp, now: pd.Timestamp) -> str:
    if pd.notna(expiry) and expiry <= now:
        return "expired"
    if record.get("is_active"):
        return "active"
    activation = utc_naive(record.get("scheduled_activation"), "s")
    return "scheduled" if pd.notna(activation) and activation > now else "delisted"


def parse_option_instruments(records: Iterable[Dict[str, Any]],
                             now: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    """``get_all_instruments`` option records -> ``OPTION_SNAPSHOT_COLUMNS`` (structured fields only)."""
    now = pd.Timestamp.now(tz="UTC").tz_localize(None) if now is None else pd.Timestamp(now)
    rows = []
    for r in records:
        details = r.get("option_details") or {}
        if r.get("instrument_type") != "option" or not details:
            continue
        expiry = utc_naive(details.get("expiry"), "s")
        settlement = to_float(details.get("settlement_price"))
        rows.append({
            "venue": VENUE,
            "venue_symbol": r["instrument_name"],
            "venue_market_id": r.get("base_asset_sub_id"),
            "venue_base": r.get("base_currency"),
            "venue_quote": r.get("quote_currency"),
            "settle_asset": r.get("quote_currency"),
            "expiry": expiry,
            "option_type": details.get("option_type"),
            "strike": to_float(details.get("strike")),
            "style": "european",
            "exercise": "cash",
            "contract_size": 1.0,
            "price_unit": "quote",
            "settlement_index_raw": details.get("index"),
            "tick_size": to_float(r.get("tick_size")),
            "min_size": to_float(r.get("minimum_amount")),
            "maker_fee_raw": to_float(r.get("maker_fee_rate")),
            "taker_fee_raw": to_float(r.get("taker_fee_rate")),
            "fee_extra_raw": json.dumps({"base_fee": r.get("base_fee"),
                                         "mark_price_fee_rate_cap": r.get("mark_price_fee_rate_cap")}),
            "listed_at": utc_naive(r.get("scheduled_activation"), "s"),
            "delisted_at": pd.NaT,
            "status": _status(r, expiry, now),
            "status_raw": f"is_active={str(bool(r.get('is_active'))).lower()}",
            "settlement_price": settlement,
            "settlement_source": "get_all_instruments" if not math.isnan(settlement) else None,
            "settlement_seen_at": pd.NaT,
            "raw_scheduled_deactivation": utc_naive(r.get("scheduled_deactivation"), "s"),
            "raw_maximum_amount": to_float(r.get("maximum_amount")),
            "raw_amount_step": to_float(r.get("amount_step")),
        })
    return frame_of(rows, OPTION_SNAPSHOT_COLUMNS)


def _side(price, size, iv=None):
    """A side served as ``"0"`` is missing: NaN price, size and IV."""
    p, s = to_float(price), to_float(size)
    if not (p > 0 and s > 0):
        return float("nan"), float("nan"), float("nan")
    v = to_float(iv)
    return p, s, v if v > 0 else float("nan")


def parse_option_tickers(tickers: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    """``get_tickers`` (options) -> ``OPTION_QUOTE_COLUMNS``; ``ts_snapshot`` and ``contract_id`` are left
    to the snapshot pass. The abbreviated keys are read through ``DERIVE_TICKER_KEYS``."""
    rows = []
    for symbol, t in tickers.items():
        pricing = t.get("option_pricing") or {}
        stats = t.get("stats") or {}
        bid, bid_size, iv_bid = _side(t.get("b"), t.get("B"), pricing.get("bi"))
        ask, ask_size, iv_ask = _side(t.get("a"), t.get("A"), pricing.get("ai"))
        iv_mark = to_float(pricing.get("i"))
        forward = to_float(pricing.get("f"))
        rows.append({
            "ts": utc_naive(t.get("t"), "ms"),
            "venue": VENUE,
            "venue_symbol": symbol,
            "bid": bid, "ask": ask, "mark": to_float(t.get("M")), "last": float("nan"),
            "bid_size": bid_size, "ask_size": ask_size,
            "iv_bid": iv_bid, "iv_ask": iv_ask, "iv_mark": iv_mark if iv_mark > 0 else float("nan"),
            "iv_source": "venue",
            "delta": to_float(pricing.get("d")), "gamma": to_float(pricing.get("g")),
            "vega": to_float(pricing.get("v")), "theta": to_float(pricing.get("t")),
            "rho": to_float(pricing.get("r")), "vanna": float("nan"), "volga": float("nan"),
            "greeks_reference": "forward", "greeks_source": "venue" if pricing else "none",
            "underlying_price": forward, "underlying_kind": "forward",
            "index_price": to_float(t.get("I")), "forward_price": forward, "forward_rate": float("nan"),
            "discount_factor": to_float(pricing.get("df")),
            "open_interest": to_float(stats.get("oi")),
            "volume_24h": to_float(stats.get("c")),
            "bid_raw": to_float(t.get("b")), "ask_raw": to_float(t.get("a")), "mark_raw": to_float(t.get("M")),
            "last_raw": float("nan"),
            "iv_bid_raw": to_float(pricing.get("bi")), "iv_ask_raw": to_float(pricing.get("ai")),
            "iv_mark_raw": iv_mark,
            "raw_min_price": to_float(t.get("minp")), "raw_max_price": to_float(t.get("maxp")),
            "raw_model_mark_price": to_float(pricing.get("m")),
            "raw_notional_volume_usd_24h": to_float(stats.get("v")),
            "raw_premium_volume_usd_24h": to_float(stats.get("pr")),
            "raw_trade_count_24h": to_float(stats.get("n")),
            "raw_high_24h": to_float(stats.get("h")), "raw_low_24h": to_float(stats.get("l")),
            "raw_price_change_24h": to_float(stats.get("p")),
        })
    return frame_of(rows, OPTION_QUOTE_COLUMNS)


def parse_trades(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """Taker rows of ``get_trade_history`` -> ``OPTION_TRADE_COLUMNS``; trader identifiers never kept."""
    rows = []
    for r in records:
        if r.get("liquidity_role") != "taker":
            continue
        price = to_float(r.get("trade_price"))
        mark = to_float(r.get("mark_price"))
        rows.append({
            "ts": utc_naive(r.get("timestamp"), "ms"),
            "venue": VENUE,
            "venue_symbol": r.get("instrument_name"),
            "trade_id": r.get("trade_id"),
            "price": price,
            "size": to_float(r.get("trade_amount")),
            "side": r.get("direction"),
            "trade_type": "rfq" if r.get("rfq_id") else "orderbook",
            "mark_at_trade": mark,
            "index_at_trade": to_float(r.get("index_price")),
            "iv_at_trade": float("nan"),
            "price_raw": price,
            "mark_at_trade_raw": mark,
            "iv_at_trade_raw": float("nan"),
        })
    return frame_of(rows, OPTION_TRADE_COLUMNS)


def parse_settlement_prices(expiries: List[Dict[str, Any]], index_raw: str,
                            seen_at: Optional[pd.Timestamp] = None) -> pd.DataFrame:
    seen_at = pd.Timestamp.now(tz="UTC").tz_localize(None) if seen_at is None else pd.Timestamp(seen_at)
    index_id = venue_index_id(VENUE, index_raw)
    info = settlement_index_info(index_id)
    rows = [{
        "ts": utc_naive(e.get("utc_expiry_sec"), "s"),
        "venue": VENUE,
        "settlement_index_id": index_id,
        "settlement_price": to_float(e.get("price")),
        "method": info["method"],
        "method_source": info["method_source"],
        "source_endpoint": "get_option_settlement_prices",
        "venue_ref": e.get("expiry_date"),
        "first_seen_at": seen_at,
    } for e in expiries]
    return frame_of(rows, SETTLEMENT_COLUMNS).sort_values("ts").reset_index(drop=True)


register_venue(DeriveVenue.name, DeriveVenue)
register_name_parser(VENUE, parse_option_name)

__all__ = [
    "DERIVE_TICKER_KEYS",
    "DeriveVenue",
    "parse_funding",
    "parse_option_instruments",
    "parse_option_name",
    "parse_option_tickers",
    "parse_perp_instruments",
    "parse_settlement_prices",
    "parse_trades",
]
