"""Binance Vision archives (data.binance.vision) over plain HTTPS: USD-M futures daily ``metrics``, the 5-minute
open-interest history that the REST ``futures/data/openInterestHist`` keeps for 30 days only.

    qmd-binance-vision --root D:\\research\\marketdata --symbols BTCUSDT ETHUSDT --start 2025-10-01

Every archive is checked against its ``.CHECKSUM`` file (SHA-256 of the zip) before it is read: an archive
without a matching checksum is never stored. Rows land in ``venues/binance/open_interest_history/<SYMBOL>.parquet``
(``Dataset.OPEN_INTEREST_HISTORY``): ``OPEN_INTEREST_HISTORY_COLUMNS`` in base units and USD counting one side like
the REST ``openInterest``, plus Binance's long/short ratios under their native names and ``source =
"binance_vision"``. The index is the time the snapshot is known (naive UTC), as the REST endpoint stamps it: an
archive row labelled ``create_time`` t holds the values REST stamps t + 5 min (identical on every common stamp,
checked 2026-10-24), so ``ts`` = t + ``SNAPSHOT_LAG`` and the label stays in ``create_time_raw``; a day's archive
runs from 00:05 to 24:00. A run fetches the UTC days of ``[start, end]`` the series does not
hold yet, older or newer; a day that is not published (404: before the listing, or not archived yet) is tried
again on the next run.

Funding is not taken from the archives: ``BinanceVenue.funding`` serves the full history from REST, under the API
terms.

Licence -- not the API's. The Binance Vision Dataset Terms v1.0 (2026-08-26, ``TERMS_AND_CONDITIONS.md`` of
github.com/binance/binance-public-data) license the archives under CC BY-NC-SA 4.0 and limit their use to academic
research, open education and "algorithmic historical backtesting for purely personal non-production research"
(§4.1). Live trading, automated order generation and paid signals (§4.2) and integration into "hedge fund
management tools" (§4.4) are excluded; redistributed derived work stays CC BY-NC-SA 4.0 and credits Binance Vision
(§4.5); posted rate limits apply and scraping around them is barred (§7). Hence ``root`` has no default (no
``QUANT_MARKETDATA_ROOT`` fallback, so a production store is never filled by accident), downloads are sequential
and paced, and no adapter, download or snapshot task of this package reads the dataset.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import logging
import re
import sys
import zipfile
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence

import pandas as pd

from quant_marketdata.venues.binance import BinanceVenue
from quant_marketdata.venues.download import DownloadResult
from quant_marketdata.venues.http import HttpError, JsonClient
from quant_marketdata.venues.schema import OPEN_INTEREST_HISTORY_COLUMNS, Dataset
from quant_marketdata.venues.store import VenueStore

logger = logging.getLogger(__name__)

BASE_URL = "https://data.binance.vision"
VENUE = "binance"
SOURCE = "binance_vision"
TERMS = ("Binance Vision Dataset Terms v1.0 (2026-08-26): CC BY-NC-SA 4.0, non-commercial and non-production "
         "research only (no live trading, no fund tooling)")
RATIO_COLUMNS = ["count_toptrader_long_short_ratio", "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                 "sum_taker_long_short_vol_ratio"]
METRICS_COLUMNS = ["create_time", "symbol", "sum_open_interest", "sum_open_interest_value", *RATIO_COLUMNS]
CHUNK_DAYS = 30  # days merged to disk at once, so an interrupted run keeps its progress
SNAPSHOT_LAG = pd.Timedelta(minutes=5)  # archive label -> REST stamp of the same snapshot


class ChecksumError(RuntimeError):
    """An archive whose SHA-256 does not match its ``.CHECKSUM`` file, or that has no usable one."""


def metrics_path(symbol: str, day) -> str:
    """One UTC day's USD-M metrics archive, relative to ``BASE_URL``."""
    return f"data/futures/um/daily/metrics/{symbol}/{symbol}-metrics-{pd.Timestamp(day):%Y-%m-%d}.zip"


def parse_checksum(text: str) -> str:
    """``<sha256>  <file name>`` -> the lowercase hex digest."""
    match = re.match(r"\s*([0-9a-fA-F]{64})\b", text)
    if not match:
        raise ChecksumError(f"not a SHA-256 checksum line: {text[:80]!r}")
    return match.group(1).lower()


