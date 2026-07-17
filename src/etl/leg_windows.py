"""Per-session leg-count-within-a-window columns (Gyrations v2.0 page).

For each session, three clock-time windows are defined from data already on
the `sessions` frame:

- "BS/SB" window: from whichever of the session's high/low occurred first to
  whichever occurred second (exactly the span `bs_sb` already describes: low
  then high on a "BS" day, high then low on a "SB" day).
- "First" window: from the session's actual open (first RTH bar) to whichever
  of high/low occurred first.
- "Last" window: from whichever of high/low occurred second to the session's
  actual close (last RTH bar, `rth_close_time` -- NOT a fixed clock time,
  since half-days end earlier).

For each of 3 fixed thresholds (40/120/200 points), this counts how many
*confirmed* `extreme_to_extreme` legs are fully contained (both endpoints)
within each window -- "contained", not "overlapping": a leg must start at or
after the window's lower bound and end at or before its upper bound. A
session with zero matching legs gets 0, never null.

This is deliberately a separate module rather than living in `sessions.py`
(which knows nothing about legs) or `gyrations.py` (which knows nothing about
the sessions frame's shape) -- a cross-cutting join can depend on both
without either depending on it.
"""

from __future__ import annotations

import polars as pl

_THRESHOLDS = (40, 120, 200)


def attach_leg_count_columns(
    sessions: pl.DataFrame,
    rth_bars: pl.DataFrame,
    legs_by_threshold: dict[float, list[dict]],
) -> pl.DataFrame:
    """`sessions` must have instrument/date/rth_high_time/rth_low_time/
    rth_close_time. `rth_bars` is that instrument's RTH-only minute bars (used
    only for each session's actual first-bar timestamp). `legs_by_threshold`
    maps a threshold (expected: 40, 120, 200) to the row-dicts produced by
    `etl.gyrations.compute_session_scope_legs(..., mode="extreme_to_extreme")`.

    Returns `sessions` with 9 new columns: `bs_sb_legs_{t}`, `first_legs_{t}`,
    `last_legs_{t}` for each threshold in `legs_by_threshold`.
    """
    t_open = (
        rth_bars.group_by(["instrument", "date"])
        .agg(pl.col("ts").min().alias("t_open"))
    )
    windows = (
        sessions.select(["instrument", "date", "rth_high_time", "rth_low_time", "rth_close_time"])
        .with_columns(
            pl.min_horizontal("rth_high_time", "rth_low_time").alias("t_first"),
            pl.max_horizontal("rth_high_time", "rth_low_time").alias("t_second"),
            pl.col("rth_close_time").alias("t_close"),
        )
        .join(t_open, on=["instrument", "date"], how="left")
    )

    result = sessions
    for threshold, rows in legs_by_threshold.items():
        suffix = int(threshold)
        col_names = [f"bs_sb_legs_{suffix}", f"first_legs_{suffix}", f"last_legs_{suffix}"]

        if not rows:
            result = result.with_columns([pl.lit(0).alias(c) for c in col_names])
            continue

        legs_df = pl.DataFrame(rows).filter(pl.col("confirmed"))
        counted = (
            legs_df.join(
                windows, left_on=["instrument", "start_date"], right_on=["instrument", "date"], how="inner",
            )
            .with_columns(
                ((pl.col("start_ts") >= pl.col("t_first")) & (pl.col("end_ts") <= pl.col("t_second")))
                .alias("_in_bs_sb"),
                ((pl.col("start_ts") >= pl.col("t_open")) & (pl.col("end_ts") <= pl.col("t_first")))
                .alias("_in_first"),
                ((pl.col("start_ts") >= pl.col("t_second")) & (pl.col("end_ts") <= pl.col("t_close")))
                .alias("_in_last"),
            )
            .group_by(["instrument", "start_date"])
            .agg(
                pl.col("_in_bs_sb").sum().alias(col_names[0]),
                pl.col("_in_first").sum().alias(col_names[1]),
                pl.col("_in_last").sum().alias(col_names[2]),
            )
        )
        result = result.join(
            counted, left_on=["instrument", "date"], right_on=["instrument", "start_date"], how="left",
        ).with_columns([pl.col(c).fill_null(0) for c in col_names])

    return result
