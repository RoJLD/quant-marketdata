"""Explicit registry decisions, versioned with the code: the only place where an ambiguous native symbol is
tied to an underlying, or a market is classified.

Each entry maps a native symbol to the fields it sets (``base``, ``quote``, ``multiplier``, ``asset_class``,
``underlying_ref``) plus a one-line ``why``. Anything not listed here is either parsed by the venue's rules
or reported as unresolved -- never matched by name similarity.

A Python module rather than YAML keeps the core free of a parser dependency while allowing comments.
"""

from __future__ import annotations

from typing import Dict

Override = Dict[str, object]


def _classify(symbols: str, asset_class: str, why: str, *, ref: bool = False) -> Dict[str, Override]:
    entries: Dict[str, Override] = {}
    for symbol in symbols.split():
        entry: Override = {"asset_class": asset_class, "why": why}
        if ref:
            entry["underlying_ref"] = symbol
        entries[symbol] = entry
    return entries


LIGHTER: Dict[str, Override] = {
    **_classify(
        "AAPL AMD AMZN ARM ASML AVGO BABA COIN CRCL CRWV DELL GME GOOGL HOOD IBM INTC META MRNA MRVL "
        "MSFT MSTR MU NBIS NOK NOW NVDA ORCL PLTR QCOM RKLB TSLA TSM TTWO WDC BMNR GEV SNDK ADI",
        "equity", "US-listed stock perp", ref=True,
    ),
    **_classify(
        "SAMSUNG SKHYNIX HYUNDAI TENCENT XIAOMI BYD SMIC POPMART",
        "equity", "Asia-listed stock perp; the *USD twins are unresolved until their difference is known",
        ref=True,
    ),
    **_classify("SPY QQQ IWM DIA EWY SOXX SOXL SOXS URA BOTZ", "etf", "Exchange-traded fund perp", ref=True),
    **_classify("SPX US500 US100", "index", "Equity index perp", ref=True),
    **_classify("US10Y", "rates", "US 10-year yield perp", ref=True),
    **_classify(
        "XAU XAG XCU XPD XPT WTI BRENTOIL NATGAS WHEAT",
        "commodity", "Commodity perp; XAU is spot gold, not a gold token (XAUT, PAXG)", ref=True,
    ),
    **_classify(
        "OPENAI ANTHROPIC SHEIN UNITREE MINIMAX ZHIPU",
        "private", "Pre-IPO company perp: no public underlying price",
    ),
    "PAXG": {
        "asset_class": "crypto",
        "underlying_ref": "XAU",
        "why": "Gold-backed token: crypto venue risk, gold exposure",
    },
}

OVERRIDES: Dict[str, Dict[str, Override]] = {
    "lighter": LIGHTER,
}

__all__ = ["OVERRIDES"]
