"""Aggregate/per-leg queries against the `gyrations` table (Gyration Legs page).

Complements `query.filters`'s session-table queries — these read gyration legs
directly, scoped by an *exact* list of dates rather than a min/max range, since
the caller's session-level filters (weekday, etc.) can produce a non-contiguous
date set that a range would over-include.

Only `scope="rth"`/`mode="close_to_close"` is precomputed/stored in the
`gyrations` table at useful scale (see config.toml's `[gyrations] precompute`)
— `extreme_to_extreme` and `continuous` scope are not usable here; the caller
is responsible for not asking for anything else.
"""

from __future__ import annotations

import sqlite3


def leg_aggregates_by_date(
    conn: sqlite3.Connection,
    instrument: str,
    threshold: float,
    dates: list[str],
    scope: str = "rth",
    mode: str = "close_to_close",
    confirmed_only: bool = True,
) -> dict[str, dict]:
    """{date: {"count", "sum_pts", "avg_pts", "avg_duration_min"}} for the dates
    in `dates` that have >=1 matching leg — dates with zero legs are simply
    absent; callers default-fill (0 count, None for the rest) when assembling
    a per-session table. Percent-of-open (avg_pts / that session's rth_open)
    is deliberately not computed here — the caller already has each session's
    rth_open from the sessions rows it fetched via `query_sessions`, so it's a
    plain division per row there, not a second joined query here.
    """
    if not dates:
        return {}

    placeholders = ", ".join("?" for _ in dates)
    confirmed_clause = " AND confirmed = 1" if confirmed_only else ""
    sql = (
        "SELECT start_date, COUNT(*), SUM(magnitude_pts), AVG(magnitude_pts), AVG(duration_min) "
        "FROM gyrations "
        "WHERE instrument = ? AND scope = ? AND mode = ? AND threshold = ? "
        f"AND start_date IN ({placeholders}){confirmed_clause} "
        "GROUP BY start_date"
    )
    params = [instrument, scope, mode, threshold, *dates]
    rows = conn.execute(sql, params).fetchall()
    return {
        date: {"count": count, "sum_pts": sum_pts, "avg_pts": avg_pts, "avg_duration_min": avg_dur}
        for date, count, sum_pts, avg_pts, avg_dur in rows
    }


def leg_pivots(
    conn: sqlite3.Connection,
    instrument: str,
    threshold: float,
    dates: list[str],
    scope: str = "rth",
    mode: str = "close_to_close",
    confirmed_only: bool = True,
) -> list[dict]:
    """One row per individual leg: start_ts/end_ts/start_date/end_date/
    start_price/end_price/direction. For scope="rth", start_date == end_date
    always (a leg never crosses a session boundary in that scope — see
    etl/gyrations.py's module docstring). Caller derives time-of-day and
    distance-from-rth_open for both the start and end pivot of each leg in
    Python/pandas — no backend support needed for that beyond this query.
    """
    if not dates:
        return []

    placeholders = ", ".join("?" for _ in dates)
    confirmed_clause = " AND confirmed = 1" if confirmed_only else ""
    sql = (
        "SELECT start_ts, end_ts, start_date, end_date, start_price, end_price, direction "
        "FROM gyrations "
        "WHERE instrument = ? AND scope = ? AND mode = ? AND threshold = ? "
        f"AND start_date IN ({placeholders}){confirmed_clause} "
        "ORDER BY start_ts"
    )
    params = [instrument, scope, mode, threshold, *dates]
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]
