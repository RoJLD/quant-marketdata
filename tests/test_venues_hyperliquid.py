"""Hyperliquid adapter on recorded payloads, plus its registry rule."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import UnsupportedDataset, get_venue
from quant_marketdata.venues import hyperliquid as hl_module
from quant_marketdata.venues.hyperliquid import (
    HyperliquidVenue,
    parse_candles,
    parse_funding_history,
)
from quant_marketdata.venues.registry import parse_hyperliquid, resolve

FIXTURES = Path(__file__).parent / "fixtures" / "hyperliquid"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeInfo:
    """Answers ``/info`` like Hyperliquid: oldest ``page`` funding events from ``startTime``."""

    def __init__(self, page=500):
        self.page = page
        self.calls = []

    def post(self, path, payload):
        assert path == "info"
        self.calls.append(payload)
        if payload["type"] == "meta":
            return _fixture("meta")
        if payload["type"] == "fundingHistory":
            records = [r for r in _fixture("funding_btc_2024")
                       if payload["startTime"] <= r["time"] <= payload.get("endTime", 2**63)]
            return records[: self.page]
        if payload["type"] == "candleSnapshot":
            req = payload["req"]
            return [c for c in _fixture("candles") if req["startTime"] <= c["t"] <= req["endTime"]]
        raise AssertionError(f"unexpected request {payload}")


def test_parse_funding_history_is_signed_per_hour():
    frame = parse_funding_history(_fixture("funding_btc_2024"))
    assert len(frame) == 72 and frame.index.is_monotonic_increasing
    assert frame["rate_raw"].iloc[0] == _fixture("funding_btc_2024")[0]["fundingRate"]
    negative = parse_funding_history([{"coin": "BTC", "fundingRate": "-0.0000125", "premium": "0", "time": 1}])
    assert negative["rate"].iloc[0] == pytest.approx(-1.25e-5)


def test_parse_candles():
    frame = parse_candles(_fixture("candles"))
    assert frame["volume_base"].gt(0).all() and frame["volume_quote"].isna().all()
    assert "trade_count" in frame.columns and (frame["price_type"] == "last").all()


def test_funding_is_canonical_and_pages_forward(monkeypatch):
    monkeypatch.setattr(hl_module, "FUNDING_PAGE", 30)
    client = FakeInfo(page=30)
    frame = HyperliquidVenue(client).funding("BTC", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-04"))
    assert len(frame) == 72 and frame["interval_h"].eq(1).all()
    assert (frame["rate_1h"] == frame["rate"]).all()
    assert len([c for c in client.calls if c["type"] == "fundingHistory"]) == 3  # 30 + 30 + 12


def test_instruments_and_registry():
    venue = get_venue("hyperliquid", client=FakeInfo())
    snapshot = venue.instruments().set_index("venue_symbol")
    assert snapshot.loc["MATIC", "status"] == "inactive" and snapshot.loc["BTC", "status"] == "active"
    assert snapshot.loc["BTC", "settle_asset"] == "USDC" and snapshot.loc["BTC", "venue_market_id"] == "0"
    rows = resolve("hyperliquid", venue.instruments()).instruments.set_index("venue_symbol")
    assert rows.loc["BTC", "instrument_id"] == "BTC-USD-PERP"
    assert rows.loc["kPEPE", "instrument_id"] == "PEPE-USD-PERP" and rows.loc["kPEPE", "multiplier"] == 1000
    assert rows.loc["PAXG", "underlying_ref"] == "XAU"
    assert parse_hyperliquid("@107") is None
    assert "symbol" in venue.specs().columns


def test_no_mark_candles_and_pacing():
    venue = HyperliquidVenue(FakeInfo())
    start, end = pd.Timestamp(1791993600000, unit="ms"), pd.Timestamp(1792000800000, unit="ms")
    assert len(venue.candles("BTC", "1h", start, end)) == 3
    with pytest.raises(UnsupportedDataset):
        venue.candles("BTC", "1h", start, end, price="mark")
    with pytest.raises(UnsupportedDataset):
        venue.daily_metrics("BTC")
    assert HyperliquidVenue(rate=5).client.limiter._interval == pytest.approx(1.0)


@pytest.mark.live
def test_live_hyperliquid_btc_one_day():
    venue = get_venue("hyperliquid")
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    funding = venue.funding("BTC", end - pd.Timedelta(days=1), end)
    candles = venue.candles("BTC", "1h", end - pd.Timedelta(days=1), end)
    assert 22 <= len(funding) <= 25 and funding["interval_h"].eq(1).all()
    assert funding["rate_1h"].abs().max() < 1e-3
    assert 23 <= len(candles) <= 25
    assert "BTC" in set(venue.instruments()["venue_symbol"])
