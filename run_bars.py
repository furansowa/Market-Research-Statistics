"""Precompute higher-timeframe OHLC bars from the 1-minute data.

Writes a `bars` table: one row per (instrument, tf_min, bar start). Generic in
the timeframe rather than hourly-only -- the aggregation is identical for
every TF and past studies in this project have wanted 5/10/30min as well as
hourly.

Bars are clock-aligned in ET (the timezone the raw data is stored in), so the
60-minute bars are 09:00-10:00, 10:00-11:00, ... and DAX's local Xetra hours
land at 03:00-11:30 ET. Sessions are NOT reset: a bar simply contains whatever
minutes exist inside its window.

Two count columns come along so research can filter on bar quality without
going back to the 1-minute data:
  n_min     -- minutes actually present (a full 60m bar has 60; session-edge
               and holiday bars have fewer, and should usually be excluded)
  n_rth_min -- how many of those minutes fell inside that instrument's regular
               session, so RTH/ETH filtering is a WHERE clause rather than a
               judgement call about bars straddling the open

Separate script from run_etl.py for the same reason as run_range_state.py:
DAX isn't in the main `minutes`/`sessions` tables and config.toml carries a
single global RTH window, so per-instrument session handling lives here.

Usage: .venv/Scripts/python.exe run_bars.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import polars as pl

from etl.parse import parse_all

ROOT = Path(__file__).resolve().parent
TIMEFRAMES = [5, 10, 15, 30, 60, 240]

# ET minutes-from-midnight. US30: NYSE 09:30-16:00. DAX: Xetra 09:00-17:30 CET.
RTH_WINDOWS = {
    "US30": (9 * 60 + 30, 16 * 60),
    "DAX": (3 * 60, 11 * 60 + 30),
}

RAW_SOURCES = {
    "DAX": r"D:\Trading\Research-Project-2026-07\2026 - Instruments Data (2008-2026)\DAX-Data",
}


def load_from_db(conn: sqlite3.Connection, instrument: str) -> pl.DataFrame:
    rows = conn.execute(
        "SELECT ts, open, high, low, close FROM minutes WHERE instrument = ? ORDER BY ts",
        (instrument,),
    ).fetchall()
    return pl.DataFrame(
        rows, schema=["ts", "open", "high", "low", "close"], orient="row"
    ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))


def load_from_raw(raw_dir: str) -> pl.DataFrame:
    return parse_all(Path(raw_dir)).sort("ts").select("ts", "open", "high", "low", "close")


def build_for_instrument(minutes: pl.DataFrame, instrument: str) -> pl.DataFrame:
    lo, hi = RTH_WINDOWS[instrument]
    mod = (
        pl.col("ts").dt.hour().cast(pl.Int32) * 60
        + pl.col("ts").dt.minute().cast(pl.Int32)
    )
    minutes = minutes.with_columns(
        ((mod >= lo) & (mod < hi)).cast(pl.Int32).alias("_rth")
    )

    frames = []
    for tf in TIMEFRAMES:
        agg = (
            minutes.with_columns(pl.col("ts").dt.truncate(f"{tf}m").alias("bar_ts"))
            .group_by("bar_ts")
            .agg(
                pl.col("open").first(),
                pl.col("high").max(),
                pl.col("low").min(),
                pl.col("close").last(),
                pl.len().cast(pl.Int32).alias("n_min"),
                pl.col("_rth").sum().cast(pl.Int32).alias("n_rth_min"),
            )
            .sort("bar_ts")
            .rename({"bar_ts": "ts"})
            .with_columns(
                pl.lit(instrument).alias("instrument"),
                pl.lit(tf, dtype=pl.Int32).alias("tf_min"),
            )
        )
        frames.append(
            agg.select("instrument", "tf_min", "ts", "open", "high", "low",
                       "close", "n_min", "n_rth_min")
        )
    return pl.concat(frames, how="vertical")


def write_table(conn: sqlite3.Connection, df: pl.DataFrame) -> None:
    conn.execute("DROP TABLE IF EXISTS bars")
    conn.execute("""
        CREATE TABLE bars (
            instrument TEXT, tf_min INTEGER, ts TEXT,
            open REAL, high REAL, low REAL, close REAL,
            n_min INTEGER, n_rth_min INTEGER,
            PRIMARY KEY (instrument, tf_min, ts)
        )
    """)
    conn.execute("CREATE INDEX ix_bars_lookup ON bars (instrument, tf_min, ts)")

    out = df.with_columns(pl.col("ts").dt.strftime("%Y-%m-%d %H:%M:%S"))
    names = ", ".join(f'"{c}"' for c in out.columns)
    placeholders = ", ".join("?" for _ in out.columns)
    conn.executemany(
        f"INSERT OR REPLACE INTO bars ({names}) VALUES ({placeholders})", out.rows()
    )
    conn.commit()


def main() -> None:
    t0 = time.time()
    config = tomllib.loads((ROOT / "config.toml").read_text())
    conn = sqlite3.connect(ROOT / config["data"]["db_path"])

    frames = []
    try:
        db_instruments = [
            r[0] for r in conn.execute("SELECT DISTINCT instrument FROM minutes").fetchall()
        ]
        for instrument in db_instruments:
            if instrument not in RTH_WINDOWS:
                print(f"  skipping {instrument}: no RTH window configured")
                continue
            t = time.time()
            frames.append(build_for_instrument(load_from_db(conn, instrument), instrument))
            print(f"  {instrument}: {frames[-1].height:,} bars in {time.time()-t:.1f}s")

        for instrument, raw_dir in RAW_SOURCES.items():
            t = time.time()
            frames.append(build_for_instrument(load_from_raw(raw_dir), instrument))
            print(f"  {instrument}: {frames[-1].height:,} bars in {time.time()-t:.1f}s")

        combined = pl.concat(frames, how="vertical")
        write_table(conn, combined)
        print(f"Wrote {combined.height:,} bars rows in {time.time()-t0:.1f}s total\n")

        for inst in combined["instrument"].unique().sort():
            for tf in TIMEFRAMES:
                sub = combined.filter(
                    (pl.col("instrument") == inst) & (pl.col("tf_min") == tf)
                )
                full = sub.filter(pl.col("n_min") == tf).height
                print(f"  {inst:5s} {tf:>4d}m: {sub.height:>8,} bars "
                      f"({full:,} complete)  {sub['ts'].min()} .. {sub['ts'].max()}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
