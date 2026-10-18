"""Dated-contract layer core: canonical ids and bounds, fixing methods, contract registry, partitioned store."""

from __future__ import annotations

import math

import pandas as pd
import pytest

from quant_marketdata.venues import get_venue
from quant_marketdata.venues.contracts import (
    OPTION_QUOTE_COLUMNS,
    ContractStore,
    binary_condition,
    binary_contract_id,
    format_strike,
    kalshi_fee,
    option_contract_id,
    outcome_from_bounds,
    p_mid,
    parse_fixing_method,
    register_name_parser,
    resolve_binary_contracts,
    resolve_option_contracts,
    settlement_index_info,
    universe_changes,
    update_contract_registry,
)
from quant_marketdata.venues.contracts.registry import _NAME_PARSERS
from quant_marketdata.venues.schema import CONTRACT_DATASETS, PERP_DATASETS, Dataset

RULE_2026 = ("If the simple average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) "
             "before 4 AM EDT is above 78799.99 at 4 AM EDT on Oct 18, 2026, then the market resolves to Yes.")
RULE_2025 = ("If the average of the sixty seconds of CF Benchmarks' Bitcoin Real-Time Index (BRTI) before 5 PM "
             "EST, excluding the top 20% and bottom 20% of values is above 99749.99 at 5 PM EST on Jan 1, "
             "2025, then the market resolves to Yes.")

# -- ids, bounds, values -----------------------------------------------------------------------------------


def test_strikes_in_minimal_decimal_notation():
    assert format_strike(82000.0) == "82000"
    assert format_strike("0.140") == "0.14"
    assert format_strike(8.5) == "8.5"
    assert format_strike(1e-5) == "0.00001"
    with pytest.raises(ValueError):
        format_strike(float("nan"))


def test_option_and_binary_ids():
    expiry = pd.Timestamp("2026-10-23 08:00")
    assert option_contract_id("BTC-USD", expiry, 82000.0, "C") == "BTC-USD-20261023T0800-82000-C"
    assert option_contract_id("ADA-USD", expiry, 0.14, "P") == "ADA-USD-20261023T0800-0.14-P"
    with pytest.raises(ValueError):
        option_contract_id("BTC-USD", expiry, 1, "CALL")
    assert binary_condition(81500.0, float("nan")) == "GE81500"
    assert binary_condition(None, 72200) == "LT72200"
    assert binary_condition(74900, 75000) == "IN74900_75000"
    assert binary_condition(float("nan"), None) is None
    with pytest.raises(ValueError):
        binary_condition(75000, 74900)
    assert binary_contract_id("BTC-USD", pd.Timestamp("2026-10-18 10:00"), "GE81500") == \
        "BTC-USD-20261018T1000-GE81500"


def test_outcome_and_mid():
    assert outcome_from_bounds(81568.22, 81500, None) == 1.0
    assert outcome_from_bounds(81499.99, 81500, None) == 0.0
    assert outcome_from_bounds(75000.0, 74900, 75000) == 0.0  # upper bound excluded
    assert outcome_from_bounds(74900.0, 74900, 75000) == 1.0
    assert math.isnan(outcome_from_bounds(None, 1, 2))
    assert math.isnan(p_mid(0.4, float("nan")))  # never 0.5 by default
    assert p_mid(0.4, 0.5) == pytest.approx(0.45)
    mids = p_mid(pd.Series([0.4, None]), pd.Series([0.5, 0.6]))
    assert mids.iloc[0] == pytest.approx(0.45) and math.isnan(mids.iloc[1])


def test_fixing_methods_read_per_market_by_explicit_patterns():
    assert parse_fixing_method(RULE_2026) == ("mean", 60.0)
    assert parse_fixing_method(RULE_2025) == ("trimmed_mean_20pct", 60.0)
    method, window = parse_fixing_method("If the median of BRTI at noon is above 1, then Yes.")
    assert method == "unparsed" and math.isnan(window)
    assert parse_fixing_method(None)[0] == "unparsed"


def test_settlement_indexes_declared_or_unknown():
    assert settlement_index_info("deribit:btc_usd")["method"] == "twap_30min"
    assert settlement_index_info("deribit:btc_usd")["method_source"] == "venue_doc"
    assert settlement_index_info("cfb:BRTI")["method_source"] == "rules_text"
    assert settlement_index_info("paradex:BTC")["method"] == "unknown"


