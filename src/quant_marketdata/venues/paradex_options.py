"""Paradex dated options: pure parsers of the public payloads (contracts, chain snapshot, settlements,
trades). ``ParadexVenue`` (``paradex.py``) does the HTTP; this module only turns payloads into frames.

Conventions (docs ``trading/dated-options/*`` and probes of 2026-10-18; every recalculation is exact):

- **Contracts.** ``markets`` lists ``asset_kind=OPTION`` (10,666 on 2026-10-18, 14 underlyings) with
  structured ``base_currency``, ``strike_price``, ``option_type`` (``CALL``/``PUT``), ``expiry_at`` and
  ``open_at`` (ms). European, cash-settled in USDC, quoted in USD per unit of the underlying (contract size
  1). An expired contract disappears from ``markets`` and ``markets/summary``.
- **Names** ``<BASE>-USD-<D><MON><YY>-<STRIKE>-<C|P>``: the day is **not** zero-padded (``6NOV26``) and a
  decimal strike is written with ``_`` (``META-USD-20OCT26-712_5-P`` = 712.5). The name carries no hour:
  ``expiry_at`` is authoritative. Crypto expiries are at 08:00 UTC; the others follow their reference
  market and move with US daylight saving (XAU 17:30, CL 18:30/19:30, META/MU/MSTR/SPCX and US500/US100
  20:00/21:00, some US500/US100 at 13:30/14:30). The name is only cross-checked against the fields; a
  disagreement leaves the contract unresolved (``name_fields_mismatch``).
- **Settlement.** Payoff on the **30-minute TWAP of the Paradex Spot Oracle Price** ending at expiry
  (the index of the perp of the same base). ``markets/settlement-price?market=<symbol>`` serves it after
  expiry, by name (checked back to 26JUN26): ``settlement_price`` is the option's payoff,
  ``underlying_twap_price`` the TWAP; expiries before 2026-05-21 answer ``MARKET_NOT_SETTLED`` (dated
  options started in March 2026). ``markets/history`` lists ``SETTLED`` events of the last days (one page
  of at most 5,000 events, ~2.5 days on 2026-10-18). Expired names are found in the trade history: every
  open position settles through a ``SETTLE_MARKET`` trade at the payoff; an expiry with no open position
  leaves no trade (BTC 2026-10-04), so its fixing is only reachable through a contract the registry saw. The canonical ``settlement_price`` of a contract
  and of a ``Settlement`` row is the TWAP (the index value at the fixing); the payoff is kept as
  ``settlement_payoff_raw`` and equals ``max(0, +-(TWAP - strike))``.
- **Chain** (``markets/summary?market=ALL``, one ~7 MB call): ``bid``/``ask``/``mark_price``/
  ``last_traded_price`` in USD, sizes and ``open_interest`` in units of the underlying, IVs as annualized
  fractions (``bid_iv``/``ask_iv`` absent on a one-sided book), ``greeks`` (delta, gamma, vega, theta, rho,
  vanna, volga), ``underlying_price`` (the spot oracle), ``forward_rate`` per expiry, ``risk_free_rate``
  (0), ``created_at`` (ms). ``volume_24h`` is the USD notional of the underlying traded (25 BTC at spot);
  ``volume_24h`` here is that notional over the current spot, an approximation (raw kept).
- **Pricing and greeks: Black-76 on ``F = S exp(f T)``**, ``S`` = ``underlying_price``, ``f`` =
  ``forward_rate``, ``T = (expiry_at - created_at) / 365 days``, discount ``exp(-r T)`` with ``r`` =
  ``risk_free_rate``. Greeks are **with respect to spot**: delta and gamma per unit of ``S``, vega per vol
  point (0.01), theta per calendar day at constant spot (it includes the forward's carry), rho per 1 % of
  ``r``. Recomputed on all 10,666 options: mark within 1e-6 relative (median 5e-9), delta, vega, theta and
  rho within 1e-4 (median ~1e-8); a 365.25-day year is off by ~5e-4. Mark IV is fitted to Deribit (BTC,
  ETH) or Derive (HYPE); ``f`` is calibrated on Deribit.
- **Fees** (``fee_config``): ``interactive_fee`` (UI orders) 0, ``api_fee`` and ``rpi_fee`` maker and
  taker 0.0001, each with ``fee_cap`` 0.125 and ``fee_floor`` -0.125. The docs cap fees at 12.5 % of the
  premium but do not say what the rate applies to; a cap at 12.5 % of the premium only binds if the rate
  applies to the underlying's notional (size x spot): ``FEE_MODEL_STATUS`` is ``unconfirmed``.
- **Trades** (``trades?base_asset=<BASE>``, every active option of a base, cursor-paged): ``side`` is the
  taker's, ``trade_type`` in {FILL, BLOCK_TRADE, LIQUIDATION, RPI, UNWIND_TRANSFER, SETTLE_MARKET}; no IV
  or mark at the trade. Most option volume is block trades; option books are mostly empty.
- **Candles**: ``markets/klines`` with ``price_kind`` ``mark`` or ``underlying`` from listing to expiry
  (1-60 min, as for the perps; ``paradex.parse_klines``).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import pandas as pd

from quant_marketdata.venues.http import HttpError
from quant_marketdata.venues.schema import UnsupportedDataset

VENUE = "paradex"
TRADE_PAGE = 1000  # the API's maximum page_size for trades
YEAR_DAYS = 365.0
SETTLEMENT_METHOD = "twap_30min"
SETTLEMENT_METHOD_SOURCE = "venue_doc"
FEE_MODEL = ("taker/maker rate x underlying notional (size x spot), capped at 12.5 % of the premium "
             "(floor -12.5 %); UI (interactive) orders 0")
FEE_MODEL_STATUS = "unconfirmed"
STYLE, EXERCISE = "european", "cash"
OPTION_TYPES = {"CALL": "C", "PUT": "P"}
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
_NAME = re.compile(r"^(?P<base>[A-Za-z0-9]+)-(?P<quote>[A-Z]+)-(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})"
                   r"-(?P<strike>\d+(?:_\d+)?)-(?P<type>[CP])$")
_DAY = pd.Timedelta(days=1)


def _number(value: Any) -> float:
    """Paradex numbers are strings; an empty string means absent."""
    if value is None or value == "":
        return float("nan")
    return float(value)


def _ms(value: Any) -> pd.Timestamp:
    return pd.Timestamp(int(value), unit="ms") if value not in (None, "", 0) else pd.NaT


def parse_option_name(symbol: str) -> Optional[Dict[str, Any]]:
    """``BTC-USD-6NOV26-712_5-P`` -> base, quote, expiry date (no hour in the name), strike, type."""
    match = _NAME.match(symbol)
    if not match or match["mon"] not in MONTHS:
        return None
    try:
        date = pd.Timestamp(2000 + int(match["yy"]), MONTHS[match["mon"]], int(match["day"]))
    except ValueError:
        return None
    return {"base": match["base"], "quote": match["quote"], "expiry_date": date,
            "strike": float(match["strike"].replace("_", ".")), "option_type": match["type"]}


def format_strike(strike: float) -> str:
    """Minimal decimal writing: 82000.0 -> ``82000``, 712.5 -> ``712.5``, 5.9 -> ``5.9``."""
    text = format(Decimal(repr(float(strike))).normalize(), "f")
    return text


def contract_id(underlying_id: str, expiry: pd.Timestamp, strike: float, option_type: str) -> str:
    """``BTC-USD-20261023T0800-82000-C`` (spec 2026-10-18 section 3.2)."""
    return f"{underlying_id}-{pd.Timestamp(expiry):%Y%m%dT%H%M}-{format_strike(strike)}-{option_type}"


def settlement_index_id(base: str) -> str:
    return f"{VENUE}:{base}"


def _fees(market: Mapping[str, Any]) -> Tuple[float, float]:
    api = (market.get("fee_config") or {}).get("api_fee") or {}
    return (_number((api.get("maker_fee") or {}).get("fee")), _number((api.get("taker_fee") or {}).get("fee")))


def _status(listed: pd.Timestamp, expiry: pd.Timestamp, as_of: pd.Timestamp) -> str:
    if pd.notna(listed) and listed > as_of:
        return "scheduled"
    if pd.notna(expiry) and expiry <= as_of:
        return "expired"
    return "active"


def parse_option_markets(markets: Iterable[Mapping[str, Any]],
                         as_of: pd.Timestamp) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """``markets`` payload -> (option contracts, unresolved). Perps and spot are ignored.

    Attributes come from the structured fields; the name is only cross-checked. ``asset_class`` and
    ``underlying_ref`` are left to the registry (overrides of the perp of the same base)."""
    as_of = pd.Timestamp(as_of)
    rows, unresolved = [], []
    for m in markets:
        if m.get("asset_kind") != "OPTION":
            continue
        symbol = m["symbol"]
        option_type = OPTION_TYPES.get(str(m.get("option_type")))
        base, quote = m.get("base_currency"), m.get("quote_currency")
        expiry, strike = _ms(m.get("expiry_at")), _number(m.get("strike_price"))
        name = parse_option_name(symbol)
        problem = None
        if option_type is None:
            problem = f"unknown option_type {m.get('option_type')!r}"
        elif name is None:
            problem = "name_fields_mismatch: unparsed name"
        elif (name["base"], name["quote"], name["option_type"]) != (base, quote, option_type) \
                or name["strike"] != strike or pd.isna(expiry) or name["expiry_date"] != expiry.normalize():
            problem = "name_fields_mismatch"
        if problem:
            unresolved.append({"venue": VENUE, "venue_symbol": symbol, "reason": problem})
            continue
        underlying = f"{base}-{'USD' if quote in ('USD', 'USDC', 'USDT') else quote}"
        listed = _ms(m.get("open_at"))
        maker, taker = _fees(m)
        rows.append({
            "venue": VENUE,
            "venue_symbol": symbol,
            "venue_market_id": symbol,
            "contract_id": contract_id(underlying, expiry, strike, option_type),
            "underlying_id": underlying,
            "base": base,
            "quote": underlying.split("-", 1)[1],
            "settle_asset": m.get("settlement_currency"),
            "asset_class": None,
            "underlying_ref": None,
            "expiry": expiry,
            "option_type": option_type,
            "strike": strike,
            "style": STYLE,
            "exercise": EXERCISE,
            "contract_size": 1.0,
            "price_unit": "quote",
            "settlement_index_raw": None,  # not served: the base's spot oracle, per the docs
            "settlement_index_id": settlement_index_id(base),
            "tick_size": _number(m.get("price_tick_size")),
            "min_size": _number(m.get("order_size_increment")),
            "min_notional": _number(m.get("min_notional")),
            "maker_fee_raw": maker,
            "taker_fee_raw": taker,
            "fee_extra_raw": json.dumps(m.get("fee_config") or {}, sort_keys=True),
            "listed_at": listed,
            "delisted_at": pd.NaT,
            "status": _status(listed, expiry, as_of),
            "settlement_price": float("nan"),
            "settlement_source": None,
            "settlement_seen_at": pd.NaT,
            "first_seen_at": as_of,
            "mapping_source": "rule",
            "expiry_raw": int(m["expiry_at"]),
            "option_type_raw": m.get("option_type"),
            "strike_raw": m.get("strike_price"),
        })
    return pd.DataFrame(rows), pd.DataFrame(unresolved, columns=["venue", "venue_symbol", "reason"])


QUOTE_GREEKS = ("delta", "gamma", "vega", "theta", "rho", "vanna", "volga")


def parse_option_summary(rows: Iterable[Mapping[str, Any]], contracts: pd.DataFrame,
                         ts_snapshot: pd.Timestamp) -> pd.DataFrame:
    """``markets/summary`` rows -> one chain row per known option contract (others ignored).

    ``contracts`` is the frame of ``parse_option_markets`` (``venue_symbol``, ``contract_id``,
    ``expiry``)."""
    by_symbol = contracts.set_index("venue_symbol")[["contract_id", "expiry"]].to_dict("index") \
        if not contracts.empty else {}
    out = []
    for r in rows:
        contract = by_symbol.get(r.get("symbol"))
        if contract is None:
            continue
        ts = _ms(r.get("created_at"))
        spot, rate, riskless = (_number(r.get("underlying_price")), _number(r.get("forward_rate")),
                                _number(r.get("risk_free_rate")))
        tau = (contract["expiry"] - ts) / pd.Timedelta(days=YEAR_DAYS) if pd.notna(ts) else float("nan")
        greeks = r.get("greeks") or {}
        volume_usd = _number(r.get("volume_24h"))
        row = {
            "ts_snapshot": pd.Timestamp(ts_snapshot),
            "ts": ts,
            "venue": VENUE,
            "venue_symbol": r["symbol"],
            "contract_id": contract["contract_id"],
            "bid": _number(r.get("bid")),
            "ask": _number(r.get("ask")),
            "mark": _number(r.get("mark_price")),
            "last": _number(r.get("last_traded_price")),
            "bid_size": _number(r.get("bid_size")),
            "ask_size": _number(r.get("ask_size")),
            "iv_bid": _number(r.get("bid_iv")),
            "iv_ask": _number(r.get("ask_iv")),
            "iv_mark": _number(r.get("mark_iv")),
            "iv_source": "venue" if r.get("mark_iv") not in (None, "") else None,
            **{name: _number(greeks.get(name)) for name in QUOTE_GREEKS},
            "greeks_reference": "spot",
            "greeks_source": "venue" if greeks else "none",
            "underlying_price": spot,
            "underlying_kind": "spot",
            "index_price": spot,
            "forward_price": spot * math.exp(rate * tau) if tau == tau else float("nan"),
            "forward_rate": rate,
            "discount_factor": math.exp(-riskless * tau) if tau == tau and riskless == riskless else float("nan"),
            "open_interest": _number(r.get("open_interest")),
            "volume_24h": volume_usd / spot if spot else float("nan"),
            "volume_24h_raw": volume_usd,
            "raw_total_volume": _number(r.get("total_volume")),
            "raw_last_iv": _number(r.get("last_iv")),
            "raw_price_change_rate_24h": _number(r.get("price_change_rate_24h")),
            "raw_future_funding_rate": _number(r.get("future_funding_rate")),
            "raw_risk_free_rate": riskless,
        }
        out.append(row)
    return pd.DataFrame(out)


def payoff(option_type: str, strike: float, settlement: float) -> float:
    return max(0.0, settlement - strike) if option_type == "C" else max(0.0, strike - settlement)


def parse_settlement_prices(payloads: Iterable[Mapping[str, Any]],
                            seen_at: pd.Timestamp) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """``markets/settlement-price`` responses -> (Settlement rows, one per index and fixing; contract
    settlements, one per option). Error payloads (``MARKET_NOT_FOUND``, ``MARKET_NOT_SETTLED``) are
    skipped."""
    contracts = []
    for p in payloads:
        name = parse_option_name(str(p.get("symbol", "")))
        if name is None or "underlying_twap_price" not in p:
            continue
        contracts.append({
            "venue": VENUE,
            "venue_symbol": p["symbol"],
            "settlement_index_id": settlement_index_id(name["base"]),
            "expiry": _ms(p.get("expiry_at")),
            "settlement_price": _number(p.get("underlying_twap_price")),
            "settlement_payoff_raw": _number(p.get("settlement_price")),
            "settlement_source": "markets/settlement-price",
            "settlement_seen_at": pd.Timestamp(seen_at),
        })
    contract_frame = pd.DataFrame(contracts)
    return _settlement_rows(contract_frame, "markets/settlement-price"), contract_frame


def parse_market_history(events: Iterable[Mapping[str, Any]], seen_at: pd.Timestamp) -> pd.DataFrame:
    """``SETTLED`` events of ``markets/history`` -> contract settlements. The events carry no expiry
    instant (``updated_at`` is a few seconds after it): the caller joins the registry's ``expiry``."""
    rows = []
    for e in events:
        if e.get("event_type") != "SETTLED":
            continue
        name = parse_option_name(str(e.get("symbol", "")))
        fields = e.get("changed_fields") or {}
        if name is None or "underlying_twap_price" not in fields:
            continue
        rows.append({
            "venue": VENUE,
            "venue_symbol": e["symbol"],
            "settlement_index_id": settlement_index_id(name["base"]),
            "settled_at": _ms(e.get("updated_at")),
            "settlement_price": _number(fields.get("underlying_twap_price")),
            "settlement_payoff_raw": _number(fields.get("settlement_price")),
            "settlement_source": "markets/history",
            "settlement_seen_at": pd.Timestamp(seen_at),
        })
    return pd.DataFrame(rows)


