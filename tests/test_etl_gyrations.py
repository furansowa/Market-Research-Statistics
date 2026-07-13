"""Tests for etl/gyrations.py's session-scope wiring — distinct from the pure
detector algorithm tests in test_gyrations.py.

Regression test for a real bug caught during development: the `gyrations`
table's PRIMARY KEY is (instrument, scope, threshold, mode, leg_index) with no
`date` component, but the detector numbers legs 0..N *within a single session*.
Reusing that per-session numbering directly across multiple sessions collided
on the primary key and silently dropped almost every session's legs via
`INSERT OR REPLACE` when written to SQLite — caught by checking leg_index
uniqueness/density, not by eyeballing a single session's output.
"""

from datetime import datetime, timedelta

import polars as pl

from etl.gyrations import compute_session_scope_legs


def _synthetic_minutes(n_sessions: int, bars_per_session: int) -> pl.DataFrame:
    """n_sessions independent sessions, each with enough swing to produce
    several legs at a small threshold, on different calendar dates."""
    rows = []
    for s in range(n_sessions):
        date = datetime(2020, 1, 1) + timedelta(days=s)
        base = 100.0 + s * 1000  # different price level per session, irrelevant to the bug
        for i in range(bars_per_session):
            # zigzag: alternates up/down by 30 points every bar, guarantees legs at T=20
            close = base + (30 if i % 2 == 0 else 0)
            rows.append({"ts": date + timedelta(minutes=i), "date": date.date(), "close": close})
    return pl.DataFrame(rows)


def test_leg_index_globally_unique_across_sessions():
    minutes = _synthetic_minutes(n_sessions=5, bars_per_session=20)
    rows = compute_session_scope_legs(minutes, "TEST", "rth", threshold=20, mode="close_to_close")

    assert len(rows) > 5, "expected multiple legs per session across 5 sessions"

    leg_indices = [r["leg_index"] for r in rows]
    assert leg_indices == list(range(len(rows))), (
        "leg_index must be a dense, globally-increasing sequence across all "
        "sessions in a scope, not reset per session"
    )

    # every row must be a distinct primary key (instrument, scope, threshold, mode, leg_index)
    keys = [(r["instrument"], r["scope"], r["threshold"], r["mode"], r["leg_index"]) for r in rows]
    assert len(keys) == len(set(keys)), "primary key collision: some rows would overwrite others"

    # sanity: legs from every session actually survived (not just the last one)
    dates_represented = {r["start_date"] for r in rows}
    assert len(dates_represented) == 5
