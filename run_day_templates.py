"""Precompute the normalised per-day RTH profiles for the Day Templates page.

Writes a `day_profiles` table: one row per (instrument, date) holding the
session's scale-invariant shape profile (K normalised bucket high/low/close
arrays, stored as JSON) plus the raw session stats the page's day table needs.

Only the PROFILE is stored, not the classification. The main-leg threshold is
a live control on the page (it's the interesting knob -- it decides how much
wiggle counts as a real leg), and re-running the zigzag over 48 normalised
buckets for a few thousand days is instant, so baking one threshold into the
table would cost flexibility for no gain.

DAX is parsed from raw CSVs rather than read from `minutes` -- it isn't in
the main tables, same situation as run_bars.py / run_range_state.py, and the
per-instrument RTH windows live here for the same reason.

Usage: .venv/Scripts/python.exe run_day_templates.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import polars as pl

from etl.parse import parse_all
from gyrations.day_templates import K_BUCKETS, build_profile

ROOT = Path(__file__).resolve().parent
MIN_BARS = 120  # skip stub sessions (holidays / feed outages)

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
    lo, hi = RTH_WINDOWS[instrument]
    mod = (
        pl.col("ts").dt.hour().cast(pl.Int32) * 60
        + pl.col("ts").dt.minute().cast(pl.Int32)
    )
    rth = minutes.filter((mod >= lo) & (mod < hi)).with_columns(
        pl.col("ts").dt.date().alias("date")
    )

    # Every session first, INCLUDING the short ones. The previous-session
    # context (gap, prev close) has to be measured against the real preceding
    # trading day even when that day was a half-session too short to have a
    # usable shape profile -- otherwise dropping a stub silently makes the
    # next day's gap reach back an extra session and be wrong. Only the
    # profile itself is gated on MIN_BARS.
    days = []
    for (date,), day in rth.group_by(["date"], maintain_order=True):
        o = day["open"].to_list()
        h = day["high"].to_list()
        l = day["low"].to_list()
        c = day["close"].to_list()
        days.append((date, o, h, l, c))

    out = []
    for i, (date, o, h, l, c) in enumerate(days):
        if len(c) < MIN_BARS:
            continue
        prof = build_profile(h, l, c, o, K_BUCKETS)
        if prof is None:
            continue

        prev = days[i - 1] if i >= 1 else None
        prev2 = days[i - 2] if i >= 2 else None
        prev_date = str(prev[0]) if prev else None
        prev_open = prev[1][0] if prev else None
        prev_close = prev[4][-1] if prev else None
        prev_range = (max(prev[2]) - min(prev[3])) if prev else None
        prev2_close = prev2[4][-1] if prev2 else None

        out.append((
            instrument, str(date), WEEKDAYS[date.weekday()],
            o[0], max(h), min(l), c[-1],
            prof["range_pts"], prof["n_bars"],
            prof["open_pos"], prof["close_pos"],
            prof["high_frac"], prof["low_frac"],
            prev_date, prev_open, prev_close, prev_range, prev2_close,
            json.dumps([round(v, 5) for v in prof["h"]]),
            json.dumps([round(v, 5) for v in prof["l"]]),
            json.dumps([round(v, 5) for v in prof["c"]]),
        ))
    return out


COLUMNS = (
    "instrument", "date", "weekday", "rth_open", "rth_high", "rth_low", "rth_close",
    "range_pts", "n_bars", "open_pos", "close_pos", "high_frac", "low_frac",
    "prev_date", "prev_open", "prev_close", "prev_range", "prev2_close",
    "prof_h", "prof_l", "prof_c",
)


def write_table(conn: sqlite3.Connection, rows: list[tuple]) -> None:
    conn.execute("DROP TABLE IF EXISTS day_profiles")
    conn.execute("""
        CREATE TABLE day_profiles (
            instrument TEXT, date TEXT, weekday TEXT,
            rth_open REAL, rth_high REAL, rth_low REAL, rth_close REAL,
            range_pts REAL, n_bars INTEGER,
            open_pos REAL, close_pos REAL, high_frac REAL, low_frac REAL,
            prev_date TEXT, prev_open REAL, prev_close REAL,
            prev_range REAL, prev2_close REAL,
            prof_h TEXT, prof_l TEXT, prof_c TEXT,
            PRIMARY KEY (instrument, date)
        )
    """)
    conn.execute("CREATE INDEX ix_day_profiles_lookup ON day_profiles (instrument, date)")
    placeholders = ", ".join("?" for _ in COLUMNS)
    conn.executemany(
        f"INSERT OR REPLACE INTO day_profiles ({', '.join(COLUMNS)}) VALUES ({placeholders})",
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
                print(f"  skipping {instrument}: no RTH window configured")
                continue
            t = time.time()
            rows = build_for_instrument(load_from_db(conn, instrument), instrument)
            all_rows += rows
            print(f"  {instrument}: {len(rows):,} sessions in {time.time()-t:.1f}s")

        for instrument, raw_dir in RAW_SOURCES.items():
            t = time.time()
            rows = build_for_instrument(load_from_raw(raw_dir), instrument)
            all_rows += rows
            print(f"  {instrument}: {len(rows):,} sessions in {time.time()-t:.1f}s")

        write_table(conn, all_rows)
        print(f"Wrote {len(all_rows):,} day_profiles rows in {time.time()-t0:.1f}s total")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
