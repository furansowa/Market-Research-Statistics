"""Precompute the multi-timeframe range-state table (Gyrational Range page).

Writes a `range_state` table: one row per (instrument, sampled timestamp)
holding the trailing ranges at 1m..1d and the forward ranges at 30m/60m/240m,
in points and %, plus an `in_rth` flag.

Why this is a SEPARATE script and not part of run_etl.py:

  1. DAX is not in the main `minutes`/`sessions` tables and can't simply be
     added -- config.toml has ONE global RTH window (09:30-16:00 ET, correct
     for US30) while DAX's cash session is Xetra 09:00-17:30 CET = 03:00-11:30
     ET. Adding DAX to the main pipeline needs per-instrument session config,
     a real refactor. This script carries its own per-instrument RTH map so
     the range page can study both markets now without disturbing anything.
  2. US30 bars are read from the already-built `minutes` table; DAX bars are
     parsed from its raw CSVs directly.

Buckets are deliberately NOT stored -- they depend on the reference window
and bucket scheme the user picks in the UI, so the page computes them on the
fly (cached) from these raw values.

Usage: .venv/Scripts/python.exe run_range_state.py
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
from gyrations.range_state import compute_range_state, TRAIL_HORIZONS, FWD_HORIZONS

ROOT = Path(__file__).resolve().parent
SAMPLE_EVERY = 15  # minutes between stored sample points

# Per-instrument regular-session window, in ET minutes-from-midnight.
# US30: NYSE 09:30-16:00 ET. DAX: Xetra 09:00-17:30 CET = 03:00-11:30 ET.
RTH_WINDOWS = {
    "US30": (9 * 60 + 30, 16 * 60 - 1),
    "DAX": (3 * 60, 11 * 60 + 30),
}

# Instruments sourced from raw CSVs rather than the `minutes` table.
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
    df = parse_all(Path(raw_dir))
    return df.sort("ts").select("ts", "open", "high", "low", "close")


def build_for_instrument(minutes: pl.DataFrame, instrument: str) -> pl.DataFrame:
    state = compute_range_state(minutes)

    start, end = RTH_WINDOWS[instrument]
    mod = (
        pl.col("ts").dt.hour().cast(pl.Int32) * 60
        + pl.col("ts").dt.minute().cast(pl.Int32)
    )
    state = state.with_columns(
        pl.lit(instrument).alias("instrument"),
        ((mod >= start) & (mod <= end)).cast(pl.Int8).alias("in_rth"),
    )

    keep = ["instrument", "ts", "in_rth", "close"]
    keep += [f"tr{h}_pts" for h in TRAIL_HORIZONS]
    keep += [f"tr{h}_pct" for h in TRAIL_HORIZONS]
    keep += [f"fw{h}_pts" for h in FWD_HORIZONS]
    keep += [f"fw{h}_pct" for h in FWD_HORIZONS]

    return state.select(keep).gather_every(SAMPLE_EVERY)


def write_table(conn: sqlite3.Connection, df: pl.DataFrame) -> None:
    cols = []
    for name, dtype in zip(df.columns, df.dtypes):
        if name == "ts":
            cols.append('"ts" TEXT')
        elif dtype == pl.Utf8:
            cols.append(f'"{name}" TEXT')
        elif dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64):
            cols.append(f'"{name}" INTEGER')
        else:
            cols.append(f'"{name}" REAL')
    conn.execute("DROP TABLE IF EXISTS range_state")
    conn.execute(f'CREATE TABLE range_state ({", ".join(cols)}, PRIMARY KEY (instrument, ts))')
    conn.execute("CREATE INDEX ix_range_state_lookup ON range_state (instrument, in_rth, ts)")

    out = df.with_columns(pl.col("ts").dt.strftime("%Y-%m-%d %H:%M:%S"))
    placeholders = ", ".join("?" for _ in out.columns)
    names = ", ".join(f'"{c}"' for c in out.columns)
    conn.executemany(
        f"INSERT OR REPLACE INTO range_state ({names}) VALUES ({placeholders})", out.rows()
    )
    conn.commit()


def main() -> None:
    t0 = time.time()
    config = tomllib.loads((ROOT / "config.toml").read_text())
    db_path = ROOT / config["data"]["db_path"]
    conn = sqlite3.connect(db_path)

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
            minutes = load_from_db(conn, instrument)
            frames.append(build_for_instrument(minutes, instrument))
            print(f"  {instrument}: {minutes.height:,} bars -> "
                  f"{frames[-1].height:,} samples in {time.time()-t:.1f}s")

        for instrument, raw_dir in RAW_SOURCES.items():
            t = time.time()
            minutes = load_from_raw(raw_dir)
            frames.append(build_for_instrument(minutes, instrument))
            print(f"  {instrument}: {minutes.height:,} bars -> "
                  f"{frames[-1].height:,} samples in {time.time()-t:.1f}s")

        combined = pl.concat(frames, how="vertical")
        write_table(conn, combined)
        print(f"Wrote {combined.height:,} range_state rows in {time.time()-t0:.1f}s total")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
