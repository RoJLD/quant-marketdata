"""qmd-snapshot passes and the daily task on recorded payloads (no network), then the store readers."""

from __future__ import annotations

import json

import pandas as pd
import pytest
from test_venues_deribit import FakeClient as DeribitClient
from test_venues_derive import FakeClient as DeriveClient
from test_venues_kalshi import FakeClient as KalshiClient
from test_venues_paradex_options import FakeParadexOptions

from quant_marketdata.venues.contracts import (
    ContractStore,
    load_chain,
    load_ladder,
    load_option_registry,
    load_settlements,
    settlement_comparison,
)
from quant_marketdata.venues.deribit import DeribitVenue
from quant_marketdata.venues.derive import DeriveVenue
from quant_marketdata.venues.kalshi import KalshiVenue
from quant_marketdata.venues.paradex import ParadexVenue
from quant_marketdata.venues.schema import Dataset
from quant_marketdata.venues.snapshot import (
    daily_main,
    daily_task,
    main,
    snapshot_pass,
    venues_with,
)

FACTORIES = {
    "derive": lambda: DeriveVenue(DeriveClient(), clock=lambda: pd.Timestamp("2026-10-18 09:20")),
    "deribit": lambda: DeribitVenue(DeribitClient()),
    "kalshi": lambda: KalshiVenue(KalshiClient()),
}
PASS_1 = pd.Timestamp("2026-10-18 09:15")
PASS_2 = pd.Timestamp("2026-10-18 09:30")


def _pass(root, ts, venues=("derive", "deribit", "kalshi")):
    return snapshot_pass(root=root, venues=list(venues), underlyings=["BTC"], series=["KXBTCD"],
                         factories=FACTORIES, ts_snapshot=ts)


def test_option_and_binary_venues_are_discovered_by_capability():
    assert {"derive", "deribit"} <= set(venues_with(Dataset.OPTION_CHAIN))
    assert venues_with(Dataset.BINARY_QUOTES) == ["kalshi"]
    assert "lighter" not in venues_with(Dataset.OPTION_CHAIN)


def test_snapshot_pass_writes_registries_parts_and_heartbeats(tmp_path):
    runs = _pass(tmp_path, PASS_1)
    assert all(run.ok for run in runs), [s for r in runs for s in r.steps if s.error]
    store = ContractStore.at(tmp_path)
    chain = store.read_snapshots("derive", Dataset.OPTION_CHAIN, "BTC-USD")
    assert len(chain) == 4 and chain["contract_id"].notna().all()
    assert set(chain["ts_snapshot"]) == {PASS_1}
    assert "BTC-USD-20261023T0800-82000-C" in set(chain["contract_id"])
    deribit = store.read_snapshots("deribit", Dataset.OPTION_CHAIN, "BTC-USD")
    assert len(deribit) == 3 and deribit["contract_id"].notna().all()
    quotes = store.read_snapshots("kalshi", Dataset.BINARY_QUOTES, "KXBTCD")
    assert len(quotes) == 9 and quotes["contract_id"].notna().all()
    registry = store.read(store.option_registry_path("derive"))
    assert set(registry["first_seen_at"]) == {PASS_1}
    assert store.binary_registry_path("kalshi", "KXBTCD", "2026-10").exists()
    heartbeat = json.loads(store.heartbeat_path("kalshi").read_text())
    assert heartbeat["ok"] and heartbeat["kind"] == "snapshot" and heartbeat["steps"][0]["rows"] == 9
    changes = store.read(store.universe_changes_path("derive"))
    assert "new_underlying" in set(changes["change"])


def test_passes_append_and_a_failing_venue_does_not_stop_the_others(tmp_path):
    _pass(tmp_path, PASS_1)
    broken = {**FACTORIES, "deribit": lambda: DeribitVenue(_Broken())}
    runs = snapshot_pass(root=tmp_path, venues=["deribit", "derive"], underlyings=["BTC"], series=[],
                         factories=broken, ts_snapshot=PASS_2)
    by_venue = {r.venue: r for r in runs}
    assert not by_venue["deribit"].ok and by_venue["derive"].ok
    assert "RuntimeError" in by_venue["deribit"].steps[0].error
    store = ContractStore.at(tmp_path)
    chain = store.read_snapshots("derive", Dataset.OPTION_CHAIN, "BTC-USD")
    assert set(chain["ts_snapshot"]) == {PASS_1, PASS_2} and len(chain) == 8
    assert not json.loads(store.heartbeat_path("deribit").read_text())["ok"]


