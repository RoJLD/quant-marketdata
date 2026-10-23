"""Fee models of the dated-contract venues: the raw rates stay in the registry (``maker_fee_raw``,
``taker_fee_raw``, ``fee_extra_raw``, ``fee_type_raw``, ``fee_multiplier_raw``); this table declares how a
venue composes them, with ``fee_model_status`` ``documented`` (read in the venue's documentation) or
``unconfirmed`` (inferred, to be checked by a human before a net-of-fees result is trusted).

Decisions kept from the spec's open questions (§6): Q4 Derive's composition (rate x index notional + a
fixed fee, capped at 12.5 % of the premium?) and the unit of its ``base_fee`` are unconfirmed; Paradex's
base of the rate (underlying notional) is inferred, hence unconfirmed too; Q5 Kalshi's
quadratic coefficient, rounding and maker fees could not be read (the published schedule answered 429 to a
script, not bypassed) -- the formula below carries the commonly cited taker coefficient as a parameter and
stays ``unconfirmed`` until the schedule is read.
"""

from __future__ import annotations

import math
from typing import Dict

FEE_MODELS: Dict[str, Dict[str, str]] = {
    "derive": {
        "fee_model": "min(taker_fee_rate x index x size + base_fee, mark_price_fee_rate_cap x premium)?",
        "fee_model_status": "unconfirmed",
        "note": "rates served per instrument (0.0001 / 0.0003, base_fee 0.5, cap 0.125); unit of base_fee "
                "and the composition not documented in the public API",
    },
    "deribit": {
        "fee_model": "min(taker_commission x index x size, cap x premium)",
        "fee_model_status": "unconfirmed",
        "note": "commissions served per instrument (0.0003); the premium cap is from Deribit's fee page, not "
                "re-read for this release",
    },
    "paradex": {
        "fee_model": "min(rate x size x spot, 0.125 x premium), rebates floored at -0.125 x premium",
        "fee_model_status": "unconfirmed",
        "note": "fee_config per market (interactive, api and rpi maker/taker fees, fee_cap 0.125, fee_floor "
                "-0.125); the underlying notional as the base of the 0.01 % rate is inferred (Paradex options "
                "adapter, 2026-10-19)",
    },
    "kalshi": {
        "fee_model": "ceil_cent(coefficient x fee_multiplier x contracts x P x (1 - P))",
        "fee_model_status": "unconfirmed",
        "note": "series fee_type=quadratic, fee_multiplier=1 (served); coefficient, rounding and maker fees "
                "not read from the published schedule (spec Q5)",
    },
}

KALSHI_TAKER_COEFFICIENT = 0.07  # commonly cited; unconfirmed (see the module docstring)


def kalshi_fee(price: float, contracts: float, *, fee_multiplier: float = 1.0,
               coefficient: float = KALSHI_TAKER_COEFFICIENT) -> float:
    """Quadratic Kalshi fee in dollars for ``contracts`` at YES (or NO) price ``price`` in [0, 1], rounded
    up to the cent. Status ``unconfirmed``: pass the coefficient read from the schedule once it is known."""
    if not 0.0 <= price <= 1.0:
        raise ValueError(f"price must be a probability in [0, 1], got {price}")
    raw = coefficient * fee_multiplier * contracts * price * (1.0 - price)
    return math.ceil(round(raw * 100, 9)) / 100


__all__ = ["FEE_MODELS", "KALSHI_TAKER_COEFFICIENT", "kalshi_fee"]
