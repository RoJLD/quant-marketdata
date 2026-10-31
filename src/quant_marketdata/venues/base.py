"""The ``Venue`` protocol and the lookup of available adapters."""

from __future__ import annotations

from datetime import datetime
from typing import Callable, Dict, FrozenSet, Optional, Protocol, Sequence, Tuple, runtime_checkable

import pandas as pd

from quant_marketdata.venues.schema import Dataset, UnsupportedDataset


@runtime_checkable
class Venue(Protocol):
    """A source of historical market data. Methods take the venue's native symbol.

    A method for a dataset outside ``capabilities`` raises ``UnsupportedDataset``.
    """

    name: str
    capabilities: FrozenSet[Dataset]
    resolutions: Tuple[str, ...]

    def instruments(self) -> pd.DataFrame:
        """Current instruments, ``INSTRUMENT_COLUMNS`` minus ``instrument_id`` (set by the registry)."""

    def funding(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Funding events in ``[start, end]``, ``FUNDING_COLUMNS``, indexed by settlement time."""

    def candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                *, price: str = "last") -> pd.DataFrame:
        """Candles opening in ``[start, end]``, ``CANDLE_COLUMNS`` (+ native extras), indexed by open time."""

    def daily_metrics(self, symbol: str) -> pd.DataFrame:
        """Daily open interest and volume since listing, ``DAILY_METRICS_COLUMNS``."""

    def order_book(self, symbol: str, depth: int = 50) -> dict:
        """Live book ``{"bids": [(price, size)], "asks": [...], "ts", "venue", "symbol"}``, best first
        (``schema.make_book``), directly usable by ``paper_execution`` / ``market_impact``."""

    def open_interest(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live open interest, ``OPEN_INTEREST_COLUMNS`` (``schema.make_open_interest``), one row per symbol
        (default: every perp; per-symbol venues then make one request per symbol), with the venue's
        ``oi_convention``."""

    def prices(self, symbols: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """Live mark / index / last prices, ``PRICES_COLUMNS`` (``schema.make_prices``), one row per symbol
        (default: every perp), one request per venue."""

    def specs(self) -> pd.DataFrame:
        """Current specs of every instrument (fees, margins, sizes, status), one row per instrument."""


# A venue serving its funding over a rolling window declares ``funding_window`` (OKX, Bitget): a floor, the venue
# may serve a little more. Nothing is asked before ``now - funding_window - WINDOW_MARGIN``.
WINDOW_MARGIN = pd.Timedelta(days=7)


def require(venue: Venue, dataset: Dataset) -> None:
    if dataset not in venue.capabilities:
        raise UnsupportedDataset(f"{venue.name} does not provide {dataset.value}")


_FACTORIES: Dict[str, Callable[..., Venue]] = {}


def register_venue(name: str, factory: Callable[..., Venue]) -> None:
    _FACTORIES[name] = factory


def get_venue(name: str, **kwargs) -> Venue:
    try:
        factory = _FACTORIES[name]
    except KeyError:
        raise ValueError(f"Unknown venue {name!r}; available: {', '.join(available_venues())}") from None
    return factory(**kwargs)


def available_venues() -> list[str]:
    return sorted(_FACTORIES)


__all__ = ["Venue", "WINDOW_MARGIN", "available_venues", "get_venue", "register_venue", "require"]
