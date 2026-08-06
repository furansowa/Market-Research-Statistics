"""Gyration-leg + session queries for the Gyrational Waves v1.0 page.

Unlike query.legs (scoped by an explicit `dates` list, matching how the
session-filter pages work), this page studies the FULL leg sequence for one
(instrument, scope, threshold, mode) at a time -- patterns are built by
walking that whole sequence (gyrations.merrill.build_patterns), and only the
resulting *patterns* get filtered down to a date range afterward, not the raw
legs feeding them (a pattern's "next pattern"/"next leg" lookups need the
real next leg/pattern regardless of whether it happens to fall outside the
selected date range -- see merrill.py).
"""

from __future__ import annotations

import sqlite3


def fetch_legs(
    conn: sqlite3.Connection, instrument: str, scope: str, threshold: float, mode: str,
    confirmed_only: bool = True,
) -> list[dict]:
    """All legs for one (instrument, scope, threshold, mode), oldest first."""
    confirmed_clause = " AND confirmed = 1" if confirmed_only else ""
    sql = (
        "SELECT leg_index, start_ts, end_ts, start_date, end_date, start_price, end_price, "
        "direction, magnitude_pts, duration_min, confirmed "
        "FROM gyrations "
        "WHERE instrument = ? AND scope = ? AND mode = ? AND threshold = ?"
        f"{confirmed_clause} "
        "ORDER BY start_ts"
    )
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, (instrument, scope, mode, threshold)).fetchall()
    return [dict(r) for r in rows]


def fetch_session_rows(conn: sqlite3.Connection, instrument: str, dates: list[str]) -> list[dict]:
    """Lightweight per-day rows for a pattern's "show days" table: date,
    weekday, bs_sb, rth_range, gap_pts, rel_close_pts, abs_close_pts."""
    if not dates:
        return []
    placeholders = ", ".join("?" for _ in dates)
    sql = (
        "SELECT date, weekday, bs_sb, rth_range, gap_pts, rel_close_pts, abs_close_pts "
        f"FROM sessions WHERE instrument = ? AND date IN ({placeholders}) ORDER BY date"
    )
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, (instrument, *dates)).fetchall()
    return [dict(r) for r in rows]


def fetch_full_session_row(conn: sqlite3.Connection, instrument: str, date: str) -> dict | None:
    """Full `sessions` row for one date -- used to feed dashboard.render_session_chart
    (needs rth_open/rth_high_time/etc., far more than the lightweight table above)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM sessions WHERE instrument = ? AND date = ?", (instrument, date)
    ).fetchone()
    return dict(row) if row else None
