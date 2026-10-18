"""Paradex adapter on recorded payloads (2026-10-18), plus its registry rule and overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import UnsupportedDataset, get_venue
from quant_marketdata.venues.paradex import (
    FIRST_EVENT,
    KLINE_PAGE,
    ParadexVenue,
    parse_funding_samples,
    parse_klines,
)
from quant_marketdata.venues.registry import parse_paradex, resolve
from quant_marketdata.venues.schema import FUNDING_COLUMNS

FIXTURES = Path(__file__).parent / "fixtures" / "paradex"
HOUR_MS = 3_600_000


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeParadex:
    """Answers like Paradex: ``funding/data`` newest first within ``[start_at, end_at]``; klines oldest
    first in ``[start_at, end_at)``."""

    def __init__(self, funding=None):
        self.funding = funding if funding is not None else _fixture("funding_samples_btc")
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "markets":
            return _fixture("markets")
        if path == "funding/data":
            hits = [r for r in self.funding if params["start_at"] <= r["created_at"] <= params["end_at"]]
            hits.sort(key=lambda r: -r["created_at"])
            return {"next": None, "prev": None, "results": hits[: params.get("page_size", 100)]}
        if path == "markets/klines":
            assert (params["end_at"] - params["start_at"]) // (params["resolution"] * 60_000) <= 1440
            step = params["resolution"] * 60_000
            first = params["start_at"] + (-params["start_at"]) % step
            return {"results": [[t, 1, 2, 0.5, 1.5, 3] for t in range(first, params["end_at"], step)]}
        if path.startswith("orderbook/"):
            return _fixture("orderbook")
        raise AssertionError(f"unexpected path {path}")


def _record(ts, index, rate, price=50_000.0):
    return {"market": "BTC-USD-PERP", "created_at": int(pd.Timestamp(ts).value // 10**6) - 3000,
            "funding_index": str(index), "funding_premium": str(rate * price), "funding_rate": str(rate),
            "funding_rate_8h": str(rate), "funding_period_hours": 8}


# -- funding ---------------------------------------------------------------------------------------


def test_hourly_events_from_index_samples():
    events = parse_funding_samples(_fixture("funding_samples_btc"))
    assert list(events.columns) == FUNDING_COLUMNS + ["funding_index_raw"]
    assert len(events) == 12 and events.index[0] == pd.Timestamp("2026-10-15 01:00")
    assert events["interval_h"].eq(1).all() and events["direction_raw"].isna().all()
    # The hour's index increment over the price tracks the quoted 8h rate / 8 sampled at both ends of the
    # hour (an EWMA that moves within the hour; the exact identity is tested on raw records below).
    quoted = events["rate_raw"].astype(float) / 8
    ratio = (events["rate_1h"] / ((quoted + quoted.shift()) / 2)).dropna()
    assert ratio.between(0.85, 1.15).all() and ratio.median() == pytest.approx(1, abs=0.02)
    assert events["rate"].gt(0).all() and events["apr"].iloc[0] == pytest.approx(events["rate_1h"].iloc[0] * 8760)


def test_index_increments_are_the_time_integral_of_the_8h_rate():
    # Five minutes of raw records: the index accrues premium * dt / period, and over the spot price that
    # is the time-average of funding_rate_8h / 8h (what the events rely on).
    records = pd.DataFrame(_fixture("funding_window_btc")).sort_values("created_at")
    for column in ("funding_index", "funding_premium", "funding_rate", "funding_rate_8h"):
        records[column] = records[column].astype(float)
    dt = records["created_at"].diff().fillna(0) / 1000
    step = records["funding_index"].iloc[-1] - records["funding_index"].iloc[0]
    premium = (records["funding_premium"].shift() * dt / (8 * 3600)).sum()
    assert step == pytest.approx(premium, rel=1e-4)
    price = (records["funding_premium"] / records["funding_rate"]).iloc[[0, -1]].mean()
    quoted = (records["funding_rate_8h"].shift() * dt / (8 * 3600)).sum()
    assert step / price == pytest.approx(quoted, rel=5e-3)


def test_sign_rising_index_longs_pay():
    up = parse_funding_samples([_record("2026-01-01 00:00", 100.0, 1e-4),
                                _record("2026-01-01 01:00", 100.625, 1e-4)])
    assert up["rate"].iloc[0] == pytest.approx(0.625 / 50_000)  # 1e-4 per 8h, i.e. 1.25e-5 per hour
    down = parse_funding_samples([_record("2026-01-01 00:00", 100.0, -1e-4),
                                  _record("2026-01-01 01:00", 99.375, -1e-4)])
    assert down["rate"].iloc[0] == pytest.approx(-1.25e-5)  # shorts pay


def test_missing_hour_merges_into_the_next_event_and_zero_rates_are_kept():
    samples = [_record("2026-01-01 00:00", 100.0, 1e-4), _record("2026-01-01 01:00", 100.625, 1e-4),
               _record("2026-01-01 03:00", 101.875, 1e-4)]  # 02:00 missing (outage)
    events = parse_funding_samples(samples)
    assert events["interval_h"].tolist() == [1.0, 2.0]
    assert events["rate_1h"].tolist() == pytest.approx([1.25e-5, 1.25e-5])
    flat = parse_funding_samples([_record("2026-01-01 00:00", 100.0, 0.0), _record("2026-01-01 01:00", 100.0, 0.0)])
    assert flat["rate"].tolist() == [0.0]  # no price to divide by, but nothing accrued


def test_history_before_the_consistent_index_is_not_served():
    # 2024-03-12: the 06:00 sample still has funding_index and funding_premium swapped.
    events = parse_funding_samples(_fixture("funding_samples_cutoff"))
    assert events.index.tolist() == [FIRST_EVENT, FIRST_EVENT + pd.Timedelta(hours=1)]
    assert events["rate_1h"].between(1e-6, 1e-3).all()
    client = FakeParadex(funding=_fixture("funding_samples_cutoff"))
    frame = ParadexVenue(client).funding("BTC-USD-PERP", pd.Timestamp("2024-03-01"), pd.Timestamp("2024-03-12 09:00"))
    assert frame.index.tolist() == events.index.tolist()
    first_sample = min(p["end_at"] for path, p in client.calls if path == "funding/data")
    assert first_sample == int((FIRST_EVENT - pd.Timedelta(hours=1)).value // 10**6)
    assert ParadexVenue(FakeParadex()).funding("BTC-USD-PERP", pd.Timestamp("2023-01-01"),
                                               pd.Timestamp("2024-01-01")).empty


def test_funding_samples_every_hour_plus_the_previous_one():
    client = FakeParadex()
    frame = ParadexVenue(client).funding("BTC-USD-PERP", pd.Timestamp("2026-10-15 01:30"),
                                         pd.Timestamp("2026-10-15 05:10"))
    assert frame.index.tolist() == list(pd.date_range("2026-10-15 02:00", "2026-10-15 05:00", freq="h"))
    calls = [p for path, p in client.calls if path == "funding/data"]
    assert len(calls) == 5 and all(p["page_size"] == 1 for p in calls)
    assert all(p["end_at"] - p["start_at"] == HOUR_MS - 1 for p in calls)


# -- candles, book, instruments --------------------------------------------------------------------


def test_parse_klines_last_and_mark():
    last = parse_klines(_fixture("klines")["results"], "last")
    assert last.index.is_monotonic_increasing and last["volume_quote"].gt(0).all()
    assert last["volume_base"].isna().all() and last.index[0] == pd.Timestamp("2026-10-15")
    mark = parse_klines(_fixture("mark_klines")["results"], "mark")
    assert mark["volume_quote"].isna().all() and (mark["price_type"] == "mark").all()


def test_candles_page_under_the_bar_cap_and_include_the_end():
    client = FakeParadex()
    venue = ParadexVenue(client)
    start, end = pd.Timestamp("2026-10-01"), pd.Timestamp("2026-10-03")
    frame = venue.candles("BTC-USD-PERP", "1m", start, end)
    assert len(frame) == 2 * 1440 + 1 and frame.index.min() == start and frame.index.max() == end
    assert len([c for c in client.calls if c[0] == "markets/klines"]) == 3  # 2880 bars / 1000
    assert all(p["price_kind"] == "last" for path, p in client.calls if path == "markets/klines")
    assert KLINE_PAGE < 1440
    venue.candles("BTC-USD-PERP", "1h", start, end, price="mark")
    assert client.calls[-1][1]["price_kind"] == "mark" and client.calls[-1][1]["resolution"] == 60
    with pytest.raises(ValueError, match="Unsupported resolution"):
        venue.candles("BTC-USD-PERP", "4h", start, end)


def test_order_book_best_first_and_depth_capped():
    client = FakeParadex()
    book = ParadexVenue(client).order_book("BTC-USD-PERP", depth=500)
    assert client.calls[-1][1]["depth"] == 100
    assert book["bids"][0][0] > book["bids"][-1][0] and book["asks"][0][0] < book["asks"][-1][0]
    assert book["bids"][0][0] < book["asks"][0][0] and book["venue"] == "paradex"
    assert book["ts"] == pd.Timestamp(_fixture("orderbook")["last_updated_at"], unit="ms")


def test_registered_capabilities_and_pacing():
    venue = get_venue("paradex", rate=100)
    assert venue.client.limiter._interval == pytest.approx(1 / 20)
    with pytest.raises(UnsupportedDataset):
        venue.daily_metrics("BTC-USD-PERP")


def test_instruments_and_registry():
    snapshot = ParadexVenue(FakeParadex()).instruments().set_index("venue_symbol")
    assert set(snapshot.index) == {"BTC-USD-PERP", "ETH-USD-PERP", "kPEPE-USD-PERP", "NVDA-USD-PERP",
                                   "XAU-USD-PERP", "CL-USD-PERP", "PAXG-USD-PERP", "JTO-USD-PERP",
                                   "US500-USD-PERP", "EWY-USD-PERP"}  # no option, no spot
    assert snapshot["funding_interval_h"].eq(1).all() and snapshot["settle_asset"].eq("USDC").all()
    assert snapshot.loc["BTC-USD-PERP", "listed_at"] == pd.Timestamp(1696431006398, unit="ms")
    assert snapshot.loc["BTC-USD-PERP", "asset_class"] == "crypto" and snapshot["session"].isna().all()
    build = resolve("paradex", snapshot.reset_index())
    assert build.ok
    rows = build.instruments.set_index("venue_symbol")
    assert rows.loc["BTC-USD-PERP", "instrument_id"] == "BTC-USD-PERP"
    assert rows.loc["BTC-USD-PERP", "mapping_source"] == "rule"  # tagged LAYER-1
    assert rows.loc["JTO-USD-PERP", "mapping_source"] == "rule-default-class"  # untagged
    assert rows.loc["kPEPE-USD-PERP", "instrument_id"] == "PEPE-USD-PERP"
    assert rows.loc["kPEPE-USD-PERP", "multiplier"] == 1000
    assert rows.loc["NVDA-USD-PERP", "asset_class"] == "equity"
    assert rows.loc["NVDA-USD-PERP", "underlying_ref"] == "NVDA"
    assert rows.loc["XAU-USD-PERP", "asset_class"] == "commodity"
    assert rows.loc["CL-USD-PERP", "instrument_id"] == "CL-USD-PERP"
    assert rows.loc["US500-USD-PERP", "asset_class"] == "index"
    assert rows.loc["EWY-USD-PERP", "asset_class"] == "etf"
    assert rows.loc["PAXG-USD-PERP", "asset_class"] == "crypto"
    assert rows.loc["PAXG-USD-PERP", "underlying_ref"] == "XAU"
    assert parse_paradex("ETH-USD") is None and parse_paradex("BTC-USD-25DEC26-150000-P") is None


@pytest.mark.live
def test_live_paradex_btc_one_day():
    venue = get_venue("paradex", rate=20)
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    funding = venue.funding("BTC-USD-PERP", end - pd.Timedelta(days=1), end)
    assert 22 <= len(funding) <= 25 and funding["interval_h"].eq(1).all()
    assert funding["rate_1h"].abs().max() < 1e-3
    quoted = funding["rate_raw"].astype(float) / 8
    ratio = funding["rate_1h"] / ((quoted + quoted.shift()) / 2)
    assert ratio.dropna().median() == pytest.approx(1, abs=0.05)  # tracks the quoted 8h rate / 8
    candles = venue.candles("BTC-USD-PERP", "1h", end - pd.Timedelta(days=1), end)
    mark = venue.candles("BTC-USD-PERP", "1h", end - pd.Timedelta(days=1), end, price="mark")
    assert 20 <= len(candles) <= 25 and 23 <= len(mark) <= 25
    book = venue.order_book("BTC-USD-PERP", depth=20)
    assert book["bids"] and book["asks"] and book["bids"][0][0] < book["asks"][0][0]
    assert resolve("paradex", venue.instruments()).instruments["instrument_id"].eq("BTC-USD-PERP").any()
