"""Fixed-price-increment (Renko-style) RANGE BARS -- pure computation, no
SQLite/Streamlit deps.

Distinct from the zigzag/leg detector in detect.py: a leg only ends at a
*reversal* pivot, so its length varies; a range bar always closes after
exactly `brick_size` points of net movement from its own open, regardless of
whether price is trending or chopping -- classic tick-chart construction.

Built here from 1-minute OHLC as a proxy for tick data, since that's all we
have: each source bar contributes an assumed intrabar path of 4 points
(open -> {low, high, in bar_direction order} -> close), using the exact same
tiebreak convention as detect.py's `_ordered_ticks` (close >= open => low
touched before high, for consistency with the rest of this codebase). Any
single directional move between two consecutive path points is sliced into
as many complete brick_size-sized bars as it spans, so one fast source bar
can legitimately produce several range bars.

A completed up-bar's high is always exactly open + brick_size and its low is
whatever was touched while still forming (may dip below open first); mirror
for a down-bar. This matches true tick-based range-bar construction: as soon
as the threshold is reached the bar closes immediately, so overshoot on the
completing side is impossible by construction -- only the side that did NOT
trigger the close can wander.

No session-boundary resets: the brick state runs continuously through
overnight/weekend gaps, same as a real range-bar chart on a trading platform.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class RangeBar:
    open: float
    high: float
    low: float
    close: float
    direction: str  # "up" or "down"
    start_ts: object  # timestamp of the source bar that opened this brick
    end_ts: object  # timestamp of the source bar that completed it
    n_source_bars: int  # new source (1-min) bars consumed to complete it;
    # 0 if it completed within the same source bar as the previous brick


def _bar_path(o: float, h: float, l: float, c: float) -> list[float]:
    low_first = c >= o
    mids = [l, h] if low_first else [h, l]
    return [o, *mids, c]


def build_range_bars(bars: list[tuple], brick_size: float) -> list[RangeBar]:
    """`bars` is a list of (ts, open, high, low, close), 1-minute, sorted,
    one continuous series (ts can be any orderable/stringable value -- it's
    only ever stored, never compared numerically)."""
    if not bars:
        return []

    out: list[RangeBar] = []
    bar_open = bars[0][1]
    pending_high = pending_low = bar_open
    open_ts = bars[0][0]
    n_source = 0

    def close_bar(target: float, end_ts, direction: str) -> None:
        nonlocal bar_open, pending_high, pending_low, open_ts, n_source
        hi = max(pending_high, target) if direction == "up" else pending_high
        lo = pending_low if direction == "up" else min(pending_low, target)
        out.append(RangeBar(
            open=bar_open, high=hi, low=lo, close=target,
            direction=direction, start_ts=open_ts, end_ts=end_ts,
            n_source_bars=n_source,
        ))
        bar_open = target
        pending_high = pending_low = target
        open_ts = end_ts
        n_source = 0

    def process_move(from_p: float, to_p: float, ts) -> None:
        nonlocal pending_high, pending_low
        if to_p == from_p:
            return
        going_up = to_p > from_p
        while True:
            if going_up:
                target = bar_open + brick_size
                if to_p < target:
                    pending_high = max(pending_high, to_p)
                    return
                close_bar(target, ts, "up")
                if target >= to_p:
                    return
            else:
                target = bar_open - brick_size
                if to_p > target:
                    pending_low = min(pending_low, to_p)
                    return
                close_bar(target, ts, "down")
                if target <= to_p:
                    return

    for ts, o, h, l, c in bars:
        n_source += 1
        path = _bar_path(o, h, l, c)
        for i in range(len(path) - 1):
            process_move(path[i], path[i + 1], ts)

    return out
