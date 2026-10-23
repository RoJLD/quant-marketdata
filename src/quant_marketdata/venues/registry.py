"""Instrument registry: ties each venue's native symbols to canonical instrument ids, point-in-time.

Canonical id: ``<BASE>-<QUOTE>-PERP`` (``-SPOT`` for spot), with ``quote`` collapsed to ``USD`` for
USD/USDC/USDT and the actual stablecoin kept in ``settle_asset``.

Resolution order for a native symbol: explicit override -> the venue's parsing rule -> unresolved.
Unresolved symbols are reported, never matched by name similarity. Two native symbols of one venue that
resolve to the same canonical id are both reported as collisions.

Asset class: a rule may leave it to the venue (``None``) when the venue declares it (Binance
``underlyingType``, Aster ``underlyingSubType``, Extended ``category``); the adapter then fills
``asset_class`` in its ``instruments()`` snapshot. Without either, the class defaults to ``crypto`` and the
row is flagged ``rule-default-class``.

Several stablecoin books of one base on one venue (Binance ``BTCUSDT`` and ``BTCUSDC``, Aster ``BTCUSDT``
and ``BTCUSD1``) cannot share one canonical id: the venue's primary book (USDT) collapses to ``USD``, the
others keep their stablecoin as quote (``BTC-USDC-PERP``).

The collapse hides the unit prices are quoted in, and ``settle_asset`` does not give it either: it is the
stablecoin positions margin and settle in, not the unit of the venue's index. On 2026-10-24 (BTC, ETH, SOL)
Hyperliquid, USDC-settled, marked against an oracle within 1 bp of Binance's USDT index, while the Extended
index and the Paradex underlying sat 9-13 bp lower, level with Binance's USDC index: the USDT/USD basis of
the day (Coinbase: 1 USDT = 0.99886 USD). Comparing price levels of one canonical id across venues
therefore measures that basis too; funding rates, being fractions of each venue's own price, do not carry it.

Trading session (decided 2026-10-18). A contract that only trades on a restricted session is a different
instrument from the continuous one, even on the same underlying: its price gaps over the closed hours and
its funding is not comparable hour for hour. The session is therefore part of the id, as a suffix:
``ADBE-USD-PERP-24X5`` (round the clock on weekdays), ``ADSK-USD-PERP-NOOVERNIGHT`` (closed during the US
overnight session); a continuous or undeclared session adds nothing (``MSFT-USD-PERP``). The session is
read from what the venue *declares* (``session`` column of its snapshot), never from the market's name:

- Extended declares ``tradingHours`` (``CONTINUOUS``, ``WEEKDAYS``, ``NO_OVERNIGHT``). Its ``_24_5`` names
  are listing-batch labels, not sessions: on 2026-10-18, 20 of the 67 ``<TICKER>_24_5-USD`` markets
  (19 active: MSFT, NVDA, TSLA, AAPL, ...) are ``CONTINUOUS`` and displayed as ``<TICKER>-USD``
  (``uiName``), while the 47 ``WEEKDAYS`` ones (ADBE, APP, CRWD, ...) are the actual 24/5 contracts.
  A ``_24_5`` name is parsed to its ticker and the declared session decides the id. ``ADSK-USD``
  (``NO_OVERNIGHT``) and ``ADSK_24_5-USD`` (``WEEKDAYS``, delisted) thus get two ids, and
  ``MSFT_24_5-USD`` (``CONTINUOUS``) shares ``MSFT-USD-PERP`` with the other venues' continuous MSFT
  perps.
- Lighter declares ``market_config.trading_hours``, empty (continuous) on all 239 perps on 2026-10-18.
- Binance, Aster, Hyperliquid and Paradex declare nothing. Paradex's TradFi perps trade at weekends
  (trades on Saturday and Sunday, 2026-10-03 to 10-11) although its docs give 24/5 and 23/5 *oracle*
  market hours, so they are continuous.

The session is a snapshot field: if a venue changes a market's session, the market's id changes at the
next registry update. A declared session value with no mapping is reported as unresolved, not guessed.

Symbols that stay unresolved by decision (``UNRESOLVED`` in ``registry_overrides``) are reported with the
reason of that decision; non-ASCII names (the Chinese-named memecoins of Binance and Aster) are reported as
such: there is no ticker to key an id on, and the same name on two venues is not proof of the same token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional, Tuple

import pandas as pd

from quant_marketdata.venues.registry_overrides import OVERRIDES, UNRESOLVED
from quant_marketdata.venues.schema import INSTRUMENT_COLUMNS

FIAT = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "HKD", "KRW", "CNH", "SGD"})
REGISTRY_COLUMNS = INSTRUMENT_COLUMNS + ["mapping_source"]
# Id suffix of each restricted session; "24x7" (and an undeclared session) adds none.
SESSION_TAGS = {"24x5": "24X5", "no-overnight": "NOOVERNIGHT"}
CONTINUOUS = "24x7"
EXTENDED_BATCH = "_24_5"
_SCALED = re.compile(r"^(1000|1M|k)([A-Z0-9]+)$")
# Longest prefix first, and the scaled base must start with a letter: 1000000MOG is MOG x 1e6, 1INCH is 1INCH.
_PREFIXED = re.compile(r"^(1000000|1000|1M|k)([A-Z][A-Z0-9]*)$")
_MULTIPLIERS = {"1000000": 1e6, "1M": 1e6, "1000": 1000.0, "k": 1000.0}
_IDENTITY = ("base", "quote", "multiplier", "asset_class", "underlying_ref")

Parsed = Dict[str, object]


def parse_lighter(symbol: str) -> Optional[Parsed]:
    """Lighter perps are quoted in USD and named after their base, e.g. ``BTC``, ``1000BONK``, ``EURUSD``."""
    if len(symbol) == 6 and symbol[:3] in FIAT and symbol[3:] in FIAT:
        return {"base": symbol[:3], "quote": symbol[3:], "multiplier": 1.0, "asset_class": "fx",
                "underlying_ref": symbol}
    if symbol.endswith("USD") and len(symbol) > 6:
        return None  # e.g. SAMSUNGUSD next to SAMSUNG: a distinct market whose meaning is not known
    match = _SCALED.match(symbol)
    if match and match.group(1) == "1000":
        return {"base": match.group(2), "quote": "USD", "multiplier": 1000.0, "asset_class": "crypto"}
    return {"base": symbol, "quote": "USD", "multiplier": 1.0, "asset_class": "crypto"}


def _scaled_base(base: str) -> Tuple[str, float]:
    match = _PREFIXED.match(base)
    if match:
        return match.group(2), _MULTIPLIERS[match.group(1)]
    return base, 1.0


def _fiat_pair(base: str) -> Optional[Parsed]:
    if len(base) == 6 and base[:3] in FIAT and base[3:] in FIAT:
        return {"base": base[:3], "quote": base[3:], "multiplier": 1.0, "asset_class": "fx",
                "underlying_ref": base}
    return None


def _quoted(symbol: str, quotes: Dict[str, str]) -> Optional[Parsed]:
    """``<BASE><QUOTE>`` with the quote taken from ``quotes`` (native suffix -> canonical quote)."""
    if not symbol.isascii():
        return None  # e.g. Binance's Chinese-named memecoins: no ASCII ticker to key the store on
    for suffix, quote in quotes.items():
        if symbol.endswith(suffix) and len(symbol) > len(suffix):
            base, multiplier = _scaled_base(symbol[: -len(suffix)])
            if not base.isalnum():
                return None
            return {"base": base, "quote": quote, "multiplier": multiplier, "asset_class": None}
    return None


def parse_binance(symbol: str) -> Optional[Parsed]:
    """Binance USD-M perps: ``BTCUSDT`` -> BTC/USD, ``BTCUSDC`` -> BTC/USDC (second book of the same base),
    ``1000PEPEUSDT`` -> PEPE x1000, ``1000000MOGUSDT`` / ``1MBABYDOGEUSDT`` -> x1e6. The asset class comes
    from the venue (``underlyingType``). Coin-quoted (``ETHBTC``) and ``U``-quoted (``BTCU``) markets are
    left unresolved."""
    return _quoted(symbol, {"USDT": "USD", "USDC": "USDC", "USD1": "USD1"})


def parse_aster(symbol: str) -> Optional[Parsed]:
    """Aster: Binance-style symbols, USDT the primary book, USD1 a second one."""
    return _quoted(symbol, {"USDT": "USD", "USD1": "USD1"})


def parse_hyperliquid(symbol: str) -> Optional[Parsed]:
    """Hyperliquid main-dex perps are USDC-settled and named after their base; ``k`` means x1000
    (``kPEPE``)."""
    if not symbol.isascii() or not symbol.isalnum():
        return None
    base, multiplier = _scaled_base(symbol)
    return {"base": base, "quote": "USD", "multiplier": multiplier, "asset_class": "crypto"}


def parse_extended(symbol: str) -> Optional[Parsed]:
    """Extended: ``<BASE>-USD``; ``1000``/``k`` prefixes scale, ``USDJPY-USD`` is the USD/JPY pair.
    ``<TICKER>_24_5-USD`` is the stock ``TICKER`` (a listing-batch name: the session comes from the
    venue's ``tradingHours``, see the module docstring). Other names with an underscore are unresolved.
    The asset class comes from the venue (``category``)."""
    if not symbol.endswith("-USD") or not symbol.isascii():
        return None
    base = symbol[: -len("-USD")]
    if base.endswith(EXTENDED_BATCH):
        base = base[: -len(EXTENDED_BATCH)]
    if not base.isalnum():
        return None
    fx = _fiat_pair(base)
    if fx is not None:
        return fx
    base, multiplier = _scaled_base(base)
    return {"base": base, "quote": "USD", "multiplier": multiplier, "asset_class": None}


def parse_paradex(symbol: str) -> Optional[Parsed]:
    """Paradex perps: ``<BASE>-USD-PERP``, USDC-settled; ``k`` means x1000 (``kPEPE-USD-PERP``). The
    adapter declares the crypto class from the venue's tags; the TradFi (``RWA``) markets are classified by
    overrides."""
    if not symbol.endswith("-USD-PERP") or not symbol.isascii():
        return None
    base = symbol[: -len("-USD-PERP")]
    if not base.isalnum():
        return None
    base, multiplier = _scaled_base(base)
    return {"base": base, "quote": "USD", "multiplier": multiplier, "asset_class": None}


def parse_derive(symbol: str) -> Optional[Parsed]:
    """Derive perps: ``<BASE>-PERP``, USDC-margined and quoted in USD (``perp_details.index`` is
    ``<BASE>-USD``)."""
    if not symbol.endswith("-PERP") or not symbol.isascii():
        return None
    base = symbol[: -len("-PERP")]
    if not base.isalnum():
        return None
    return {"base": base, "quote": "USD", "multiplier": 1.0, "asset_class": "crypto"}


PARSERS: Dict[str, Callable[[str], Optional[Parsed]]] = {
    "aster": parse_aster,
    "binance": parse_binance,
    "derive": parse_derive,
    "extended": parse_extended,
    "hyperliquid": parse_hyperliquid,
    "lighter": parse_lighter,
    "paradex": parse_paradex,
}


def _present(value: object) -> bool:
    return value is not None and not (isinstance(value, float) and pd.isna(value))


def _unresolved_reason(venue: str, symbol: str, has_parser: bool) -> str:
    if not has_parser:
        return f"no parser for {venue}"
    if not symbol.isascii():
        return "non-ASCII name: no ticker to key an id on"
    return "no rule or override"


def session_suffix(session: object) -> Optional[str]:
    """Id suffix for a declared session (``""`` when continuous or undeclared); ``None`` if unknown."""
    if not _present(session) or session == CONTINUOUS:
        return ""
    tag = SESSION_TAGS.get(str(session))
    return None if tag is None else f"-{tag}"


@dataclass
class RegistryBuild:
    instruments: pd.DataFrame
    unresolved: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def ok(self) -> bool:
        return self.unresolved.empty


def resolve(venue: str, snapshot: pd.DataFrame,
            overrides: Optional[Dict[str, Dict[str, Parsed]]] = None,
            decided: Optional[Dict[str, Dict[str, str]]] = None) -> RegistryBuild:
    """Fill the identity columns of one venue's ``instruments()`` snapshot.

    ``decided`` lists symbols left unresolved by decision, with the reason reported for each (default:
    ``registry_overrides.UNRESOLVED``); such a decision wins over the venue's parsing rule.
    """
    overrides = OVERRIDES if overrides is None else overrides
    decided = UNRESOLVED if decided is None else decided
    parser = PARSERS.get(venue)
    venue_overrides = overrides.get(venue, {})
    venue_decided = decided.get(venue, {})
    rows, unresolved = [], []
    for row in snapshot.to_dict("records"):
        symbol = row["venue_symbol"]
        override = venue_overrides.get(symbol)
        if override is None and symbol in venue_decided:
            unresolved.append({"venue": venue, "venue_symbol": symbol, "reason": venue_decided[symbol]})
            continue
        parsed = parser(symbol) if parser else None
        if override is not None:
            parsed = {**(parsed or {"base": symbol, "quote": "USD", "multiplier": 1.0}),
                      **{k: v for k, v in override.items() if k != "why"}}
            source = "override"
        elif parsed is not None:
            # Venues rarely say what a market is: a rule-parsed "crypto" is a default, flagged as such.
            source = "rule-default-class" if parsed.get("asset_class") == "crypto" else "rule"
        else:
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": _unresolved_reason(venue, symbol, parser is not None)})
            continue
        suffix = session_suffix(row.get("session"))
        if suffix is None:
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": f"unknown session {row.get('session')!r}"})
            continue
        identity = {k: parsed.get(k) for k in _IDENTITY}
        for key in _IDENTITY:  # what the rule leaves open, the venue may declare
            if identity[key] is None and _present(row.get(key)):
                identity[key] = row[key]
        if identity["asset_class"] is None:
            identity["asset_class"] = "crypto"
            if source != "override":
                source = "rule-default-class"
        kind = "SPOT" if row.get("kind") == "spot" else "PERP"
        rows.append({
            **row,
            **identity,
            "instrument_id": f"{identity['base']}-{identity['quote']}-{kind}{suffix}",
            "mapping_source": source,
        })
    frame = pd.DataFrame(rows).reindex(columns=REGISTRY_COLUMNS)
    duplicated = frame["instrument_id"].duplicated(keep=False)
    if duplicated.any():
        for symbol, instrument in frame.loc[duplicated, ["venue_symbol", "instrument_id"]].itertuples(index=False):
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": f"collides on {instrument}"})
        frame = frame.loc[~duplicated]
    return RegistryBuild(frame.reset_index(drop=True), pd.DataFrame(unresolved))


def update_registry(existing: pd.DataFrame, build: RegistryBuild, venue: str,
                    as_of: pd.Timestamp) -> pd.DataFrame:
    """Merge a fresh build for ``venue`` into the stored registry, point-in-time.

    A symbol that disappeared from the venue's listing is marked delisted at ``as_of`` (kept, not
    removed); ``listed_at`` keeps the earliest value ever seen.
    """
    fresh = build.instruments.copy()
    if existing.empty:
        return fresh.reindex(columns=REGISTRY_COLUMNS).reset_index(drop=True)
    others = existing[existing["venue"] != venue]
    previous = existing[existing["venue"] == venue].set_index("venue_symbol")
    fresh = fresh.set_index("venue_symbol")
    common = fresh.index.intersection(previous.index)
    earlier = previous.loc[common, "listed_at"]
    fresh.loc[common, "listed_at"] = pd.concat(
        [fresh.loc[common, "listed_at"], earlier], axis=1
    ).min(axis=1)
    gone = previous.loc[previous.index.difference(fresh.index)].copy()
    gone["delisted_at"] = gone["delisted_at"].fillna(as_of)
    gone["status"] = "delisted"
    merged = pd.concat([fresh, gone]).reset_index()
    merged = pd.concat([others, merged], ignore_index=True)
    return merged.reindex(columns=REGISTRY_COLUMNS).sort_values(["venue", "venue_symbol"]).reset_index(drop=True)


__all__ = [
    "PARSERS",
    "REGISTRY_COLUMNS",
    "RegistryBuild",
    "SESSION_TAGS",
    "parse_aster",
    "parse_binance",
    "parse_derive",
    "parse_extended",
    "parse_hyperliquid",
    "parse_lighter",
    "parse_paradex",
    "resolve",
    "session_suffix",
    "update_registry",
]
