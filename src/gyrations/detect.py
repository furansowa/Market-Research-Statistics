"""Threshold-based gyration/leg detector (Phase 2 spec §2).

A **leg** is one directional move between two consecutive pivots. A
**gyration** is one up+down cycle = two legs. This module emits legs (one row
per leg is what the `gyrations` table stores).

Zigzag-family reversal detection, **no minimum-bar requirement** — only a
minimum move size in points (`threshold`). Two modes:

- `close_to_close` (default, implemented here): runs over each bar's close, a
  1-D series. This is the primary/default mode per the spec — Ch***'s stated
  measurement basis is "bar close to bar close," and measurement on real data
  shows close mode filters out wick noise that extreme mode picks up (spec §2.2).
- `extreme_to_extreme`: tracks bar highs for up-legs, lows for down-legs. A
  single bar can make a new high *and* fall `threshold` below the running
  high — OHLC can't say which happened first, so an `intrabar_tiebreak` is
  required (`bar_direction` default: close>=open means low-before-high,
  else high-before-low; see `_ordered_ticks`).

The properties below (P1-P8, spec §2.4) and the invariant (§2.8) are
non-negotiable and are exercised in `tests/test_gyrations.py`, written before
this module. Do not "fix" a failing property test by relaxing the test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Pivot:
    index: int
    price: float


@dataclass
class Leg:
    leg_index: int
    start_index: int
    end_index: int
    start_price: float
    end_price: float
    direction: str  # "up" | "down"
    magnitude_pts: float
    confirmed: bool
    deepest_retr_pts: Optional[float] = None
    deepest_retr_pct_final: Optional[float] = None
    deepest_retr_progress: Optional[float] = None
    deepest_retr_start_index: Optional[int] = None
    deepest_retr_end_index: Optional[int] = None

    @property
    def duration_bars(self) -> int:
        return self.end_index - self.start_index

    @property
    def midprice(self) -> float:
        return (self.start_price + self.end_price) / 2


def detect_pivots_close_to_close(series: list[float], threshold: float) -> list[Pivot]:
    """Threshold-based zigzag pivot detector, close-to-close mode (spec §2.4).

    Properties (verify against tests/test_gyrations.py, not by eyeballing output):
      P1 - pivot indices strictly increasing.
      P2 - a pivot is the running extreme reached, never the confirming bar.
      P3 - two-sided seeding: tracks a running high AND running low until the
           first threshold-sized reversal establishes direction (no bias).
      P4 - simultaneous triggers (unseeded range > 2*threshold): tie-break on
           whichever extreme occurred later.
      P5 - degenerate seed (seed index == confirm index): emit one pivot, not two.
      P6 - no reversal ever occurs -> zero pivots, never synthesise one.
    """
    if not series:
        return []

    T = threshold
    hi = lo = series[0]
    i_hi = i_lo = 0
    lo_at_hi, i_lo_at_hi = series[0], 0
    hi_at_lo, i_hi_at_lo = series[0], 0
    dirn: Optional[str] = None
    pivots: list[tuple[int, float]] = []
    ext = ei = None

    for i, c in enumerate(series):
        if dirn is None:
            if c > hi:
                hi, i_hi = c, i
                lo_at_hi, i_lo_at_hi = lo, i_lo
            if c < lo:
                lo, i_lo = c, i
                hi_at_lo, i_hi_at_lo = hi, i_hi

            down_trig = (hi - c) >= T
            up_trig = (c - lo) >= T
            if down_trig and up_trig:
                down_trig = i_hi > i_lo
                up_trig = not down_trig

            if down_trig:
                seed, i_seed, conf, i_conf = lo_at_hi, i_lo_at_hi, hi, i_hi
                dirn = "down"
            elif up_trig:
                seed, i_seed, conf, i_conf = hi_at_lo, i_hi_at_lo, lo, i_lo
                dirn = "up"
            else:
                continue

            if i_seed < i_conf:  # P5
                pivots.append((i_seed, seed))
            pivots.append((i_conf, conf))
            ext, ei = c, i

        elif dirn == "up":
            if c > ext:
                ext, ei = c, i
            elif ext - c >= T:
                pivots.append((ei, ext))
                dirn = "down"
                ext, ei = c, i

        else:  # dirn == "down"
            if c < ext:
                ext, ei = c, i
            elif c - ext >= T:
                pivots.append((ei, ext))
                dirn = "up"
                ext, ei = c, i

    if dirn and (not pivots or ei > pivots[-1][0]):
        pivots.append((ei, ext))

    return [Pivot(index=idx, price=price) for idx, price in pivots]


def _legs_from_pivots(pivots: list[Pivot], threshold: float) -> list[Leg]:
    """Consecutive pivot pairs -> legs.

    Confirmation (§2.5, revisited twice): every leg is confirmed purely by
    `magnitude_pts >= threshold`, with no special-casing by position. An
    interior leg's start is always a validated pivot (a real reversal put it
    there), so its magnitude is structurally guaranteed >= threshold — the
    check is a no-op for it, kept only as a safety net. The **trailing** leg's
    start is likewise a validated pivot; only its end was ever in question
    (the scope ran out of bars before a further reversal), so if its magnitude
    already clears the threshold, that's a real, already-observed move —
    whether the market kept going afterward is out of scope.

    The **seed** leg is the interesting case: its start is the two-sided
    unseeded-phase bookkeeping (`lo_at_hi`/`hi_at_lo`), not a confirmed pivot —
    unlike interior/trailing legs, its magnitude is genuinely NOT guaranteed
    to reach threshold (verified: constructing a series where the seed leg's
    own span is smaller than the move that triggered the reversal). But when
    it *does* reach threshold, that's still a real, already-observed move —
    same reasoning as the trailing leg, symmetric around "we don't care what
    price did before the open" vs "after the close." Verified empirically
    (9000+ randomized trials, close_to_close mode) that confirming the seed
    leg by magnitude never violates the §2.8 retracement invariant: the
    retracement scan starts fresh at the seed's own price
    (`leg.start_price`/`start_index`), so the same "would have already ended
    the leg" argument that protects interior legs applies here too — a
    pullback of >= threshold anywhere in [seed_index, confirm_index] would
    have fired an earlier reversal instead of the one that actually fired.
    (This also resolves the old "424-point retracement" concern from an early
    draft — that was a scan-scoping bug, already fixed by scanning from
    `leg.start_index` rather than bar 0; it wasn't inherent to the seed leg.)

    A leg that is *both* first and last (a single leg spanning the whole run)
    falls out of the same one rule with no special-casing needed.
    """
    if len(pivots) < 2:
        return []

    n_legs = len(pivots) - 1
    legs = []
    for i in range(n_legs):
        start, end = pivots[i], pivots[i + 1]
        direction = "up" if end.price > start.price else "down"
        magnitude = abs(end.price - start.price)
        confirmed = magnitude >= threshold

        legs.append(Leg(
            leg_index=i,
            start_index=start.index,
            end_index=end.index,
            start_price=start.price,
            end_price=end.price,
            direction=direction,
            magnitude_pts=magnitude,
            confirmed=confirmed,
        ))
    return legs


def _compute_retracements_close_to_close(series: list[float], legs: list[Leg]) -> None:
    """Deepest retracement ("elasticity"), spec §2.6. Mutates legs in place.

    Close-to-close mode needs no terminal-bar special-casing (unlike
    extreme_to_extreme, §2.6) — each bar contributes exactly one value, so
    there's no separate high/low of the terminal bar to misattribute.

    Holds the invariant (§2.8) by construction: `run`/`dd` here retraces the
    exact same running-extreme tracking the detector itself uses to decide
    when a leg ends, so an interior drawdown reaching `threshold` would have
    already ended the leg at that point instead.

    For the trailing leg, when confirmed by magnitude (see `_legs_from_pivots`),
    the scan is extended through the series' literal last bar — not just to
    `leg.end_index` (the last new extreme reached) — so a small pullback after
    that extreme but before the scope ended is still measured. This does NOT
    change the leg's recorded `end_price`/`end_index` (the pivot itself stays
    exactly the local top/bottom reached); it only widens what the retracement
    scan looks at. Safe by the same construction argument: no bar between the
    last extreme and the series' end ever exceeded it (else that bar would
    have become the new extreme instead), so `run` cannot change during the
    extension — only `dd` against the already-fixed `run` can grow.
    """
    n_bars = len(series)
    for idx, leg in enumerate(legs):
        i0 = leg.start_index
        is_trailing = idx == len(legs) - 1
        i1 = (n_bars - 1) if (is_trailing and leg.confirmed) else leg.end_index
        up = leg.direction == "up"

        run = leg.start_price
        run_idx = i0
        best = 0.0
        progress_at_best = 0.0
        best_start_idx = i0
        best_end_idx = i0

        for j in range(i0, i1 + 1):
            c = series[j]
            if up:
                if c > run:
                    run, run_idx = c, j
                dd = run - c
            else:
                if c < run:
                    run, run_idx = c, j
                dd = c - run

            if dd > best:
                best = dd
                progress_at_best = abs(run - leg.start_price)
                best_start_idx = run_idx
                best_end_idx = j

        leg.deepest_retr_pts = best
        leg.deepest_retr_pct_final = (best / leg.magnitude_pts) if leg.magnitude_pts else 0.0
        leg.deepest_retr_progress = progress_at_best
        leg.deepest_retr_start_index = best_start_idx
        leg.deepest_retr_end_index = best_end_idx


def detect_legs_close_to_close(series: list[float], threshold: float) -> list[Leg]:
    pivots = detect_pivots_close_to_close(series, threshold)
    legs = _legs_from_pivots(pivots, threshold)
    _compute_retracements_close_to_close(series, legs)
    return legs


# ---------------------------------------------------------------------------
# extreme_to_extreme mode
# ---------------------------------------------------------------------------

Bar = tuple  # (open, high, low, close)


def _ordered_ticks(o: float, h: float, l: float, c: float, dirn: Optional[str], tiebreak: str):
    """Resolve intrabar path ambiguity: which of (high, low) happened first?

    - bar_direction (default): close >= open => low before high, else high
      before low. The only rule using information actually present in the bar.
    - adverse_first: the extreme that would *reverse* the current direction is
      tested first (legs end earlier). Only meaningful once a direction (up/
      down) is established; falls back to bar_direction while unseeded.
    - favourable_first: the extreme that *extends* the current direction is
      tested first (legs run longer). Same unseeded fallback.

    Returns [(kind, value), (kind, value)] with kind in {"high", "low"}.
    """
    if tiebreak == "bar_direction" or dirn is None:
        low_first = c >= o
    elif tiebreak == "adverse_first":
        low_first = dirn == "up"  # low is adverse to 'up', favourable to 'down'
    elif tiebreak == "favourable_first":
        low_first = dirn == "down"  # low is favourable to 'down'
    else:
        raise ValueError(f"unknown intrabar_tiebreak: {tiebreak}")

    return [("low", l), ("high", h)] if low_first else [("high", h), ("low", l)]


def detect_pivots_extreme_to_extreme(
    bars: list[Bar], threshold: float, tiebreak: str = "bar_direction"
) -> list[Pivot]:
    """Threshold-based zigzag pivot detector, extreme-to-extreme mode (spec §2.2).

    Same P1-P8 properties as close_to_close (see that function's docstring),
    generalised: 'up' legs extend via bar highs and reverse via bar lows;
    'down' legs extend via lows and reverse via highs. Each bar's two ticks
    are resolved into a definite order by `tiebreak` before processing, which
    is what makes the P4-style "simultaneous trigger" case impossible here in
    the way it's possible in close_to_close (a single tick is single-typed,
    so it can only ever test one direction) — the tiebreak has already
    resolved the ambiguity by imposing a sequential order within the bar.
    Only one reversal is resolved per bar; the second tick of a bar in which
    a reversal just fired is not reprocessed under the new direction (mirrors
    the §2.6 terminal-bar rule: that tick "belongs" to the next leg).
    """
    if not bars:
        return []

    T = threshold
    o0, h0, l0, c0 = bars[0]
    hi, lo = h0, l0
    i_hi = i_lo = 0
    lo_at_hi, i_lo_at_hi = l0, 0
    hi_at_lo, i_hi_at_lo = h0, 0
    dirn: Optional[str] = None
    pivots: list[tuple[int, float]] = []
    ext = ei = None

    for i, (o, h, l, c) in enumerate(bars):
        for kind, val in _ordered_ticks(o, h, l, c, dirn, tiebreak):
            if dirn is None:
                down_trig = up_trig = False
                if kind == "high":
                    if val > hi:
                        hi, i_hi = val, i
                        lo_at_hi, i_lo_at_hi = lo, i_lo
                    up_trig = (val - lo) >= T
                else:
                    if val < lo:
                        lo, i_lo = val, i
                        hi_at_lo, i_hi_at_lo = hi, i_hi
                    down_trig = (hi - val) >= T

                if down_trig:
                    seed, i_seed, conf, i_conf = lo_at_hi, i_lo_at_hi, hi, i_hi
                    dirn = "down"
                elif up_trig:
                    seed, i_seed, conf, i_conf = hi_at_lo, i_hi_at_lo, lo, i_lo
                    dirn = "up"
                else:
                    continue

                if i_seed < i_conf:  # P5
                    pivots.append((i_seed, seed))
                pivots.append((i_conf, conf))
                ext, ei = val, i
                break  # one reversal resolution per bar

            elif dirn == "up":
                if kind == "high":
                    if val > ext:
                        ext, ei = val, i
                else:  # low tick tests reversal
                    if ext - val >= T:
                        pivots.append((ei, ext))
                        dirn = "down"
                        ext, ei = val, i
                        break

            else:  # dirn == "down"
                if kind == "low":
                    if val < ext:
                        ext, ei = val, i
                else:  # high tick tests reversal
                    if val - ext >= T:
                        pivots.append((ei, ext))
                        dirn = "up"
                        ext, ei = val, i
                        break

    if dirn and (not pivots or ei > pivots[-1][0]):
        pivots.append((ei, ext))

    return [Pivot(index=idx, price=price) for idx, price in pivots]


def _terminal_tick_type(direction: str, at_start: bool) -> str:
    """Which extreme IS the pivot at a leg's first/last bar (spec §2.6).

    up leg: starts at a low, ends at a high. down leg: the reverse.
    """
    if direction == "up":
        return "low" if at_start else "high"
    return "high" if at_start else "low"


def _compute_retracements_extreme_to_extreme(
    bars: list[Bar], legs: list[Leg], tiebreak: str = "bar_direction"
) -> None:
    """Deepest retracement, extreme_to_extreme mode (spec §2.6). Mutates legs in place.

    **Terminal-bar rule (required, or the invariant fails).** On a leg's first
    and last bars, only the extreme that IS that leg's pivot participates in
    the scan — the opposite extreme of those bars belongs to the *adjacent*
    leg (it's what seeded/triggered the transition), not this one. Interior
    bars scan both ticks, in the same tiebreak order used during detection,
    so the invariant holds by the same "would have ended the leg" argument as
    close_to_close mode.

    For the trailing leg, when confirmed by magnitude, the scan is extended
    through the series' literal last bar (see the close_to_close version's
    docstring for why this is safe). The bar at the leg's *original*
    `end_index` is no longer terminal in that case — there is no "next leg"
    for its opposite extreme to belong to, since no further reversal ever
    fired — so it's scanned normally (both ticks) like any interior bar; only
    `i0` still gets terminal treatment.
    """
    n_bars = len(bars)
    for idx, leg in enumerate(legs):
        i0 = leg.start_index
        is_trailing = idx == len(legs) - 1
        extend = is_trailing and leg.confirmed and leg.end_index < n_bars - 1
        i1 = (n_bars - 1) if extend else leg.end_index
        up = leg.direction == "up"

        run = leg.start_price
        run_idx = i0
        best = 0.0
        progress_at_best = 0.0
        best_start_idx = i0
        best_end_idx = i0

        for j in range(i0, i1 + 1):
            o, h, l, c = bars[j]

            if j == i0:
                # degenerate single-bar leg: still only the start pivot's
                # extreme participates (dd=0 trivially either way)
                ttype = _terminal_tick_type(leg.direction, True)
                ticks = [(ttype, h if ttype == "high" else l)]
            elif j == i1 and not extend:
                ttype = _terminal_tick_type(leg.direction, False)
                ticks = [(ttype, h if ttype == "high" else l)]
            else:
                ticks = _ordered_ticks(o, h, l, c, leg.direction, tiebreak)

            for kind, val in ticks:
                if up:
                    if kind == "high" and val > run:
                        run, run_idx = val, j
                    dd = run - val
                else:
                    if kind == "low" and val < run:
                        run, run_idx = val, j
                    dd = val - run

                if dd > best:
                    best = dd
                    progress_at_best = abs(run - leg.start_price)
                    best_start_idx = run_idx
                    best_end_idx = j

        leg.deepest_retr_pts = best
        leg.deepest_retr_pct_final = (best / leg.magnitude_pts) if leg.magnitude_pts else 0.0
        leg.deepest_retr_progress = progress_at_best
        leg.deepest_retr_start_index = best_start_idx
        leg.deepest_retr_end_index = best_end_idx


def detect_legs_extreme_to_extreme(
    bars: list[Bar], threshold: float, tiebreak: str = "bar_direction"
) -> list[Leg]:
    pivots = detect_pivots_extreme_to_extreme(bars, threshold, tiebreak)
    legs = _legs_from_pivots(pivots, threshold)
    _compute_retracements_extreme_to_extreme(bars, legs, tiebreak)
    return legs
