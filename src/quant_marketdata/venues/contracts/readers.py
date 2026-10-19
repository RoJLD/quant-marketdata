"""Readers of the dated-contract store, without estimation (estimators live in Quant-lab).

- ``load_chain(underlying_id, at)``: each venue's last chain snapshot at or before ``at``, joined to the
  contract registry (expiry, strike, type, settlement index...). ``ts_snapshot`` shows how fresh it is.
- ``load_ladder(series, event, at)``: a binary event's ladder at ``at``: the last snapshot, or else the
  close of the 1-minute candles (``quote_source = "candle_close"``), with the canonical bounds.
- ``load_settlements(index_ids, start, end)``: fixings of every venue; ``settlement_comparison`` lines them
  up per fixing instant with the gap to a reference index in basis points (the 08:00 UTC index gap).
"""

from __future__ import annotations

from typing import List, Optional, Sequence

import pandas as pd

from quant_marketdata.venues.contracts.schema import p_mid
from quant_marketdata.venues.contracts.store import ContractStore, day_of
from quant_marketdata.venues.schema import Dataset

CHAIN_REGISTRY_COLUMNS = ["contract_id", "underlying_id", "expiry", "option_type", "strike", "contract_size",
                          "price_unit", "settle_asset", "settlement_index_id", "status"]
LADDER_REGISTRY_COLUMNS = ["contract_id", "underlying_id", "cadence", "k_low", "k_high", "expiry",
                           "settlement_index_id", "fixing_method", "fixing_window_s", "payout", "status",
                           "outcome", "settlement_value"]


def _venues(store: ContractStore, dataset: Dataset, key: str, venues: Optional[Sequence[str]]) -> List[str]:
    base = store.root / "venues"
    found = sorted(p.name for p in base.glob("*") if (p / dataset.value / key).is_dir()) if base.is_dir() else []
    return [v for v in found if venues is None or v in venues]


def _last_pass(store: ContractStore, venue: str, dataset: Dataset, key: str, at: pd.Timestamp,
               rows_filter=None) -> pd.DataFrame:
    """Rows of the last snapshot pass at or before ``at`` (walking back day by day)."""
    for day in reversed([d for d in store.snapshot_days(venue, dataset, key) if d <= day_of(at)]):
        rows = store.read_snapshots(venue, dataset, key, start=day, end=day)
        if rows.empty:
            continue
        rows = rows[rows["ts_snapshot"] <= at]
        if rows_filter is not None:
            rows = rows_filter(rows)
        if not rows.empty:
            return rows[rows["ts_snapshot"] == rows["ts_snapshot"].max()]
    return pd.DataFrame()


