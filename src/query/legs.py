"""Aggregate/per-leg queries against the `gyrations` table (Gyration Legs page).

Complements `query.filters`'s session-table queries — these read gyration legs
directly, scoped by an *exact* list of dates rather than a min/max range, since
the caller's session-level filters (weekday, etc.) can produce a non-contiguous
date set that a range would over-include.

`scope="rth"`/`mode="close_to_close"` (Gyration Legs page) and, as of
2026-07-18, `scope="rth"`/`mode="extreme_to_extreme"` (Gyrations v2.0 page,
all 14 thresholds stored though that page only uses 40/120/200) are
precomputed/stored in the `gyrations` table at useful scale (see
config.toml's `[gyrations] precompute`) — `eth`/`extreme_to_extreme` and
`continuous` scope are not usable here; the caller is responsible for not
asking for anything else.
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


def _full_leg_rows(
    conn: sqlite3.Connection,
    instrument: str,
    threshold: float,
    dates: list[str],
    scope: str = "rth",
    mode: str = "extreme_to_extreme",
    confirmed_only: bool = True,
) -> list[dict]:
    """One row per leg: start_ts, end_ts, start_date, magnitude_pts,
    duration_min, confirmed, direction, start_price, end_price — ordered by
    start_ts. Private to this module; `leg_pivots` above is too narrow to
    reuse here (missing magnitude_pts/duration_min/confirmed) and is left
    untouched since the existing Gyration Legs page depends on its exact
    shape. Default mode is "extreme_to_extreme" (opposite of the other
    functions in this module) since that's the Gyrations v2.0 page's whole
    point.
    """
    if not dates:
        return []

    placeholders = ", ".join("?" for _ in dates)
    confirmed_clause = " AND confirmed = 1" if confirmed_only else ""
    sql = (
        "SELECT start_ts, end_ts, start_date, magnitude_pts, duration_min, confirmed, "
        "direction, start_price, end_price "
        "FROM gyrations "
        "WHERE instrument = ? AND scope = ? AND mode = ? AND threshold = ? "
        f"AND start_date IN ({placeholders}){confirmed_clause} "
        "ORDER BY start_ts"
    )
    params = [instrument, scope, mode, threshold, *dates]
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def leg_pair_aggregates_by_date(
    conn: sqlite3.Connection,
    instrument: str,
    threshold: float,
    dates: list[str],
    scope: str = "rth",
    mode: str = "extreme_to_extreme",
    confirmed_only: bool = True,
) -> dict[str, dict]:
    """{date: {"avg_pair_pts", "avg_pair_duration_min"}} — a "gyration" pairs
    up consecutive legs within a session (0&1, 2&3, ...; a trailing unpaired
    leg is dropped, not counted). For each complete pair, sums both legs'
    magnitude_pts (and separately duration_min); the returned value per date
    is the average of those pair-sums across all complete pairs that date. A
    date with zero complete pairs is simply absent — caller fills `None`
    (matching how `leg_aggregates_by_date`'s Avg/Sum already default to
    `None`, not 0, when there's nothing to average).
    """
    legs = _full_leg_rows(conn, instrument, threshold, dates, scope=scope, mode=mode, confirmed_only=confirmed_only)

    by_date: dict[str, list[dict]] = {}
    for leg in legs:
        by_date.setdefault(leg["start_date"], []).append(leg)

    result: dict[str, dict] = {}
    for date, date_legs in by_date.items():
        pair_pts = []
        pair_durations = []
        for i in range(0, len(date_legs) - 1, 2):
            pair_pts.append(date_legs[i]["magnitude_pts"] + date_legs[i + 1]["magnitude_pts"])
            pair_durations.append(date_legs[i]["duration_min"] + date_legs[i + 1]["duration_min"])
        if pair_pts:
            result[date] = {
                "avg_pair_pts": sum(pair_pts) / len(pair_pts),
                "avg_pair_duration_min": sum(pair_durations) / len(pair_durations),
            }
    return result


def leg_detail_rows(
    conn: sqlite3.Connection,
    instrument: str,
    threshold: float,
    dates: list[str],
    scope: str = "rth",
    mode: str = "extreme_to_extreme",
    confirmed_only: bool = True,
) -> list[dict]:
    """One row per individual leg (not per session) — backend-only for now,
    no page wired to it yet. Fields: date, weekday, session_open_price,
    direction, start_price_rel_open, start_time, end_price_rel_open,
    end_time, duration_min, size_pts, size_pct, pattern, time_ratio,
    size_ratio, gyration_size_pts.

    "Previous leg" resets at each session's first leg (grouped by date,
    ordered by start_ts) — there's no real continuity of the swing sequence
    across a session boundary, since `compute_session_scope_legs` runs one
    independent detector call per (instrument, date); `leg_index` there is
    only a global storage counter, not evidence of a continuous sequence.

    Pattern/ratios/gyration size for the leg at position `i` in its per-date
    sequence: `i == 0` -> pattern "1st", ratios/gyration_size all `None`;
    else `pattern` is "V1"/"V2" (direction up, magnitude strictly greater
    than / less-or-equal to the previous leg's) or "A1"/"A2" (direction
    down, same comparison); `time_ratio`/`size_ratio` are this leg's
    duration/magnitude divided by the previous leg's (`None` if the
    denominator is 0); `gyration_size_pts` is this leg's magnitude plus the
    previous leg's, but ONLY when `i` is odd (this leg is the *second* of a
    pair — same 0&1/2&3/... pairing as `leg_pair_aggregates_by_date`),
    `None` otherwise.
    """
    legs = _full_leg_rows(conn, instrument, threshold, dates, scope=scope, mode=mode, confirmed_only=confirmed_only)
    if not legs:
        return []

    placeholders = ", ".join("?" for _ in dates)
    session_rows = conn.execute(
        f'SELECT date, weekday, rth_open FROM sessions WHERE instrument = ? AND date IN ({placeholders})',
        [instrument, *dates],
    ).fetchall()
    session_by_date = {date: (weekday, rth_open) for date, weekday, rth_open in session_rows}

    by_date: dict[str, list[dict]] = {}
    for leg in legs:
        by_date.setdefault(leg["start_date"], []).append(leg)

    result: list[dict] = []
    for date, date_legs in by_date.items():
        weekday, open_price = session_by_date.get(date, (None, None))
        prev = None
        for i, leg in enumerate(date_legs):
            mag, dur, direction = leg["magnitude_pts"], leg["duration_min"], leg["direction"]

            if prev is None:
                pattern, time_ratio, size_ratio = "1st", None, None
            else:
                prev_mag, prev_dur = prev["magnitude_pts"], prev["duration_min"]
                bigger = mag > prev_mag
                if direction == "up":
                    pattern = "V1" if bigger else "V2"
                else:
                    pattern = "A1" if bigger else "A2"
                time_ratio = (dur / prev_dur) if prev_dur else None
                size_ratio = (mag / prev_mag) if prev_mag else None

            gyration_size_pts = mag + date_legs[i - 1]["magnitude_pts"] if i % 2 == 1 else None

            result.append({
                "date": date,
                "weekday": weekday,
                "session_open_price": open_price,
                "direction": direction,
                "start_price_rel_open": (leg["start_price"] - open_price) if open_price is not None else None,
                "start_time": leg["start_ts"],
                "end_price_rel_open": (leg["end_price"] - open_price) if open_price is not None else None,
                "end_time": leg["end_ts"],
                "duration_min": dur,
                "size_pts": mag,
                "size_pct": (mag / open_price * 100) if open_price else None,
                "pattern": pattern,
                "time_ratio": time_ratio,
                "size_ratio": size_ratio,
                "gyration_size_pts": gyration_size_pts,
            })
            prev = leg

    return result
