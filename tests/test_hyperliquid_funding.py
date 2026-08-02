"""Hyperliquid funding-rate loader -- pure parse/daily-mean of hourly funding (no network)."""

from __future__ import annotations

import pandas as pd
import pytest

from quant_marketdata.hyperliquid_funding import _daily_funding, _parse_funding


def _rec(ts_ms, rate):
    return {"coin": "BTC", "fundingRate": str(rate), "premium": "0.0001", "time": ts_ms}


def test_parse_funding_basic():
    recs = [_rec(1748001600000, 0.0000368), _rec(1748005200000, 0.0000422)]
    df = _parse_funding(recs)
    assert list(df.columns) == ["funding_rate"]
    assert df["funding_rate"].iloc[0] == pytest.approx(0.0000368)
    assert df.index.is_monotonic_increasing


def test_parse_funding_empty():
    df = _parse_funding([])
    assert df.empty and list(df.columns) == ["funding_rate"]


def test_daily_funding_means_hourly():
    # 24 hourly readings on one UTC day -> one daily row = their mean
    base = 1748044800000  # 2025-05-24 00:00 UTC
    recs = [_rec(base + i * 3_600_000, 0.001 if i % 2 == 0 else 0.003) for i in range(24)]
    daily = _daily_funding(_parse_funding(recs))
    assert len(daily) == 1
    assert daily.iloc[0] == pytest.approx(0.002)  # mean of alternating 0.001 / 0.003
    assert (daily.index == daily.index.normalize()).all()


def test_daily_funding_empty():
    assert _daily_funding(pd.DataFrame(columns=["funding_rate"])).empty
