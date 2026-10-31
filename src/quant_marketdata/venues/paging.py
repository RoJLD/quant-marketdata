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


def backward_fetch(
    fetch_before: Callable[[int], pd.DataFrame],
    start: int,
    end: int,
    page_size: Optional[int] = None,
    *,
    empty_step: Optional[int] = None,
    unit: str = "ms",
) -> pd.DataFrame:
    """Cursor pagination for APIs that return the **newest** points up to an end time (Bybit ``endTime``,
    OKX ``after``, Gate ``to``).

    ``fetch_before(cursor)`` returns a time-indexed page of points stamped at or before ``cursor``; the next
    cursor is one ``unit`` before its oldest timestamp. Stops once the cursor falls before ``start``, on a short
    page (fewer than ``page_size`` points; ``None``: pages are time windows whose length says nothing), or on a
    page that does not move back. An empty page ends the walk, unless ``empty_step`` is given: the cursor then
    steps that far back (a venue serving fixed time windows can hold a hole longer than one window).
    """
    pages, cursor = [], end
    while cursor >= start:
        page = fetch_before(cursor)
        if page.empty:
            if empty_step is None:
                break
            cursor -= empty_step
            continue
        pages.append(page)
        nxt = int(page.index.min().value // pd.Timedelta(1, unit=unit).value) - 1
        if (page_size is not None and len(page) < page_size) or nxt >= cursor:
            break
        cursor = nxt
    return _concat(pages)


def numbered_fetch(
    fetch_page: Callable[[int], pd.DataFrame],
    start: pd.Timestamp,
    page_size: int,
    *,
    max_pages: int = 1000,
) -> pd.DataFrame:
    """Page-number pagination, newest first (Bitget ``pageNo``): pages 1, 2, ... until a short or empty page, a
    page reaching back to ``start``, or ``max_pages`` (a guard against a venue that never stops)."""
    pages = []
    for number in range(1, max_pages + 1):
        page = fetch_page(number)
        if page.empty:
            break
        pages.append(page)
        if len(page) < page_size or page.index.min() <= start:
            break
    return _concat(pages)


def _concat(pages: list) -> pd.DataFrame:
    if not pages:
        return pd.DataFrame()
    frame = pd.concat(pages)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


def window_count(start: int, end: int, step: int, cap: int) -> int:
    """Number of points of period ``step`` in ``[start, end]`` (bounds inclusive), capped at ``cap``."""
    return max(1, min(cap, (end - start) // step + 1))


__all__ = ["backward_fetch", "first_non_empty", "forward_fetch", "numbered_fetch", "paged_fetch",
           "window_count", "windows"]
