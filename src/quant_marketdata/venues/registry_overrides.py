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
    **_classify("US500 US100", "index", "Equity index perp", ref=True),
    "SPX": {
        "asset_class": "crypto",
        "why": "SPX6900 memecoin, as Hyperliquid SPX and Binance/Aster SPXUSDT: trades near 0.35 and sits in "
               "Lighter's crypto insurance fund (checked 2026-10-24); the S&P 500 perp is US500",
    },
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

DERIVE: Dict[str, Override] = {
    "XAUT-PERP": {**_GOLD_TOKEN, "why": "Tether gold token: crypto venue risk, gold exposure"},
}

_XAUT: Override = {**_GOLD_TOKEN, "why": "Tether gold token: crypto venue risk, gold exposure"}
# Pre-IPO companies (no public underlying price), as on Lighter, Binance and Extended.
_PRE_IPO = "ANTHROPIC OPENAI MOONSHOT OURA POLYMARKET"

# Bybit declares classes (symbolType); its pre-IPO perps (PreLaunch on 2026-10-30) are declared "stock".
BYBIT: Dict[str, Override] = {
    **{f"SHIB1000{book}": {"base": "SHIB", "multiplier": 1000.0,
                           "why": "Bybit's x1000 SHIB contract, scale as a suffix (Binance: 1000SHIBUSDT)"}
       for book in ("USDT", "PERP")},
    **_classify(_PRE_IPO, "private", "Pre-IPO company perp, declared stock (PreLaunch)", suffix="USDT"),
}

# OKX files equity indices under instCategory 3 ("stocks").
OKX: Dict[str, Override] = {
    **_classify("US500 US100 JP225 KR200", "index", "Equity index perp (OKX category 3, 'stocks')", ref=True,
                suffix="-USDT-SWAP"),
}

# Gate's contract_type "metals" holds gold tokens and metal ETFs; "forex" holds the USDC stablecoin.
GATE: Dict[str, Override] = {
    "PAXG_USDT": _GOLD_TOKEN,
    "XAUT_USDT": _XAUT,
    **_classify("IAU SLV", "etf", "Exchange-traded fund perp (Gate 'metals')", ref=True, suffix="_USDT"),
    "USDC_USDT": {"asset_class": "crypto", "why": "USDC stablecoin priced in USDT (Gate 'forex')"},
}

# Bitget flags RWA perps (isRwa) without their class: those classified here resolve, the others stay unresolved.
# Beyond the gold tokens, metals and pre-IPO companies, a ticker gets the class that every venue declaring the
# same ticker gave it on 2026-10-30 (Bybit, OKX, Gate, Binance, Aster, Extended, Paradex, and the decisions
# above), provided no venue lists that id as crypto: 287 of Bitget's 404 RWA perps. OKX, Gate and Binance file
# ETFs as stocks: a ticker that only they declare stays "equity". The other RWA perps (HKD lines, Asian and
# single-venue tickers) are unresolved until classified.
BITGET: Dict[str, Override] = {
    "PAXGUSDT": _GOLD_TOKEN,
    "XAUTUSDT": _XAUT,
    **_classify("XAU XAG XPT XPD", "commodity", "Metal perp (Bitget RWA)", ref=True, suffix="USDT"),
    **_classify(_PRE_IPO, "private", "Pre-IPO company perp (Bitget RWA)", suffix="USDT"),
    **_classify(
        "AAL AAPL AAPU ABBV ABNB ADBE ADI AEHR AGPU AKAM ALAB AMAT AMC AMD AMGN AMKR AMZN AMZU ANET APD "
        "APLD APP ARM ASML ASTS ASX AVGO BA BABA BAC BHP BKNG BLSH BMNR BNC BRKB BSP BUD BX BYD CCL CEG "
        "CGNX CIEN CLS CLSK CMCSA COHR COIN COP CORZ COST CPNG CRCL CRDO CRM CRML CRWD CRWV CSCO CSTOCK "
        "CSX CVNA CVXSTOCK CYPH DDOG DELL DHR DJT DKNG EBAY EIDO EQT FASTRETAILING FCX FLEX FLNC FLY FOXA "
        "FTNT FUTU FWDI GE GEV GFS GGLL GIGADEVICE GILD GLW GNRC GOOGL GPRO GS GTLB HD HLT HOOD HPE HUT "
        "HYUNDAI IBB IBKR IBM INFQ INTC IONQ IREN ISRG ITUB JD JEPQ JIO JMKE JPM KLAC KO KUAISHOU KWEB "
        "LEN LGELECTRONICS LIN LLY LMT LRCX MAR MARA MCD MDB MEITUAN MELI META METU MINIMAX MINIMAXHKD MP "
        "MRK MRNA MRVL MSCI MSFT MSTR MU MUFG NAVER NBIS NET NFLX NKE NOC NOW NU NVDA OKLO OKTA ONDS ORCL "
        "OUST OXY PANW PATH PBR PDD PEP PFE PG PLTR POET POPMART PYPL QCOM QLD RDDT RDW RIO RKLB ROK RUM "
        "SAMSUNG SAMSUNGEM SBUX SDGR SECZ SHAZ SHEIN SHOP SIMO SKHYNIX SMCI SMIC SMR SNDK SNOW SNPS SOFI "
        "SOFTBANK SONY SPOT STM TAIYOYUDEN TEAM TEM TENCENT TENCENTHKD TER TM TSEM TSLA TSM TTWO TWLO "
        "TWST TXN UBER UNH UNITREE USAR V VALE VKTX VLO VOO VRT VST WDC WMT XIAOMI XIAOMIHKD XOM XP ZHIPU "
        "ZHIPUHKD ZHONGJI ZM ZS",
        "equity", "Stock perp (Bitget RWA): equity wherever declared", ref=True, suffix="USDT",
    ),
    **_classify(
        "BITO BOTZ BWET CONL EWJ EWT EWZ EWZS GDX IBIT INTW IWM KSTR LYTE MSFU MSTU MUU MVLL NVDL QQQ RAM "
        "SKDD SKUU SMH SNXX SOXL SOXS SOXX SPCH SPY SQQQ SSPC TBT TLT TMF TQQQ TSLL TZA URNM UVXY XBI XLE "
        "XLK XLU XLV ZCSH",
        "etf", "Exchange-traded fund perp (Bitget RWA): etf wherever declared", ref=True, suffix="USDT",
    ),
    **_classify(
        "B200 JP225 KR200",
        "index", "Index perp (Bitget RWA): index wherever declared", ref=True, suffix="USDT",
    ),
    **_classify(
        "BZ CL COPPER NATGAS",
        "commodity", "Commodity perp (Bitget RWA): commodity wherever declared", ref=True, suffix="USDT",
    ),
}

