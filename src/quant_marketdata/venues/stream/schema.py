"""Arrow schemas of the recorded streams, shared by every venue protocol.

Timestamps are naive UTC microseconds. Every row carries the canonical ``instrument_id`` from the registry;
the venue is given by the store path.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import pyarrow as pa

CHANNELS = ("order_book", "trades", "market_stats")
PARQUET_COMPRESSION = "zstd"

_TS = pa.timestamp("us")

ORDER_BOOK_SCHEMA = pa.schema([
    ("timestamp", _TS),       # venue timestamp of the message
    ("recv_time", _TS),
    ("instrument_id", pa.string()),
    ("kind", pa.string()),    # snapshot | checkpoint | delta
    ("offset", pa.int64()),
    ("begin_nonce", pa.int64()),
    ("nonce", pa.int64()),
    ("side", pa.string()),    # bid | ask; null for a message without levels
    ("price", pa.float64()),
    ("size", pa.float64()),   # 0 removes the level
])

TRADES_SCHEMA = pa.schema([
    ("timestamp", _TS),
    ("recv_time", _TS),
    ("instrument_id", pa.string()),
    ("trade_id", pa.int64()),
    ("type", pa.string()),    # trade | liquidation | deleverage | market-settlement
    ("price", pa.float64()),
    ("size", pa.float64()),
    ("usd_amount", pa.float64()),
    ("is_maker_ask", pa.bool_()),
    ("ask_id", pa.int64()),
    ("bid_id", pa.int64()),
    ("ask_account_id", pa.int64()),
    ("bid_account_id", pa.int64()),
    ("taker_fee", pa.int64()),
    ("maker_fee", pa.int64()),
    ("block_height", pa.int64()),
    ("transaction_time", pa.int64()),  # microseconds
    ("tx_hash", pa.string()),
])

STATS_FLOATS = (
    "mark_price",
    "index_price",
    "mid_price",
    "best_bid_price",
    "best_ask_price",
    "last_trade_price",
    "open_interest",
    "current_funding_rate",
    "funding_rate",
    "premium",
    "daily_base_token_volume",
    "daily_quote_token_volume",
    "daily_price_low",
    "daily_price_high",
    "daily_price_change",
)
MARKET_STATS_SCHEMA = pa.schema(
    [("timestamp", _TS), ("recv_time", _TS), ("instrument_id", pa.string())]
    + [(name, pa.float64()) for name in STATS_FLOATS]
    + [("funding_timestamp", _TS)]
)

SCHEMAS: Dict[str, pa.Schema] = {
    "order_book": ORDER_BOOK_SCHEMA,
    "trades": TRADES_SCHEMA,
    "market_stats": MARKET_STATS_SCHEMA,
}
# Compaction of the small datasets drops duplicates (replays after reconnections) and sorts by time.
# The order book keeps arrival order and is streamed as is.
DEDUPE_KEYS = {"trades": ["trade_id"], "market_stats": ["timestamp"]}


def day_of(micros: int) -> str:
    return datetime.fromtimestamp(micros / 1e6, tz=timezone.utc).strftime("%Y-%m-%d")


def now_us() -> int:
    return time.time_ns() // 1000


def to_float(value: Any) -> Optional[float]:
    return None if value is None or value == "" else float(value)


def to_int(value: Any) -> Optional[int]:
    return None if value is None or value == "" else int(value)
