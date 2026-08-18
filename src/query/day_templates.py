"""Queries for the Day Templates v1.0 page.

Reads the precomputed `day_profiles` table (see run_day_templates.py) and,
for the per-day charts, the `bars` table. Both cover US30 AND DAX, which is
why this page can offer a real instrument selector while the older pages are
US30-only -- those depend on `sessions`/`minutes`, which DAX is not in.
"""

from __future__ import annotations

import json
import sqlite3

import polars as pl

PROFILE_COLUMNS = (
    "date", "weekday", "rth_open", "rth_high", "rth_low", "rth_close",
    "range_pts", "n_bars", "open_pos", "close_pos", "high_frac", "low_frac",
    "prev_date", "prev_open", "prev_close", "prev_range", "prev2_close",
    "prof_h", "prof_l", "prof_c",
)


def available_instruments(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT instrument FROM day_profiles ORDER BY instrument"
        ).fetchall()
    ]


def date_bounds(conn: sqlite3.Connection, instrument: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM day_profiles WHERE instrument = ?",
        (instrument,),
    ).fetchone()
    return row[0] or "", row[1] or ""


def load_profiles(conn: sqlite3.Connection, instrument: str) -> list[dict]:
    """Every session profile for one instrument, oldest first, with the JSON
    bucket arrays already decoded into lists of floats."""
    sql = (
        f"SELECT {', '.join(PROFILE_COLUMNS)} FROM day_profiles "
        "WHERE instrument = ? ORDER BY date"
    )
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, (instrument,)).fetchall()]
    for r in rows:
        r["h"] = json.loads(r.pop("prof_h"))
        r["l"] = json.loads(r.pop("prof_l"))
        r["c"] = json.loads(r.pop("prof_c"))
    return rows


def load_day_bars(
    conn: sqlite3.Connection, instrument: str, date: str, tf_min: int = 5,
    rth_only: bool = True,
) -> pl.DataFrame:
    """Bars for one session, for the drill-down chart."""
    sql = (
        "SELECT ts, open, high, low, close FROM bars "
        "WHERE instrument = ? AND tf_min = ? AND ts LIKE ? AND n_min = ?"
    )
    params: list = [instrument, tf_min, f"{date}%", tf_min]
    if rth_only:
        sql += " AND n_rth_min = n_min"
    sql += " ORDER BY ts"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows, schema=["ts", "open", "high", "low", "close"], orient="row"
    ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))
