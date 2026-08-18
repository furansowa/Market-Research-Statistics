"""Queries against the precomputed `bars` table (higher-timeframe OHLC).

Bars are clock-aligned in ET. `n_min` is how many 1-minute bars actually went
into each one -- session-edge and holiday bars are short, so most research
should pass complete_only=True rather than silently mixing a 12-minute bar in
with the 60-minute ones.
"""

from __future__ import annotations

import sqlite3

import polars as pl

TIMEFRAMES = [5, 10, 15, 30, 60, 240]


def available(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (r[0], r[1]) for r in conn.execute(
            "SELECT DISTINCT instrument, tf_min FROM bars ORDER BY instrument, tf_min"
        ).fetchall()
    ]


def date_bounds(conn: sqlite3.Connection, instrument: str, tf_min: int) -> tuple[str, str]:
    row = conn.execute(
        "SELECT MIN(ts), MAX(ts) FROM bars WHERE instrument = ? AND tf_min = ?",
        (instrument, tf_min),
    ).fetchone()
    return (row[0] or "")[:10], (row[1] or "")[:10]


def load_bars(
    conn: sqlite3.Connection,
    instrument: str,
    tf_min: int,
    complete_only: bool = True,
    rth_only: bool = False,
    start: str | None = None,
    end: str | None = None,
) -> pl.DataFrame:
    """Chronological OHLC for one (instrument, timeframe).

    complete_only -- keep only bars holding a full timeframe of minutes.
    rth_only      -- keep only bars whose minutes were ALL inside the regular
                     session, so bars straddling the open/close are dropped
                     rather than half-counted.
    """
    sql = "SELECT ts, open, high, low, close, n_min, n_rth_min FROM bars WHERE instrument = ? AND tf_min = ?"
    params: list = [instrument, tf_min]
    if complete_only:
        sql += " AND n_min = ?"
        params.append(tf_min)
    if rth_only:
        sql += " AND n_rth_min = n_min"
    if start:
        sql += " AND ts >= ?"
        params.append(start)
    if end:
        sql += " AND ts <= ?"
        params.append(end)
    sql += " ORDER BY ts"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows,
        schema=["ts", "open", "high", "low", "close", "n_min", "n_rth_min"],
        orient="row",
    ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))
