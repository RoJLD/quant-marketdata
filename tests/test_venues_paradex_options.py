"""Paradex options on payloads recorded on 2026-10-18 (no network), in the canonical contract schemas."""

from __future__ import annotations

import copy
import json
import math
from pathlib import Path
from statistics import NormalDist

import pandas as pd
import pytest

from quant_marketdata.venues.contracts import (
    OPTION_MARK_CANDLE_COLUMNS,
    OPTION_QUOTE_COLUMNS,
    OPTION_SNAPSHOT_COLUMNS,
    OPTION_TRADE_COLUMNS,
    SETTLEMENT_COLUMNS,
    ContractStore,
    OptionsVenue,
    resolve_option_contracts,
    update_contract_registry,
)
from quant_marketdata.venues.http import HttpError
from quant_marketdata.venues.paradex import ParadexVenue
from quant_marketdata.venues.paradex_options import (
    YEAR_DAYS,
    contract_settlements,
    parse_market_history,
    parse_option_markets,
    parse_option_name,
    parse_option_summary,
    parse_option_trades,
    payoff,
    settlement_rows,
)
from quant_marketdata.venues.paradex_snapshot import (
    download_mark_candles,
    settle_expired,
    snapshot_pass,
)
from quant_marketdata.venues.schema import Dataset

FIXTURES = Path(__file__).parent / "fixtures" / "paradex"
AS_OF = pd.Timestamp("2026-10-18 19:00")
N = NormalDist().cdf


def _fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


def _listing(markets=None, as_of=AS_OF):
    return parse_option_markets(markets if markets is not None else _fixture("option_markets")["results"], as_of)


def _resolved():
    return resolve_option_contracts("paradex", _listing())


# -- names and contracts ---------------------------------------------------------------------------


def test_names_are_parsed_only_for_the_cross_check():
    name = parse_option_name("META-USD-20OCT26-712_5-P")
    assert name == {"base": "META", "quote": "USD", "expiry_date": pd.Timestamp("2026-10-20"),
                    "strike": 712.5, "option_type": "P"}
    assert parse_option_name("NEAR-USD-6NOV26-5_2-C")["expiry_date"] == pd.Timestamp("2026-11-06")
    assert parse_option_name("BTC-USD-PERP") is None and parse_option_name("BTC-USD-31FEB26-1-C") is None
    assert parse_option_name("BTC-USD-113000-C") is None  # a 2025 perpetual option


def test_listing_in_the_snapshot_schema():
    listing = _listing()
    assert list(listing.columns[: len(OPTION_SNAPSHOT_COLUMNS)]) == OPTION_SNAPSHOT_COLUMNS
    assert len(listing) == 10  # the perp is ignored
    btc = listing.set_index("venue_symbol").loc["BTC-USD-23OCT26-82000-C"]
    assert (btc["venue_base"], btc["venue_quote"], btc["settle_asset"]) == ("BTC", "USD", "USDC")
    assert (btc["option_type"], btc["strike"], btc["expiry"]) == ("C", 82000.0, pd.Timestamp("2026-10-23 08:00"))
    assert (btc["style"], btc["exercise"], btc["contract_size"], btc["price_unit"]) == ("european", "cash", 1.0, "quote")
    assert (btc["maker_fee_raw"], btc["taker_fee_raw"], btc["status"]) == (0.0001, 0.0001, "active")
    assert json.loads(btc["fee_extra_raw"])["api_fee"]["taker_fee"]["fee_cap"] == "0.125"
    assert (btc["option_type_raw"], btc["strike_raw"], btc["raw_min_notional"]) == ("CALL", "82000", 20.0)


