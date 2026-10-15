"""Binance and Aster (Binance-compatible) adapters on recorded payloads, plus their registry rules."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import Dataset, UnsupportedDataset, get_venue
from quant_marketdata.venues import binance as binance_module
from quant_marketdata.venues.aster import AsterVenue
from quant_marketdata.venues.binance import BinanceVenue, parse_funding_rates, parse_klines
from quant_marketdata.venues.registry import parse_aster, parse_binance, resolve

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(venue, name):
    return json.loads((FIXTURES / venue / f"{name}.json").read_text(encoding="utf-8"))


class FakeFapi:
    """Answers like a Binance-style fapi: oldest ``limit`` records from ``startTime``, within ``endTime``."""

    def __init__(self, venue="binance", funding=None):
        self.venue = venue
        self.funding = funding or {}
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "exchangeInfo":
            return _fixture(self.venue, "exchange_info")
        if path == "fundingInfo":
            return _fixture(self.venue, "funding_info")
        if path == "fundingRate":
            records = [r for r in _fixture(self.venue, self.funding[params["symbol"]])
                       if params["startTime"] <= r["fundingTime"] <= params["endTime"]]
            return records[: params["limit"]]
        if path in ("klines", "markPriceKlines"):
            step = {"1h": 3_600_000, "1d": 86_400_000}[params["interval"]]
            first = params["startTime"] + (-params["startTime"]) % step
            stamps = list(range(first, params["endTime"] + 1, step))[: params["limit"]]
            return [[t, "1", "2", "0.5", "1.5", "3", t + step - 1, "4", 7, "0", "0", "0"] for t in stamps]
        raise AssertionError(f"unexpected path {path}")


# -- parsers -----------------------------------------------------------------------------------------


def test_parse_funding_rates_is_already_signed():
    frame = parse_funding_rates(_fixture("binance", "funding_btc_2024"))
    assert frame.index.is_monotonic_increasing and frame.index.name == "ts"
    first = _fixture("binance", "funding_btc_2024")[0]
    assert frame["rate_raw"].iloc[0] == first["fundingRate"]
    assert frame["rate"].iloc[0] == pytest.approx(float(first["fundingRate"]))
    assert frame["direction_raw"].isna().all()
    negative = parse_funding_rates([{"symbol": "X", "fundingTime": 1, "fundingRate": "-0.0001"}])
    assert negative["rate"].iloc[0] == pytest.approx(-1e-4)
    assert parse_funding_rates([]).empty


def test_parse_klines_last_and_mark():
    last = parse_klines(_fixture("binance", "klines"), "last")
    assert len(last) == 3 and last["volume_quote"].gt(0).all() and "trade_count" in last.columns
    assert last.index[0] == pd.Timestamp(1791993600000, unit="ms")
    mark = parse_klines(_fixture("binance", "mark_klines"), "mark")
    assert mark["volume_base"].isna().all() and (mark["price_type"] == "mark").all()


# -- adapter -----------------------------------------------------------------------------------------


def test_registered_capabilities_and_pacing():
    venue = get_venue("binance", client=FakeFapi())
    assert isinstance(venue, BinanceVenue) and "8h" in venue.resolutions
    with pytest.raises(UnsupportedDataset):
        venue.daily_metrics("BTCUSDT")
    # fundingRate is limited to 500 requests / 5 min per IP: the default client never exceeds 1.5/s.
    assert BinanceVenue(rate=5).client.limiter._interval == pytest.approx(1 / 1.5)
    assert AsterVenue(rate=5).client.limiter._interval == pytest.approx(1 / 5)


def test_instruments_keep_perps_with_their_interval_and_class():
    frame = BinanceVenue(FakeFapi()).instruments().set_index("venue_symbol")
    assert "BTCUSDT_261225" not in frame.index  # quarterly future
    assert frame.loc["GTCUSDT", "funding_interval_h"] == 4
    assert frame.loc["BTCUSDT", "funding_interval_h"] == 8
    assert frame.loc["OMGUSDT", "funding_interval_h"] == 8  # absent from fundingInfo: the default
    assert frame.loc["OMGUSDT", "status"] == "inactive" and frame.loc["BTCUSDT", "status"] == "active"
    assert frame.loc["TSLAUSDT", "asset_class"] == "equity"
    assert frame.loc["TSLAUSDT", "underlying_ref"] == "TSLA"
    assert frame.loc["XAUUSDT", "asset_class"] == "commodity"
    assert frame.loc["BTCUSDC", "settle_asset"] == "USDC"
    assert frame.loc["BTCUSDT", "listed_at"] == pd.Timestamp("2019-09-08 17:55")


def test_funding_follows_interval_switches():
    # GTCUSDT, 2026-10-12: 8h, hits the -2% cap at 16:00, moved to 1h, then to 4h after 10-14 04:00.
    venue = BinanceVenue(FakeFapi(funding={"GTCUSDT": "funding_gtc_switch"}))
    frame = venue.funding("GTCUSDT", pd.Timestamp("2026-10-12 12:00"), pd.Timestamp("2026-10-15 13:00"))
    frame.index = frame.index.floor("h")  # settlements are stamped a few ms late
    interval = frame["interval_h"]
    # The look-back reaches the 08:00 event, so 16:00 is measured as 8h, not given today's 4h.
    assert interval.loc["2026-10-12 16:00"] == 8
    assert frame["rate_1h"].loc["2026-10-12 16:00"] == pytest.approx(-0.02 / 8)
    assert interval.loc["2026-10-12 17:00":"2026-10-14 04:00"].eq(1).all()
    assert interval.loc["2026-10-14 08:00":].eq(4).all()
    assert frame.index.min() == pd.Timestamp("2026-10-12 16:00")  # the look-back event is not returned
    assert list(frame.columns) == ["rate_raw", "direction_raw", "interval_h", "rate", "rate_1h", "apr"]


def test_funding_pages_forward(monkeypatch):
    monkeypatch.setattr(binance_module, "FUNDING_PAGE", 10)
    client = FakeFapi(funding={"BTCUSDT": "funding_btc_2024"})
    frame = BinanceVenue(client).funding("BTCUSDT", pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-11"))
    assert len(frame) == 31 and frame["interval_h"].eq(8).all()
    pages = [p for path, p in client.calls if path == "fundingRate"]
    tenth = _fixture("binance", "funding_btc_2024")[9]["fundingTime"]
    assert len(pages) == 4 and pages[1]["startTime"] == tenth + 1  # 10 + 10 + 10 + 1


def test_candles_size_the_limit_to_the_window():
    client = FakeFapi()
    venue = BinanceVenue(client)
    start, end = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-01 10:00")
    frame = venue.candles("BTCUSDT", "1h", start, end)
    assert len(frame) == 11 and frame.index.min() == start and frame.index.max() == end
    assert [p["limit"] for path, p in client.calls if path == "klines"] == [11]
    mark = venue.candles("BTCUSDT", "1h", start, end, price="mark")
    assert mark["volume_base"].isna().all()
    with pytest.raises(ValueError):
        venue.candles("BTCUSDT", "7h", start, end)


# -- registry ----------------------------------------------------------------------------------------


def test_parse_binance_rules():
    assert parse_binance("BTCUSDT") == {"base": "BTC", "quote": "USD", "multiplier": 1.0, "asset_class": None}
    assert parse_binance("BTCUSDC")["quote"] == "USDC"
    assert (parse_binance("1000PEPEUSDT")["base"], parse_binance("1000PEPEUSDT")["multiplier"]) == ("PEPE", 1000)
    assert (parse_binance("1000000MOGUSDT")["base"], parse_binance("1000000MOGUSDT")["multiplier"]) == ("MOG", 1e6)
    assert parse_binance("1MBABYDOGEUSDT")["base"] == "BABYDOGE"
    assert parse_binance("1INCHUSDT")["base"] == "1INCH"
    assert parse_binance("ETHBTC") is None and parse_binance("BTCU") is None
    assert parse_binance("币安人生USDT") is None
    assert parse_aster("BTCUSD1")["quote"] == "USD1"


def test_resolve_binance_snapshot():
    build = resolve("binance", BinanceVenue(FakeFapi()).instruments())
    rows = build.instruments.set_index("venue_symbol")
    assert rows.loc["BTCUSDT", "instrument_id"] == "BTC-USD-PERP"
    assert rows.loc["BTCUSDT", "mapping_source"] == "rule"  # class declared by the venue, not defaulted
    assert rows.loc["BTCUSDC", "instrument_id"] == "BTC-USDC-PERP"
    assert rows.loc["1000PEPEUSDT", "instrument_id"] == "PEPE-USD-PERP"
    assert rows.loc["TSLAUSDT", "asset_class"] == "equity"
    assert rows.loc["OPENAIUSDT", "asset_class"] == "private"
    assert rows.loc["PAXGUSDT", "asset_class"] == "crypto"
    assert rows.loc["PAXGUSDT", "underlying_ref"] == "XAU"
    assert rows.loc["PAXGUSDT", "mapping_source"] == "override"
    assert set(build.unresolved["venue_symbol"]) == {"ETHBTC", "BTCU", "币安人生USDT"}


# -- Aster -------------------------------------------------------------------------------------------


def test_aster_instruments_and_registry():
    venue = get_venue("aster", client=FakeFapi("aster"))
    snapshot = venue.instruments().set_index("venue_symbol")
    assert "MBLUSDT" not in snapshot.index  # announced, empty contractType
    assert snapshot.loc["TONUSDT", "status"] == "inactive"
    assert snapshot.loc["XAUUSDT", "funding_interval_h"] == 4
    build = resolve("aster", venue.instruments())
    rows = build.instruments.set_index("venue_symbol")
    assert rows.loc["QQQUSDT", "asset_class"] == "etf"  # tagged STOCK and ETF
    assert rows.loc["NVDAUSDT", "asset_class"] == "equity"
    assert rows.loc["XAUUSDT", "asset_class"] == "commodity"
    assert rows.loc["PAXGUSDT", "asset_class"] == "crypto"  # tagged Commodities by Aster
    assert rows.loc["BTCUSDT", "mapping_source"] == "rule-default-class"  # untagged
    assert rows.loc["BTCUSD1", "instrument_id"] == "BTC-USD1-PERP"
    assert rows.loc["BTCUSDT", "settle_asset"] == "USDT"


def test_aster_funding_8h_and_1h():
    client = FakeFapi("aster", funding={"BTCUSDT": "funding_btc", "ARGUSUSDT": "funding_1h"})
    venue = AsterVenue(client)
    btc = venue.funding("BTCUSDT", pd.Timestamp("2026-10-14"), pd.Timestamp("2026-10-15 08:00"))
    assert btc["interval_h"].eq(8).all() and len(btc) == 5
    assert btc["rate_1h"].iloc[-1] == pytest.approx(1e-4 / 8)
    hourly = venue.funding("ARGUSUSDT", pd.Timestamp(1792000000000, unit="ms"),
                           pd.Timestamp(1792020000000, unit="ms"))
    assert hourly["interval_h"].eq(1).all() and len(hourly) == 6
    assert hourly["rate_1h"].iloc[0] == pytest.approx(1.25e-5)


@pytest.mark.live
@pytest.mark.parametrize("name, symbol", [("binance", "BTCUSDT"), ("aster", "BTCUSDT")])
def test_live_binance_style_btc_two_days(name, symbol):
    venue = get_venue(name)
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    funding = venue.funding(symbol, end - pd.Timedelta(days=2), end)
    candles = venue.candles(symbol, "1h", end - pd.Timedelta(days=1), end)
    assert 5 <= len(funding) <= 7 and funding["interval_h"].eq(8).all()
    assert funding["rate_1h"].abs().max() < 1e-3
    assert 23 <= len(candles) <= 25
    assert venue.funding_interval(symbol) == 8
    assert Dataset.FUNDING in venue.capabilities
