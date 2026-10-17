"""Venue-agnostic forward pagination over time windows."""

from __future__ import annotations

from typing import Callable, Iterator, Optional

import pandas as pd


def windows(start: int, end: int, step: int) -> Iterator[tuple[int, int]]:
    """Contiguous ``(window_start, window_end)`` pairs covering ``[start, end]``, in seconds.

    Consecutive windows share their boundary; callers deduplicate on the timestamp index.
    """
    if step <= 0:
        raise ValueError("step must be positive")
    cursor = start
    while cursor < end:
        window_end = min(cursor + step, end)
        yield cursor, window_end
        cursor = window_end


def paged_fetch(
    fetch_window: Callable[[int, int], pd.DataFrame],
    start: int,
    end: int,
    step: int,
) -> pd.DataFrame:
    """Fetch ``[start, end]`` window by window and return one deduplicated, sorted frame."""
    pages = [page for a, b in windows(start, end, step) if not (page := fetch_window(a, b)).empty]
    if not pages:
        return pd.DataFrame()
    frame = pd.concat(pages)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def first_non_empty(
    fetch_window: Callable[[int, int], pd.DataFrame],
    start: int,
    end: int,
    step: int,
) -> Optional[pd.Timestamp]:
    """Earliest timestamp of the first non-empty window, or ``None`` if the whole range is empty."""
    for a, b in windows(start, end, step):
        page = fetch_window(a, b)
        if not page.empty:
            return page.index.min()
    return None


def forward_fetch(
    fetch_from: Callable[[int], pd.DataFrame],
    start: int,
    end: int,
    page_size: int,
    *,
    unit: str = "ms",
) -> pd.DataFrame:
    """Cursor pagination for APIs that return the **oldest** ``page_size`` points from a start time.

    ``fetch_from(cursor)`` returns a time-indexed page; the next cursor is one ``unit`` past its last
    timestamp. Stops on a short page, an empty page or once the cursor passes ``end``.
    """
    pages, cursor = [], start
    while cursor <= end:
        page = fetch_from(cursor)
        if page.empty:
            break
        pages.append(page)
        nxt = int(page.index.max().value // pd.Timedelta(1, unit=unit).value) + 1
        if len(page) < page_size or nxt <= cursor:
            break
        cursor = nxt
    if not pages:
        return pd.DataFrame()
    frame = pd.concat(pages)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def window_count(start: int, end: int, step: int, cap: int) -> int:
    """Number of points of period ``step`` in ``[start, end]`` (bounds inclusive), capped at ``cap``."""
    return max(1, min(cap, (end - start) // step + 1))


__all__ = ["first_non_empty", "forward_fetch", "paged_fetch", "window_count", "windows"]
