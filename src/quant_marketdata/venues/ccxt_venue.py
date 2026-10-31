"""Generic perp venue backed by ccxt, for the exchanges without a hand-written adapter (Bybit, OKX, Bitget and
Gate so far, ``bybit.py`` ... ``gate.py``). Needs the ``ccxt`` extra: ``pip install "quant-marketdata[ccxt]"``.

ccxt is imported lazily, the first time a venue talks to its exchange: the core stays pandas-only, and the
venues register, list and declare their capabilities without it.

What ccxt does here, and what it does not:

- **Transport.** Every request goes through the venue's ccxt exchange object, with ccxt's own throttle on
  (``enableRateLimit``, the venue's documented limits) and, on top of it, this package's ``RateLimiter`` at
  ``min(rate, max_rate)`` requests per second. Network errors and 429-style refusals are retried with backoff;
  an anti-bot refusal (``DDoSProtection``) is not. Raw endpoints are called through ccxt's implicit API
  methods (``public_get_v5_market_funding_history``...), so a venue reads each payload exactly as served.
- **Markets.** ``load_markets`` (restricted to the venue's ``market_types``), filtered to linear perps settled
  in ``settle_assets`` (USDT, USDC); the venue's own fields stay in ``market["info"]``.
- **Unified methods** are the generic path, for a venue whose overrides are not written yet:
  ``fetch_funding_rate_history`` paged forward from ``since``, ``fetch_funding_rates`` for the intervals,
  ``fetch_open_interest`` per symbol and ``fetch_tickers`` for prices. The 2026-10-29 inventory (Quant-lab
  ``docs/reference/venue_inventory_2026-10.md`` §6) found that most venues ignore ccxt's ``since`` (Bitget,
  Gate, MEXC, BingX, HTX, WhiteBIT, Crypto.com, Bitfinex; OKX maps it to ``before``, which returns the newest
  page after it), that ccxt reads Bybit's both-sides open interest and has none for Gate. A venue is therefore
  registered only with overrides checked on its raw payloads, frozen in recorded fixtures.

Conventions shared by the venue modules:

- Native symbol = the venue's market id (``market["id"]``: ``BTCUSDT``, ``BTC-USDT-SWAP``, ``BTC_USDT``);
  ``venue_market_id`` = ccxt's unified symbol (``BTC/USDT:USDT``).
- Funding ``rate`` = the venue's rate per interval, **positive when longs pay** (checked on every venue against
  Binance, see each module); ``interval_h`` = the gap to the previous settlement, capped at ``max_interval_h``,
  because every venue moves symbols between 8, 4, 2 and 1 hour intervals over time.
- ``funding_window``: how much funding history the venue serves (``None``: since listing; else a rolling
  window, OKX ~94 days, Bitget 90). A download that resumes before the window logs what was lost; the raw
  archive (``venues.raw_archive``) holds what was collected before the adapter existed.
- ``parse_funding``: the venue's pure parser of raw funding records, also used on archived rows.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any, Callable, Dict, FrozenSet, List, Optional, Sequence, Tuple

import pandas as pd

from quant_marketdata.venues.base import WINDOW_MARGIN, require
from quant_marketdata.venues.http import RateLimiter
from quant_marketdata.venues.paging import forward_fetch
from quant_marketdata.venues.schema import (
    INSTRUMENT_COLUMNS,
    Dataset,
    finalize_funding,
    make_book,
    make_open_interest,
    make_prices,
)

logger = logging.getLogger(__name__)

PERP_CAPABILITIES = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.SPECS, Dataset.BOOK,
                               Dataset.OPEN_INTEREST, Dataset.PRICES})
# ``instruments()`` adds this column for markets the venue itself leaves unclassified (``registry.resolve``).
SNAPSHOT_COLUMNS = [c for c in INSTRUMENT_COLUMNS if c != "instrument_id"] + ["unresolved_reason"]
ATTEMPTS = 4
BASE_DELAY_S = 2.0


def ms(value: datetime | pd.Timestamp) -> int:
    return int(pd.Timestamp(value).value // 1_000_000)


def stamp(value: Any, unit: str = "ms") -> pd.Timestamp:
    """A venue time field (ms or s, as text or number) -> naive UTC timestamp; NaT when empty or <= 0."""
    try:
        number = int(float(value))
    except (TypeError, ValueError):
        return pd.NaT
    return pd.Timestamp(number, unit=unit) if number > 0 else pd.NaT


def num(value: Any) -> float:
    try:
        return float(value) if value not in (None, "") else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def make_exchange(exchange_id: str, market_types: Optional[Sequence[str]] = None) -> Any:
    """A public (keyless) ccxt exchange, its market loading restricted to ``market_types``."""
    try:
        import ccxt
    except ImportError as exc:  # the core works without the extra
        raise ImportError(f"the {exchange_id} venue needs ccxt: pip install \"quant-marketdata[ccxt]\"") from exc
    config: Dict[str, Any] = {"enableRateLimit": True, "timeout": 30_000}
    if market_types:
        config["options"] = {"fetchMarkets": {"types": list(market_types)}}
    return getattr(ccxt, exchange_id)(config)


def _retryable(exc: Exception) -> bool:
    try:
        from ccxt.base.errors import DDoSProtection, NetworkError
    except ImportError:
        return False
    return isinstance(exc, NetworkError) and not isinstance(exc, DDoSProtection)


def parse_unified_funding(records: List[Dict[str, Any]]) -> pd.DataFrame:
    """ccxt ``fetch_funding_rate_history`` records -> ``rate_raw`` and signed fractional ``rate``."""
    rows = [r for r in records if r.get("timestamp") is not None and r.get("fundingRate") is not None]
    if not rows:
        return pd.DataFrame()
    index = pd.DatetimeIndex(pd.to_datetime([int(r["timestamp"]) for r in rows], unit="ms"), name="ts")
    return pd.DataFrame({"rate_raw": [str(r["fundingRate"]) for r in rows], "direction_raw": None,
                         "rate": [float(r["fundingRate"]) for r in rows]}, index=index).sort_index()


def records_frame(records: List[Dict[str, Any]], time_field: str, rate_field: str, *,
                  unit: str = "ms") -> pd.DataFrame:
    """Raw funding records (values as served, text or number) -> ``rate_raw`` (text) and ``rate``."""
    rows = [r for r in records if r.get(time_field) not in (None, "") and r.get(rate_field) not in (None, "")]
    if not rows:
        return pd.DataFrame()
    index = pd.DatetimeIndex(pd.to_datetime([int(float(r[time_field])) for r in rows], unit=unit), name="ts")
    out = pd.DataFrame({"rate_raw": [str(r[rate_field]) for r in rows], "direction_raw": None,
                        "rate": [float(r[rate_field]) for r in rows]}, index=index)
    out = out[~out.index.duplicated(keep="last")]
    return out.sort_index()


class CcxtVenue:
    """A ccxt exchange as a ``Venue`` (perps only). Subclasses set the class attributes and override the hooks
    whose generic (unified) version is wrong for them."""

    name = ""
    exchange_id = ""
    capabilities: FrozenSet[Dataset] = PERP_CAPABILITIES
    resolutions: Tuple[str, ...] = ()
    max_rate: Optional[float] = 2.0
    market_types: Optional[Tuple[str, ...]] = None
    settle_assets = frozenset({"USDT", "USDC"})
    funding_window: Optional[pd.Timedelta] = None
    default_interval_h = 8
    max_interval_h = 8
    funding_page = 100
    book_limits: Tuple[int, ...] = ()
    oi_convention: Optional[str] = None  # generic path: not verified
    index_kind = "index"
    parse_funding: Callable[[List[Dict[str, Any]]], pd.DataFrame] = staticmethod(parse_unified_funding)

    def __init__(self, exchange: Any = None, *, rate: float = 2.0,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], pd.Timestamp] = lambda: pd.Timestamp.now(tz="UTC").tz_localize(None)) -> None:
        self._exchange = exchange
        self.limiter = RateLimiter(rate if self.max_rate is None else min(rate, self.max_rate), sleep=sleep)
        self._sleep = sleep
        self.clock = clock
        self._markets: Optional[Dict[str, Dict[str, Any]]] = None
        self._intervals: Optional[Dict[str, float]] = None

    # -- transport -----------------------------------------------------------------------------------

    @property
    def exchange(self) -> Any:
        if self._exchange is None:
            self._exchange = make_exchange(self.exchange_id, self.market_types)
        return self._exchange

    def call(self, method: Callable[..., Any], *args: Any) -> Any:
        """One paced request, retried with backoff on network errors and rate-limit refusals."""
        for attempt in range(1, ATTEMPTS + 1):
            self.limiter.wait()
            try:
                return method(*args)
            except Exception as exc:
                if attempt == ATTEMPTS or not _retryable(exc):
                    raise
                delay = BASE_DELAY_S * 2 ** (attempt - 1)
                logger.debug("%s: retrying in %.0fs after %s (%d/%d)", self.name, delay, type(exc).__name__,
                             attempt, ATTEMPTS)
                self._sleep(delay)
        raise AssertionError("unreachable")

    def raw(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """A venue endpoint through ccxt's implicit API (``endpoint`` = the method name), payload as served."""
        return self.call(getattr(self.exchange, endpoint), dict(params or {}))

    # -- markets -------------------------------------------------------------------------------------

    def markets(self) -> Dict[str, Dict[str, Any]]:
        """Linear perps settled in ``settle_assets``, by native id (cached for the venue's lifetime)."""
        if self._markets is None:
            loaded = self.call(self.exchange.load_markets)
            perps = {m["id"]: m for m in self.extra_markets()}
            perps.update({m["id"]: m for m in loaded.values() if self.is_perp(m)})
            self._markets = dict(sorted(perps.items()))
        return self._markets

    def is_perp(self, market: Dict[str, Any]) -> bool:
        return bool(market.get("swap") and market.get("linear") and market.get("settle") in self.settle_assets)

    def extra_markets(self) -> List[Dict[str, Any]]:
        """Perps ccxt does not load (e.g. delisted ones), as market-like dicts; listed markets win."""
        return []

    def ccxt_symbol(self, symbol: str) -> str:
        try:
            unified = self.markets()[symbol].get("symbol")
        except KeyError:
            raise ValueError(f"Unknown {self.name} symbol {symbol!r}") from None
        if not unified:
            raise ValueError(f"{self.name} {symbol} is not a ccxt market (delisted)")
        return unified

    def funding_intervals(self) -> Dict[str, float]:
        """Current funding interval in hours per native symbol (cached)."""
        if self._intervals is None:
            self._intervals = self.load_intervals()
        return self._intervals

    def load_intervals(self) -> Dict[str, float]:
        """Generic: ccxt's ``fetch_funding_rates`` ``interval`` ("8h"); absent intervals fall to the default."""
        by_unified = {m.get("symbol"): sid for sid, m in self.markets().items() if m.get("symbol")}
        rates = self.call(self.exchange.fetch_funding_rates, list(by_unified))
        out = {}
        for unified, rate in (rates or {}).items():
            interval = str(rate.get("interval") or "")
            if unified in by_unified and interval.endswith("h") and interval[:-1].isdigit():
                out[by_unified[unified]] = float(interval[:-1])
        return out

    def funding_interval(self, symbol: str) -> float:
        return float(self.funding_intervals().get(symbol, self.default_interval_h))

    # Per-market hooks of ``instruments()``: generic versions read ccxt's unified fields.

    def asset_class(self, market: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """``(asset_class, unresolved_reason)``: the class the venue declares (``None``: left to the registry,
        which defaults to crypto), or a reason to leave the market unresolved."""
        return None, None

    def underlying_ref(self, market: Dict[str, Any]) -> Optional[str]:
        return market.get("base")

    def listed_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        return stamp(market.get("created"))

    def delisted_at(self, market: Dict[str, Any]) -> pd.Timestamp:
        return pd.NaT

    def status(self, market: Dict[str, Any]) -> str:
        return "active" if market.get("active") else "inactive"

    def instruments(self) -> pd.DataFrame:
        require(self, Dataset.INSTRUMENTS)
        rows = []
        for symbol, m in self.markets().items():
            asset_class, reason = self.asset_class(m)
            rows.append({
                "venue": self.name,
                "venue_symbol": symbol,
                "venue_market_id": m.get("symbol") or symbol,
                "settle_asset": m.get("settle"),
                "asset_class": asset_class,
                "underlying_ref": self.underlying_ref(m) if asset_class not in (None, "crypto") else None,
                "chain": None,
                "contract_address": None,
                "funding_interval_h": self.funding_interval(symbol),
                "listed_at": self.listed_at(m),
                "delisted_at": self.delisted_at(m),
                "status": self.status(m),
                "unresolved_reason": reason,
            })
        return pd.DataFrame(rows).reindex(columns=SNAPSHOT_COLUMNS)

    def specs(self) -> pd.DataFrame:
        """One row per perp: native and ccxt symbols, contract size, then the venue's own market fields as
        text (JSON for nested values), so a day's snapshot never mixes types across rows."""
        require(self, Dataset.SPECS)
        rows = []
        for symbol, m in self.markets().items():
            row = {"symbol": symbol, "ccxt_symbol": m.get("symbol"), "contract_size": num(m.get("contractSize"))}
            for key, value in (m.get("info") or {}).items():
                if key not in row:
                    row[key] = value if value is None or isinstance(value, str) else json.dumps(value)
            rows.append(row)
        return pd.DataFrame(rows)

    # -- funding -------------------------------------------------------------------------------------

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Funding events in ``[start, end]``; one interval is read before ``start`` so that the first event's
        ``interval_h`` comes from the data, not the current interval."""
        require(self, Dataset.FUNDING)
        start, end = pd.Timestamp(start), pd.Timestamp(end)
        if self.funding_window is not None:
            floor = self.clock() - self.funding_window - WINDOW_MARGIN
            if end < floor:  # entirely before the window: nothing is served
                return finalize_funding(pd.DataFrame(), self.funding_interval(symbol))
            start = max(start, floor)
        lookback = start - pd.Timedelta(hours=self.max_interval_h)
        raw = self.funding_records(symbol, ms(lookback), ms(end))
        if not raw.empty:
            raw = raw.loc[lookback:end]
        events = finalize_funding(raw, self.funding_interval(symbol), self.max_interval_h)
        return events.loc[start:end]

    def funding_records(self, symbol: str, start_ms: int, end_ms: int) -> pd.DataFrame:
        """Raw events (``rate_raw``, ``direction_raw``, ``rate``) in ``[start_ms, end_ms]``. Generic: ccxt's
        documented ``since`` semantics (the oldest ``funding_page`` events from ``since``), paged forward."""
        unified = self.ccxt_symbol(symbol)

        def fetch_from(cursor: int) -> pd.DataFrame:
            page = self.parse_funding(self.call(self.exchange.fetch_funding_rate_history, unified, cursor,
                                                self.funding_page) or [])
            return page if page.empty else page.loc[: pd.Timestamp(end_ms, unit="ms")]

        return forward_fetch(fetch_from, start_ms, end_ms, self.funding_page)

    # -- live snapshots ------------------------------------------------------------------------------

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book from ccxt ``fetch_order_book`` (depth rounded up to a size the venue accepts)."""
        require(self, Dataset.BOOK)
        limit = next((n for n in self.book_limits if n >= depth), self.book_limits[-1]) if self.book_limits else depth
        book = self.call(self.exchange.fetch_order_book, self.ccxt_symbol(symbol), limit)
        ts = book.get("timestamp")
        return make_book([lv[:2] for lv in book.get("bids") or []], [lv[:2] for lv in book.get("asks") or []],
                         venue=self.name, symbol=symbol, depth=depth,
                         ts=pd.Timestamp(int(ts), unit="ms") if ts else None)

    def live_symbols(self, symbols: Optional[Sequence[str]]) -> List[str]:
        if symbols is not None:
            return list(dict.fromkeys(symbols))
        return [s for s, m in self.markets().items() if self.status(m) == "active"]

    def open_interest(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Generic: ccxt ``fetch_open_interest`` per symbol (contracts x contract size), valued at the mark of
        ``fetch_tickers``; ``oi_convention`` is the venue's declared one (``None`` until checked)."""
        require(self, Dataset.OPEN_INTEREST)
        wanted = self.live_symbols(symbols)
        tickers = self.call(self.exchange.fetch_tickers, [self.ccxt_symbol(s) for s in wanted
                                                         if s in self.markets()])
        records: Dict[str, Dict[str, Any]] = {}
        for symbol in wanted:
            try:
                market = self.markets()[symbol]
                oi = self.call(self.exchange.fetch_open_interest, self.ccxt_symbol(symbol))
            except Exception as exc:  # one failing symbol must not stop the others
                records[symbol] = {"error": f"{type(exc).__name__}: {exc}".splitlines()[0]}
                continue
            mark = (tickers.get(market["symbol"]) or {}).get("markPrice")
            records[symbol] = {"open_interest_base": num(oi.get("openInterestAmount")) * num(market.get("contractSize")),
                               "price": num(mark), "price_kind": "mark", "oi_convention": self.oi_convention,
                               "ts": stamp(oi.get("timestamp")) if oi.get("timestamp") else None}
        return make_open_interest(records, wanted)

    def prices(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Generic: ccxt ``fetch_tickers`` (``markPrice``, ``indexPrice``, ``last``), one request."""
        require(self, Dataset.PRICES)
        by_unified = {m["symbol"]: s for s, m in self.markets().items() if m.get("symbol")}
        tickers = self.call(self.exchange.fetch_tickers, list(by_unified))
        records = {by_unified[u]: {"mark_price": t.get("markPrice"), "index_price": t.get("indexPrice"),
                                   "last_price": t.get("last"), "index_kind": self.index_kind,
                                   "ts": stamp(t.get("timestamp")) if t.get("timestamp") else None}
                   for u, t in (tickers or {}).items() if u in by_unified}
        return make_prices(records, symbols)

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        require(self, Dataset.CANDLES if price == "last" else Dataset.MARK_CANDLES)
        raise AssertionError("unreachable")

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        require(self, Dataset.DAILY_METRICS)
        raise AssertionError("unreachable")


__all__ = ["CcxtVenue", "PERP_CAPABILITIES", "make_exchange", "ms", "num", "parse_unified_funding",
           "records_frame", "stamp"]