def _settlement_rows(contracts: pd.DataFrame, endpoint: str) -> pd.DataFrame:
    if contracts.empty:
        return pd.DataFrame()
    rows = (contracts.dropna(subset=["expiry"])
            .drop_duplicates(["settlement_index_id", "expiry"], keep="last")
            .sort_values(["settlement_index_id", "expiry"]))
    return pd.DataFrame({
        "ts": rows["expiry"].to_numpy(),
        "venue": VENUE,
        "settlement_index_id": rows["settlement_index_id"].to_numpy(),
        "settlement_price": rows["settlement_price"].to_numpy(),
        "method": SETTLEMENT_METHOD,
        "method_source": SETTLEMENT_METHOD_SOURCE,
        "source_endpoint": endpoint,
        "first_seen_at": rows["settlement_seen_at"].to_numpy(),
    })


def parse_option_trades(records: Iterable[Mapping[str, Any]],
                        contract_ids: Optional[Mapping[str, str]] = None) -> pd.DataFrame:
    """``trades`` records -> option trades: ``side`` of the taker (``buy``/``sell``), ``trade_type`` as
    served, ``contract_id`` when the symbol is known."""
    contract_ids = contract_ids or {}
    rows: List[Dict[str, Any]] = []
    for t in records:
        rows.append({
            "ts": _ms(t.get("created_at")),
            "venue_symbol": t.get("market"),
            "contract_id": contract_ids.get(t.get("market")),
            "price": _number(t.get("price")),
            "size": _number(t.get("size")),
            "side": str(t.get("side", "")).lower() or None,
            "trade_type": t.get("trade_type"),
            "mark_at_trade": float("nan"),
            "index_at_trade": float("nan"),
            "iv_at_trade": float("nan"),
            "trade_id": t.get("id"),
            "block_id": t.get("block_id") or None,
        })
    frame = pd.DataFrame(rows)
    return frame.sort_values("ts").reset_index(drop=True) if not frame.empty else frame


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def _base(underlying: Optional[str]) -> Optional[str]:
    """``BTC`` or ``BTC-USD`` -> ``BTC``; ``paradex:BTC`` -> ``BTC``."""
    if underlying is None:
        return None
    return underlying.split(":")[-1].split("-")[0]


