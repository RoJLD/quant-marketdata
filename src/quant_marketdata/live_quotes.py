"""Live top-of-book quote adapter for crypto carry. ccxt; public API, no key.

ISOLATED network boundary: the only module that touches ccxt (imported lazily inside the fetch
functions, so importing this module needs only stdlib). Returns observed half-spreads (bps) / order
books per leg. Requires the ``live`` extra (``pip install quant-marketdata[live]``).
"""

from __future__ import annotations


def _half_spread_bps(bid: float, ask: float) -> float:
    """Top-of-book half-spread in bps: (ask-bid)/2 / mid * 1e4. NaN if mid <= 0."""
    mid = 0.5 * (bid + ask)
    if mid <= 0:
        return float("nan")
    return float((ask - bid) / 2.0 / mid * 1e4)


def fetch_leg_spreads(symbols: dict, *, venue: str = "binance") -> dict:
    """Observed half-spread (bps) per leg via ccxt ``fetch_order_book`` (top of book).

    ``symbols`` maps a leg name (e.g. "btc_spot", "btc_perp") to its ccxt symbol (e.g. "BTC/USDT",
    "BTC/USDT:USDT"). Public market data -- no API key. ``venue`` is a ccxt exchange id.
    """
    import ccxt

    ex = getattr(ccxt, venue)({"enableRateLimit": True})
    out: dict = {}
    for leg, sym in symbols.items():
        ob = ex.fetch_order_book(sym, limit=5)
        bid = ob["bids"][0][0] if ob.get("bids") else float("nan")
        ask = ob["asks"][0][0] if ob.get("asks") else float("nan")
        out[leg] = _half_spread_bps(bid, ask)
    return out


def _parse_hl_l2book(levels: list, depth: int) -> dict:
    """Parse a Hyperliquid ``l2Book`` ``levels`` payload ([bid dicts, ask dicts], each {px, sz}) into the
    {bids, asks} (price, qty)-tuple shape (network-free)."""
    bids = [(float(lvl["px"]), float(lvl["sz"])) for lvl in levels[0][:depth]]
    asks = [(float(lvl["px"]), float(lvl["sz"])) for lvl in levels[1][:depth]]
    return {"bids": bids, "asks": asks}


def _hl_l2book_urllib(coin: str, depth: int) -> dict:
    """Hyperliquid L2 book via the public ``l2Book`` info endpoint (urllib) -- the fallback for when ccxt's
    Hyperliquid adapter fails (its load_markets POST is unreliable in some environments)."""
    import json
    import urllib.request

    body = json.dumps({"type": "l2Book", "coin": coin}).encode()
    req = urllib.request.Request(
        "https://api.hyperliquid.xyz/info", data=body, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310 (trusted Hyperliquid host)
        return _parse_hl_l2book(json.load(r)["levels"], depth)


def fetch_leg_books(symbols: dict, *, venue: str = "binance", depth: int = 50) -> dict:
    """Observed order book (bids/asks as (price, qty) tuples) per leg via ccxt ``fetch_order_book``.

    ``symbols`` maps a leg name to its ccxt symbol. Public market data -- no API key. ``depth`` is the number
    of book levels requested (for the paper-execution book-walking fill model). For ``venue="hyperliquid"``,
    a ccxt failure falls back to a direct urllib ``l2Book`` fetch (ccxt's HL adapter is unreliable in some
    environments); the coin is taken from the symbol's base (e.g. ``BTC/USDC:USDC`` -> ``BTC``).
    """
    import ccxt

    ex = getattr(ccxt, venue)({"enableRateLimit": True})
    out: dict = {}
    for leg, sym in symbols.items():
        try:
            ob = ex.fetch_order_book(sym, limit=depth)
            out[leg] = {
                "bids": [(float(p), float(q)) for p, q in ob.get("bids", [])],
                "asks": [(float(p), float(q)) for p, q in ob.get("asks", [])],
            }
        except Exception:  # noqa: BLE001 (ccxt HL adapter is flaky -> fall back to urllib for that venue only)
            if venue != "hyperliquid":
                raise
            out[leg] = _hl_l2book_urllib(sym.split("/")[0], depth)
    return out
