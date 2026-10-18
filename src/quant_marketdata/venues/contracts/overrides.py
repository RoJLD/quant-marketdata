"""Explicit decisions for dated contracts, versioned with the code (the analogue of ``registry_overrides``).

- ``CRYPTO_BASES``: bare crypto tickers whose meaning is established; an option venue's native base in this
  set resolves by rule to ``<BASE>-USD`` (asset class ``crypto``). Anything else needs an entry in
  ``OPTION_UNDERLYINGS`` or is reported unresolved -- never matched by name similarity.
- ``OPTION_UNDERLYINGS[venue][native base]``: the fields an override sets (``base``, ``quote``,
  ``asset_class``, ``underlying_ref``) and a one-line ``why``. Venues add their own entries here (Paradex's
  XAU, CL, SPCX, US100, US500, META, MU, MSTR belong to its options adapter).
- ``KALSHI_SERIES``: the only source of a Kalshi series' underlying and settlement index (never the series
  name). A series missing from the table is not even downloaded.
"""

from __future__ import annotations

from typing import Dict

Override = Dict[str, object]

CRYPTO_BASES = frozenset({
    "ADA", "AVAX", "BNB", "BTC", "DOGE", "ETH", "HYPE", "LINK", "NEAR", "SOL", "TRX", "XRP", "ZEC",
})


def _token(why: str) -> Override:
    return {"asset_class": "crypto", "why": why}


DERIVE: Dict[str, Override] = {
    "XAUT": {"asset_class": "crypto", "underlying_ref": "XAU",
             "why": "Tether gold token: crypto venue risk, gold exposure (not spot gold XAU)"},
    # Derive currencies with perp and option markets (get_all_currencies) whose identity is only what
    # Derive's listing says: a token, crypto class, linked to other venues by (base, quote) alone.
    **{base: _token(f"Derive currency {base}: a token listed with perp and options on Derive")
       for base in ("CC", "LIT", "VVV", "PUMP")},
}

OPTION_UNDERLYINGS: Dict[str, Dict[str, Override]] = {
    "derive": DERIVE,
}

KALSHI_SERIES: Dict[str, Override] = {
    "KXBTCD": {"underlying_id": "BTC-USD", "settlement_index_id": "cfb:BRTI", "kind": "thresholds",
               "why": "Bitcoin price above/below: hourly ($100 grid), daily ($250), weekly ($500) events"},
    "KXBTC": {"underlying_id": "BTC-USD", "settlement_index_id": "cfb:BRTI", "kind": "ranges",
              "why": "Bitcoin price range: $100 brackets plus two tails"},
    "KXETHD": {"underlying_id": "ETH-USD", "settlement_index_id": "cfb:ERTI", "kind": "thresholds",
               "why": "Ethereum price above/below"},
    "KXETH": {"underlying_id": "ETH-USD", "settlement_index_id": "cfb:ERTI", "kind": "ranges",
              "why": "Ethereum price range"},
}

__all__ = ["CRYPTO_BASES", "KALSHI_SERIES", "OPTION_UNDERLYINGS"]
