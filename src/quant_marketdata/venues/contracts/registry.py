"""Contract registry: canonical ids for option and binary contracts, point-in-time, separate from the perp
instrument registry (``registry/instruments.parquet`` is untouched).

Resolution of an option contract (``resolve_option_contracts``):

1. the underlying comes from the **structured** native base (``venue_base``): an explicit override
   (``overrides.OPTION_UNDERLYINGS``), else the rule for bare crypto tickers (``overrides.CRYPTO_BASES``),
   else the contract is unresolved;
2. ``expiry``, ``strike`` and ``option_type`` come from the venue's structured fields;
3. the name is only a **cross-check**: the venue's registered name parser (``register_name_parser``) reads
   base, expiry date, strike and type from ``venue_symbol``; any disagreement leaves the contract
   unresolved (``name_fields_mismatch``) without arbitrating.

A binary contract (``resolve_binary_contracts``) takes its underlying and settlement index from the series
table (``overrides.KALSHI_SERIES``) and its condition from the canonical bounds the adapter computed.

``update_contract_registry`` merges a fresh listing into the stored one: nothing is ever removed; a contract
missing from the listing is ``delisted`` before its expiry and ``expired`` after it; settlement fields, once
observed, are kept with the time they were first seen (``settlement_seen_at``); ``first_seen_at`` is the first
pass that saw the contract. ``universe_changes`` lists new underlyings, expiries and series.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, Optional, Tuple

import pandas as pd

from quant_marketdata.venues.contracts.indexes import venue_index_id
from quant_marketdata.venues.contracts.overrides import (
    CRYPTO_BASES,
    KALSHI_SERIES,
    OPTION_UNDERLYINGS,
)
from quant_marketdata.venues.contracts.schema import (
    BINARY_CONTRACT_COLUMNS,
    OPTION_CONTRACT_COLUMNS,
    OPTION_TYPES,
    binary_condition,
    binary_contract_id,
    canonical_quote,
    frame_of,
    make_underlying_id,
    option_contract_id,
)

NameFields = Dict[str, object]  # {"base": str, "expiry_date": Timestamp (midnight), "strike": float, "option_type"}
NameParser = Callable[[str], Optional[NameFields]]

_NAME_PARSERS: Dict[str, NameParser] = {}

# Settlement fields kept once observed, whatever later listings say.
_STICKY = ("settlement_price", "settlement_source", "outcome", "result_raw", "settlement_value", "settled_at")


def register_name_parser(venue: str, parser: NameParser) -> None:
    """Declare how to read a venue's contract names, for the cross-check only."""
    _NAME_PARSERS[venue] = parser


def name_parser(venue: str) -> Optional[NameParser]:
    return _NAME_PARSERS.get(venue)


@dataclass
class ContractBuild:
    contracts: pd.DataFrame
    unresolved: pd.DataFrame = field(default_factory=pd.DataFrame)

    @property
    def ok(self) -> bool:
        return self.unresolved.empty


def _present(value: object) -> bool:
    return value is not None and value is not pd.NaT and not (isinstance(value, float) and math.isnan(value))


def _mismatches(row: dict, parsed: NameFields) -> list[str]:
    fields = []
    if parsed.get("base") != row["venue_base"]:
        fields.append("base")
    if pd.Timestamp(parsed["expiry_date"]).normalize() != pd.Timestamp(row["expiry"]).normalize():
        fields.append("expiry")
    if not math.isclose(float(parsed["strike"]), float(row["strike"]), rel_tol=1e-9, abs_tol=1e-12):
        fields.append("strike")
    if parsed.get("option_type") != row["option_type"]:
        fields.append("option_type")
    return fields


def _collisions(frame: pd.DataFrame, venue: str, by: Tuple[str, ...]) -> Tuple[pd.DataFrame, list]:
    if frame.empty:
        return frame, []
    duplicated = frame.duplicated(list(by), keep=False)
    unresolved = [{"venue": venue, "venue_symbol": s, "reason": f"collides on {c}"}
                  for s, c in frame.loc[duplicated, ["venue_symbol", "contract_id"]].itertuples(index=False)]
    return frame.loc[~duplicated].reset_index(drop=True), unresolved