def test_kalshi_quadratic_fee_rounds_up_to_the_cent():
    assert kalshi_fee(0.5, 100) == pytest.approx(1.75)  # 0.07 x 100 x 0.25
    assert kalshi_fee(0.5, 1) == pytest.approx(0.02)  # 0.0175 rounded up
    assert kalshi_fee(0.99, 1) == pytest.approx(0.01)
    assert kalshi_fee(0.5, 100, coefficient=0.0175) == pytest.approx(0.44)
    with pytest.raises(ValueError):
        kalshi_fee(1.5, 1)


def test_contract_datasets_are_not_perp_capabilities():
    assert Dataset.OPTION_CHAIN in CONTRACT_DATASETS and not CONTRACT_DATASETS & PERP_DATASETS
    assert get_venue("lighter").capabilities == PERP_DATASETS


# -- option registry ---------------------------------------------------------------------------------------


def _options(rows):
    base = {"venue": "testvenue", "venue_market_id": None, "settle_asset": "USDC", "venue_quote": "USDC",
            "style": "european", "exercise": "cash", "contract_size": 1.0, "price_unit": "quote",
            "settlement_index_raw": "BTC-USD", "status": "active", "settlement_price": float("nan"),
            "listed_at": pd.Timestamp("2026-10-01"), "delisted_at": pd.NaT,
            "settlement_seen_at": pd.NaT, "first_seen_at": pd.NaT}
    return pd.DataFrame([{**base, **row} for row in rows])


def _parse_test_name(symbol):
    parts = symbol.split("-")
    if len(parts) != 4:
        return None
    return {"base": parts[0], "expiry_date": pd.Timestamp(parts[1]), "strike": float(parts[2]),
            "option_type": parts[3]}


@pytest.fixture
def name_parser():
    register_name_parser("testvenue", _parse_test_name)
    yield
    _NAME_PARSERS.pop("testvenue", None)


def test_resolve_options_rule_override_and_unresolved(name_parser):
    expiry = pd.Timestamp("2026-10-23 08:00")
    snapshot = _options([
        {"venue_symbol": "BTC-20261023-82000-C", "venue_base": "BTC", "expiry": expiry, "strike": 82000.0,
         "option_type": "C"},
        {"venue_symbol": "XAUT-20261023-4000-P", "venue_base": "XAUT", "expiry": expiry, "strike": 4000.0,
         "option_type": "P"},
        {"venue_symbol": "CL-20261023-60-C", "venue_base": "CL", "expiry": expiry, "strike": 60.0,
         "option_type": "C"},
        {"venue_symbol": "BTC-20261023-83000-C", "venue_base": "BTC", "expiry": expiry, "strike": 84000.0,
         "option_type": "C"},
        {"venue_symbol": "BTC-20261024-85000-P", "venue_base": "BTC", "expiry": expiry, "strike": 85000.0,
         "option_type": "P"},
        {"venue_symbol": "ETH-20261023-2000-X", "venue_base": "ETH", "expiry": expiry, "strike": 2000.0,
         "option_type": "X"},
        {"venue_symbol": "BTC-oops", "venue_base": "BTC", "expiry": expiry, "strike": 1.0, "option_type": "C"},
    ])
    overrides = {"testvenue": {"XAUT": {"asset_class": "crypto", "underlying_ref": "XAU", "why": "token"}}}
    build = resolve_option_contracts("testvenue", snapshot, overrides=overrides)
    contracts = build.contracts.set_index("venue_symbol")
    assert contracts.loc["BTC-20261023-82000-C", "contract_id"] == "BTC-USD-20261023T0800-82000-C"
    assert contracts.loc["BTC-20261023-82000-C", "mapping_source"] == "rule"
    assert contracts.loc["BTC-20261023-82000-C", "settlement_index_id"] == "testvenue:BTC-USD"
    assert contracts.loc["XAUT-20261023-4000-P", "underlying_id"] == "XAUT-USD"
    assert contracts.loc["XAUT-20261023-4000-P", "underlying_ref"] == "XAU"
    assert contracts.loc["XAUT-20261023-4000-P", "mapping_source"] == "override"
    reasons = dict(zip(build.unresolved["venue_symbol"], build.unresolved["reason"], strict=True))
    assert reasons["CL-20261023-60-C"] == "no rule or override for base 'CL'"
    assert reasons["BTC-20261023-83000-C"] == "name_fields_mismatch: strike"
    assert reasons["BTC-20261024-85000-P"] == "name_fields_mismatch: expiry"
    assert reasons["ETH-20261023-2000-X"].startswith("missing or invalid structured field")
    assert reasons["BTC-oops"] == "name_unparsed"
    assert not build.ok


