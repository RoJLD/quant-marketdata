"""Canonical schemas of dated contracts: options, binaries, their quotes, trades, candles and settlements.

Spec: Quant-lab ``docs/specs/2026-10-18-options-prediction-markets-design.md`` (§3.2, §3.3).

Timestamps are naive UTC ``datetime64[ns]``. Unlike the perp tables, contract tables hold many rows per
timestamp, so time is a **column** (``ts``, ``ts_snapshot``, ``expiry``) on a plain ``RangeIndex``.

Canonical ids (the settlement index is **not** part of an id: two venues' contracts with the same id are not
the same bet, the index is a column that comparisons log and never equalize):

- underlying: ``<BASE>-<QUOTE>`` with USD/USDC/USDT collapsed to ``USD`` (``BTC-USD``, ``XAUT-USD``);
- option: ``<underlying_id>-<YYYYMMDD>T<HHMM>-<STRIKE>-<C|P>`` (``BTC-USD-20261023T0800-82000-C``), strike in
  minimal decimal notation (``0.14``, never Derive's ``0_14``); the hour is part of the id;
- binary: ``<underlying_id>-<YYYYMMDD>T<HHMM>-<CONDITION>`` with ``GE<k>`` (YES iff S_T >= k), ``LT<k>``
  (YES iff S_T < k) and ``IN<k_low>_<k_high>`` (YES iff k_low <= S_T < k_high).

Canonical units: option prices in USD per unit of underlying, sizes in units of underlying, IV as an
annualized fraction, ``delta``/``gamma`` per unit of the declared ``greeks_reference`` (``forward`` or
``spot``), ``vega`` in USD per vol point (0.01), ``theta`` in USD per calendar day. Binary quotes are YES
probabilities on [0, 1]; ``p_mid`` is NaN when a side is missing (never 0.5 by default). Binary bounds are
``k_low <= S_T < k_high`` at the fixing's resolution, NaN meaning unbounded. ``outcome`` is 1 when YES pays,
0 otherwise, NaN when not settled or voided.

Every converted value keeps its native value in a ``*_raw`` column; native fields without a canonical
equivalent are kept as ``raw_<field>`` columns. Nothing is read from a contract's name except to cross-check
the structured fields (see ``contracts.registry``).
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Iterable, Mapping, Optional

import pandas as pd

USD_QUOTES = frozenset({"USD", "USDC", "USDT"})

CONTRACT_STATUSES = frozenset({"active", "scheduled", "expired", "delisted"})
OPTION_TYPES = frozenset({"C", "P"})
IV_SOURCES = frozenset({"venue", "inverted"})
GREEKS_REFERENCES = frozenset({"forward", "spot"})
GREEKS_SOURCES = frozenset({"venue", "none"})
UNDERLYING_KINDS = frozenset({"forward", "future", "spot", "unknown"})
QUOTE_SOURCES = frozenset({"snapshot", "candle_close"})
METHOD_SOURCES = frozenset({"rules_text", "venue_doc", "unknown"})
MAPPING_SOURCES = frozenset({"rule", "override", "series-table"})
CADENCES = frozenset({"hourly", "daily", "weekly", "monthly", "annual", "other"})

# -- registries ----------------------------------------------------------------------------------------

OPTION_CONTRACT_COLUMNS = [
    "venue",
    "venue_symbol",
    "venue_market_id",
    "contract_id",
    "underlying_id",
    "base",
    "quote",
    "settle_asset",
    "asset_class",
    "underlying_ref",
    "venue_base",  # native base asset the underlying is resolved from (Derive ``base_currency``...)
    "venue_quote",  # native currency the strike is denominated in
    "expiry",
    "option_type",
    "strike",
    "style",
    "exercise",
    "contract_size",
    "price_unit",  # "quote" (USD) or "base" (inverse: Deribit BTC/ETH)
    "settlement_index_raw",
    "settlement_index_id",
    "tick_size",
    "min_size",
    "maker_fee_raw",
    "taker_fee_raw",
    "fee_extra_raw",  # JSON text (Derive base_fee and cap, Paradex fee_config...)
    "listed_at",
    "delisted_at",
    "status",
    "status_raw",
    "settlement_price",
    "settlement_source",
    "settlement_seen_at",
    "first_seen_at",
    "mapping_source",
]

# Filled by ``contracts.registry.resolve_option_contracts``, not by the adapters.
OPTION_RESOLVED_COLUMNS = ("contract_id", "underlying_id", "base", "quote", "asset_class",
                           "underlying_ref", "settlement_index_id", "first_seen_at", "mapping_source")
OPTION_SNAPSHOT_COLUMNS = [c for c in OPTION_CONTRACT_COLUMNS if c not in OPTION_RESOLVED_COLUMNS]

BINARY_CONTRACT_COLUMNS = [
    "venue",
    "venue_symbol",
    "venue_event_id",
    "venue_series",
    "contract_id",
    "underlying_id",
    "cadence",
    "strike_type_raw",
    "floor_strike_raw",
    "cap_strike_raw",
    "k_low",
    "k_high",
    "expiry",
    "trading_open",
    "trading_close",
    "expected_settlement_at",
    "latest_settlement_at",
    "settlement_index_id",
    "fixing_method",
    "fixing_window_s",
    "rules_text",
    "payout",
    "price_tick",
    "fee_type_raw",
    "fee_multiplier_raw",
    "outcome",
    "result_raw",
    "settlement_value",
    "settled_at",
    "settlement_seen_at",
    "listed_at",
    "delisted_at",
    "status",
    "status_raw",
    "first_seen_at",
    "mapping_source",
]

BINARY_RESOLVED_COLUMNS = ("contract_id", "underlying_id", "settlement_index_id", "first_seen_at",
                           "mapping_source")
BINARY_SNAPSHOT_COLUMNS = [c for c in BINARY_CONTRACT_COLUMNS if c not in BINARY_RESOLVED_COLUMNS]

# -- snapshots (one row per contract and per pass) ---------------------------------------------------

OPTION_QUOTE_COLUMNS = [
    "ts_snapshot",  # start of the pass, local clock
    "ts",  # venue timestamp
    "venue",
    "venue_symbol",
    "contract_id",
    "bid",
    "ask",
    "mark",
    "last",
    "bid_size",
    "ask_size",
    "iv_bid",
    "iv_ask",
    "iv_mark",
    "iv_source",
    "delta",
    "gamma",
    "vega",
    "theta",
    "rho",
    "vanna",
    "volga",
    "greeks_reference",
    "greeks_source",
    "underlying_price",
    "underlying_kind",
    "index_price",
    "forward_price",
    "forward_rate",
    "discount_factor",
    "open_interest",
    "volume_24h",
    "bid_raw",
    "ask_raw",
    "mark_raw",
    "last_raw",
    "iv_bid_raw",
    "iv_ask_raw",
    "iv_mark_raw",
]

BINARY_QUOTE_COLUMNS = [
    "ts_snapshot",
    "ts",
    "venue",
    "venue_symbol",
    "venue_event_id",
    "contract_id",
    "yes_bid",
    "yes_ask",
    "yes_bid_size",  # contracts; one contract pays ``payout`` if YES
    "yes_ask_size",
    "p_mid",
    "last",
    "volume",
    "volume_24h",
    "open_interest",
    "no_bid_raw",
    "no_ask_raw",
    "quote_source",
]

# -- history ---------------------------------------------------------------------------------------------

SETTLEMENT_COLUMNS = [
    "ts",  # fixing instant
    "venue",
    "settlement_index_id",
    "settlement_price",
    "method",
    "method_source",
    "source_endpoint",
    "venue_ref",  # what the venue keys the fixing by (Derive expiry date, Kalshi event ticker...)
    "first_seen_at",
]

OPTION_TRADE_COLUMNS = [
    "ts",
    "venue",
    "venue_symbol",
    "contract_id",
    "trade_id",
    "price",
    "size",
    "side",  # taker side: "buy" or "sell"
    "trade_type",
    "mark_at_trade",
    "index_at_trade",
    "iv_at_trade",
    "price_raw",
    "mark_at_trade_raw",
    "iv_at_trade_raw",
]

BINARY_TRADE_COLUMNS = [
    "ts",
    "venue",
    "venue_symbol",
    "contract_id",
    "trade_id",
    "price",  # YES price
    "size",
    "side",  # taker side: "yes" or "no"
    "trade_type",
    "no_price_raw",
]

BINARY_CANDLE_COLUMNS = [
    "ts",  # end of the period
    "venue",
    "venue_symbol",
    "contract_id",
    "period",
    "yes_bid_open",
    "yes_bid_high",
    "yes_bid_low",
    "yes_bid_close",
    "yes_ask_open",
    "yes_ask_high",
    "yes_ask_low",
    "yes_ask_close",
    "price_open",
    "price_high",
    "price_low",
    "price_close",
    "price_mean",
    "price_previous",
    "volume",
    "open_interest",
]

OPTION_MARK_CANDLE_COLUMNS = ["ts", "venue", "venue_symbol", "contract_id", "open", "high", "low", "close",
                              "volume_base", "volume_quote", "price_type"]

DATETIME_COLUMNS = frozenset({
    "ts", "ts_snapshot", "expiry", "listed_at", "delisted_at", "settlement_seen_at", "first_seen_at",
    "trading_open", "trading_close", "expected_settlement_at", "latest_settlement_at", "settled_at",
})

# -- identifiers -------------------------------------------------------------------------------------------


def canonical_quote(quote: str) -> str:
    """``USD`` for USD and the dollar stablecoins, the currency itself otherwise."""
    quote = str(quote).upper()
    return "USD" if quote in USD_QUOTES else quote


def make_underlying_id(base: str, quote: str) -> str:
    return f"{base}-{canonical_quote(quote)}"


def format_strike(value: float | str | Decimal) -> str:
    """Minimal decimal notation: ``82000``, ``0.14``, ``8.5`` (never an exponent nor trailing zeros)."""
    number = Decimal(str(value))
    if not number.is_finite():
        raise ValueError(f"Strike must be finite, got {value!r}")
    text = format(number.normalize(), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def format_expiry(expiry) -> str:
    return pd.Timestamp(expiry).strftime("%Y%m%dT%H%M")


def option_contract_id(underlying_id: str, expiry, strike, option_type: str) -> str:
    if option_type not in OPTION_TYPES:
        raise ValueError(f"option_type must be 'C' or 'P', got {option_type!r}")
    return f"{underlying_id}-{format_expiry(expiry)}-{format_strike(strike)}-{option_type}"


def _missing(value) -> bool:
    return value is None or (isinstance(value, float) and math.isnan(value)) or value is pd.NA


def binary_condition(k_low, k_high) -> Optional[str]:
    """``GE<k>``, ``LT<k>`` or ``IN<k_low>_<k_high>`` from canonical bounds; ``None`` when both are missing."""
    low, high = _missing(k_low), _missing(k_high)
    if low and high:
        return None
    if high:
        return f"GE{format_strike(k_low)}"
    if low:
        return f"LT{format_strike(k_high)}"
    if not float(k_low) < float(k_high):
        raise ValueError(f"Empty binary range [{k_low}, {k_high})")
    return f"IN{format_strike(k_low)}_{format_strike(k_high)}"


def binary_contract_id(underlying_id: str, expiry, condition: str) -> str:
    return f"{underlying_id}-{format_expiry(expiry)}-{condition}"


# -- values ------------------------------------------------------------------------------------------------


def outcome_from_bounds(value, k_low, k_high) -> float:
    """1.0 when ``k_low <= value < k_high`` (missing bound = unbounded), 0.0 otherwise, NaN without a value."""
    if _missing(value):
        return float("nan")
    value = float(value)
    if not _missing(k_low) and value < float(k_low):
        return 0.0
    if not _missing(k_high) and value >= float(k_high):
        return 0.0
    return 1.0


def p_mid(bid, ask):
    """Mid probability; NaN wherever one side is missing (``bid``/``ask`` scalars or Series)."""
    if isinstance(bid, pd.Series) or isinstance(ask, pd.Series):
        return (pd.Series(bid, dtype="float64") + pd.Series(ask, dtype="float64")) / 2
    if _missing(bid) or _missing(ask):
        return float("nan")
    return (float(bid) + float(ask)) / 2


def to_float(value) -> float:
    """Float from a venue's number or numeric string; NaN for ``None``, ``""`` or ``null``."""
    if value is None or value == "":
        return float("nan")
    try:
        return float(value)
    except (TypeError, ValueError):
        return float("nan")


