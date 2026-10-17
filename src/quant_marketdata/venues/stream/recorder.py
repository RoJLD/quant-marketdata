"""Venue-agnostic websocket recorder: connections, (re)subscription, order book continuity, checkpoints,
trade de-duplication, buffering and writes to the stream store.

Order book integrity: a delta must chain onto the current book (``begin_nonce == book.nonce``); on a gap the
recorder resubscribes to get a fresh snapshot, and after a disconnection it reconnects with exponential backoff
and waits for the new snapshot; updates in between are not written. ``checkpoint`` rows (the full book, at the
start of every UTC day and every ``checkpoint_interval``) make each day file replayable on its own.

A small health file (``_health.json`` in the venue's stream directory) is rewritten at every flush: last event
per channel and market, resyncs, reconnections. The monitoring cycle can read it as a freshness source.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import pyarrow as pa

from quant_marketdata.venues.stream.book import OrderBook
from quant_marketdata.venues.stream.protocol import BookUpdate, StreamVenue
from quant_marketdata.venues.stream.schema import SCHEMAS, day_of, now_us
from quant_marketdata.venues.stream.store import StreamStore

logger = logging.getLogger(__name__)

# Trade ids remembered per market to drop the trades replayed on (re)subscription.
SEEN_TRADES_PER_MARKET = 5000


@dataclass(frozen=True)
class Market:
    symbol: str  # native symbol (store path)
    instrument_id: Optional[str] = None  # canonical id from the registry


class _Buffer:
    """Column lists for one (channel, symbol, day) partition."""

    def __init__(self, schema: pa.Schema) -> None:
        self.schema = schema
        self.columns: Dict[str, List[Any]] = {name: [] for name in schema.names}
        self.rows = 0

    def append(self, row: Mapping[str, Any]) -> None:
        for name, values in self.columns.items():
            values.append(row.get(name))
        self.rows += 1

    def to_table(self) -> pa.Table:
        return pa.table([pa.array(self.columns[f.name], type=f.type) for f in self.schema], schema=self.schema)


@dataclass
class _BookState:
    book: OrderBook = field(default_factory=OrderBook)
    synced: bool = False
    last_day: Optional[str] = None
    last_checkpoint: int = 0  # microseconds


@dataclass
class RecorderStats:
    messages: int = 0
    rows: Dict[str, int] = field(default_factory=lambda: defaultdict(int))
    resyncs: int = 0
    reconnections: int = 0
    last_event: Dict[str, int] = field(default_factory=dict)  # "<channel>/<symbol>" -> recv time (us)


class Recorder:
    """Records ``channels`` of ``markets`` (``market_id -> Market``) from ``venue`` into ``store``."""

    def __init__(
        self,
        venue: StreamVenue,
        store: StreamStore,
        markets: Mapping[int, Market],
        channels: Sequence[str] = ("order_book", "trades", "market_stats"),
        *,
        flush_interval: float = 10.0,
        max_buffer_rows: int = 200_000,
        checkpoint_interval: float = 3600.0,
        stale_timeout: float = 30.0,
        max_backoff: float = 60.0,
        connect: Any = None,
    ) -> None:
        unknown = [c for c in channels if c not in venue.channels]
        if unknown:
            raise ValueError(f"{venue.name} cannot record: {', '.join(unknown)}")
        if not markets:
            raise ValueError("No market to record")
        self.venue = venue
        self.store = store
        self.markets = dict(markets)
        self.channels = list(channels)
        self.flush_interval = flush_interval
        self.max_buffer_rows = max_buffer_rows
        self.checkpoint_interval_us = int(checkpoint_interval * 1e6)
        self.stale_timeout = stale_timeout
        self.max_backoff = max_backoff
        self._connect = connect
        self.books: Dict[int, _BookState] = {m: _BookState() for m in self.markets}
        self.stats = RecorderStats()
        self._buffers: Dict[Tuple[str, str, str], _Buffer] = {}
        self._buffered_rows = 0
        self._seen_trades: Dict[int, deque] = {m: deque(maxlen=SEEN_TRADES_PER_MARKET) for m in self.markets}
        self._seen_trade_ids: Dict[int, set] = {m: set() for m in self.markets}
        self._flush_now = asyncio.Event()
        self._inflight: Optional[asyncio.Future] = None

    # -- subscriptions

    def subscriptions(self) -> List[str]:
        return [self.venue.subscription(c, m) for m in sorted(self.markets) for c in self.channels]

    # -- message handling (no I/O, unit tested)

    def handle_message(self, message: Mapping[str, Any], recv_time: Optional[int] = None) -> List[str]:
        """Buffer the rows of one decoded data message. Returns the subscriptions to renew (book gaps)."""
        recv_time = recv_time if recv_time is not None else now_us()
        routed = self.venue.route(message)
        if routed is None:
            return []
        channel, market_id = routed
        if channel not in self.channels or market_id not in self.markets:
            return []
        self.stats.messages += 1
        self.stats.last_event[f"{channel}/{self.markets[market_id].symbol}"] = recv_time
        if channel == "order_book":
            return self._on_order_book(market_id, self.venue.book_update(message), recv_time)
        if channel == "trades":
            self._on_trades(market_id, self.venue.trade_rows(message), recv_time)
        elif channel == "market_stats":
            row = self.venue.stats_row(message)
            timestamp = row.get("timestamp") or recv_time
            self._add("market_stats", market_id, timestamp, {**row, "timestamp": timestamp, "recv_time": recv_time})
        return []

    def _buffer(self, channel: str, market_id: int, day: str) -> _Buffer:
        key = (channel, self.markets[market_id].symbol, day)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = self._buffers[key] = _Buffer(SCHEMAS[channel])
        return buffer

    def _add(self, channel: str, market_id: int, timestamp: int, row: Dict[str, Any]) -> None:
        row = {**row, "instrument_id": self.markets[market_id].instrument_id}
        self._buffer(channel, market_id, day_of(timestamp)).append(row)
        self.stats.rows[channel] += 1
        self._buffered_rows += 1
        if self._buffered_rows >= self.max_buffer_rows:
            self._flush_now.set()

    def _write_book_rows(self, market_id: int, kind: str, timestamp: int, recv_time: int,
                         offset: Optional[int], begin_nonce: Optional[int], nonce: int,
                         asks: Sequence[Tuple[float, float]], bids: Sequence[Tuple[float, float]]) -> None:
        base = {"timestamp": timestamp, "recv_time": recv_time, "kind": kind, "offset": offset,
                "begin_nonce": begin_nonce, "nonce": nonce}
        levels = [("ask", p, s) for p, s in asks] + [("bid", p, s) for p, s in bids]
        if not levels:  # keep the nonce chain visible in the file
            levels = [(None, None, None)]
        for side, price, size in levels:
            self._add("order_book", market_id, timestamp, {**base, "side": side, "price": price, "size": size})

    def _on_order_book(self, market_id: int, update: BookUpdate, recv_time: int) -> List[str]:
        state = self.books[market_id]
        symbol = self.markets[market_id].symbol
        subscription = self.venue.subscription("order_book", market_id)
        timestamp = update.timestamp if update.timestamp is not None else recv_time
        if update.nonce is None:
            logger.warning("%s: order book message without nonce, resyncing", symbol)
            state.synced = False
            return [subscription]
        if update.kind == "snapshot":
            state.book.reset(update.asks, update.bids, update.nonce)
            state.synced = True
            state.last_day = day_of(timestamp)
            state.last_checkpoint = timestamp
            self._write_book_rows(market_id, "snapshot", timestamp, recv_time, update.offset, update.begin_nonce,
                                  update.nonce, state.book.levels("ask"), state.book.levels("bid"))
            return []
        if not state.synced:
            return []  # waiting for the snapshot of a resubscription
        if update.begin_nonce != state.book.nonce:
            logger.warning("%s: order book gap (begin_nonce %s, expected %s), resyncing",
                           symbol, update.begin_nonce, state.book.nonce)
            state.synced = False
            self.stats.resyncs += 1
            return [subscription]
        day = day_of(timestamp)
        if day != state.last_day or timestamp - state.last_checkpoint >= self.checkpoint_interval_us:
            # Full book at the start of each day file, and periodically for fast seeks.
            self._write_book_rows(market_id, "checkpoint", timestamp, recv_time, None, 0, state.book.nonce,
                                  state.book.levels("ask"), state.book.levels("bid"))
            state.last_day = day
            state.last_checkpoint = timestamp
        state.book.apply(update.asks, update.bids, update.nonce)
        state.book.timestamp = timestamp
        self._write_book_rows(market_id, "delta", timestamp, recv_time, update.offset, update.begin_nonce,
                              update.nonce, update.asks, update.bids)
        return []

    def _on_trades(self, market_id: int, rows: Iterable[Dict[str, Any]], recv_time: int) -> None:
        seen, order = self._seen_trade_ids[market_id], self._seen_trades[market_id]
        for row in rows:
            trade_id = row["trade_id"]
            if trade_id in seen:
                continue
            if len(order) == order.maxlen:
                seen.discard(order[0])
            order.append(trade_id)
            seen.add(trade_id)
            self._add("trades", market_id, row["timestamp"], {**row, "recv_time": recv_time})

    def mark_disconnected(self, market_ids: Iterable[int]) -> None:
        """Deltas are not trusted again until a fresh snapshot arrives."""
        for market_id in market_ids:
            self.books[market_id].synced = False

    # -- persistence

    def take_buffers(self) -> List[Tuple[Tuple[str, str, str], pa.Table]]:
        buffers, self._buffers, self._buffered_rows = self._buffers, {}, 0
        self._flush_now.clear()
        return [(key, buffer.to_table()) for key, buffer in buffers.items() if buffer.rows]

    def _write(self, tables: List[Tuple[Tuple[str, str, str], pa.Table]], compact_days_before: Optional[str]) -> None:
        for (channel, symbol, day), table in tables:
            self.store.write_part(channel, symbol, day, table)
        for channel, symbol, day in self.store.pending():
            if compact_days_before is None or day < compact_days_before:
                rows = self.store.compact(channel, symbol, day)
                logger.info("Compacted %s %s %s (%d rows)", channel, symbol, day, rows)
        self._write_health()

    def _write_health(self) -> None:
        health = {
            "venue": self.venue.name,
            "written_at_us": now_us(),
            "messages": self.stats.messages,
            "rows": dict(self.stats.rows),
            "resyncs": self.stats.resyncs,
            "reconnections": self.stats.reconnections,
            "last_event_us": dict(self.stats.last_event),
        }
        self.store.root.mkdir(parents=True, exist_ok=True)
        tmp = self.store.root / "_health.json.tmp"
        tmp.write_text(json.dumps(health, indent=1), encoding="utf-8")
        tmp.replace(self.store.root / "_health.json")

    async def flush(self, *, final: bool = False) -> None:
        """Write buffered rows to part files; compact finished days (every day if ``final``)."""
        if self._inflight is not None:
            await asyncio.shield(self._inflight)
        tables = self.take_buffers()
        today = None if final else day_of(now_us())
        # Shielded: a cancelled flush loop must not leave a half-written flush behind.
        self._inflight = asyncio.ensure_future(asyncio.to_thread(self._write, tables, today))
        await asyncio.shield(self._inflight)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._flush_now.wait(), self.flush_interval)
            except asyncio.TimeoutError:
                pass
            await self.flush()

    # -- network

    def _open(self):
        if self._connect is not None:
            return self._connect(self.venue.url)
        import websockets  # the `stream` extra

        return websockets.connect(self.venue.url, max_size=None, open_timeout=15, ping_interval=20,
                                  ping_timeout=20)

    async def _connection_loop(self, subscriptions: Sequence[str], market_ids: Sequence[int]) -> None:
        backoff = min(1.0, self.max_backoff)
        while True:
            started = time.monotonic()
            try:
                async with self._open() as ws:
                    logger.info("Connected to %s (%d subscriptions)", self.venue.url, len(subscriptions))
                    await self._consume(ws, subscriptions)
            except (OSError, asyncio.TimeoutError) as exc:
                logger.warning("Websocket closed: %s: %s", type(exc).__name__, exc)
            except Exception as exc:  # websockets' own exceptions, without importing them eagerly
                if type(exc).__module__.split(".")[0] != "websockets":
                    raise
                logger.warning("Websocket closed: %s: %s", type(exc).__name__, exc)
            self.mark_disconnected(market_ids)
            self.stats.reconnections += 1
            if time.monotonic() - started > 60:
                backoff = min(1.0, self.max_backoff)
            logger.info("Reconnecting in %.0fs", backoff)
            await asyncio.sleep(backoff)
            backoff = min(self.max_backoff, backoff * 2)

    async def _consume(self, ws: Any, subscriptions: Sequence[str]) -> None:
        subscribed = False
        while True:
            # Also catches a silent stream: the venues send updates continuously.
            raw = await asyncio.wait_for(ws.recv(), self.stale_timeout)
            recv_time = now_us()
            message = json.loads(raw)
            control = self.venue.control(message)
            if control is not None:
                if control.kind == "reply" and control.reply is not None:
                    await ws.send(json.dumps(control.reply))
                elif control.kind == "connected" and not subscribed:
                    for sub in subscriptions:
                        await ws.send(json.dumps(self.venue.subscribe_message(sub)))
                    subscribed = True
                elif control.kind == "error":
                    logger.warning("Server error: %s", control.detail)
                else:
                    logger.info("Server notice: %s", control.detail)
                continue
            for sub in self.handle_message(message, recv_time):
                await ws.send(json.dumps(self.venue.unsubscribe_message(sub)))
                await ws.send(json.dumps(self.venue.subscribe_message(sub)))

    async def run(self, duration: Optional[float] = None) -> RecorderStats:
        """Record until cancelled (Ctrl+C) or for ``duration`` seconds, then flush and compact."""
        per_conn = self.venue.max_subscriptions
        markets = sorted(self.markets)
        # Keep each market's channels on one connection so a disconnection resyncs whole markets.
        per_market = max(1, per_conn // max(1, len(self.channels)))
        groups = [markets[i:i + per_market] for i in range(0, len(markets), per_market)]
        # Leftovers of a previous run that stopped without compacting.
        await asyncio.to_thread(self._write, [], day_of(now_us()))
        tasks = [
            asyncio.create_task(self._connection_loop(
                [self.venue.subscription(c, m) for m in group for c in self.channels], group))
            for group in groups
        ]
        tasks.append(asyncio.create_task(self._flush_loop()))
        try:
            done, _ = await asyncio.wait(tasks, timeout=duration, return_when=asyncio.FIRST_EXCEPTION)
            for task in done:
                task.result()  # surface a crash
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self._inflight is not None:
                await asyncio.gather(self._inflight, return_exceptions=True)
                self._inflight = None
            await self.flush(final=True)
        return self.stats


__all__ = ["Market", "Recorder", "RecorderStats", "SEEN_TRADES_PER_MARKET"]
