"""Lighter websocket protocol (``wss://mainnet.zklighter.elliot.ai/stream``), spoken directly: the SDK's
``WsClient`` only handles ``order_book`` and ``account_all`` and has no reconnection.

Channels ``order_book/<market_id>``, ``trade/<market_id>`` and ``market_stats/<market_id>``; messages carry
``channel: "<name>:<market_id>"``. Each order book message holds ``begin_nonce`` (the previous ``nonce``) and
``nonce``; the subscription reply (``subscribed/order_book``) is a full snapshot. Trades are replayed on
(re)subscription, hence the recorder's de-duplication by ``trade_id``. The server sends ``connected`` once,
``ping`` (answered with ``pong``) and ``shutdown`` notices. 50 subscriptions per connection stays well below
the undocumented server limit.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from quant_marketdata.venues.stream.protocol import BookUpdate, Control, Level
from quant_marketdata.venues.stream.schema import STATS_FLOATS, to_float, to_int

WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
# Recorded channel -> websocket channel name.
_WS_CHANNELS = {"order_book": "order_book", "trades": "trade", "market_stats": "market_stats"}
_RECORDED = {ws: channel for channel, ws in _WS_CHANNELS.items()}


def _levels(levels: Sequence[Mapping[str, str]]) -> List[Level]:
    return [(float(level["price"]), float(level["size"])) for level in levels]


class LighterStream:
    name = "lighter"
    url = WS_URL
    channels = tuple(_WS_CHANNELS)
    max_subscriptions = 50

    def subscription(self, channel: str, market_id: int) -> str:
        return f"{_WS_CHANNELS[channel]}/{market_id}"

    def subscribe_message(self, subscription: str) -> Dict[str, Any]:
        return {"type": "subscribe", "channel": subscription}

    def unsubscribe_message(self, subscription: str) -> Dict[str, Any]:
        return {"type": "unsubscribe", "channel": subscription}

    def control(self, message: Mapping[str, Any]) -> Optional[Control]:
        kind = message.get("type", "")
        if kind == "ping":
            return Control("reply", reply={"type": "pong"})
        if kind == "connected":
            return Control("connected")
        if kind == "shutdown":
            return Control("info", detail=message)
        if kind == "error" or "error" in message:
            return Control("error", detail=message)
        return None

    def route(self, message: Mapping[str, Any]) -> Optional[Tuple[str, int]]:
        kind, channel_name = message.get("type", ""), message.get("channel", "")
        if "/" not in kind or ":" not in channel_name:
            return None
        ws_channel, _, market = channel_name.partition(":")
        recorded = _RECORDED.get(ws_channel)
        try:
            return (recorded, int(market)) if recorded else None
        except ValueError:
            return None

    def book_update(self, message: Mapping[str, Any]) -> BookUpdate:
        payload = message.get("order_book") or {}
        return BookUpdate(
            kind="snapshot" if message.get("type", "").startswith("subscribed") else "delta",
            nonce=to_int(payload.get("nonce")),
            begin_nonce=to_int(payload.get("begin_nonce")),
            timestamp=to_int(message.get("last_updated_at", payload.get("last_updated_at"))),
            offset=to_int(message.get("offset", payload.get("offset"))),
            asks=_levels(payload.get("asks") or []),
            bids=_levels(payload.get("bids") or []),
        )

    def trade_rows(self, message: Mapping[str, Any]) -> Sequence[Dict[str, Any]]:
        trades = list(message.get("trades") or []) + list(message.get("liquidation_trades") or [])
        rows = []
        # Oldest first, so that rows land in time order.
        for trade in sorted(trades, key=lambda t: (t.get("timestamp", 0), t.get("trade_id", 0))):
            trade_id = to_int(trade.get("trade_id"))
            if trade_id is None:
                continue
            rows.append({
                "timestamp": int(trade["timestamp"]) * 1000,  # ms -> us
                "trade_id": trade_id,
                "type": trade.get("type"),
                "price": to_float(trade.get("price")),
                "size": to_float(trade.get("size")),
                "usd_amount": to_float(trade.get("usd_amount")),
                "is_maker_ask": trade.get("is_maker_ask"),
                "ask_id": to_int(trade.get("ask_id")),
                "bid_id": to_int(trade.get("bid_id")),
                "ask_account_id": to_int(trade.get("ask_account_id")),
                "bid_account_id": to_int(trade.get("bid_account_id")),
                "taker_fee": to_int(trade.get("taker_fee")),
                "maker_fee": to_int(trade.get("maker_fee")),
                "block_height": to_int(trade.get("block_height")),
                "transaction_time": to_int(trade.get("transaction_time")),
                "tx_hash": trade.get("tx_hash"),
            })
        return rows

    def stats_row(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        stats = message.get("market_stats") or {}
        stamp = to_int(message.get("timestamp"))
        funding = to_int(stats.get("funding_timestamp"))
        row: Dict[str, Any] = {name: to_float(stats.get(name)) for name in STATS_FLOATS}
        row.update(
            timestamp=stamp * 1000 if stamp is not None else None,
            funding_timestamp=funding * 1000 if funding is not None else None,
        )
        return row


__all__ = ["LighterStream", "WS_URL"]
