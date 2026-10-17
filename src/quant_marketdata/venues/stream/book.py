"""Order books rebuilt from recorded snapshots and deltas; replay and comparison with a REST snapshot."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, Iterable, Iterator, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

Level = Tuple[float, float]


class OrderBook:
    """Price -> size per side, rebuilt from snapshots and deltas."""

    def __init__(self) -> None:
        self.bids: Dict[float, float] = {}
        self.asks: Dict[float, float] = {}
        self.nonce: Optional[int] = None
        self.timestamp: Optional[int] = None  # microseconds

    def reset(self, asks: Iterable[Level], bids: Iterable[Level], nonce: int) -> None:
        self.asks = {p: s for p, s in asks if s > 0}
        self.bids = {p: s for p, s in bids if s > 0}
        self.nonce = nonce

    def apply(self, asks: Iterable[Level], bids: Iterable[Level], nonce: int) -> None:
        for levels, book in ((asks, self.asks), (bids, self.bids)):
            for price, size in levels:
                if size > 0:
                    book[price] = size
                else:
                    book.pop(price, None)
        self.nonce = nonce

    def levels(self, side: str, depth: Optional[int] = None) -> List[Level]:
        """Levels from the best price outwards."""
        book = self.bids if side == "bid" else self.asks
        return sorted(book.items(), reverse=side == "bid")[:depth]

    def best_bid(self) -> Optional[float]:
        return max(self.bids) if self.bids else None

    def best_ask(self) -> Optional[float]:
        return min(self.asks) if self.asks else None

    def copy(self) -> "OrderBook":
        clone = OrderBook()
        clone.bids, clone.asks = dict(self.bids), dict(self.asks)
        clone.nonce, clone.timestamp = self.nonce, self.timestamp
        return clone


def iter_books(frame: pd.DataFrame) -> Iterator[OrderBook]:
    """Replay recorded order book rows; yields the book after each message.

    The same ``OrderBook`` object is yielded each time (``copy()`` it to keep a state). Deltas that do not
    chain onto the current book (``begin_nonce`` != book ``nonce``) are skipped with a warning until the next
    snapshot or checkpoint, so a yielded book is always consistent.
    """
    if frame.empty:
        return
    book = OrderBook()
    synced = False
    kinds = frame["kind"].to_numpy()
    begins = frame["begin_nonce"].fillna(-1).to_numpy()
    nonces = frame["nonce"].to_numpy()
    sides = frame["side"].to_numpy()
    prices = frame["price"].to_numpy()
    sizes = frame["size"].to_numpy()
    stamps = frame["timestamp"].to_numpy(dtype="datetime64[us]").astype("int64")

    start, n = 0, len(frame)
    while start < n:
        end = start + 1
        while (end < n and nonces[end] == nonces[start] and kinds[end] == kinds[start]
               and begins[end] == begins[start]):
            end += 1
        asks, bids = [], []
        for i in range(start, end):
            if sides[i] == "ask":
                asks.append((prices[i], sizes[i]))
            elif sides[i] == "bid":
                bids.append((prices[i], sizes[i]))
        nonce = int(nonces[start])
        if kinds[start] in ("snapshot", "checkpoint"):
            book.reset(asks, bids, nonce)
            synced = True
        elif synced and int(begins[start]) == book.nonce:
            book.apply(asks, bids, nonce)
        else:
            if synced:
                logger.warning("Order book gap: begin_nonce %s after nonce %s", begins[start], book.nonce)
            synced = False
        if synced:
            book.timestamp = int(stamps[start])
            yield book
        start = end


def book_at(frame: pd.DataFrame, when: Optional[pd.Timestamp] = None) -> Optional[OrderBook]:
    """Book after the last message at or before ``when`` (default: the last one)."""
    if when is not None:
        frame = frame[frame["timestamp"] <= when]
    last = None
    for book in iter_books(frame):
        last = book
    return last.copy() if last is not None else None


def book_from_orders(asks: Iterable[Level], bids: Iterable[Level]) -> OrderBook:
    """Aggregate individual orders ``(price, size)`` (e.g. a REST snapshot) into price levels."""
    book = OrderBook()
    for orders, side in ((asks, book.asks), (bids, book.bids)):
        for price, size in orders:
            side[float(price)] = side.get(float(price), 0.0) + float(size)
    return book


@dataclass
class BookComparison:
    side: str
    compared: int
    matching: int
    mismatches: List[Tuple[float, Optional[float], Optional[float]]]

    @property
    def ratio(self) -> float:
        return self.matching / self.compared if self.compared else 1.0


def compare_books(recorded: OrderBook, reference: OrderBook, depth: int = 20) -> List[BookComparison]:
    """Compare the ``depth`` best levels of each side of ``reference`` with ``recorded``.

    A REST snapshot lists individual orders up to a cap, so its deepest level may be partial: it is left out,
    as is any recorded level beyond it.
    """
    results = []
    for side in ("ask", "bid"):
        ref_levels = reference.levels(side)
        if len(ref_levels) > 1:
            ref_levels = ref_levels[:-1]
        ref_levels = ref_levels[:depth]
        if not ref_levels:
            results.append(BookComparison(side, 0, 0, []))
            continue
        worst = ref_levels[-1][0]
        inside = (lambda p, w=worst: p <= w) if side == "ask" else (lambda p, w=worst: p >= w)
        ref = dict(ref_levels)
        rec = {p: s for p, s in recorded.levels(side) if inside(p)}
        prices = sorted(set(ref) | set(rec), reverse=side == "bid")
        mismatches = [
            (p, rec.get(p), ref.get(p))
            for p in prices
            if rec.get(p) is None or ref.get(p) is None or abs(rec[p] - ref[p]) > 1e-9
        ]
        results.append(BookComparison(side, len(prices), len(prices) - len(mismatches), mismatches))
    return results


__all__ = ["BookComparison", "OrderBook", "book_at", "book_from_orders", "compare_books", "iter_books"]