def resolve_option_contracts(venue: str, snapshot: pd.DataFrame,
                             overrides: Optional[Dict[str, Dict[str, dict]]] = None,
                             crypto_bases: Iterable[str] = CRYPTO_BASES) -> ContractBuild:
    """Fill the identity columns of one venue's ``option_contracts()`` listing."""
    venue_overrides = (OPTION_UNDERLYINGS if overrides is None else overrides).get(venue, {})
    crypto_bases = frozenset(crypto_bases)
    parser = _NAME_PARSERS.get(venue)
    rows, unresolved = [], []
    for row in snapshot.to_dict("records"):
        symbol, native = row["venue_symbol"], row.get("venue_base")
        override = venue_overrides.get(native)
        if override is not None:
            identity = {"base": override.get("base", native),
                        "quote": override.get("quote", canonical_quote(row["venue_quote"])),
                        "asset_class": override["asset_class"],
                        "underlying_ref": override.get("underlying_ref")}
            source = "override"
        elif native in crypto_bases:
            identity = {"base": native, "quote": canonical_quote(row["venue_quote"]),
                        "asset_class": "crypto", "underlying_ref": None}
            source = "rule"
        else:
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": f"no rule or override for base {native!r}"})
            continue
        missing = [c for c in ("expiry", "strike", "option_type") if not _present(row.get(c))]
        if not missing and row["option_type"] not in OPTION_TYPES:
            missing.append("option_type")
        if not missing and not float(row["strike"]) > 0:
            missing.append("strike")
        if missing:
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": f"missing or invalid structured field(s): {', '.join(missing)}"})
            continue
        if parser is not None:
            parsed = parser(symbol)
            if parsed is None:
                unresolved.append({"venue": venue, "venue_symbol": symbol, "reason": "name_unparsed"})
                continue
            wrong = _mismatches(row, parsed)
            if wrong:
                unresolved.append({"venue": venue, "venue_symbol": symbol,
                                   "reason": f"name_fields_mismatch: {', '.join(wrong)}"})
                continue
        underlying = make_underlying_id(identity["base"], identity["quote"])
        raw_index = row.get("settlement_index_raw")
        rows.append({
            **row,
            **identity,
            "underlying_id": underlying,
            "contract_id": option_contract_id(underlying, row["expiry"], row["strike"], row["option_type"]),
            "settlement_index_id": venue_index_id(venue, raw_index) if _present(raw_index) else None,
            "mapping_source": source,
        })
    frame = frame_of(rows, OPTION_CONTRACT_COLUMNS)
    frame, collided = _collisions(frame, venue, ("contract_id",))
    return ContractBuild(frame, pd.DataFrame(unresolved + collided))


def resolve_binary_contracts(venue: str, snapshot: pd.DataFrame,
                             series_table: Optional[Dict[str, dict]] = None) -> ContractBuild:
    """Fill the identity columns of one venue's ``binary_contracts()`` listing from the series table."""
    table = KALSHI_SERIES if series_table is None else series_table
    rows, unresolved = [], []
    for row in snapshot.to_dict("records"):
        symbol, series = row["venue_symbol"], row.get("venue_series")
        entry = table.get(series)
        if entry is None:
            unresolved.append({"venue": venue, "venue_symbol": symbol,
                               "reason": f"series {series!r} not in the series table"})
            continue
        if not _present(row.get("expiry")):
            unresolved.append({"venue": venue, "venue_symbol": symbol, "reason": "missing expiry"})
            continue
        try:
            condition = binary_condition(row.get("k_low"), row.get("k_high"))
        except ValueError as exc:
            condition, reason = None, str(exc)
        else:
            reason = f"no canonical bounds (strike_type {row.get('strike_type_raw')!r})"
        if condition is None:
            unresolved.append({"venue": venue, "venue_symbol": symbol, "reason": reason})
            continue
        rows.append({
            **row,
            "underlying_id": entry["underlying_id"],
            "settlement_index_id": entry["settlement_index_id"],
            "contract_id": binary_contract_id(entry["underlying_id"], row["expiry"], condition),
            "mapping_source": "series-table",
        })
    frame = frame_of(rows, BINARY_CONTRACT_COLUMNS)
    # Two series of one venue may list the same bet (KXBTC's upper tail is a KXBTCD threshold): ids are
    # unique within a series, the registry key is the venue symbol.
    frame, collided = _collisions(frame, venue, ("venue_series", "contract_id"))
    return ContractBuild(frame, pd.DataFrame(unresolved + collided))


