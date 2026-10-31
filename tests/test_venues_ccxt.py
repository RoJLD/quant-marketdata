"""The ccxt-backed CEX venues (Bybit, OKX, Bitget, Gate) on payloads recorded 2026-10-30/31, the generic ccxt path,
the raw-archive backfill and the download changes that came with them. No network (live tests: ``-m live``)."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from quant_marketdata.venues import Dataset, get_venue, one_side_usd
from quant_marketdata.venues import download as download_module
from quant_marketdata.venues.ccxt_venue import CcxtVenue
from quant_marketdata.venues.download import DownloadPlan, DownloadResult, download_venue
from quant_marketdata.venues.paging import backward_fetch, numbered_fetch
from quant_marketdata.venues.raw_archive import archive_dir, backfill_funding
from quant_marketdata.venues.registry import (
    REGISTRY_COLUMNS,
    class_conflicts,
    parse_bitget,
    parse_bybit,
    parse_gate,
    parse_okx,
    resolve,
)
from quant_marketdata.venues.store import VenueStore

FIXTURES = Path(__file__).parent / "fixtures"
CLOCK = pd.Timestamp("2026-10-31 12:00")
DAY_S = 86_400


def _fixture(venue, name):
    return json.loads((FIXTURES / venue / f"{name}.json").read_text(encoding="utf-8"))


def _stamp_ms(row, field, unit="ms"):
    return int(float(row[field])) * (1000 if unit == "s" else 1)


class FakeExchange:
    """A ccxt exchange answering from recorded payloads: markets, snapshots, and funding history paged with each
    venue's documented semantics over a recorded series (newest first)."""

    def __init__(self, venue, funding=None):
        self.venue = venue
        self.funding = funding or {}
        self.calls = []

    def load_markets(self):
        self.calls.append(("load_markets", {}))
        return {m["symbol"]: m for m in _fixture(self.venue, "markets")}

    def __getattr__(self, name):
        if name.startswith("public_"):
            return lambda params: self._answer(name, params)
        raise AttributeError(name)

    def funding_calls(self, endpoint):
        return [params for name, params in self.calls if name == endpoint]

    def _records(self, symbol, field, unit="ms"):
        rows = _fixture(self.venue, self.funding[symbol])
        return sorted(rows, key=lambda r: -_stamp_ms(r, field, unit))

    def _answer(self, name, params):
        self.calls.append((name, dict(params)))
        return getattr(self, f"_{self.venue}")(name, params)

    def _bybit(self, name, p):
        if name == "public_get_v5_market_instruments_info":
            assert p["status"] == "Closed"
            return _fixture("bybit", "instruments_closed")
        if name == "public_get_v5_market_funding_history":
            assert "startTime" not in p  # refused alone by the venue; the adapter pages with endTime
            rows = [r for r in self._records(p["symbol"], "fundingRateTimestamp")
                    if int(r["fundingRateTimestamp"]) <= p["endTime"]]
            return {"retCode": 0, "result": {"category": "linear", "list": rows[: p["limit"]]}}
        if name == "public_get_v5_market_tickers":
            return _fixture("bybit", "tickers")
        raise AssertionError(name)

    def _okx(self, name, p):
        if name == "public_get_public_funding_rate_history":
            rows = [r for r in self._records(p["instId"], "fundingTime") if int(r["fundingTime"]) < p["after"]]
            return {"code": "0", "data": rows[: p["limit"]]}
        return _fixture("okx", {"public_get_public_funding_rate": "funding_rate_any",
                                "public_get_public_open_interest": "open_interest",
                                "public_get_public_mark_price": "mark_price",
                                "public_get_market_index_tickers": "index_tickers",
                                "public_get_market_tickers": "tickers"}[name])

    def _bitget(self, name, p):
        if name == "public_mix_get_v2_mix_market_history_fund_rate":
            rows = self._records(p["symbol"], "fundingTime")
            first = (p["pageNo"] - 1) * p["pageSize"]
            return {"code": "00000", "data": rows[first:first + p["pageSize"]]}
        if name == "public_mix_get_v2_mix_market_tickers":
            return _fixture("bitget", "tickers_" + p["productType"].split("-")[0].lower())
        raise AssertionError(name)

    def _gate(self, name, p):
        if name == "public_futures_get_settle_funding_rate":
            return [r for r in self._records(p["contract"], "t", "s") if p["to"] - 30 * DAY_S < r["t"] <= p["to"]]
        if name == "public_futures_get_settle_tickers":
            return _fixture("gate", "tickers")
        raise AssertionError(name)


def make(venue, **funding):
    exchange = FakeExchange(venue, funding)
    return get_venue(venue, exchange=exchange, rate=1000, sleep=lambda s: None, clock=lambda: CLOCK), exchange


def registry_of(venue):
    venue_obj, _ = make(venue)
    build = resolve(venue, venue_obj.instruments())
    return build.instruments.set_index("venue_symbol"), dict(zip(build.unresolved["venue_symbol"],
                                                                 build.unresolved["reason"], strict=True))


