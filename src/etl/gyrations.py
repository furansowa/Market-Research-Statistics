"""Minute bars -> gyration legs, per scope (Phase 2 spec §2.9, §3.2).

Glue between the pure `gyrations.detect` algorithm (which knows nothing about
Polars, SQLite, or timestamps — just plain float series and integer indices)
and the rest of the ETL. Three scopes:

- `rth`  — detector runs per (instrument, date) over that session's RTH bars only.
- `eth`  — detector runs per (instrument, date) over ALL of that date's bars
  (RTH+ETH), so `rth` and `eth` legs are directly comparable.
- `continuous` — detector runs ONCE per instrument over the entire sorted
  minute series, never resetting at date boundaries. A leg here is not owned
  by one session, hence `gyrations` is keyed on timestamps, not `date`.

For `rth`/`eth`, `start_date == end_date` always (neither scope crosses a
calendar date). For `continuous`, a leg's `start_date`/`end_date` can differ.

Both `mode="close_to_close"` and `mode="extreme_to_extreme"` are supported
(§2.2) -- the latter needs full OHLC bars and a `tiebreak`, not just closes.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gyrations.detect import detect_legs_close_to_close, detect_legs_extreme_to_extreme

_DETECTORS = {
    "close_to_close": detect_legs_close_to_close,
    "extreme_to_extreme": detect_legs_extreme_to_extreme,
}


def _legs_to_rows(
    bars: list[tuple], ts_list: list, instrument: str, scope: str, threshold: float, mode: str,
    tiebreak: str = "bar_direction",
) -> list[dict]:
    """`bars` is a list of (open, high, low, close) tuples. close_to_close only
    ever looks at the close (bars[i][3]); extreme_to_extreme needs the full
    OHLC tuple plus a tiebreak. One code path for both rather than two
    divergent bar-list constructions per caller -- doesn't change
    close_to_close's actual values (same closes, same order)."""
    detector = _DETECTORS[mode]
    if mode == "close_to_close":
        legs = detector([bar[3] for bar in bars], threshold)
    else:
        legs = detector(bars, threshold, tiebreak=tiebreak)

    rows = []
    for leg in legs:
        start_ts = ts_list[leg.start_index]
        end_ts = ts_list[leg.end_index]
        rows.append({
            "instrument": instrument,
            "scope": scope,
            "threshold": threshold,
            "mode": mode,
            "leg_index": leg.leg_index,
            "confirmed": leg.confirmed,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "start_date": start_ts.date(),
            "end_date": end_ts.date(),
            "start_price": leg.start_price,
            "end_price": leg.end_price,
            "direction": leg.direction,
            "magnitude_pts": leg.magnitude_pts,
            "duration_min": int((end_ts - start_ts).total_seconds() // 60),
            "midprice": leg.midprice,
            "deepest_retr_pts": leg.deepest_retr_pts,
            "deepest_retr_pct_final": leg.deepest_retr_pct_final,
            "deepest_retr_progress": leg.deepest_retr_progress,
            "deepest_retr_start_ts": ts_list[leg.deepest_retr_start_index],
            "deepest_retr_end_ts": ts_list[leg.deepest_retr_end_index],
        })
    return rows


def compute_session_scope_legs(
    minutes: pl.DataFrame, instrument: str, scope: str, threshold: float, mode: str = "close_to_close",
    tiebreak: str = "bar_direction",
) -> list[dict]:
    """`rth` or `eth` scope: one detector run per (instrument, date).

    `minutes` should already be filtered to the bars that define the scope
    (RTH-only for `rth`, all bars for `eth`) for a single instrument.

    `leg_index` is renumbered to increment **globally** across all sessions in
    chronological order, not reset to 0 per session — the `gyrations` PRIMARY
    KEY is `(instrument, scope, threshold, mode, leg_index)` with no `date`
    component, so per-session-reset indices from different sessions would
    collide and silently overwrite each other via `INSERT OR REPLACE`.
    """
    rows: list[dict] = []
    partitions = minutes.sort("ts").partition_by(["date"], as_dict=True)

    for (_date,) in sorted(partitions.keys()):
        bars_df = partitions[(_date,)]
        bars = list(zip(bars_df["open"], bars_df["high"], bars_df["low"], bars_df["close"]))
        ts_list = bars_df["ts"].to_list()
        rows.extend(_legs_to_rows(bars, ts_list, instrument, scope, threshold, mode, tiebreak))

    for global_index, row in enumerate(rows):
        row["leg_index"] = global_index

    return rows


def compute_continuous_scope_legs(
    minutes: pl.DataFrame, instrument: str, threshold: float, mode: str = "close_to_close",
    tiebreak: str = "bar_direction",
) -> list[dict]:
    """`continuous` scope: one detector run over the entire per-instrument series."""
    bars_df = minutes.sort("ts")
    bars = list(zip(bars_df["open"], bars_df["high"], bars_df["low"], bars_df["close"]))
    ts_list = bars_df["ts"].to_list()
    return _legs_to_rows(bars, ts_list, instrument, "continuous", threshold, mode, tiebreak)
