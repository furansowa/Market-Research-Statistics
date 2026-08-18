"""Minute-bar loading for the Time Waves page.

Deliberately reads `minutes` and nothing else. The Waves page leans on the
`sessions` table (for instrument discovery, date bounds and the per-day stats
table), but `sessions` is built for US30 only -- so anything routed through it
would silently exclude DAX. Everything here works for any instrument present
in `minutes`.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import polars as pl


def available_instruments(conn: sqlite3.Connection) -> list[str]:
    return [r[0] for r in conn.execute(
        "SELECT DISTINCT instrument FROM minutes ORDER BY instrument"
    ).fetchall()]


def load_minutes(
    conn: sqlite3.Connection, instrument: str, rth_only: bool, years: int | None = None
) -> pl.DataFrame:
    """Chronological 1-minute OHLC. `rth_only` selects the instrument's own
    regular session via the stored `session` tag (per-instrument, so DAX gets
    Xetra hours and US30 gets NYSE hours)."""
    sql = "SELECT ts, open, high, low, close FROM minutes WHERE instrument = ?"
    params: list = [instrument]
    if rth_only:
        sql += " AND session = 'RTH'"
    sql += " ORDER BY ts"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    df = pl.DataFrame(
        rows, schema=["ts", "open", "high", "low", "close"], orient="row"
    ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))

    if years:
        df = df.filter(pl.col("ts") >= df["ts"].max() - timedelta(days=365 * years))
    return df


def load_day_minutes(
    conn: sqlite3.Connection, instrument: str, date: str, rth_only: bool
) -> pl.DataFrame:
    sql = "SELECT ts, open, high, low, close FROM minutes WHERE instrument = ? AND date = ?"
    params: list = [instrument, date]
    if rth_only:
        sql += " AND session = 'RTH'"
    sql += " ORDER BY ts"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(
        rows, schema=["ts", "open", "high", "low", "close"], orient="row"
    ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))