def test_registry_resolves_every_paradex_base():
    build = _resolved()
    assert build.ok, build.unresolved
    rows = build.contracts.set_index("venue_symbol")
    assert rows.loc["BTC-USD-23OCT26-82000-C", "contract_id"] == "BTC-USD-20261023T0800-82000-C"
    assert rows.loc["BTC-USD-23OCT26-82000-C", "settlement_index_id"] == "paradex:BTC"
    assert rows.loc["BTC-USD-23OCT26-82000-C", "mapping_source"] == "rule"
    # The hour comes from expiry_at, never from the name.
    assert rows.loc["META-USD-20OCT26-712_5-P", "contract_id"] == "META-USD-20261020T2000-712.5-P"
    assert rows.loc["XAU-USD-23OCT26-4200-C", "contract_id"] == "XAU-USD-20261023T1730-4200-C"
    assert rows.loc["US500-USD-18DEC26-7500-C", "expiry"] == pd.Timestamp("2026-12-18 14:30")
    assert rows.loc["NEAR-USD-6NOV26-5_2-C", "contract_id"] == "NEAR-USD-20261106T0800-5.2-C"
    assert (rows.loc["XAU-USD-23OCT26-4200-C", ["asset_class", "underlying_ref", "mapping_source"]].tolist()
            == ["commodity", "XAU", "override"])
    assert rows.loc["META-USD-20OCT26-712_5-P", "asset_class"] == "equity"
    assert rows.loc["US500-USD-18DEC26-7500-C", "asset_class"] == "index"


def test_name_field_disagreements_are_unresolved_by_the_registry():
    markets = copy.deepcopy(_fixture("option_markets")["results"])
    markets[0]["strike_price"] = "80500"
    markets[1]["option_type"] = "CALL"
    markets[2]["expiry_at"] += 86_400_000
    markets[3]["option_type"] = "STRADDLE"
    build = resolve_option_contracts("paradex", _listing(markets))
    reasons = build.unresolved.set_index("venue_symbol")["reason"]
    assert reasons[markets[0]["symbol"]] == "name_fields_mismatch: strike"
    assert reasons[markets[1]["symbol"]] == "name_fields_mismatch: option_type"
    assert reasons[markets[2]["symbol"]] == "name_fields_mismatch: expiry"
    assert reasons[markets[3]["symbol"]].startswith("missing or invalid structured field(s): option_type")
    assert len(build.contracts) == 6


def test_status_and_registry_update_through_expiry():
    later = _listing(as_of=pd.Timestamp("2026-10-24"))
    status = later.set_index("venue_symbol")["status"]
    assert status["BTC-USD-23OCT26-82000-C"] == "expired" and status["BTC-USD-27NOV26-88000-C"] == "active"
    assert _listing(as_of=pd.Timestamp("2026-01-01"))["status"].eq("scheduled").all()
    # Paradex drops expired contracts from the listing: the registry marks them expired, keeps them.
    registry = update_contract_registry(pd.DataFrame(), _resolved().contracts, AS_OF)
    alive = [m for m in _fixture("option_markets")["results"] if "23OCT26" not in m["symbol"]]
    fresh = resolve_option_contracts("paradex", _listing(alive, pd.Timestamp("2026-10-24"))).contracts
    merged = update_contract_registry(registry, fresh, pd.Timestamp("2026-10-24")).set_index("venue_symbol")
    assert merged.loc["BTC-USD-23OCT26-82000-C", "status"] == "expired" and len(merged) == 10


# -- chain snapshot --------------------------------------------------------------------------------


def _b76(spot, strike, tau, vol, rate, call):
    forward = spot * math.exp(rate * tau)
    sd = vol * math.sqrt(tau)
    d1 = (math.log(forward / strike) + sd * sd / 2) / sd
    d2 = d1 - sd
    return forward * N(d1) - strike * N(d2) if call else strike * N(-d2) - forward * N(-d1)


def _chain():
    return parse_option_summary(_fixture("option_summary")["results"], _resolved().contracts, AS_OF)


