"""Derive Point of Control / Value Area columns onto `sessions` (no raw
reprocessing -- reads the already-built `minutes` table).

For every session, computes its own POC/VA70Hi/VA70Lo (src/gyrations/
market_profile.py, real time-at-price expansion -- not the fixed-%-of-range
approximation from the ProRealTime script this was modeled on), plus 9
derived comparison columns against the immediately preceding session (plain
shift(1) semantics throughout, matching every other "prev_X" column in this
app -- a session with no computable POC produces nulls that propagate exactly
one day forward, never a stale carry-forward). va_prev_va is a 6-way symbolic
TEXT code ("1"/"-1"/"0"/"11"/"-11"/"111"), not a magnitude -- see
classify_va_relationship's docstring for the exact case definitions.

Persisted directly onto `sessions` (ALTER TABLE + UPDATE), same pattern as
run_shapes.py's shape_40/120/200 and pivot_pattern_40/120/200 -- these are
plain pass-through FeatureSpecs in registry.py, no compute= lambda, because
they need the full per-bar minutes data, not just the sessions row.

Usage: .venv/Scripts/python.exe run_market_profile.py
"""

from __future__ import annotations

import sqlite3
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from gyrations.market_profile import compute_market_profile, classify_va_relationship

DB_PATH = Path(__file__).resolve().parent / "data" / "db" / "lookup.sqlite"

# sessions column name -> SQL type, in insertion order
COLUMNS = [
    ("poc", "REAL"), ("va70_hi", "REAL"), ("va70_lo", "REAL"),
    ("o_prev_poc", "REAL"), ("o_prev_va", "INTEGER"),
    ("h_prev_va", "INTEGER"), ("l_prev_va", "INTEGER"),
    ("cl_poc", "REAL"), ("cl_va", "INTEGER"),
    ("va_range", "REAL"), ("va_range_diff", "REAL"), ("va_prev_va", "TEXT"),
]


def _vs_va(price, va_lo, va_hi):
    if price is None or va_lo is None or va_hi is None:
        return None
    if price > va_hi:
        return 1
    if price < va_lo:
        return -1
    return 0


def main() -> None:
    t0 = time.time()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    instruments = [r["instrument"] for r in cur.execute("SELECT DISTINCT instrument FROM sessions")]
    all_rows = []

    for instrument in instruments:
        sessions = {
            r["date"]: dict(r)
            for r in conn.execute(
                "SELECT date, rth_open, rth_high, rth_low, rth_close FROM sessions "
                "WHERE instrument=? ORDER BY date", (instrument,),
            )
        }
        closes_by_date: dict[str, list[float]] = defaultdict(list)
        for date, close in conn.execute(
            "SELECT date, close FROM minutes WHERE instrument=? AND session='RTH' ORDER BY ts",
            (instrument,),
        ):
            closes_by_date[date].append(close)

        prev = None  # {"poc","va_hi","va_lo"} of the immediately preceding date, or None
        for date in sorted(sessions):
            sess = sessions[date]
            o, h, l, cl = sess["rth_open"], sess["rth_high"], sess["rth_low"], sess["rth_close"]

            mp = compute_market_profile(closes_by_date.get(date, []), o) if o is not None else None
            poc, va_hi, va_lo = (mp.poc, mp.va_hi, mp.va_lo) if mp else (None, None, None)

            o_prev_poc = (o - prev["poc"]) if (prev and prev["poc"] is not None and o is not None) else None
            o_prev_va = _vs_va(o, prev["va_lo"], prev["va_hi"]) if prev else None
            h_prev_va = _vs_va(h, prev["va_lo"], prev["va_hi"]) if prev else None
            l_prev_va = _vs_va(l, prev["va_lo"], prev["va_hi"]) if prev else None
            cl_poc = (cl - poc) if (poc is not None and cl is not None) else None
            cl_va = _vs_va(cl, va_lo, va_hi) if poc is not None else None
            va_range = (va_hi - va_lo) if poc is not None else None
            prev_va_range = (
                prev["va_hi"] - prev["va_lo"]
                if (prev and prev["va_hi"] is not None and prev["va_lo"] is not None) else None
            )
            va_range_diff = (
                va_range - prev_va_range if (va_range is not None and prev_va_range is not None) else None
            )
            if prev and poc is not None and prev["poc"] is not None:
                va_prev_va = classify_va_relationship(va_hi, va_lo, prev["va_hi"], prev["va_lo"])
            else:
                va_prev_va = None

            all_rows.append((
                instrument, date, poc, va_hi, va_lo, o_prev_poc, o_prev_va,
                h_prev_va, l_prev_va, cl_poc, cl_va, va_range, va_range_diff, va_prev_va,
            ))

            # Plain shift(1) semantics: always advance, even to None -- a gap
            # propagates exactly one day forward, it is never bridged.
            prev = {"poc": poc, "va_hi": va_hi, "va_lo": va_lo}

        n_ok = sum(1 for r in all_rows if r[2] is not None)
        print(f"{instrument}: {len(sessions)} sessions, {n_ok} with a computable POC/VA")

    for col, sqltype in COLUMNS:
        # DROP+ADD, not just ADD-if-missing: a plain "ADD COLUMN IF NOT
        # EXISTS"-style guard would silently keep a stale declared TYPE from
        # an earlier run (bit us once already -- va_prev_va was first created
        # as INTEGER, then switched to TEXT in code, but SQLite's type
        # affinity kept coercing the new text codes like "111" back into the
        # integer 111 because the old INTEGER column was never actually
        # replaced). Dropping first guarantees the column always has the type
        # this script currently declares. Requires SQLite >= 3.35 (2021).
        try:
            cur.execute(f"ALTER TABLE sessions DROP COLUMN {col}")
        except sqlite3.OperationalError:
            pass  # column didn't exist yet
        cur.execute(f"ALTER TABLE sessions ADD COLUMN {col} {sqltype}")
        # freshly added column is NULL for every existing row already; no
        # separate reset needed.

    col_names = [c for c, _ in COLUMNS]
    set_clause = ", ".join(f"{c} = ?" for c in col_names)
    cur.executemany(
        f"UPDATE sessions SET {set_clause} WHERE instrument = ? AND date = ?",
        [(*row[2:], row[0], row[1]) for row in all_rows],
    )

    conn.commit()
    conn.close()
    print(f"done: {len(all_rows)} sessions updated in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()
