"""Dataset.OPEN_INTEREST: live open-interest snapshots per venue, with each venue's convention (no network).

Payloads trimmed from the venues' public APIs on 2026-10-19.
"""

from __future__ import annotations

import math

import pandas as pd
import pytest

from quant_marketdata.venues import Dataset, UnsupportedDataset, one_side_usd
from quant_marketdata.venues.aster import AsterVenue
from quant_marketdata.venues.binance import BinanceVenue
from quant_marketdata.venues.extended import ExtendedVenue
from quant_marketdata.venues.http import HttpError
from quant_marketdata.venues.hyperliquid import HyperliquidVenue
from quant_marketdata.venues.lighter import LighterVenue
from quant_marketdata.venues.paradex import ParadexVenue
from quant_marketdata.venues.schema import OPEN_INTEREST_COLUMNS, make_open_interest


class FakeClient:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        route = self.routes[path]
        answer = route(dict(params or {})) if callable(route) else route
        if isinstance(answer, Exception):
            raise answer
        return answer

    def post(self, path, payload):
        self.calls.append((path, dict(payload)))
        return self.routes[(path, payload["type"])]


def test_make_open_interest_values_conventions_and_unknown_symbols():
    frame = make_open_interest({"A": {"open_interest_base": 2.0, "price": 10.0, "price_kind": "mark",
                                      "oi_convention": "one_side"},
                                "B": {"open_interest_base": 4.0, "price": 10.0, "open_interest_usd": 41.0,
                                      "price_kind": "mark", "oi_convention": "both_sides"}},
                               ["B", "A", "C"], ts=pd.Timestamp("2026-10-19"))
    assert list(frame.columns) == OPEN_INTEREST_COLUMNS and frame["symbol"].tolist() == ["B", "A", "C"]
    assert frame["open_interest_usd"].tolist()[:2] == [41.0, 20.0]  # published USD wins, else base x price
    assert one_side_usd(frame).tolist()[:2] == [20.5, 20.0]  # both sides halved
    unknown = frame.iloc[2]
    assert unknown["error"] == "unknown symbol" and math.isnan(unknown["open_interest_usd"])
    with pytest.raises(ValueError, match="convention"):
        make_open_interest({"A": {"oi_convention": "longs"}})


def test_lighter_one_side_at_mark():
    client = FakeClient({"orderBookDetails": {"code": 200, "order_book_details": [
        {"symbol": "BTC", "market_id": 1, "open_interest": 2080.36809, "mark_price": 82590.1,
         "last_trade_price": 82596.8},
        {"symbol": "NEW", "market_id": 2, "open_interest": 0, "last_trade_price": 1.5}]}})
    frame = LighterVenue(client).open_interest().set_index("symbol")
    assert frame.loc["BTC", "open_interest_usd"] == pytest.approx(2080.36809 * 82590.1)
    assert frame.loc["BTC", "oi_convention"] == "one_side" and frame.loc["BTC", "price_kind"] == "mark"
    assert frame.loc["NEW", "price_kind"] == "last" and frame.loc["NEW", "open_interest_usd"] == 0.0
    assert len([c for c in client.calls if c[0] == "orderBookDetails"]) == 1  # one request for every market


def test_hyperliquid_asset_contexts():
    meta = {"universe": [{"name": "BTC", "szDecimals": 5}, {"name": "kPEPE", "szDecimals": 0}]}
    ctxs = [{"openInterest": "39636.16306", "markPx": "82587.0", "funding": "0.0000125"},
            {"openInterest": "1000", "markPx": "0.012", "funding": "0"}]
    frame = HyperliquidVenue(FakeClient({("info", "metaAndAssetCtxs"): [meta, ctxs]})).open_interest(["BTC"])
    assert frame["symbol"].tolist() == ["BTC"]
    assert frame["open_interest_usd"].iloc[0] == pytest.approx(39636.16306 * 82587.0)
    assert frame["oi_convention"].iloc[0] == "one_side"