# -- lazy ccxt -----------------------------------------------------------------------------------------


def test_venues_register_and_list_without_ccxt(monkeypatch):
    monkeypatch.setitem(sys.modules, "ccxt", None)  # ``import ccxt`` now fails
    venue = get_venue("okx")
    assert Dataset.FUNDING in venue.capabilities and Dataset.CANDLES not in venue.capabilities
    assert venue.funding_window == pd.Timedelta(days=94)
    with pytest.raises(ImportError, match=r"quant-marketdata\[ccxt\]"):
        venue.markets()


def test_capped_pacing_and_unsupported_datasets():
    from quant_marketdata.venues import UnsupportedDataset

    venue, _ = make("bybit")
    assert venue.limiter._interval == pytest.approx(1 / 10)  # max_rate 10/s whatever was asked
    assert make("okx")[0].limiter._interval == pytest.approx(1 / 4)
    with pytest.raises(UnsupportedDataset):
        venue.candles("BTCUSDT", "1h", CLOCK - pd.Timedelta(days=1), CLOCK)


# -- registry rules ------------------------------------------------------------------------------------


@pytest.mark.parametrize("parse, symbol, expected", [
    (parse_bybit, "BTCUSDT", ("BTC", "USD", 1.0)),
    (parse_bybit, "BTCPERP", ("BTC", "USDC", 1.0)),
    (parse_bybit, "1000PEPEPERP", ("PEPE", "USDC", 1000.0)),
    (parse_bybit, "10000SATSUSDT", ("SATS", "USD", 1e4)),
    (parse_bybit, "10000000AIDOGEUSDT", ("AIDOGE", "USD", 1e7)),
    (parse_bybit, "1000000MOGUSDT", ("MOG", "USD", 1e6)),
    (parse_bybit, "1INCHUSDT", ("1INCH", "USD", 1.0)),
    (parse_bybit, "EURUSDUSDT", ("EUR", "USD", 1.0)),
    (parse_bitget, "1MBABYDOGEUSDT", ("BABYDOGE", "USD", 1e6)),
    (parse_bitget, "BTCPERP", ("BTC", "USDC", 1.0)),
    (parse_okx, "BTC-USDT-SWAP", ("BTC", "USD", 1.0)),
    (parse_okx, "BTC-USDC-SWAP", ("BTC", "USDC", 1.0)),
    (parse_gate, "2Z_USDT", ("2Z", "USD", 1.0)),
    (parse_gate, "USDBRL_USDT", ("USD", "BRL", 1.0)),
])
def test_cex_parsing_rules(parse, symbol, expected):
    parsed = parse(symbol)
    assert (parsed["base"], parsed["quote"], parsed["multiplier"]) == expected


def test_cex_parsing_rejects():
    assert parse_okx("BTC-USD-SWAP") is None  # coin-margined: not a stablecoin book
    assert parse_bitget("龙虾USDT") is None and parse_gate("龙虾_USDT") is None
    assert parse_gate("A_B_USDT") is None


def test_bybit_registry():
    reg, unresolved = registry_of("bybit")
    btc = reg.loc["BTCUSDT"]
    assert (btc.instrument_id, btc.settle_asset, btc.asset_class, btc.funding_interval_h) == ("BTC-USD-PERP", "USDT",
                                                                                              "crypto", 8.0)
    assert btc.listed_at == pd.Timestamp("2020-03-15") and btc.status == "active"
    assert reg.loc["BTCPERP", "instrument_id"] == "BTC-USDC-PERP" and reg.loc["BTCPERP", "settle_asset"] == "USDC"
    assert reg.loc["SHIB1000USDT", ["instrument_id", "multiplier"]].tolist() == ["SHIB-USD-PERP", 1000.0]
    assert reg.loc["10000SATSUSDT", ["instrument_id", "multiplier"]].tolist() == ["SATS-USD-PERP", 1e4]
    assert reg.loc["AAPLUSDT", ["asset_class", "underlying_ref"]].tolist() == ["equity", "AAPL"]
    # a renamed stock keeps the venue's name as base and its ticker as reference: no clash with the CAT memecoin
    assert reg.loc["CATSTOCKUSDT", ["instrument_id", "asset_class", "underlying_ref"]].tolist() == [
        "CATSTOCK-USD-PERP", "equity", "CAT"]
    assert reg.loc["SPYUSDT", "asset_class"] == "etf" and reg.loc["XAUUSDT", "asset_class"] == "commodity"
    assert reg.loc["EURUSDUSDT", ["instrument_id", "asset_class"]].tolist() == ["EUR-USD-PERP", "fx"]
    assert reg.loc["OPENAIUSDT", ["asset_class", "mapping_source"]].tolist() == ["private", "override"]
    assert reg.loc["BLURUSDT", "funding_interval_h"] == 1.0
    ladys = reg.loc["10000LADYSUSDT"]  # delisted perps are listed too
    assert ladys.status == "delisted" and ladys.delisted_at == pd.Timestamp(1759741200000, unit="ms")
    assert "BTC-01DEC23" not in reg.index  # dated futures dropped
    assert "1000BTTUSDT" in reg.index and "replaced by 1000BTTUSDT" in unresolved["BTTUSDT"]
    assert "Binance" in unresolved["GMEUSDT"]


