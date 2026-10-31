"""Bitget USDT-M and USDC-M perps (v2 mix API) through the generic ccxt venue (``ccxt_venue.py``; ``[ccxt]``
extra).

Conventions checked on raw payloads, 2026-10-30 (fixtures in ``tests/fixtures/bitget``):

- ccxt's swap markets: 868 USDT perps (``BTCUSDT``, product type ``USDT-FUTURES``), 49 USDC perps (``BTCPERP``,
  ``USDC-FUTURES``) and **seven demo contracts** settled in ``SUSDT``, ``SUSDC``, ``SBTC``, ``SETH``
  (``SBTCSUSDT``...): dropped by the settle-asset filter. Scaled bases ``1000``, ``1000000`` and ``1M``. Three
  Chinese-named memecoins are unresolved (no ASCII ticker).
- **404 of the 868 USDT perps are flagged ``isRwa=YES``** (stocks, ETFs, indices, metals, FX, pre-IPO
  companies, and the gold tokens PAXG and XAUT) without their class: they are left unresolved, with that
  reason, unless an override classifies them; ``isRwa=NO`` is crypto.
- ``fundInterval`` (hours) is in every contract (8 h 491, 4 h 376, 1 h 1); ``openTime`` is the listing (748
  of 868; ``launchTime`` empty), ``offTime`` -1 unless a delisting is scheduled.
- **Funding history is a rolling window of 90 days** (``funding_window``): BTC and a 4 h symbol both start at
  2026-08-02 00:00 on 2026-10-30. ``history-fund-rate`` ignores time parameters (and ccxt's ``since``): page
  numbers, 100 rows each, newest first (a 4 h symbol: pages of 100 x 5 + 40, then an empty page).
- ``fundingRate`` is a signed fraction per interval, **positive = longs pay**: current rates correlate with
  Binance's at 0.97 (661 shared symbols, no opposite sign beyond 2 bp). Settled BTC events are noisier (6
  decimals, 29 % at the 1e-4 default): correlation 0.09 with Binance's over 170 settlements, no lag does
  better, means 5.5e-5 and 5.0e-5.
- ``tickers`` (one request per product type) serves last, mark and index prices and ``holdingAmount``, which
  equals ``open-interest`` ``size`` (base units). Counted as **one side**: BTC's 37,053 is level with Bybit's
  one-side figure (34,012) and OKX's (34,655); the payload does not say.
- No public open-interest history. Limit: 20 requests per endpoint and second (header); paced at 8 at most.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import pandas as pd

from quant_marketdata.venues.base import register_venue, require
from quant_marketdata.venues.ccxt_venue import CcxtVenue, num, records_frame, stamp
from quant_marketdata.venues.paging import numbered_fetch
from quant_marketdata.venues.schema import Dataset, make_open_interest, make_prices

FUNDING_PAGE = 100
WINDOW = pd.Timedelta(days=90)
PRODUCT_TYPES = {"USDT": "USDT-FUTURES", "USDC": "USDC-FUTURES"}
STATUSES = {"normal": "active", "limit_open": "active", "restrictedAPI": "active", "listed": "pending",
            "maintain": "inactive", "off": "delisted"}
RWA_REASON = "Bitget RWA perp (isRwa=YES): the venue does not declare its class"


def _data(payload: Dict[str, Any]) -> Any:
    return (payload or {}).get("data") or []


def parse_funding_history(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """``history-fund-rate`` rows (``fundingTime`` ms, ``fundingRate``) -> raw events."""
    return records_frame(records, "fundingTime", "fundingRate")


def parse_tickers(payload: Dict[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """``tickers`` -> (open-interest records, price records) by symbol."""
    oi, prices = {}, {}
    for t in _data(payload):
        ts = stamp(t.get("ts"))
        oi[t["symbol"]] = {"open_interest_base": num(t.get("holdingAmount")), "price": num(t.get("markPrice")),
                           "price_kind": "mark", "oi_convention": "one_side", "ts": ts}
        prices[t["symbol"]] = {"mark_price": t.get("markPrice"), "index_price": t.get("indexPrice"),
                               "last_price": t.get("lastPr"), "index_kind": "index", "ts": ts}
    return oi, prices


class BitgetVenue(CcxtVenue):
    name = "bitget"
    exchange_id = "bitget"
    max_rate = 8.0
    market_types = ("swap",)
    funding_window = WINDOW
    funding_page = FUNDING_PAGE
    book_limits = (1, 5, 15, 50, 100)
    oi_convention = "one_side"
    parse_funding = staticmethod(parse_funding_history)

    def product_type(self, symbol: str) -> str:
        return PRODUCT_TYPES[self.markets()[symbol]["settle"]]

    def load_intervals(self) -> Dict[str, float]:
        """``fundInterval`` (hours) of each contract: no extra request."""
        return {s: num(m["info"]["fundInterval"]) for s, m in self.markets().items()
                if num((m.get("info") or {}).get("fundInterval")) > 0}

    def asset_class(self, market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        flag = (market.get("info") or {}).get("isRwa")
        if flag == "NO":
            return "crypto", None
        return None, RWA_REASON if flag == "YES" else f"undeclared class: isRwa {flag!r}"

    def listed_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        info = market.get("info") or {}
        return stamp(info.get("openTime") or info.get("launchTime"))

    def delisted_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        info = market.get("info") or {}
        return stamp(info.get("offTime")) if info.get("symbolStatus") == "off" else pd.NaT

    def status(self, market: Dict[str, Any]) -> str:
        raw = (market.get("info") or {}).get("symbolStatus")
        return STATUSES.get(raw, str(raw or "").lower())

    def funding_records(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        product = self.product_type(symbol)

        def fetch_page(number: int) -> pd.DataFrame:
            return parse_funding_history(_data(self.raw("public_mix_get_v2_mix_market_history_fund_rate", {
                "symbol": symbol, "productType": product, "pageSize": FUNDING_PAGE, "pageNo": number})))

        raw = numbered_fetch(fetch_page, pd.Timestamp(start_ms, unit="ms"), FUNDING_PAGE)
        return raw if raw.empty else raw.loc[pd.Timestamp(start_ms, unit="ms"):pd.Timestamp(end_ms, unit="ms")]

    def _tickers(self):
        oi, prices = {}, {}
        for settle in sorted({m["settle"] for m in self.markets().values()}):
            o, p = parse_tickers(self.raw("public_mix_get_v2_mix_market_tickers",
                                          {"productType": PRODUCT_TYPES[settle]}))
            oi.update(o)
            prices.update(p)
        known = self.markets()
        return ({s: r for s, r in oi.items() if s in known}, {s: r for s, r in prices.items() if s in known})

    def open_interest(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live open interest of every perp (``holdingAmount``, base units, one side) at the mark price from
        ``tickers``: one request per product type."""
        require(self, Dataset.OPEN_INTEREST)
        return make_open_interest(self._tickers()[0], symbols)

    def prices(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live mark, index and last price of every perp from ``tickers``: one request per product type."""
        require(self, Dataset.PRICES)
        return make_prices(self._tickers()[1], symbols)


register_venue(BitgetVenue.name, BitgetVenue)

__all__ = ["BitgetVenue", "parse_funding_history", "parse_tickers"]