def read_archive(blob: bytes, checksum: str) -> bytes:
    """The single CSV member of a zip archive, once the archive's SHA-256 matches ``checksum`` (``.CHECKSUM``)."""
    expected = parse_checksum(checksum)
    actual = hashlib.sha256(blob).hexdigest()
    if actual != expected:
        raise ChecksumError(f"SHA-256 {actual} does not match the published {expected}")
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = [name for name in archive.namelist() if name.endswith(".csv")]
        if len(members) != 1:
            raise ValueError(f"expected one CSV member, got {archive.namelist()}")
        return archive.read(members[0])


def fetch_archive(client: JsonClient, path: str) -> Optional[bytes]:
    """Download ``path`` and its ``.CHECKSUM``, verify, return the CSV bytes; None when it is not published."""
    try:
        blob = client.get_bytes(path)
    except HttpError as exc:
        if exc.status == 404:
            return None
        raise
    try:
        checksum = client.get_bytes(f"{path}.CHECKSUM")
    except HttpError as exc:
        if exc.status == 404:
            raise ChecksumError(f"{path} has no .CHECKSUM file") from exc
        raise
    return read_archive(blob, checksum.decode("ascii", "replace"))


def parse_metrics(csv: bytes, symbol: Optional[str] = None) -> pd.DataFrame:
    """One metrics CSV -> ``OPEN_INTEREST_HISTORY_COLUMNS`` + ``RATIO_COLUMNS`` + ``create_time_raw``, indexed by
    the REST stamp of each snapshot (``create_time`` + ``SNAPSHOT_LAG``)."""
    frame = pd.read_csv(io.BytesIO(csv), dtype={"create_time": str})
    missing = [c for c in METRICS_COLUMNS if c not in frame.columns]
    if missing:
        raise ValueError(f"metrics archive without {', '.join(missing)}")
    if symbol is not None and not frame["symbol"].eq(symbol).all():
        raise ValueError(f"metrics archive of {sorted(set(frame['symbol']))}, expected {symbol}")
    index = pd.DatetimeIndex(pd.to_datetime(frame["create_time"], format="ISO8601") + SNAPSHOT_LAG, name="ts")
    out = pd.DataFrame({
        "open_interest_base": frame["sum_open_interest"].astype(float).to_numpy(),
        "open_interest_usd": frame["sum_open_interest_value"].astype(float).to_numpy(),
        "oi_convention": "one_side",
        "source": SOURCE,
    }, index=index)
    for column in RATIO_COLUMNS:
        out[column] = frame[column].astype(float).to_numpy()
    out["create_time_raw"] = frame["create_time"].to_numpy()
    out = out[OPEN_INTEREST_HISTORY_COLUMNS + RATIO_COLUMNS + ["create_time_raw"]]
    return out[~out.index.duplicated(keep="last")].sort_index()


def archive_days(index: pd.DatetimeIndex) -> pd.DatetimeIndex:
    """The UTC day of the archive each stored row came from (its last row is stamped 24:00)."""
    return (index - SNAPSHOT_LAG).normalize()


def _today() -> pd.Timestamp:
    return pd.Timestamp.now(tz="UTC").tz_localize(None).normalize()


def download_metrics(symbols: Sequence[str], start, end=None, *, root, rate: float = 2.0,
                     client: Optional[JsonClient] = None,
                     listed: Optional[Mapping[str, pd.Timestamp]] = None) -> List[DownloadResult]:
    """Store the metrics of ``symbols`` for the UTC days of ``[start, end]`` (default end: yesterday) each series
    lacks. ``root`` is required (licence: see the module docstring); ``listed`` = {symbol: listing time} skips the
    days before a listing."""
    if root is None:
        raise ValueError("root is required: Binance Vision data never goes to the default store (licence)")
    client = client or JsonClient(BASE_URL, rate=rate)
    store = VenueStore(Path(root))
    first = pd.Timestamp(start).normalize()
    last = (pd.Timestamp(end) if end is not None else _today() - pd.Timedelta(days=1)).normalize()
    results: List[DownloadResult] = []
    for index, symbol in enumerate(symbols, 1):
        since = first
        if listed and pd.notna(listed.get(symbol)):
            since = max(first, pd.Timestamp(listed[symbol]).normalize())
        try:
            result = _download_symbol(store, client, symbol, since, last)
        except Exception as exc:  # one failing series must not stop the others
            result = DownloadResult(VENUE, symbol, Dataset.OPEN_INTEREST_HISTORY.value,
                                    error=f"{type(exc).__name__}: {exc}".splitlines()[0])
            logger.warning("[%d/%d] %s open_interest_history failed: %s", index, len(symbols), symbol,
                           result.error)
        else:
            logger.info("[%d/%d] %s open_interest_history: +%d rows (%d total)", index, len(symbols), symbol,
                        result.new_rows, result.total_rows)
        results.append(result)
    return results


