"""Derive macro shape tables from the built DB (no raw reprocessing).

Reads confirmed rth/extreme_to_extreme legs from `gyrations` at thresholds
40/120/200, classifies every session's macro shape (src/gyrations/shapes.py),
and writes two tables to data/db/lookup.sqlite:

- session_shapes: one row per (instrument, date, threshold) — the template
  name, swing string, counts, and the fade from the final extreme to close.
- shape_swings: one row per macro swing — the yellow-line segments: start/end
  ts+price, size in points, duration in minutes.

Also derives PivotPattern (a distinct, simpler concept from the macro shape
above -- every CONFIRMED leg's own endpoint, not the shape's merged macro
swings): one '1'/'0' digit per confirmed leg, in order, '1' if that leg's end
price is >= the session's RTH open else '0'. E.g. 4 legs ending at +30, -95,
+150, +20 relative to open -> "1011". Persisted straight onto `sessions` as
pivot_pattern_40/120/200 (no separate backing table -- it's simple enough to
carry as a plain per-session string, unlike the macro-shape swing geometry).

Usage: .venv/Scripts/python.exe run_shapes.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from gyrations.shapes import classify_session

DB_PATH = Path(__file__).resolve().parent / "data" / "db" / "lookup.sqlite"
THRESHOLDS = (40, 120, 200)
SCOPE = "rth"
MODE = "extreme_to_extreme"


def _minutes_between(ts_a: str, ts_b: str) -> float:
    return (datetime.fromisoformat(ts_b) - datetime.fromisoformat(ts_a)).total_seconds() / 60


def main() -> None:
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("DROP TABLE IF EXISTS session_shapes")
    cur.execute("DROP TABLE IF EXISTS shape_swings")
    cur.execute(
        """CREATE TABLE session_shapes (
            instrument TEXT NOT NULL,
            date TEXT NOT NULL,
            threshold REAL NOT NULL,
            shape TEXT NOT NULL,
            swings TEXT NOT NULL,
            n_swings INTEGER NOT NULL,
            n_legs INTEGER NOT NULL,
            fade_pts REAL,
            last_extreme_ts TEXT,
            PRIMARY KEY (instrument, date, threshold)
        )"""
    )
    cur.execute(
        """CREATE TABLE shape_swings (
            instrument TEXT NOT NULL,
            date TEXT NOT NULL,
            threshold REAL NOT NULL,
            swing_index INTEGER NOT NULL,
            direction TEXT NOT NULL,
            start_ts TEXT NOT NULL,
            end_ts TEXT NOT NULL,
            start_price REAL NOT NULL,
            end_price REAL NOT NULL,
            size_pts REAL NOT NULL,
            duration_min REAL NOT NULL,
            PRIMARY KEY (instrument, date, threshold, swing_index)
        )"""
    )

    instruments = [r["instrument"] for r in cur.execute("SELECT DISTINCT instrument FROM sessions")]
    n_shape_rows = n_swing_rows = 0
    pivot_pattern_updates: dict[float, list[tuple]] = {t: [] for t in THRESHOLDS}

    for instrument in instruments:
        sessions = {
            r["date"]: dict(r)
            for r in conn.execute(
                "SELECT date, rth_open, rth_close FROM sessions WHERE instrument=? ORDER BY date",
                (instrument,),
            )
        }
        for threshold in THRESHOLDS:
            legs_by_date: dict[str, list[dict]] = defaultdict(list)
            for r in conn.execute(
                "SELECT start_date, direction, start_price, end_price, start_ts, end_ts "
                "FROM gyrations WHERE instrument=? AND scope=? AND mode=? AND threshold=? "
                "AND confirmed=1 ORDER BY start_ts",
                (instrument, SCOPE, MODE, threshold),
            ):
                legs_by_date[r["start_date"]].append(dict(r))

            shape_rows = []
            swing_rows = []
            for date, sess in sessions.items():
                day_legs = legs_by_date.get(date, [])
                res = classify_session(day_legs)
                pivots = res["pivots"]
                fade = last_ts = None
                if pivots:
                    last_ts, last_price, _ = pivots[-1]
                    fade = sess["rth_close"] - last_price
                shape_rows.append((
                    instrument, date, threshold, res["shape"], res["swings"],
                    res["n_swings"], res["n_legs"], fade, last_ts,
                ))
                prev = res["path_start"]
                for i, (ts, price, side) in enumerate(pivots):
                    p_ts, p_price, _ = prev
                    swing_rows.append((
                        instrument, date, threshold, i, "U" if side == "H" else "D",
                        p_ts, ts, p_price, price, abs(price - p_price),
                        _minutes_between(p_ts, ts),
                    ))
                    prev = (ts, price, side)

                if day_legs and sess["rth_open"] is not None:
                    pattern = "".join(
                        "1" if leg["end_price"] >= sess["rth_open"] else "0" for leg in day_legs
                    )
                    pivot_pattern_updates[threshold].append((pattern, instrument, date))

            cur.executemany(
                "INSERT INTO session_shapes VALUES (?,?,?,?,?,?,?,?,?)", shape_rows
            )
            cur.executemany(
                "INSERT INTO shape_swings VALUES (?,?,?,?,?,?,?,?,?,?,?)", swing_rows
            )
            n_shape_rows += len(shape_rows)
            n_swing_rows += len(swing_rows)
            print(f"{instrument} T={threshold}: {len(shape_rows)} sessions, {len(swing_rows)} swings")

    # persist per-threshold shape as sessions columns (shape_40/120/200) so
    # the dashboard gets table/filter/offset parity like the leg-count columns
    for threshold in THRESHOLDS:
        col = f"shape_{int(threshold)}"
        try:
            cur.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists (rerun without ETL rebuild)
        cur.execute(
            f"UPDATE sessions SET {col} = ("
            "SELECT shape FROM session_shapes ss WHERE ss.instrument = sessions.instrument "
            "AND ss.date = sessions.date AND ss.threshold = ?)",
            (threshold,),
        )

    # persist PivotPattern per threshold -- no separate backing table, so the
    # rows collected above are pushed straight onto `sessions` here.
    n_pivot_rows = 0
    for threshold in THRESHOLDS:
        col = f"pivot_pattern_{int(threshold)}"
        try:
            cur.execute(f"ALTER TABLE sessions ADD COLUMN {col} TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists (rerun without ETL rebuild)
        # Reset to NULL first so a rerun can't leave a stale pattern behind on
        # a date that no longer has any confirmed legs at this threshold.
        cur.execute(f"UPDATE sessions SET {col} = NULL")
        rows = pivot_pattern_updates[threshold]
        cur.executemany(
            f"UPDATE sessions SET {col} = ? WHERE instrument = ? AND date = ?", rows
        )
        n_pivot_rows += len(rows)

    conn.commit()
    conn.close()
    print(f"done: {n_shape_rows} session_shapes rows, {n_swing_rows} shape_swings rows, "
          f"{n_pivot_rows} pivot_pattern updates in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
