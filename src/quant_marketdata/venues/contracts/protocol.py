"""``OptionsVenue`` and ``BinaryVenue``, next to the perp ``Venue`` protocol.

A venue implements one or more protocols with **one adapter**: Derive is ``Venue`` (perps) +
``OptionsVenue``, Deribit ``OptionsVenue``, Kalshi ``BinaryVenue`` only. Methods outside the declared
``capabilities`` raise ``UnsupportedDataset`` (``base.require``). ``order_book(symbol, depth)``
(``Dataset.BOOK``) also serves option legs and binary markets; a binary book is expressed in YES
probability (asks = 1 - the NO side's bids).
"""

from __future__ import annotations

from datetime import datetime
from typing import FrozenSet, Optional, Protocol, runtime_checkable

import pandas as pd

from quant_marketdata.venues.schema import Dataset


@runtime_checkable
class OptionsVenue(Protocol):
    name: str
    capabilities: FrozenSet[Dataset]

    def option_contracts(self, underlying: Optional[str] = None, *, expired: bool = False) -> pd.DataFrame:
        """Listed (or recently expired) option contracts, ``OPTION_SNAPSHOT_COLUMNS``; ``underlying`` is the
        venue's native base (``BTC``). Identity columns are filled by ``resolve_option_contracts``."""

    def option_chain(self, underlying: str, expiry: Optional[datetime] = None) -> pd.DataFrame:
        """Live chain snapshot, ``OPTION_QUOTE_COLUMNS`` (``contract_id`` empty; ``ts_snapshot`` set by the
        caller of a pass)."""

    def option_trades(self, underlying: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Public trades in ``[start, end]``, ``OPTION_TRADE_COLUMNS``; no trader identifier is kept."""

    def option_mark_candles(self, symbol: str, resolution: str, start: datetime, end: datetime,
                            *, price: str = "mark") -> pd.DataFrame:
        """Mark (or underlying) price candles of one contract, ``OPTION_MARK_CANDLE_COLUMNS``."""

    def settlements(self, index: str, start: Optional[datetime] = None,
                    end: Optional[datetime] = None) -> pd.DataFrame:
        """Fixings of a settlement index (venue-native name), ``SETTLEMENT_COLUMNS``."""


@runtime_checkable
class BinaryVenue(Protocol):
    name: str
    capabilities: FrozenSet[Dataset]

    def binary_contracts(self, series: str, *, status: str = "open", start: Optional[datetime] = None,
                         end: Optional[datetime] = None) -> pd.DataFrame:
        """Markets of a series, ``BINARY_SNAPSHOT_COLUMNS``; ``start``/``end`` bound the expiry."""

    def binary_quotes(self, series: str) -> pd.DataFrame:
        """Live quotes of the series' open markets, ``BINARY_QUOTE_COLUMNS``."""

    def binary_candles(self, event: str, period: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Candles of every market of an event, ``BINARY_CANDLE_COLUMNS``."""

    def binary_trades(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
        """Public trades of one market, ``BINARY_TRADE_COLUMNS``."""


__all__ = ["BinaryVenue", "OptionsVenue"]