def test_chain_rows_and_units():
    chain = _chain()
    assert list(chain.columns[: len(OPTION_QUOTE_COLUMNS)]) == OPTION_QUOTE_COLUMNS
    assert len(chain) == 10 and chain["ts_snapshot"].eq(AS_OF).all() and chain["contract_id"].notna().all()
    row = chain.set_index("venue_symbol").loc["BTC-USD-23OCT26-88000-C"]
    assert row["contract_id"] == "BTC-USD-20261023T0800-88000-C"
    assert (row["greeks_reference"], row["underlying_kind"], row["greeks_source"]) == ("spot", "spot", "venue")
    assert 0 < row["iv_bid"] < row["iv_mark"] < row["iv_ask"] < 2  # fractions, not percent
    assert row["index_price"] == row["underlying_price"] and row["discount_factor"] == 1.0
    tau = (pd.Timestamp("2026-10-23 08:00") - row["ts"]) / pd.Timedelta(days=YEAR_DAYS)
    assert row["forward_price"] == pytest.approx(row["underlying_price"] * math.exp(row["forward_rate"] * tau))
    assert pd.isna(row["last"]) and row["last_raw"] == ""  # never traded
    assert row["mark_raw"] == "33.93396002" and row["iv_mark_raw"] == "0.37361458"
    traded = chain.set_index("venue_symbol").loc["BTC-USD-27NOV26-88000-C"]
    assert traded["volume_24h"] == pytest.approx(traded["volume_24h_raw"] / traded["underlying_price"])
    one_sided = chain[chain["iv_bid"].isna()]
    assert not one_sided.empty and one_sided["iv_mark"].notna().all()


def test_greeks_recompute_black76_on_the_spot_convention():
    contracts = _resolved().contracts[["venue_symbol", "strike", "option_type", "expiry"]]
    chain = _chain().merge(contracts, on="venue_symbol")
    checked = 0
    for r in chain.itertuples():
        tau = (r.expiry - r.ts) / pd.Timedelta(days=YEAR_DAYS)
        call, spot = r.option_type == "C", r.underlying_price
        args = (r.strike, tau, r.iv_mark, r.forward_rate, call)
        value = _b76(spot, *args)
        if value < 1e-6 * spot:
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


# -- settlements, trades ---------------------------------------------------------------------------


def test_settlements_are_the_spot_twap_and_payoffs_follow():
    seen = pd.Timestamp("2026-10-18 19:05")
    contracts = contract_settlements(_fixture("option_settlements"), seen)
    for c in contracts.itertuples():
        name = parse_option_name(c.venue_symbol)
        assert c.settlement_payoff_raw == pytest.approx(payoff(name["option_type"], name["strike"], c.settlement_price))
    rows = settlement_rows(contracts)
    assert list(rows.columns) == SETTLEMENT_COLUMNS and len(rows) == 4  # two BTC contracts, one fixing
    indexed = rows.set_index(["settlement_index_id", "ts"])
    btc = indexed.loc[("paradex:BTC", pd.Timestamp("2026-10-18 08:00"))]
    assert btc["settlement_price"] == pytest.approx(81587.70086941296) and btc["venue_ref"] == "BTC-18OCT26"
    assert ("paradex:XAU", pd.Timestamp("2026-10-16 17:30")) in indexed.index
    assert rows["first_seen_at"].eq(seen).all() and rows["source_endpoint"].eq("markets/settlement-price").all()
    assert rows["method"].eq("twap_30min").all() and rows["method_source"].eq("venue_doc").all()  # the table's
    assert contract_settlements([{"error": "MARKET_NOT_FOUND", "message": "Market not found"}], seen).empty


def test_market_history_settled_events():
    events = parse_market_history(_fixture("market_history")["results"], pd.Timestamp("2026-10-18 19:05"))
    assert len(events) == 3 and events["settlement_source"].eq("markets/history").all()
    for e in events.itertuples():
        name = parse_option_name(e.venue_symbol)
        assert e.settlement_payoff_raw == pytest.approx(payoff(name["option_type"], name["strike"], e.settlement_price))
    assert events["settled_at"].dt.floor("h").eq(pd.Timestamp("2026-10-18 08:00")).all()
    assert events["expiry"].isna().all()  # joined from the registry by the caller


