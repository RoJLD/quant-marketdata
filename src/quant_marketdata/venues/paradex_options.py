"""Paradex dated options, in the canonical contract schemas (``venues.contracts``): parsers of the public
payloads and ``ParadexOptions``, the options half of ``ParadexVenue`` (``paradex.py``).

Conventions (docs ``trading/dated-options/*`` and probes of 2026-10-18; every recalculation is exact):

- **Contracts.** ``markets`` lists ``asset_kind=OPTION`` (10,666 on 2026-10-18, 14 underlyings) with
  structured ``base_currency``, ``strike_price``, ``option_type`` (``CALL``/``PUT``), ``expiry_at`` and
  ``open_at`` (ms). European, cash-settled in USDC, quoted in USD per unit of the underlying (contract size
  1). An expired contract disappears from ``markets`` and ``markets/summary``; the listing is ~16 MB.
- **Names** ``<BASE>-USD-<D><MON><YY>-<STRIKE>-<C|P>``: the day is **not** zero-padded (``6NOV26``) and a
  decimal strike is written with ``_`` (``META-USD-20OCT26-712_5-P`` = 712.5). The name carries no hour:
  ``expiry_at`` is authoritative. Crypto expiries are at 08:00 UTC; the others follow their reference
  market and move with US daylight saving (XAU 17:30, CL 18:30/19:30, META/MU/MSTR/SPCX and US500/US100
  20:00/21:00, some US500/US100 at 13:30/14:30). ``parse_option_name`` is registered for the registry's
  name/fields cross-check only.
- **Underlyings.** BTC, ETH, SOL, HYPE, NEAR and ZEC resolve by the registry's crypto rule; the TradFi
  bases (XAU, CL, SPCX, META, MU, MSTR, US500, US100) by ``contracts.overrides.OPTION_UNDERLYINGS``. The
  settlement index of a contract is its base's **Paradex Spot Oracle Price** (``settlement_index_raw`` = the
  base, id ``paradex:<BASE>`` in ``SETTLEMENT_INDEXES``), the index of the perp of that base.
- **Settlement.** Payoff on the **30-minute TWAP of the spot oracle** ending at expiry (docs
  ``dated-options/overview``). ``markets/settlement-price?market=<symbol>`` serves it after expiry, by name:
  ``settlement_price`` is the option's **payoff**, ``underlying_twap_price`` the TWAP; expiries before
  2026-05-21 answer ``MARKET_NOT_SETTLED`` (dated options started in March 2026). ``markets/history`` lists
  ``SETTLED`` events of the last days (one page of at most 5,000 events, ~2.5 days on 2026-10-18).
  Expired names are found in the trade history: every open position settles through a ``SETTLE_MARKET``
  trade at the payoff; an expiry with no open position leaves no trade (BTC 2026-10-04), so its fixing is
  only reachable through a contract the registry saw. The canonical ``settlement_price`` (contract and
  ``Settlement`` row) is the TWAP, the index value at the fixing; the payoff is kept as
  ``settlement_payoff_raw`` and equals ``max(0, +-(TWAP - strike))``.
- **Chain** (``markets/summary?market=ALL``, one ~7 MB call): ``bid``/``ask``/``mark_price``/
  ``last_traded_price`` in USD (``""`` when absent), sizes and ``open_interest`` in units of the
  underlying, IVs as annualized fractions (``bid_iv``/``ask_iv`` absent on a one-sided book), ``greeks``
  (delta, gamma, vega, theta, rho, vanna, volga), ``underlying_price`` (the spot oracle), ``forward_rate``
  per expiry, ``risk_free_rate`` (0), ``created_at`` (ms). ``volume_24h`` is the USD notional of the
  underlying traded (25 BTC at spot): the canonical ``volume_24h`` is that notional over the current spot,
  an approximation (``volume_24h_raw`` keeps the USD figure).
- **Pricing and greeks: Black-76 on ``F = S exp(f T)``**, ``S`` = ``underlying_price``, ``f`` =
  ``forward_rate``, ``T = (expiry_at - created_at) / 365 days``, discount ``exp(-r T)`` with ``r`` =
  ``risk_free_rate``. Greeks are **with respect to spot** (``greeks_reference = spot``): delta and gamma per
  unit of ``S``, vega per vol point (0.01), theta per calendar day at constant spot (it includes the
  forward's carry), rho per 1 % of ``r``. Recomputed on all 10,666 options: mark within 1e-6 relative
  (median 5e-9), delta, vega, theta and rho within 1e-4 (median ~1e-8); a 365.25-day year is off by ~5e-4.
  Mark IV is fitted to Deribit (BTC, ETH) or Derive (HYPE); ``f`` is calibrated on Deribit.
- **Fees** (``fee_config``): ``interactive_fee`` (UI orders) 0, ``api_fee`` and ``rpi_fee`` maker and
  taker 0.0001, each with ``fee_cap`` 0.125 and ``fee_floor`` -0.125. The docs cap fees at 12.5 % of the
  premium but do not say what the rate applies to; a cap at 12.5 % of the premium only binds if the rate
  applies to the underlying's notional (size x spot): ``FEE_MODEL`` is ``unconfirmed``.
- **Trades** (``trades?base_asset=<BASE>``, every option of a base, expired ones included, newest first,
  cursor-paged, at most 1,000 a page): ``side`` is the taker's, ``trade_type`` in {FILL, BLOCK_TRADE,
  LIQUIDATION, RPI, UNWIND_TRANSFER, SETTLE_MARKET}; no IV or mark at the trade. Most option volume is block
  trades; option books are mostly empty. Names without an expiry (2025 perpetual options,
  ``BTC-USD-113000-C``) are dropped.
- **Candles**: ``markets/klines`` with ``price_kind`` ``mark`` or ``underlying`` from listing to expiry
  (1-60 min, as for the perps; the volume column is the premium traded, dropped).
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pandas as pd

from quant_marketdata.venues.base import require
from quant_marketdata.venues.contracts.fees import FEE_MODELS
from quant_marketdata.venues.contracts.indexes import settlement_index_info, venue_index_id
from quant_marketdata.venues.contracts.registry import (
    register_name_parser,
    resolve_option_contracts,
)
from quant_marketdata.venues.contracts.schema import (
    OPTION_CONTRACT_COLUMNS,
    OPTION_MARK_CANDLE_COLUMNS,
    OPTION_QUOTE_COLUMNS,
    OPTION_SNAPSHOT_COLUMNS,
    OPTION_TRADE_COLUMNS,
    SETTLEMENT_COLUMNS,
    frame_of,
    to_float,
    utc_naive,
)
from quant_marketdata.venues.http import HttpError
from quant_marketdata.venues.schema import Dataset

VENUE = "paradex"
YEAR_DAYS = 365.0
TRADE_PAGE = 1000  # the API's maximum page_size for trades
OPTION_DATASETS = frozenset({Dataset.OPTION_CONTRACTS, Dataset.OPTION_CHAIN, Dataset.OPTION_TRADES,
                             Dataset.OPTION_MARK_CANDLES, Dataset.SETTLEMENTS})
FEE_MODEL = {
    "fee_model": "min(rate x size x spot, 0.125 x premium), rebates floored at -0.125 x premium; "
                 "api_fee (0.0001) for API orders, interactive_fee (0) for UI orders",
    "fee_model_status": "unconfirmed",
    "note": "fee_config served per market; the docs (trading/trading-fees) document the 12.5 % premium cap "
            "but not the base of the rate (size x spot inferred: the cap would never bind on the premium)",
}
STYLE, EXERCISE = "european", "cash"
OPTION_TYPES = {"CALL": "C", "PUT": "P"}
MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}
_NAME = re.compile(r"^(?P<base>[A-Za-z0-9]+)-(?P<quote>[A-Z]+)-(?P<day>\d{1,2})(?P<mon>[A-Z]{3})(?P<yy>\d{2})"
                   r"-(?P<strike>\d+(?:_\d+)?)-(?P<type>[CP])$")


def parse_option_name(symbol: str) -> Optional[Dict[str, Any]]:
    """``META-USD-6NOV26-712_5-P`` -> base, quote, expiry date (no hour in the name), strike, type."""
    match = _NAME.match(symbol)
    if not match or match["mon"] not in MONTHS:
        return None
    try:
        date = pd.Timestamp(2000 + int(match["yy"]), MONTHS[match["mon"]], int(match["day"]))
    except ValueError:
        return None
    return {"base": match["base"], "quote": match["quote"], "expiry_date": date,
            "strike": float(match["strike"].replace("_", ".")), "option_type": match["type"]}


# Registered into the shared tables, which stay the single place a decision is read from.
register_name_parser(VENUE, parse_option_name)
FEE_MODELS.setdefault(VENUE, FEE_MODEL)


def _status(listed: pd.Timestamp, expiry: pd.Timestamp, as_of: pd.Timestamp) -> str:
    if pd.notna(listed) and listed > as_of:
        return "scheduled"
    if pd.notna(expiry) and expiry <= as_of:
        return "expired"
    return "active"


def parse_option_markets(markets: Iterable[Mapping[str, Any]], as_of: pd.Timestamp) -> pd.DataFrame:
    """``markets`` payload -> option listing in ``OPTION_SNAPSHOT_COLUMNS`` (perps and spot ignored). The
    identity columns are filled by ``contracts.resolve_option_contracts``."""
    as_of = pd.Timestamp(as_of)
    rows = []
    for m in markets:
        if m.get("asset_kind") != "OPTION":
            continue
        api = (m.get("fee_config") or {}).get("api_fee") or {}
        expiry, listed = utc_naive(m.get("expiry_at"), "ms"), utc_naive(m.get("open_at"), "ms")
        rows.append({
            "venue": VENUE,
            "venue_symbol": m["symbol"],
            "venue_market_id": m["symbol"],
            "settle_asset": m.get("settlement_currency"),
            "venue_base": m.get("base_currency"),
            "venue_quote": m.get("quote_currency"),
            "expiry": expiry,
            "option_type": OPTION_TYPES.get(str(m.get("option_type"))),
            "strike": to_float(m.get("strike_price")),
            "style": STYLE,
            "exercise": EXERCISE,
            "contract_size": 1.0,
            "price_unit": "quote",
            "settlement_index_raw": m.get("base_currency"),  # the base's spot oracle (not served by name)
            "tick_size": to_float(m.get("price_tick_size")),
            "min_size": to_float(m.get("order_size_increment")),
            "maker_fee_raw": to_float((api.get("maker_fee") or {}).get("fee")),
            "taker_fee_raw": to_float((api.get("taker_fee") or {}).get("fee")),
            "fee_extra_raw": json.dumps(m.get("fee_config") or {}, sort_keys=True),
            "listed_at": listed,
            "delisted_at": pd.NaT,
            "status": _status(listed, expiry, as_of),
            "status_raw": None,  # Paradex serves no status
            "settlement_price": float("nan"),
            "settlement_source": None,
            "settlement_seen_at": pd.NaT,
            "expiry_raw": m.get("expiry_at"),
            "option_type_raw": m.get("option_type"),
            "strike_raw": m.get("strike_price"),
            "raw_min_notional": to_float(m.get("min_notional")),
        })
    return frame_of(rows, OPTION_SNAPSHOT_COLUMNS)


GREEKS = ("delta", "gamma", "vega", "theta", "rho", "vanna", "volga")


def parse_option_summary(rows: Iterable[Mapping[str, Any]], contracts: pd.DataFrame,
                         ts_snapshot: pd.Timestamp) -> pd.DataFrame:
    """``markets/summary`` rows -> chain rows (``OPTION_QUOTE_COLUMNS``) of the contracts of ``contracts``
    (a listing: ``venue_symbol``, ``expiry`` and, once resolved, ``contract_id``); other rows are ignored."""
    if contracts.empty:
        return frame_of([], OPTION_QUOTE_COLUMNS)
    known = contracts.set_index("venue_symbol")
    ids = known["contract_id"] if "contract_id" in known.columns else pd.Series(dtype="object")
    out = []
    for r in rows:
        symbol = r.get("symbol")
        if symbol not in known.index:
            continue
        expiry = known.at[symbol, "expiry"]
        ts = utc_naive(r.get("created_at"), "ms")
        spot, rate, riskless = (to_float(r.get("underlying_price")), to_float(r.get("forward_rate")),
                                to_float(r.get("risk_free_rate")))
        tau = (expiry - ts) / pd.Timedelta(days=YEAR_DAYS) if pd.notna(ts) and pd.notna(expiry) else math.nan
        greeks = r.get("greeks") or {}
        volume_usd = to_float(r.get("volume_24h"))
        contract_id = ids.get(symbol) if not ids.empty else None
        out.append({
            "ts_snapshot": pd.Timestamp(ts_snapshot),
            "ts": ts,
            "venue": VENUE,
            "venue_symbol": symbol,
            "contract_id": contract_id if isinstance(contract_id, str) else None,
            "bid": to_float(r.get("bid")),
            "ask": to_float(r.get("ask")),
            "mark": to_float(r.get("mark_price")),
            "last": to_float(r.get("last_traded_price")),
            "bid_size": to_float(r.get("bid_size")),
            "ask_size": to_float(r.get("ask_size")),
            "iv_bid": to_float(r.get("bid_iv")),
            "iv_ask": to_float(r.get("ask_iv")),
            "iv_mark": to_float(r.get("mark_iv")),
            "iv_source": "venue" if r.get("mark_iv") not in (None, "") else None,
            **{name: to_float(greeks.get(name)) for name in GREEKS},
            "greeks_reference": "spot",
            "greeks_source": "venue" if greeks else "none",
            "underlying_price": spot,
            "underlying_kind": "spot",
            "index_price": spot,  # the spot oracle is the settlement index
            "forward_price": spot * math.exp(rate * tau) if not math.isnan(tau) else math.nan,
            "forward_rate": rate,
            "discount_factor": math.exp(-riskless * tau) if not (math.isnan(tau) or math.isnan(riskless))
            else math.nan,
            "open_interest": to_float(r.get("open_interest")),
            "volume_24h": volume_usd / spot if spot else math.nan,
            "bid_raw": r.get("bid"),
            "ask_raw": r.get("ask"),
            "mark_raw": r.get("mark_price"),
            "last_raw": r.get("last_traded_price"),
            "iv_bid_raw": r.get("bid_iv"),
            "iv_ask_raw": r.get("ask_iv"),
            "iv_mark_raw": r.get("mark_iv"),
            "volume_24h_raw": volume_usd,
            "raw_total_volume": to_float(r.get("total_volume")),
            "raw_last_iv": to_float(r.get("last_iv")),
            "raw_price_change_rate_24h": to_float(r.get("price_change_rate_24h")),
            "raw_future_funding_rate": to_float(r.get("future_funding_rate")),
            "raw_risk_free_rate": riskless,
        })
    return frame_of(out, OPTION_QUOTE_COLUMNS)


def payoff(option_type: str, strike: float, settlement: float) -> float:
    return max(0.0, settlement - strike) if option_type == "C" else max(0.0, strike - settlement)


def _venue_ref(name: Mapping[str, Any]) -> str:
    """What Paradex keys a fixing by: base and expiry date, as written in its names (``BTC-18OCT26``)."""
    date = name["expiry_date"]
    return f"{name['base']}-{date.day}{date.strftime('%b').upper()}{date:%y}"


def contract_settlements(payloads: Iterable[Mapping[str, Any]], seen_at: pd.Timestamp) -> pd.DataFrame:
    """``markets/settlement-price`` responses -> per-contract settlement fields (``venue_symbol``,
    ``expiry``, ``settlement_index_id``, ``settlement_price`` = TWAP, ``settlement_source``,
    ``settlement_seen_at``, ``settlement_payoff_raw`` = payoff). Error payloads are skipped."""
    rows = []
    for p in payloads:
        name = parse_option_name(str(p.get("symbol", "")))
        if name is None or "underlying_twap_price" not in p:
            continue
        rows.append({
            "venue": VENUE,
            "venue_symbol": p["symbol"],
            "expiry": utc_naive(p.get("expiry_at"), "ms"),
            "settlement_index_id": venue_index_id(VENUE, name["base"]),
            "settlement_price": to_float(p.get("underlying_twap_price")),
            "settlement_source": "markets/settlement-price",
            "settlement_seen_at": pd.Timestamp(seen_at),
            "venue_ref": _venue_ref(name),
            "settlement_payoff_raw": to_float(p.get("settlement_price")),
        })
    return pd.DataFrame(rows)


def settlement_rows(contracts: pd.DataFrame) -> pd.DataFrame:
    """One ``SETTLEMENT_COLUMNS`` row per index and fixing from per-contract settlements; the method is the
    one ``SETTLEMENT_INDEXES`` declares for the index."""
    if contracts.empty:
        return frame_of([], SETTLEMENT_COLUMNS)
    fixings = (contracts.dropna(subset=["expiry"])
               .drop_duplicates(["settlement_index_id", "expiry"], keep="last")
               .sort_values(["settlement_index_id", "expiry"]))
    rows = []
    for f in fixings.itertuples():
        info = settlement_index_info(f.settlement_index_id)
        rows.append({
            "ts": f.expiry,
            "venue": VENUE,
            "settlement_index_id": f.settlement_index_id,
            "settlement_price": f.settlement_price,
            "method": info["method"],
            "method_source": info["method_source"],
            "source_endpoint": f.settlement_source,
            "venue_ref": f.venue_ref,
            "first_seen_at": f.settlement_seen_at,
        })
    return frame_of(rows, SETTLEMENT_COLUMNS)


def parse_market_history(events: Iterable[Mapping[str, Any]], seen_at: pd.Timestamp) -> pd.DataFrame:
    """``SETTLED`` events of ``markets/history`` -> per-contract settlement fields. The events carry no
    expiry instant (``updated_at`` is a few seconds after it): ``expiry`` is NaT, for the caller to join
    from the registry."""
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
            "expiry": pd.NaT,
            "settled_at": utc_naive(e.get("updated_at"), "ms"),
            "settlement_index_id": venue_index_id(VENUE, name["base"]),
            "settlement_price": to_float(fields.get("underlying_twap_price")),
            "settlement_source": "markets/history",
            "settlement_seen_at": pd.Timestamp(seen_at),
            "venue_ref": _venue_ref(name),
            "settlement_payoff_raw": to_float(fields.get("settlement_price")),
        })
    return pd.DataFrame(rows)


def parse_option_trades(records: Iterable[Mapping[str, Any]],
                        contract_ids: Optional[Mapping[str, str]] = None) -> pd.DataFrame:
    """``trades`` records -> ``OPTION_TRADE_COLUMNS``: taker ``side`` (``buy``/``sell``), ``trade_type`` as
    served, ``contract_id`` when the symbol is resolved. Names without an expiry are dropped."""
    contract_ids = contract_ids or {}
    rows: List[Dict[str, Any]] = []
    for t in records:
        symbol = str(t.get("market", ""))
        if parse_option_name(symbol) is None:
            continue
        rows.append({
            "ts": utc_naive(t.get("created_at"), "ms"),
            "venue": VENUE,
            "venue_symbol": symbol,
            "contract_id": contract_ids.get(symbol),
            "trade_id": t.get("id"),
            "price": to_float(t.get("price")),
            "size": to_float(t.get("size")),
            "side": str(t.get("side", "")).lower() or None,
            "trade_type": t.get("trade_type"),
            "mark_at_trade": math.nan,
            "index_at_trade": math.nan,
            "iv_at_trade": math.nan,
            "price_raw": t.get("price"),
            "raw_block_id": t.get("block_id") or None,
        })
    frame = frame_of(rows, OPTION_TRADE_COLUMNS)
    return frame.sort_values("ts", kind="stable").reset_index(drop=True) if not frame.empty else frame


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def _base(underlying: Optional[str]) -> Optional[str]:
    """``BTC``, ``BTC-USD`` or ``paradex:BTC`` -> ``BTC``."""
    if underlying is None:
        return None
    return underlying.split(":")[-1].split("-")[0]


class ParadexOptions:
    """The dated-options half of ``ParadexVenue`` (``OptionsVenue``): HTTP on top of the parsers above."""

    client: Any
    # A pass asks for each underlying in turn: the ~16 MB listing and the ~7 MB summary serve all of them.
    listing_ttl = pd.Timedelta(seconds=60)
    summary_ttl = pd.Timedelta(seconds=60)
    _listing: Optional[pd.DataFrame] = None
    _listing_at: Optional[pd.Timestamp] = None
    _resolved: Optional[pd.DataFrame] = None
    _summary: Optional[List[Mapping[str, Any]]] = None
    _summary_at: Optional[pd.Timestamp] = None

    def _fetch_listing(self, max_age: pd.Timedelta) -> pd.DataFrame:
        now = _now()
        if self._listing is None or self._listing_at is None or now - self._listing_at > max_age:
            self._listing = parse_option_markets(self.client.get("markets").get("results") or [], now)
            self._listing_at, self._resolved = now, None
        return self._listing

    def option_contracts(self, underlying: Optional[str] = None, *, expired: bool = False) -> pd.DataFrame:
        """Live option contracts (``markets``, ~16 MB, reused for ``listing_ttl``) in
        ``OPTION_SNAPSHOT_COLUMNS``, optionally of one native base. Paradex drops a contract from ``markets``
        at expiry: ``expired=True`` gives an empty listing (expired contracts are the registry's, their
        fixings ``settlements()``'s)."""
        require(self, Dataset.OPTION_CONTRACTS)
        if expired:
            return frame_of([], OPTION_SNAPSHOT_COLUMNS)
        listing = self._fetch_listing(self.listing_ttl)
        base = _base(underlying)
        if base is None or listing.empty:
            return listing
        return listing[listing["venue_base"] == base].reset_index(drop=True)

    def _contracts(self, symbols: Iterable[str]) -> pd.DataFrame:
        """Resolved live contracts, cached; the listing is fetched again when a symbol is unknown (strikes
        are listed every 8 hours)."""
        if self._listing is None or not set(symbols) <= set(self._listing["venue_symbol"]):
            self._fetch_listing(pd.Timedelta(0))
        if self._resolved is None:
            build = resolve_option_contracts(VENUE, self._listing)
            self._resolved = (build.contracts if not build.contracts.empty
                              else frame_of([], OPTION_CONTRACT_COLUMNS))
            self._resolved = pd.concat([self._resolved, self._listing[
                ~self._listing["venue_symbol"].isin(self._resolved["venue_symbol"])]], ignore_index=True)
        return self._resolved

    def resolved_contracts(self, underlying: Optional[str] = None) -> pd.DataFrame:
        """The cached live listing with the registry's identity columns (``contract_id``... empty for the
        unresolved ones), fetched on first use."""
        resolved = self._contracts([])
        base = _base(underlying)
        return resolved if base is None else resolved[resolved["venue_base"] == base].reset_index(drop=True)

    def option_chain(self, underlying: str, expiry: Optional[datetime] = None, *,
                     ts_snapshot: Optional[pd.Timestamp] = None) -> pd.DataFrame:
        """Live chain of one native base from ``markets/summary?market=ALL`` (one ~7 MB call, reused for
        ``summary_ttl``: the underlyings of one pass share it; each row keeps its own venue ``ts``).
        ``contract_id`` is set for the resolved contracts."""
        require(self, Dataset.OPTION_CHAIN)
        now = _now()
        ts_snapshot = now if ts_snapshot is None else pd.Timestamp(ts_snapshot)
        if self._summary is None or self._summary_at is None or now - self._summary_at > self.summary_ttl:
            self._summary = self.client.get("markets/summary", {"market": "ALL"}).get("results") or []
            self._summary_at = now
        base = _base(underlying)
        rows = [r for r in self._summary
                if str(r.get("symbol", "")).startswith(f"{base}-") and parse_option_name(r["symbol"])]
        contracts = self._contracts(r["symbol"] for r in rows)
        if expiry is not None:
            contracts = contracts[contracts["expiry"] == pd.Timestamp(expiry)]
        return parse_option_summary(rows, contracts, ts_snapshot)

    def option_trades(self, underlying: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Trades of every option of one base in ``[start, end]``, expired contracts included;
        ``contract_id`` is set for the live resolved ones."""
        require(self, Dataset.OPTION_TRADES)
        records, cursor = [], None
        params = {"base_asset": _base(underlying), "start_at": int(pd.Timestamp(start).value // 10**6),
                  "end_at": int(pd.Timestamp(end).value // 10**6), "page_size": TRADE_PAGE}
        while True:
            payload = self.client.get("trades", {**params, "cursor": cursor} if cursor else params)
            records.extend(payload.get("results") or [])
            cursor = payload.get("next")
            if not cursor or not payload.get("results"):
                break
        resolved = self._resolved if self._resolved is not None else pd.DataFrame()
        ids = {} if resolved.empty else dict(
            resolved.dropna(subset=["contract_id"])[["venue_symbol", "contract_id"]].itertuples(index=False))
        return parse_option_trades(records, ids)

    def option_mark_candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                            *, price: str = "mark") -> pd.DataFrame:
        """``mark`` or ``underlying`` candles of one live contract, ``OPTION_MARK_CANDLE_COLUMNS``."""
        require(self, Dataset.OPTION_MARK_CANDLES)
        if price not in ("mark", "underlying"):
            raise ValueError(f"price must be 'mark' or 'underlying', got {price!r}")
        candles = self._klines(symbol, resolution, start, end, price)
        resolved = self._resolved if self._resolved is not None else pd.DataFrame()
        match = resolved.loc[resolved["venue_symbol"] == symbol, "contract_id"] if not resolved.empty else []
        frame = candles.reset_index().assign(
            venue=VENUE, venue_symbol=symbol, volume_quote=math.nan,  # klines volume = premium traded
            contract_id=match.iloc[0] if len(match) else None)
        return frame_of(frame.to_dict("records"), OPTION_MARK_CANDLE_COLUMNS)

    def settlement_prices(self, symbols: Iterable[str]) -> pd.DataFrame:
        """Per-contract settlements of expired contracts, by name (``contract_settlements``); a market not
        settled (or unknown) is skipped."""
        payloads = []
        for symbol in symbols:
            try:
                payloads.append(self.client.get("markets/settlement-price", {"market": symbol}))
            except HttpError as exc:  # 404 MARKET_NOT_SETTLED / MARKET_NOT_FOUND
                payloads.append({"symbol": symbol, "error": exc.body[:200]})
        return contract_settlements(payloads, _now())

    def settlements(self, index: str, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> pd.DataFrame:
        """Fixings of one index (``paradex:BTC`` or ``BTC``), ``SETTLEMENT_COLUMNS``. Expiries are found by
        name: in the ``SETTLED`` events of ``markets/history`` (every settled contract, ~2.5 days back) and,
        with ``start``, in the trade history since ``start`` (``SETTLE_MARKET`` trades name the contracts
        that had an open position); one ``settlement-price`` request per expiry gives its exact instant and
        TWAP. Run at least every other day, nothing is missed; an older expiry without an open position is
        only reachable through a name the registry kept (``paradex_snapshot.settle_expired``)."""
        require(self, Dataset.SETTLEMENTS)
        base = _base(index)
        end = _now() if end is None else pd.Timestamp(end)
        events = self.client.get("markets/history", {"market": "ALL"}).get("results") or []
        names = [e["symbol"] for e in events if e.get("event_type") == "SETTLED"
                 and str(e.get("symbol", "")).startswith(f"{base}-")]
        if start is not None:
            names += self.option_trades(base, start, end)["venue_symbol"].tolist()
        by_date: Dict[pd.Timestamp, str] = {}
        for name in sorted(set(names)):
            parsed = parse_option_name(name)
            if parsed and parsed["base"] == base and parsed["expiry_date"] <= end:
                by_date.setdefault(parsed["expiry_date"], name)
        rows = settlement_rows(self.settlement_prices(by_date.values()))
        if rows.empty:
            return rows
        keep = rows["ts"] <= end if start is None else rows["ts"].between(pd.Timestamp(start), end)
        return rows[keep].reset_index(drop=True)


__all__ = [
    "FEE_MODEL",
    "OPTION_DATASETS",
    "ParadexOptions",
    "YEAR_DAYS",
    "contract_settlements",
    "parse_market_history",
    "parse_option_markets",
    "parse_option_name",
    "parse_option_summary",
    "parse_option_trades",
    "payoff",
    "settlement_rows",
]
