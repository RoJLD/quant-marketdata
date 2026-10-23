"""Derive adapter on recorded payloads (no network): perps funding, option contracts, chains, greeks
convention, trades without trader identifiers, settlement prices."""

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
    OPTION_SNAPSHOT_COLUMNS,
    OPTION_TRADE_COLUMNS,
    OptionsVenue,
    resolve_option_contracts,
)
from quant_marketdata.venues.derive import (
    DeriveVenue,
    parse_funding,
    parse_option_instruments,
    parse_option_name,
    parse_option_tickers,
    parse_perp_instruments,
    parse_settlement_prices,
    parse_trades,
)
from quant_marketdata.venues.registry import resolve

FIXTURES = Path(__file__).parent / "fixtures" / "derive"
NOW = pd.Timestamp("2026-10-18 09:20")


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    def __init__(self):
        self.calls = []

    def post(self, method, payload):
        self.calls.append((method, dict(payload)))
        if method == "get_all_instruments":
            if payload["instrument_type"] == "perp":
                return _fixture("instruments_perps")
            name = "instruments_options_expired" if payload["expired"] else "instruments_options"
            fixture = _fixture(name)
            fixture["result"]["instruments"] = [i for i in fixture["result"]["instruments"]
                                                if i["base_currency"] == payload["currency"]]
            return fixture
        if method == "get_tickers":
            return _fixture("tickers_btc_20261023") if payload["expiry_date"] == "20261023" else \
                {"result": {"tickers": {}}}
        if method == "get_funding_rate_history":
            return _fixture("funding_btc")
        if method == "get_option_settlement_prices":
            return _fixture("settlement_prices_btc")
        if method == "get_trade_history":
            return _fixture("trades_btc_options")
        if method == "get_all_currencies":
            return {"result": [{"currency": "BTC", "option": {"name": "BTC-OPTION"}},
                               {"currency": "USDC", "option": None}]}
        raise AssertionError(f"unexpected method {method}")


# -- parsers -----------------------------------------------------------------------------------------


def test_option_names_are_parsed_for_the_cross_check_only():
    assert parse_option_name("BTC-20261023-82000-C") == {
        "base": "BTC", "expiry_date": pd.Timestamp("2026-10-23"), "strike": 82000.0, "option_type": "C"}
    assert parse_option_name("ADA-20261225-1_7-C")["strike"] == pytest.approx(1.7)
    assert parse_option_name("BTC-PERP") is None


def test_option_instruments_from_structured_fields():
    frame = parse_option_instruments(_fixture("instruments_options")["result"]["instruments"], now=NOW)
    assert list(frame.columns[: len(OPTION_SNAPSHOT_COLUMNS)]) == OPTION_SNAPSHOT_COLUMNS
    rows = frame.set_index("venue_symbol")
    call = rows.loc["BTC-20261023-82000-C"]
    assert call["expiry"] == pd.Timestamp("2026-10-23 08:00") and call["strike"] == 82000.0
    assert call["settlement_index_raw"] == "BTC-USD" and call["status"] == "active"
    assert call["taker_fee_raw"] == pytest.approx(0.0003)
    assert json.loads(call["fee_extra_raw"]) == {"base_fee": "0.5", "mark_price_fee_rate_cap": "0.125"}
    assert rows.loc["BTC-20271231-90000-P", "status"] == "scheduled"
    assert rows.loc["ADA-20261225-1_7-C", "strike"] == pytest.approx(1.7)
    expired = parse_option_instruments(_fixture("instruments_options_expired")["result"]["instruments"], now=NOW)
    settled = expired.set_index("venue_symbol").loc["BTC-20261018-95000-P"]
    assert settled["status"] == "expired" and settled["settlement_price"] == pytest.approx(81585.941225826275)


def test_option_contracts_resolve_with_canonical_ids():
    listing = parse_option_instruments(_fixture("instruments_options")["result"]["instruments"], now=NOW)
    build = resolve_option_contracts("derive", listing)
    assert build.ok, build.unresolved
    ids = dict(zip(build.contracts["venue_symbol"], build.contracts["contract_id"], strict=True))
    assert ids["BTC-20261023-82000-C"] == "BTC-USD-20261023T0800-82000-C"
    assert ids["ADA-20261225-1_7-C"] == "ADA-USD-20261225T0800-1.7-C"
    xaut = build.contracts.set_index("venue_symbol").loc["XAUT-20270326-6000-P"]
    assert xaut["underlying_id"] == "XAUT-USD" and xaut["underlying_ref"] == "XAU"
    assert xaut["mapping_source"] == "override"
    assert set(build.contracts["settlement_index_id"]) == {"derive:BTC-USD", "derive:ADA-USD", "derive:XAUT-USD"}
    # A name that disagrees with the structured fields is reported, not arbitrated.
    tampered = listing.copy()
    tampered.loc[tampered["venue_symbol"] == "BTC-20261023-82000-C", "strike"] = 83000.0
    build = resolve_option_contracts("derive", tampered)
    assert build.unresolved["reason"].tolist() == ["name_fields_mismatch: strike"]


