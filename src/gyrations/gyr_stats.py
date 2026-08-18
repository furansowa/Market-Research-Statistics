"""Gyrational Stats v1.0 -- 123 retracement zones and F231 continuation zones.

Pure computation: no SQLite, no Streamlit. Built on the already-verified
zigzag detector in `gyrations.detect` (properties P1-P8, tests/test_gyrations.py).

THE METHOD (from the user's expert source, adapted from M15 to M5 bars):

A "123" is three consecutive zigzag legs at the FAST threshold (80pt on DAX):

    X --swing1--> A --swing2--> B --swing3--> C

where swing1 runs in the direction of the SLOW threshold's (160pt) current
trend, swing2 retraces, and swing3 makes a FRESH BREAK beyond A. The tradeable
question is: how deep does swing2 go before swing3 starts? Enter near B's
expected level, stop beyond its 2-sigma point.

CAUSALITY, which is the whole difficulty here:

1. The slow trend direction is NOT the direction of the leg you can see in
   hindsight. A leg's direction only becomes knowable once price has actually
   travelled `t_slow` points from that leg's start pivot -- before that, the
   last direction you legitimately knew is the PREVIOUS leg's. `causal_direction`
   implements exactly this, and nothing downstream may use leg directions
   directly.

2. A setup only becomes DRAWABLE once swing2 has itself travelled `t_fast`
   from pivot A (that's what confirms A as a pivot at all). `decision_index`
   records that bar. Everything the display shows must be anchored there, not
   at A.

3. The volatility normaliser for a given date uses only the 5 STRICTLY PRIOR
   completed sessions.

KNOWN BIAS, stated because it changes how the output should be read: the 123
statistics are gathered from confirmed 123s only (per the expert's method), so
retracement depths are conditioned on swing3 having succeeded. The resulting
2-sigma stop will therefore be breached MORE often in live use than 2 sigma
implies -- setups that retraced deeper are disproportionately the ones that
failed, and they are excluded from the sample. The F231 machinery exists
precisely to handle that excluded population.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

import numpy as np

from gyrations.detect import detect_legs_extreme_to_extreme

ET = ZoneInfo("America/New_York")
CET = ZoneInfo("Europe/Berlin")

# forward scan cap for F231 resolution (~1 week of 24h 5-min bars)
F231_MAX_SCAN_BARS = 2016


def cet_five_min_index(ts_naive_et: datetime) -> int:
    """5-minute-of-day index in CET: 0 = 00:00, 1 = 00:05 ... 287 = 23:55.

    Stored timestamps are naive ET (see query/bars.py). Converting through
    real tz rules rather than a fixed +6h offset matters: the US and EU switch
    DST on different dates, so a fixed offset is wrong for ~3 weeks a year.
    """
    cet = ts_naive_et.replace(tzinfo=ET).astimezone(CET)
    return (cet.hour * 60 + cet.minute) // 5


def causal_direction(bars: list[tuple], legs, threshold: float) -> np.ndarray:
    """dirs[i] = +1 / -1 / 0 -- the slow-trend direction KNOWN at bar i.

    A leg contributes its direction only from the bar at which price has
    travelled `threshold` from the leg's start pivot; bars before that carry
    the previously established direction. 0 means nothing established yet.
    """
    n = len(bars)
    dirs = np.zeros(n, dtype=int)
    prev_dir = 0
    for leg in legs:
        s, e = leg.start_index, leg.end_index
        d = 1 if leg.direction == "up" else -1
        j = None
        for k in range(s, e + 1):
            _o, h, l, _c = bars[k]
            moved = (h - leg.start_price) if d == 1 else (leg.start_price - l)
            if moved >= threshold:
                j = k
                break
        if j is None:
            j = e + 1
        dirs[s:min(j, n)] = prev_dir
        if j <= e:
            dirs[j:e + 1] = d
            prev_dir = d
    last = legs[-1].end_index if legs else -1
    if last + 1 < n:
        dirs[last + 1:] = prev_dir
    return dirs


def rolling_vol5(session_ranges: dict) -> dict:
    """{date: mean of the 5 STRICTLY PRIOR completed RTH ranges}.

    Dates without 5 prior sessions are absent (not back-filled) -- a setup on
    such a date simply cannot be normalised and is dropped upstream.
    """
    out = {}
    ordered = sorted(session_ranges)
    for i, d in enumerate(ordered):
        if i < 5:
            continue
        prior = [session_ranges[ordered[k]] for k in range(i - 5, i)]
        out[d] = sum(prior) / 5.0
    return out


def slow_direction(bars: list[tuple], t_slow: float) -> np.ndarray:
    """Causal slow-trend direction per bar -- the same array `find_setups`
    filters on, exposed for callers that need it afterwards (F231 scanning,
    chart shading)."""
    legs_slow = [L for L in detect_legs_extreme_to_extreme(bars, t_slow) if L.confirmed]
    return causal_direction(bars, legs_slow, t_slow)


@dataclass
class Setup:
    """One direction-filtered 123 attempt. `success` is hindsight only -- it
    plays no part in whether the setup was drawable at `decision_index`."""
    direction: int          # +1 up, -1 down
    i_x: int                # swing1 start
    i_a: int                # swing1 end  (pivot A)
    i_b: int                # swing2 end  (pivot B)
    i_c: int                # swing3 end  (pivot C)
    decision_index: int     # bar at which swing2 reached t_fast -> setup drawable
    a_price: float
    b_price: float
    c_price: float
    retr_pts: float         # swing2 magnitude
    retr_bars: int          # swing2 duration in bars
    retr_norm: float        # swing2 magnitude / vol5
    time_index: int         # CET 5-min index of pivot A
    day: date               # calendar date of pivot A (CET)
    success: bool           # swing3 made a fresh break beyond A
    # Bar at which swing3's FAILURE becomes known: the leg after swing3 has
    # travelled t_fast back from C, confirming C as a pivot. None if no such
    # leg exists yet. This is the earliest honest moment to show an F231 zone.
    f231_index: int | None = None


def find_setups(
    bars: list[tuple],
    ts_list: list[datetime],
    t_fast: float,
    t_slow: float,
    vol5_by_date: dict,
) -> list[Setup]:
    """All direction-filtered 123 attempts, successful or not.

    Every returned setup was legitimately identifiable in real time at its
    `decision_index`; only `success` requires hindsight.
    """
    legs_fast = [L for L in detect_legs_extreme_to_extreme(bars, t_fast) if L.confirmed]
    legs_slow = [L for L in detect_legs_extreme_to_extreme(bars, t_slow) if L.confirmed]
    if len(legs_fast) < 3 or not legs_slow:
        return []

    dirs = causal_direction(bars, legs_slow, t_slow)
    setups: list[Setup] = []

    for i in range(len(legs_fast) - 2):
        L1, L2, L3 = legs_fast[i], legs_fast[i + 1], legs_fast[i + 2]
        d = 1 if L1.direction == "up" else -1

        # bar at which swing2 has travelled t_fast from A -- the setup's birth
        dec = None
        for k in range(L2.start_index, L2.end_index + 1):
            _o, h, l, _c = bars[k]
            moved = (L2.start_price - l) if d == 1 else (h - L2.start_price)
            if moved >= t_fast:
                dec = k
                break
        if dec is None:
            continue

        if dirs[dec] != d:
            continue

        ts_a = ts_list[L1.end_index]
        day = ts_a.replace(tzinfo=ET).astimezone(CET).date()
        vol5 = vol5_by_date.get(day)
        if not vol5:
            continue

        fresh = (L3.end_price > L1.end_price) if d == 1 else (L3.end_price < L1.end_price)

        # when does swing3's failure become knowable? when the NEXT leg has
        # travelled t_fast back from C
        f231_idx = None
        if i + 3 < len(legs_fast):
            L4 = legs_fast[i + 3]
            for k in range(L4.start_index, L4.end_index + 1):
                _o, h, l, _c = bars[k]
                moved = (L3.end_price - l) if d == 1 else (h - L3.end_price)
                if moved >= t_fast:
                    f231_idx = k
                    break

        setups.append(Setup(
            direction=d,
            i_x=L1.start_index,
            i_a=L1.end_index,
            i_b=L2.end_index,
            i_c=L3.end_index,
            decision_index=dec,
            a_price=L1.end_price,
            b_price=L2.end_price,
            c_price=L3.end_price,
            retr_pts=L2.magnitude_pts,
            retr_bars=L2.end_index - L2.start_index,
            retr_norm=L2.magnitude_pts / vol5,
            time_index=cet_five_min_index(ts_a),
            day=day,
            success=fresh,
            f231_index=f231_idx,
        ))
    return setups


def summarise_123(setups: list[Setup]) -> dict:
    """Retracement-depth and duration statistics from CONFIRMED 123s only.

    `entry_norm` is the MEDIAN normalised depth (robust: the distribution is
    truncated below at t_fast and right-skewed, so the mean sits deeper than
    the typical bottom). `stop_norm` is mean + 2*sd, per the expert's method.
    """
    wins = [s for s in setups if s.success]
    if len(wins) < 2:
        return {}
    norm = np.array([s.retr_norm for s in wins])
    barsd = np.array([s.retr_bars for s in wins], dtype=float)
    pts = np.array([s.retr_pts for s in wins])
    return {
        "n": len(wins),
        "n_all_setups": len(setups),
        "success_rate": len(wins) / len(setups) * 100,
        "entry_norm": float(np.median(norm)),
        "mean_norm": float(norm.mean()),
        "sd_norm": float(norm.std(ddof=1)),
        "stop_norm": float(norm.mean() + 2 * norm.std(ddof=1)),
        "mean_bars": float(barsd.mean()),
        "sd_bars": float(barsd.std(ddof=1)),
        "time_stop_bars": float(barsd.mean() + 2 * barsd.std(ddof=1)),
        "median_pts": float(np.median(pts)),
        "pct": {q: float(np.percentile(norm, q)) for q in (10, 25, 50, 75, 90, 95)},
    }


@dataclass
class F231:
    setup: Setup
    extra_depth_norm: float   # further adverse excursion beyond B, normalised
    bars_to_break: int | None  # bars from B to eventual fresh break; None if never
    outcome: str              # "broke" | "stopped" | "censored"


def measure_f231(
    bars: list[tuple],
    setups: list[Setup],
    stats123: dict,
    vol5_by_date: dict,
    dirs_slow: np.ndarray,
    max_scan: int = F231_MAX_SCAN_BARS,
) -> list[F231]:
    """For setups whose swing3 FAILED to break, what happens next?

    Per the expert's filter: only occurrences where the original 123 price stop
    still held are counted. A run that breaches that stop is reported as
    "stopped" and excluded from the zone statistics -- it was a losing trade,
    not a Figure-2.31 continuation.

    Scanning stops at a fresh break, a stop breach, a slow-trend flip (the
    premise is dead), or `max_scan` bars.
    """
    if not stats123:
        return []
    out: list[F231] = []
    n = len(bars)

    for s in setups:
        if s.success:
            continue
        vol5 = vol5_by_date.get(s.day)
        if not vol5:
            continue

        stop_price = (s.a_price - stats123["stop_norm"] * vol5) if s.direction == 1 \
            else (s.a_price + stats123["stop_norm"] * vol5)

        worst = s.b_price
        outcome = "censored"
        bars_to_break = None

        end = min(s.i_b + max_scan, n - 1)
        for k in range(s.i_b + 1, end + 1):
            _o, h, l, _c = bars[k]
            if s.direction == 1:
                if l < stop_price:
                    outcome = "stopped"
                    break
                worst = min(worst, l)
                if h > s.a_price:
                    outcome = "broke"
                    bars_to_break = k - s.i_b
                    break
            else:
                if h > stop_price:
                    outcome = "stopped"
                    break
                worst = max(worst, h)
                if l < s.a_price:
                    outcome = "broke"
                    bars_to_break = k - s.i_b
                    break
            if dirs_slow[k] != s.direction:
                outcome = "flipped"
                break

        if outcome == "stopped":
            continue

        extra = (s.b_price - worst) if s.direction == 1 else (worst - s.b_price)
        out.append(F231(
            setup=s,
            extra_depth_norm=extra / vol5,
            bars_to_break=bars_to_break,
            outcome=outcome,
        ))
    return out


def summarise_f231(rows: list[F231]) -> dict:
    """Zone statistics for the F231 population.

    Per the expert's filters, the zone is measured over occurrences that BOTH
    held the original 123 stop (already enforced in `measure_f231`) AND made a
    fresh break later ("regardless of swing count"). Runs that merely survived
    without ever resolving are not the pattern -- including them would collapse
    the depth statistic toward zero, since most of them terminate almost
    immediately when the slow trend flips, contributing a 0-depth sample that
    describes a dead setup rather than a continuation.

    `survival_rate` reports how rare the qualifying population is, which is the
    number that decides whether holding through a failed swing3 is worth doing
    at all.
    """
    broke = [r for r in rows if r.outcome == "broke"]
    if len(broke) < 2:
        return {}
    depth = np.array([r.extra_depth_norm for r in broke])
    tb = np.array([r.bars_to_break for r in broke], dtype=float)
    return {
        "n": len(broke),
        "n_survived": len(rows),
        "break_rate": len(broke) / len(rows) * 100,
        "mean_depth_norm": float(depth.mean()),
        "sd_depth_norm": float(depth.std(ddof=1)),
        "stop_norm": float(depth.mean() + 2 * depth.std(ddof=1)),
        "median_depth_norm": float(np.median(depth)),
        "mean_bars": float(tb.mean()),
        "sd_bars": float(tb.std(ddof=1)),
        "time_stop_bars": float(tb.mean() + 2 * tb.std(ddof=1)),
        "median_bars": float(np.median(tb)),
    }
