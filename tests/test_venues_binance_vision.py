"""Binance Vision metrics archives: checksum verification, parsing, store download (recorded CSV, no network)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pandas as pd
import pytest

from quant_marketdata.venues import Dataset, VenueStore
from quant_marketdata.venues import binance_vision as vision
from quant_marketdata.venues.binance_vision import (
    RATIO_COLUMNS,
    ChecksumError,
    archive_days,
    download_metrics,
    fetch_archive,
    metrics_path,
    parse_checksum,
    parse_metrics,
    read_archive,
)
from quant_marketdata.venues.http import HttpError, JsonClient
from quant_marketdata.venues.schema import (
    ARCHIVE_DATASETS,
    CONTRACT_DATASETS,
    OPEN_INTEREST_HISTORY_COLUMNS,
)

FIXTURES = Path(__file__).parent / "fixtures" / "binance_vision"
# Header + the first six 5-minute rows of BTCUSDT-metrics-2026-01-01.zip (recorded 2026-10-24).
HEAD = (FIXTURES / "BTCUSDT-metrics-2026-01-01-head.csv").read_bytes()
# That archive's last row (labelled 23:55: the REST stamp of the snapshot is 24:00).
LAST = (b"2026-01-01 23:55:00,BTCUSDT,93941.0260000000000000,8341953714.6974000000000000,"
        b"2.54877591,2.49442900,2.27343239,0.65617000\n")


def _day_csv(day: str, symbol: str = "BTCUSDT") -> bytes:
    """The recorded rows moved to ``day`` (same values, same 5-minute labels), last row included."""
    return (HEAD + LAST).replace(b"2026-01-01", day.encode()).replace(b"BTCUSDT", symbol.encode())


def _zip(csv: bytes, name: str = "BTCUSDT-metrics-2026-01-01.csv") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(name, csv)
    return buffer.getvalue()


def _checksum(blob: bytes, name: str = "BTCUSDT-metrics-2026-01-01.zip") -> bytes:
    return f"{hashlib.sha256(blob).hexdigest()}  {name}\n".encode()


class FakeVision:
    """Serves archives and their .CHECKSUM files by path; anything else is a 404."""

    def __init__(self, files=None):
        self.files = dict(files or {})
        self.calls = []

    def publish(self, symbol, day, csv=None, *, checksum=True):
        path = metrics_path(symbol, day)
        blob = _zip(csv if csv is not None else _day_csv(day, symbol))
        self.files[path] = blob
        if checksum:
            self.files[f"{path}.CHECKSUM"] = _checksum(blob)

    def get_bytes(self, path, params=None):
        self.calls.append(path)
        if path not in self.files:
            raise HttpError(404, path, "<Error><Code>NoSuchKey</Code></Error>")
        return self.files[path]


# -- archives ------------------------------------------------------------------------------------------


def test_metrics_path_is_the_daily_um_layout():
    assert metrics_path("BTCUSDT", "2026-01-01") == \
        "data/futures/um/daily/metrics/BTCUSDT/BTCUSDT-metrics-2026-01-01.zip"


def test_read_archive_verifies_the_sha256_before_reading():
    blob = _zip(HEAD)
    assert read_archive(blob, _checksum(blob).decode()) == HEAD
    tampered = blob[:-30] + bytes(30)
    with pytest.raises(ChecksumError, match="does not match"):
        read_archive(tampered, _checksum(blob).decode())
    with pytest.raises(ChecksumError, match="not a SHA-256"):
        read_archive(blob, "<html>404</html>")


def test_parse_checksum_takes_the_digest_of_the_sha256sum_line():
    digest = "b57d5f67f3ffcad415f0f7908d4434e9a1328092e3166d5590673f4d615fcd06"
    assert parse_checksum(f"{digest.upper()}  BTCUSDT-metrics-2026-01-01.zip") == digest


def test_fetch_archive_none_when_unpublished_error_without_checksum():
    fake = FakeVision()
    assert fetch_archive(fake, metrics_path("BTCUSDT", "2026-10-24")) is None
    fake.publish("BTCUSDT", "2026-01-01", checksum=False)
    with pytest.raises(ChecksumError, match="no .CHECKSUM"):
        fetch_archive(fake, metrics_path("BTCUSDT", "2026-01-01"))


def test_json_client_get_bytes_returns_the_raw_body():
    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    client = JsonClient("https://data.example", rate=1000, opener=lambda request, timeout: _Response(b"PK\x03\x04"))
    assert client.get_bytes("data/x.zip") == b"PK\x03\x04"


# -- parsing ---------------------------------------------------------------------------------------------


def test_parse_metrics_one_side_open_interest_in_base_and_usd():
    frame = parse_metrics(HEAD, "BTCUSDT")
    assert list(frame.columns) == OPEN_INTEREST_HISTORY_COLUMNS + RATIO_COLUMNS + ["create_time_raw"]
    assert frame.index.name == "ts" and frame.index.is_monotonic_increasing and len(frame) == 6
    # Stamped like the REST openInterestHist: the row labelled 00:00 is the 00:05 snapshot.
    assert frame.index[0] == pd.Timestamp("2026-01-01 00:05") and frame.index[-1] == pd.Timestamp("2026-01-01 00:30")
    assert frame["create_time_raw"].iloc[0] == "2026-01-01 00:00:00"
    assert frame["open_interest_base"].iloc[0] == pytest.approx(94262.03)
    assert frame["open_interest_usd"].iloc[0] == pytest.approx(8264112415.551)
    assert (frame["open_interest_usd"] / frame["open_interest_base"]).between(87_000, 89_000).all()  # BTC price
    assert frame["oi_convention"].eq("one_side").all() and frame["source"].eq("binance_vision").all()
    assert frame["sum_taker_long_short_vol_ratio"].iloc[1] == pytest.approx(2.371866)


def test_parse_metrics_rejects_another_symbol_or_header():
    with pytest.raises(ValueError, match="expected ETHUSDT"):
        parse_metrics(HEAD, "ETHUSDT")
    with pytest.raises(ValueError, match="without sum_open_interest_value"):
        parse_metrics(HEAD.replace(b"sum_open_interest_value", b"oi_value"))


def test_open_interest_history_is_an_archive_dataset():
    assert Dataset.OPEN_INTEREST_HISTORY in ARCHIVE_DATASETS
    assert Dataset.OPEN_INTEREST_HISTORY not in CONTRACT_DATASETS


# -- download ----------------------------------------------------------------------------------------------


def test_download_stores_verified_days_and_fetches_only_missing_ones(tmp_path):
    fake = FakeVision()
    for day in ("2026-01-01", "2026-01-02", "2026-01-04"):
        fake.publish("BTCUSDT", day)
    results = download_metrics(["BTCUSDT"], "2026-01-01", "2026-01-04", root=tmp_path, client=fake)
    assert [(r.new_rows, r.total_rows, r.error) for r in results] == [(21, 21, None)]
    path = VenueStore(tmp_path).path("binance", Dataset.OPEN_INTEREST_HISTORY, "BTCUSDT")
    assert path == tmp_path / "venues/binance/open_interest_history/BTCUSDT.parquet"
    stored = pd.read_parquet(path)
    assert pd.Timestamp("2026-01-02 00:00") in stored.index  # 01-01's last snapshot, not a row of 01-02
    assert sorted(set(archive_days(stored.index).strftime("%Y-%m-%d"))) == ["2026-01-01", "2026-01-02", "2026-01-04"]

    # Next run, one day older and the late 01-03 archive: only the days the series lacks are requested.
    fake.publish("BTCUSDT", "2026-01-03")
    fake.publish("BTCUSDT", "2025-12-31")
    fake.calls.clear()
    results = download_metrics(["BTCUSDT"], "2025-12-31", "2026-01-04", root=tmp_path, client=fake)
    assert [(r.new_rows, r.total_rows) for r in results] == [(14, 35)]
    assert sorted(c for c in fake.calls if not c.endswith(".CHECKSUM")) == [
        metrics_path("BTCUSDT", "2025-12-31"), metrics_path("BTCUSDT", "2026-01-03")]


def test_download_skips_days_before_listing_and_isolates_failures(tmp_path):
    fake = FakeVision()
    fake.publish("BTCUSDT", "2026-01-01")
    fake.publish("BTCUSDT", "2026-01-02")
    fake.files[metrics_path("BTCUSDT", "2026-01-02") + ".CHECKSUM"] = b"0" * 64 + b"  wrong\n"
    fake.publish("NEWUSDT", "2026-01-02")
    results = download_metrics(["BTCUSDT", "NEWUSDT"], "2026-01-01", "2026-01-02", root=tmp_path, client=fake,
                               listed={"NEWUSDT": pd.Timestamp("2026-01-02 08:00")})
    btc, new = results
    assert btc.error.startswith("ChecksumError") and new.error is None and new.total_rows == 7
    assert metrics_path("NEWUSDT", "2026-01-01") not in fake.calls  # before the listing: never asked
    # The day verified before the bad checksum is kept; the bad one is not stored.
    stored = pd.read_parquet(VenueStore(tmp_path).path("binance", Dataset.OPEN_INTEREST_HISTORY, "BTCUSDT"))
    assert set(archive_days(stored.index)) == {pd.Timestamp("2026-01-01")}


def test_root_has_no_default(tmp_path, monkeypatch):
    monkeypatch.setenv("QUANT_MARKETDATA_ROOT", str(tmp_path))
    with pytest.raises(ValueError, match="root is required"):
        download_metrics(["BTCUSDT"], "2026-01-01", "2026-01-01", root=None, client=FakeVision())
    with pytest.raises(SystemExit):
        vision.main(["--symbols", "BTCUSDT", "--start", "2026-01-01"])
    assert not any(tmp_path.iterdir())


@pytest.mark.live
def test_live_archive_matches_rest_open_interest_history():
    """A day archived ~10 days ago against the REST openInterestHist (it keeps 30 days): same snapshots, same
    stamps once the archive's label is shifted by SNAPSHOT_LAG."""
    day = (pd.Timestamp.now(tz="UTC").tz_localize(None) - pd.Timedelta(days=10)).normalize()
    csv = fetch_archive(JsonClient(vision.BASE_URL, rate=2.0), metrics_path("BTCUSDT", day))
    frame = parse_metrics(csv, "BTCUSDT")
    assert 280 <= len(frame) <= 288 and frame.index.min() > day
    assert frame["open_interest_usd"].min() > 1e9
    rest = JsonClient("https://fapi.binance.com", rate=1.0).get("futures/data/openInterestHist", {
        "symbol": "BTCUSDT", "period": "5m", "startTime": int(day.timestamp() * 1000),
        "endTime": int((day + pd.Timedelta(hours=1)).timestamp() * 1000), "limit": 12})
    rest = pd.Series({pd.Timestamp(int(r["timestamp"]), unit="ms"): float(r["sumOpenInterest"]) for r in rest})
    common = frame.index.intersection(rest.index)
    assert len(common) >= 11
    assert frame.loc[common, "open_interest_base"].to_numpy() == pytest.approx(rest[common].to_numpy(), rel=1e-6)
