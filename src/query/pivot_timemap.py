"""Minute-bar loading for the Pivots TimeMap page.

Reads `minutes` only, for the same reason `query.time_waves` does: `sessions`
exists for US30 alone, so routing instrument discovery or date bounds through
it would silently hide DAX — which is the instrument this page was built for.
`available_instruments` is imported from the Time Waves query module rather
than restated, so the two pages can never disagree about what's loadable.

Unlike Time Waves' loader this one keeps the `date` column (the detector is
run per session here, so sessions must stay separable) and takes an explicit
date range instead of a trailing-years window.
"""

from __future__ import annotations

import sqlite3

import polars as pl

from query.time_waves import available_instruments  # noqa: F401  (re-exported)


def date_bounds(conn: sqlite3.Connection, instrument: str) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(date), MAX(date) FROM minutes WHERE instrument = ?", (instrument,)
    ).fetchone()
    return row[0], row[1]


def load_session_minutes(
    conn: sqlite3.Connection, instrument: str, rth_only: bool
) -> pl.DataFrame:
    """Full-history 1-minute OHLC with its `date`, chronological.

    Loads everything rather than accepting a date range: the caller caches
    this once per (instrument, basis) and slices it per query — re-reading
    2M+ rows out of SQLite costs ~18s and dwarfs the detector itself.
    """
    sql = "SELECT ts, open, high, low, close, date FROM minutes WHERE instrument = ?"
    if rth_only:
        sql += " AND session = 'RTH'"
    sql += " ORDER BY ts"

    rows = conn.execute(sql, (instrument,)).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows, schema=["ts", "open", "high", "low", "close", "date"], orient="row"
    ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))


def session_open_mod(df: pl.DataFrame) -> int:
    """ET minutes-from-midnight of the session open, read off the data.

    Derived rather than hardcoded per instrument: the RTH window already
    lives in two places (`run_bars.RTH_WINDOWS` and `run_dax_minutes`), and a
    third copy here would be the one that goes stale.
    """
    if df.is_empty():
        return 0
    mod = (
        df["ts"].dt.hour().cast(pl.Int32) * 60 + df["ts"].dt.minute().cast(pl.Int32)
    )
    return int(mod.min())
