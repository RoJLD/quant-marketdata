"""Dataset.BOOK: live order book snapshots per venue, in the friction functions' format (no network)."""

from __future__ import annotations

import pandas as pd
import pytest

from quant_marketdata import leg_roundtrip_bps_impact
from quant_marketdata.venues import Dataset, UnsupportedDataset
from quant_marketdata.venues.aster import AsterVenue
from quant_marketdata.venues.binance import BinanceVenue
from quant_marketdata.venues.extended import ExtendedVenue
from quant_marketdata.venues.hyperliquid import HyperliquidVenue
from quant_marketdata.venues.lighter import LighterVenue
from quant_marketdata.venues.schema import make_book


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


def test_make_book_aggregates_sorts_and_truncates():
    book = make_book([("99", "1"), ("100", "2"), ("100", "0.5"), ("98", "0")], [("102", "1"), ("101", "3")],
                     venue="v", symbol="X", ts=0, depth=2)
    assert book["bids"] == [(100.0, 2.5), (99.0, 1.0)]  # aggregated, best first, zero dropped
    assert book["asks"] == [(101.0, 3.0), (102.0, 1.0)]
    assert book["ts"] == pd.Timestamp(0) and (book["venue"], book["symbol"]) == ("v", "X")


def test_lighter_aggregates_orders():
    client = FakeClient({
        "orderBookDetails": {"code": 200, "order_book_details": [{"symbol": "BTC", "market_id": 1}]},
        "orderBookOrders": {"code": 200,
                            "asks": [{"price": "101", "remaining_base_amount": "1"},
                                     {"price": "101", "remaining_base_amount": "0.5"}],
                            "bids": [{"price": "99", "remaining_base_amount": "2"}]},
    })
    book = LighterVenue(client).order_book("BTC", depth=10)
    assert book["asks"] == [(101.0, 1.5)] and book["bids"] == [(99.0, 2.0)]
    assert ("orderBookOrders", {"market_id": 1, "limit": 250}) in client.calls


def test_hyperliquid_l2book():
    client = FakeClient({("info", "l2Book"): {
        "coin": "BTC", "time": 1792249864182,
        "levels": [[{"px": "100", "sz": "1", "n": 2}, {"px": "99", "sz": "2", "n": 1}],
                   [{"px": "101", "sz": "3", "n": 1}]]}})
    book = HyperliquidVenue(client).order_book("BTC")
    assert book["bids"] == [(100.0, 1.0), (99.0, 2.0)] and book["asks"] == [(101.0, 3.0)]
    assert book["ts"] == pd.Timestamp(1792249864182, unit="ms")


@pytest.mark.parametrize("venue_cls", [BinanceVenue, AsterVenue])
def test_binance_compatible_depth_limit_and_timestamp(venue_cls):
    client = FakeClient({"depth": {"T": 1792249864921, "bids": [["100.0", "1"]], "asks": [["100.1", "2"]]}})
    book = venue_cls(client=client).order_book("BTCUSDT", depth=30)
    assert client.calls[-1] == ("depth", {"symbol": "BTCUSDT", "limit": 50})  # smallest allowed >= 30
    assert book["bids"] == [(100.0, 1.0)] and book["asks"] == [(100.1, 2.0)]
    assert book["ts"] == pd.Timestamp(1792249864921, unit="ms")


def test_extended_orderbook():
    client = FakeClient({"info/markets/BTC-USD/orderbook": {"status": "OK", "data": {
        "market": "BTC-USD", "bid": [{"qty": "1", "price": "100"}], "ask": [{"qty": "2", "price": "101"}]}}})
    book = ExtendedVenue(client).order_book("BTC-USD")
    assert book["bids"] == [(100.0, 1.0)] and book["asks"] == [(101.0, 2.0)]


def test_book_feeds_the_friction_functions():
    book = make_book([(100.0, 10.0), (99.0, 10.0)], [(101.0, 10.0), (102.0, 10.0)], venue="v", symbol="X")
    rt = leg_roundtrip_bps_impact(book, 500.0, taker_fee_bps=0.0)
    assert rt["roundtrip_bps"] > 0


def test_every_adapter_declares_book():
    for cls in (LighterVenue, HyperliquidVenue, BinanceVenue, AsterVenue, ExtendedVenue):
        assert Dataset.BOOK in cls.capabilities
    venue = LighterVenue(FakeClient({}))
    venue.capabilities = frozenset({Dataset.FUNDING})
    with pytest.raises(UnsupportedDataset):
        venue.order_book("BTC")


@pytest.mark.live
@pytest.mark.parametrize("name,symbol", [("lighter", "BTC"), ("hyperliquid", "BTC"), ("binance", "BTCUSDT"),
                                         ("aster", "BTCUSDT"), ("extended", "BTC-USD")])
def test_live_order_books(name, symbol):
    from quant_marketdata.venues import get_venue

    book = get_venue(name).order_book(symbol, depth=10)
    assert len(book["bids"]) >= 5 and len(book["asks"]) >= 5
    assert book["bids"][0][0] < book["asks"][0][0]
