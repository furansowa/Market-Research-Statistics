"""Queries for the Hourly Composite page's `session_hour_bars` table."""

from __future__ import annotations

import sqlite3

# (RTH open, RTH close) ET minutes-from-midnight -- must match run_hour_bars.py.
RTH_WINDOWS = {
    "US30": (9 * 60 + 30, 16 * 60),
    "DAX": (3 * 60, 11 * 60 + 30),
}


def available_instruments(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT instrument FROM session_hour_bars ORDER BY instrument"
        ).fetchall()
    ]


def bar_slots(instrument: str) -> list[tuple[int, str]]:
    """[(bar_num, 'HH:MM-HH:MM ET'), ...] for the given instrument."""
    lo, hi = RTH_WINDOWS[instrument]
    n_hours = (hi - lo) // 60
    out = []
    for k in range(1, n_hours + 1):
        a = lo + (k - 1) * 60
        b = a + 60
        out.append((k, f"{a//60:02d}:{a%60:02d}-{b//60:02d}:{b%60:02d} ET"))
    return out


def load_bar_series(conn: sqlite3.Connection, instrument: str, bar_num: int) -> list[dict]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT date, weekday, open, high, low, close FROM session_hour_bars "
        "WHERE instrument = ? AND bar_num = ? ORDER BY date",
        (instrument, bar_num),
    ).fetchall()
    return [dict(r) for r in rows]
