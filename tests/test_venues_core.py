"""Venue layer core: funding normalization, hourly grids, HTTP pacing/retries, pagination, store (no network)."""

from __future__ import annotations

import io
import json
import urllib.error

import pandas as pd
import pytest

from quant_marketdata.venues.http import HttpError, JsonClient, RateLimiter
from quant_marketdata.venues.paging import first_non_empty, paged_fetch, window_count, windows
from quant_marketdata.venues.schema import FUNDING_COLUMNS, Dataset, finalize_funding, funding_grid
from quant_marketdata.venues.store import ROOT_ENV, VenueStore, resolve_root


def _events(stamps, rates):
    index = pd.DatetimeIndex(pd.to_datetime(stamps, format="ISO8601"), name="ts")
    return pd.DataFrame({"rate_raw": [str(r) for r in rates], "direction_raw": "x", "rate": rates},
                        index=index)


# -- funding normalization -------------------------------------------------------------------------


def test_finalize_funding_hourly_rate_and_apr_for_8h_venue():
    events = _events(["2026-01-01 00:00", "2026-01-01 08:00:02", "2026-01-01 16:00"], [8e-4, 8e-4, -8e-4])
    out = finalize_funding(events, nominal_interval_h=8)
    assert list(out.columns) == FUNDING_COLUMNS
    assert out["interval_h"].tolist() == [8, 8, 8]  # late stamp absorbed, first gets the nominal
    assert out["rate_1h"].tolist() == pytest.approx([1e-4, 1e-4, -1e-4])
    assert out["apr"].iloc[0] == pytest.approx(1e-4 * 24 * 365)


def test_finalize_funding_caps_gaps_and_follows_interval_changes():
    # A missing 08:00 event must not double the next hourly rate; a switch to 4h shows up as 4h.
    events = _events(["2026-01-01 00:00", "2026-01-01 16:00", "2026-01-01 20:00"], [8e-4, 8e-4, 4e-4])
    out = finalize_funding(events, nominal_interval_h=8)
    assert out["interval_h"].tolist() == [8, 8, 4]
    assert out["rate_1h"].tolist() == pytest.approx([1e-4, 1e-4, 1e-4])


def test_finalize_funding_empty():
    out = finalize_funding(pd.DataFrame(), nominal_interval_h=1)
    assert out.empty and list(out.columns) == FUNDING_COLUMNS


def test_funding_grid_settlement_vs_accrual():
    events = finalize_funding(_events(["2026-01-01 08:00", "2026-01-01 16:00"], [8e-4, 4e-4]), 8)
    settlement = funding_grid(events, "settlement")
    assert settlement.loc["2026-01-01 08:00"] == pytest.approx(8e-4)
    assert settlement.loc["2026-01-01 12:00"] == 0
    assert settlement.sum() == pytest.approx(12e-4)
    accrual = funding_grid(events, "accrual")
    assert accrual.loc["2026-01-01 01:00"] == pytest.approx(1e-4)
    assert accrual.loc["2026-01-01 09:00"] == pytest.approx(0.5e-4)
    assert accrual.sum() == pytest.approx(12e-4)  # same total cash, spread over the hours it accrued
    with pytest.raises(ValueError):
        funding_grid(events, "daily")


# -- HTTP --------------------------------------------------------------------------------------------


def test_rate_limiter_spaces_calls():
    now = [0.0]
    slept = []

    def sleep(delay):
        slept.append(delay)
        now[0] += delay

    limiter = RateLimiter(2.0, clock=lambda: now[0], sleep=sleep)
    for _ in range(3):
        limiter.wait()
    assert slept == pytest.approx([0.5, 0.5])


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code):
    return urllib.error.HTTPError("https://x/y", code, "err", {}, io.BytesIO(b'{"message":"nope"}'))


def test_json_client_retries_rate_limits_then_succeeds():
    calls, slept = [], []

    def opener(request, timeout):
        calls.append(request.full_url)
        if len(calls) < 3:
            raise _http_error(429)
        return _Response(json.dumps({"ok": True}).encode())

    client = JsonClient("https://x", rate=1000, sleep=slept.append, opener=opener)
    assert client.get("y", {"a": 1, "flag": True, "skip": None}) == {"ok": True}
    assert calls[0] == "https://x/y?a=1&flag=true"
    assert [s for s in slept if s >= 1] == [2.0, 4.0]


def test_json_client_does_not_retry_client_errors():
    calls = []

    def opener(request, timeout):
        calls.append(1)
        raise _http_error(400)

    client = JsonClient("https://x", rate=1000, sleep=lambda s: None, opener=opener)
    with pytest.raises(HttpError) as info:
        client.get("y")
    assert info.value.status == 400 and "nope" in info.value.body
    assert len(calls) == 1


# -- pagination --------------------------------------------------------------------------------------


def test_windows_are_contiguous_and_cover_the_range():
    spans = list(windows(0, 1050, 500))
    assert spans == [(0, 500), (500, 1000), (1000, 1050)]
    with pytest.raises(ValueError):
        list(windows(0, 10, 0))


def test_paged_fetch_deduplicates_shared_boundaries():
    def fetch(a, b):
        seconds = list(range(a, b + 1, 60))
        return pd.DataFrame({"v": seconds}, index=pd.to_datetime(seconds, unit="s"))

    frame = paged_fetch(fetch, 0, 600, 300)
    assert len(frame) == 11 and frame.index.is_unique and frame.index.is_monotonic_increasing


def test_first_non_empty_and_window_count():
    def fetch(a, b):
        return pd.DataFrame({"v": [1]}, index=pd.to_datetime([a], unit="s")) if a >= 1000 else pd.DataFrame()

    assert first_non_empty(fetch, 0, 2000, 500) == pd.Timestamp(1000, unit="s")
    assert first_non_empty(fetch, 0, 900, 500) is None
    assert window_count(0, 3600, 60, 500) == 61
    assert window_count(0, 500 * 3600, 3600, 500) == 500


# -- store -------------------------------------------------------------------------------------------


def test_store_paths_and_root_resolution(tmp_path, monkeypatch):
    store = VenueStore(tmp_path)
    assert store.path("lighter", Dataset.FUNDING, "BTC") == tmp_path / "venues/lighter/funding/BTC.parquet"
    assert store.path("lighter", Dataset.CANDLES, "1000BONK", "1h").parts[-3:] == ("candles", "1h",
                                                                                  "1000BONK.parquet")
    assert store.path("lighter", Dataset.SPECS) == tmp_path / "venues/lighter/specs.parquet"
    assert store.path("x", Dataset.FUNDING, "LINK/USDC").name == "LINK_USDC.parquet"
    monkeypatch.setenv(ROOT_ENV, str(tmp_path / "shared"))
    assert resolve_root() == tmp_path / "shared"
    assert resolve_root(tmp_path) == tmp_path


def test_store_merge_newer_rows_win(tmp_path):
    path = tmp_path / "s.parquet"
    first = pd.DataFrame({"close": [1.0, 2.0]}, index=pd.to_datetime([0, 60], unit="s"))
    second = pd.DataFrame({"close": [20.0, 3.0]}, index=pd.to_datetime([60, 120], unit="s"))
    VenueStore.merge(path, first)
    assert VenueStore.merge(path, second) == 3
    assert VenueStore.read(path)["close"].tolist() == [1.0, 20.0, 3.0]
    assert VenueStore.last_timestamp(path) == pd.Timestamp(120, unit="s")
    assert VenueStore.row_count(path) == 3
    assert not path.with_suffix(".parquet.tmp").exists()
