"""Precompute fixed-price-increment range bars (Renko-style) and store them.

Writes a `range_bars` table: one row per completed brick, for each
(instrument, brick_size) pair, over the last SPAN_YEARS of data. Separate
script, same reasoning as run_range_state.py -- DAX isn't in the main
`minutes` table, so it's parsed from raw CSVs directly; US30 is read from
`minutes`. No session filtering: bricks form continuously through
overnight/weekend gaps, same as a real range-bar chart.

Usage: .venv/Scripts/python.exe run_range_bars.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
import tomllib
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import polars as pl

from etl.parse import parse_all
from gyrations.range_bars import build_range_bars

ROOT = Path(__file__).resolve().parent
SPAN_YEARS = 3
BRICK_SIZES = [20, 40, 100]

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


def last_n_years(minutes: pl.DataFrame, years: int) -> pl.DataFrame:
    cutoff = minutes["ts"].max() - timedelta(days=365 * years)
    return minutes.filter(pl.col("ts") >= cutoff)


def build_for_instrument(minutes: pl.DataFrame, instrument: str) -> pl.DataFrame:
    minutes = last_n_years(minutes, SPAN_YEARS)
    bars = minutes.select("ts", "open", "high", "low", "close").rows()

    frames = []
    for size in BRICK_SIZES:
        bricks = build_range_bars(bars, float(size))
        if not bricks:
            continue
        frames.append(pl.DataFrame({
            "instrument": [instrument] * len(bricks),
            "brick_size": [size] * len(bricks),
            "bar_index": list(range(len(bricks))),
            "start_ts": [b.start_ts for b in bricks],
            "end_ts": [b.end_ts for b in bricks],
            "open": [b.open for b in bricks],
            "high": [b.high for b in bricks],
            "low": [b.low for b in bricks],
            "close": [b.close for b in bricks],
            "direction": [b.direction for b in bricks],
            "n_source_bars": [b.n_source_bars for b in bricks],
        }))
    return pl.concat(frames, how="vertical")


def write_table(conn: sqlite3.Connection, df: pl.DataFrame) -> None:
    conn.execute("DROP TABLE IF EXISTS range_bars")
    conn.execute("""
        CREATE TABLE range_bars (
            instrument TEXT, brick_size INTEGER, bar_index INTEGER,
            start_ts TEXT, end_ts TEXT,
            open REAL, high REAL, low REAL, close REAL,
            direction TEXT, n_source_bars INTEGER,
            PRIMARY KEY (instrument, brick_size, bar_index)
        )
    """)
    conn.execute("CREATE INDEX ix_range_bars_lookup ON range_bars (instrument, brick_size, end_ts)")

    out = df.with_columns(
        pl.col("start_ts").dt.strftime("%Y-%m-%d %H:%M:%S"),
        pl.col("end_ts").dt.strftime("%Y-%m-%d %H:%M:%S"),
    )
    names = ", ".join(f'"{c}"' for c in out.columns)
    placeholders = ", ".join("?" for _ in out.columns)
    conn.executemany(
        f"INSERT OR REPLACE INTO range_bars ({names}) VALUES ({placeholders})", out.rows()
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
            t = time.time()
            minutes = load_from_db(conn, instrument)
            frames.append(build_for_instrument(minutes, instrument))
            print(f"  {instrument}: {minutes.height:,} min bars -> "
                  f"{frames[-1].height:,} range bars in {time.time()-t:.1f}s")

        for instrument, raw_dir in RAW_SOURCES.items():
            t = time.time()
            minutes = load_from_raw(raw_dir)
            frames.append(build_for_instrument(minutes, instrument))
            print(f"  {instrument}: {minutes.height:,} min bars -> "
                  f"{frames[-1].height:,} range bars in {time.time()-t:.1f}s")

        combined = pl.concat(frames, how="vertical")
        write_table(conn, combined)
        print(f"Wrote {combined.height:,} range_bars rows in {time.time()-t0:.1f}s total")

        for inst in combined["instrument"].unique().sort():
            for size in BRICK_SIZES:
                sub = combined.filter(
                    (pl.col("instrument") == inst) & (pl.col("brick_size") == size)
                )
                if sub.height == 0:
                    continue
                print(f"  {inst} @ {size}pt: {sub.height:,} bars, "
                      f"{sub['start_ts'].min()} .. {sub['end_ts'].max()}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
