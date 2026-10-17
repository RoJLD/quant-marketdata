"""Continuous websocket recording of market microstructure (order book, trades, market stats).

REST APIs keep no history of the order book, intraday open interest or mark price, so these streams are
recorded live into the quant-marketdata store. Needs the ``stream`` extra (``websockets``, ``pyarrow``); the
core package does not import this sub-package.

    from quant_marketdata.venues.stream import read_stream, book_at
    trades = read_stream("lighter", "trades", "BTC", start="2026-10-17")
    liquidations = trades[trades["type"] == "liquidation"]
    book = book_at(read_stream("lighter", "order_book", "BTC", start="2026-10-17"))

Spec: Quant-lab ``docs/specs/2026-10-17-stream-recorder-design.md``. Ported from the Quant_Lighter prototype.
"""

from quant_marketdata.venues.stream.book import (
    BookComparison,
    OrderBook,
    book_at,
    book_from_orders,
    compare_books,
    iter_books,
)
from quant_marketdata.venues.stream.lighter import LighterStream
from quant_marketdata.venues.stream.protocol import BookUpdate, Control, StreamVenue
from quant_marketdata.venues.stream.recorder import Market, Recorder, RecorderStats
from quant_marketdata.venues.stream.schema import CHANNELS, SCHEMAS
from quant_marketdata.venues.stream.store import StreamStore, read_stream

__all__ = [
    "CHANNELS",
    "SCHEMAS",
    "BookComparison",
    "BookUpdate",
    "Control",
    "LighterStream",
    "Market",
    "OrderBook",
    "Recorder",
    "RecorderStats",
    "StreamStore",
    "StreamVenue",
    "book_at",
    "book_from_orders",
    "compare_books",
    "iter_books",
    "read_stream",
]
