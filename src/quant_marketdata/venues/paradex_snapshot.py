"""Recording what Paradex forgets: its option listing, chains and mark candles exist only while a contract
lives, and an expired contract's fixing can only be asked for by name. Three passes over a ``ContractStore``
(spec 2026-10-18 §3.8), for ``qmd-snapshot`` and the daily options task to call:

- ``snapshot_pass`` (every 15 minutes, plus 07:58 UTC): the full listing into the contract registry
  (``registry/options/paradex.parquet``, point-in-time, nothing removed; ``universe_change`` events), then one
  chain snapshot per underlying (append-only parts under ``venues/paradex/option_chain/<UNDERLYING>/``), then
  the heartbeat. Measured 2026-10-19: ~16 MB listing + ~7 MB summary, BTC + ETH ~2,150 rows a pass.
- ``settle_expired`` (daily): the fixing of every expired registry contract still without one, one
  ``markets/settlement-price`` request per (base, expiry), into ``venues/paradex/settlements/<index>.parquet``
  and the registry (``settlement_price`` = the TWAP, ``settlement_source``, ``settlement_seen_at``).
- ``download_mark_candles`` (daily, before expiries): mark candles of every live contract of an underlying and
  the underlying (spot oracle) candles once per base, into monthly partitions, re-reading ``lookback`` so a
  short gap is closed.

Each step of a pass is independent: a failure is reported in the returned summary (and the heartbeat), the
other steps still run.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from quant_marketdata.venues.contracts.registry import (
    resolve_option_contracts,
    universe_changes,
    update_contract_registry,
)
from quant_marketdata.venues.contracts.schema import make_underlying_id
from quant_marketdata.venues.contracts.store import ContractStore
from quant_marketdata.venues.paradex_options import VENUE, settlement_rows
from quant_marketdata.venues.schema import Dataset

SNAPSHOT_UNDERLYINGS = ("BTC", "ETH")
SETTLEMENTS_FROM = pd.Timestamp("2026-05-21")  # earlier expiries answer MARKET_NOT_SETTLED
CANDLE_WORKERS = 8
CANDLE_LOOKBACK = pd.Timedelta(days=2)


def _now() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None)


def _error(exc: Exception) -> str:
    return f"{type(exc).__name__}: {exc}".splitlines()[0][:300]


def snapshot_pass(venue: Any, store: ContractStore, *, underlyings: Iterable[str] = SNAPSHOT_UNDERLYINGS,
                  ts_snapshot: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    """Registry update from the full listing, then one chain snapshot per underlying, then the heartbeat."""
    ts = _now() if ts_snapshot is None else pd.Timestamp(ts_snapshot)
    report: Dict[str, Any] = {"venue": VENUE, "ts_snapshot": ts, "contracts": 0, "unresolved": 0,
                              "universe_changes": 0, "chains": {}, "errors": {}}
    try:
        build = resolve_option_contracts(VENUE, venue.option_contracts())
        path = store.option_registry_path(VENUE)
        existing = store.read(path)
        merged = update_contract_registry(existing, build.contracts, ts)
        store.write(path, merged)
        report["universe_changes"] = store.append_universe_changes(
            VENUE, universe_changes(existing, merged, ts))
        report["contracts"], report["unresolved"] = len(build.contracts), len(build.unresolved)
        if not build.unresolved.empty:
            report["unresolved_sample"] = build.unresolved.head(5).to_dict("records")
    except Exception as exc:  # noqa: BLE001 -- a pass reports, it does not stop
        report["errors"]["registry"] = _error(exc)
    for underlying in underlyings:
        key = make_underlying_id(underlying, "USD")
        try:
            chain = venue.option_chain(underlying, ts_snapshot=ts)
            store.append_snapshot(VENUE, Dataset.OPTION_CHAIN, key, chain)
            report["chains"][key] = len(chain)
        except Exception as exc:  # noqa: BLE001
            report["errors"][key] = _error(exc)
    report["ok"] = not report["errors"]
    store.write_heartbeat(VENUE, report)
    return report


def settle_expired(venue: Any, store: ContractStore, *, as_of: Optional[pd.Timestamp] = None) -> Dict[str, Any]:
    """Fixings of the expired registry contracts without a settlement (from ``SETTLEMENTS_FROM`` on)."""
    as_of = _now() if as_of is None else pd.Timestamp(as_of)
    path = store.option_registry_path(VENUE)
    registry = store.read(path)
    report: Dict[str, Any] = {"venue": VENUE, "as_of": as_of, "fixings": 0, "contracts_settled": 0,
                              "errors": {}}
    if registry.empty:
        return report
    todo = registry[(registry["expiry"] <= as_of) & (registry["expiry"] >= SETTLEMENTS_FROM)
                    & registry["settlement_price"].isna()]
    if todo.empty:
        return report
    one_per_fixing = todo.drop_duplicates(["venue_base", "expiry"])["venue_symbol"]
    settled = venue.settlement_prices(one_per_fixing)
    if settled.empty:
        return report
    rows = settlement_rows(settled)
    for index_id, group in rows.groupby("settlement_index_id"):
        store.merge(store.settlements_path(VENUE, index_id), group, ["ts", "settlement_index_id"], sort=("ts",))
    report["fixings"] = len(rows)
    by_fixing = settled.set_index(["settlement_index_id", "expiry"])
    keys = list(zip(registry["settlement_index_id"], registry["expiry"], strict=True))
    hit = [k in by_fixing.index and pd.isna(p) for k, p in zip(keys, registry["settlement_price"], strict=True)]
    hit = pd.Series(hit, index=registry.index)
    for column in ("settlement_price", "settlement_source", "settlement_seen_at"):
        registry.loc[hit, column] = [by_fixing.at[k, column] for k, h in zip(keys, hit, strict=True) if h]
    registry["settlement_seen_at"] = pd.to_datetime(registry["settlement_seen_at"]).astype("datetime64[ns]")
    store.write(path, registry)
    report["contracts_settled"] = int(hit.sum())
    return report


def download_mark_candles(venue: Any, store: ContractStore, underlying: str, *, resolution: str = "1h",
                          lookback: pd.Timedelta = CANDLE_LOOKBACK, as_of: Optional[pd.Timestamp] = None,
                          workers: int = CANDLE_WORKERS) -> Dict[str, Any]:
    """Mark candles of the live contracts of one base (and its underlying candles, once), merged into
    ``option_mark_candles/<UNDERLYING>/<resolution>/<YYYY-MM>``. A contract never stored starts at its
    listing; the others re-read the last ``lookback``."""
    as_of = _now() if as_of is None else pd.Timestamp(as_of)
    key = make_underlying_id(underlying, "USD")
    live = venue.resolved_contracts(underlying)
    live = live[(live["status"] == "active") & (live["expiry"] > as_of)]
    stored = store.read_monthly(VENUE, Dataset.OPTION_MARK_CANDLES, key, resolution=resolution,
                                start=(as_of - lookback).strftime("%Y-%m"))
    seen = set(stored["venue_symbol"]) if not stored.empty else set()
    report: Dict[str, Any] = {"venue": VENUE, "underlying": key, "contracts": len(live), "rows": 0,
                              "errors": {}}

    def fetch(row) -> pd.DataFrame:
        start = as_of - lookback if row.venue_symbol in seen else row.listed_at
        return venue.option_mark_candles(row.venue_symbol, resolution, start, as_of)

    frames: List[pd.DataFrame] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(fetch, row): row.venue_symbol for row in live.itertuples()}
        for future, symbol in futures.items():
            try:
                frames.append(future.result())
            except Exception as exc:  # noqa: BLE001 -- one contract must not stop the others
                report["errors"][symbol] = _error(exc)
    if not live.empty:  # the spot oracle: one series per base, keyed by the underlying id
        first = live.sort_values("listed_at").iloc[0]
        start = as_of - lookback if key in seen else first["listed_at"]
        try:
            frames.append(venue.option_mark_candles(first["venue_symbol"], resolution, start, as_of,
                                                    price="underlying").assign(venue_symbol=key,
                                                                               contract_id=None))
        except Exception as exc:  # noqa: BLE001
            report["errors"]["underlying"] = _error(exc)
    frames = [f for f in frames if not f.empty]
    if frames:
        frame = pd.concat(frames, ignore_index=True)
        store.merge_monthly(VENUE, Dataset.OPTION_MARK_CANDLES, key, frame,
                            ["venue_symbol", "price_type", "ts"], resolution=resolution)
        report["rows"] = len(frame)
    return report


__all__ = ["SETTLEMENTS_FROM", "SNAPSHOT_UNDERLYINGS", "download_mark_candles", "settle_expired",
           "snapshot_pass"]