def test_resolve_options_reports_collisions():
    expiry = pd.Timestamp("2026-10-23 08:00")
    snapshot = _options([
        {"venue_symbol": "A", "venue_base": "BTC", "expiry": expiry, "strike": 1.0, "option_type": "C"},
        {"venue_symbol": "B", "venue_base": "BTC", "expiry": expiry, "strike": 1.0, "option_type": "C"},
    ])
    build = resolve_option_contracts("testvenue", snapshot)
    assert build.contracts.empty and set(build.unresolved["venue_symbol"]) == {"A", "B"}


def test_registry_point_in_time(name_parser):
    t0 = pd.Timestamp("2026-10-20 09:00")
    near, far = pd.Timestamp("2026-10-21 08:00"), pd.Timestamp("2026-10-30 08:00")
    first = resolve_option_contracts("testvenue", _options([
        {"venue_symbol": "BTC-20261021-80000-C", "venue_base": "BTC", "expiry": near, "strike": 80000.0,
         "option_type": "C"},
        {"venue_symbol": "BTC-20261030-80000-C", "venue_base": "BTC", "expiry": far, "strike": 80000.0,
         "option_type": "C"},
        {"venue_symbol": "ETH-20261030-2000-C", "venue_base": "ETH", "expiry": far, "strike": 2000.0,
         "option_type": "C"},
    ])).contracts
    registry = update_contract_registry(pd.DataFrame(), first, t0)
    assert (registry["first_seen_at"] == t0).all() and registry["settlement_seen_at"].isna().all()

    # A day later: the near BTC contract expired and settled, the far one vanished early, ETH not listed.
    t1 = pd.Timestamp("2026-10-21 09:00")
    second = resolve_option_contracts("testvenue", _options([
        {"venue_symbol": "BTC-20261021-80000-C", "venue_base": "BTC", "expiry": near, "strike": 80000.0,
         "option_type": "C", "status": "expired", "settlement_price": 81000.0},
    ])).contracts
    registry = update_contract_registry(registry, second, t1).set_index("venue_symbol")
    assert len(registry) == 3  # nothing removed
    settled = registry.loc["BTC-20261021-80000-C"]
    assert settled["first_seen_at"] == t0 and settled["settlement_seen_at"] == t1
    assert settled["settlement_price"] == 81000.0
    gone = registry.loc["BTC-20261030-80000-C"]
    assert gone["status"] == "delisted" and gone["delisted_at"] == t1
    assert registry.loc["ETH-20261030-2000-C", "status"] == "active"  # outside the listing's scope

    # Later listings without the settlement keep it; expiry passed -> expired, not delisted.
    t2 = pd.Timestamp("2026-10-31 09:00")
    registry = update_contract_registry(registry.reset_index(), second.assign(settlement_price=float("nan")),
                                        t2, scope=("venue_base", ["BTC", "ETH"])).set_index("venue_symbol")
    assert registry.loc["BTC-20261021-80000-C", "settlement_price"] == 81000.0
    assert registry.loc["BTC-20261021-80000-C", "settlement_seen_at"] == t1
    assert registry.loc["ETH-20261030-2000-C", "status"] == "expired"
    assert registry.loc["BTC-20261030-80000-C", "delisted_at"] == t1


def test_universe_changes(name_parser):
    expiry = pd.Timestamp("2026-10-23 08:00")
    rows = [{"venue_symbol": "BTC-20261023-82000-C", "venue_base": "BTC", "expiry": expiry,
             "strike": 82000.0, "option_type": "C"}]
    first = resolve_option_contracts("testvenue", _options(rows)).contracts
    events = universe_changes(pd.DataFrame(), first, pd.Timestamp("2026-10-18"))
    assert set(events["change"]) == {"new_underlying", "new_expiry"}
    rows.append({"venue_symbol": "BTC-20261030-82000-C", "venue_base": "BTC",
                 "expiry": pd.Timestamp("2026-10-30 08:00"), "strike": 82000.0, "option_type": "C"})
    second = resolve_option_contracts("testvenue", _options(rows)).contracts
    events = universe_changes(first, second, pd.Timestamp("2026-10-19"))
    assert events[["change", "key"]].values.tolist() == [["new_expiry", "BTC-USD|2026-10-30T08:00"]]


# -- binary registry ---------------------------------------------------------------------------------------