class ParadexOptions:
    """The dated-options half of ``ParadexVenue``: HTTP on top of the parsers above. Needs ``client``."""

    client: Any
    _option_contracts: Optional[pd.DataFrame] = None
    option_unresolved: pd.DataFrame = pd.DataFrame(columns=["venue", "venue_symbol", "reason"])

    def option_contracts(self, underlying: Optional[str] = None, *, expired: bool = False) -> pd.DataFrame:
        """Live option contracts (``markets``, ~16 MB), optionally of one underlying; the unresolved ones
        are left in ``option_unresolved``. Paradex drops a contract from ``markets`` at expiry."""
        if expired:
            raise UnsupportedDataset("paradex drops expired options from /markets: expired contracts come "
                                     "from the contract registry, settlements from settlements()")
        contracts, self.option_unresolved = parse_option_markets(
            self.client.get("markets").get("results") or [], _now())
        self._option_contracts = contracts
        base = _base(underlying)
        return contracts if base is None or contracts.empty else contracts[contracts["base"] == base]

    def _known_contracts(self, symbols: Iterable[str]) -> pd.DataFrame:
        """Cached contracts, refreshed once when a symbol is unknown (new strikes list every 8 hours)."""
        cached = self._option_contracts
        if cached is None or not set(symbols) <= set(cached["venue_symbol"]) | set(self.option_unresolved["venue_symbol"]):
            self.option_contracts()
        return self._option_contracts

    def option_chain(self, underlying: str, expiry: Optional[datetime] = None, *,
                     ts_snapshot: Optional[pd.Timestamp] = None) -> pd.DataFrame:
        """Live chain of one underlying from ``markets/summary?market=ALL`` (one ~7 MB call)."""
        ts_snapshot = _now() if ts_snapshot is None else pd.Timestamp(ts_snapshot)
        base = _base(underlying)
        rows = [r for r in self.client.get("markets/summary", {"market": "ALL"}).get("results") or []
                if str(r.get("symbol", "")).startswith(f"{base}-") and parse_option_name(r["symbol"])]
        contracts = self._known_contracts(r["symbol"] for r in rows)
        chain = parse_option_summary(rows, contracts, ts_snapshot)
        if expiry is not None and not chain.empty:
            wanted = contracts.loc[contracts["expiry"] == pd.Timestamp(expiry), "venue_symbol"]
            chain = chain[chain["venue_symbol"].isin(wanted)].reset_index(drop=True)
        return chain

    def option_trades(self, underlying: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Trades of every option of one base in ``[start, end]`` (``trades?base_asset=``, newest first,
        cursor-paged), expired contracts included; ``contract_id`` is set for the live ones only."""
        records, cursor = [], None
        params = {"base_asset": _base(underlying), "start_at": int(pd.Timestamp(start).value // 10**6),
                  "end_at": int(pd.Timestamp(end).value // 10**6), "page_size": TRADE_PAGE}
        while True:
            payload = self.client.get("trades", {**params, "cursor": cursor} if cursor else params)
            records.extend(payload.get("results") or [])
            cursor = payload.get("next")
            if not cursor or not payload.get("results"):
                break
        records = [r for r in records if parse_option_name(str(r.get("market", "")))]
        contracts = self._option_contracts if self._option_contracts is not None else pd.DataFrame()
        ids = dict(zip(contracts["venue_symbol"], contracts["contract_id"], strict=True)) if not contracts.empty else {}
        return parse_option_trades(records, ids)

    def option_mark_candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                            *, price: str = "mark") -> pd.DataFrame:
        """``mark`` or ``underlying`` candles of one live contract (served from listing to expiry)."""
        if price not in ("mark", "underlying"):
            raise ValueError(f"price must be 'mark' or 'underlying', got {price!r}")
        return self._klines(symbol, resolution, start, end, price)

    def settlement_prices(self, symbols: Iterable[str]) -> Tuple[pd.DataFrame, pd.DataFrame]:
        """(Settlement rows, contract settlements) of expired contracts, by name. Served for expiries from
        2026-05-21 on; older ones answer ``MARKET_NOT_SETTLED``."""
        payloads = []
        for symbol in symbols:
            try:
                payloads.append(self.client.get("markets/settlement-price", {"market": symbol}))
            except HttpError as exc:  # 404 MARKET_NOT_SETTLED / MARKET_NOT_FOUND: skipped by the parser
                payloads.append({"symbol": symbol, "error": exc.body[:200]})
        return parse_settlement_prices(payloads, _now())

    def settlements(self, index: str, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> pd.DataFrame:
        """Fixings of one index (``paradex:BTC`` or ``BTC``): the expiries in ``[start, end]`` are found
        in the trade history (``SETTLE_MARKET`` trades name every settled contract) or, without
        ``start``, in the ``SETTLED`` events of ``markets/history``; one ``settlement-price`` request per
        expiry gives its exact instant and TWAP."""
        base = _base(index)
        end = _now() if end is None else pd.Timestamp(end)
        if start is None:
            events = self.client.get("markets/history", {"market": "ALL"}).get("results") or []
            names = [e["symbol"] for e in events if e.get("event_type") == "SETTLED"
                     and str(e.get("symbol", "")).startswith(f"{base}-")]
        else:
            names = self.option_trades(base, start, end)["venue_symbol"].tolist()
        by_date: Dict[pd.Timestamp, str] = {}
        for name in sorted(set(names)):
            parsed = parse_option_name(name)
            if parsed and parsed["base"] == base and parsed["expiry_date"] <= end:
                by_date.setdefault(parsed["expiry_date"], name)
        rows, _ = self.settlement_prices(by_date.values())
        if rows.empty:
            return rows
        keep = rows["ts"] <= end if start is None else rows["ts"].between(pd.Timestamp(start), end)
        return rows[keep].reset_index(drop=True)


__all__ = [
    "FEE_MODEL",
    "FEE_MODEL_STATUS",
    "ParadexOptions",
    "SETTLEMENT_METHOD",
    "YEAR_DAYS",
    "contract_id",
    "format_strike",
    "parse_market_history",
    "parse_option_markets",
    "parse_option_name",
    "parse_option_summary",
    "parse_option_trades",
    "parse_settlement_prices",
    "payoff",
    "settlement_index_id",
]
