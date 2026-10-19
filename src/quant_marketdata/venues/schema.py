"""Canonical schemas shared by every venue adapter.

Timestamps are naive UTC ``datetime64[ns]``, like the rest of the platform. Every table keeps the venue's
native values (``*_raw`` columns) next to the canonical ones, so a normalization bug can be fixed without
re-downloading.

Funding convention: ``rate`` is the signed fraction paid over the funding interval, **positive when longs
pay shorts**; ``rate_1h`` divides it by the interval in hours and is the column to compare across venues.

Open interest (``Dataset.OPEN_INTEREST``, a live snapshot): venues publish it in base units or USD, counting ONE
side (longs = shorts, the usual convention) or BOTH sides (longs + shorts). Each snapshot row carries its
``oi_convention``; compare venues on ``one_side_usd``.

Trading session (``session`` instrument column), as the venue declares it: ``"24x7"`` (continuous),
``"24x5"`` (round the clock on weekdays, closed at weekends), ``"no-overnight"`` (closed during the US
overnight session), or ``None`` when the venue does not declare one. A restricted session is part of the
instrument's identity (see the registry).
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
    OPEN_INTEREST = "open_interest"  # live open-interest snapshot (not stored: consumers journal it)
    # Dated contracts (options and binaries): ``quant_marketdata.venues.contracts``.
    OPTION_CONTRACTS = "option_contracts"
    OPTION_CHAIN = "option_chain"
    OPTION_TRADES = "option_trades"
    OPTION_MARK_CANDLES = "option_mark_candles"
    BINARY_CONTRACTS = "binary_contracts"
    BINARY_QUOTES = "binary_quotes"
    BINARY_CANDLES = "binary_candles"
    BINARY_TRADES = "binary_trades"
    SETTLEMENTS = "settlements"


PERP_DATASETS = frozenset({Dataset.INSTRUMENTS, Dataset.FUNDING, Dataset.CANDLES, Dataset.MARK_CANDLES,
                           Dataset.DAILY_METRICS, Dataset.SPECS, Dataset.BOOK, Dataset.OPEN_INTEREST})
CONTRACT_DATASETS = frozenset(Dataset) - PERP_DATASETS

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
    "session",
]

SESSIONS = frozenset({"24x7", "24x5", "no-overnight"})

FUNDING_COLUMNS = ["rate_raw", "direction_raw", "interval_h", "rate", "rate_1h", "apr"]

CANDLE_COLUMNS = ["open", "high", "low", "close", "volume_base", "volume_quote", "price_type"]

DAILY_METRICS_COLUMNS = ["open_interest_usd", "volume_usd", "oi_convention"]

OPEN_INTEREST_COLUMNS = ["symbol", "open_interest_base", "price", "open_interest_usd", "price_kind",
                         "oi_convention", "ts", "error"]
OI_CONVENTIONS = frozenset({"one_side", "both_sides"})


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


def make_open_interest(records: dict, symbols=None, *, ts=None) -> pd.DataFrame:
    """Live open-interest snapshot, ``OPEN_INTEREST_COLUMNS``, one row per symbol.

    ``records``: {native symbol: {"open_interest_base", "price", "price_kind", "oi_convention"} plus optionally
    "open_interest_usd" (when the venue publishes the USD value itself), "ts" and "error"}. Without a published
    USD value, ``open_interest_usd`` = base x price. ``symbols`` restricts the rows (default: every record); a
    requested symbol without a record is a row with NaN values and ``error = "unknown symbol"``.
    """
    stamp = pd.Timestamp.now(tz="UTC").tz_localize(None) if ts is None else pd.Timestamp(ts)
    rows = []
    for symbol in (sorted(records) if symbols is None else list(dict.fromkeys(symbols))):
        record = records.get(symbol)
        if record is None:
            record = {"error": "unknown symbol"}
        convention = record.get("oi_convention")
        if convention is not None and convention not in OI_CONVENTIONS:
            raise ValueError(f"unknown open-interest convention {convention!r}")
        base = float(record.get("open_interest_base", float("nan")))
        price = float(record.get("price", float("nan")))
        usd = record.get("open_interest_usd")
        rows.append({"symbol": symbol, "open_interest_base": base, "price": price,
                     "open_interest_usd": float(usd) if usd is not None else base * price,
                     "price_kind": record.get("price_kind"), "oi_convention": convention,
                     "ts": pd.Timestamp(record["ts"]) if record.get("ts") is not None else stamp,
                     "error": record.get("error")})
    return pd.DataFrame(rows, columns=OPEN_INTEREST_COLUMNS)


def one_side_usd(snapshot: pd.DataFrame) -> pd.Series:
    """Open interest in USD counting one side, whatever the venue's convention (both sides -> / 2)."""
    both = snapshot["oi_convention"] == "both_sides"
    return snapshot["open_interest_usd"].where(~both, snapshot["open_interest_usd"] / 2)


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
    "CONTRACT_DATASETS",
    "DAILY_METRICS_COLUMNS",
    "Dataset",
    "FUNDING_COLUMNS",
    "HOURS_PER_YEAR",
    "INSTRUMENT_COLUMNS",
    "OI_CONVENTIONS",
    "OPEN_INTEREST_COLUMNS",
    "PERP_DATASETS",
    "SESSIONS",
    "UnsupportedDataset",
    "empty_frame",
    "finalize_funding",
    "funding_grid",
    "make_book",
    "make_open_interest",
    "one_side_usd",
]
