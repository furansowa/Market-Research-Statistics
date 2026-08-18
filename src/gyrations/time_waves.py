"""Time Waves v1.0 -- zigzag legs defined by TIME (bar count), not leg size.

Faithful port of the user's ProRealTime "TimeWaves v1.1" indicator. Where the
point-threshold detector in `gyrations.detect` ends a leg when price reverses
by N points, this one ends a leg when the running extreme has STOOD UNBEATEN
for `min_bars` bars. Every leg therefore spans at least `min_bars` bars.

State machine, per bar, exactly as the PRT source:
  1. extend the running extreme of the leg in progress (high if trend is up,
     low if down);
  2. elapsed = current bar - the bar that last set the extreme;
  3. once elapsed >= min_bars, emit the leg (last pivot -> the extreme),
     move the pivot to that extreme, flip the trend, and start the new leg's
     extreme tracking AT THE CONFIRMATION BAR.

Nothing here looks forward, so a leg is final the moment it is emitted -- the
property that makes the original indicator usable live.

KNOWN BEHAVIOUR, PRESERVED ON PURPOSE (mode="prt"). Step 3 restarts the new
leg's extreme from the confirmation bar's own high/low, not from the pivot. Any
more extreme price between the pivot bar and the confirmation bar is therefore
invisible to the new leg. Example with min_bars=3 on the series
[10, 12, 5, 11, 11, 11, 11]: the up-leg is confirmed at bar 4 and the new
down-leg seeds its low from bar 4 (11), so the genuine low of 5 at bar 2 is
never registered. This is the "not perfect" the user described. It is NOT a
port bug and must not be silently corrected -- `tests`/verification pin it.

mode="scan_back" is the optional strict improvement: at the confirmation bar it
seeds the new extreme from the true extreme over (pivot_bar, confirmation_bar],
which is information already fully available at that instant, so it stays
non-repainting while eliminating the blind spot above. Legs still alternate and
still span >= min_bars bars, so it is a drop-in for Merrill classification.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

MODES = ("prt", "scan_back")


@dataclass
class TimeLeg:
    start_index: int
    end_index: int
    start_price: float
    end_price: float
    direction: str  # "up" | "down"

    @property
    def duration_bars(self) -> int:
        return self.end_index - self.start_index

    @property
    def magnitude_pts(self) -> float:
        return abs(self.end_price - self.start_price)


def detect_time_legs(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    min_bars: int,
    use_close: bool = False,
    mode: str = "prt",
) -> list[TimeLeg]:
    """Legs whose reversal is triggered by elapsed bars rather than points.

    `use_close` mirrors the indicator's UseClose flag: track closes instead of
    highs/lows. `mode` selects the seeding behaviour documented above.
    """
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}, expected one of {MODES}")
    n = len(close)
    if n == 0:
        return []

    hi_src = close if use_close else high
    lo_src = close if use_close else low

    # ONCE block: seeded from the first bar's close, trend up
    trend = 1
    ext_price = float(close[0])
    ext_bar = 0
    pivot_price = float(close[0])
    pivot_bar = 0

    legs: list[TimeLeg] = []

    for i in range(n):
        if trend == 1:
            if hi_src[i] > ext_price:
                ext_price = float(hi_src[i])
                ext_bar = i
        else:
            if lo_src[i] < ext_price:
                ext_price = float(lo_src[i])
                ext_bar = i

        if i - ext_bar >= min_bars:
            legs.append(TimeLeg(
                start_index=pivot_bar, end_index=ext_bar,
                start_price=pivot_price, end_price=ext_price,
                direction="up" if trend == 1 else "down",
            ))
            pivot_bar, pivot_price = ext_bar, ext_price

            if mode == "prt":
                # new leg's extreme seeded from the confirmation bar only
                ext_price = float(lo_src[i]) if trend == 1 else float(hi_src[i])
                ext_bar = i
            else:
                # seed from the true extreme since the pivot -- causal, since
                # every one of those bars has already closed
                span = slice(pivot_bar + 1, i + 1)
                if trend == 1:
                    seg = lo_src[span]
                    off = int(np.argmin(seg))
                    ext_price = float(seg[off])
                else:
                    seg = hi_src[span]
                    off = int(np.argmax(seg))
                    ext_price = float(seg[off])
                ext_bar = pivot_bar + 1 + off

            trend = -trend

    return legs


def legs_to_rows(legs: list[TimeLeg], ts_list: list) -> list[dict]:
    """TimeLeg -> the dict shape the rest of the app expects.

    `gyrations.merrill.build_patterns` needs start_price/end_price/start_date/
    end_date; dates are ISO strings to match the SQLite-backed leg rows the
    Waves page consumes, so date-range filtering is the same string comparison
    on both pages. `confirmed` is always True: unlike the threshold detector,
    which can leave a trailing unconfirmed leg, a time leg is only ever emitted
    at its confirmation bar.
    """
    rows = []
    for i, leg in enumerate(legs):
        start_ts, end_ts = ts_list[leg.start_index], ts_list[leg.end_index]
        rows.append({
            "leg_index": i,
            "start_ts": start_ts,
            "end_ts": end_ts,
            "start_date": start_ts.date().isoformat(),
            "end_date": end_ts.date().isoformat(),
            "start_price": leg.start_price,
            "end_price": leg.end_price,
            "direction": leg.direction,
            "magnitude_pts": leg.magnitude_pts,
            "duration_bars": leg.duration_bars,
            "duration_min": int((end_ts - start_ts).total_seconds() // 60),
            "confirmed": True,
        })
    return rows
