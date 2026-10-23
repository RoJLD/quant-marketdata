"""Black-76 reference greeks for the venue-convention tests (test helper, not a library estimator)."""

from __future__ import annotations

import math


def _cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def black76(forward: float, strike: float, years: float, vol: float, kind: str, df: float = 1.0) -> dict:
    """Price and greeks on the forward: ``delta``/``gamma`` per unit of forward, ``vega`` per vol point
    (0.01), ``theta`` per calendar day (forward held constant, discounting included)."""
    sqrt_t = math.sqrt(years)
    d1 = (math.log(forward / strike) + 0.5 * vol * vol * years) / (vol * sqrt_t)
    d2 = d1 - vol * sqrt_t
    rate = -math.log(df) / years if df != 1.0 else 0.0
    if kind == "C":
        price = df * (forward * _cdf(d1) - strike * _cdf(d2))
        delta = df * _cdf(d1)
    else:
        price = df * (strike * _cdf(-d2) - forward * _cdf(-d1))
        delta = -df * _cdf(-d1)
    gamma = df * _pdf(d1) / (forward * vol * sqrt_t)
    vega = df * forward * _pdf(d1) * sqrt_t / 100.0
    theta = (-df * forward * _pdf(d1) * vol / (2.0 * sqrt_t) + rate * price) / 365.0
    return {"price": price, "delta": delta, "gamma": gamma, "vega": vega, "theta": theta}
