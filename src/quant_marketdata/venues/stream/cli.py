"""Command line of the stream recorder.

    qmd-record run   --venue lighter --symbols BTC ETH [--channels order_book trades] [--duration 300]
    qmd-record check --venue lighter --symbols BTC ETH     # live book vs REST snapshots, files vs live

Markets come from the venue's REST adapter and the registry (canonical ``instrument_id`` on every row), not
from the venue SDK.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import tempfile
import time
from collections import deque
from typing import Dict, List, Optional, Sequence, Tuple

from quant_marketdata.venues.base import get_venue
from quant_marketdata.venues.registry import resolve
from quant_marketdata.venues.stream.book import book_at, book_from_orders, compare_books
from quant_marketdata.venues.stream.lighter import LighterStream
from quant_marketdata.venues.stream.recorder import Market, Recorder
from quant_marketdata.venues.stream.schema import CHANNELS
from quant_marketdata.venues.stream.store import StreamStore, read_stream

STREAMS = {"lighter": LighterStream}


def resolve_markets(venue_name: str, symbols: Optional[Sequence[str]] = None) -> Dict[int, Market]:
    """``market_id -> Market`` for the requested native symbols (default: every active instrument)."""
    rest = get_venue(venue_name)
    instruments = rest.instruments()
    build = resolve(venue_name, instruments)
    canonical = dict(zip(build.instruments["venue_symbol"], build.instruments["instrument_id"], strict=True))
    if symbols:
        unknown = sorted(set(symbols) - set(instruments["venue_symbol"]))
        if unknown:
            raise ValueError(f"Unknown {venue_name} symbols: {', '.join(unknown)}")
        selected = instruments[instruments["venue_symbol"].isin(symbols)]
    else:
        selected = instruments[instruments["status"] == "active"]
    return {
        int(row["venue_market_id"]): Market(row["venue_symbol"], canonical.get(row["venue_symbol"]))
        for row in selected.to_dict("records")
    }


def rest_book(venue_name: str, market_id: int, limit: int = 250) -> Tuple[List, List]:
    """A REST order book snapshot as ``(asks, bids)`` lists of ``(price, size)`` orders."""
    if venue_name != "lighter":
        raise NotImplementedError(f"No REST book snapshot for {venue_name} yet (Dataset.BOOK)")
    rest = get_venue(venue_name)
    payload = rest.client.get("orderBookOrders", {"market_id": market_id, "limit": limit})

    def orders(side):
        return [(o["price"], o["remaining_base_amount"]) for o in payload.get(side) or []]

    return orders("asks"), orders("bids")


def _run(args) -> int:
    markets = resolve_markets(args.venue, args.symbols)
    stream = STREAMS[args.venue]()
    store = StreamStore.at(args.venue, args.root)
    recorder = Recorder(stream, store, markets, args.channels, flush_interval=args.flush_interval,
                        checkpoint_interval=args.checkpoint_minutes * 60)
    logging.info("Recording %s for %d markets into %s", ", ".join(args.channels), len(markets), store.root)
    try:
        stats = asyncio.run(recorder.run(args.duration))
    except KeyboardInterrupt:
        return 0
    print(f"messages={stats.messages} rows={dict(stats.rows)} resyncs={stats.resyncs} "
          f"reconnections={stats.reconnections}")
    return 0


class _HistoryRecorder(Recorder):
    """Keeps a copy of each market's book after every message, for a few seconds."""

    def __init__(self, *args, history_seconds: float = 10.0, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.history_seconds = history_seconds
        self.history = {m: deque() for m in self.markets}

    def _on_order_book(self, market_id, update, recv_time):
        resync = super()._on_order_book(market_id, update, recv_time)
        if self.books[market_id].synced:
            history, now = self.history[market_id], time.monotonic()
            history.append((now, self.books[market_id].book.copy()))
            while history and now - history[0][0] > self.history_seconds:
                history.popleft()
        return resync


def _ratio(results) -> float:
    return sum(r.matching for r in results) / max(1, sum(r.compared for r in results))


async def _check_async(args, root) -> bool:
    """Record the order book for a while and, every few seconds, match a REST snapshot against the live
    states of a few seconds around the request (the REST snapshot is not aligned with the stream): at least
    one snapshot per market must match a live state exactly. Then the book replayed from the files must
    equal the live book exactly."""
    markets = resolve_markets(args.venue, args.symbols)
    recorder = _HistoryRecorder(STREAMS[args.venue](), StreamStore.at(args.venue, root), markets,
                                ["order_book"], flush_interval=5.0)
    task = asyncio.create_task(recorder.run(args.seconds))
    ratios = {m: [] for m in markets}
    await asyncio.sleep(min(args.every, args.seconds / 2))
    while not task.done():
        for market_id, market in markets.items():
            if not recorder.books[market_id].synced:
                continue
            asks, bids = await asyncio.to_thread(rest_book, args.venue, market_id)
            reference = book_from_orders(asks, bids)
            await asyncio.sleep(2)  # let the stream catch up with (or pass) the snapshot
            states = [book for _, book in recorder.history[market_id]]
            if not states:
                continue
            best = max((compare_books(book, reference, args.depth) for book in states), key=_ratio)
            ratios[market_id].append(_ratio(best))
            logging.info("%s vs REST: %s (%.0f%%)", market.symbol,
                         ", ".join(f"{r.side}s {r.matching}/{r.compared}" for r in best), 100 * _ratio(best))
        await asyncio.wait([task], timeout=args.every)
    await task
    ok = True
    for market_id, market in markets.items():
        live = recorder.books[market_id].book
        replayed = book_at(read_stream(args.venue, "order_book", market.symbol, root=root))
        exact = (replayed is not None and replayed.bids == live.bids and replayed.asks == live.asks
                 and replayed.nonce == live.nonce)
        best_ratio = max(ratios[market_id], default=0.0)
        print(f"{market.symbol}: replayed files == live book: {exact} ({len(live.bids)} bids, "
              f"{len(live.asks)} asks, nonce {live.nonce}); best REST match {best_ratio:.0%} "
              f"over {len(ratios[market_id])} snapshots")
        ok &= exact and best_ratio == 1.0
    return ok


def _check(args) -> int:
    if args.root:
        ok = asyncio.run(_check_async(args, args.root))
    else:
        with tempfile.TemporaryDirectory() as tmp:
            ok = asyncio.run(_check_async(args, tmp))
    print("OK" if ok else "MISMATCH")
    return 0 if ok else 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Record venue websocket streams into the quant-marketdata store.")
    parser.add_argument("--root", help="Store root (default: $QUANT_MARKETDATA_ROOT or ./data/marketdata).")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="Record until Ctrl+C or --duration.")
    run.add_argument("--venue", choices=sorted(STREAMS), default="lighter")
    run.add_argument("--symbols", nargs="+", help="Native symbols (default: every active instrument).")
    run.add_argument("--channels", nargs="+", choices=CHANNELS, default=list(CHANNELS))
    run.add_argument("--duration", type=float, help="Seconds to record (default: until Ctrl+C).")
    run.add_argument("--flush-interval", type=float, default=10.0)
    run.add_argument("--checkpoint-minutes", type=float, default=60.0)
    check = sub.add_parser("check", help="Record briefly and verify the book against REST and the files.")
    check.add_argument("--venue", choices=sorted(STREAMS), default="lighter")
    check.add_argument("--symbols", nargs="+", default=["BTC", "ETH"])
    check.add_argument("--seconds", type=float, default=120.0, help="Recording length (default: 120).")
    check.add_argument("--every", type=float, default=5.0, help="Seconds between REST checks (default: 5).")
    check.add_argument("--depth", type=int, default=20, help="Levels compared per side (default: 20).")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    return _run(args) if args.cmd == "run" else _check(args)


if __name__ == "__main__":
    sys.exit(main())
