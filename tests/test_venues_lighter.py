"""Lighter adapter on recorded payloads, plus the instrument registry and the download loop (no network)."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import Dataset, UnsupportedDataset, get_venue, load_funding
from quant_marketdata.venues.download import DownloadPlan, download_venue
from quant_marketdata.venues.lighter import (
    LighterVenue,
    parse_candles,
    parse_daily_metrics,
    parse_fundings,
)
from quant_marketdata.venues.registry import parse_lighter, resolve, update_registry
from quant_marketdata.venues.store import VenueStore

FIXTURES = Path(__file__).parent / "fixtures" / "lighter"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeClient:
    """Answers like the Lighter API from fixtures, honouring the window and the count_back quirk."""

    def __init__(self, mark_from=None):
        self.calls = []
        self.mark_from = mark_from

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "orderBookDetails":
            return _fixture("order_book_details")
        if path == "fundings":
            return _fixture("fundings")
        if path == "exchangeMetrics":
            return _fixture("metrics_oi")
        if path in ("candles", "markPriceCandles"):
            step = {"1h": 3600, "1d": 86400}[params["resolution"]]
            start = params["end_timestamp"] - (params["count_back"] - 1) * step
            start -= start % step
            if path == "markPriceCandles" and self.mark_from is not None:
                start = max(start, self.mark_from)
            stamps = range(start, params["end_timestamp"] + 1, step)
            return {"code": 200, "c": [{"t": t * 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 3, "V": 4,
                                        "i": 7, "sc": 9} for t in stamps]}
        raise AssertionError(f"unexpected path {path}")


# -- parsers -----------------------------------------------------------------------------------------


def test_parse_fundings_signs_and_scales_percent():
    frame = parse_fundings(_fixture("fundings")["fundings"])
    assert frame["rate_raw"].iloc[0] == "0.0012"
    assert frame["rate"].iloc[0] == pytest.approx(0.0012 / 100)
    short = parse_fundings([{"timestamp": 1792051200, "rate": "0.0012", "direction": "short", "value": "1"}])
    assert short["rate"].iloc[0] == pytest.approx(-1.2e-5)
    with pytest.raises(ValueError):
        parse_fundings([{"timestamp": 1, "rate": "0.1", "direction": "sideways", "value": "0"}])


def test_parse_candles_last_and_mark():
    last = parse_candles(_fixture("candles")["c"], "last")
    assert {"open", "close", "volume_base", "volume_quote", "last_trade_id"} <= set(last.columns)
    assert (last["price_type"] == "last").all() and last.index.is_monotonic_increasing
    mark = parse_candles(_fixture("mark_candles")["c"], "mark")
    assert "sample_count" in mark.columns and mark["volume_base"].isna().all()


def test_parse_daily_metrics_drops_padding():
    oi = pd.Series({pd.Timestamp("2025-01-01"): 0.0, pd.Timestamp("2025-01-02"): 5.0})
    volume = pd.Series({pd.Timestamp("2025-01-01"): 0.0, pd.Timestamp("2025-01-02"): 0.0})
    frame = parse_daily_metrics(oi, volume)
    assert frame.index.tolist() == [pd.Timestamp("2025-01-02")]
    assert frame["oi_convention"].iloc[0] == "both_sides"
    assert parse_daily_metrics(oi * 0, volume).empty


# -- adapter -----------------------------------------------------------------------------------------


def test_registered_and_capabilities():
    venue = get_venue("lighter", client=FakeClient())
    assert isinstance(venue, LighterVenue)
    assert Dataset.FUNDING in venue.capabilities and "12h" in venue.resolutions
    with pytest.raises(ValueError):
        get_venue("nowhere")
    venue.capabilities = frozenset({Dataset.FUNDING})
    with pytest.raises(UnsupportedDataset):
        venue.daily_metrics("BTC")


def test_candles_size_count_back_to_the_window():
    client = FakeClient()
    venue = LighterVenue(client)
    start, end = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-01 10:00")
    frame = venue.candles("BTC", "1h", start, end)
    assert len(frame) == 11 and frame.index.min() == start and frame.index.max() == end
    params = [p for path, p in client.calls if path == "candles"]
    assert params[0]["count_back"] == 11  # not 500: the API would reach back before `start`
    with pytest.raises(ValueError):
        venue.candles("BTC", "2h", start, end)
    with pytest.raises(ValueError):
        venue.candles("NOPE", "1h", start, end)


def test_mark_candles_skip_the_empty_history_prefix():
    mark_from = int(pd.Timestamp("2026-03-01").timestamp())
    client = FakeClient(mark_from=mark_from)
    venue = LighterVenue(client)
    frame = venue.candles("BTC", "1h", pd.Timestamp("2025-01-01"), pd.Timestamp("2026-03-02"), price="mark")
    hourly = [p for path, p in client.calls if path == "markPriceCandles" and p["resolution"] == "1h"]
    assert frame.index.min() == pd.Timestamp("2026-03-01") and len(frame) == 25
    assert len(hourly) == 1  # paging started at the first day with data, not at 2025-01-01


def test_short_mark_range_skips_the_daily_probe():
    # Incremental updates span minutes; a 1d probe over them is rejected by the API.
    client = FakeClient()
    venue = LighterVenue(client)
    frame = venue.candles("BTC", "1h", pd.Timestamp("2026-03-01 10:00"), pd.Timestamp("2026-03-01 10:08"),
                          price="mark")
    assert len(frame) == 1
    assert all(p["resolution"] == "1h" for path, p in client.calls if path == "markPriceCandles")


def test_funding_is_canonical():
    venue = LighterVenue(FakeClient())
    frame = venue.funding("BTC", pd.Timestamp(1792051200, unit="s"), pd.Timestamp(1792069200, unit="s"))
    assert frame["interval_h"].eq(1).all()
    assert frame["rate_1h"].iloc[0] == pytest.approx(1.2e-5)
    assert frame["apr"].iloc[0] == pytest.approx(1.2e-5 * 24 * 365)


# -- registry ----------------------------------------------------------------------------------------


def test_parse_lighter_rules():
    assert parse_lighter("BTC")["base"] == "BTC"
    bonk = parse_lighter("1000BONK")
    assert (bonk["base"], bonk["multiplier"]) == ("BONK", 1000.0)
    fx = parse_lighter("EURUSD")
    assert (fx["base"], fx["quote"], fx["asset_class"]) == ("EUR", "USD", "fx")
    assert parse_lighter("SAMSUNGUSD") is None


def test_resolve_lighter_snapshot():
    build = resolve("lighter", LighterVenue(FakeClient()).instruments())
    by_symbol = build.instruments.set_index("venue_symbol")
    assert by_symbol.loc["BTC", "instrument_id"] == "BTC-USD-PERP"
    assert by_symbol.loc["BTC", "mapping_source"] == "rule-default-class"
    assert by_symbol.loc["1000BONK", "instrument_id"] == "BONK-USD-PERP"
    assert by_symbol.loc["XAU", "asset_class"] == "commodity"
    assert by_symbol.loc["XAU", "mapping_source"] == "override"
    assert by_symbol.loc["EURUSD", "instrument_id"] == "EUR-USD-PERP"
    assert by_symbol.loc["BTC", "settle_asset"] == "USDC"
    assert build.unresolved["venue_symbol"].tolist() == ["SAMSUNGUSD"]
    assert build.instruments["session"].eq("24x7").all()  # trading_hours declared empty


def test_lighter_spx_is_the_memecoin_not_the_index():
    snapshot = pd.DataFrame({"venue_symbol": ["SPX", "US500", "US100"], "venue": "lighter", "status": "active"})
    rows = resolve("lighter", snapshot).instruments.set_index("venue_symbol")
    spx = rows.loc["SPX"]
    assert (spx["instrument_id"], spx["asset_class"], spx["mapping_source"]) == ("SPX-USD-PERP", "crypto", "override")
    assert pd.isna(spx["underlying_ref"])
    assert rows.loc["US500", "asset_class"] == "index" and rows.loc["US500", "underlying_ref"] == "US500"


def test_resolve_reports_collisions():
    snapshot = pd.DataFrame({"venue_symbol": ["BONK", "1000BONK"], "venue": "lighter", "status": "active"})
    build = resolve("lighter", snapshot)
    assert build.instruments.empty
    assert set(build.unresolved["venue_symbol"]) == {"BONK", "1000BONK"}


def test_update_registry_marks_delisting_and_keeps_first_listing():
    day1, day2 = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-02-01")
    snap = pd.DataFrame({"venue": "lighter", "venue_symbol": ["BTC", "OLD"], "status": "active",
                         "listed_at": [pd.Timestamp("2025-01-01"), pd.Timestamp("2025-06-01")]})
    registry = update_registry(pd.DataFrame(), resolve("lighter", snap), "lighter", day1)
    later = snap[snap["venue_symbol"] == "BTC"].assign(listed_at=pd.Timestamp("2025-03-01"))
    registry = update_registry(registry, resolve("lighter", later), "lighter", day2)
    rows = registry.set_index("venue_symbol")
    assert rows.loc["OLD", "status"] == "delisted" and rows.loc["OLD", "delisted_at"] == day2
    assert rows.loc["BTC", "listed_at"] == pd.Timestamp("2025-01-01")


# -- download loop -----------------------------------------------------------------------------------


def test_download_is_incremental_and_isolates_failures(tmp_path, monkeypatch):
    now = [pd.Timestamp("2026-01-02")]
    monkeypatch.setattr("quant_marketdata.venues.download._now", lambda: now[0])
    venue = LighterVenue(FakeClient())
    plan = DownloadPlan(venue="lighter", symbols=["BTC", "XAU"], datasets=[Dataset.CANDLES],
                        resolutions=["1h"], since=pd.Timestamp("2026-01-01"))
    original = venue.candles

    def flaky(symbol, *args, **kwargs):
        if symbol == "XAU":
            raise RuntimeError("boom")
        return original(symbol, *args, **kwargs)

    venue.candles = flaky
    results = download_venue(plan, root=tmp_path, venue=venue)
    by_symbol = {r.symbol: r for r in results if r.dataset == "candles/1h"}
    assert by_symbol["BTC"].new_rows == 25 and by_symbol["XAU"].error.startswith("RuntimeError")
    assert any(r.dataset == "specs" for r in results)

    now[0] = pd.Timestamp("2026-01-02 05:00")
    again = download_venue(plan, root=tmp_path, venue=venue)
    assert next(r for r in again if r.symbol == "BTC" and r.dataset == "candles/1h").new_rows == 5
    specs = VenueStore.read(VenueStore(tmp_path).path("lighter", Dataset.SPECS))
    assert len(specs) == len(_fixture("order_book_details")["order_book_details"])  # one row per day


def test_load_funding_across_the_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("quant_marketdata.venues.download._now", lambda: pd.Timestamp(1792069200, unit="s"))
    venue = LighterVenue(FakeClient())
    plan = DownloadPlan(venue="lighter", symbols=["BTC"], datasets=[Dataset.FUNDING],
                        since=pd.Timestamp(1792051200, unit="s"))
    download_venue(plan, root=tmp_path, venue=venue)
    funding = load_funding("BTC-USD-PERP", root=tmp_path)
    assert set(funding["venue"]) == {"lighter"} and funding["rate_1h"].notna().all()


@pytest.mark.live
def test_live_lighter_btc_one_day(tmp_path):
    venue = get_venue("lighter")
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    funding = venue.funding("BTC", end - pd.Timedelta(days=1), end)
    candles = venue.candles("BTC", "1h", end - pd.Timedelta(days=1), end)
    assert 20 <= len(funding) <= 25 and funding["rate_1h"].abs().max() < 1e-3
    assert 20 <= len(candles) <= 25