def load_option_registry(venues: Optional[Sequence[str]] = None, root=None) -> pd.DataFrame:
    store = ContractStore.at(root)
    directory = store.root / "registry" / "options"
    paths = sorted(directory.glob("*.parquet")) if directory.is_dir() else []
    frames = [store.read(p) for p in paths if venues is None or p.stem in venues]
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_binary_registry(series: str, venue: str = "kalshi", root=None) -> pd.DataFrame:
    store = ContractStore.at(root)
    directory = store.binary_registry_path(venue, series, "x").parent
    frames = [store.read(p) for p in sorted(directory.glob("*.parquet"))] if directory.is_dir() else []
    frames = [f for f in frames if not f.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def load_chain(underlying_id: str, at=None, venues: Optional[Sequence[str]] = None, root=None) -> pd.DataFrame:
    """Last chain snapshot per venue at or before ``at`` (default: now), with registry columns."""
    store = ContractStore.at(root)
    at = pd.Timestamp.now(tz="UTC").tz_localize(None) if at is None else pd.Timestamp(at)
    registry = load_option_registry(venues, root)
    frames = []
    for venue in _venues(store, Dataset.OPTION_CHAIN, underlying_id, venues):
        rows = _last_pass(store, venue, Dataset.OPTION_CHAIN, underlying_id, at)
        if not rows.empty:
            frames.append(rows)
    if not frames:
        return pd.DataFrame()
    chain = pd.concat(frames, ignore_index=True).drop(columns=["contract_id"], errors="ignore")
    if registry.empty:
        return chain
    keep = ["venue", "venue_symbol"] + [c for c in CHAIN_REGISTRY_COLUMNS if c in registry.columns]
    return chain.merge(registry[keep], on=["venue", "venue_symbol"], how="left")


def load_ladder(series: str, event: str, at=None, venue: str = "kalshi", root=None) -> pd.DataFrame:
    """A binary event's ladder at ``at``: last snapshot quotes, else 1-minute candle closes."""
    store = ContractStore.at(root)
    at = pd.Timestamp.now(tz="UTC").tz_localize(None) if at is None else pd.Timestamp(at)
    registry = load_binary_registry(series, venue, root)
    if not registry.empty:
        registry = registry[registry["venue_event_id"] == event]
    rows = _last_pass(store, venue, Dataset.BINARY_QUOTES, series, at,
                      rows_filter=lambda r: r[r["venue_event_id"] == event])
    if rows.empty and not registry.empty:
        candles = store.read_monthly(venue, Dataset.BINARY_CANDLES, series, resolution="1m",
                                     start=at.strftime("%Y-%m"), end=at.strftime("%Y-%m"))
        if not candles.empty:
            candles = candles[candles["venue_symbol"].isin(registry["venue_symbol"]) & (candles["ts"] <= at)]
            candles = candles.sort_values("ts").groupby("venue_symbol", as_index=False).last()
            rows = pd.DataFrame({
                "ts_snapshot": pd.NaT, "ts": candles["ts"], "venue": venue,
                "venue_symbol": candles["venue_symbol"], "venue_event_id": event,
                "yes_bid": candles["yes_bid_close"], "yes_ask": candles["yes_ask_close"],
                "p_mid": p_mid(candles["yes_bid_close"], candles["yes_ask_close"]),
                "last": candles["price_close"], "volume": candles["volume"],
                "open_interest": candles["open_interest"], "quote_source": "candle_close",
            })
    if rows.empty:
        return pd.DataFrame()
    rows = rows.drop(columns=["contract_id"], errors="ignore")
    if not registry.empty:
        keep = ["venue_symbol"] + [c for c in LADDER_REGISTRY_COLUMNS if c in registry.columns]
        rows = rows.merge(registry[keep], on="venue_symbol", how="left")
    order = [c for c in ("k_low", "k_high", "venue_symbol") if c in rows.columns]
    return rows.sort_values(order, kind="stable").reset_index(drop=True)


def load_settlements(index_ids: Optional[Sequence[str]] = None, start=None, end=None,
                     venues: Optional[Sequence[str]] = None, root=None) -> pd.DataFrame:
    store = ContractStore.at(root)
    base = store.root / "venues"
    frames = []
    for path in sorted(base.glob(f"*/{Dataset.SETTLEMENTS.value}/*.parquet")) if base.is_dir() else []:
        if venues is not None and path.parent.parent.name not in venues:
            continue
        frame = store.read(path)
        if not frame.empty:
            frames.append(frame)
    if not frames:
        return pd.DataFrame()
    frame = pd.concat(frames, ignore_index=True)
    if index_ids is not None:
        frame = frame[frame["settlement_index_id"].isin(index_ids)]
    if start is not None:
        frame = frame[frame["ts"] >= pd.Timestamp(start)]
    if end is not None:
        frame = frame[frame["ts"] <= pd.Timestamp(end)]
    return frame.sort_values(["ts", "settlement_index_id"], kind="stable").reset_index(drop=True)


def settlement_comparison(settlements: pd.DataFrame, reference: str = "cfb:BRTI") -> pd.DataFrame:
    """One row per fixing instant with every index's value and ``gap_bp:<index>`` = (index / reference - 1)
    x 1e4. Only instants where the reference and another index were both fixed are kept. Logged, never
    corrected."""
    if settlements.empty:
        return pd.DataFrame()
    wide = settlements.pivot_table(index="ts", columns="settlement_index_id", values="settlement_price",
                                   aggfunc="first")
    if reference not in wide.columns:
        return pd.DataFrame()
    others = [c for c in wide.columns if c != reference]
    wide = wide[wide[reference].notna() & wide[others].notna().any(axis=1)]
    for column in others:
        wide[f"gap_bp:{column}"] = (wide[column] / wide[reference] - 1) * 1e4
    wide.columns.name = None
    return wide


__all__ = [
    "load_binary_registry",
    "load_chain",
    "load_ladder",
    "load_option_registry",
    "load_settlements",
    "settlement_comparison",
]
