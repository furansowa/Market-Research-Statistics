"""Helper expressions for base features computed from the `minutes` table.

These are used by etl/sessions.py when aggregating minute bars into the
`sessions` table (rth_high_minute, rth_high_bucket, is_half_day, ...).
Kept separate from registry.py because they operate on time-of-day values
rather than on the sessions frame itself.
"""

from __future__ import annotations

import polars as pl


def minute_of_session(ts_col: str, session_start: str = "09:30") -> pl.Expr:
    """Minutes elapsed since session_start (HH:MM), for a datetime column."""
    start_h, start_m = (int(x) for x in session_start.split(":"))
    start_of_day_minutes = start_h * 60 + start_m
    hour = pl.col(ts_col).dt.hour().cast(pl.Int32)
    minute = pl.col(ts_col).dt.minute().cast(pl.Int32)
    return hour * 60 + minute - start_of_day_minutes


def time_bucket_30min(ts_col: str) -> pl.Expr:
    """Label like '09:30-10:00' for the 30-minute bucket containing ts_col."""
    hour = pl.col(ts_col).dt.hour().cast(pl.Int32)
    minute = pl.col(ts_col).dt.minute().cast(pl.Int32)
    minute_of_day = hour * 60 + minute
    bucket_start = (minute_of_day // 30) * 30
    bucket_end = bucket_start + 30

    def _hhmm(total_minutes: pl.Expr) -> pl.Expr:
        h = (total_minutes // 60) % 24
        m = total_minutes % 60
        return (
            h.cast(pl.Utf8).str.zfill(2) + pl.lit(":") + m.cast(pl.Utf8).str.zfill(2)
        )

    return _hhmm(bucket_start) + pl.lit("-") + _hhmm(bucket_end)


def is_half_day_expr(last_rth_ts_col: str, half_day_flag_before: str = "14:00") -> pl.Expr:
    """True if the last RTH bar's clock time is earlier than half_day_flag_before."""
    return pl.col(last_rth_ts_col).dt.strftime("%H:%M") < half_day_flag_before
