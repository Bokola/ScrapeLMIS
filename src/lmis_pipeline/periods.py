"""Shared month-window calculation, used by both country pipelines to
compute which reporting periods to pull."""
from __future__ import annotations

import calendar
from datetime import date


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


def month_abbr_period(year: int, month: int) -> str:
    """Format (year, month) as the scraper-facing period string Malawi's
    site expects, e.g. (2026, 6) -> "Jun2026"."""
    return f"{calendar.month_abbr[month]}{year}"