def test_resolve_binaries_from_the_series_table():
    expiry = pd.Timestamp("2026-10-18 10:00")
    snapshot = pd.DataFrame([
        {"venue": "kalshi", "venue_symbol": "KXBTCD-26OCT1806-T81499.99", "venue_event_id": "KXBTCD-26OCT1806",
         "venue_series": "KXBTCD", "expiry": expiry, "k_low": 81500.0, "k_high": float("nan"),
         "strike_type_raw": "greater"},
        {"venue": "kalshi", "venue_symbol": "KXBTC-26OCT1806-T90799.99", "venue_event_id": "KXBTC-26OCT1806",
         "venue_series": "KXBTC", "expiry": expiry, "k_low": 90800.0, "k_high": float("nan"),
         "strike_type_raw": "greater"},
        {"venue": "kalshi", "venue_symbol": "KXBTCD-26OCT1806-T90799.99", "venue_event_id": "KXBTCD-26OCT1806",
         "venue_series": "KXBTCD", "expiry": expiry, "k_low": 90800.0, "k_high": float("nan"),
         "strike_type_raw": "greater"},
        {"venue": "kalshi", "venue_symbol": "KXSOLD-X", "venue_event_id": "KXSOLD-E", "venue_series": "KXSOLD",
         "expiry": expiry, "k_low": 1.0, "k_high": float("nan"), "strike_type_raw": "greater"},
        {"venue": "kalshi", "venue_symbol": "KXBTCD-UP", "venue_event_id": "KXBTCD-26OCT1806",
         "venue_series": "KXBTCD", "expiry": expiry, "k_low": float("nan"), "k_high": float("nan"),
         "strike_type_raw": "custom"},
    ])
    build = resolve_binary_contracts("kalshi", snapshot)
    contracts = build.contracts.set_index("venue_symbol")
    assert contracts.loc["KXBTCD-26OCT1806-T81499.99", "contract_id"] == "BTC-USD-20261018T1000-GE81500"
    assert contracts.loc["KXBTCD-26OCT1806-T81499.99", "settlement_index_id"] == "cfb:BRTI"
    assert contracts.loc["KXBTCD-26OCT1806-T81499.99", "mapping_source"] == "series-table"
    # Same bet in two series: both kept, the venue symbol is the key.
    assert contracts.loc["KXBTC-26OCT1806-T90799.99", "contract_id"] == \
        contracts.loc["KXBTCD-26OCT1806-T90799.99", "contract_id"]
    reasons = dict(zip(build.unresolved["venue_symbol"], build.unresolved["reason"], strict=True))
    assert reasons["KXSOLD-X"] == "series 'KXSOLD' not in the series table"
    assert reasons["KXBTCD-UP"].startswith("no canonical bounds")


def test_binary_registry_keeps_outcomes():
    expiry = pd.Timestamp("2026-10-18 10:00")
    row = {"venue": "kalshi", "venue_symbol": "M", "venue_event_id": "E", "venue_series": "KXBTCD",
           "expiry": expiry, "k_low": 81500.0, "k_high": float("nan"), "status": "active",
           "outcome": float("nan"), "settlement_value": float("nan")}
    open_ = resolve_binary_contracts("kalshi", pd.DataFrame([row])).contracts
    registry = update_contract_registry(pd.DataFrame(), open_, pd.Timestamp("2026-10-18 09:10"))
    settled = resolve_binary_contracts("kalshi", pd.DataFrame([{
        **row, "status": "expired", "outcome": 1.0, "settlement_value": 81568.22, "result_raw": "yes"}])).contracts
    registry = update_contract_registry(registry, settled, pd.Timestamp("2026-10-18 11:00"))
    again = update_contract_registry(registry, open_, pd.Timestamp("2026-10-18 12:00"))
    assert again["outcome"].iloc[0] == 1.0 and again["result_raw"].iloc[0] == "yes"
    assert again["settlement_seen_at"].iloc[0] == pd.Timestamp("2026-10-18 11:00")


# -- store --------------------------------------------------------------------------------------------------


def _chain(ts_snapshot, n=3):
    rows = [{"ts_snapshot": pd.Timestamp(ts_snapshot), "ts": pd.Timestamp(ts_snapshot), "venue": "derive",
             "venue_symbol": f"S{i}", "contract_id": f"C{i}", "bid": float(i)} for i in range(n)]
    return pd.DataFrame(rows).reindex(columns=OPTION_QUOTE_COLUMNS)


