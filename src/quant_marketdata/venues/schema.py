"""Canonical schemas shared by every venue adapter.

Timestamps are naive UTC ``datetime64[ns]``, like the rest of the platform. Every table keeps the venue's
native values (``*_raw`` columns) next to the canonical ones, so a normalization bug can be fixed without
re-downloading.

Funding convention: ``rate`` is the signed fraction paid over the funding interval, **positive when longs
pay shorts**; ``rate_1h`` divides it by the interval in hours and is the column to compare across venues.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import pandas as pd

HOURS_PER_YEAR = 24 * 365


class Dataset(str, Enum):
    INSTRUMENTS = "instruments"
    FUNDING = "funding"
    CANDLES = "candles"
    MARK_CANDLES = "mark_candles"
    DAILY_METRICS = "daily_metrics"
    SPECS = "specs"
    BOOK = "book"  # live order book snapshot (not stored: the stream recorder keeps the history)


ASSET_CLASSES = frozenset({"crypto", "equity", "etf", "index", "rates", "commodity", "fx", "private"})


class UnsupportedDataset(NotImplementedError):
    """Raised when a venue is asked for a dataset outside its declared capabilities."""


INSTRUMENT_COLUMNS = [
    "venue",
    "venue_symbol",
    "venue_market_id",
    "instrument_id",
    "base",
    "quote",
    "settle_asset",
    "multiplier",
    "asset_class",
    "underlying_ref",
    "chain",
    "contract_address",
    "funding_interval_h",
    "listed_at",
    "delisted_at",
    "status",
]

FUNDING_COLUMNS = ["rate_raw", "direction_raw", "interval_h", "rate", "rate_1h", "apr"]

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume_base", "volume_quote", "price_type"]

DAILY_METRICS_COLUMNS = ["open_interest_usd", "volume_usd", "oi_convention"]


def make_book(bids, asks, *, venue: str, symbol: str, ts=None, depth: int | None = None) -> dict:
    """Order book snapshot in the format of the friction functions (``paper_execution``,
    ``market_impact``): ``{"bids": [(price, size), ...], "asks": [...]}`` best price first, plus ``ts``,
    ``venue`` and ``symbol``. Individual orders at one price are aggregated; empty levels are dropped."""

    def side(levels, best_high):
        agg: dict = {}
        for price, size in levels:
            price, size = float(price), float(size)
            if size > 0:
                agg[price] = agg.get(price, 0.0) + size
        return sorted(agg.items(), reverse=best_high)[:depth]

    stamp = pd.Timestamp.now(tz="UTC").tz_localize(None) if ts is None else pd.Timestamp(ts)
    return {"bids": side(bids, True), "asks": side(asks, False), "ts": stamp, "venue": venue, "symbol": symbol}


def empty_frame(columns: list[str], index_name: str = "ts") -> pd.DataFrame:
    frame = pd.DataFrame(columns=columns)
    frame.index = pd.DatetimeIndex([], name=index_name)
    return frame


def finalize_funding(frame: pd.DataFrame, nominal_interval_h: float,
                     max_interval_h: Optional[float] = None) -> pd.DataFrame:
    """Derive ``interval_h``, ``rate_1h`` and ``apr`` from a frame holding a signed ``rate`` per event.

    ``interval_h`` is the gap to the previous settlement, capped at ``max_interval_h`` (default: the
    nominal interval): a missing event must not inflate the hourly rate of the next one. The first event
    gets the nominal interval. Venues whose per-symbol interval changes over time (Binance moves symbols
    between 8h, 4h and 1h) pass their longest interval as ``max_interval_h`` and the current one as nominal.
    """
    if frame.empty:
        return empty_frame(FUNDING_COLUMNS)
    cap = nominal_interval_h if max_interval_h is None else max(max_interval_h, nominal_interval_h)
    frame = frame.sort_index()
    frame = frame[~frame.index.duplicated(keep="last")]
    # Venues settle on whole hours; rounding absorbs settlements stamped a few seconds late.
    gaps_h = (frame.index.to_series().diff().dt.total_seconds() / 3600).round()
    interval = gaps_h.clip(lower=1, upper=cap).fillna(nominal_interval_h)
    out = frame.copy()
    out["interval_h"] = interval.to_numpy()
    out["rate_1h"] = out["rate"] / out["interval_h"]
    out["apr"] = out["rate_1h"] * HOURS_PER_YEAR
    out.index.name = "ts"
    return out[FUNDING_COLUMNS]


def funding_grid(events: pd.DataFrame, mode: str) -> pd.Series:
    """Hourly funding series from settlement events.

    - ``"settlement"``: the cash flow, i.e. ``rate`` at the settlement hour and 0 elsewhere (PnL backtests).
    - ``"accrual"``: ``rate_1h`` spread over every hour of the interval ending at the settlement (level
      comparison across venues with different intervals).
    """
    if events.empty:
        return pd.Series(dtype="float64", name=f"funding_{mode}")
    hours = events.index.floor("h")
    grid = pd.date_range(hours.min() - pd.Timedelta(hours=events["interval_h"].max()), hours.max(), freq="h")
    if mode == "settlement":
        series = pd.Series(0.0, index=grid)
        series.loc[hours] = events["rate"].to_numpy()
        return series.loc[hours.min():].rename("funding_settlement")
    if mode == "accrual":
        series = pd.Series(float("nan"), index=grid)
        for settled, row in zip(hours, events.itertuples(), strict=True):
            start = settled - pd.Timedelta(hours=row.interval_h) + pd.Timedelta(hours=1)
            series.loc[start:settled] = row.rate_1h
        return series.dropna().rename("funding_accrual")
    raise ValueError(f"mode must be 'settlement' or 'accrual', got {mode!r}")


__all__ = [
    "CANDLE_COLUMNS",
    "DAILY_METRICS_COLUMNS",
    "Dataset",
    "FUNDING_COLUMNS",
    "HOURS_PER_YEAR",
    "INSTRUMENT_COLUMNS",
    "UnsupportedDataset",
    "empty_frame",
    "finalize_funding",
    "funding_grid",
    "make_book",
]