def utc_naive(value, unit: Optional[str] = None) -> pd.Timestamp:
    """Naive UTC timestamp from epoch numbers (``unit``) or ISO strings; ``NaT`` for missing values."""
    if value is None or value == "" or (isinstance(value, float) and math.isnan(value)):
        return pd.NaT
    stamp = pd.Timestamp(int(value), unit=unit) if unit else pd.Timestamp(value)
    if stamp.tzinfo is not None:
        stamp = stamp.tz_convert("UTC").tz_localize(None)
    return stamp


def frame_of(rows: Iterable[Mapping], columns: list[str]) -> pd.DataFrame:
    """Rows -> frame with the canonical columns first (in order), extra ``raw_*`` columns kept after them,
    and the time columns as ``datetime64[ns]``."""
    frame = pd.DataFrame(list(rows))
    extras = sorted(c for c in frame.columns if c not in columns)
    frame = frame.reindex(columns=list(columns) + extras)
    for column in DATETIME_COLUMNS.intersection(frame.columns):
        frame[column] = pd.to_datetime(frame[column]).astype("datetime64[ns]")
    return frame.reset_index(drop=True)


__all__ = [
    "BINARY_CANDLE_COLUMNS",
    "BINARY_CONTRACT_COLUMNS",
    "BINARY_QUOTE_COLUMNS",
    "BINARY_RESOLVED_COLUMNS",
    "BINARY_SNAPSHOT_COLUMNS",
    "BINARY_TRADE_COLUMNS",
    "CADENCES",
    "CONTRACT_STATUSES",
    "DATETIME_COLUMNS",
    "GREEKS_REFERENCES",
    "GREEKS_SOURCES",
    "IV_SOURCES",
    "MAPPING_SOURCES",
    "METHOD_SOURCES",
    "OPTION_CONTRACT_COLUMNS",
    "OPTION_MARK_CANDLE_COLUMNS",
    "OPTION_QUOTE_COLUMNS",
    "OPTION_RESOLVED_COLUMNS",
    "OPTION_SNAPSHOT_COLUMNS",
    "OPTION_TRADE_COLUMNS",
    "OPTION_TYPES",
    "QUOTE_SOURCES",
    "SETTLEMENT_COLUMNS",
    "UNDERLYING_KINDS",
    "USD_QUOTES",
    "binary_condition",
    "binary_contract_id",
    "canonical_quote",
    "format_expiry",
    "format_strike",
    "frame_of",
    "make_underlying_id",
    "option_contract_id",
    "outcome_from_bounds",
    "p_mid",
    "to_float",
    "utc_naive",
]