def test_okx_registry():
    reg, unresolved = registry_of("okx")
    assert reg.loc["BTC-USDT-SWAP", ["instrument_id", "asset_class", "funding_interval_h"]].tolist() == [
        "BTC-USD-PERP", "crypto", 8.0]
    assert reg.loc["CHIP-USDT-SWAP", "funding_interval_h"] == 4.0
    assert reg.loc["AAPL-USDT-SWAP", "asset_class"] == "equity"
    assert reg.loc["US500-USDT-SWAP", "asset_class"] == "index"  # category 3 "stocks": override
    assert reg.loc["XAU-USDT-SWAP", "asset_class"] == "commodity"
    assert reg.loc["OPENAI-USDT-SWAP", ["asset_class", "mapping_source"]].tolist() == ["private", "rule"]
    assert "ON-USDT-SWAP" in unresolved and "BTC-USD-SWAP" not in reg.index


def test_bitget_registry():
    reg, unresolved = registry_of("bitget")
    assert reg.loc["BTCUSDT", ["asset_class", "funding_interval_h"]].tolist() == ["crypto", 8.0]
    assert reg.loc["BTCPERP", "instrument_id"] == "BTC-USDC-PERP"
    assert reg.loc["1MBABYDOGEUSDT", ["instrument_id", "multiplier"]].tolist() == ["BABYDOGE-USD-PERP", 1e6]
    assert reg.loc["XTZUSDT", "funding_interval_h"] == 4.0
    for symbol, asset_class in (("NVDAUSDT", "equity"), ("SPYUSDT", "etf"), ("XAUUSDT", "commodity"),
                                ("OPENAIUSDT", "private")):
        assert reg.loc[symbol, ["asset_class", "mapping_source"]].tolist() == [asset_class, "override"]
    assert reg.loc["PAXGUSDT", ["asset_class", "underlying_ref"]].tolist() == ["crypto", "XAU"]
    assert unresolved["CATUSDT"].startswith("Bitget RWA perp") and unresolved["TWTSMUSDT"].startswith("Bitget RWA")
    assert unresolved["龙虾USDT"].startswith("non-ASCII")
    assert "SBTCSUSDT" not in reg.index and "SBTCSUSDT" not in unresolved  # demo contract: not a perp here


def test_gate_registry():
    reg, unresolved = registry_of("gate")
    btc = reg.loc["BTC_USDT"]
    assert (btc.instrument_id, btc.funding_interval_h, btc.listed_at) == ("BTC-USD-PERP", 8.0,
                                                                          pd.Timestamp("2019-11-18"))
    assert reg.loc["2Z_USDT", "funding_interval_h"] == 4.0
    for symbol, asset_class in (("AAPL_USDT", "equity"), ("SPX500_USDT", "index"), ("XAU_USDT", "commodity"),
                                ("OPENAI_USDT", "private")):
        assert reg.loc[symbol, "asset_class"] == asset_class
    assert reg.loc["PAXG_USDT", ["asset_class", "underlying_ref"]].tolist() == ["crypto", "XAU"]  # "metals"
    assert reg.loc["USDC_USDT", "asset_class"] == "crypto"  # "forex"
    assert reg.loc["EURUSD_USDT", ["instrument_id", "asset_class"]].tolist() == ["EUR-USD-PERP", "fx"]
    assert "Caterpillar" in unresolved["CAT_USDT"] and "RTX_USDT" in unresolved
    assert "BTC_USD" not in reg.index


def test_class_conflicts_report_crypto_against_other_classes_only():
    registry = pd.DataFrame([
        {"instrument_id": "CAT-USD-PERP", "venue": "binance", "venue_symbol": "1000CATUSDT", "asset_class": "crypto",
         "mapping_source": "rule"},
        {"instrument_id": "CAT-USD-PERP", "venue": "gate", "venue_symbol": "CAT_USDT", "asset_class": "equity",
         "mapping_source": "rule"},
        {"instrument_id": "SPY-USD-PERP", "venue": "lighter", "venue_symbol": "SPY", "asset_class": "etf",
         "mapping_source": "override"},
        {"instrument_id": "SPY-USD-PERP", "venue": "okx", "venue_symbol": "SPY-USDT-SWAP", "asset_class": "equity",
         "mapping_source": "rule"},
    ])
    conflicts = class_conflicts(registry)
    assert conflicts["instrument_id"].unique().tolist() == ["CAT-USD-PERP"] and len(conflicts) == 2
    assert class_conflicts(pd.DataFrame()).empty


