"""Dataset.PRICES: live mark / index / last snapshots per venue, one request each (no network).

Payloads trimmed from the venues' public APIs on 2026-10-19.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from quant_marketdata.venues import Dataset, UnsupportedDataset
from quant_marketdata.venues.aster import AsterVenue
from quant_marketdata.venues.binance import BinanceVenue
from quant_marketdata.venues.extended import ExtendedVenue
from quant_marketdata.venues.hyperliquid import HyperliquidVenue
from quant_marketdata.venues.lighter import LighterVenue
from quant_marketdata.venues.paradex import ParadexVenue
from quant_marketdata.venues.schema import PRICES_COLUMNS, make_prices


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        return self.routes[path]

    def post(self, path, payload):
        self.calls.append((path, dict(payload)))
        return self.routes[(path, payload["type"])]


def test_make_prices_columns_and_unknown_symbols():
    frame = make_prices({"A": {"mark_price": "10.5", "index_price": 10.4, "last_price": "", "index_kind": "index"}},
                        ["A", "B"], ts=pd.Timestamp("2026-10-19"))
    assert list(frame.columns) == PRICES_COLUMNS
    a, b = frame.iloc[0], frame.iloc[1]
    assert (a["mark_price"], a["index_price"]) == (10.5, 10.4) and math.isnan(a["last_price"])
    assert b["error"] == "unknown symbol" and math.isnan(b["mark_price"])


def test_lighter_order_book_details():
    client = FakeClient({"orderBookDetails": {"code": 200, "order_book_details": [
        {"symbol": "BTC", "market_id": 1, "mark_price": 82590.1, "index_price": 82601.0, "last_trade_price": 82596.8}]}})
    row = LighterVenue(client).prices().iloc[0]
    assert (row["mark_price"], row["index_price"], row["last_price"]) == (82590.1, 82601.0, 82596.8)
    assert row["index_kind"] == "index"


def test_hyperliquid_oracle_and_mid():
    meta = {"universe": [{"name": "BTC"}]}
    ctxs = [{"markPx": "82587.0", "oraclePx": "82621.0", "midPx": "82582.5", "openInterest": "1"}]
    row = HyperliquidVenue(FakeClient({("info", "metaAndAssetCtxs"): [meta, ctxs]})).prices().iloc[0]
    assert (row["mark_price"], row["index_price"], row["last_price"]) == (82587.0, 82621.0, 82582.5)
    assert row["index_kind"] == "oracle"


@pytest.mark.parametrize("venue_cls", [BinanceVenue, AsterVenue])
def test_binance_compatible_premium_index_in_one_request(venue_cls):
    client = FakeClient({"premiumIndex": [
        {"symbol": "BTCUSDT", "markPrice": "82580.10", "indexPrice": "82590.00", "time": 1792397178000},
        {"symbol": "ETHUSDT", "markPrice": "2426.1", "indexPrice": "2426.5", "time": 1792397178000}]})
    frame = venue_cls(client=client).prices(["BTCUSDT"])
    assert frame["symbol"].tolist() == ["BTCUSDT"] and frame["index_price"].iloc[0] == 82590.0
    assert math.isnan(frame["last_price"].iloc[0]) and frame["ts"].iloc[0] == pd.Timestamp(1792397178000, unit="ms")
    assert client.calls == [("premiumIndex", {})]


def test_extended_market_stats():
    client = FakeClient({"info/markets": {"status": "OK", "data": [
        {"name": "BTC-USD", "type": "PERPETUAL", "marketStats": {
            "markPrice": "82493.43", "indexPrice": "82528.17", "lastPrice": "82508"}}]}})
    row = ExtendedVenue(client).prices().iloc[0]
    assert (row["mark_price"], row["index_price"], row["last_price"]) == (82493.43, 82528.17, 82508.0)


def test_paradex_underlying_perps_only():
    client = FakeClient({"markets/summary": {"results": [
        {"symbol": "BTC-USD-PERP", "mark_price": "82576.9", "underlying_price": "82535.2", "last_traded_price": "82589.5",
         "created_at": 1792397176002},
        {"symbol": "BTC-USD-26DEC26-100000-C", "mark_price": "1200", "underlying_price": "82535.2"}]}})
    frame = ParadexVenue(client).prices()
    assert frame["symbol"].tolist() == ["BTC-USD-PERP"] and frame["index_kind"].iloc[0] == "underlying"
    assert frame["index_price"].iloc[0] == pytest.approx(82535.2)


def test_every_perp_adapter_declares_prices():
    for cls in (LighterVenue, HyperliquidVenue, BinanceVenue, AsterVenue, ExtendedVenue, ParadexVenue):
        assert Dataset.PRICES in cls.capabilities
    venue = ExtendedVenue(FakeClient({}))
    venue.capabilities = frozenset({Dataset.FUNDING})
    with pytest.raises(UnsupportedDataset):
        venue.prices()


@pytest.mark.live
@pytest.mark.parametrize("name,symbol", [("lighter", "BTC"), ("hyperliquid", "BTC"), ("binance", "BTCUSDT"),
                                         ("aster", "BTCUSDT"), ("extended", "BTC-USD"), ("paradex", "BTC-USD-PERP")])
def test_live_prices(name, symbol):
    from quant_marketdata.venues import get_venue

    row = get_venue(name).prices([symbol]).iloc[0]
    assert row["mark_price"] > 1000 and row["index_price"] > 1000
    assert abs(row["mark_price"] / row["index_price"] - 1) < 0.01  # BTC: mark within 1 % of the index