def update_contract_registry(existing: pd.DataFrame, fresh: pd.DataFrame, as_of: pd.Timestamp, *,
                             scope: Optional[Tuple[str, Iterable]] = None) -> pd.DataFrame:
    """Merge a fresh resolved listing of **one venue** into its stored registry, point-in-time.

    ``scope = (column, values)`` names the stored rows the listing covers (default: options by
    ``venue_base``, binaries by ``venue_event_id``, with the listing's own values): a covered row missing from
    the listing is ``expired`` if its expiry has passed, ``delisted`` (at ``as_of``) otherwise. The listing
    must therefore be complete for its scope: live and expired contracts together.
    """
    as_of = pd.Timestamp(as_of)
    columns = BINARY_CONTRACT_COLUMNS if "venue_event_id" in fresh.columns else OPTION_CONTRACT_COLUMNS
    fresh = frame_of(fresh.to_dict("records"), columns) if not fresh.empty else fresh.reindex(columns=columns)
    if existing.empty:
        merged = fresh.copy()
        merged["first_seen_at"] = merged["first_seen_at"].fillna(as_of)
        marker = "settlement_price" if "settlement_price" in merged.columns else "settlement_value"
        seen = merged[marker].notna() & merged["settlement_seen_at"].isna()
        merged.loc[seen, "settlement_seen_at"] = as_of
        return _sorted(merged)
    if scope is None:
        key = "venue_event_id" if "venue_event_id" in fresh.columns else "venue_base"
        scope = (key, fresh[key].dropna().unique())
    prev = existing.set_index("venue_symbol")
    new = fresh.set_index("venue_symbol")

    def stored(column: str) -> pd.Series:
        """The stored value of ``column`` aligned on the fresh rows (missing for new contracts)."""
        if column not in prev.columns:
            return pd.Series(pd.NA, index=new.index, dtype="object")
        return prev[column].reindex(new.index)

    new["first_seen_at"] = stored("first_seen_at").fillna(new["first_seen_at"]).fillna(as_of)
    new["listed_at"] = pd.concat([new["listed_at"], stored("listed_at")], axis=1).min(axis=1)
    marker = "settlement_price" if "settlement_price" in new.columns else "settlement_value"
    had = stored(marker).notna()
    for column in _STICKY:
        if column in new.columns:
            before = stored(column)
            if before.notna().any():
                new[column] = new[column].where(new[column].notna(), before)
    seen = stored("settlement_seen_at")
    seen = seen.where(had, pd.NaT)
    seen = seen.where(~(new[marker].notna() & seen.isna()), as_of)
    new["settlement_seen_at"] = pd.to_datetime(seen).astype("datetime64[ns]")
    gone = prev.loc[prev.index.difference(new.index)].copy()
    column, values = scope
    covered = gone[column].isin(list(values))
    expired = covered & (gone["expiry"] <= as_of)
    delisted = covered & ~expired
    status = gone["status"].astype("object")
    gone["status"] = status.where(~expired, "expired").where(~delisted, "delisted")
    gone["delisted_at"] = gone["delisted_at"].where(~delisted | gone["delisted_at"].notna(), as_of)
    merged = pd.concat([new, gone]).reset_index().rename(columns={"index": "venue_symbol"})
    return _sorted(merged.reindex(columns=list(dict.fromkeys(columns + list(merged.columns)))))


def _sorted(frame: pd.DataFrame) -> pd.DataFrame:
    return frame.sort_values(["expiry", "venue_symbol"], kind="stable").reset_index(drop=True)


def universe_changes(existing: pd.DataFrame, merged: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """``universe_change`` events of a registry update: ``new_underlying``, ``new_expiry`` (per underlying)
    and, for binaries, ``new_series`` -- relative to the stored registry."""
    columns = ["ts", "venue", "change", "key"]
    if merged.empty:
        return pd.DataFrame(columns=columns)
    events = []
    venue = merged["venue"].iloc[0]

    def known(column_set):
        if existing.empty or not set(column_set) <= set(existing.columns):
            return set()
        return set(existing[list(column_set)].dropna().itertuples(index=False, name=None))

    checks = [("new_underlying", ("underlying_id",)), ("new_expiry", ("underlying_id", "expiry"))]
    if "venue_series" in merged.columns:
        checks.insert(0, ("new_series", ("venue_series",)))
    for change, cols in checks:
        seen = known(cols)
        for values in sorted(set(merged[list(cols)].dropna().itertuples(index=False, name=None)) - seen):
            key = "|".join(pd.Timestamp(v).strftime("%Y-%m-%dT%H:%M") if isinstance(v, pd.Timestamp) else str(v)
                           for v in values)
            events.append({"ts": pd.Timestamp(as_of), "venue": venue, "change": change, "key": key})
    return pd.DataFrame(events, columns=columns)


__all__ = [
    "ContractBuild",
    "name_parser",
    "register_name_parser",
    "resolve_binary_contracts",
    "resolve_option_contracts",
    "universe_changes",
    "update_contract_registry",
]
