"""Shared month-window calculation, used by both country pipelines to
compute which reporting periods to pull."""
from __future__ import annotations

import calendar
from datetime import date, timedelta


def month_window(
    n_months: int, offset_months: int = 0, today: date | None = None
) -> list[tuple[int, int]]:
    """Return n_months (year, month) tuples, oldest first, ending
    offset_months before the current month (inclusive of that ending
    month). offset_months=0 (the default) means the window ends at the
    CURRENT month - the original "trailing N months inclusive of now"
    behavior both pipelines started with.

    E.g. today=2026-09-10, n_months=2, offset_months=2 ->
    [(2026, 6), (2026, 7)] - June and July, skipping August and September
    entirely (the two most recent months), per an explicit "only the
    months from 2 months ago, not the most recent ones" reporting-lag
    request.
    """
    today = today or date.today()
    year, month = today.year, today.month

    for _ in range(offset_months):
        month -= 1
        if month == 0:
            month = 12
            year -= 1

    months: list[tuple[int, int]] = []
    for _ in range(n_months):
        months.append((year, month))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return list(reversed(months))


def month_range(start: tuple[int, int], end: tuple[int, int]) -> list[tuple[int, int]]:
    """Return every (year, month) tuple from start to end inclusive, oldest
    first - e.g. start=(2024,1), end=(2024,3) ->
    [(2024,1), (2024,2), (2024,3)]. Used for a fixed historical backfill
    range, as opposed to month_window()'s rolling window relative to
    today.
    """
    (start_year, start_month), (end_year, end_month) = start, end
    months: list[tuple[int, int]] = []
    year, month = start_year, start_month
    while (year, month) <= (end_year, end_month):
        months.append((year, month))
        month += 1
        if month == 13:
            month = 1
            year += 1
    return months


def resolve_historical_end(
    raw_value: str, n_months: int, offset_months: int, today: date | None = None
) -> tuple[int, int]:
    """Resolve filters.historical_end to a concrete (year, month).

    A literal date string (e.g. "01/05/2026") is parsed directly - same
    as before. The special value "auto" instead computes the month
    immediately BEFORE the regular (non-historical) run's own rolling
    window currently starts, so there's never a gap between the two.

    CONFIRMED to be a real, not hypothetical, risk: a fixed
    historical_end left the regular run's rolling window (relative to
    today, unlike historical_end) to drift forward as time passed -
    leaving a month uncovered by EITHER mode once enough time had gone
    by since historical_end was last set (June 2026, for both MZ and MW,
    by the time this was caught). "auto" removes the need to keep
    historical_end updated by hand to avoid this recurring every month.
    """
    if str(raw_value).strip().lower() == "auto":
        today = today or date.today()
        regular_start = month_window(n_months, offset_months, today=today)[0]
        year, month = regular_start
        month -= 1
        if month == 0:
            month, year = 12, year - 1
        return (year, month)
    return parse_ddmmyyyy_to_year_month(raw_value)


def parse_ddmmyyyy_to_year_month(s: str) -> tuple[int, int]:
    """Parse a 'DD/MM/YYYY' date string (matching this project's own
    Period output format) into (year, month) - the day is ignored, since
    every period this project deals with is month-level."""
    day, month, year = s.strip().split("/")
    return int(year), int(month)


def chunk_month_range(
    start: tuple[int, int], end: tuple[int, int], chunk_size: int = 3
) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    """Split [start, end] (inclusive) into consecutive chunks of at most
    chunk_size months each, e.g. start=(2024,1), end=(2024,7),
    chunk_size=3 -> [((2024,1),(2024,3)), ((2024,4),(2024,6)),
    ((2024,7),(2024,7))] - the last chunk is shorter if the range doesn't
    divide evenly. Used for historical backfills that need to stay within
    a safe per-download size (e.g. Mozambique's chunked 3-month fixed-date-
    range downloads).
    """
    months = month_range(start, end)
    return [
        (chunk[0], chunk[-1])
        for chunk in (months[i : i + chunk_size] for i in range(0, len(months), chunk_size))
    ]


def month_bounds_to_widget_dates(
    start: tuple[int, int], end: tuple[int, int], pad_days: int = 25
) -> tuple[date, date]:
    """Compute a calendar date range covering [start, end] (inclusive
    (year, month) bounds), padded by pad_days on each side, for use with a
    "fixed date range" style widget that takes actual calendar dates
    rather than year/month.

    The padding exists because it's NOT CONFIRMED whether such a widget's
    date semantics align exactly with this project's own "Período de
    análise" reporting-period cycle (which spans the 21st of one month to
    the 20th of the next, per confirmed real data) - padding generously on
    both sides guarantees every relevant reporting period is captured
    regardless, since a client-side trim (e.g.
    main.py's filter_to_absolute_period_range()) is relied on afterward to
    cut back down to exactly [start, end] - this function only needs to
    err toward "too wide", never "too narrow".
    """
    start_date = date(start[0], start[1], 1) - timedelta(days=pad_days)
    last_day = calendar.monthrange(end[0], end[1])[1]
    end_date = date(end[0], end[1], last_day) + timedelta(days=pad_days)
    return start_date, end_date


def month_abbr_period(year: int, month: int) -> str:
    """Format (year, month) as the scraper-facing period string Malawi's
    site expects, e.g. (2026, 6) -> "Jun2026"."""
    return f"{calendar.month_abbr[month]}{year}"


ENGLISH_MONTH_ABBR: tuple[str, ...] = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


def latest_completed_bimonthly_window(today: date | None = None) -> tuple[int, int, int]:
    """Return (year, first_month, second_month) of the most recent fully
    completed two month window, for sites that publish in fixed windows
    (Jan-Feb, Mar-Apr, May-Jun, Jul-Aug, Sep-Oct, Nov-Dec).

    The window containing today is still open, so it is skipped: in October
    that is Sep-Oct, so the answer is Jul-Aug; in November the open window
    is Nov-Dec, so the answer is Sep-Oct. Windows never cross a year end,
    but the answer can fall in the previous year (January -> Nov-Dec).
    """
    today = today or date.today()
    current_first = today.month if today.month % 2 == 1 else today.month - 1
    first = current_first - 2
    year = today.year
    if first < 1:
        first += 12
        year -= 1
    return year, first, first + 1


def bimonthly_window_label(year: int, first_month: int) -> str:
    """Format a two month window the way the Nigeria site shows it, e.g.
    (2026, 7) -> "Jul-Aug 2026". Uses a fixed English list rather than the
    calendar module, which follows the machine's locale."""
    first = ENGLISH_MONTH_ABBR[first_month - 1]
    second = ENGLISH_MONTH_ABBR[first_month]
    return f"{first}-{second} {year}"
