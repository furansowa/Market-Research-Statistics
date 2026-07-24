"""SowaDonchian indicator -- pure, dependency-free translation of the ProRealTime original.

Copied verbatim (2026-07-21) from the sibling `Sowa_donchian_app` repo's `src/indicators/
sowa_donchian.py` for reuse on this app's "OpenNormalisation v1.0" page -- that app is the
canonical home for this indicator (its own DB persists 1min/5min results across GER30/US30 with a
regime-segment stats layer); this copy exists only because the two apps are separate repos with
separate DBs. Keep the two files in sync by hand if the algorithm changes.

Operates on plain sequences of high/low/close (no Polars/SQLite dependency), mirroring
`gyrations/detect.py`'s pure-algorithm style in the reference app -- keeps it directly unit
testable against small hand-built fixtures.

Source behavior (ProRealTime pseudocode, period renamed `DonchianPeriod` -> `period`):

    IF high = highest[period](high) THEN SowaDonchianHi = high ENDIF
    IF low  = lowest[period](low)   THEN SowaDonchianLo = low  ENDIF
    IF BarIndex > 10 THEN
        [UpAvg recomputes when SowaDonchianHi just changed; its window length is the distance
         back to the most recent prior bar whose LOW matches the *current* SowaDonchianLo.
         DnAvg is the mirror image, keyed off SowaDonchianHi. AvgMethod=0 -> plain mean of
         Close; a match exactly 1 bar back (or no match within the lookback cap) uses
         MedianPrice = (High+Low)/2 of the current bar instead.]
    ENDIF

Two deliberate deviations from a literal line-by-line port:

1. **Outside-bar handling** (user-directed, after investigation -- see below). An "outside bar" is
   one where SowaDonchianHi *and* SowaDonchianLo both update on the same bar. The source routes
   this through a branch gated on Close's position relative to both averages, which can silently
   update *neither* average even though both extremes are genuinely fresh -- clearly worth fixing.
   But the deeper issue (found by tracing a real anomaly against live GER30 data, not hypothetical)
   is structural, not just that gating: `UpAvg`'s lookback key is the *current* channel low,
   `DnAvg`'s is the *current* channel high -- each average is keyed off the *opposite* side,
   deliberately. On a normal single-sided update, that key is an already-anchored value that's
   typically been touched recently, so the backward search finds a nearby match. On an outside
   bar, **both** keys go fresh on the same bar, so *both* lookups are searching for a price that
   was only just set -- with no recency guarantee at all. Empirically, on GER30's full ~4.3M-bar
   1-minute history: 918,628 lookback-matched updates, median match distance 21 bars, 99% under
   104 bars -- but every single one of the 341 events with a match distance over 5,000 bars was
   outside-bar-triggered (0 of 918,628 non-outside-bar events exceeded that). Confirmed this isn't
   an artifact of deviation #1 above either: replaying the *original* Close-gated branch on the
   real anomalous bar (GER30 2025-07-16 07:00, Close below both averages) still routes to the same
   Hi-keyed DnAvg lookup and produces the identical far-distant result -- the source's own logic
   has the same exposure. Per explicit user decision: outside bars **always** use MedianPrice for
   both averages, bypassing the Count1/Count2 lookup entirely (not just when the lookback happens
   to land far away) -- simpler to reason about than a distance-based threshold, at the cost of
   discarding the lookback even on the rarer outside bar whose match would have been nearby/sane.
   See `test_outside_bar_always_uses_median_price` for a worked example including a case where a
   genuine nearby lookback match *would* have been available and is deliberately not used.
2. **Performance**. The source's Count1/Count2 lookup is a `WHILE i<9900` backward scan per bar.
   Implemented here as an O(1)-amortized hashmap lookup (most-recent-occurrence-of-an-exact-price)
   -- `max_lookback` plays the same role as the source's hardcoded 9900 cap, just checked as a
   cheap index-distance comparison instead of bounding an actual scan, so raising it (per user
   request, 200,000 here) costs nothing extra. (Non-outside-bar updates only, per #1 above.)

Everything else, including the quirk that "no match found within max_lookback" collapses to the
*same* Count==1 (MedianPrice) result as "matched exactly 1 bar back" (because the source's Count
variable is initialized to 1 and a failed scan never reassigns it), is preserved as-is for the
non-outside-bar case.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Optional, Sequence

DEFAULT_PERIOD = 20
DEFAULT_MAX_LOOKBACK = 200_000
DEFAULT_DECIMALS = 2
DEFAULT_WARMUP_BARS = 10


@dataclass
class SowaDonchianResult:
    up_avg: list[Optional[float]]
    dn_avg: list[Optional[float]]
    diff: list[Optional[float]]  # up_avg - dn_avg where both are known, else None


def _price_to_tick(price: float, decimals: int) -> int:
    return round(price * (10**decimals))


class _RollingExtreme:
    """Sliding-window max (mode='max') / min (mode='min') over the last `period` bars,
    O(1) amortized via a monotonic deque of indices. Ties favor the most recent index, so a bar
    that merely *ties* the existing window extreme still counts as "this bar is the extreme"."""

    __slots__ = ("period", "mode", "_dq")

    def __init__(self, period: int, mode: str):
        self.period = period
        self.mode = mode
        self._dq: deque[int] = deque()

    def push(self, i: int, value: float, values: Sequence[float]) -> float:
        dq = self._dq
        if self.mode == "max":
            while dq and values[dq[-1]] <= value:
                dq.pop()
        else:
            while dq and values[dq[-1]] >= value:
                dq.pop()
        dq.append(i)
        while dq[0] <= i - self.period:
            dq.popleft()
        return values[dq[0]]


def compute_sowa_donchian(
    high: Sequence[float],
    low: Sequence[float],
    close: Sequence[float],
    period: int = DEFAULT_PERIOD,
    max_lookback: int = DEFAULT_MAX_LOOKBACK,
    decimals: int = DEFAULT_DECIMALS,
    warmup_bars: int = DEFAULT_WARMUP_BARS,
) -> SowaDonchianResult:
    n = len(high)
    if len(low) != n or len(close) != n:
        raise ValueError("high, low, close must be the same length")

    up_avg: list[Optional[float]] = [None] * n
    dn_avg: list[Optional[float]] = [None] * n

    hi_roll = _RollingExtreme(period, "max")
    lo_roll = _RollingExtreme(period, "min")

    hi_val: Optional[float] = None
    lo_val: Optional[float] = None
    cur_up: Optional[float] = None
    cur_dn: Optional[float] = None

    # Most recent prior index at which low/high last touched a given price tick.
    last_low_at: dict[int, int] = {}
    last_high_at: dict[int, int] = {}

    # Prefix sums of close, for O(1) mean(close[j+1 .. i]) lookups.
    prefix = [0.0] * (n + 1)
    for i in range(n):
        prefix[i + 1] = prefix[i] + close[i]

    def mean_close(count: int, i: int) -> float:
        j = i - count + 1  # inclusive start
        return (prefix[i + 1] - prefix[j]) / count

    for i in range(n):
        roll_hi = hi_roll.push(i, high[i], high)
        roll_lo = lo_roll.push(i, low[i], low)

        hi_is_fresh = high[i] == roll_hi
        lo_is_fresh = low[i] == roll_lo

        new_hi_val = high[i] if hi_is_fresh else hi_val
        new_lo_val = low[i] if lo_is_fresh else lo_val

        hi_changed = hi_is_fresh and (hi_val is None or new_hi_val != hi_val)
        lo_changed = lo_is_fresh and (lo_val is None or new_lo_val != lo_val)

        hi_val = new_hi_val
        lo_val = new_lo_val
        is_outside_bar = hi_changed and lo_changed

        if i >= warmup_bars:
            if is_outside_bar:
                median_price = (high[i] + low[i]) / 2.0
                cur_up = median_price
                cur_dn = median_price
            else:
                if hi_changed:
                    lo_tick = _price_to_tick(lo_val, decimals)
                    j = last_low_at.get(lo_tick)
                    count1 = (i - j) if (j is not None and i - j <= max_lookback) else 1
                    cur_up = mean_close(count1, i) if count1 > 1 else (high[i] + low[i]) / 2.0
                if lo_changed:
                    hi_tick = _price_to_tick(hi_val, decimals)
                    j = last_high_at.get(hi_tick)
                    count2 = (i - j) if (j is not None and i - j <= max_lookback) else 1
                    cur_dn = mean_close(count2, i) if count2 > 1 else (high[i] + low[i]) / 2.0

        up_avg[i] = cur_up
        dn_avg[i] = cur_dn

        last_low_at[_price_to_tick(low[i], decimals)] = i
        last_high_at[_price_to_tick(high[i], decimals)] = i

    diff = [
        (u - d) if (u is not None and d is not None) else None
        for u, d in zip(up_avg, dn_avg)
    ]

    return SowaDonchianResult(up_avg=up_avg, dn_avg=dn_avg, diff=diff)