def test_trades_drop_trader_ids_and_undated_names():
    ids = dict(_resolved().contracts[["venue_symbol", "contract_id"]].itertuples(index=False))
    records = _fixture("option_trades")["results"] + [
        {"id": "x", "market": "BTC-USD-113000-C", "side": "BUY", "size": "1", "price": "1",
         "created_at": 1767225873164, "trade_type": "FILL", "block_id": ""}]
    trades = parse_option_trades(records, ids)
    assert list(trades.columns[: len(OPTION_TRADE_COLUMNS)]) == OPTION_TRADE_COLUMNS and len(trades) == 2
    assert trades["ts"].is_monotonic_increasing and set(trades["side"]) <= {"buy", "sell"}
    assert trades["trade_type"].eq("BLOCK_TRADE").all() and trades["raw_block_id"].notna().all()
    assert trades["contract_id"].eq("BTC-USD-20261127T0800-88000-C").all()
    assert not {"wallet", "subaccount_id", "tx_hash"} & set(trades.columns)


# -- venue methods (fake client) -------------------------------------------------------------------


class FakeParadexOptions:
    """Answers like Paradex for the options endpoints; trades come in pages of ``page`` behind a cursor and
    every contract of a (base, expiry date) settles on the same TWAP."""

    def __init__(self, trades=None, page=2):
        self.trades = trades if trades is not None else _fixture("option_trades")["results"]
        self.page = page
        self.calls = []
        self.settled = {}
        for p in _fixture("option_settlements"):
            name = parse_option_name(p["symbol"])
            self.settled[(name["base"], name["expiry_date"])] = p

    def get(self, path, params=None):
        params = dict(params or {})
        self.calls.append((path, params))
        if path == "markets":
            return _fixture("option_markets")
        if path == "markets/summary":
            assert params == {"market": "ALL"}
            return _fixture("option_summary")
        if path == "markets/history":
            return _fixture("market_history")
        if path == "markets/settlement-price":
            name = parse_option_name(params["market"])
            known = self.settled.get((name["base"], name["expiry_date"])) if name else None
            if known is None:
                raise HttpError(404, path, '{"error":"MARKET_NOT_SETTLED"}')
            twap = float(known["underlying_twap_price"])
            return {**known, "symbol": params["market"],
                    "settlement_price": str(payoff(name["option_type"], name["strike"], twap))}
        if path == "trades":
            offset = int(params.get("cursor") or 0)
            hits = [t for t in self.trades if params["start_at"] <= t["created_at"] <= params["end_at"]]
            hits.sort(key=lambda t: -t["created_at"])
            more = offset + self.page < len(hits)
            return {"results": hits[offset: offset + self.page],
                    "next": str(offset + self.page) if more else None, "prev": None}
        if path == "markets/klines":
            return _fixture(f"option_{params['price_kind']}_klines")
        raise AssertionError(f"unexpected path {path}")


