"""Gate USDT perpetual futures (v4 API) through the generic ccxt venue (``ccxt_venue.py``; ``[ccxt]`` extra).

Conventions checked on raw payloads, 2026-10-30 (fixtures in ``tests/fixtures/gate``):

- ccxt's swap markets: 1,070 USDT perps (``BTC_USDT``) and one BTC-margined inverse (dropped). The contract
  size is ``quanto_multiplier`` (BTC 0.0001). ``contract_type`` declares the class: ``""`` crypto (595),
  ``stocks`` (438, ETFs included: equity, as declared), ``indices`` (18), ``metals`` (12, incl. the gold tokens
  PAXG and XAUT and the ETFs IAU and SLV: overrides), ``commodities`` (3), ``forex`` (4, incl. the stablecoin
  ``USDC_USDT``: override); ``is_pre_market`` stocks are pre-IPO companies (ANTHROPIC, OPENAI, ANDURIL...):
  private. ``create_time`` (s) is the listing (the first funding record falls on it).
- ``funding_interval`` (seconds) is in every contract: 8 h 646, 4 h 420, 1 h 4.
- **Funding history since listing** (BTC from 2019-11-18 08:00): ``funding_rate`` returns the **30 days before
  ``to``** whatever ``limit`` (90 rows on 8 h, 180 on 4 h, 720 on 1 h), newest first, and nothing before the
  listing; without ``to``, the last 30 days. ccxt maps ``since`` to ``from``, refused beyond 180 days. Paged
  back with ``to``; an empty window (a suspension longer than 30 days) is stepped over down to the start. Some
  2026 rows are stamped one second past the hour (``00:00:01``), kept as served.
- ``r`` is a signed fraction per interval, **positive = longs pay**: current rates correlate with Binance's at
  0.92 (717 shared symbols, no opposite sign beyond 2 bp), settled BTC events at 0.47 (90 settlements, means
  3.7e-5 and 4.2e-5).
- **Open interest counts both sides.** ``tickers`` (one request) gives ``total_size`` in contracts, and the
  contract list's ``position_size`` (documented as the total long position) is exactly half of it:
  ``total_size / position_size`` = 2.0000 at the median over 1,031 contracts (1st-99th percentiles 1.9998 and
  2.0001; the two payloads are seconds apart). BTC: 586.7M contracts = 58,669 BTC both sides, 29,333 one side.
  The 2026-10-29 inventory read ``total_size`` as one side (0.60x Binance): it is 0.31x. Stored as published,
  ``oi_convention`` "both_sides" (``one_side_usd`` halves it). ccxt has no open interest for Gate.
- ``tickers`` also serves last, mark and index prices. Header limit: 200 requests per endpoint window; paced
  at 8 requests/s at most.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.ccxt_venue import CcxtVenue, num, records_frame, stamp
from quant_marketdata.venues.paging import backward_fetch
from quant_marketdata.venues.schema import Dataset, make_open_interest, make_prices

FUNDING_LIMIT = 1000
WINDOW_MS = 30 * 86_400_000  # what one ``to`` request serves
CLASSES = {"": "crypto", "stocks": "equity", "indices": "index", "metals": "commodity",
           "commodities": "commodity", "forex": "fx"}
STATUSES = {"trading": "active", "delisting": "inactive", "circuit_breaker": "inactive", "delisted": "delisted"}


def parse_funding_history(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """``funding_rate`` rows (``t`` in seconds, ``r``) -> raw events."""
    return records_frame(records, "t", "r", unit="s")


def parse_tickers(payload: List[Dict[str, Any]], ts: Optional[pd.Timestamp] = None
                  ) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """``tickers`` -> (open-interest records: ``total_size`` x ``quanto_multiplier``, both sides; price
    records) by contract."""
    oi, prices = {}, {}
    for t in payload or []:
        oi[t["contract"]] = {"open_interest_base": num(t.get("total_size")) * num(t.get("quanto_multiplier")),
                             "price": num(t.get("mark_price")), "price_kind": "mark",
                             "oi_convention": "both_sides", "ts": ts}
        prices[t["contract"]] = {"mark_price": t.get("mark_price"), "index_price": t.get("index_price"),
                                 "last_price": t.get("last"), "index_kind": "index", "ts": ts}
    return oi, prices


class GateVenue(CcxtVenue):
    name = "gate"
    exchange_id = "gate"
    max_rate = 8.0
    market_types = ("swap",)
    settle_assets = frozenset({"USDT"})
    book_limits = (1, 5, 10, 20, 50, 100, 200, 300)
    oi_convention = "both_sides"
    parse_funding = staticmethod(parse_funding_history)

    def load_intervals(self) -> Dict[str, float]:
        """``funding_interval`` (seconds) of each contract: no extra request."""
        return {s: num(m["info"]["funding_interval"]) / 3600 for s, m in self.markets().items()
                if num((m.get("info") or {}).get("funding_interval")) > 0}

    def asset_class(self, market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        info = market.get("info") or {}
        declared = info.get("contract_type") or ""
        if declared not in CLASSES:
            return None, f"undeclared class: contract_type {declared!r}"
        if declared == "stocks" and info.get("is_pre_market"):
            return "private", None
        return CLASSES[declared], None

    def listed_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        return stamp((market.get("info") or {}).get("create_time"), unit="s")

    def status(self, market: Dict[str, Any]) -> str:
        info = market.get("info") or {}
        if info.get("in_delisting") and info.get("status") == "trading":
            return "inactive"
        return STATUSES.get(info.get("status"), str(info.get("status") or "").lower())

    def funding_records(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        def fetch_before(cursor: int) -> pd.DataFrame:
            return parse_funding_history(self.raw("public_futures_get_settle_funding_rate", {
                "settle": "usdt", "contract": symbol, "to": cursor // 1000, "limit": FUNDING_LIMIT}) or [])

        return backward_fetch(fetch_before, start_ms, end_ms, empty_step=WINDOW_MS)

    def _tickers(self):
        stamp_now = self.clock()  # the payload carries no time
        oi, prices = parse_tickers(self.raw("public_futures_get_settle_tickers", {"settle": "usdt"}), stamp_now)
        known = self.markets()
        return ({s: r for s, r in oi.items() if s in known}, {s: r for s, r in prices.items() if s in known})

    def open_interest(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live open interest of every perp, **both sides** as published (``total_size`` x
        ``quanto_multiplier``, base units), at the mark price: one request."""
        require(self, Dataset.OPEN_INTEREST)
        return make_open_interest(self._tickers()[0], symbols)

    def prices(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live mark, index and last price of every perp from ``tickers``: one request."""
        require(self, Dataset.PRICES)
        return make_prices(self._tickers()[1], symbols)


register_venue(GateVenue.name, GateVenue)

__all__ = ["GateVenue", "parse_funding_history", "parse_tickers"]
