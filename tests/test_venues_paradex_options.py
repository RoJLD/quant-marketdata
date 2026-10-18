"""Paradex options parsers on payloads recorded on 2026-10-18 (no network)."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from statistics import NormalDist

import pandas as pd
import pytest

from quant_marketdata.venues.paradex import parse_klines
from quant_marketdata.venues.paradex_options import (
    YEAR_DAYS,
    contract_id,
    format_strike,
    parse_market_history,
    parse_option_markets,
    parse_option_name,
    parse_option_summary,
    parse_option_trades,
    parse_settlement_prices,
    payoff,
)

FIXTURES = Path(__file__).parent / "fixtures" / "paradex"
AS_OF = pd.Timestamp("2026-10-18 19:00")
N, PDF = NormalDist().cdf, NormalDist().pdf


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _contracts():
    return parse_option_markets(_fixture("option_markets")["results"], AS_OF)


# -- names and ids ---------------------------------------------------------------------------------


def test_names_are_parsed_only_for_the_cross_check():
    name = parse_option_name("META-USD-20OCT26-712_5-P")
    assert name == {"base": "META", "quote": "USD", "expiry_date": pd.Timestamp("2026-10-20"),
                    "strike": 712.5, "option_type": "P"}
    assert parse_option_name("NEAR-USD-6NOV26-5_2-C")["expiry_date"] == pd.Timestamp("2026-11-06")
    assert parse_option_name("BTC-USD-PERP") is None and parse_option_name("BTC-USD-31FEB26-1-C") is None
    assert format_strike(82000.0) == "82000" and format_strike(712.5) == "712.5" and format_strike(0.14) == "0.14"
    assert contract_id("BTC-USD", pd.Timestamp("2026-10-23 08:00"), 82000.0, "C") == "BTC-USD-20261023T0800-82000-C"


# -- contracts -------------------------------------------------------------------------------------


def test_contracts_from_structured_fields():
    contracts, unresolved = _contracts()
    assert unresolved.empty and len(contracts) == 10  # the perp is ignored
    rows = contracts.set_index("venue_symbol")
    btc = rows.loc["BTC-USD-23OCT26-82000-C"]
    assert btc["contract_id"] == "BTC-USD-20261023T0800-82000-C" and btc["underlying_id"] == "BTC-USD"
    assert (btc["option_type"], btc["strike"], btc["settle_asset"]) == ("C", 82000.0, "USDC")
    assert (btc["style"], btc["exercise"], btc["contract_size"], btc["price_unit"]) == ("european", "cash", 1.0, "quote")
    assert btc["settlement_index_id"] == "paradex:BTC" and btc["status"] == "active"
    assert (btc["maker_fee_raw"], btc["taker_fee_raw"]) == (0.0001, 0.0001)
    assert json.loads(btc["fee_extra_raw"])["api_fee"]["taker_fee"]["fee_cap"] == "0.125"
    assert btc["first_seen_at"] == AS_OF and pd.isna(btc["settlement_price"])
    # The hour comes from expiry_at, never from the name.
    assert rows.loc["META-USD-20OCT26-712_5-P", "contract_id"] == "META-USD-20261020T2000-712.5-P"
    assert rows.loc["XAU-USD-23OCT26-4200-C", "expiry"] == pd.Timestamp("2026-10-23 17:30")
    assert rows.loc["US500-USD-18DEC26-7500-C", "expiry"] == pd.Timestamp("2026-12-18 14:30")
    assert rows.loc["NEAR-USD-6NOV26-5_2-C", "contract_id"] == "NEAR-USD-20261106T0800-5.2-C"


def test_name_field_disagreements_are_unresolved_not_arbitrated():
    markets = copy.deepcopy(_fixture("option_markets")["results"])
    markets[0]["strike_price"] = "80500"
    markets[1]["option_type"] = "CALL"
    markets[2]["expiry_at"] += 86_400_000
    markets[3]["option_type"] = "STRADDLE"
    contracts, unresolved = parse_option_markets(markets, AS_OF)
    reasons = unresolved.set_index("venue_symbol")["reason"]
    assert reasons[markets[0]["symbol"]] == "name_fields_mismatch"
    assert reasons[markets[1]["symbol"]] == "name_fields_mismatch"
    assert reasons[markets[2]["symbol"]] == "name_fields_mismatch"
    assert reasons[markets[3]["symbol"]] == "unknown option_type 'STRADDLE'"
    assert len(contracts) == 6


def test_status_follows_listing_and_expiry():
    contracts, _ = parse_option_markets(_fixture("option_markets")["results"], pd.Timestamp("2026-10-24"))
    status = contracts.set_index("venue_symbol")["status"]
    assert status["BTC-USD-23OCT26-82000-C"] == "expired" and status["BTC-USD-27NOV26-88000-C"] == "active"
    early, _ = parse_option_markets(_fixture("option_markets")["results"], pd.Timestamp("2026-01-01"))
    assert early["status"].eq("scheduled").all()


# -- chain snapshot --------------------------------------------------------------------------------


def _b76(spot, strike, tau, vol, rate, call):
    forward = spot * math.exp(rate * tau)
    sd = vol * math.sqrt(tau)
    d1 = (math.log(forward / strike) + sd * sd / 2) / sd
    d2 = d1 - sd
    return forward * N(d1) - strike * N(d2) if call else strike * N(-d2) - forward * N(-d1)


def test_chain_rows_and_units():
    contracts, _ = _contracts()
    chain = parse_option_summary(_fixture("option_summary")["results"], contracts, AS_OF)
    assert len(chain) == 10 and chain["ts_snapshot"].eq(AS_OF).all()
    row = chain.set_index("venue_symbol").loc["BTC-USD-23OCT26-88000-C"]
    assert row["contract_id"] == "BTC-USD-20261023T0800-88000-C"
    assert (row["greeks_reference"], row["underlying_kind"], row["greeks_source"]) == ("spot", "spot", "venue")
    assert 0 < row["iv_bid"] < row["iv_mark"] < row["iv_ask"] < 2  # fractions, not percent
    assert row["index_price"] == row["underlying_price"] and row["discount_factor"] == 1.0
    tau = (pd.Timestamp("2026-10-23 08:00") - row["ts"]) / pd.Timedelta(days=YEAR_DAYS)
    assert row["forward_price"] == pytest.approx(row["underlying_price"] * math.exp(row["forward_rate"] * tau))
    assert pd.isna(row["last"])  # "" when never traded
    traded = chain.set_index("venue_symbol").loc["BTC-USD-27NOV26-88000-C"]
    assert traded["volume_24h"] == pytest.approx(traded["volume_24h_raw"] / traded["underlying_price"])
    one_sided = chain[chain["iv_bid"].isna()]
    assert not one_sided.empty and one_sided["iv_mark"].notna().all()


def test_greeks_recompute_black76_on_the_spot_convention():
    contracts, _ = _contracts()
    chain = parse_option_summary(_fixture("option_summary")["results"], contracts, AS_OF)
    chain = chain.merge(contracts[["venue_symbol", "strike", "option_type", "expiry"]], on="venue_symbol")
    checked = 0
    for r in chain.itertuples():
        tau = (r.expiry - r.ts) / pd.Timedelta(days=YEAR_DAYS)
        call, spot = r.option_type == "C", r.underlying_price
        args = (r.strike, tau, r.iv_mark, r.forward_rate, call)
        value = _b76(spot, *args)
        if value < 1e-3 * spot * 1e-3:
            continue  # far wings: relative errors are meaningless
        h = spot * 1e-4
        delta = (_b76(spot + h, *args) - _b76(spot - h, *args)) / (2 * h)
        vega = (_b76(spot, r.strike, tau, r.iv_mark + 1e-4, r.forward_rate, call)
                - _b76(spot, r.strike, tau, r.iv_mark - 1e-4, r.forward_rate, call)) / 2e-4 / 100
        step = 1 / (YEAR_DAYS * 24 * 60)
        theta = (_b76(spot, r.strike, tau - step, r.iv_mark, r.forward_rate, call) - value) / step / YEAR_DAYS
        assert value == pytest.approx(r.mark, rel=1e-4)
        assert delta == pytest.approx(r.delta, rel=5e-3)  # w.r.t. spot
        assert vega == pytest.approx(r.vega, rel=5e-3)  # per vol point
        assert theta == pytest.approx(r.theta, rel=5e-3)  # per calendar day, constant spot
        assert -tau * value / 100 == pytest.approx(r.rho, rel=5e-3)  # per 1 % of r
        checked += 1
    assert checked >= 6


# -- settlements, trades, candles ------------------------------------------------------------------


def test_settlements_are_the_spot_twap_and_payoffs_follow():
    seen = pd.Timestamp("2026-10-18 19:05")
    rows, contracts = parse_settlement_prices(_fixture("option_settlements"), seen)
    for c in contracts.itertuples():
        name = parse_option_name(c.venue_symbol)
        assert c.settlement_payoff_raw == pytest.approx(payoff(name["option_type"], name["strike"], c.settlement_price))
    indexed = rows.set_index(["settlement_index_id", "ts"])["settlement_price"]
    assert indexed[("paradex:BTC", pd.Timestamp("2026-10-18 08:00"))] == pytest.approx(81587.70086941296)
    assert ("paradex:XAU", pd.Timestamp("2026-10-16 17:30")) in indexed.index
    assert len(rows) == 4  # two BTC contracts, one fixing
    assert rows["method"].eq("twap_30min").all() and rows["method_source"].eq("venue_doc").all()
    assert rows["first_seen_at"].eq(seen).all()
    errors = parse_settlement_prices([{"error": "MARKET_NOT_FOUND", "message": "Market not found"}], seen)
    assert errors[0].empty and errors[1].empty


def test_market_history_settled_events():
    events = parse_market_history(_fixture("market_history")["results"], pd.Timestamp("2026-10-18 19:05"))
    assert len(events) == 3 and events["settlement_source"].eq("markets/history").all()
    for e in events.itertuples():
        name = parse_option_name(e.venue_symbol)
        assert e.settlement_payoff_raw == pytest.approx(payoff(name["option_type"], name["strike"], e.settlement_price))
    assert events["settled_at"].dt.floor("h").eq(pd.Timestamp("2026-10-18 08:00")).all()


def test_trades_and_mark_candles():
    contracts, _ = _contracts()
    ids = dict(zip(contracts["venue_symbol"], contracts["contract_id"], strict=True))
    trades = parse_option_trades(_fixture("option_trades")["results"], ids)
    assert trades["ts"].is_monotonic_increasing and set(trades["side"]) <= {"buy", "sell"}
    assert trades["trade_type"].eq("BLOCK_TRADE").all() and trades["block_id"].notna().all()
    assert trades["contract_id"].eq("BTC-USD-20261127T0800-88000-C").all()
    assert "wallet" not in trades.columns
    mark = parse_klines(_fixture("option_mark_klines")["results"], "mark")
    underlying = parse_klines(_fixture("option_underlying_klines")["results"], "underlying")
    assert len(mark) == 3 and mark["volume_quote"].isna().all() and (underlying["price_type"] == "underlying").all()
    assert underlying["close"].iloc[0] > 50_000 > mark["close"].iloc[0]  # spot vs option mark
