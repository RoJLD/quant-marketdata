"""Cross-venue funding differential: the load-bearing annualization (CEX per-8h vs DEX per-hour -- a raw
difference is an 8x scale artifact) and the anti-look-ahead differential-carry frame."""
from __future__ import annotations

import numpy as np
import pandas as pd

from quant_marketdata.funding_diff import annualized_diff, differential_carry_frame


def test_annualized_diff_normalizes_the_8x_scale():
    idx = pd.date_range("2023-01-01", periods=50, freq="D")
    # Binance 1 bp per-8h and HL 1.25e-5 per-hour are the SAME annualized funding (10.95%/yr) despite
    # raw values differing 8x -> the annualized diff must be ~0, not the raw diff (~8.75e-5).
    fb = pd.Series(1e-4, index=idx)           # per-8h
    fh = pd.Series(1e-4 * 3 / 24, index=idx)  # per-hour, same daily total
    d = annualized_diff(fb, fh)
    assert d.name == "diff"
    assert np.allclose(d.to_numpy(), 0.0, atol=1e-9)
    # a genuine +10.95%/yr Binance excess: Binance 2 bp/8h vs HL 1.25e-5/hr -> diff = 0.219 - 0.1095.
    d2 = annualized_diff(pd.Series(2e-4, index=idx), pd.Series(1e-4 * 3 / 24, index=idx))
    assert np.allclose(d2.to_numpy(), 2e-4 * 3 * 365 - (1e-4 * 3 / 24) * 24 * 365)


def test_differential_carry_frame_is_forward_and_sign_following():
    idx = pd.date_range("2023-01-01", periods=60, freq="D")
    diff = pd.Series(np.linspace(-0.2, 0.2, 60), index=idx)   # rising differential
    fr = differential_carry_frame(diff, k=7)
    assert list(fr.columns) == ["position", "carry_return"]
    assert not fr.isna().any().any()
    i = fr.index[20]
    j = idx.get_loc(i)
    # carry_return(t) = position(t) * diff(t+1)/365 (strictly forward)
    expected_pos = np.sign(diff.iloc[j - 6 : j + 1].mean())
    assert fr.loc[i, "position"] == expected_pos
    assert np.isclose(fr.loc[i, "carry_return"], expected_pos * diff.iloc[j + 1] / 365.0)