def test_tickers_abbreviated_keys_and_missing_sides():
    chain = parse_option_tickers(_fixture("tickers_btc_20261023")["result"]["tickers"])
    assert list(chain.columns[: len(OPTION_QUOTE_COLUMNS)]) == OPTION_QUOTE_COLUMNS
    chain = chain.set_index("venue_symbol")
    call = chain.loc["BTC-20261023-82000-C"]
    assert (call["bid"], call["bid_size"], call["ask"], call["ask_size"]) == (1056.0, 5.0, 1130.0, 4.536)
    assert call["mark"] == 1039.0 and call["index_price"] == 81809.0 and call["forward_price"] == 81850.0
    assert call["iv_mark"] == pytest.approx(0.2924) and call["iv_bid"] == pytest.approx(0.29678)
    assert call["open_interest"] == pytest.approx(13.25) and call["volume_24h"] == pytest.approx(0.02)
    assert call["greeks_reference"] == "forward" and call["underlying_kind"] == "forward"
    assert call["ts"] == pd.Timestamp(1792314457489, unit="ms")
    # "0" means no bid: NaN price, size and IV (never 0).
    put = chain.loc["BTC-20261023-60000-P"]
    assert math.isnan(put["bid"]) and math.isnan(put["bid_size"]) and math.isnan(put["iv_bid"])
    assert put["ask"] == 19.0 and math.isnan(put["iv_ask"])  # ask IV served as 0
    assert put["bid_raw"] == 0.0


@pytest.mark.parametrize("symbol", ["BTC-20261023-82000-C", "BTC-20261023-79000-P"])
def test_greeks_are_undiscounted_black76_on_the_forward(symbol):
    ticker = _fixture("tickers_btc_20261023")["result"]["tickers"][symbol]
    quote = parse_option_tickers({symbol: ticker}).iloc[0]
    expiry = pd.Timestamp("2026-10-23 08:00")
    years = (expiry - quote["ts"]).total_seconds() / (365 * 86400)
    strike, kind = float(symbol.split("-")[2]), symbol[-1]
    ref = black76(quote["forward_price"], strike, years, quote["iv_mark"], kind)
    for greek in ("delta", "gamma", "vega", "theta"):
        assert quote[greek] == pytest.approx(ref[greek], rel=5e-3), greek
    # A unit error (vega per 1.00, annual theta) would be off by 100x / 365x.
    assert not quote["vega"] == pytest.approx(ref["vega"] * 100, rel=0.5)
    discounted = black76(quote["forward_price"], strike, years, quote["iv_mark"], kind,
                         df=quote["discount_factor"])
    assert quote["mark"] == pytest.approx(discounted["price"], rel=2e-3)


def test_trades_keep_the_taker_side_and_no_trader_identifier():
    trades = parse_trades(_fixture("trades_btc_options")["result"]["trades"])
    assert list(trades.columns) == OPTION_TRADE_COLUMNS
    assert len(trades) == 3 and trades["trade_id"].is_unique
    assert set(trades["side"]) <= {"buy", "sell"}
    assert set(trades["trade_type"]) == {"orderbook", "rfq"}
    assert not {"wallet", "subaccount_id", "tx_hash", "realized_pnl"} & set(trades.columns)
    assert trades["index_at_trade"].gt(50000).all()


def test_settlement_prices_of_recent_expiries():
    frame = parse_settlement_prices(_fixture("settlement_prices_btc")["result"]["expiries"], "BTC-USD",
                                    seen_at=NOW)
    assert len(frame) == 12 and (frame["ts"].dt.hour == 8).all()
    last = frame.iloc[-1]
    assert last["ts"] == pd.Timestamp("2026-10-18 08:00") and last["settlement_price"] == pytest.approx(81585.94, abs=0.01)
    assert last["settlement_index_id"] == "derive:BTC-USD" and last["method"] == "unknown"
    assert last["venue_ref"] == "20261018"


