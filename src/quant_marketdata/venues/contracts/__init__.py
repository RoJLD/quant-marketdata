"""Dated contracts: options and binaries (prediction markets), next to the perp venue layer.

- ``schema``: canonical columns of contracts, chain and quote snapshots, trades, candles, settlements, and
  the canonical ids (``option_contract_id``, ``binary_contract_id``);
- ``protocol``: ``OptionsVenue`` and ``BinaryVenue``;
- ``registry``: point-in-time contract registry (``resolve_option_contracts``,
  ``resolve_binary_contracts``, ``update_contract_registry``), separate from the perp registry;
- ``overrides``, ``indexes``, ``fees``: the explicit tables (underlying overrides, Kalshi series,
  settlement indexes and fixing methods, fee models);
- ``store``: day/month partitions and append-only snapshots (``ContractStore``).

Spec: Quant-lab ``docs/specs/2026-10-18-options-prediction-markets-design.md``. Data only: estimators
(implied probabilities, distributions, hourly vol) live in Quant-lab.
"""

from quant_marketdata.venues.contracts.fees import FEE_MODELS, kalshi_fee
from quant_marketdata.venues.contracts.indexes import (
    SETTLEMENT_INDEXES,
    parse_fixing_method,
    settlement_index_info,
    venue_index_id,
)
from quant_marketdata.venues.contracts.overrides import (
    CRYPTO_BASES,
    KALSHI_SERIES,
    OPTION_UNDERLYINGS,
)
from quant_marketdata.venues.contracts.protocol import BinaryVenue, OptionsVenue
from quant_marketdata.venues.contracts.registry import (
    ContractBuild,
    register_name_parser,
    resolve_binary_contracts,
    resolve_option_contracts,
    universe_changes,
    update_contract_registry,
)
from quant_marketdata.venues.contracts.schema import (
    BINARY_CANDLE_COLUMNS,
    BINARY_CONTRACT_COLUMNS,
    BINARY_QUOTE_COLUMNS,
    BINARY_SNAPSHOT_COLUMNS,
    BINARY_TRADE_COLUMNS,
    OPTION_CONTRACT_COLUMNS,
    OPTION_MARK_CANDLE_COLUMNS,
    OPTION_QUOTE_COLUMNS,
    OPTION_SNAPSHOT_COLUMNS,
    OPTION_TRADE_COLUMNS,
    SETTLEMENT_COLUMNS,
    binary_condition,
    binary_contract_id,
    canonical_quote,
    format_strike,
    frame_of,
    make_underlying_id,
    option_contract_id,
    outcome_from_bounds,
    p_mid,
)
from quant_marketdata.venues.contracts.store import ContractStore

__all__ = [
    "BINARY_CANDLE_COLUMNS",
    "BINARY_CONTRACT_COLUMNS",
    "BINARY_QUOTE_COLUMNS",
    "BINARY_SNAPSHOT_COLUMNS",
    "BINARY_TRADE_COLUMNS",
    "BinaryVenue",
    "CRYPTO_BASES",
    "ContractBuild",
    "ContractStore",
    "FEE_MODELS",
    "KALSHI_SERIES",
    "OPTION_CONTRACT_COLUMNS",
    "OPTION_MARK_CANDLE_COLUMNS",
    "OPTION_QUOTE_COLUMNS",
    "OPTION_SNAPSHOT_COLUMNS",
    "OPTION_TRADE_COLUMNS",
    "OPTION_UNDERLYINGS",
    "OptionsVenue",
    "SETTLEMENT_COLUMNS",
    "SETTLEMENT_INDEXES",
    "binary_condition",
    "binary_contract_id",
    "canonical_quote",
    "format_strike",
    "frame_of",
    "kalshi_fee",
    "make_underlying_id",
    "option_contract_id",
    "outcome_from_bounds",
    "p_mid",
    "parse_fixing_method",
    "register_name_parser",
    "resolve_binary_contracts",
    "resolve_option_contracts",
    "settlement_index_info",
    "universe_changes",
    "update_contract_registry",
    "venue_index_id",
]
