"""Aster (asterdex.com perp DEX) adapter: a Binance-compatible ``fapi``, served by the Binance adapter.

Differences from Binance (verified 2026-10-15):

- Host ``fapi.asterdex.com``; same endpoints and payload shapes (``exchangeInfo``, ``fundingInfo``,
  ``fundingRate``, ``klines``, ``markPriceKlines``); ``fundingRate`` carries no ``markPrice``.
- ``fundingInfo`` lists **every** symbol (delisted ones included) with intervals of 1, 2, 4 or 8 hours.
- ``exchangeInfo`` says ``underlyingType=COIN`` for everything, stock and commodity perps included; the
  real class is in ``underlyingSubType`` (``STOCK``, ``ETF``, ``Commodities``). Markets announced but not
  yet trading have an empty ``contractType`` and are dropped.
- Books are USDT (primary) and USD1 (``BTCUSD1``, mostly the RWA perps).
- Funding history goes back to 2021 for BTCUSDT (the venue's ApolloX lineage).
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from quant_marketdata.venues.base import register_venue
from quant_marketdata.venues.binance import BinanceVenue

BASE_URL = "https://fapi.asterdex.com/fapi/v1"
# Most specific first: an ETF is tagged ["STOCK", "ETF"].
SUBTYPE_CLASSES = (("ETF", "etf"), ("STOCK", "equity"), ("Commodities", "commodity"))


class AsterVenue(BinanceVenue):
    name = "aster"
    base_url = BASE_URL
    max_rate: Optional[float] = None
    perp_types = frozenset({"PERPETUAL"})

    def asset_class(self, market: Dict[str, Any]) -> Optional[str]:
        subtypes = set(market.get("underlyingSubType") or [])
        for subtype, asset_class in SUBTYPE_CLASSES:
            if subtype in subtypes:
                return asset_class
        return None  # untagged: the registry defaults it to crypto, flagged as a default


register_venue(AsterVenue.name, AsterVenue)

__all__ = ["AsterVenue"]
