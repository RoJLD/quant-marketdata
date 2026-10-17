"""Stream recorder (venues.stream): ported from the Quant_Lighter prototype tests, on the venue-agnostic
recorder with the Lighter protocol. No network: messages are built in the Lighter wire format."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import pandas as pd
import pyarrow as pa
import pytest

from quant_marketdata.venues.stream import (
    LighterStream,
    Market,
    OrderBook,
    Recorder,
    StreamStore,
    book_at,
    book_from_orders,
    compare_books,
    iter_books,
    read_stream,
)
from quant_marketdata.venues.stream.schema import TRADES_SCHEMA

DAY1_US = 1_791_936_000_000_000  # 2026-10-14 00:00:00 UTC
DAY2_US = DAY1_US + 86_400_000_000
MARKETS = {1: Market("BTC", "BTC-USD-PERP"), 2: Market("ETH", "ETH-USD-PERP")}


def _levels(*pairs):
    return [{"price": str(p), "size": str(s)} for p, s in pairs]


def _book_msg(kind, nonce, begin, asks=(), bids=(), ts=DAY1_US + 1_000_000, market=1):
    return {
        "type": f"{kind}/order_book",
        "channel": f"order_book:{market}",
        "offset": nonce,
        "last_updated_at": ts,
        "order_book": {"code": 0, "asks": _levels(*asks), "bids": _levels(*bids), "nonce": nonce,
                       "begin_nonce": begin},
    }


def _trade(trade_id, ts_ms, kind="trade", price="100.5", size="0.1"):
    return {"trade_id": trade_id, "type": kind, "market_id": 1, "price": price, "size": size,
            "usd_amount": "10.05", "is_maker_ask": True, "ask_id": 11, "bid_id": 12, "ask_account_id": 21,
            "bid_account_id": 22, "maker_fee": 28, "block_height": 5, "timestamp": ts_ms,
            "transaction_time": ts_ms * 1000, "tx_hash": "ab"}


def _recorder(tmp_path, channels=("order_book", "trades", "market_stats"), markets=MARKETS, **kwargs):
    return Recorder(LighterStream(), StreamStore.at("lighter", tmp_path), markets, channels, **kwargs)


def _read(tmp_path, channel, symbol, start=None, end=None):
    return read_stream("lighter", channel, symbol, start, end, root=tmp_path)


def _flush(recorder):
    asyncio.run(recorder.flush(final=True))


# -- order book ------------------------------------------------------------------------------------


def test_snapshot_and_deltas_rebuild_the_book_and_replay_from_disk(tmp_path):
    rec = _recorder(tmp_path)
    assert rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1), (102, 2)], bids=[(99, 3), (98, 4)])) == []
    rec.handle_message(_book_msg("update", 11, 10, asks=[(101, 0), (103, 5)], bids=[(99, 1.5)]))
    rec.handle_message(_book_msg("update", 12, 11))  # no level changed
    rec.handle_message(_book_msg("update", 13, 12, bids=[(100, 0.5)]))
    book = rec.books[1].book
    assert book.levels("ask") == [(102.0, 2.0), (103.0, 5.0)]
    assert book.levels("bid") == [(100.0, 0.5), (99.0, 1.5), (98.0, 4.0)]
    assert book.nonce == 13

    _flush(rec)
    frame = _read(tmp_path, "order_book", "BTC")
    assert (tmp_path / "venues/lighter/stream/order_book/BTC/2026-10-14.parquet").exists()
    assert frame["kind"].tolist()[:4] == ["snapshot"] * 4
    assert set(frame["instrument_id"]) == {"BTC-USD-PERP"}
    # The empty update keeps a placeholder row, so the nonce chain stays visible.
    assert frame["nonce"].unique().tolist() == [10, 11, 12, 13]
    replayed = book_at(frame)
    assert replayed.asks == book.asks and replayed.bids == book.bids and replayed.nonce == 13
    assert [(b.nonce, b.best_bid()) for b in iter_books(frame)] == [(10, 99.0), (11, 99.0), (12, 99.0), (13, 100.0)]
    assert book_at(frame, pd.Timestamp(0)) is None


def test_gap_triggers_resync_and_drops_deltas_until_snapshot(tmp_path):
    rec = _recorder(tmp_path)
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)], bids=[(99, 1)]))
    assert rec.handle_message(_book_msg("update", 15, 14, asks=[(101, 9)])) == ["order_book/1"]
    assert rec.stats.resyncs == 1
    assert rec.handle_message(_book_msg("update", 16, 15, asks=[(101, 7)])) == []  # in flight, ignored
    rec.handle_message(_book_msg("subscribed", 20, 0, asks=[(105, 1)], bids=[(95, 1)]))
    rec.handle_message(_book_msg("update", 21, 20, bids=[(96, 2)]))
    _flush(rec)
    frame = _read(tmp_path, "order_book", "BTC")
    assert 15 not in frame["nonce"].tolist() and 16 not in frame["nonce"].tolist()
    book = book_at(frame)
    assert book.asks == {105.0: 1.0} and book.bids == {95.0: 1.0, 96.0: 2.0}


def test_disconnection_waits_for_a_fresh_snapshot(tmp_path):
    rec = _recorder(tmp_path)
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)]))
    rec.mark_disconnected([1])
    rec.handle_message(_book_msg("update", 11, 10, asks=[(101, 2)]))
    assert rec.books[1].book.asks == {101.0: 1.0}


def test_replay_skips_deltas_after_a_gap_in_the_file():
    rows = [
        ("snapshot", 0, 10, "ask", 101.0, 1.0),
        ("delta", 10, 11, "ask", 101.0, 2.0),
        ("delta", 99, 100, "ask", 101.0, 3.0),  # gap
        ("delta", 100, 101, "ask", 101.0, 4.0),
        ("checkpoint", 0, 200, "ask", 105.0, 1.0),
        ("delta", 200, 201, "bid", 99.0, 1.0),
    ]
    frame = pd.DataFrame(rows, columns=["kind", "begin_nonce", "nonce", "side", "price", "size"])
    frame["timestamp"] = pd.to_datetime(range(len(rows)), unit="s")
    assert [b.nonce for b in iter_books(frame)] == [10, 11, 200, 201]
    assert book_at(frame).asks == {105.0: 1.0}


def test_day_rollover_writes_a_checkpoint_so_each_day_replays_alone(tmp_path):
    rec = _recorder(tmp_path)
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)], bids=[(99, 1)], ts=DAY2_US - 2_000_000))
    rec.handle_message(_book_msg("update", 11, 10, asks=[(102, 1)], ts=DAY2_US - 1_000_000))
    rec.handle_message(_book_msg("update", 12, 11, bids=[(99, 0), (98, 3)], ts=DAY2_US + 1_000_000))
    _flush(rec)
    day2 = _read(tmp_path, "order_book", "BTC", start="2026-10-15")
    assert day2["kind"].iloc[0] == "checkpoint"
    book = book_at(day2)
    assert book.asks == {101.0: 1.0, 102.0: 1.0} and book.bids == {98.0: 3.0}
    assert set(_read(tmp_path, "order_book", "BTC", end="2026-10-14")["nonce"]) == {10, 11}


def test_periodic_checkpoints(tmp_path):
    rec = _recorder(tmp_path, checkpoint_interval=60)
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)], ts=DAY1_US))
    rec.handle_message(_book_msg("update", 11, 10, asks=[(102, 1)], ts=DAY1_US + 30_000_000))
    rec.handle_message(_book_msg("update", 12, 11, asks=[(103, 1)], ts=DAY1_US + 61_000_000))
    _flush(rec)
    frame = _read(tmp_path, "order_book", "BTC")
    checkpoints = frame[frame["kind"] == "checkpoint"]
    assert checkpoints["nonce"].unique().tolist() == [11]
    assert sorted(checkpoints["price"]) == [101.0, 102.0]


# -- trades and stats ------------------------------------------------------------------------------


def test_trades_keep_type_and_drop_replayed_duplicates(tmp_path):
    rec = _recorder(tmp_path)
    t0 = DAY1_US // 1000 + 5_000
    liquidation = _trade(3, t0 + 2, kind="liquidation")
    rec.handle_message({"type": "subscribed/trade", "channel": "trade:1", "nonce": 1,
                        "trades": [_trade(2, t0 + 1), _trade(1, t0), liquidation],
                        "liquidation_trades": [liquidation]})
    rec.handle_message({"type": "update/trade", "channel": "trade:1", "nonce": 2,
                        "trades": [_trade(4, t0 + 3, kind="deleverage"), _trade(2, t0 + 1)],
                        "liquidation_trades": []})
    _flush(rec)
    frame = _read(tmp_path, "trades", "BTC")
    assert frame["trade_id"].tolist() == [1, 2, 3, 4]
    assert frame["type"].tolist() == ["trade", "trade", "liquidation", "deleverage"]
    assert frame["price"].iloc[0] == 100.5
    assert frame["timestamp"].iloc[0] == pd.Timestamp(t0, unit="ms")
    assert pd.isna(frame["taker_fee"].iloc[0]) and frame["maker_fee"].iloc[0] == 28
    assert set(frame["instrument_id"]) == {"BTC-USD-PERP"}


def test_market_stats_rows(tmp_path):
    rec = _recorder(tmp_path)
    stats = {"symbol": "ETH", "market_id": 2, "index_price": "2350.1", "mark_price": "2349.9",
             "open_interest": "1234.5", "funding_rate": "0.0012", "funding_timestamp": 1792069200001,
             "daily_price_low": 2300}
    rec.handle_message({"type": "update/market_stats", "channel": "market_stats:2",
                        "market_stats": stats, "timestamp": DAY1_US // 1000 + 1})
    _flush(rec)
    row = _read(tmp_path, "market_stats", "ETH").iloc[0]
    assert (row["mark_price"], row["index_price"], row["open_interest"]) == (2349.9, 2350.1, 1234.5)
    assert row["daily_price_low"] == 2300.0 and pd.isna(row["premium"])
    assert row["funding_timestamp"] == pd.Timestamp(1792069200001, unit="ms")
    assert row["instrument_id"] == "ETH-USD-PERP"


def test_messages_for_other_markets_or_channels_are_ignored(tmp_path):
    rec = _recorder(tmp_path, channels=["trades"])
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)]))
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)], market=7))
    rec.handle_message({"type": "connected"})
    rec.handle_message({"type": "unsubscribed", "channel": "trade:1"})
    assert rec.take_buffers() == []
    assert rec.subscriptions() == ["trade/1", "trade/2"]


def test_unknown_channel_for_the_venue_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        _recorder(tmp_path, channels=["liquidations"])


# -- store -----------------------------------------------------------------------------------------


def test_store_compaction_merges_parts_and_keeps_existing_rows(tmp_path):
    store = StreamStore.at("lighter", tmp_path)

    def trades(ids):
        rows = {name: [None] * len(ids) for name in TRADES_SCHEMA.names}
        rows["trade_id"] = ids
        rows["timestamp"] = [DAY1_US + i for i in ids]
        return pa.table(rows, schema=TRADES_SCHEMA)

    store.write_part("trades", "BTC", "2026-10-14", trades([1, 2]))
    store.write_part("trades", "BTC", "2026-10-14", trades([2, 3]))
    assert store.pending() == [("trades", "BTC", "2026-10-14")]
    assert store.read("trades", "BTC", "2026-10-14").num_rows == 4  # parts readable before compaction
    assert store.compact("trades", "BTC", "2026-10-14") == 3
    assert store.pending() == []
    assert not store.parts_dir("trades", "BTC", "2026-10-14").exists()
    store.write_part("trades", "BTC", "2026-10-14", trades([0, 3, 4]))
    store.compact("trades", "BTC", "2026-10-14")
    assert store.read("trades", "BTC", "2026-10-14").column("trade_id").to_pylist() == [0, 1, 2, 3, 4]


def test_flush_compacts_finished_days_only_and_writes_health(tmp_path):
    rec = _recorder(tmp_path)
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)], ts=DAY1_US))
    rec.handle_message(_book_msg("subscribed", 10, 0, asks=[(101, 1)], ts=4_102_444_800_000_000, market=2))
    asyncio.run(rec.flush())  # ETH's day (2100-01-01) is not over yet
    assert rec.store.pending() == [("order_book", "ETH", "2100-01-01")]
    assert rec.store.day_path("order_book", "BTC", "2026-10-14").exists()
    health = json.loads((rec.store.root / "_health.json").read_text(encoding="utf-8"))
    assert health["venue"] == "lighter" and health["messages"] == 2
    assert set(health["last_event_us"]) == {"order_book/BTC", "order_book/ETH"}


def test_read_stream_rejects_unknown_channel(tmp_path):
    with pytest.raises(ValueError):
        read_stream("lighter", "funding", "BTC", root=tmp_path)


# -- book comparison -------------------------------------------------------------------------------


def test_compare_books_with_rest_orders():
    reference = book_from_orders(
        asks=[("101", "1"), ("101", "0.5"), ("102", "2"), ("103", "0.1")],
        bids=[("99", "3"), ("98", "1"), ("97", "1")],
    )
    recorded = OrderBook()
    recorded.reset([(101.0, 1.5), (102.0, 2.0), (103.0, 7.0), (104.0, 1.0)], [(99.0, 3.0), (98.5, 1.0), (98.0, 5.0)], 1)
    asks, bids = compare_books(recorded, reference)
    # The deepest REST level (possibly partial) and anything beyond are not compared.
    assert (asks.compared, asks.matching) == (2, 2)
    assert (bids.compared, bids.matching) == (3, 1)
    assert bids.mismatches == [(98.5, 1.0, None), (98.0, 5.0, 1.0)]


# -- network (fake websocket) ----------------------------------------------------------------------


class FakeWebsocket:
    def __init__(self, messages):
        self.incoming = [json.dumps(m) for m in messages]
        self.sent = []

    async def recv(self):
        if not self.incoming:
            raise ConnectionResetError("closed")
        return self.incoming.pop(0)

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_consume_subscribes_answers_pings_and_resubscribes_on_gap(tmp_path):
    rec = _recorder(tmp_path, channels=["order_book"])
    ws = FakeWebsocket([
        {"type": "connected"},
        {"type": "ping"},
        _book_msg("subscribed", 10, 0, asks=[(101, 1)]),
        _book_msg("update", 12, 11),
    ])
    with pytest.raises(ConnectionResetError):
        asyncio.run(rec._consume(ws, rec.subscriptions()))
    assert ws.sent == [
        {"type": "subscribe", "channel": "order_book/1"},
        {"type": "subscribe", "channel": "order_book/2"},
        {"type": "pong"},
        {"type": "unsubscribe", "channel": "order_book/1"},
        {"type": "subscribe", "channel": "order_book/1"},
    ]


def test_run_reconnects_and_resyncs_the_book(tmp_path):
    sessions = [
        [{"type": "connected"}, _book_msg("subscribed", 10, 0, asks=[(101, 1)]),
         _book_msg("update", 11, 10, asks=[(102, 1)])],
        [{"type": "connected"}, _book_msg("subscribed", 30, 0, asks=[(105, 1)]),
         _book_msg("update", 31, 30, bids=[(99, 1)])],
    ]
    opened = []

    class Session(FakeWebsocket):
        async def recv(self):
            if not self.incoming and not sessions:
                await asyncio.sleep(3600)  # stay connected until the run ends
            return await super().recv()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    def connect(url):
        opened.append(url)
        return Session(sessions.pop(0))

    rec = _recorder(tmp_path, channels=["order_book"], markets={1: MARKETS[1]}, max_backoff=0.01,
                    stale_timeout=3600, connect=connect)
    stats = asyncio.run(rec.run(duration=0.5))
    assert len(opened) == 2 and stats.reconnections == 1
    frame = _read(tmp_path, "order_book", "BTC")
    assert (frame["kind"] == "snapshot").sum() == 2
    book = book_at(frame)
    assert book.asks == {105.0: 1.0} and book.bids == {99.0: 1.0} and book.nonce == 31


# -- packaging -------------------------------------------------------------------------------------


def test_core_does_not_import_the_stream_extra():
    code = ("import sys, quant_marketdata, quant_marketdata.venues; "
            "print(any(m in sys.modules for m in ('websockets', 'quant_marketdata.venues.stream')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"


@pytest.mark.live
def test_live_lighter_recording_short(tmp_path):
    from quant_marketdata.venues.stream.cli import resolve_markets

    markets = resolve_markets("lighter", ["BTC"])
    rec = Recorder(LighterStream(), StreamStore.at("lighter", tmp_path), markets, ["order_book", "trades"],
                   flush_interval=2.0)
    stats = asyncio.run(rec.run(duration=15))
    assert stats.messages > 0 and rec.books[next(iter(markets))].synced
    replayed = book_at(_read(tmp_path, "order_book", "BTC"))
    live = rec.books[next(iter(markets))].book
    assert replayed.bids == live.bids and replayed.asks == live.asks
