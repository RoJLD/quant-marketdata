"""Binance funding-rate loader -- pure parsing/aggregation parts (no network)."""

import pandas as pd
import pytest

from quant_marketdata.binance_funding import _daily_funding, _parse_funding


def test_parse_funding_basic():
    records = [
        {"symbol": "BTCUSDT", "fundingTime": 1567296000000, "fundingRate": "0.0001"},
        {"symbol": "BTCUSDT", "fundingTime": 1567324800000, "fundingRate": "-0.0002"},
    ]
    df = _parse_funding(records)
    assert list(df.columns) == ["funding_rate"]
    assert df["funding_rate"].tolist() == [0.0001, -0.0002]
    assert isinstance(df.index, pd.DatetimeIndex)
    assert df.index.is_monotonic_increasing


def test_parse_funding_empty():
    df = _parse_funding([])
    assert df.empty and list(df.columns) == ["funding_rate"]


def test_daily_funding_means_intraday():
    # two 8h fundings on the same day -> daily mean
    idx = pd.to_datetime(["2020-01-01 00:00", "2020-01-01 08:00", "2020-01-02 00:00"])
    df = pd.DataFrame({"funding_rate": [0.0002, 0.0004, 0.0010]}, index=idx)
    s = _daily_funding(df)
    assert s.loc[pd.Timestamp("2020-01-01")] == pytest.approx(0.0003)  # mean of 0.0002, 0.0004
    assert s.loc[pd.Timestamp("2020-01-02")] == pytest.approx(0.0010)
    assert (s.index == s.index.normalize()).all()


def test_daily_funding_empty():
    s = _daily_funding(pd.DataFrame(columns=["funding_rate"]))
    assert s.empty
