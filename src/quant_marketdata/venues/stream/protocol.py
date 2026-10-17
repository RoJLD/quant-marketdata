"""What a venue must provide to be recorded: subscription names, control messages and decoding into
normalized events. The recorder is venue-agnostic and only sees these events."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Protocol, Sequence, Tuple

Level = Tuple[float, float]


@dataclass
class BookUpdate:
    """One order book message. ``begin_nonce`` must equal the previous message's ``nonce`` for a delta to
    chain; a snapshot resets the book."""

    kind: str  # snapshot | delta
    nonce: Optional[int]
    begin_nonce: Optional[int]
    timestamp: Optional[int]  # microseconds; None -> receive time
    offset: Optional[int] = None
    asks: List[Level] = field(default_factory=list)
    bids: List[Level] = field(default_factory=list)


@dataclass
class Control:
    """A non-data message: ``connected`` (send the subscriptions), ``reply`` (send ``reply`` back, e.g. a
    pong), ``error`` or ``info`` (logged)."""

    kind: str
    reply: Optional[Dict[str, Any]] = None
    detail: Any = None


class StreamVenue(Protocol):
    name: str
    url: str
    channels: Tuple[str, ...]  # recorded channel names it supports (see schema.CHANNELS)
    max_subscriptions: int  # per connection

    def subscription(self, channel: str, market_id: int) -> str: ...

    def subscribe_message(self, subscription: str) -> Dict[str, Any]: ...

    def unsubscribe_message(self, subscription: str) -> Dict[str, Any]: ...

    def control(self, message: Mapping[str, Any]) -> Optional[Control]: ...

    def route(self, message: Mapping[str, Any]) -> Optional[Tuple[str, int]]:
        """``(recorded channel, market_id)`` of a data message, or None."""

    def book_update(self, message: Mapping[str, Any]) -> BookUpdate: ...

    def trade_rows(self, message: Mapping[str, Any]) -> Sequence[Dict[str, Any]]:
        """Rows of ``schema.TRADES_SCHEMA`` (``timestamp`` in microseconds, ``trade_id`` set)."""

    def stats_row(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        """One row of ``schema.MARKET_STATS_SCHEMA``; ``timestamp`` None -> receive time."""


__all__ = ["BookUpdate", "Control", "Level", "StreamVenue"]