OVERRIDES: Dict[str, Dict[str, Override]] = {
    "aster": ASTER,
    "binance": BINANCE,
    "bitget": BITGET,
    "bybit": BYBIT,
    "derive": DERIVE,
    "extended": EXTENDED,
    "gate": GATE,
    "hyperliquid": HYPERLIQUID,
    "lighter": LIGHTER,
    "okx": OKX,
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
    # Ids carry no class (registry docstring): where a new venue lists, under an existing id, an underlying of
    # the other kind (a memecoin where Binance has a stock, or the reverse), the newcomer stays unresolved.
    "bybit": {
        "BTTUSDT": "delisted 2022-02-14 and replaced by 1000BTTUSDT (BTT x1000): both would be BTT-USD-PERP",
        "GMEUSDT": "GME memecoin (the stock is GMESTOCKUSDT); GME-USD-PERP is GameStop on Binance, Aster, Lighter",
        "MDTUSDT": "MDT token (the stock is MDTSTOCKUSDT); MDT-USD-PERP is Medtronic on Binance",
        "PENGUSDT": "PENG token (the stock is PENGSTOCKUSDT); PENG-USD-PERP is Penguin Solutions on Binance",
        "10000WENUSDT": "WEN memecoin x1e4; WEN-USD-PERP is Wendy's on Binance",
        "ONUSDT": "ON Semiconductor stock (symbolType stock); ON-USD-PERP is a crypto token on Binance",
    },
    "okx": {
        "ON-USDT-SWAP": "ON Semiconductor stock (category 3); ON-USD-PERP is a crypto token on Binance",
        "BB-USDT-SWAP": "BlackBerry stock (category 3); BB-USD-PERP is BounceBit on Binance (Extended's stock BB "
                        "is a known clash)",
        "QNT-USDT-SWAP": "stock (category 3); QNT-USD-PERP is the Quant token on Binance (Extended's stock QNT is "
                         "a known clash)",
    },
    "gate": {
        "CAT_USDT": "Caterpillar stock (contract_type stocks); CAT-USD-PERP is the Simon's Cat memecoin "
                    "(1000CAT on Aster, Bybit, Bitget)",
        "RTX_USDT": "RTX stock (contract_type stocks); RTX-USD-PERP is a crypto perp on Aster",
    },
}

__all__ = ["OVERRIDES", "UNRESOLVED"]
