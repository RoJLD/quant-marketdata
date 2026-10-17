"""Instrument registry: ties each venue's native symbols to canonical instrument ids, point-in-time.

Canonical id: ``<BASE>-<QUOTE>-PERP`` (``-SPOT`` for spot), with ``quote`` collapsed to ``USD`` for
USD/USDC/USDT and the actual stablecoin kept in ``settle_asset``.

Resolution order for a native symbol: explicit override -> the venue's parsing rule -> unresolved.
Unresolved symbols are reported, never matched by name similarity. Two native symbols of one venue that
resolve to the same canonical id are both reported as collisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

import pandas as pd

from quant_marketdata.venues.registry_overrides import OVERRIDES
from quant_marketdata.venues.schema import INSTRUMENT_COLUMNS

FIAT = frozenset({"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "HKD", "KRW", "CNH", "SGD"})
REGISTRY_COLUMNS = INSTRUMENT_COLUMNS + ["mapping_source"]
_SCALED = re.compile(r"^(1000|1M|k)([A-Z0-9]+)$")

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


PARSERS: Dict[str, Callable[[str], Optional[Parsed]]] = {
    "lighter": parse_lighter,
}


@dataclass
class RegistryBuild:
    instruments: pd.DataFrame
    unresolved: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def ok(self) -> bool:
        return self.unresolved.empty


def resolve(venue: str, snapshot: pd.DataFrame,
            overrides: Optional[Dict[str, Dict[str, Parsed]]] = None) -> RegistryBuild:
    """Fill the identity columns of one venue's ``instruments()`` snapshot."""
    overrides = OVERRIDES if overrides is None else overrides
    parser = PARSERS.get(venue)
    venue_overrides = overrides.get(venue, {})
    rows, unresolved = [], []
    for row in snapshot.to_dict("records"):
        symbol = row["venue_symbol"]
        parsed = parser(symbol) if parser else None
        override = venue_overrides.get(symbol)
        if override is not None:
            parsed = {**(parsed or {"base": symbol, "quote": "USD", "multiplier": 1.0}),
                      **{k: v for k, v in override.items() if k != "why"}}
            source = "override"
        elif parsed is not None:
            # Venues rarely say what a market is: a rule-parsed "crypto" is a default, flagged as such.
            source = "rule-default-class" if parsed.get("asset_class") == "crypto" else "rule"
        else:
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": "no rule or override" if parser else f"no parser for {venue}"})
            continue
        kind = "SPOT" if row.get("kind") == "spot" else "PERP"
        rows.append({
            **row,
            **{k: parsed.get(k) for k in ("base", "quote", "multiplier", "asset_class", "underlying_ref")},
            "instrument_id": f"{parsed['base']}-{parsed['quote']}-{kind}",
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


__all__ = ["PARSERS", "REGISTRY_COLUMNS", "RegistryBuild", "parse_lighter", "resolve", "update_registry"]
