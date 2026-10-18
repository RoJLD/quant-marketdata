"""Extended adapter on recorded payloads, plus its registry rule and overrides."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import get_venue
from quant_marketdata.venues.extended import ExtendedVenue, parse_candles, parse_funding
from quant_marketdata.venues.registry import parse_extended, resolve, session_suffix

FIXTURES = Path(__file__).parent / "fixtures" / "extended"
HOUR_MS = 3_600_000


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class FakeExtended:
    """Answers like Extended: funding newest first, capped at ``cap`` events; candles by endTime + limit."""

    def __init__(self, funding=None, cap=1000):
        self.funding = funding
        self.cap = cap
        self.calls = []

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "info/markets":
            return _fixture("markets")
        if path.endswith("/funding"):
            records = self.funding if self.funding is not None else _fixture("funding")["data"]
            hits = [r for r in records if params["startTime"] <= r["T"] <= params["endTime"]]
            return {"status": "OK", "data": sorted(hits, key=lambda r: -r["T"])[: self.cap]}
        if path.startswith("info/candles/"):
            last = params["endTime"] - params["endTime"] % HOUR_MS
            stamps = [last - i * HOUR_MS for i in range(params["limit"])]
            return {"status": "OK", "data": [{"o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "3", "T": t}
                                             for t in stamps]}
        raise AssertionError(f"unexpected path {path}")


def test_parse_funding_and_candles():
    frame = parse_funding(_fixture("funding")["data"])
    assert len(frame) == 6 and frame.index.is_monotonic_increasing  # served newest first
    assert frame["rate"].iloc[-1] == pytest.approx(1.3e-5) and frame["rate_raw"].iloc[-1] == "0.000013"
    candles = parse_candles(_fixture("candles")["data"], "last")
    assert candles.index.is_monotonic_increasing and candles["volume_base"].gt(0).all()
    mark = parse_candles(_fixture("mark_candles")["data"], "mark")
    assert mark["volume_base"].isna().all()


def test_funding_windows_stay_under_the_cap():
    # Hourly events over 60 days: a 1000-event cap would silently drop the oldest of a 1440-hour window.
    start = pd.Timestamp("2026-01-01")
    events = [{"m": "BTC-USD", "f": "0.00001", "T": int((start + pd.Timedelta(hours=h)).value // 10**6) + 849}
              for h in range(60 * 24)]
    client = FakeExtended(funding=events)
    frame = ExtendedVenue(client).funding("BTC-USD", start, start + pd.Timedelta(days=60))
    assert len(frame) == 60 * 24 and frame["interval_h"].eq(1).all()
    assert frame["rate_1h"].iloc[0] == pytest.approx(1e-5)


def test_api_error_raises():
    class Failing(FakeExtended):
        def get(self, path, params=None):
            return {"status": "ERROR", "error": {"code": 1, "message": "nope"}}

    with pytest.raises(ValueError, match="Extended API error"):
        ExtendedVenue(Failing()).instruments()


def test_candles_size_the_limit_to_the_window():
    client = FakeExtended()
    start, end = pd.Timestamp("2026-01-01"), pd.Timestamp("2026-01-01 10:00")
    frame = ExtendedVenue(client).candles("BTC-USD", "1h", start, end)
    assert len(frame) == 11 and frame.index.min() == start and frame.index.max() == end
    calls = [p for path, p in client.calls if path.startswith("info/candles/")]
    assert calls[0]["limit"] == 11 and calls[0]["interval"] == "PT1H"


def test_instruments_classes_and_registry():
    venue = get_venue("extended", client=FakeExtended())
    snapshot = venue.instruments().set_index("venue_symbol")
    assert "BTCSPOT-USD" not in snapshot.index
    assert snapshot.loc["FTM-USD", "status"] == "inactive"
    assert snapshot.loc["XAU-USD", "asset_class"] == "commodity"
    assert snapshot.loc["BTC-USD", "listed_at"] == pd.Timestamp(1752829532673, unit="ms")
    build = resolve("extended", venue.instruments())
    rows = build.instruments.set_index("venue_symbol")
    assert rows.loc["BTC-USD", "instrument_id"] == "BTC-USD-PERP"
    assert rows.loc["BTC-USD", "mapping_source"] == "rule"  # Extended declares "Crypto"
    assert rows.loc["1000BONK-USD", "instrument_id"] == "BONK-USD-PERP"
    assert rows.loc["kBOME-USD", "multiplier"] == 1000
    assert rows.loc["XAU-USD", "underlying_ref"] == "XAU"
    assert rows.loc["NOW-USD", "asset_class"] == "equity"
    assert rows.loc["OPENAI-USD", "asset_class"] == "private"
    assert rows.loc["EUR-USD", "instrument_id"] == "EUR-USD-PERP"
    assert rows.loc["USDJPY-USD", "instrument_id"] == "USD-JPY-PERP"
    assert rows.loc["SPX500m-USD", "asset_class"] == "index"
    assert rows.loc["PAXG-USD", "underlying_ref"] == "XAU"
    assert parse_extended("BTC") is None
    reasons = build.unresolved.set_index("venue_symbol")["reason"]
    assert reasons.to_dict() == {
        "PLACE_JPY-USD": "delisted placeholder market (description PLACE_JPY): no known underlying"}


def test_sessions_key_the_id_never_the_name():
    snapshot = ExtendedVenue(FakeExtended()).instruments().set_index("venue_symbol")
    assert snapshot.loc["AAPL_24_5-USD", "session"] == "24x7"  # declared CONTINUOUS
    assert snapshot.loc["ADBE_24_5-USD", "session"] == "24x5"  # WEEKDAYS
    assert snapshot.loc["ADSK-USD", "session"] == "no-overnight"
    assert snapshot.loc["AAPL_24_5-USD", "underlying_ref"] == "AAPL"
    rows = resolve("extended", snapshot.reset_index()).instruments.set_index("venue_symbol")
    # A "_24_5" name trading continuously is the continuous contract, shared with the other venues.
    assert rows.loc["AAPL_24_5-USD", "instrument_id"] == "AAPL-USD-PERP"
    assert rows.loc["ADBE_24_5-USD", "instrument_id"] == "ADBE-USD-PERP-24X5"
    # Same ticker, two sessions on one venue: two instruments, not a collision.
    assert rows.loc["ADSK-USD", "instrument_id"] == "ADSK-USD-PERP-NOOVERNIGHT"
    assert rows.loc["ADSK_24_5-USD", "instrument_id"] == "ADSK-USD-PERP-24X5"
    assert rows.loc["ADSK_24_5-USD", "status"] == "inactive"
    assert rows.loc["BTC-USD", "instrument_id"] == "BTC-USD-PERP"


def test_an_unknown_session_is_reported_not_guessed():
    markets = _fixture("markets")
    btc = next(m for m in markets["data"] if m["name"] == "BTC-USD")
    btc["tradingHours"] = "WEEKENDS"

    class Renamed(FakeExtended):
        def get(self, path, params=None):
            return markets if path == "info/markets" else super().get(path, params)

    build = resolve("extended", ExtendedVenue(Renamed()).instruments())
    reasons = build.unresolved.set_index("venue_symbol")["reason"]
    assert reasons["BTC-USD"] == "unknown session 'weekends'"
    assert "BTC-USD" not in set(build.instruments["venue_symbol"])
    assert session_suffix(None) == "" and session_suffix("24x7") == "" and session_suffix("x") is None


@pytest.mark.live
def test_live_extended_btc_one_day():
    venue = get_venue("extended")
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    funding = venue.funding("BTC-USD", end - pd.Timedelta(days=1), end)
    candles = venue.candles("BTC-USD", "1h", end - pd.Timedelta(days=1), end)
    mark = venue.candles("BTC-USD", "1h", end - pd.Timedelta(days=1), end, price="mark")
    assert 22 <= len(funding) <= 26 and funding["interval_h"].eq(1).all()
    assert funding["rate_1h"].abs().max() < 1e-3
    assert 23 <= len(candles) <= 25 and 23 <= len(mark) <= 25
