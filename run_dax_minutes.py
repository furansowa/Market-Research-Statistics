"""Ingest DAX 1-minute bars into the `minutes` table.

Until now `minutes` held US30 only; DAX lived in the DB solely as aggregated
`bars` rows (5min and coarser), which makes any 1-minute study impossible
without re-parsing CSVs by hand. This loads the canonical raw CSVs -- the same
files `run_bars.py` already builds DAX bars from, via the same `parse_all`
code path used for US30 -- so both instruments sit in one table with identical
semantics.

Session tagging is PER-INSTRUMENT here, which `etl.parse.tag_sessions`
deliberately is not: that function takes one global rth_start/rth_close_bar
from config (09:30/15:59, i.e. NYSE) and applying it to DAX would silently
label the wrong 6.5 hours as RTH. DAX's Xetra session is 09:00-17:30 CET =
03:00-11:30 ET, matching `run_bars.RTH_WINDOWS` exactly so `n_rth_min` in the
`bars` table and `session` here agree.

VERIFICATION: aggregating the newly written 1-minute rows back up to 5 minutes
must reproduce the existing `bars` table bit for bit -- both derive from the
same CSVs, so any mismatch means the parse or the timezone handling drifted.
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

import polars as pl

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from etl.parse import parse_all
from etl.load import write_minutes

DB_PATH = ROOT / "data" / "db" / "lookup.sqlite"
RAW_DIR = Path(r"D:\Trading\Research-Project-2026-07\2026 - Instruments Data (2008-2026)\DAX-Data")

INSTRUMENT = "DAX"
# The raw CSVs carry the vendor ticker in their header; `run_bars.py` relabels
# it to "DAX" by fiat when building bars from the same directory, so this does
# the same rather than introducing a second name for one instrument.
RAW_TICKER = "GER30"
# ET minutes-from-midnight, half-open [lo, hi) -- identical to run_bars.RTH_WINDOWS
RTH_LO, RTH_HI = 3 * 60, 11 * 60 + 30


def tag_dax_sessions(df: pl.DataFrame) -> pl.DataFrame:
    """`date` + `session` columns using DAX's own RTH window, weekends dropped
    (same weekday rule as etl.parse.tag_sessions)."""
    mod = pl.col("ts").dt.hour().cast(pl.Int32) * 60 + pl.col("ts").dt.minute().cast(pl.Int32)
    return (
        df.with_columns(
            pl.col("ts").dt.date().alias("date"),
            pl.col("ts").dt.weekday().alias("_weekday"),
            mod.alias("_mod"),
        )
        .filter(pl.col("_weekday") <= 5)
        .with_columns(
            pl.when((pl.col("_mod") >= RTH_LO) & (pl.col("_mod") < RTH_HI))
            .then(pl.lit("RTH")).otherwise(pl.lit("ETH")).alias("session")
        )
        .drop(["_weekday", "_mod"])
    )


def verify_against_bars(conn: sqlite3.Connection) -> bool:
    """Re-aggregate the stored 1-minute rows to 5 minutes and compare against
    the pre-existing `bars` table, which was built from the same CSVs."""
    print("VERIFICATION -- 1min re-aggregated to 5min vs existing `bars` table")
    rows = conn.execute(
        "SELECT ts, open, high, low, close FROM minutes WHERE instrument = ? ORDER BY ts",
        (INSTRUMENT,),
    ).fetchall()
    m = pl.DataFrame(rows, schema=["ts", "open", "high", "low", "close"], orient="row")
    m = m.with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))
    agg = (
        m.with_columns(pl.col("ts").dt.truncate("5m").alias("bucket"))
        .group_by("bucket")
        .agg(
            pl.col("open").first().alias("open"),
            pl.col("high").max().alias("high"),
            pl.col("low").min().alias("low"),
            pl.col("close").last().alias("close"),
            pl.len().alias("n_min"),
        )
        .sort("bucket")
        .filter(pl.col("n_min") == 5)
    )

    brows = conn.execute(
        "SELECT ts, open, high, low, close FROM bars WHERE instrument = ? AND tf_min = 5 "
        "AND n_min = 5 ORDER BY ts",
        (INSTRUMENT,),
    ).fetchall()
    b = pl.DataFrame(brows, schema=["bucket", "open", "high", "low", "close"], orient="row")
    b = b.with_columns(pl.col("bucket").str.to_datetime("%Y-%m-%d %H:%M:%S"))

    # `bars` was built straight from the raw CSVs and therefore KEEPS Sunday
    # bars, whereas `minutes` drops weekends for both instruments (US30 via
    # etl.parse.tag_sessions' ignore_weekends, DAX via tag_dax_sessions above).
    # Comparing against unfiltered `bars` would flag that intended difference
    # as a data error, so weekends are excluded from the comparison and
    # reported separately.
    n_weekend = b.filter(pl.col("bucket").dt.weekday() > 5).height
    b = b.filter(pl.col("bucket").dt.weekday() <= 5)

    joined = agg.join(b, on="bucket", how="inner", suffix="_db")
    if joined.is_empty():
        print("   FAIL  no overlapping 5-minute buckets to compare")
        return False

    ok = True
    for col in ("open", "high", "low", "close"):
        dev = (joined[col] - joined[f"{col}_db"]).abs().max()
        good = dev is not None and dev < 1e-9
        ok &= good
        print(f"   {'PASS' if good else 'FAIL'}  {col}: max deviation {dev}")

    cov = len(joined) / len(b) * 100
    good = cov > 99.99
    ok &= good
    print(f"   {'PASS' if good else 'FAIL'}  coverage: {len(joined):,} of {len(b):,} "
          f"complete weekday `bars` rows matched ({cov:.4f}%)")
    print(f"   note: {n_weekend:,} Sunday `bars` rows excluded from the comparison "
          f"by design (weekends are not stored in `minutes`)")
    print(f"   => {'ALL PASSED' if ok else 'FAILED'}\n")
    return ok


def main() -> None:
    t0 = time.time()
    print(f"Parsing DAX CSVs from {RAW_DIR}")
    minutes = parse_all(RAW_DIR)
    print(f"  {minutes.height:,} minute bars parsed in {time.time() - t0:.1f}s")
    print(f"  instruments found: {minutes['instrument'].unique().to_list()}")

    found = minutes["instrument"].unique().to_list()
    if found != [RAW_TICKER]:
        print(f"ERROR: expected only {RAW_TICKER!r} in {RAW_DIR}, found {found}")
        return
    minutes = minutes.with_columns(pl.lit(INSTRUMENT).alias("instrument"))

    minutes = tag_dax_sessions(minutes)
    minutes = minutes.select("instrument", "ts", "open", "high", "low", "close", "date", "session")
    print(f"  {minutes.height:,} bars after weekend filter  "
          f"({minutes['ts'].min()} .. {minutes['ts'].max()})")
    print(f"  session split: "
          f"{dict(zip(*minutes['session'].value_counts().to_dict().values()))}")

    conn = sqlite3.connect(DB_PATH)
    existing = conn.execute(
        "SELECT COUNT(*) FROM minutes WHERE instrument = ?", (INSTRUMENT,)
    ).fetchone()[0]
    print(f"\n  existing DAX rows in `minutes`: {existing:,} (will be replaced)")

    t1 = time.time()
    write_minutes(conn, minutes)
    print(f"  wrote {minutes.height:,} rows in {time.time() - t1:.1f}s\n")

    ok = verify_against_bars(conn)

    for inst in ("US30", "DAX"):
        n, lo, hi = conn.execute(
            "SELECT COUNT(*), MIN(ts), MAX(ts) FROM minutes WHERE instrument = ?", (inst,)
        ).fetchone()
        print(f"  minutes[{inst}]: {n:,} rows  {lo} .. {hi}")
    conn.close()

    print(f"\n{'DONE' if ok else 'DONE WITH FAILURES'} in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
