import math

import ccxt

from quant_marketdata.live_quotes import (
    _half_spread_bps,
    _parse_hl_l2book,
    fetch_leg_books,
    fetch_leg_spreads,
)


def test_half_spread_bps_value():
    # bid 100.00 / ask 100.02 -> spread 0.02, half 0.01, mid 100.01 -> ~0.9999 bps
    assert abs(_half_spread_bps(100.00, 100.02) - 0.9999) < 1e-3


def test_half_spread_bps_degenerate_mid():
    assert math.isnan(_half_spread_bps(0.0, 0.0))


class _FakeExchange:
    _BOOKS = {
        "BTC/USDT": {"bids": [[100.00, 1]], "asks": [[100.02, 1]]},
        "BTC/USDT:USDT": {"bids": [[100.00, 1]], "asks": [[100.04, 1]]},
    }

    def __init__(self, cfg):
        self.cfg = cfg

    def fetch_order_book(self, symbol, limit=5):
        return self._BOOKS[symbol]


def test_fetch_leg_spreads_mocked(monkeypatch):
    monkeypatch.setattr(ccxt, "binance", _FakeExchange)
    out = fetch_leg_spreads({"btc_spot": "BTC/USDT", "btc_perp": "BTC/USDT:USDT"}, venue="binance")
    assert set(out) == {"btc_spot", "btc_perp"}
    assert abs(out["btc_spot"] - 0.9999) < 1e-3
    assert out["btc_perp"] > out["btc_spot"]  # wider perp book -> larger half-spread


class _FakeBookEx:
    def __init__(self, *a, **k):
        pass

    def fetch_order_book(self, sym, limit=50):
        return {"bids": [[100.0, 1.0], [99.0, 2.0]], "asks": [[101.0, 1.0], [102.0, 2.0]]}


def test_fetch_leg_books_shapes(monkeypatch):
    monkeypatch.setattr(ccxt, "binance", _FakeBookEx, raising=False)
    books = fetch_leg_books({"btc_spot": "BTC/USDT"}, venue="binance", depth=5)
    assert books["btc_spot"]["bids"][0] == (100.0, 1.0)
    assert books["btc_spot"]["asks"][1] == (102.0, 2.0)


def test_parse_hl_l2book_shapes_and_depth():
    # HL l2Book levels = [bid dicts, ask dicts], each {px, sz} (str). depth truncates each side.
    levels = [[{"px": "100.0", "sz": "5"}, {"px": "99.0", "sz": "2"}], [{"px": "101.0", "sz": "3"}]]
    out = _parse_hl_l2book(levels, depth=50)
    assert out["bids"] == [(100.0, 5.0), (99.0, 2.0)] and out["asks"] == [(101.0, 3.0)]
    assert _parse_hl_l2book(levels, depth=1)["bids"] == [(100.0, 5.0)]     # depth-1 keeps only the touch


class _RaisingHLEx:
    def __init__(self, *a, **k):
        pass

    def fetch_order_book(self, sym, limit=50):
        raise ccxt.NetworkError("hyperliquid load_markets failed")   # the flaky ccxt HL adapter


def test_fetch_leg_books_hyperliquid_falls_back_to_urllib(monkeypatch):
    # ccxt HL raises -> fetch_leg_books falls back to the urllib l2Book fetch for the coin (BTC from the symbol).
    monkeypatch.setattr(ccxt, "hyperliquid", _RaisingHLEx, raising=False)
    monkeypatch.setattr(
        "quant_marketdata.live_quotes._hl_l2book_urllib",
        lambda coin, depth: {"bids": [(coin, 1.0)], "asks": [(coin, 1.0)]},   # stub proves the fallback fired
    )
    books = fetch_leg_books({"p": "BTC/USDC:USDC"}, venue="hyperliquid", depth=5)
    assert books["p"]["bids"][0] == ("BTC", 1.0)                     # coin extracted from the symbol base


def test_fetch_leg_books_non_hl_reraises(monkeypatch):
    # a ccxt failure on a NON-Hyperliquid venue must propagate (no urllib fallback).
    monkeypatch.setattr(ccxt, "binance", _RaisingHLEx, raising=False)
    try:
        fetch_leg_books({"p": "BTC/USDT:USDT"}, venue="binance", depth=5)
        raise AssertionError("expected the ccxt error to propagate for a non-HL venue")
    except ccxt.NetworkError:
        pass
