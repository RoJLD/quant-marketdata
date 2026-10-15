"""The Binance and Hyperliquid venue adapters reproduce the v0.2 loaders' daily means (spec section 3.7).

Both sides run against the same recorded payloads, served by one fake HTTP endpoint that behaves like the
real API (oldest page from ``startTime``): the v0.2 loaders through ``urllib.request.urlopen``, the
adapters through ``JsonClient``. The loaders are not modified.
"""

from __future__ import annotations

import io
import json
import urllib.parse
import urllib.request
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata import load_binance_funding, load_hl_funding
from quant_marketdata.venues import Dataset, load_funding
from quant_marketdata.venues.binance import BASE_URL as BINANCE_URL
from quant_marketdata.venues.binance import BinanceVenue
from quant_marketdata.venues.download import DownloadPlan, download_venue
from quant_marketdata.venues.http import JsonClient
from quant_marketdata.venues.hyperliquid import BASE_URL as HL_URL
from quant_marketdata.venues.hyperliquid import HyperliquidVenue

FIXTURES = Path(__file__).parent / "fixtures"
BINANCE_RECORDS = json.loads((FIXTURES / "binance" / "funding_btc_2024.json").read_text())
HL_RECORDS = json.loads((FIXTURES / "hyperliquid" / "funding_btc_2024.json").read_text())
START, END = pd.Timestamp("2024-01-01"), pd.Timestamp("2024-01-11")


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def fake_urlopen(request, timeout=None):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    parsed = urllib.parse.urlparse(url)
    if parsed.netloc == "fapi.binance.com" and parsed.path.endswith("/fundingRate"):
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        start, end = int(query["startTime"]), int(query.get("endTime", 2**63))
        hits = [r for r in BINANCE_RECORDS if start <= r["fundingTime"] <= end]
        body = hits[: int(query.get("limit", 100))]
    elif parsed.netloc == "fapi.binance.com" and parsed.path.endswith("/fundingInfo"):
        body = []  # BTCUSDT is on the default 8h
    elif parsed.netloc == "fapi.binance.com" and parsed.path.endswith("/exchangeInfo"):
        body = json.loads((FIXTURES / "binance" / "exchange_info.json").read_text(encoding="utf-8"))
    elif parsed.netloc == "api.hyperliquid.xyz" and parsed.path == "/info":
        payload = json.loads(request.data)
        if payload["type"] == "meta":
            body = json.loads((FIXTURES / "hyperliquid" / "meta.json").read_text())
        else:
            assert payload["type"] == "fundingHistory" and payload["coin"] == "BTC"
            start, end = payload["startTime"], payload.get("endTime", 2**63)
            body = [r for r in HL_RECORDS if start <= r["time"] <= end][:500]
    else:
        raise AssertionError(f"unexpected request {url}")
    return _Response(json.dumps(body).encode())


def _client(base_url):
    return JsonClient(base_url, rate=1e6, opener=fake_urlopen, sleep=lambda _: None)


def _daily(events: pd.DataFrame) -> pd.Series:
    return events["rate"].resample("D").mean().dropna().rename("funding_rate")


@pytest.fixture
def served(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)


def test_binance_adapter_matches_v02_daily_means(served, tmp_path):
    legacy = load_binance_funding("BTCUSDT", start=str(START.date()), use_cache=False, cache_dir=tmp_path)
    events = BinanceVenue(_client(BINANCE_URL)).funding("BTCUSDT", START, END)
    assert len(legacy) == 11 and len(events) == len(BINANCE_RECORDS)
    pd.testing.assert_series_equal(_daily(events), legacy, check_freq=False, check_index_type=False,
                                   check_names=False)  # index named ts vs t
    assert events["rate_1h"].mul(8).equals(events["rate"])  # 8h book: rate_1h = rate / 8


def test_hyperliquid_adapter_matches_v02_daily_means(served, tmp_path):
    legacy = load_hl_funding("BTC", start=str(START.date()), use_cache=False, cache_dir=tmp_path)
    events = HyperliquidVenue(_client(HL_URL)).funding("BTC", START, END)
    assert len(legacy) == 3 and len(events) == len(HL_RECORDS)
    pd.testing.assert_series_equal(_daily(events), legacy, check_freq=False, check_index_type=False,
                                   check_names=False)  # index named ts vs t
    assert events["rate_1h"].equals(events["rate"])  # hourly book


def test_two_venues_meet_in_the_registry(tmp_path, monkeypatch):
    monkeypatch.setattr("quant_marketdata.venues.download._now", lambda: END)
    for venue, symbol in ((BinanceVenue(_client(BINANCE_URL)), "BTCUSDT"),
                          (HyperliquidVenue(_client(HL_URL)), "BTC")):
        plan = DownloadPlan(venue=venue.name, symbols=[symbol], datasets=[Dataset.FUNDING], since=START,
                            specs=False)
        assert not [r for r in download_venue(plan, root=tmp_path, venue=venue) if r.error]
    funding = load_funding("BTC-USD-PERP", root=tmp_path)
    assert set(funding["venue"]) == {"binance", "hyperliquid"}
    hourly = funding.groupby("venue")["rate_1h"].mean()
    assert 0.1 < hourly["binance"] / hourly["hyperliquid"] < 10  # same unit: a fraction per hour