def test_specs_are_text_one_row_per_perp():
    venue, _ = make("gate")
    specs = venue.specs()
    assert specs.columns[0] == "symbol" and len(specs) == len(venue.markets())
    btc = specs.set_index("symbol").loc["BTC_USDT"]
    assert btc["funding_interval"] == "28800" and btc["contract_size"] == pytest.approx(1e-4)


# -- funding history, paged with each venue's own parameters ---------------------------------------------


def _rates(venue, fixture, field, time_field, unit="ms"):
    rows = _fixture(venue, fixture)
    return {pd.Timestamp(_stamp_ms(r, time_field, unit), unit="ms"): r[field] for r in rows}


def test_bybit_funding_pages_back_with_end_time():
    venue, exchange = make("bybit", BTCUSDT="funding_btc")
    start, end = pd.Timestamp("2026-06-25"), pd.Timestamp("2026-10-31 08:00")
    events = venue.funding("BTCUSDT", start, end)
    raw = {t: r for t, r in _rates("bybit", "funding_btc", "fundingRate", "fundingRateTimestamp").items()
           if start <= t <= end}
    assert len(events) == len(raw) and events.index.is_unique and events.index.is_monotonic_increasing
    assert events["rate_raw"].tolist() == [raw[t] for t in events.index]
    assert (events["interval_h"] == 8).all() and np.allclose(events["rate_1h"], events["rate"] / 8)
    calls = exchange.funding_calls("public_get_v5_market_funding_history")
    assert len(calls) == 2 and calls[0]["endTime"] == int(end.value // 1_000_000)
    assert calls[1]["endTime"] < calls[0]["endTime"]


def test_bybit_interval_change_and_listing_stop():
    venue, _ = make("bybit", BLURUSDT="funding_blur")
    events = venue.funding("BLURUSDT", pd.Timestamp("2026-09-06"), pd.Timestamp("2026-10-31"))
    assert set(events["interval_h"]) == {8.0, 1.0}  # moved from 8 h to 1 h settlements
    assert events["interval_h"].iloc[-1] == 1.0 and events["interval_h"].iloc[0] == 8.0
    venue, exchange = make("bybit", BTCUSDT="funding_btc_listing")
    events = venue.funding("BTCUSDT", pd.Timestamp("2020-03-15"), pd.Timestamp("2020-04-14"))
    assert len(events) == 59 and events.index[0] == pd.Timestamp("2020-03-25 16:00")
    assert len(exchange.funding_calls("public_get_v5_market_funding_history")) == 1  # a short page ends it


def test_okx_funding_pages_back_with_after_over_its_window():
    venue, exchange = make("okx", **{"CHIP-USDT-SWAP": "funding_chip", "BTC-USDT-SWAP": "funding_btc"})
    events = venue.funding("CHIP-USDT-SWAP", pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-31"))
    assert len(events) == 573 and events.index[0] == pd.Timestamp("2026-07-27 12:00")
    assert (events["interval_h"] == 4).all()
    calls = exchange.funding_calls("public_get_public_funding_rate_history")
    assert len(calls) == 2 and calls[0]["limit"] == 400
    first_page = sorted(_fixture("okx", "funding_chip"), key=lambda r: -int(r["fundingTime"]))[:400]
    assert calls[1]["after"] == min(int(r["fundingTime"]) for r in first_page)  # exclusive bound
    btc = venue.funding("BTC-USDT-SWAP", pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-31"))
    raw = _rates("okx", "funding_btc", "realizedRate", "fundingTime")
    assert len(btc) == 286 and btc["rate_raw"].tolist() == [raw[t] for t in btc.index]


def test_okx_and_bitget_ask_nothing_before_their_window():
    venue, exchange = make("okx", **{"BTC-USDT-SWAP": "funding_btc"})
    assert venue.funding("BTC-USDT-SWAP", pd.Timestamp("2026-01-01"), pd.Timestamp("2026-03-01")).empty
    assert not exchange.funding_calls("public_get_public_funding_rate_history")
    venue, exchange = make("bitget", XTZUSDT="funding_xtz")
    assert venue.funding("XTZUSDT", pd.Timestamp("2025-01-01"), pd.Timestamp("2026-06-01")).empty
    assert not exchange.funding_calls("public_mix_get_v2_mix_market_history_fund_rate")


def test_bitget_funding_pages_by_number():
    venue, exchange = make("bitget", XTZUSDT="funding_xtz")
    events = venue.funding("XTZUSDT", pd.Timestamp("2026-07-01"), pd.Timestamp("2026-10-31"))
    assert len(events) == 540 and (events["interval_h"] == 4).all()
    calls = exchange.funding_calls("public_mix_get_v2_mix_market_history_fund_rate")
    assert [c["pageNo"] for c in calls] == [1, 2, 3, 4, 5, 6]  # the sixth holds 40 rows: the end
    assert {c["productType"] for c in calls} == {"USDT-FUTURES"} and calls[0]["pageSize"] == 100


def test_gate_funding_pages_back_by_30_day_windows():
    venue, exchange = make("gate", BTC_USDT="funding_btc")
    start, end = pd.Timestamp("2026-08-15"), pd.Timestamp("2026-10-31 08:00")
    events = venue.funding("BTC_USDT", start, end)
    raw = {t: r for t, r in _rates("gate", "funding_btc", "r", "t", "s").items() if start <= t <= end}
    assert len(events) == len(raw) and (events["interval_h"] == 8).all()
    assert any(t.second == 1 for t in events.index)  # stamps one second past the hour, kept as served
    tos = [c["to"] for c in exchange.funding_calls("public_futures_get_settle_funding_rate")]
    assert len(tos) == 3 and tos[0] == int(end.value // 1_000_000_000) and tos == sorted(tos, reverse=True)


def test_gate_funding_stops_before_listing():
    venue, exchange = make("gate", BTC_USDT="funding_btc_listing")
    events = venue.funding("BTC_USDT", pd.Timestamp("2019-11-18"), pd.Timestamp("2019-12-07 16:00"))
    assert len(events) == 59 and events.index[0] == pd.Timestamp("2019-11-18 08:00")
    # the window before the listing is empty: one step back, then the start is passed
    assert len(exchange.funding_calls("public_futures_get_settle_funding_rate")) == 2


def test_backward_fetch_stops():
    def pages(stamps):
        return pd.DataFrame({"rate": 0.0}, index=pd.DatetimeIndex(pd.to_datetime(stamps, unit="ms"), name="ts"))

    series = list(range(0, 100, 10))  # points at 0, 10, ..., 90

    def fetch(cursor, size=3):
        return pages([t for t in series if t <= cursor][-size:])

    out = backward_fetch(fetch, 0, 95, 3)
    assert len(out) == 10  # 3 + 3 + 3 + a short page of 1
    assert len(backward_fetch(fetch, 45, 95, 3)) == 6  # stops once the start is passed
    gaps = []

    def windowed(cursor):  # a 30-unit window per request, with a hole between 20 and 70
        gaps.append(cursor)
        return pages([t for t in (0, 10, 20, 70, 80, 90) if cursor - 30 < t <= cursor])

    assert len(backward_fetch(windowed, 0, 95, None, empty_step=30)) == 6
    assert len(backward_fetch(lambda c: pages([50]), 0, 95, None)) == 1  # a page that does not move back


def test_numbered_fetch_stops():
    stamps = pd.date_range("2026-01-01", periods=25, freq="h")[::-1]

    def page(number):
        chunk = stamps[(number - 1) * 10: number * 10]
        return pd.DataFrame({"rate": 0.0}, index=pd.DatetimeIndex(chunk, name="ts"))

    assert len(numbered_fetch(page, stamps[-1], 10)) == 25  # 10 + 10 + a short 5
    assert len(numbered_fetch(page, stamps[12], 10)) == 20  # the second page reaches the start


# -- live snapshots ------------------------------------------------------------------------------------


def test_bybit_open_interest_reads_the_single_side():
    venue, exchange = make("bybit")
    oi = venue.open_interest().set_index("symbol")
    btc = oi.loc["BTCUSDT"]
    assert btc.open_interest_base == pytest.approx(34531.533)  # singleOpenInterest; openInterest is 69063.066
    assert btc.oi_convention == "one_side" and btc.price_kind == "mark" and btc.price == pytest.approx(77479.27)
    assert not any(s.startswith("BTCUSDT-") for s in oi.index)  # dated futures dropped
    prices = venue.prices(["BTCUSDT", "NOPE"]).set_index("symbol")
    assert prices.loc["BTCUSDT", ["mark_price", "index_price", "last_price"]].notna().all()
    assert prices.loc["NOPE", "error"] == "unknown symbol"
    assert len(exchange.funding_calls("public_get_v5_market_tickers")) == 2  # one request each


def test_okx_open_interest_and_prices():
    venue, exchange = make("okx")
    oi = venue.open_interest().set_index("symbol")
    raw = {r["instId"]: r for r in _fixture("okx", "open_interest")["data"]}["BTC-USDT-SWAP"]
    mark = {r["instId"]: r for r in _fixture("okx", "mark_price")["data"]}["BTC-USDT-SWAP"]["markPx"]
    btc = oi.loc["BTC-USDT-SWAP"]
    assert btc.open_interest_base == pytest.approx(float(raw["oiCcy"]))
    assert btc.open_interest_usd == pytest.approx(float(raw["oiUsd"]))  # as published
    assert btc.price == pytest.approx(float(mark)) and btc.oi_convention == "one_side"
    prices = venue.prices().set_index("symbol")
    index = {r["instId"]: r["idxPx"] for r in _fixture("okx", "index_tickers")["data"]}
    assert prices.loc["BTC-USDT-SWAP", "index_price"] == pytest.approx(float(index["BTC-USDT"]))
    assert prices[["mark_price", "index_price", "last_price"]].notna().all().all()
    assert [p["quoteCcy"] for p in exchange.funding_calls("public_get_market_index_tickers")] == ["USDT"]


def test_bitget_open_interest_and_prices_per_product_type():
    venue, exchange = make("bitget")
    oi = venue.open_interest().set_index("symbol")
    raw = {t["symbol"]: t for t in _fixture("bitget", "tickers_usdt")["data"]}["BTCUSDT"]
    assert oi.loc["BTCUSDT", "open_interest_base"] == pytest.approx(float(raw["holdingAmount"]))
    assert "BTCPERP" in oi.index and oi["oi_convention"].eq("one_side").all()
    assert sorted(p["productType"] for p in exchange.funding_calls("public_mix_get_v2_mix_market_tickers")) == [
        "USDC-FUTURES", "USDT-FUTURES"]
    prices = venue.prices().set_index("symbol")
    assert prices.loc["BTCUSDT", "last_price"] == pytest.approx(float(raw["lastPr"]))


def test_gate_open_interest_counts_both_sides():
    venue, _ = make("gate")
    oi = venue.open_interest()
    btc = oi.set_index("symbol").loc["BTC_USDT"]
    assert btc.open_interest_base == pytest.approx(611094608 * 1e-4)
    assert btc.oi_convention == "both_sides" and btc.ts == CLOCK
    assert one_side_usd(oi[oi["symbol"] == "BTC_USDT"]).iloc[0] == pytest.approx(btc.open_interest_usd / 2)
    prices = venue.prices(["BTC_USDT"])
    assert prices["index_price"].iloc[0] > 0


# -- conventions frozen from the 2026-10-30 payloads -----------------------------------------------------


@pytest.mark.parametrize("venue", ["bybit", "okx", "bitget", "gate"])
def test_funding_sign_and_unit_match_binance(venue):
    """Positive = longs pay, a fraction per interval: current rates against Binance's on shared symbols."""
    evidence = _fixture("evidence", "funding_vs_binance")
    common = sorted(set(evidence[venue]) & set(evidence["binance"]))
    a = np.array([float(evidence[venue][s]) for s in common])
    b = np.array([float(evidence["binance"][s]) for s in common])
    assert len(common) > 400 and np.corrcoef(a, b)[0, 1] > 0.9
    assert not ((np.abs(a) > 2e-4) & (np.abs(b) > 2e-4) & (np.sign(a) != np.sign(b))).any()


def test_open_interest_sides_frozen():
    evidence = _fixture("evidence", "open_interest_sides")
    bybit = np.array([float(a) / float(b) for a, b in evidence["bybit_open_interest_vs_single"].values()])
    assert len(bybit) > 900 and ((bybit > 1.999) & (bybit < 2.001)).all()  # openInterest = 2 x one side
    gate = np.array([float(a) / b for a, b in evidence["gate_total_size_vs_position_size"].values() if b > 1000])
    assert len(gate) > 1000 and abs(np.median(gate) - 2) < 1e-3 and np.mean(abs(gate - 2) < 0.02) > 0.98


# -- the generic (unified) path ------------------------------------------------------------------------


class FakeUnified:
    """A ccxt exchange whose unified methods honour ``since`` (the generic path's assumption)."""

    def __init__(self):
        self.calls = []
        start = pd.Timestamp("2026-01-01")
        self.history = [{"timestamp": int((start + pd.Timedelta(hours=4 * i)).value // 1_000_000),
                         "fundingRate": 1e-4 * (i - 3)} for i in range(12)]

    def load_markets(self):
        perp = {"id": "BTCUSDT", "symbol": "BTC/USDT:USDT", "base": "BTC", "settle": "USDT", "swap": True,
                "linear": True, "active": True, "contractSize": 0.001, "created": 1600000000000,
                "info": {"tick": "0.1", "nested": {"a": 1}, "flag": True}}
        inverse = {**perp, "id": "BTCUSD", "symbol": "BTC/USD:BTC", "settle": "BTC", "linear": False}
        spot = {**perp, "id": "BTCUSDT_SPOT", "symbol": "BTC/USDT", "swap": False}
        return {m["symbol"]: m for m in (perp, inverse, spot)}

    def fetch_funding_rates(self, symbols):
        return {"BTC/USDT:USDT": {"interval": "4h"}}

    def fetch_funding_rate_history(self, symbol, since, limit):
        self.calls.append(since)
        return [r for r in self.history if r["timestamp"] >= since][:limit]

    def fetch_tickers(self, symbols):
        return {"BTC/USDT:USDT": {"markPrice": 100.0, "indexPrice": 99.5, "last": 100.2,
                                  "timestamp": 1793000000000}}

    def fetch_open_interest(self, symbol):
        return {"openInterestAmount": 2000.0, "timestamp": 1793000000000}

    def fetch_order_book(self, symbol, limit):
        return {"bids": [[99.0, 1.0, 0], [98.0, 2.0, 0]], "asks": [[101.0, 1.5, 0]], "timestamp": 1793000000000}


class GenericVenue(CcxtVenue):
    name = "generic-test"
    exchange_id = "generic"
    funding_page = 3


def test_generic_unified_path():
    exchange = FakeUnified()
    venue = GenericVenue(exchange, sleep=lambda s: None, clock=lambda: CLOCK)
    inst = venue.instruments()
    assert inst["venue_symbol"].tolist() == ["BTCUSDT"] and inst["funding_interval_h"].iloc[0] == 4.0
    assert inst["venue_market_id"].iloc[0] == "BTC/USDT:USDT" and inst["listed_at"].iloc[0] == pd.Timestamp(
        1600000000000, unit="ms")
    events = venue.funding("BTCUSDT", pd.Timestamp("2026-01-01 04:00"), pd.Timestamp("2026-01-02 20:00"))
    assert len(events) == 11 and (events["interval_h"] == 4).all() and events["rate"].iloc[0] == pytest.approx(-2e-4)
    assert len(exchange.calls) > 3  # paged forward by 3
    oi = venue.open_interest().iloc[0]
    assert oi.open_interest_base == pytest.approx(2.0) and oi.price == 100.0 and oi.oi_convention is None
    assert venue.prices().iloc[0][["mark_price", "index_price", "last_price"]].tolist() == [100.0, 99.5, 100.2]
    specs = venue.specs().iloc[0]
    assert specs["nested"] == '{"a": 1}' and specs["flag"] == "true" and specs["tick"] == "0.1"
    book = venue.order_book("BTCUSDT", depth=1)
    assert book["bids"] == [(99.0, 1.0)] and book["asks"] == [(101.0, 1.5)]


def test_call_retries_network_errors_but_not_anti_bot_refusals():
    errors = pytest.importorskip("ccxt.base.errors")
    venue = GenericVenue(FakeUnified(), sleep=lambda s: None)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise errors.RequestTimeout("slow")
        return "ok"

    assert venue.call(flaky) == "ok" and len(attempts) == 3

    def blocked():
        raise errors.DDoSProtection("403")

    with pytest.raises(errors.DDoSProtection):
        venue.call(blocked)


# -- raw archive backfill (Quant-lab #94's archive) -------------------------------------------------------


def write_archive(root, venue, symbol, rows, time_field):
    """An archive file as Quant-lab's collect_window_funding.py writes it."""
    rows = sorted(rows, key=lambda r: int(r[time_field]))
    stamps = [int(r[time_field]) for r in rows]
    data = {"funding_time": pa.array(stamps, pa.int64()).cast(pa.timestamp("ms", tz="UTC"))}
    for column in rows[0]:
        data[column] = pa.array([str(r[column]) for r in rows], pa.string())
    data["fetched_at"] = pa.array([1793390416000] * len(rows), pa.int64()).cast(pa.timestamp("ms", tz="UTC"))
    path = archive_dir(root, venue) / f"{symbol}.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table(data), path)
    return path


def test_backfill_from_raw_archive(tmp_path):
    venue, _ = make("okx", **{"BTC-USDT-SWAP": "funding_btc"})
    store = VenueStore.at(tmp_path)
    rows = sorted(_fixture("okx", "funding_btc"), key=lambda r: int(r["fundingTime"]))
    archived = write_archive(tmp_path, "okx", "BTC-USDT-SWAP", rows[:200], "fundingTime")
    (archive_dir(tmp_path, "okx") / "BROKEN.parquet").write_bytes(b"not parquet")
    before = archived.read_bytes()
    stored = venue.funding("BTC-USDT-SWAP", pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-31"))
    store.merge(store.path("okx", Dataset.FUNDING, "BTC-USDT-SWAP"), stored)  # the API's last month
    results = {r.symbol: r for r in backfill_funding(venue, root=tmp_path)}
    assert results["BROKEN"].error and results["BTC-USDT-SWAP"].error is None
    merged = store.read(store.path("okx", Dataset.FUNDING, "BTC-USDT-SWAP"))
    # the archive (oldest 200 rows) and the stored month overlap: the union is the whole series
    assert len(merged) == 286 and merged.index.min() == pd.Timestamp(int(rows[0]["fundingTime"]), unit="ms")
    assert results["BTC-USDT-SWAP"].new_rows == len(merged) - len(stored)
    assert merged.index.is_unique and (merged["interval_h"] == 8).all()
    assert merged.loc[stored.index.min():, "rate_raw"].tolist() == stored["rate_raw"].tolist()
    assert archived.read_bytes() == before  # the archive is read, never written


def test_download_with_raw_backfill(tmp_path):
    venue, _ = make("bitget", BTCUSDT="funding_btc")
    write_archive(tmp_path, "bitget", "BTCUSDT", _fixture("bitget", "funding_btc"), "fundingTime")
    results = download_venue(DownloadPlan("bitget", symbols=["BTCUSDT"], datasets=[Dataset.FUNDING],
                                          specs=False, raw_backfill=True), root=tmp_path, venue=venue)
    labels = {r.dataset: r for r in results}
    assert labels["funding/raw_archive"].new_rows == 170 and labels["funding"].error is None
    stored = VenueStore.at(tmp_path).read(VenueStore.at(tmp_path).path("bitget", Dataset.FUNDING, "BTCUSDT"))
    assert len(stored) == 170 and stored["interval_h"].max() == 8


# -- download changes -----------------------------------------------------------------------------------


def test_download_skips_markets_delisted_before_the_start(tmp_path):
    venue, exchange = make("bybit", BTCUSDT="funding_btc")
    results = download_venue(DownloadPlan("bybit", symbols=["BTCUSDT", "10000LADYSUSDT"], datasets=[Dataset.FUNDING],
                                          since=pd.Timestamp("2026-10-25"), specs=False), root=tmp_path, venue=venue)
    assert [r.symbol for r in results] == ["BTCUSDT"] and results[0].new_rows > 0
    assert {c["symbol"] for c in exchange.funding_calls("public_get_v5_market_funding_history")} == {"BTCUSDT"}


def test_download_warns_on_a_series_older_than_the_window(tmp_path, caplog):
    venue, _ = make("okx", **{"BTC-USDT-SWAP": "funding_btc"})
    store = VenueStore.at(tmp_path)
    old = venue.funding("BTC-USDT-SWAP", pd.Timestamp("2026-07-27"), pd.Timestamp("2026-07-29"))
    store.merge(store.path("okx", Dataset.FUNDING, "BTC-USDT-SWAP"), old)
    plan = DownloadPlan("okx", symbols=["BTC-USDT-SWAP"], datasets=[Dataset.FUNDING], specs=False)
    with caplog.at_level(logging.WARNING, logger="quant_marketdata.venues.download"):
        # the clock of the download is now: the stored series ends before the 94-day window
        download_venue(plan, root=tmp_path, venue=venue)
    assert any("older than the venue's window" in m for m in caplog.messages)


def test_download_logs_class_conflicts(tmp_path, caplog):
    store = VenueStore.at(tmp_path)
    store.write(store.registry_path(), pd.DataFrame([{
        "venue": "lighter", "venue_symbol": "AAPL", "instrument_id": "AAPL-USD-PERP", "asset_class": "crypto",
        "mapping_source": "rule-default-class"}]).reindex(columns=REGISTRY_COLUMNS))
    venue, _ = make("okx")
    with caplog.at_level(logging.WARNING, logger="quant_marketdata.venues.download"):
        download_venue(DownloadPlan("okx", datasets=[], specs=False), root=tmp_path, venue=venue)
    assert any("AAPL-USD-PERP carries several asset classes" in m for m in caplog.messages)


def test_cli_downloads_several_venues_and_isolates_failures(monkeypatch, tmp_path, capsys):
    seen = []

    def fake_download(plan, root=None, rate=2.0):
        seen.append((plan.venue, plan.raw_backfill))
        if plan.venue == "broken":
            raise RuntimeError("venue down")
        return [DownloadResult(plan.venue, "X", "funding", 3, 3)]

    monkeypatch.setattr(download_module, "download_venue", fake_download)
    code = download_module.main(["--venues", "bybit", "broken", "gate", "--root", str(tmp_path),
                                 "--datasets", "funding", "--raw-backfill"])
    assert code == 1 and seen == [("bybit", True), ("broken", True), ("gate", True)]
    assert "FAILED broken * venue: RuntimeError: venue down" in capsys.readouterr().out


# -- live (opt-in: pytest -m live) ------------------------------------------------------------------------

BTC = {"bybit": "BTCUSDT", "okx": "BTC-USDT-SWAP", "bitget": "BTCUSDT", "gate": "BTC_USDT"}


@pytest.mark.live
@pytest.mark.parametrize("name", sorted(BTC))
def test_live_venue(name):
    pytest.importorskip("ccxt")
    venue = get_venue(name, rate=2)
    build = resolve(name, venue.instruments())
    assert len(build.instruments) > 300 and BTC[name] in set(build.instruments["venue_symbol"])
    now = pd.Timestamp.now(tz="UTC").tz_localize(None)
    events = venue.funding(BTC[name], now - pd.Timedelta(days=2), now)
    assert len(events) >= 4 and events["rate"].abs().max() < 0.01
    oi = venue.open_interest([BTC[name]])
    assert one_side_usd(oi).iloc[0] > 1e8
    assert venue.prices([BTC[name]])[["mark_price", "index_price", "last_price"]].gt(0).all(axis=None)