def _download_symbol(store: VenueStore, client: JsonClient, symbol: str, first: pd.Timestamp,
                     last: pd.Timestamp) -> DownloadResult:
    path = store.path(VENUE, Dataset.OPEN_INTEREST_HISTORY, symbol)
    before = store.row_count(path)
    held = set(archive_days(pd.read_parquet(path, columns=[]).index)) if path.exists() else set()
    days = [d for d in pd.date_range(first, last, freq="D") if d not in held]
    frames: List[pd.DataFrame] = []
    unpublished: List[pd.Timestamp] = []
    try:
        for day in days:
            csv = fetch_archive(client, metrics_path(symbol, day))
            if csv is None:
                unpublished.append(day)
                continue
            frames.append(parse_metrics(csv, symbol))
            if len(frames) >= CHUNK_DAYS:
                store.merge(path, pd.concat(frames))
                frames = []
    finally:  # keep the verified days even when a later one fails
        if frames:
            store.merge(path, pd.concat(frames))
    if unpublished:
        logger.info("%s: %d day(s) not published, %s to %s", symbol, len(unpublished),
                    f"{unpublished[0]:%Y-%m-%d}", f"{unpublished[-1]:%Y-%m-%d}")
    total = store.row_count(path)
    return DownloadResult(VENUE, symbol, Dataset.OPEN_INTEREST_HISTORY.value, total - before, total)


def listing_dates(symbols: Sequence[str], venue: Optional[BinanceVenue] = None) -> Dict[str, pd.Timestamp]:
    """Listing times of ``symbols`` from the REST ``exchangeInfo`` (``onboardDate``), to skip the days before."""
    instruments = (venue or BinanceVenue()).instruments()
    known = dict(zip(instruments["venue_symbol"], instruments["listed_at"], strict=True))
    for symbol in symbols:
        if symbol not in known:
            logger.warning("%s is not a listed Binance USD-M perp (delisted?): every day from --start is tried",
                           symbol)
    return {s: known[s] for s in symbols if s in known}


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=f"Download Binance Vision USD-M metrics (5-minute open-interest history) into a store. {TERMS}.")
    parser.add_argument("--root", required=True,
                        help="Store root (required, no default: keep this data out of the production store).")
    parser.add_argument("--symbols", nargs="+", required=True, help="USD-M symbols, e.g. BTCUSDT ETHUSDT.")
    parser.add_argument("--start", type=pd.Timestamp, required=True, help="First UTC day.")
    parser.add_argument("--end", type=pd.Timestamp, help="Last UTC day (default: yesterday).")
    parser.add_argument("--rate", type=float, default=2.0,
                        help="Max requests per second (default: 2; two requests per day archive).")
    parser.add_argument("-v", "--verbose", action="store_true", help="Log retries.")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    logger.info("%s", TERMS)
    results = download_metrics(args.symbols, args.start, args.end, root=args.root, rate=args.rate,
                               listed=listing_dates(args.symbols))
    failed = [r for r in results if r.error]
    print(f"Done: {len(results)} series, {sum(r.new_rows for r in results)} new rows, {len(failed)} failed")
    for r in failed:
        print(f"  FAILED {r.symbol} {r.dataset}: {r.error}")
    return 1 if failed else 0


__all__ = ["BASE_URL", "ChecksumError", "METRICS_COLUMNS", "RATIO_COLUMNS", "SNAPSHOT_LAG", "SOURCE", "TERMS",
           "archive_days", "download_metrics", "fetch_archive", "listing_dates", "main", "metrics_path", "parse_checksum", "parse_metrics",
           "read_archive"]


if __name__ == "__main__":
    sys.exit(main())