def test_funding_events_close_their_hour():
    records = _fixture("funding_btc")["result"]["funding_rate_history"]
    opened_last = pd.Timestamp(records[-1]["timestamp"], unit="ms")
    events = parse_funding(records, complete_before=opened_last + pd.Timedelta(minutes=10))
    assert events.index.min() == pd.Timestamp(records[0]["timestamp"], unit="ms") + pd.Timedelta(hours=1)
    assert events.index.max() == opened_last  # the still-forming last hour is dropped
    varying = next(r for r in records if r["open"] != r["close"])
    event = events.loc[pd.Timestamp(varying["timestamp"], unit="ms") + pd.Timedelta(hours=1)]
    assert event["rate"] == pytest.approx(float(varying["close"]))
    assert event["raw_open"] == pytest.approx(float(varying["open"]))


# -- adapter -----------------------------------------------------------------------------------------


def test_registered_capabilities_and_protocols():
    venue = get_venue("derive", client=FakeClient(), clock=lambda: NOW)
    assert isinstance(venue, DeriveVenue) and isinstance(venue, OptionsVenue)
    assert Dataset.OPTION_CHAIN in venue.capabilities and Dataset.CANDLES not in venue.capabilities
    with pytest.raises(UnsupportedDataset):
        venue.option_mark_candles("BTC-20261023-82000-C", "1h", NOW, NOW)
    with pytest.raises(UnsupportedDataset):
        venue.candles("BTC-PERP", "1h", NOW, NOW)


def test_adapter_chain_is_one_request_per_active_expiry():
    client = FakeClient()
    venue = DeriveVenue(client, clock=lambda: NOW)
    chain = venue.option_chain("BTC")
    tickers = [p for m, p in client.calls if m == "get_tickers"]
    # Active BTC expiries of the fixture: 2026-10-23 (the 2027 contract is only scheduled).
    assert [p["expiry_date"] for p in tickers] == ["20261023"]
    assert len(chain) == 4 and set(chain["venue"]) == {"derive"}


def test_adapter_contracts_funding_trades_settlements():
    client = FakeClient()
    venue = DeriveVenue(client, clock=lambda: NOW)
    contracts = venue.option_contracts()
    assert set(contracts["venue_base"]) == {"BTC"}  # currencies with option markets only
    listing = [p for m, p in client.calls if m == "get_all_instruments"]
    assert listing[0] == {"instrument_type": "option", "expired": False, "page": 1, "page_size": 1000,
                          "currency": "BTC"}
    records = _fixture("funding_btc")["result"]["funding_rate_history"]
    start = pd.Timestamp(records[0]["timestamp"], unit="ms")
    funding = venue.funding("BTC-PERP", start, start + pd.Timedelta(hours=5))
    assert funding["interval_h"].eq(1).all() and funding["rate_1h"].equals(funding["rate"])
    assert "raw_open" in funding.columns
    trades = venue.option_trades("BTC", pd.Timestamp("2026-10-17"), pd.Timestamp("2026-10-19"))
    assert trades["trade_id"].is_unique
    settlements = venue.settlements("BTC-USD", start=pd.Timestamp("2026-10-15"))
    assert settlements["ts"].min() == pd.Timestamp("2026-10-15 08:00")


def test_perps_resolve_in_the_perp_registry():
    instruments = parse_perp_instruments(_fixture("instruments_perps")["result"]["instruments"])
    build = resolve("derive", instruments)
    assert build.ok
    ids = dict(zip(build.instruments["venue_symbol"], build.instruments["instrument_id"], strict=True))
    assert ids == {"BTC-PERP": "BTC-USD-PERP", "ETH-PERP": "ETH-USD-PERP", "XAUT-PERP": "XAUT-USD-PERP"}
    xaut = build.instruments.set_index("venue_symbol").loc["XAUT-PERP"]
    assert xaut["underlying_ref"] == "XAU" and xaut["mapping_source"] == "override"
    assert set(build.instruments["settle_asset"]) == {"USDC"}


@pytest.mark.live
def test_live_derive_btc_chain_and_settlements():
    venue = get_venue("derive")
    contracts = venue.option_contracts("BTC")
    build = resolve_option_contracts("derive", contracts)
    assert build.ok, build.unresolved.head()
    chain = venue.option_chain("BTC")
    assert len(chain) > 100 and chain["iv_mark"].dropna().between(0.05, 5).all()
    assert 10 <= len(venue.settlements("BTC-USD")) <= 24  # about two weeks of expiries (12-17 seen; a window, not a count)
    now = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    funding = venue.funding("BTC-PERP", now - pd.Timedelta(days=2), now)
    assert 40 <= len(funding) <= 49 and funding["rate_1h"].abs().max() < 1e-3
