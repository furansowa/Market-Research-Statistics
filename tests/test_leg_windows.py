"""Unit tests for etl/leg_windows.py's leg-count-within-window containment
logic, against small hand-built synthetic fixtures (no DB, no real ETL run) --
mirrors test_etl_gyrations.py's synthetic-fixture style.
"""

from datetime import datetime

import polars as pl

from etl.leg_windows import attach_leg_count_columns

INSTRUMENT = "TEST"
DATE1 = datetime(2020, 1, 1).date()
DATE2 = datetime(2020, 1, 2).date()


def _ts(date, hh, mm):
    return datetime(date.year, date.month, date.day, hh, mm)


def _sessions_df():
    return pl.DataFrame({
        "instrument": [INSTRUMENT, INSTRUMENT],
        "date": [DATE1, DATE2],
        # session 1: low (09:45) before high (14:00) -> "BS"-shaped window
        "rth_high_time": [_ts(DATE1, 14, 0), _ts(DATE2, 14, 30)],
        "rth_low_time": [_ts(DATE1, 9, 45), _ts(DATE2, 9, 50)],
        "rth_close_time": [_ts(DATE1, 16, 0), _ts(DATE2, 15, 45)],
    })


def _rth_bars_df():
    # only needs enough bars per (instrument, date) to establish the min ts
    return pl.DataFrame({
        "instrument": [INSTRUMENT, INSTRUMENT, INSTRUMENT, INSTRUMENT],
        "date": [DATE1, DATE1, DATE2, DATE2],
        "ts": [_ts(DATE1, 9, 30), _ts(DATE1, 9, 31), _ts(DATE2, 9, 30), _ts(DATE2, 9, 31)],
    })


def _leg(start_hh, start_mm, end_hh, end_mm, confirmed=True, date=DATE1):
    return {
        "instrument": INSTRUMENT,
        "start_date": date,
        "start_ts": _ts(date, start_hh, start_mm),
        "end_ts": _ts(date, end_hh, end_mm),
        "confirmed": confirmed,
    }


def test_containment_counts_and_boundary_cases():
    # Session 1 windows: t_open=09:30, t_first=09:45, t_second=14:00, t_close=16:00
    legs_40 = [
        _leg(9, 35, 9, 40),               # A: fully inside [open, first] -> FirstLegs
        _leg(9, 50, 13, 0),                # B: fully inside [first, second] -> BS/SBLegs
        _leg(14, 5, 15, 0),                # C: fully inside [second, close] -> LastLegs
        _leg(9, 40, 9, 50),                # D: straddles the "first" boundary -> counts nowhere
        _leg(9, 45, 13, 30),                # E: starts exactly on the boundary (inclusive) -> BS/SBLegs
        _leg(9, 36, 9, 39, confirmed=False),  # F: would fit "first" but unconfirmed -> excluded
        # session 2 has zero legs at this threshold at all
    ]

    out = attach_leg_count_columns(_sessions_df(), _rth_bars_df(), {40: legs_40, 120: []})

    row1 = out.filter(pl.col("date") == DATE1).to_dicts()[0]
    row2 = out.filter(pl.col("date") == DATE2).to_dicts()[0]

    assert row1["first_legs_40"] == 1
    assert row1["bs_sb_legs_40"] == 2
    assert row1["last_legs_40"] == 1

    # session 2: no matching legs at all -> 0, not null
    assert row2["first_legs_40"] == 0
    assert row2["bs_sb_legs_40"] == 0
    assert row2["last_legs_40"] == 0

    # threshold with a totally empty rows list -> 0 for every session
    assert row1["first_legs_120"] == 0
    assert row1["bs_sb_legs_120"] == 0
    assert row1["last_legs_120"] == 0
    assert row2["first_legs_120"] == 0


def test_no_columns_are_null():
    out = attach_leg_count_columns(_sessions_df(), _rth_bars_df(), {40: [], 120: [], 200: []})
    for col in [
        "bs_sb_legs_40", "first_legs_40", "last_legs_40",
        "bs_sb_legs_120", "first_legs_120", "last_legs_120",
        "bs_sb_legs_200", "first_legs_200", "last_legs_200",
    ]:
        assert out[col].null_count() == 0
        assert (out[col] >= 0).all()