@pytest.mark.parametrize("venue_cls", [BinanceVenue, AsterVenue])
def test_binance_compatible_one_request_per_symbol(venue_cls):
    def open_interest(params):
        if params["symbol"] == "DEADUSDT":
            return HttpError(400, "openInterest", '{"code":-1121,"msg":"Invalid symbol."}')
        return {"symbol": params["symbol"], "openInterest": "94930.871", "time": 1792397173383}

    client = FakeClient({
        "premiumIndex": [{"symbol": "BTCUSDT", "markPrice": "82580.10"}, {"symbol": "ETHUSDT", "markPrice": "2426"}],
        "openInterest": open_interest,
        "exchangeInfo": {"symbols": [
            {"symbol": "BTCUSDT", "contractType": "PERPETUAL", "status": "TRADING"},
            {"symbol": "OLDUSDT", "contractType": "PERPETUAL", "status": "SETTLING"},
            {"symbol": "BTCUSDT_261225", "contractType": "CURRENT_QUARTER", "status": "TRADING"}]},
    })
    frame = venue_cls(client=client).open_interest(["BTCUSDT", "DEADUSDT"]).set_index("symbol")
    assert frame.loc["BTCUSDT", "open_interest_usd"] == pytest.approx(94930.871 * 82580.10)
    assert frame.loc["BTCUSDT", "ts"] == pd.Timestamp(1792397173383, unit="ms")
    assert frame.loc["DEADUSDT", "error"].startswith("HttpError") and math.isnan(frame.loc["DEADUSDT", "price"])
    assert [p["symbol"] for path, p in client.calls if path == "openInterest"] == ["BTCUSDT", "DEADUSDT"]
    client.calls.clear()
    assert venue_cls(client=client).open_interest()["symbol"].tolist() == ["BTCUSDT"]  # trading perps only


def test_extended_market_stats_usd_as_published():
    client = FakeClient({"info/markets": {"status": "OK", "data": [
        {"name": "BTC-USD", "type": "PERPETUAL", "marketStats": {
            "openInterest": "45595290.397275", "openInterestBase": "551.95720", "markPrice": "82493.43"}},
        {"name": "BTCSPOT-USD", "type": "SPOT", "marketStats": {}}]}})
    frame = ExtendedVenue(client).open_interest()
    assert frame["symbol"].tolist() == ["BTC-USD"]  # spot markets are not perps
    assert frame["open_interest_usd"].iloc[0] == pytest.approx(45595290.397275)
    assert frame["open_interest_base"].iloc[0] == pytest.approx(551.9572)


def test_paradex_counts_both_sides():
    client = FakeClient({"markets/summary": {"results": [
        {"symbol": "BTC-USD-PERP", "mark_price": "82576.91877094", "open_interest": "80.5905",
         "open_interest_long": "40.29525", "open_interest_short": "40.29525", "created_at": 1792397176002},
        {"symbol": "BTC-USD-26DEC26-100000-C", "mark_price": "1200", "open_interest": "3"}]}})
    frame = ParadexVenue(client).open_interest()
    assert frame["symbol"].tolist() == ["BTC-USD-PERP"] and frame["oi_convention"].iloc[0] == "both_sides"
    assert one_side_usd(frame).iloc[0] == pytest.approx(40.29525 * 82576.91877094)
    assert client.calls == [("markets/summary", {"market": "ALL"})]


def test_every_adapter_declares_open_interest():
    for cls in (LighterVenue, HyperliquidVenue, BinanceVenue, AsterVenue, ExtendedVenue, ParadexVenue):
        assert Dataset.OPEN_INTEREST in cls.capabilities
    venue = HyperliquidVenue(FakeClient({}))
    venue.capabilities = frozenset({Dataset.FUNDING})
    with pytest.raises(UnsupportedDataset):
        venue.open_interest()


@pytest.mark.live
@pytest.mark.parametrize("name,symbol", [("lighter", "BTC"), ("hyperliquid", "BTC"), ("binance", "BTCUSDT"),
                                         ("aster", "BTCUSDT"), ("extended", "BTC-USD"), ("paradex", "BTC-USD-PERP")])
def test_live_open_interest(name, symbol):
    from quant_marketdata.venues import get_venue

    frame = get_venue(name).open_interest([symbol])
    assert frame["symbol"].tolist() == [symbol] and frame["error"].isna().all()
    assert one_side_usd(frame).iloc[0] > 1e6  # BTC: millions of USD on every venue