def test_store_layout(tmp_path):
    store = ContractStore.at(tmp_path)
    assert store.option_registry_path("derive") == tmp_path / "registry" / "options" / "derive.parquet"
    assert store.binary_registry_path("kalshi", "KXBTCD", "2026-10") == \
        tmp_path / "registry" / "binaries" / "kalshi" / "KXBTCD" / "2026-10.parquet"
    assert store.partition_path("derive", Dataset.OPTION_CHAIN, "BTC-USD", "2026-10-18") == \
        tmp_path / "venues" / "derive" / "option_chain" / "BTC-USD" / "2026-10-18.parquet"
    assert store.partition_path("kalshi", Dataset.BINARY_CANDLES, "KXBTCD", "2026-10", "1m") == \
        tmp_path / "venues" / "kalshi" / "binary_candles" / "KXBTCD" / "1m" / "2026-10.parquet"
    assert store.settlements_path("kalshi", "cfb:BRTI").name == "cfb_BRTI.parquet"


def test_snapshots_are_append_only_and_compacted(tmp_path):
    store = ContractStore.at(tmp_path)
    store.append_snapshot("derive", Dataset.OPTION_CHAIN, "BTC-USD", _chain("2026-10-18 09:00"))
    store.append_snapshot("derive", Dataset.OPTION_CHAIN, "BTC-USD", _chain("2026-10-18 09:15"))
    # A pass spanning midnight lands in two days.
    late = pd.concat([_chain("2026-10-18 23:59:59", 1), _chain("2026-10-19 00:00:01", 1)])
    store.append_snapshot("derive", Dataset.OPTION_CHAIN, "BTC-USD", late)
    assert {p[3] for p in store.pending("derive")} == {"2026-10-18", "2026-10-19"}
    rows = store.read_snapshots("derive", Dataset.OPTION_CHAIN, "BTC-USD", start="2026-10-18", end="2026-10-18")
    assert len(rows) == 7 and rows["ts_snapshot"].nunique() == 3
    done = store.compact_before("2026-10-19")
    assert done == [("derive", "option_chain", "BTC-USD", "2026-10-18", 7)]
    assert store.pending("derive") == [("derive", Dataset.OPTION_CHAIN, "BTC-USD", "2026-10-19")]
    # Parts written after compaction add to the day file, never replace it.
    store.append_snapshot("derive", Dataset.OPTION_CHAIN, "BTC-USD", _chain("2026-10-18 23:30"))
    assert store.compact("derive", Dataset.OPTION_CHAIN, "BTC-USD", "2026-10-18") == 10
    # A replayed part (crash between compaction and part removal) is dropped.
    store.append_snapshot("derive", Dataset.OPTION_CHAIN, "BTC-USD", _chain("2026-10-18 23:30"))
    assert store.compact("derive", Dataset.OPTION_CHAIN, "BTC-USD", "2026-10-18") == 10
    everything = store.read_snapshots("derive", Dataset.OPTION_CHAIN, "BTC-USD")
    assert len(everything) == 11 and list(everything.columns) == OPTION_QUOTE_COLUMNS
    with pytest.raises(ValueError):
        store.append_snapshot("derive", Dataset.OPTION_TRADES, "BTC-USD", _chain("2026-10-18"))


def test_monthly_partitions_merge_on_keys(tmp_path):
    store = ContractStore.at(tmp_path)
    frame = pd.DataFrame({"ts": pd.to_datetime(["2026-09-30 23:00", "2026-10-01 00:00", "2026-10-01 00:01"]),
                          "venue_symbol": ["M", "M", "M"], "volume": [1.0, 2.0, 3.0]})
    written = store.merge_monthly("kalshi", Dataset.BINARY_CANDLES, "KXBTCD", frame, ["venue_symbol", "ts"],
                                  resolution="1m")
    assert [(p.stem, n) for p, n in written] == [("2026-09", 1), ("2026-10", 2)]
    update = frame.iloc[[2]].assign(volume=30.0)
    store.merge_monthly("kalshi", Dataset.BINARY_CANDLES, "KXBTCD", update, ["venue_symbol", "ts"],
                        resolution="1m")
    stored = store.read_monthly("kalshi", Dataset.BINARY_CANDLES, "KXBTCD", resolution="1m")
    assert stored["volume"].tolist() == [1.0, 2.0, 30.0]
    assert store.last_partition("kalshi", Dataset.BINARY_CANDLES, "KXBTCD", "1m") == "2026-10"
    with pytest.raises(ValueError):
        store.merge_monthly("kalshi", Dataset.OPTION_CHAIN, "BTC-USD", frame, ["ts"])
