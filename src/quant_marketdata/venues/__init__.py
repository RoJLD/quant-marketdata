"""Venue-agnostic market-data layer: one ``Venue`` protocol, canonical schemas, an instrument registry and
an incremental Parquet store. Adapters register themselves on import: Lighter, Hyperliquid, Binance
(USD-M futures), Extended and Aster.

Read side::

    from quant_marketdata.venues import load_funding
    btc = load_funding("BTC-USD-PERP")   # every venue's events, with a ``venue`` column
"""

from __future__ import annotations

from typing import Optional, Sequence

import pandas as pd

from quant_marketdata.venues.aster import AsterVenue
from quant_marketdata.venues.base import Venue, available_venues, get_venue, register_venue
from quant_marketdata.venues.binance import BinanceVenue
from quant_marketdata.venues.download import DownloadPlan, DownloadResult, download_venue
from quant_marketdata.venues.extended import ExtendedVenue
from quant_marketdata.venues.http import HttpError, JsonClient, RateLimiter
from quant_marketdata.venues.hyperliquid import HyperliquidVenue
from quant_marketdata.venues.lighter import LighterVenue
from quant_marketdata.venues.registry import RegistryBuild, resolve, update_registry
from quant_marketdata.venues.schema import (
    ASSET_CLASSES,
    Dataset,
    UnsupportedDataset,
    finalize_funding,
    funding_grid,
)
from quant_marketdata.venues.store import ROOT_ENV, VenueStore


def load_registry(root=None) -> pd.DataFrame:
    store = VenueStore.at(root)
    return store.read(store.registry_path())


def load_series(venue: str, dataset: Dataset, symbol: str, resolution: Optional[str] = None,
                root=None) -> pd.DataFrame:
    """One stored series, by native symbol."""
    store = VenueStore.at(root)
    return store.read(store.path(venue, dataset, symbol, resolution))


def load_funding(instrument_id: str, venues: Optional[Sequence[str]] = None, root=None) -> pd.DataFrame:
    """Funding events of one canonical instrument across venues, long format with a ``venue`` column."""
    registry = load_registry(root)
    if registry.empty:
        raise FileNotFoundError("No registry in the store: run a download first")
    rows = registry[registry["instrument_id"] == instrument_id]
    if venues is not None:
        rows = rows[rows["venue"].isin(venues)]
    frames = []
    for venue, symbol in rows[["venue", "venue_symbol"]].itertuples(index=False):
        frame = load_series(venue, Dataset.FUNDING, symbol, root=root)
        if not frame.empty:
            frames.append(frame.assign(venue=venue, venue_symbol=symbol))
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames).sort_index()


__all__ = [
    "ASSET_CLASSES",
    "AsterVenue",
    "BinanceVenue",
    "Dataset",
    "DownloadPlan",
    "DownloadResult",
    "ExtendedVenue",
    "HttpError",
    "HyperliquidVenue",
    "JsonClient",
    "LighterVenue",
    "ROOT_ENV",
    "RateLimiter",
    "RegistryBuild",
    "UnsupportedDataset",
    "Venue",
    "VenueStore",
    "available_venues",
    "download_venue",
    "finalize_funding",
    "funding_grid",
    "get_venue",
    "load_funding",
    "load_registry",
    "load_series",
    "register_venue",
    "resolve",
    "update_registry",
]
