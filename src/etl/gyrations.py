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
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gyrations.detect import detect_legs_close_to_close

_DETECTORS = {
    "close_to_close": detect_legs_close_to_close,
}


def _legs_to_rows(
    closes: list[float], ts_list: list, instrument: str, scope: str, threshold: float, mode: str
) -> list[dict]:
    detector = _DETECTORS[mode]
    legs = detector(closes, threshold)

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
    minutes: pl.DataFrame, instrument: str, scope: str, threshold: float, mode: str = "close_to_close"
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
        bars = partitions[(_date,)]
        closes = bars["close"].to_list()
        ts_list = bars["ts"].to_list()
        rows.extend(_legs_to_rows(closes, ts_list, instrument, scope, threshold, mode))

    for global_index, row in enumerate(rows):
        row["leg_index"] = global_index

    return rows


def compute_continuous_scope_legs(
    minutes: pl.DataFrame, instrument: str, threshold: float, mode: str = "close_to_close"
) -> list[dict]:
    """`continuous` scope: one detector run over the entire per-instrument series."""
    bars = minutes.sort("ts")
    closes = bars["close"].to_list()
    ts_list = bars["ts"].to_list()
    return _legs_to_rows(closes, ts_list, instrument, "continuous", threshold, mode)
