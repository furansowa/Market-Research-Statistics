"""Duration-rank 4-leg pattern classification (Gyrational Time v1.0).

Sibling to `merrill.py`'s price-based M/W classification, but ranks each
window's 4 LEG DURATIONS (minutes) instead of its 5 pivot PRICES. The legs
themselves are unchanged -- still real, threshold-filtered, alternating
up/down/up/down (or down/up/down/up) legs, so "M" (observed/4th leg down) vs
"W" (observed/4th leg up) is still assigned exactly the same way as in
merrill.py, straight from the leg's own `direction`.

What's different from the price version: a duration has no sign/direction,
so there is no alternation constraint on the rank sequence the way price
pivots had to represent a valid zigzag. All 4! = 24 permutations of
{1,2,3,4} are valid duration patterns (1 = quickest/shortest leg, 4 =
slowest/longest, straight ascending-duration rank -- user's own convention,
2026-08-06), and -- because duration-rank doesn't determine direction the
way price-rank did -- the SAME 24 patterns can occur under an M-family
window or a W-family window. Patterns are labeled M1-M24 / W1-W24, where the
number is just this pattern's position (1-indexed) in the 24 permutations of
"1234" sorted ascending as strings -- M7 and W7 share the exact same
duration-rank digit string ("2134"), differing only in which family of
window it occurred on.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

# All 24 permutations of 1234, ascending -- position (1-indexed) is the
# pattern number shared by both the M and W label spaces.
ALL_PATTERNS: tuple[str, ...] = tuple(sorted("".join(map(str, p)) for p in itertools.permutations([1, 2, 3, 4])))
PATTERN_NUMBER: dict[str, int] = {s: i + 1 for i, s in enumerate(ALL_PATTERNS)}

M_LABELS: tuple[str, ...] = tuple(f"M{i}" for i in range(1, 25))
W_LABELS: tuple[str, ...] = tuple(f"W{i}" for i in range(1, 25))


def _duration_rank_string(durations: list[float]) -> str:
    """1 = shortest, 4 = longest; ties (equal-duration legs, plausible since
    durations are whole minutes) broken by chronological order -- the
    earlier leg keeps the lower rank number."""
    order = sorted(range(len(durations)), key=lambda i: (durations[i], i))
    ranks = [0] * len(durations)
    for rank, idx in enumerate(order, start=1):
        ranks[idx] = rank
    return "".join(str(r) for r in ranks)


@dataclass
class TimePattern:
    leg_index: int  # position of the observed (4th) leg within the leg list build_time_patterns was given
    legs: list[dict]  # the 4 underlying leg rows, chronological
    durations: list[float]  # [d1, d2, d3, d4] minutes, chronological
    ranks: str  # duration-rank digit string, e.g. "2134"
    pattern_number: int  # 1-24, position of `ranks` in ALL_PATTERNS
    family: str  # "M" | "W", from the observed (4th) leg's actual direction
    label: str  # e.g. "M7" / "W19"
    start_date: str
    end_date: str


def build_time_patterns(legs: list[dict]) -> dict[int, TimePattern]:
    """`legs`: chronologically ordered leg rows (dicts with direction,
    duration_min, start_date, end_date -- see query.gyr_waves.fetch_legs),
    all the same (instrument, scope, threshold, mode). One `TimePattern` per
    leg index i >= 3 (sliding, one pattern per leg -- same numbering
    convention as merrill.build_patterns, so "the next pattern" is simply
    `patterns[i + 4]` and "the next leg" is `legs[i + 1]`).
    """
    patterns: dict[int, TimePattern] = {}
    for i in range(3, len(legs)):
        window = legs[i - 3:i + 1]
        durations = [leg["duration_min"] for leg in window]
        ranks = _duration_rank_string(durations)
        pattern_number = PATTERN_NUMBER[ranks]
        family = "M" if window[-1]["direction"] == "down" else "W"
        patterns[i] = TimePattern(
            leg_index=i, legs=window, durations=durations, ranks=ranks,
            pattern_number=pattern_number, family=family, label=f"{family}{pattern_number}",
            start_date=window[0]["start_date"], end_date=window[-1]["end_date"],
        )
    return patterns
