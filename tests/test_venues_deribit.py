"""Deribit option chains on recorded payloads (no network): inverse prices in USD, IV in fractions,
greeks convention, deliveries since 2016, archive trades."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest
from black76 import black76

from quant_marketdata.venues import Dataset, UnsupportedDataset, get_venue
from quant_marketdata.venues.contracts import (
    OPTION_QUOTE_COLUMNS,
    OptionsVenue,
    resolve_option_contracts,
)
from quant_marketdata.venues.deribit import (
    DeribitVenue,
    parse_book_summary,
    parse_delivery_prices,
    parse_instruments,
    parse_option_name,
    parse_trades,
)
from quant_marketdata.venues.download import DownloadPlan, download_venue

FIXTURES = Path(__file__).parent / "fixtures" / "deribit"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "get_instruments":
            if params["currency"] == "USDC":
                return _fixture("instruments_usdc")
            return _fixture("instruments_btc_expired" if params["expired"] else "instruments_btc")
        if path == "get_book_summary_by_currency":
            return _fixture("book_summary_btc") if params["currency"] == "BTC" else {"result": []}
        if path == "get_index_price":
            return _fixture("index_btc")
        if path == "get_delivery_prices":
            return _fixture("delivery_btc_page0" if params["offset"] == 0 else "delivery_btc_page1")
        if path == "get_last_trades_by_currency_and_time":
            return _fixture("history_trades_btc")
        raise AssertionError(f"unexpected path {path}")


# -- parsers -----------------------------------------------------------------------------------------


def test_names_inverse_and_linear():
    assert parse_option_name("BTC-23OCT26-82000-C") == {
        "base": "BTC", "expiry_date": pd.Timestamp("2026-10-23"), "strike": 82000.0, "option_type": "C"}
    linear = parse_option_name("AVAX_USDC-19OCT26-8d5-C")
    assert linear["base"] == "AVAX" and linear["strike"] == 8.5
    assert parse_option_name("BTC-PERPETUAL") is None


def test_instruments_inverse_and_linear_resolve():
    inverse = parse_instruments(_fixture("instruments_btc")["result"])
    linear = parse_instruments(_fixture("instruments_usdc")["result"])
    rows = inverse.set_index("venue_symbol")
    call = rows.loc["BTC-23OCT26-82000-C"]
    assert call["expiry"] == pd.Timestamp("2026-10-23 08:00") and call["option_type"] == "C"
    assert call["price_unit"] == "base" and call["settle_asset"] == "BTC" and call["venue_quote"] == "USD"
    assert call["settlement_index_raw"] == "btc_usd" and call["status"] == "active"
    assert linear["price_unit"].iloc[0] == "quote" and linear["settle_asset"].iloc[0] == "USDC"
    build = resolve_option_contracts("deribit", pd.concat([inverse, linear], ignore_index=True))
    assert build.ok, build.unresolved
    ids = dict(zip(build.contracts["venue_symbol"], build.contracts["contract_id"], strict=True))
    assert ids["BTC-23OCT26-82000-C"] == "BTC-USD-20261023T0800-82000-C"
    assert ids["AVAX_USDC-19OCT26-8d5-C"] == "AVAX-USD-20261019T0800-8.5-C"
    assert set(build.contracts["settlement_index_id"]) == {"deribit:btc_usd", "deribit:avax_usdc"}
    expired = parse_instruments(_fixture("instruments_btc_expired")["result"])
    assert expired["status"].tolist() == ["expired"] and expired["status_raw"].tolist() == ["delivered"]


def test_book_summary_converts_inverse_prices_and_percent_iv():
    index = _fixture("index_btc")["result"]["index_price"]
    chain = parse_book_summary(_fixture("book_summary_btc")["result"], index)
    assert list(chain.columns[: len(OPTION_QUOTE_COLUMNS)]) == OPTION_QUOTE_COLUMNS
    rows = chain.set_index("venue_symbol")
    call = rows.loc["BTC-23OCT26-82000-C"]
    assert call["bid"] == pytest.approx(call["bid_raw"] * index) and call["bid_raw"] == 0.01
    assert call["mark"] == pytest.approx(0.01047285 * index)
    assert call["iv_mark"] == pytest.approx(0.3461) and call["iv_mark_raw"] == 34.61
    assert call["index_price"] == index and call["underlying_kind"] == "future"
    assert call["greeks_source"] == "none" and math.isnan(call["delta"])
    assert call["raw_underlying_index"] == "BTC-23OCT26"
    no_bid = rows.loc["BTC-19OCT26-86000-C"]
    assert math.isnan(no_bid["bid"]) and math.isnan(no_bid["bid_raw"]) and no_bid["ask"] > 0


def test_ticker_greeks_are_black76_on_underlying_price():
    ticker = _fixture("ticker_btc_82000c")["result"]
    years = (pd.Timestamp("2026-10-23 08:00") - pd.Timestamp(ticker["timestamp"], unit="ms")).total_seconds() \
        / (365 * 86400)
    ref = black76(ticker["underlying_price"], 82000.0, years, ticker["mark_iv"] / 100, "C")
    for greek in ("delta", "vega", "theta"):
        assert ticker["greeks"][greek] == pytest.approx(ref[greek], rel=5e-3), greek
    # The served BTC mark is the USD price over the index: what the snapshot multiplies back.
    assert ticker["mark_price"] * ticker["index_price"] == pytest.approx(ref["price"], rel=1e-2)


def test_deliveries_at_0800_with_the_declared_method():
    records = _fixture("delivery_btc_page0")["result"]["data"] + _fixture("delivery_btc_page1")["result"]["data"]
    frame = parse_delivery_prices(records, "btc_usd", seen_at=pd.Timestamp("2026-10-18 09:20"))
    assert frame["ts"].iloc[0] == pd.Timestamp("2016-03-18 08:00")
    last = frame.iloc[-1]
    assert last["ts"] == pd.Timestamp("2026-10-18 08:00") and last["settlement_price"] == 81583.27
    assert (last["settlement_index_id"], last["method"], last["method_source"]) == \
        ("deribit:btc_usd", "twap_30min", "venue_doc")


def test_archive_trades_in_usd_with_iv():
    trades = parse_trades(_fixture("history_trades_btc")["result"]["trades"], inverse=True)
    first = trades.iloc[0]
    assert first["price"] == pytest.approx(first["price_raw"] * first["index_at_trade"])
    assert first["iv_at_trade"] == pytest.approx(first["iv_at_trade_raw"] / 100)
    assert set(trades["side"]) <= {"buy", "sell"} and set(trades["trade_type"]) == {"orderbook"}


# -- adapter -----------------------------------------------------------------------------------------


def test_adapter_chain_is_two_requests_per_currency():
    client = FakeClient()
    venue = get_venue("deribit", client=client)
    assert isinstance(venue, DeribitVenue) and isinstance(venue, OptionsVenue)
    chain = venue.option_chain("BTC")
    assert [p for p, _ in client.calls] == ["get_index_price", "get_book_summary_by_currency"]
    assert len(chain) == 3
    only = venue.option_chain("BTC", expiry=pd.Timestamp("2026-10-23 08:00"))
    assert set(only["venue_symbol"]) == {"BTC-23OCT26-82000-C", "BTC-23OCT26-79000-P"}
    with pytest.raises(UnsupportedDataset):
        venue.option_mark_candles("BTC-23OCT26-82000-C", "1h", pd.Timestamp("2026-10-18"), pd.Timestamp("2026-10-19"))


def test_adapter_linear_is_opt_in():
    client = FakeClient()
    assert set(DeribitVenue(client).option_contracts()["venue_base"]) == {"BTC"}
    linear = DeribitVenue(client, linear=True).option_contracts("AVAX")
    assert linear["venue_symbol"].tolist() == ["AVAX_USDC-19OCT26-8d5-C"]


def test_adapter_settlements_page_until_start():
    client = FakeClient()
    venue = DeribitVenue(client)
    recent = venue.settlements("btc_usd", start=pd.Timestamp("2026-10-17"))
    assert recent["ts"].tolist() == [pd.Timestamp("2026-10-17 08:00"), pd.Timestamp("2026-10-18 08:00")]
    assert len([p for p, _ in client.calls if p == "get_delivery_prices"]) == 1
    everything = venue.settlements("btc_usd")
    assert len(everything) == 5


def test_download_skips_perp_steps_for_an_options_only_venue(tmp_path):
    venue = DeribitVenue(FakeClient())
    assert Dataset.INSTRUMENTS not in venue.capabilities
    assert download_venue(DownloadPlan(venue="deribit"), root=tmp_path, venue=venue) == []


@pytest.mark.live
def test_live_deribit_btc_chain_and_deliveries():
    venue = get_venue("deribit")
    contracts = venue.option_contracts("BTC")
    build = resolve_option_contracts("deribit", contracts)
    assert build.ok, build.unresolved.head()
    chain = venue.option_chain("BTC")
    assert len(chain) > 300 and chain["iv_mark"].dropna().between(0.05, 5).all()
    priced = chain[chain["mark_raw"] > 0]
    assert (priced["mark"] > 1000 * priced["mark_raw"]).all()  # USD, not BTC
    deliveries = venue.settlements("btc_usd", start=pd.Timestamp.now() - pd.Timedelta(days=10))
    assert 8 <= len(deliveries) <= 11
