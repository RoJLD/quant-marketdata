"""Bybit linear perps (v5 API) through the generic ccxt venue (``ccxt_venue.py``; ``[ccxt]`` extra).

Conventions checked on raw payloads, 2026-10-30 (fixtures in ``tests/fixtures/bybit``):

- ``instruments-info?category=linear`` lists 883 perps on USDT (815, ``<BASE>USDT``) and USDC (68,
  ``<BASE>PERP``: ``BTCPERP`` is BTC/USDC) plus dated ``LinearFutures`` (dropped), only those ``Trading`` or
  ``PreLaunch`` unless asked: the **332 delisted perps** (2026-10-31) come with ``status=Closed`` (two pages
  of 1,000 with a cursor), each with its ``deliveryTime`` (the delisting), and their funding history is served
  (``10000LADYSUSDT`` until 2025-10-06). They are listed too, ``status`` "delisted": no survivorship bias.
- ``fundingInterval`` (minutes) is in every instrument: 480 (466), 240 (415), 60 (2) on the day. Intervals
  change over time (``BLURUSDT``: 8 h, then 1 h), hence the gap rule capped at 8 h.
- ``symbolType`` declares the class: ``""`` and ``innovation`` (the innovation zone) are crypto, ``stock``
  (232), ``ETF`` (59), ``commodity`` (4: XAU, XAG, CL, BZ), ``forex`` (3: EURUSD, GBPUSD, USDJPY);
  ``underlyingTicker`` names the stock (``AMDSTOCKUSDT`` -> AMD, ``BRKBUSDT`` -> BRK.B, ``TENCENTUSDT`` ->
  00700). Pre-IPO companies in ``PreLaunch`` (OPENAI, ANTHROPIC, MOONSHOT, OURA, POLYMARKET) are declared
  ``stock``: overrides make them private, as on Lighter. Scaled bases: ``1000``, ``10000``, ``1000000``,
  ``10000000`` prefixes and one suffix, ``SHIB1000`` (override).
- ``funding/history`` returns the **newest** 200 records up to ``endTime``, newest first; ``startTime``
  alone is refused (``Time Is Invalid``). Paged back with ``endTime``. History since listing: BTCUSDT from
  2020-03-25 16:00. ``fundingRate`` is a signed fraction per interval, **positive = longs pay**: the current
  rates correlate with Binance's at 0.95 (Pearson, 675 shared symbols, no opposite sign beyond 2 bp), and the
  settled BTC events at 0.57 over 200 common settlements (mean 4.4e-5 per event against Binance's 5.0e-5).
- ``tickers?category=linear`` (one request) serves last, mark and index prices, ``fundingIntervalHour`` and
  two open interests: ``openInterest`` is **both sides** (the docs: "the sum of both sides") and
  ``singleOpenInterest`` one side, exactly half on 926 of 926 symbols. ccxt 4.5.52 ``fetch_open_interest``
  reads ``openInterest`` (BTC 68,181 instead of 34,090): this adapter reads ``singleOpenInterest`` (base units).
- Limits: 600 requests per 5 s per IP, then 403 and 10 minutes away (docs); paced at 10 requests/s at most.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.ccxt_venue import CcxtVenue, num, records_frame, stamp
from quant_marketdata.venues.paging import backward_fetch
from quant_marketdata.venues.schema import Dataset, make_open_interest, make_prices

FUNDING_PAGE = 200
CLASSES = {"": "crypto", "innovation": "crypto", "stock": "equity", "ETF": "etf", "commodity": "commodity",
           "forex": "fx"}
STATUSES = {"Trading": "active", "PreLaunch": "pending", "PendingOpen": "pending", "Settling": "inactive",
            "Delivering": "inactive", "Closed": "delisted"}


def parse_funding_history(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """``funding/history`` records (``fundingRateTimestamp`` ms, ``fundingRate``) -> raw events."""
    return records_frame(records, "fundingRateTimestamp", "fundingRate")


def parse_tickers(payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """``tickers?category=linear`` -> (open-interest records, price records) by symbol."""
    ts = stamp(payload.get("time"))
    oi, prices = {}, {}
    for t in (payload.get("result") or {}).get("list") or []:
        single = t.get("singleOpenInterest")
        if single not in (None, ""):
            base, convention = num(single), "one_side"
        else:  # not served: the both-sides figure, declared as such
            base, convention = num(t.get("openInterest")), "both_sides"
        oi[t["symbol"]] = {"open_interest_base": base, "price": num(t.get("markPrice")), "price_kind": "mark",
                           "oi_convention": convention, "ts": ts}
        prices[t["symbol"]] = {"mark_price": t.get("markPrice"), "index_price": t.get("indexPrice"),
                               "last_price": t.get("lastPrice"), "index_kind": "index", "ts": ts}
    return oi, prices


class BybitVenue(CcxtVenue):
    name = "bybit"
    exchange_id = "bybit"
    max_rate = 10.0
    market_types = ("linear",)
    funding_page = FUNDING_PAGE
    book_limits = (1, 25, 50, 100, 200, 500, 1000)
    oi_convention = "one_side"
    parse_funding = staticmethod(parse_funding_history)

    def extra_markets(self) -> List[Dict[str, Any]]:
        """Delisted perps (``status=Closed``), which ccxt does not load."""
        out, params = [], {"category": "linear", "status": "Closed", "limit": 1000}
        for _ in range(20):
            payload = self.raw("public_get_v5_market_instruments_info", params)
            result = payload.get("result") or {}
            for i in result.get("list") or []:
                if i.get("contractType") == "LinearPerpetual" and i.get("settleCoin") in self.settle_assets:
                    out.append({"id": i["symbol"], "symbol": None, "base": i.get("baseCoin"),
                                "settle": i.get("settleCoin"), "swap": True, "linear": True, "active": False,
                                "contractSize": 1.0, "info": i})
            if not result.get("nextPageCursor"):
                break
            params = {**params, "cursor": result["nextPageCursor"]}
        return out

    def load_intervals(self) -> Dict[str, float]:
        """``fundingInterval`` (minutes) of each instrument: no extra request."""
        return {s: num(m["info"]["fundingInterval"]) / 60 for s, m in self.markets().items()
                if num((m.get("info") or {}).get("fundingInterval")) > 0}

    def asset_class(self, market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        declared = (market.get("info") or {}).get("symbolType") or ""
        if declared in CLASSES:
            return CLASSES[declared], None
        return None, f"undeclared class: symbolType {declared!r}"

    def underlying_ref(self, market: Dict[str, Any]) -> Optional[str]:
        return (market.get("info") or {}).get("underlyingTicker") or market.get("base")

    def listed_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        return stamp((market.get("info") or {}).get("launchTime"))

    def delisted_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        info = market.get("info") or {}
        return stamp(info.get("deliveryTime")) if info.get("status") == "Closed" else pd.NaT

    def status(self, market: Dict[str, Any]) -> str:
        raw = (market.get("info") or {}).get("status")
        return STATUSES.get(raw, str(raw or "").lower())

    def funding_records(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        def fetch_before(cursor: int) -> pd.DataFrame:
            payload = self.raw("public_get_v5_market_funding_history",
                               {"category": "linear", "symbol": symbol, "endTime": cursor, "limit": FUNDING_PAGE})
            return parse_funding_history((payload.get("result") or {}).get("list") or [])

        return backward_fetch(fetch_before, start_ms, end_ms, FUNDING_PAGE)

    def _tickers(self):
        oi, prices = parse_tickers(self.raw("public_get_v5_market_tickers", {"category": "linear"}))
        known = self.markets()
        return ({s: r for s, r in oi.items() if s in known}, {s: r for s, r in prices.items() if s in known})

    def open_interest(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live one-side open interest of every perp (``singleOpenInterest``, base units, at the mark price)
        from ``tickers``: one request."""
        require(self, Dataset.OPEN_INTEREST)
        return make_open_interest(self._tickers()[0], symbols)

    def prices(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live mark, index and last price of every perp from ``tickers``: one request."""
        require(self, Dataset.PRICES)
        return make_prices(self._tickers()[1], symbols)


register_venue(BybitVenue.name, BybitVenue)

__all__ = ["BybitVenue", "parse_funding_history", "parse_tickers"]
