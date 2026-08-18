"""Data loading for the Gyrational Stats page.

Pattern detection runs on ETH (24h) 5-minute bars, but the volatility
normaliser is built from RTH session ranges -- so both are loaded here, and
both are keyed to CET dates so a setup's date and its normaliser agree even
across the weeks when US and EU daylight-saving transitions are out of step.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

import polars as pl

from query.bars import load_bars
from gyrations.gyr_stats import ET, CET

# RTH session-range history needed before the first usable setup date
_WARMUP_DAYS = 20


def _cet_date(ts) -> date:
    return ts.replace(tzinfo=ET).astimezone(CET).date()


def load_eth_bars(conn: sqlite3.Connection, instrument: str, years: int) -> pl.DataFrame:
    """Complete 5-minute bars, all hours, most recent `years`."""
    df = load_bars(conn, instrument, 5, complete_only=True, rth_only=False)
    if df.is_empty():
        return df
    cut = df["ts"].max() - timedelta(days=365 * years)
    return df.filter(pl.col("ts") >= cut)


def load_session_ranges(conn: sqlite3.Connection, instrument: str, years: int) -> dict:
    """{CET date: that session's RTH high-low range}, with warm-up so the
    5-session normaliser is defined from the first setup date onward."""
    df = load_bars(conn, instrument, 5, complete_only=True, rth_only=True)
    if df.is_empty():
        return {}
    cut = df["ts"].max() - timedelta(days=365 * years + _WARMUP_DAYS)
    df = df.filter(pl.col("ts") >= cut).with_columns(
        pl.col("ts").map_elements(_cet_date, return_dtype=pl.Date).alias("cet_date")
    )
    agg = df.group_by("cet_date").agg(
        (pl.col("high").max() - pl.col("low").min()).alias("rng")
    )
    return dict(zip(agg["cet_date"].to_list(), agg["rng"].to_list()))
