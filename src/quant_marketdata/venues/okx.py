"""OKX USDT perpetual swaps (v5 API) through the generic ccxt venue (``ccxt_venue.py``; ``[ccxt]`` extra).

Conventions checked on raw payloads, 2026-10-30 (fixtures in ``tests/fixtures/okx``):

- ccxt's swap markets: 517 linear swaps, all USDT-settled (``<BASE>-USDT-SWAP``), plus 15 coin-margined
  ``-USD-SWAP`` (dropped). The contract value ``ctVal`` (BTC 0.01) is in ``ctValCcy``, always the base: no
  scaled bases. ``instCategory`` declares the class: 1 crypto (289), 3 "stocks" (219: stocks, ETFs and the
  indices US500, US100, JP225, KR200, the last four classified by overrides), 4 commodities (9);
  ``ruleType=pre_market`` with a non-crypto category is a pre-IPO company (ANTHROPIC, OPENAI, MOONSHOT,
  OURA): private. ``listTime`` is the listing.
- **Funding history is a rolling window of about 94 days** (``funding_window``): on 2026-10-30 every symbol
  started at 2026-07-27 12:00 or 16:00, BTC included (286 rows); asking further back (``after`` older than the
  window) returns nothing. ``funding-rate-history`` returns the newest 400 records before ``after`` (exclusive),
  newest first: paged back with ``after``. ccxt maps ``since`` to ``before``, which returns the newest page
  **after** it: forward paging with ccxt would leave holes. A 4 h symbol: 400 + 173 rows over the window.
- Rows carry ``fundingRate`` and ``realizedRate`` (equal on all 286 BTC rows), ``method=current_period``;
  ``rate`` is ``realizedRate`` (the rate paid), a signed fraction per interval, **positive = longs pay**:
  current rates correlate with Binance's at 0.95 (452 shared symbols, no opposite sign beyond 2 bp), settled
  BTC events at 0.53 (286 settlements, means 5.4e-5 and 5.4e-5).
- Intervals: ``public/funding-rate?instId=ANY`` serves every swap's current ``fundingTime`` and
  ``nextFundingTime`` in one request; their gap is the interval (8 h 455, 4 h 333, 1 h 1). History shows
  symbols moved between 8, 4, 2 and 1 h.
- Open interest: ``public/open-interest?instType=SWAP``, every swap in one request: ``oi`` (contracts),
  ``oiCcy`` (base) and ``oiUsd``. Counted as **one side**: OKX's convention, and BTC's 34,655 is level with
  Bybit's one-side ``singleOpenInterest`` (34,012); the payload itself does not say. The ccxt ticker has no
  mark or index: ``public/mark-price`` and ``market/index-tickers`` (keyed by ``instFamily``, ``BTC-USDT``).
- Terms: public market data "solely for your own personal, non-commercial trading" (OKX API Agreement §9.4):
  Robin's reading pending (Quant-lab ``wi-venue-data-terms``).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.ccxt_venue import CcxtVenue, num, records_frame, stamp
from quant_marketdata.venues.paging import backward_fetch
from quant_marketdata.venues.schema import Dataset, make_open_interest, make_prices

FUNDING_PAGE = 400
WINDOW = pd.Timedelta(days=94)
CLASSES = {"1": "crypto", "3": "equity", "4": "commodity", "5": "fx", "6": "rates"}
STATUSES = {"live": "active", "suspend": "inactive", "preopen": "pending", "rebase": "inactive"}


def _data(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    return (payload or {}).get("data") or []


def parse_funding_history(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """``funding-rate-history`` rows -> raw events, ``rate`` = ``realizedRate`` (``fundingRate`` if absent)."""
    rows = [{**r, "rate": r.get("realizedRate") or r.get("fundingRate")} for r in records]
    return records_frame(rows, "fundingTime", "rate")


def parse_intervals(payload: Dict[str, Any]) -> Dict[str, float]:
    """``funding-rate?instId=ANY`` -> interval in hours per instId (``nextFundingTime - fundingTime``)."""
    out = {}
    for r in _data(payload):
        gap = num(r.get("nextFundingTime")) - num(r.get("fundingTime"))
        if gap > 0:
            out[r["instId"]] = gap / 3_600_000
    return out


def parse_open_interest(payload: Dict[str, Any], marks: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """``open-interest?instType=SWAP`` + marks by instId -> records (``oiCcy`` base, ``oiUsd`` as published)."""
    return {r["instId"]: {"open_interest_base": num(r.get("oiCcy")), "open_interest_usd": num(r.get("oiUsd")),
                          "price": num(marks.get(r["instId"])), "price_kind": "mark",
                          "oi_convention": "one_side", "ts": stamp(r.get("ts"))}
            for r in _data(payload)}


def parse_prices(marks: Dict[str, Any], indexes: Dict[str, Any], tickers: Dict[str, Any],
                 families: Dict[str, str]) -> Dict[str, Dict[str, Any]]:
    """``mark-price``, ``index-tickers`` and ``tickers`` payloads -> price records by instId; ``families``
    maps an instId to its index (``instFamily``, ``BTC-USDT``)."""
    mark = {r["instId"]: r for r in _data(marks)}
    index = {r["instId"]: r.get("idxPx") for r in _data(indexes)}
    last = {r["instId"]: r.get("last") for r in _data(tickers)}
    return {inst: {"mark_price": r.get("markPx"), "index_price": index.get(families.get(inst)),
                   "last_price": last.get(inst), "index_kind": "index", "ts": stamp(r.get("ts"))}
            for inst, r in mark.items()}


class OkxVenue(CcxtVenue):
    name = "okx"
    exchange_id = "okx"
    max_rate = 4.0  # funding-rate-history: 10 requests per 2 s per IP
    market_types = ("swap",)
    funding_window = WINDOW
    funding_page = FUNDING_PAGE
    book_limits = (1, 5, 10, 20, 50, 100, 200, 400)
    oi_convention = "one_side"
    parse_funding = staticmethod(parse_funding_history)

    def load_intervals(self) -> Dict[str, float]:
        """One request for every swap (``instId=ANY``)."""
        return parse_intervals(self.raw("public_get_public_funding_rate", {"instId": "ANY"}))

    def asset_class(self, market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        info = market.get("info") or {}
        category = str(info.get("instCategory") or "")
        if category not in CLASSES:
            return None, f"undeclared class: instCategory {category!r}"
        if info.get("ruleType") == "pre_market" and category != "1":
            return "private", None
        return CLASSES[category], None

    def listed_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        return stamp((market.get("info") or {}).get("listTime"))

    def status(self, market: Dict[str, Any]) -> str:
        raw = (market.get("info") or {}).get("state")
        return STATUSES.get(raw, str(raw or "").lower())

    def funding_records(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        def fetch_before(cursor: int) -> pd.DataFrame:  # ``after`` is exclusive
            return parse_funding_history(_data(self.raw("public_get_public_funding_rate_history",
                                                        {"instId": symbol, "after": cursor + 1,
                                                         "limit": FUNDING_PAGE})))

        return backward_fetch(fetch_before, start_ms, end_ms, FUNDING_PAGE)

    def _marks(self) -> Dict[str, Any]:
        return self.raw("public_get_public_mark_price", {"instType": "SWAP"})

    def open_interest(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live one-side open interest of every swap (``oiCcy``, ``oiUsd``) at the mark price: two requests."""
        require(self, Dataset.OPEN_INTEREST)
        marks = {r["instId"]: r.get("markPx") for r in _data(self._marks())}
        records = parse_open_interest(self.raw("public_get_public_open_interest", {"instType": "SWAP"}), marks)
        return make_open_interest({s: r for s, r in records.items() if s in self.markets()}, symbols)

    def prices(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live mark (``mark-price``), index (``index-tickers`` of the swap's ``instFamily``) and last
        (``tickers``) of every swap: three requests (four with USDC swaps)."""
        require(self, Dataset.PRICES)
        families = {s: (m.get("info") or {}).get("instFamily") for s, m in self.markets().items()}
        quotes = sorted({str(f).rsplit("-", 1)[-1] for f in families.values() if f})
        indexes = {"data": [r for q in quotes
                            for r in _data(self.raw("public_get_market_index_tickers", {"quoteCcy": q}))]}
        records = parse_prices(self._marks(), indexes, self.raw("public_get_market_tickers", {"instType": "SWAP"}),
                               families)
        return make_prices({s: r for s, r in records.items() if s in self.markets()}, symbols)


register_venue(OkxVenue.name, OkxVenue)

__all__ = ["OkxVenue", "parse_funding_history", "parse_intervals", "parse_open_interest", "parse_prices"]
