"""Point of Control / Value Area -- pure, dependency-free time-at-price algorithm.

Ported concept from a ProRealTime indicator the user showed (Initial Balance /
Value Area), but NOT a line-by-line port -- that script had a hard 500-bar
lookback cap (fine for a 1-hour IB, silently wrong for a full 6.5h RTH session
on 10s bars) and approximated the Value Area as a fixed +/-35% of the day's
range around POC (ignores the actual distribution shape). This module instead
runs the standard Market Profile algorithm directly against the full session
of 1-minute closes (no lookback cap possible -- we always have the complete
list in hand), and expands the Value Area outward from the POC bin one step
at a time, same as the real thing:

1. Bin every close in the session into price buckets.
2. POC = the bucket with the most bars in it (ties -> whichever bucket is
   closer to the middle of the session's touched range, not just the lowest).
3. Value Area: starting at the POC bucket, repeatedly add whichever adjacent
   bucket (immediately above the current top, or immediately below the
   current bottom) has more bars, until >= `va_pct` of the session's bars are
   included. This is what makes a real Value Area asymmetric on skewed days,
   unlike a fixed-% band around POC.

No volume/tick data available (see registry.py's minutes table), so "bars in
a bucket" (1-minute closes) stands in for "time at that price" -- the same
close-only compromise already used elsewhere in this app (Ch***'s own stated
basis is close-to-close, and intrabar wicks are noise per §2.2 spec).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

# All 6 mutually-exclusive, exhaustive relationships between today's Value
# Area [lo, hi] and the previous day's [prev_lo, prev_hi] (given lo < hi and
# prev_lo < prev_hi always hold). User's own spec (2026-07-22), symbolic
# codes -- NOT magnitudes, "11"/"111" don't mean "more" than "1":
#   "1"   : hi >  prev_hi  AND  prev_lo <  lo <= prev_hi   (shifted up, overlap)
#   "-1"  : prev_lo <= hi < prev_hi  AND  lo < prev_lo     (shifted down, overlap)
#   "0"   : prev_lo <= lo  AND  hi <= prev_hi              (today's VA contained inside)
#   "11"  : lo > prev_hi   (both today's bounds above prev_hi -- no overlap, shifted up)
#   "-11" : hi < prev_lo   (both today's bounds below prev_lo -- no overlap, shifted down)
#   "111" : hi > prev_hi   AND  lo < prev_lo               (today's VA engulfs yesterday's)
VA_RELATIONSHIP_CODES = ("1", "-1", "0", "11", "-11", "111")


def classify_va_relationship(hi: float, lo: float, prev_hi: float, prev_lo: float) -> str:
    """Classify today's Value Area [lo, hi] against the previous day's
    [prev_lo, prev_hi]. Exhaustive and mutually exclusive by construction
    (equivalent to asking where each of lo/hi falls: below prev_lo, inside
    [prev_lo, prev_hi], or above prev_hi -- there are exactly 6 valid
    combinations given lo < hi, one per code above)."""
    if lo > prev_hi:
        return "11"
    if hi < prev_lo:
        return "-11"
    if hi > prev_hi and lo < prev_lo:
        return "111"
    if hi > prev_hi:
        return "1"
    if lo < prev_lo:
        return "-1"
    return "0"

DEFAULT_VA_PCT = 0.70
DEFAULT_BIN_PCT = 0.0005  # bin width = 0.05% of the reference price (scale-free across DOW's 7k-52k range)
DEFAULT_MIN_BIN_PTS = 0.5
DEFAULT_MIN_BARS = 30  # below this, a session's POC/VA is too noisy to trust


@dataclass(frozen=True)
class MarketProfile:
    poc: float
    va_hi: float
    va_lo: float
    n_bars: int


def compute_market_profile(
    closes: Sequence[float],
    reference_price: float,
    va_pct: float = DEFAULT_VA_PCT,
    bin_pct: float = DEFAULT_BIN_PCT,
    min_bin_pts: float = DEFAULT_MIN_BIN_PTS,
    min_bars: int = DEFAULT_MIN_BARS,
) -> Optional[MarketProfile]:
    """POC + Value Area from a session's 1-min closes. `reference_price` (the
    session's own RTH open) scales the bin width so granularity stays
    comparable across eras of very different price levels. Returns None if
    there are fewer than `min_bars` closes (session too short/thin to trust)."""
    n = len(closes)
    if n < min_bars:
        return None

    bin_width = max(reference_price * bin_pct, min_bin_pts)
    lo = min(closes)
    hi = max(closes)
    n_bins = int((hi - lo) / bin_width) + 1

    counts = [0] * n_bins
    for c in closes:
        idx = min(int((c - lo) / bin_width), n_bins - 1)
        counts[idx] += 1

    mid = (n_bins - 1) / 2
    poc_idx = max(range(n_bins), key=lambda i: (counts[i], -abs(i - mid)))

    lo_idx = hi_idx = poc_idx
    running = counts[poc_idx]
    target = va_pct * n
    while running < target and (lo_idx > 0 or hi_idx < n_bins - 1):
        next_hi = counts[hi_idx + 1] if hi_idx < n_bins - 1 else -1
        next_lo = counts[lo_idx - 1] if lo_idx > 0 else -1
        if next_hi >= next_lo:
            hi_idx += 1
            running += counts[hi_idx]
        else:
            lo_idx -= 1
            running += counts[lo_idx]

    return MarketProfile(
        poc=lo + (poc_idx + 0.5) * bin_width,
        va_hi=lo + (hi_idx + 1) * bin_width,
        va_lo=lo + lo_idx * bin_width,
        n_bars=n,
    )
