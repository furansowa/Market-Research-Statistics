"""Incremental ETL step: add `continuous`-scope gyration legs (both modes,
all configured thresholds) without repeating the full pipeline.

`continuous` scope was added to config.toml's [gyrations] precompute list
after the main DB was already built. Re-running run_etl.py would re-parse
5.7M raw bars and redo sessions/shapes/market-profile for no reason, since
none of that is affected by adding a new gyrations scope. This script reads
the already-parsed `minutes` table straight from the DB and computes only
the new (scope="continuous", mode=...) rows.

Usage: .venv/Scripts/python.exe run_gyrations_continuous.py
"""

from __future__ import annotations

import sys
import time
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

import polars as pl

from etl.load import get_connection, create_gyrations_table, delete_gyrations_scope, write_gyrations_rows
from etl.gyrations import compute_continuous_scope_legs


def main() -> None:
    root = Path(__file__).resolve().parent
    config = tomllib.loads((root / "config.toml").read_text())
    db_path = root / config["data"]["db_path"]

    gyr_config = config["gyrations"]
    thresholds = gyr_config["thresholds"]
    tiebreak = gyr_config["intrabar_tiebreak"]
    continuous_modes = [
        entry["mode"] for entry in gyr_config["precompute"] if entry["scope"] == "continuous"
    ]
    if not continuous_modes:
        print("No scope='continuous' entries in config.toml [gyrations] precompute -- nothing to do.")
        return

    t0 = time.time()
    conn = get_connection(db_path)
    try:
        create_gyrations_table(conn)

        instruments = [r[0] for r in conn.execute("SELECT DISTINCT instrument FROM minutes").fetchall()]
        for instrument in instruments:
            rows = conn.execute(
                "SELECT ts, open, high, low, close FROM minutes WHERE instrument = ? ORDER BY ts",
                (instrument,),
            ).fetchall()
            minutes = pl.DataFrame(
                rows, schema=["ts", "open", "high", "low", "close"], orient="row"
            ).with_columns(pl.col("ts").str.to_datetime("%Y-%m-%d %H:%M:%S"))

            for mode in continuous_modes:
                delete_gyrations_scope(conn, instrument, "continuous", mode)
                t_scope = time.time()
                total_legs = 0
                for threshold in thresholds:
                    leg_rows = compute_continuous_scope_legs(
                        minutes, instrument, threshold, mode, tiebreak=tiebreak
                    )
                    write_gyrations_rows(conn, leg_rows)
                    total_legs += len(leg_rows)
                conn.commit()
                print(
                    f"  {instrument} continuous/{mode}: {total_legs:,} legs across "
                    f"{len(thresholds)} thresholds in {time.time() - t_scope:.1f}s"
                )
    finally:
        conn.close()

    print(f"Done in {time.time() - t0:.1f}s total")


if __name__ == "__main__":
    main()