class _Broken:
    def get(self, path, params=None):
        raise RuntimeError("venue down")


def test_readers_after_a_pass(tmp_path):
    _pass(tmp_path, PASS_1)
    _pass(tmp_path, PASS_2, venues=("derive",))
    chain = load_chain("BTC-USD", at=pd.Timestamp("2026-10-18 09:20"), root=tmp_path)
    assert set(chain["venue"]) == {"derive", "deribit"} and set(chain["ts_snapshot"]) == {PASS_1}
    assert {"expiry", "strike", "option_type", "settlement_index_id"} <= set(chain.columns)
    latest = load_chain("BTC-USD", root=tmp_path, venues=["derive"])
    assert set(latest["ts_snapshot"]) == {PASS_2}
    ladder = load_ladder("KXBTCD", "KXBTCD-26OCT1904", at=PASS_2, root=tmp_path)
    assert len(ladder) == 3 and set(ladder["quote_source"]) == {"snapshot"}
    assert ladder["k_low"].is_monotonic_increasing and ladder["settlement_index_id"].eq("cfb:BRTI").all()
    assert load_option_registry(["deribit"], root=tmp_path)["venue"].eq("deribit").all()


def test_daily_task_settlements_and_the_0800_index_gap(tmp_path):
    as_of = pd.Timestamp("2026-10-18 21:00")  # after the recorded trades
    _pass(tmp_path, PASS_1, venues=("derive",))
    runs = daily_task(root=tmp_path, venues=["derive", "deribit", "kalshi"], underlyings=["BTC"],
                      series=["KXBTCD"], factories=FACTORIES, as_of=as_of)
    assert all(run.ok for run in runs), [s for r in runs for s in r.steps if s.error]
    store = ContractStore.at(tmp_path)
    registry = store.read(store.option_registry_path("derive")).set_index("venue_symbol")
    expired = registry.loc["BTC-20261018-95000-P"]
    assert expired["status"] == "expired" and expired["settlement_price"] == pytest.approx(81585.94, abs=0.01)
    assert expired["settlement_seen_at"] == as_of
    trades = store.read_monthly("derive", Dataset.OPTION_TRADES, "BTC-USD")
    assert len(trades) == 3 and trades["trade_id"].is_unique
    candles = store.read_monthly("kalshi", Dataset.BINARY_CANDLES, "KXBTCD", resolution="1m")
    assert not candles.empty and set(candles["period"]) == {"1m"}  # fixture candles: another event

    settlements = load_settlements(root=tmp_path)
    assert set(settlements["settlement_index_id"]) == {"derive:BTC-USD", "deribit:btc_usd", "cfb:BRTI"}
    gaps = settlement_comparison(settlements).loc[pd.Timestamp("2026-10-18 08:00")]
    # Spec §3.9: Kalshi 81,568.22, Deribit 81,583.27 (+1.8 bp), Derive 81,585.94 (+2.2 bp).
    assert gaps["cfb:BRTI"] == pytest.approx(81568.22)
    assert gaps["gap_bp:deribit:btc_usd"] == pytest.approx(1.845, abs=0.01)
    assert gaps["gap_bp:derive:BTC-USD"] == pytest.approx(2.172, abs=0.01)

    # A second run keeps the first sighting of every fixing.
    later = pd.Timestamp("2026-10-19 07:30")
    daily_task(root=tmp_path, venues=["deribit"], underlyings=["BTC"], series=[], factories=FACTORIES,
               as_of=later, trades=False)
    again = load_settlements(["deribit:btc_usd"], root=tmp_path)
    assert len(again) == 5 and again["first_seen_at"].max() < later


PARADEX_AS_OF = pd.Timestamp("2026-10-18 16:00")  # after the recorded mark candles (13:00-15:00)


@pytest.fixture
def paradex_clock(monkeypatch):
    import quant_marketdata.venues.paradex_options as options
    import quant_marketdata.venues.paradex_snapshot as passes

    monkeypatch.setattr(options, "_now", lambda: PARADEX_AS_OF)
    monkeypatch.setattr(passes, "_now", lambda: PARADEX_AS_OF)


