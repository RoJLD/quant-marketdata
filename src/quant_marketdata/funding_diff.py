"""Cross-venue funding differential: annualized Δf = f_binance - f_hyperliquid (the CEX per-8h vs DEX
per-hour normalization) + the delta-neutral differential-carry frame. Δf is a RELATIVE signal (venue
divergence). Shared by the research realism experiments and the live fund."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from quant_marketdata.binance_funding import load_funding_rate as _binance_funding
from quant_marketdata.hyperliquid_funding import load_funding_rate as _hl_funding

_BINANCE_PER_DAY = 3     # Binance funding paid every 8h -> 3 periods/day
_HL_PER_DAY = 24         # Hyperliquid funding paid hourly -> 24 periods/day
_DAYS = 365


def annualized_diff(f_binance_raw: pd.Series, f_hl_raw: pd.Series) -> pd.Series:
    """Δf = annualized Binance funding - annualized Hyperliquid funding, aligned on the common span.

    Binance daily = mean per-8h rate -> x3 x365; Hyperliquid daily = mean per-hour rate -> x24 x365.
    Differencing the RAW dailies would be an ~8x scale artifact; annualizing puts both on a common basis.
    """
    fb = f_binance_raw.astype(float) * _BINANCE_PER_DAY * _DAYS
    fh = f_hl_raw.astype(float) * _HL_PER_DAY * _DAYS
    aligned = pd.concat({"binance": fb, "hl": fh}, axis=1).dropna()
    return (aligned["binance"] - aligned["hl"]).rename("diff")


def cross_venue_diff(
    asset: str, *, start: str = "2023-01-01", use_cache: bool = True, cache_dir: str | Path | None = None
) -> pd.Series:
    """Annualized cross-venue funding differential for `asset` (BTC/ETH): Binance perp - Hyperliquid perp.

    ``cache_dir`` threads through to both funding loaders (shared 2019+ cache)."""
    fb = _binance_funding(f"{asset}USDT", start=start, use_cache=use_cache, cache_dir=cache_dir)
    fh = _hl_funding(asset, start=start, use_cache=use_cache, cache_dir=cache_dir)
    return annualized_diff(fb, fh)


def differential_carry_frame(diff: pd.Series, *, k: int = 7) -> pd.DataFrame:
    """Delta-neutral cross-venue differential-carry: position = sign(MA_k(Δf)) known at t; the day's
    return = position(t) * Δf(t+1)/365 (Δf is annualized -> the day's funding is Δf/365). Strictly forward."""
    d = diff.dropna().astype(float)
    pos = np.sign(d.rolling(k).mean())
    ret = pos * d.shift(-1) / _DAYS
    return pd.DataFrame({"position": pos, "carry_return": ret}).dropna()
