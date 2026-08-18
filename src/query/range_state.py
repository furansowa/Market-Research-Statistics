"""Queries against the precomputed `range_state` table (Gyrational Range page).

The table stores raw trailing/forward ranges only -- buckets depend on the
reference window and bucket scheme chosen in the UI, so they're derived at
read time (see gyrations.range_state.add_causal_buckets).
"""

from __future__ import annotations

import sqlite3

import polars as pl


def available_instruments(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0] for r in conn.execute(
            "SELECT DISTINCT instrument FROM range_state ORDER BY instrument"
        ).fetchall()
    ]


def date_bounds(conn: sqlite3.Connection, instrument: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM range_state WHERE instrument = ?", (instrument,)
    ).fetchone()
    return (row[0] or "")[:10], (row[1] or "")[:10]


def load_state(conn: sqlite3.Connection, instrument: str, rth_only: bool) -> pl.DataFrame:
    """All sample points for one instrument, optionally restricted to the
    regular session. Returned in chronological order -- the rolling
    reference windows used for bucketing depend on that order."""
    sql = "SELECT * FROM range_state WHERE instrument = ?"
    params: list = [instrument]
    if rth_only:
        sql += " AND in_rth = 1"
    sql += " ORDER BY ts"

    cur = conn.execute(sql, params)
    names = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(rows, schema=names, orient="row")
    return df.with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))
