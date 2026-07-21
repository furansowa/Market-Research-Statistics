"""ETL entry point: raw CSVs -> data/db/lookup.sqlite.

Usage: .venv/Scripts/python.exe run_etl.py
"""

from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import polars as pl

from etl.parse import parse_all, tag_sessions
from etl.sessions import build_sessions
from etl.load import (
    get_connection,
    write_minutes,
    write_sessions,
    create_gyrations_table,
    delete_gyrations_scope,
    write_gyrations_rows,
)
from etl.gyrations import compute_session_scope_legs, compute_continuous_scope_legs
from etl.leg_windows import attach_leg_count_columns


def main() -> None:
    root = Path(__file__).resolve().parent
    config = tomllib.loads((root / "config.toml").read_text())

    raw_dir = Path(config["data"]["raw_dir"])
    db_path = root / config["data"]["db_path"]
    ignore_weekends = config["session"]["ignore_weekends"]
    rth_start = config["session"]["rth_start"]
    rth_close_bar = config["session"]["rth_close_bar"]
    half_day_flag_before = config["session"]["half_day_flag_before"]

    t0 = time.time()
    print(f"Parsing raw CSVs from {raw_dir} ...")
    minutes = parse_all(raw_dir)
    print(f"  {minutes.height:,} minute bars parsed in {time.time() - t0:.1f}s")

    t1 = time.time()
    minutes = tag_sessions(
        minutes, rth_start=rth_start, rth_close_bar=rth_close_bar, ignore_weekends=ignore_weekends
    )
    print(f"Tagged RTH/ETH in {time.time() - t1:.1f}s")

    t2 = time.time()
    sessions = build_sessions(minutes, half_day_flag_before=half_day_flag_before, windows=config["windows"])
    print(f"Built {sessions.height:,} sessions in {time.time() - t2:.1f}s")

    gyr_config = config["gyrations"]
    thresholds = gyr_config["thresholds"]
    tiebreak = gyr_config["intrabar_tiebreak"]
    instruments = minutes["instrument"].unique().sort().to_list()
    all_rth_bars = minutes.filter(pl.col("session") == "RTH")

    # Gyrations v2.0 page: the 9 leg-count-in-window columns need
    # rth/extreme_to_extreme legs at 40/120/200 to exist *before* sessions is
    # written, so they can be persisted as real columns (see etl/leg_windows.py).
    # These 3 thresholds get recomputed again below when the main precompute
    # loop reaches the new rth/extreme_to_extreme entry -- a deliberate,
    # near-zero-cost duplication rather than threading this pre-step's rows
    # into that loop's per-threshold structure.
    t2b = time.time()
    legs_by_threshold: dict[float, list[dict]] = {40: [], 120: [], 200: []}
    for instrument in instruments:
        rth_bars = all_rth_bars.filter(pl.col("instrument") == instrument)
        for threshold in legs_by_threshold:
            legs_by_threshold[threshold].extend(
                compute_session_scope_legs(
                    rth_bars, instrument, "rth", threshold,
                    mode="extreme_to_extreme", tiebreak=tiebreak,
                )
            )
    sessions = attach_leg_count_columns(sessions, all_rth_bars, legs_by_threshold)
    print(f"Computed leg-count window columns in {time.time() - t2b:.1f}s")

    t3 = time.time()
    conn = get_connection(db_path)
    try:
        write_minutes(conn, minutes)
        write_sessions(conn, sessions)
        conn.commit()
        print(f"Wrote minutes+sessions to {db_path} in {time.time() - t3:.1f}s")

        t4 = time.time()
        create_gyrations_table(conn)

        for instrument in instruments:
            rth_bars = all_rth_bars.filter(pl.col("instrument") == instrument)
            eth_bars = minutes.filter(pl.col("instrument") == instrument)

            for entry in gyr_config["precompute"]:
                scope, mode = entry["scope"], entry["mode"]
                delete_gyrations_scope(conn, instrument, scope, mode)

                if scope == "rth":
                    bars = rth_bars
                elif scope == "eth":
                    bars = eth_bars
                else:
                    bars = eth_bars  # continuous: same source, no per-date partitioning

                t_scope = time.time()
                total_legs = 0
                for threshold in thresholds:
                    if scope == "continuous":
                        rows = compute_continuous_scope_legs(
                            bars, instrument, threshold, mode, tiebreak=tiebreak
                        )
                    else:
                        rows = compute_session_scope_legs(
                            bars, instrument, scope, threshold, mode, tiebreak=tiebreak
                        )
                    write_gyrations_rows(conn, rows)
                    total_legs += len(rows)
                conn.commit()
                print(
                    f"  {instrument} {scope}/{mode}: {total_legs:,} legs across "
                    f"{len(thresholds)} thresholds in {time.time() - t_scope:.1f}s"
                )
        print(f"Computed gyrations in {time.time() - t4:.1f}s")
    finally:
        conn.close()

    # macro shape tables + sessions.shape_40/120/200 columns — derived from
    # the gyrations table just written, so must run after it (own connection)
    import run_shapes
    run_shapes.main()

    print(f"Done in {time.time() - t0:.1f}s total")


if __name__ == "__main__":
    main()
