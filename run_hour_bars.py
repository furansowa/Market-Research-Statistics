"""Precompute RTH-open-ANCHORED hourly bars (for the Hourly Composite page).

Different from `bars` (run_bars.py): that table is clock-aligned (09:00-10:00,
10:00-11:00, ...). This one is anchored to each instrument's own RTH open, so
bar_num=1 is exactly the session's first hour regardless of what the wall
clock reads -- 09:30-10:30 for US30 (RTH opens 09:30), 09:00-10:00 CET =
03:00-04:00 ET for DAX (Xetra opens 09:00 CET). Only FULL 60-minute bars are
kept; a trailing partial hour (e.g. US30's 15:30-16:00) is dropped rather than
stored short.

Usage: .venv/Scripts/python.exe run_hour_bars.py
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
MIN_BARS_FOR_FULL = 60  # a bar must have all 60 minutes present

# (RTH open, RTH close), ET minutes-from-midnight.
RTH_WINDOWS = {
    "US30": (9 * 60 + 30, 16 * 60),
    "DAX": (3 * 60, 11 * 60 + 30),
}

RAW_SOURCES = {
    "DAX": r"D:\Trading\Research-Project-2026-07\2026 - Instruments Data (2008-2026)\DAX-Data",
}

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


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


def build_for_instrument(minutes: pl.DataFrame, instrument: str) -> list[tuple]:
    rth_open, rth_close = RTH_WINDOWS[instrument]
    n_hours = (rth_close - rth_open) // 60

    mod = (
        pl.col("ts").dt.hour().cast(pl.Int32) * 60
        + pl.col("ts").dt.minute().cast(pl.Int32)
    )
    minutes = minutes.with_columns(
        mod.alias("_mod"), pl.col("ts").dt.date().alias("_date")
    ).filter((mod >= rth_open) & (mod < rth_open + n_hours * 60))

    out = []
    for bar_num in range(1, n_hours + 1):
        lo = rth_open + (bar_num - 1) * 60
        hi = lo + 60
        sub = minutes.filter((pl.col("_mod") >= lo) & (pl.col("_mod") < hi))
        agg = (
            sub.group_by("_date", maintain_order=True)
            .agg(
                pl.col("open").first(), pl.col("high").max(),
                pl.col("low").min(), pl.col("close").last(),
                pl.len().cast(pl.Int32).alias("n_min"),
            )
            .filter(pl.col("n_min") == MIN_BARS_FOR_FULL)
            .sort("_date")
        )
        for date, o, h, l, c, n_min in agg.iter_rows():
            out.append((instrument, bar_num, str(date), WEEKDAYS[date.weekday()],
                       o, h, l, c, n_min))
    return out


def write_table(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.execute("DROP TABLE IF EXISTS session_hour_bars")
    conn.execute("""
        CREATE TABLE session_hour_bars (
            instrument TEXT, bar_num INTEGER, date TEXT, weekday TEXT,
            open REAL, high REAL, low REAL, close REAL, n_min INTEGER,
            PRIMARY KEY (instrument, bar_num, date)
        )
    """)
    conn.execute("CREATE INDEX ix_session_hour_bars ON session_hour_bars (instrument, bar_num, date)")
    conn.executemany(
        "INSERT OR REPLACE INTO session_hour_bars "
        "(instrument, bar_num, date, weekday, open, high, low, close, n_min) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()


def main() -> None:
    t0 = time.time()
    config = tomllib.loads((ROOT / "config.toml").read_text())
    conn = sqlite3.connect(ROOT / config["data"]["db_path"])

    all_rows: list[tuple] = []
    try:
        db_instruments = [
            r[0] for r in conn.execute("SELECT DISTINCT instrument FROM minutes").fetchall()
        ]
        for instrument in db_instruments:
            if instrument not in RTH_WINDOWS:
                continue
            t = time.time()
            rows = build_for_instrument(load_from_db(conn, instrument), instrument)
            all_rows += rows
            print(f"  {instrument}: {len(rows):,} hour-bars in {time.time()-t:.1f}s")

        for instrument, raw_dir in RAW_SOURCES.items():
            t = time.time()
            rows = build_for_instrument(load_from_raw(raw_dir), instrument)
            all_rows += rows
            print(f"  {instrument}: {len(rows):,} hour-bars in {time.time()-t:.1f}s")

        write_table(conn, all_rows)
        print(f"\nWrote {len(all_rows):,} session_hour_bars rows in {time.time()-t0:.1f}s total\n")

        for inst in sorted({r[0] for r in all_rows}):
            n_hours = (RTH_WINDOWS[inst][1] - RTH_WINDOWS[inst][0]) // 60
            for bar_num in range(1, n_hours + 1):
                n = sum(1 for r in all_rows if r[0] == inst and r[1] == bar_num)
                lo = RTH_WINDOWS[inst][0] + (bar_num - 1) * 60
                print(f"  {inst:5s} bar{bar_num}: {n:5,d} days   "
                      f"({lo//60:02d}:{lo%60:02d}-{(lo+60)//60:02d}:{(lo+60)%60:02d} ET)")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