def _trade(market, ts, trade_type="FILL"):
    return {"id": f"{market}-{ts}", "market": market, "side": "SELL", "size": "1", "price": "10",
            "created_at": int(pd.Timestamp(ts).value // 10**6), "trade_type": trade_type, "block_id": ""}


def test_paradex_is_an_options_venue():
    venue = ParadexVenue(FakeParadexOptions())
    assert isinstance(venue, OptionsVenue)
    assert {Dataset.OPTION_CHAIN, Dataset.SETTLEMENTS, Dataset.FUNDING} <= venue.capabilities


def test_contracts_and_chain_through_the_venue():
    client = FakeParadexOptions()
    venue = ParadexVenue(client)
    assert set(venue.option_contracts("BTC")["venue_base"]) == {"BTC"}
    expired = venue.option_contracts("BTC", expired=True)  # Paradex lists none: an empty listing
    assert expired.empty and list(expired.columns) == OPTION_SNAPSHOT_COLUMNS
    chain = venue.option_chain("BTC", ts_snapshot=AS_OF)
    assert len(chain) == 6 and chain["contract_id"].str.startswith("BTC-USD-").all()
    assert [path for path, _ in client.calls].count("markets") == 1  # cached since option_contracts
    week = venue.option_chain("BTC", pd.Timestamp("2026-10-23 08:00"), ts_snapshot=AS_OF)
    assert len(week) == 5 and week["contract_id"].str.contains("20261023T0800").all()
    assert [path for path, _ in client.calls].count("markets") == 1


def test_a_pass_fetches_the_listing_and_the_summary_once():
    # qmd-snapshot asks per underlying: option_contracts(u) then option_chain(u), BTC then ETH.
    client = FakeParadexOptions()
    venue = ParadexVenue(client)
    for underlying in ("BTC", "ETH"):
        venue.option_contracts(underlying)
        venue.option_chain(underlying)
    paths = [path for path, _ in client.calls]
    assert paths.count("markets") == 1 and paths.count("markets/summary") == 1
    venue.listing_ttl = venue.summary_ttl = pd.Timedelta(0)  # an expired cache is fetched again
    venue.option_contracts("BTC")
    venue.option_chain("BTC")
    paths = [path for path, _ in client.calls]
    assert paths.count("markets") == 2 and paths.count("markets/summary") == 2


def test_chain_refreshes_the_listing_once_on_an_unknown_symbol(monkeypatch):
    # A frozen clock: the forced refresh must not depend on two readings differing (it did, and the test
    # failed about once in eight runs on Windows, whose clock often reads the same twice).
    monkeypatch.setattr("quant_marketdata.venues.paradex_options._now", lambda: pd.Timestamp("2026-10-18 13:00"))
    client = FakeParadexOptions()
    venue = ParadexVenue(client)
    venue.option_contracts()
    venue._listing = venue._listing.iloc[1:]  # a strike listed since
    assert len(venue.option_chain("BTC", ts_snapshot=AS_OF)) == 6
    assert [path for path, _ in client.calls].count("markets") == 2


def test_option_trades_follow_the_cursor():
    trades = [_trade("BTC-USD-23OCT26-82000-C", f"2026-10-18 0{h}:00") for h in range(5)]
    trades.append(_trade("BTC-USD-113000-C", "2026-10-18 06:00"))
    client = FakeParadexOptions(trades=trades, page=2)
    venue = ParadexVenue(client)
    venue.option_chain("BTC", ts_snapshot=AS_OF)  # resolves the listing
    frame = venue.option_trades("BTC", pd.Timestamp("2026-10-18"), pd.Timestamp("2026-10-18 12:00"))
    assert len(frame) == 5 and frame["ts"].is_monotonic_increasing
    assert frame["contract_id"].eq("BTC-USD-20261023T0800-82000-C").all()
    pages = [p for path, p in client.calls if path == "trades"]
    assert len(pages) == 3 and pages[0]["base_asset"] == "BTC" and pages[0]["page_size"] == 1000


def test_mark_candles_and_settlements_through_the_venue():
    client = FakeParadexOptions(trades=[
        _trade("BTC-USD-18OCT26-81000-C", "2026-10-18 08:00:10", "SETTLE_MARKET"),
        _trade("BTC-USD-18OCT26-82000-P", "2026-10-18 08:00:12", "SETTLE_MARKET"),
        _trade("BTC-USD-17OCT26-82000-C", "2026-10-17 08:00:05", "SETTLE_MARKET"),  # unsettled in the fake
    ])
    venue = ParadexVenue(client)
    venue.option_chain("BTC", ts_snapshot=AS_OF)
    mark = venue.option_mark_candles("BTC-USD-27NOV26-88000-C", "1h", pd.Timestamp("2026-10-18 13:00"),
                                     pd.Timestamp("2026-10-18 15:00"))
    assert list(mark.columns) == OPTION_MARK_CANDLE_COLUMNS and len(mark) == 3
    assert mark["contract_id"].eq("BTC-USD-20261127T0800-88000-C").all() and mark["volume_quote"].isna().all()
    assert (mark["price_type"] == "mark").all() and client.calls[-1][1]["price_kind"] == "mark"
    underlying = venue.option_mark_candles("BTC-USD-27NOV26-88000-C", "1h", pd.Timestamp("2026-10-18 13:00"),
                                           pd.Timestamp("2026-10-18 15:00"), price="underlying")
    assert underlying["close"].iloc[0] > 50_000 > mark["close"].iloc[0]  # spot vs option mark
    with pytest.raises(ValueError, match="mark"):
        venue.option_mark_candles("BTC-USD-27NOV26-88000-C", "1h", AS_OF, AS_OF, price="last")
    rows = venue.settlements("paradex:BTC", pd.Timestamp("2026-10-17"), pd.Timestamp("2026-10-18 12:00"))
    assert rows["ts"].tolist() == [pd.Timestamp("2026-10-18 08:00")]  # 17OCT26 answered 404
    assert len([p for path, p in client.calls if path == "markets/settlement-price"]) == 2  # one per date
    recent = venue.settlements("ETH")  # from the SETTLED events of markets/history
    assert recent["ts"].tolist() == [pd.Timestamp("2026-10-18 08:00")]
    assert recent["settlement_price"].iloc[0] == pytest.approx(2406.5910561466653)
    # With a start (the daily task's last stored fixing), an expiry no open position settled through a
    # trade is still found in markets/history: no ETH trade exists in this fake.
    since = venue.settlements("ETH", pd.Timestamp("2026-10-17"), pd.Timestamp("2026-10-18 12:00"))
    assert since["ts"].tolist() == [pd.Timestamp("2026-10-18 08:00")]


# -- passes over a ContractStore -------------------------------------------------------------------


@pytest.fixture
def frozen(monkeypatch):
    """Freeze the venue clock (statuses and seen-at stamps) at ``AS_OF``."""
    import quant_marketdata.venues.paradex_options as options
    import quant_marketdata.venues.paradex_snapshot as passes

    clock = {"now": AS_OF}
    monkeypatch.setattr(options, "_now", lambda: clock["now"])
    monkeypatch.setattr(passes, "_now", lambda: clock["now"])
    return clock


def test_snapshot_pass_writes_registry_chains_and_heartbeat(tmp_path, frozen):
    store = ContractStore.at(tmp_path)
    report = snapshot_pass(ParadexVenue(FakeParadexOptions()), store, ts_snapshot=AS_OF)
    assert report["ok"] and report["contracts"] == 10 and report["unresolved"] == 0
    assert report["chains"] == {"BTC-USD": 6, "ETH-USD": 0} and report["universe_changes"] > 0
    registry = store.read(store.option_registry_path("paradex"))
    assert len(registry) == 10 and registry["first_seen_at"].eq(AS_OF).all()
    later = AS_OF + pd.Timedelta(minutes=15)
    snapshot_pass(ParadexVenue(FakeParadexOptions()), store, ts_snapshot=later)
    chains = store.read_snapshots("paradex", Dataset.OPTION_CHAIN, "BTC-USD")
    assert len(chains) == 12 and set(chains["ts_snapshot"]) == {AS_OF, later}  # append-only
    assert json.loads(store.heartbeat_path("paradex").read_text())["ok"] is True


def test_snapshot_pass_reports_a_failing_step_and_runs_the_others(tmp_path, frozen):
    class Broken(FakeParadexOptions):
        def get(self, path, params=None):
            if path == "markets/summary":
                raise HttpError(503, path, "down")
            return super().get(path, params)

    report = snapshot_pass(ParadexVenue(Broken()), ContractStore.at(tmp_path), ts_snapshot=AS_OF)
    assert not report["ok"] and report["contracts"] == 10 and set(report["errors"]) == {"BTC-USD", "ETH-USD"}


def test_settle_expired_fills_the_registry_and_the_settlement_file(tmp_path, frozen):
    store = ContractStore.at(tmp_path)
    markets = _fixture("option_markets")["results"]
    expiring = {**copy.deepcopy(markets[0]), "symbol": "BTC-USD-18OCT26-81000-C", "strike_price": "81000",
                "expiry_at": int(pd.Timestamp("2026-10-18 08:00").value // 10**6)}

    class WithExpiring(FakeParadexOptions):
        def __init__(self, listing):
            super().__init__()
            self.listing = listing

        def get(self, path, params=None):
            return {"results": self.listing} if path == "markets" else super().get(path, params)

    frozen["now"] = pd.Timestamp("2026-10-17 12:00")
    snapshot_pass(ParadexVenue(WithExpiring(markets + [expiring])), store, underlyings=())
    frozen["now"] = AS_OF  # the contract left the listing at its expiry
    snapshot_pass(ParadexVenue(WithExpiring(markets)), store, underlyings=())
    report = settle_expired(ParadexVenue(FakeParadexOptions()), store)
    assert (report["fixings"], report["contracts_settled"]) == (1, 1)
    registry = store.read(store.option_registry_path("paradex")).set_index("venue_symbol")
    row = registry.loc["BTC-USD-18OCT26-81000-C"]
    assert row["status"] == "expired" and row["settlement_price"] == pytest.approx(81587.70086941296)
    assert row["settlement_source"] == "markets/settlement-price" and row["settlement_seen_at"] == AS_OF
    fixings = store.read(store.settlements_path("paradex", "paradex:BTC"))
    assert fixings["ts"].tolist() == [pd.Timestamp("2026-10-18 08:00")] and fixings["method"].iloc[0] == "twap_30min"
    assert settle_expired(ParadexVenue(FakeParadexOptions()), store)["fixings"] == 0  # nothing left to do


def test_mark_candles_per_contract_and_one_underlying_series(tmp_path, frozen):
    store = ContractStore.at(tmp_path)
    frozen["now"] = pd.Timestamp("2026-10-18 16:00")
    report = download_mark_candles(ParadexVenue(FakeParadexOptions()), store, "BTC")
    assert report["contracts"] == 6 and not report["errors"]
    candles = store.read_monthly("paradex", Dataset.OPTION_MARK_CANDLES, "BTC-USD", resolution="1h")
    marks = candles[candles["price_type"] == "mark"]
    assert marks["venue_symbol"].nunique() == 6 and marks["contract_id"].notna().all()
    spot = candles[candles["price_type"] == "underlying"]
    assert set(spot["venue_symbol"]) == {"BTC-USD"} and spot["contract_id"].isna().all() and len(spot) == 3
    download_mark_candles(ParadexVenue(FakeParadexOptions()), store, "BTC")
    again = store.read_monthly("paradex", Dataset.OPTION_MARK_CANDLES, "BTC-USD", resolution="1h")
    assert len(again) == len(candles)  # merged on (venue_symbol, price_type, ts)


@pytest.mark.live
def test_live_paradex_options_pass(tmp_path):
    store = ContractStore.at(tmp_path)
    venue = ParadexVenue(rate=20)
    report = snapshot_pass(venue, store, underlyings=("BTC",))
    assert report["ok"] and report["contracts"] > 1000 and report["unresolved"] == 0
    chain = store.read_snapshots("paradex", Dataset.OPTION_CHAIN, "BTC-USD")
    assert len(chain) > 100 and chain["contract_id"].notna().all() and chain["greeks_source"].eq("venue").all()
    assert chain["iv_mark"].between(0.01, 5).all() and chain["underlying_price"].nunique() == 1
    fixings = venue.settlements("BTC")
    assert not fixings.empty and fixings["method"].eq("twap_30min").all()
    assert fixings["settlement_price"].between(1_000, 1_000_000).all()
    live = venue.resolved_contracts("BTC").sort_values("expiry").iloc[-1]
    end = pd.Timestamp.now(tz="UTC").tz_localize(None).floor("h")
    candles = venue.option_mark_candles(live["venue_symbol"], "1h", end - pd.Timedelta(hours=6), end)
    assert 5 <= len(candles) <= 7 and candles["contract_id"].eq(live["contract_id"]).all()