class _NoKlines(FakeParadexOptions):
    def get(self, path, params=None):
        if path == "markets/klines":
            raise RuntimeError("klines down")
        return super().get(path, params)


def _paradex_daily(root, client=FakeParadexOptions, **kwargs):
    runs = daily_task(root=root, venues=["paradex"], underlyings=["BTC"], series=[], as_of=PARADEX_AS_OF,
                      factories={"paradex": lambda: ParadexVenue(client())}, **kwargs)
    return runs[0], [s for s in runs[0].steps if s.step == "option_mark_candles"]


def test_daily_task_records_paradex_mark_candles(tmp_path, paradex_clock):
    run, candles = _paradex_daily(tmp_path)
    assert run.ok, [s for s in run.steps if s.error]
    assert [s.step for s in run.steps] == ["option_registry", "settlements", "option_trades", "option_mark_candles"]
    assert (candles[0].key, candles[0].rows, candles[0].deferred) == ("BTC-USD", 21, 0)  # 6 contracts + spot
    store = ContractStore.at(tmp_path)
    stored = store.read_monthly("paradex", Dataset.OPTION_MARK_CANDLES, "BTC-USD", resolution="1h")
    assert stored["venue_symbol"].nunique() == 7 and stored.loc[stored["price_type"] == "mark", "contract_id"].notna().all()
    heartbeat = json.loads(store.heartbeat_path("paradex").read_text())
    assert heartbeat["ok"] and heartbeat["steps"][-1]["step"] == "option_mark_candles"
    assert _paradex_daily(tmp_path, mark_candles=False)[1] == []


def test_daily_mark_candles_are_bounded_and_failure_isolated(tmp_path, paradex_clock):
    run, candles = _paradex_daily(tmp_path, mark_candles_budget=pd.Timedelta(0))
    assert run.ok and (candles[0].rows, candles[0].deferred) == (0, 7)  # all left to the next run
    run, candles = _paradex_daily(tmp_path, client=_NoKlines)
    assert not run.ok and all(s.error is None for s in run.steps if s.step != "option_mark_candles")
    assert candles[0].error.startswith("7 of 7 series failed, e.g. ") and "klines down" in candles[0].error
    assert not json.loads(ContractStore.at(tmp_path).heartbeat_path("paradex").read_text())["ok"]


def test_daily_command_line_passes_the_mark_candle_options(tmp_path, monkeypatch):
    import quant_marketdata.venues.snapshot as snapshot

    seen = {}
    monkeypatch.setattr(snapshot, "daily_task", lambda **kwargs: seen.update(kwargs) or [])
    assert daily_main(["--root", str(tmp_path), "--mark-candles-minutes", "1.5", "--mark-candles-rate", "5"]) == 0
    assert seen["mark_candles"] and seen["mark_candles_budget"] == pd.Timedelta(seconds=90)
    assert seen["mark_candles_rate"] == 5 and seen["rate"] == 2
    daily_main(["--root", str(tmp_path), "--no-mark-candles"])
    assert not seen["mark_candles"] and seen["mark_candles_budget"] == pd.Timedelta(minutes=3)


def test_command_line_reports_failures(tmp_path, monkeypatch, capsys):
    import quant_marketdata.venues.snapshot as snapshot

    def fake_pass(**kwargs):
        run = snapshot.VenueRun("derive", [snapshot.StepResult("derive", "option_chain", "BTC", error="X: y")])
        return [run]

    monkeypatch.setattr(snapshot, "snapshot_pass", fake_pass)
    assert main(["--root", str(tmp_path)]) == 1
    assert "FAILED derive option_chain BTC: X: y" in capsys.readouterr().out


@pytest.mark.live
def test_live_snapshot_pass_btc(tmp_path):
    runs = snapshot_pass(root=tmp_path, venues=["derive", "deribit", "kalshi"], underlyings=["BTC"],
                         series=["KXBTCD"])
    assert all(run.ok for run in runs), [s for r in runs for s in r.steps if s.error]
    chain = load_chain("BTC-USD", root=tmp_path)
    assert {"derive", "deribit"} <= set(chain["venue"]) and chain["contract_id"].notna().mean() > 0.99
