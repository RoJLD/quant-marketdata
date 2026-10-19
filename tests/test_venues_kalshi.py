"""Kalshi adapter on recorded payloads (no network): canonical bounds and outcomes, fixing methods per
market, both data tiers under one schema, quotes without fake mids, YES book, settlements."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import get_venue
from quant_marketdata.venues.contracts import (
    BINARY_CANDLE_COLUMNS,
    BINARY_QUOTE_COLUMNS,
    BinaryVenue,
    resolve_binary_contracts,
)
from quant_marketdata.venues.contracts.registry import binary_settlements
from quant_marketdata.venues.kalshi import (
    KalshiVenue,
    kalshi_bounds,
    parse_candles,
    parse_market_quotes,
    parse_markets,
    parse_orderbook,
    parse_trades,
    recomputed_outcomes,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kalshi"


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _settled(name):
    payload = _fixture(name)
    return parse_markets(payload["markets"], payload["event"], _fixture("series_kxbtcd")["series"])


class FakeClient:
    def __init__(self):
        self.calls = []

    def get(self, path, params=None):
        params = {k: v for k, v in dict(params or {}).items() if v is not None}
        self.calls.append((path, params))
        if path == "series/KXBTCD":
            return _fixture("series_kxbtcd")
        if path == "historical/cutoff":
            return _fixture("historical_cutoff")
        if path == "events":
            if params["status"] == "open":
                return _fixture("events_open")
            settled = _fixture("event_kxbtcd_settled")
            old = _fixture("historical_event_2025")
            return {"cursor": "", "events": [{**settled["event"], "markets": settled["markets"]},
                                             {**old["event"], "markets": []}]}
        if path == "events/KXBTCD-26OCT1804":
            return _fixture("event_kxbtcd_settled")
        if path == "events/KXBTCD-25JAN0117":
            return _fixture("historical_event_2025")
        if path == "historical/markets":
            return _fixture("historical_markets_2025")
        if path == "markets":
            return _fixture("markets_open")
        if path.startswith("series/KXBTCD/events/"):
            payload = _fixture("candles_event")
            if params["start_ts"] >= payload["adjusted_end_ts"]:
                return {"adjusted_end_ts": params["end_ts"], "market_tickers": [], "market_candlesticks": []}
            return payload
        if path.startswith("historical/markets/") and path.endswith("/candlesticks"):
            return _fixture("candles_market_historical")
        if path in ("markets/trades", "historical/trades"):
            if "cursor" in params:  # the recorded page carries a cursor: the next page is empty
                return {"cursor": "", "trades": []}
            return _fixture("trades" if path == "markets/trades" else "historical_trades")
        if path.endswith("/orderbook"):
            return _fixture("orderbook")
        raise AssertionError(f"unexpected path {path}")


# -- bounds, outcomes, fixing ------------------------------------------------------------------------


def test_bounds_on_the_cent_resolution():
    assert kalshi_bounds("greater", 81499.99, None)[0] == 81500.0
    assert math.isnan(kalshi_bounds("greater", 81499.99, None)[1])
    low, high = kalshi_bounds("between", 74900, 74999.99)
    assert (low, high) == (74900.0, 75000.0)
    low, high = kalshi_bounds("less", None, 72200)
    assert math.isnan(low) and high == 72200.0
    assert all(math.isnan(x) for x in kalshi_bounds("custom", 1, 2))


@pytest.mark.parametrize("fixture", ["event_kxbtcd_settled", "event_kxbtc_settled"])
def test_recomputed_outcome_equals_result_on_every_settled_market(fixture):
    contracts = _settled(fixture)
    assert contracts["outcome"].notna().all()
    assert (recomputed_outcomes(contracts) == contracts["outcome"]).all()  # 100 %
    assert set(contracts["fixing_method"]) == {"mean"} and set(contracts["fixing_window_s"]) == {60.0}


def test_settled_markets_take_the_event_strike_date_not_the_ticker():
    contracts = _settled("event_kxbtcd_settled")
    # KXBTCD-26OCT1804: "04" is 4 am EDT; the fixing is 08:00 UTC.
    assert set(contracts["expiry"]) == {pd.Timestamp("2026-10-18 08:00")}
    row = contracts.iloc[0]
    assert row["cadence"] == "hourly" and row["venue_series"] == "KXBTCD"
    assert row["trading_open"] == pd.Timestamp("2026-10-18 07:00")
    assert row["settlement_value"] == pytest.approx(81568.22)
    assert (row["payout"], row["price_tick"], row["fee_type_raw"], row["fee_multiplier_raw"]) == \
        (1.0, 0.01, "quadratic", 1.0)
    build = resolve_binary_contracts("kalshi", contracts)
    assert build.ok
    assert "BTC-USD-20261018T0800-GE81500" in set(build.contracts["contract_id"])
    assert set(build.contracts["settlement_index_id"]) == {"cfb:BRTI"}


def test_range_series_ids():
    build = resolve_binary_contracts("kalshi", _settled("event_kxbtc_settled"))
    assert build.ok
    conditions = {c.split("-")[-1][:2] for c in build.contracts["contract_id"]}
    assert conditions == {"IN", "GE", "LT"}


def test_historical_tier_same_schema_and_the_2025_rule():
    old = _fixture("historical_event_2025")
    contracts = parse_markets(_fixture("historical_markets_2025")["markets"], old["event"])
    current = _settled("event_kxbtcd_settled")
    assert list(contracts.columns) == list(current.columns)
    assert set(contracts["fixing_method"]) == {"trimmed_mean_20pct"}
    assert contracts["cadence"].isna().all()  # no product_metadata before the cutoff
    assert (recomputed_outcomes(contracts) == contracts["outcome"]).all()


def test_unexpected_result_is_nan_and_flagged(caplog):
    payload = _fixture("event_kxbtcd_settled")
    market = {**payload["markets"][0], "result": "scalar"}
    contracts = parse_markets([market], payload["event"])
    assert math.isnan(contracts["outcome"].iloc[0]) and "unexpected result" in caplog.text


# -- quotes, candles, trades, book -------------------------------------------------------------------


def test_quotes_never_invent_a_side():
    quotes = parse_market_quotes(_fixture("markets_open")["markets"], pd.Timestamp("2026-10-19 07:10"))
    assert list(quotes.columns[: len(BINARY_QUOTE_COLUMNS)]) == BINARY_QUOTE_COLUMNS
    two_sided = quotes.dropna(subset=["yes_bid", "yes_ask"])
    assert len(two_sided) == 6
    assert (two_sided["p_mid"] == (two_sided["yes_bid"] + two_sided["yes_ask"]) / 2).all()
    one_sided = quotes[quotes["yes_bid"].isna()]
    assert len(one_sided) == 3 and one_sided["p_mid"].isna().all() and one_sided["yes_bid_size"].isna().all()
    assert (one_sided["raw_yes_bid"] == 0).all()
    assert set(quotes["quote_source"]) == {"snapshot"}


def test_candles_both_tiers_one_schema():
    current = parse_candles(_fixture("candles_market")["candlesticks"], "T", "1m")
    historical = parse_candles(_fixture("candles_market_historical")["candlesticks"], "T", "1h")
    assert list(current.columns[: len(BINARY_CANDLE_COLUMNS)]) == BINARY_CANDLE_COLUMNS
    assert list(current.columns) == list(historical.columns)
    first = current.iloc[0]
    assert first["ts"] == pd.Timestamp(1792306860, unit="s")
    assert first["yes_bid_close"] == 0.88 and first["yes_ask_open"] != first["yes_ask_open"]  # 1.00 = no ask
    assert first["price_mean"] == pytest.approx(0.8715) and first["volume"] == pytest.approx(2146.67)
    old = historical.iloc[0]
    assert old["yes_bid_close"] == 0.05 and old["open_interest"] == 257.0 and math.isnan(old["price_close"])


def test_trades_and_book():
    trades = parse_trades(_fixture("trades")["trades"] + _fixture("historical_trades")["trades"])
    assert set(trades["side"]) == {"yes", "no"} and trades["price"].between(0, 1).all()
    assert (trades["price"] + trades["no_price_raw"]).round(4).eq(1).all()
    book = parse_orderbook(_fixture("orderbook"), "KXBTCD-26OCT1806-T81499.99")
    assert book["bids"][0][0] == 0.96 and book["asks"][0][0] == 0.97  # 1 - best NO bid 0.03
    assert book["bids"][0][0] < book["asks"][0][0]


def test_settlements_one_row_per_event():
    contracts = resolve_binary_contracts("kalshi", _settled("event_kxbtcd_settled")).contracts
    frame = binary_settlements(contracts, source_endpoint="markets.expiration_value",
                               seen_at=pd.Timestamp("2026-10-18 09:20"))
    assert len(frame) == 1
    row = frame.iloc[0]
    assert (row["ts"], row["settlement_price"], row["method"], row["venue_ref"]) == \
        (pd.Timestamp("2026-10-18 08:00"), 81568.22, "mean", "KXBTCD-26OCT1804")
    assert (row["settlement_index_id"], row["method_source"]) == ("cfb:BRTI", "rules_text")


# -- adapter -----------------------------------------------------------------------------------------


def test_only_declared_series_are_served():
    venue = get_venue("kalshi", client=FakeClient())
    assert isinstance(venue, KalshiVenue) and isinstance(venue, BinaryVenue)
    with pytest.raises(ValueError, match="not in the series table"):
        venue.binary_contracts("KXSOLD")
    with pytest.raises(ValueError, match="not in the series table"):
        venue.binary_quotes("INXD")


def test_adapter_contracts_from_both_tiers():
    client = FakeClient()
    venue = KalshiVenue(client)
    open_ = venue.binary_contracts("KXBTCD")
    assert set(open_["cadence"]) == {"hourly", "daily", "weekly"} and len(open_) == 9
    settled = venue.binary_contracts("KXBTCD", status="settled")
    assert set(settled["venue_event_id"]) == {"KXBTCD-26OCT1804", "KXBTCD-25JAN0117"}
    assert ("historical/markets", {"event_ticker": "KXBTCD-25JAN0117", "limit": 1000}) in client.calls
    recent = venue.binary_contracts("KXBTCD", status="settled", start=pd.Timestamp("2026-01-01"))
    assert set(recent["venue_event_id"]) == {"KXBTCD-26OCT1804"}
    assert len(venue.settlements("KXBTCD")) == 2


def test_adapter_event_candles_resume_from_adjusted_end():
    client = FakeClient()
    venue = KalshiVenue(client)
    start = pd.Timestamp("2026-10-19 05:00")
    candles = venue.binary_candles("KXBTCD-26OCT1804", "1m", start, start + pd.Timedelta(hours=1))
    requests = [p for path, p in client.calls if path.startswith("series/KXBTCD/events/")]
    assert len(requests) == 2 and requests[1]["start_ts"] == _fixture("candles_event")["adjusted_end_ts"]
    assert len(candles) == 6 and candles["venue_symbol"].nunique() == 2
    with pytest.raises(ValueError):
        venue.binary_candles("KXBTCD-26OCT1804", "5m", start, start)


def test_adapter_historical_event_candles_go_per_market():
    client = FakeClient()
    venue = KalshiVenue(client)
    start = pd.Timestamp("2025-01-01 21:00")
    candles = venue.binary_candles("KXBTCD-25JAN0117", "1h", start, start + pd.Timedelta(hours=1))
    per_market = [path for path, _ in client.calls if path.startswith("historical/markets/")]
    assert len(per_market) == 2 and not candles.empty


def test_adapter_trades_split_at_the_cutoff():
    client = FakeClient()
    venue = KalshiVenue(client)
    trades = venue.binary_trades("T", pd.Timestamp("2025-01-01"), pd.Timestamp("2026-10-19"))
    assert {"historical/trades", "markets/trades"} <= {path for path, _ in client.calls}
    assert trades["trade_id"].is_unique


@pytest.mark.live
def test_live_kalshi_open_ladder_and_a_settled_event():
    venue = get_venue("kalshi")
    contracts = venue.binary_contracts("KXBTCD")
    build = resolve_binary_contracts("kalshi", contracts)
    assert build.ok and "hourly" in set(contracts["cadence"])
    quotes = venue.binary_quotes("KXBTCD")
    assert len(quotes) > 100 and quotes["yes_bid"].dropna().between(0, 1).all()
    settled = venue.binary_contracts("KXBTCD", status="settled",
                                     start=pd.Timestamp.now() - pd.Timedelta(hours=3))
    assert len(settled) and (recomputed_outcomes(settled) == settled["outcome"]).all()
