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


def _classify(symbols: str, asset_class: str, why: str, *, ref: bool = False,
              suffix: str = "") -> Dict[str, Override]:
    """Entries for ``symbols`` (bare names; ``suffix`` turns them into native symbols, e.g. ``-USD``)."""
    entries: Dict[str, Override] = {}
    for symbol in symbols.split():
        entry: Override = {"asset_class": asset_class, "why": why}
        if ref:
            entry["underlying_ref"] = symbol
        entries[symbol + suffix] = entry
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

_GOLD_TOKEN: Override = {
    "asset_class": "crypto",
    "underlying_ref": "XAU",
    "why": "Gold-backed token: crypto venue risk, gold exposure",
}

# BTCU / ETHU (Binance and Aster): exchangeInfo declares baseAsset BTC / ETH with quoteAsset and marginAsset
# "U", a stablecoin. Like BTCUSDC and BTCUSD1, a second stablecoin book keeps its stablecoin as quote.
_U_BOOKS: Dict[str, Override] = {
    f"{base}U": {"base": base, "quote": "U", "multiplier": 1.0,
                 "why": f"Declared baseAsset {base}, quoteAsset/marginAsset U: second stablecoin book"}
    for base in ("BTC", "ETH")
}

# Binance declares the class of every market (underlyingType); only the gold token needs a decision.
BINANCE: Dict[str, Override] = {
    "PAXGUSDT": _GOLD_TOKEN,
    **_U_BOOKS,
}

# Aster tags PAXG as "Commodities", like spot gold: it is a token.
ASTER: Dict[str, Override] = {
    "PAXGUSDT": _GOLD_TOKEN,
    **_U_BOOKS,
}

HYPERLIQUID: Dict[str, Override] = {
    "PAXG": _GOLD_TOKEN,
}

EXTENDED: Dict[str, Override] = {
    "PAXG-USD": _GOLD_TOKEN,
    "XAUT-USD": {**_GOLD_TOKEN, "why": "Tether gold token: crypto venue risk, gold exposure"},
    # The "ETF/Index" sub-category mixes both classes.
    **_classify("SPX500m TECH100m KR200 JP225", "index", "Equity index perp (Extended ETF/Index)",
                ref=True, suffix="-USD"),
    **_classify("SOXL EWZ KORU DRAM LYTE", "etf", "Exchange-traded fund perp (Extended ETF/Index)",
                ref=True, suffix="-USD"),
}

# Paradex tags every TradFi perp "RWA" without saying which class: each is classified here. Its oracle
# docs (trading/tradfi-markets) describe the equities, the US500/US100 indices and the commodities; CL, BZ
# and NG track the active front-month future (WTI, Brent, Henry Hub gas) and keep Paradex's tickers: they
# are not tied to Lighter's WTI / BRENTOIL / NATGAS, whose roll schedule is not known.
PARADEX: Dict[str, Override] = {
    "PAXG-USD-PERP": _GOLD_TOKEN,
    **_classify("CRCL GOOGL INTC META MRVL MSFT MSTR MU NVDA SNDK SPCX TSLA", "equity",
                "US-listed stock perp (Paradex RWA)", ref=True, suffix="-USD-PERP"),
    **_classify("DRAM EWY", "etf", "Exchange-traded fund perp (Paradex RWA)", ref=True,
                suffix="-USD-PERP"),
    **_classify("US500 US100", "index", "Equity index perp (Paradex RWA)", ref=True, suffix="-USD-PERP"),
    **_classify("XAU XAG XPT XCU", "commodity", "Metal perp (Paradex RWA)", ref=True, suffix="-USD-PERP"),
    **_classify("CL BZ NG", "commodity", "Front-month future perp (Paradex RWA): WTI, Brent, Henry Hub",
                ref=True, suffix="-USD-PERP"),
}

OVERRIDES: Dict[str, Dict[str, Override]] = {
    "aster": ASTER,
    "binance": BINANCE,
    "extended": EXTENDED,
    "hyperliquid": HYPERLIQUID,
    "lighter": LIGHTER,
    "paradex": PARADEX,
}

# Symbols left unresolved by decision, each with the reason the registry reports. A decision here wins over
# the venue's parsing rule; a symbol cannot be both here and in OVERRIDES.
UNRESOLVED: Dict[str, Dict[str, str]] = {
    "extended": {
        **{symbol: "delisted placeholder market (description PLACE_JPY): no known underlying"
           for symbol in ("PLACE_JPY-USD", "PLACE_JPY_1-USD", "PLACE_JPY-USD_1-USD")},
        "XYZSPCX_ORCLPX-USD": "delisted market described as SpaceX but named after SPCX and ORCL: "
                              "not tied to SPCX-USD without evidence",
    },
    "aster": {
        "B-MONEYUSDT": "delisted memecoin whose declared base asset (B-MONEY) is not a ticker; single venue",
    },
}

__all__ = ["OVERRIDES", "UNRESOLVED"]
