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
from gyrations.detect import detect_legs_close_to_close


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
            # open/high/low = close: this fixture only exercises close_to_close
            # detection, but compute_session_scope_legs now always builds full
            # OHLC bar tuples regardless of mode (see etl/gyrations.py).
            rows.append({
                "ts": date + timedelta(minutes=i), "date": date.date(),
                "open": close, "high": close, "low": close, "close": close,
            })
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


def _synthetic_ohlc_minutes(n_sessions: int, bars_per_session: int) -> pl.DataFrame:
    """Same shape as _synthetic_minutes but with real high/low wicks, for
    exercising extreme_to_extreme (which needs full OHLC, not just closes)."""
    rows = []
    for s in range(n_sessions):
        date = datetime(2020, 1, 1) + timedelta(days=s)
        base = 100.0 + s * 1000
        for i in range(bars_per_session):
            close = base + (30 if i % 2 == 0 else 0)
            open_ = base + (0 if i % 2 == 0 else 30)
            high = max(open_, close) + 5
            low = min(open_, close) - 5
            rows.append({
                "ts": date + timedelta(minutes=i), "date": date.date(),
                "open": open_, "high": high, "low": low, "close": close,
            })
    return pl.DataFrame(rows)


def test_leg_index_globally_unique_across_sessions_extreme_to_extreme():
    """Same regression guard as the close_to_close test above, for the newly
    supported extreme_to_extreme mode -- the leg_index/PK-collision bug this
    file guards against is mode-agnostic plumbing, so must hold for both."""
    minutes = _synthetic_ohlc_minutes(n_sessions=5, bars_per_session=20)
    rows = compute_session_scope_legs(minutes, "TEST", "rth", threshold=20, mode="extreme_to_extreme")

    assert len(rows) > 5, "expected multiple legs per session across 5 sessions"

    leg_indices = [r["leg_index"] for r in rows]
    assert leg_indices == list(range(len(rows)))

    keys = [(r["instrument"], r["scope"], r["threshold"], r["mode"], r["leg_index"]) for r in rows]
    assert len(keys) == len(set(keys))

    dates_represented = {r["start_date"] for r in rows}
    assert len(dates_represented) == 5


def test_close_to_close_output_unaffected_by_ohlc_tuple_refactor():
    """Regression guard: compute_session_scope_legs now always builds full
    OHLC tuples internally (needed to support extreme_to_extreme) -- this must
    not change close_to_close's actual leg output, which should be identical
    to calling the detector directly on the plain closes list."""
    minutes = _synthetic_minutes(n_sessions=3, bars_per_session=20)
    rows = compute_session_scope_legs(minutes, "TEST", "rth", threshold=20, mode="close_to_close")

    for (_date,), bars_df in minutes.sort("ts").partition_by(["date"], as_dict=True).items():
        expected_legs = detect_legs_close_to_close(bars_df["close"].to_list(), 20)
        actual = [r for r in rows if r["start_date"] == _date]
        assert len(actual) == len(expected_legs)
        for row, leg in zip(actual, expected_legs):
            assert row["magnitude_pts"] == leg.magnitude_pts
            assert row["direction"] == leg.direction
            assert row["confirmed"] == leg.confirmed
