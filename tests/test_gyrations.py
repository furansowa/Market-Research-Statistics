"""Property tests for the gyration/leg detector (Phase 2 spec §2.4, §2.8).

Written BEFORE the detector exists, per spec's explicit instruction: "Write the
property tests P1-P8 and the invariant before the detector, not after." These
use the exact fixture series named in spec §2.4 ("Verified time-ordered on:
rise-then-fall, fall-then-rise, no-trigger, monotone drop, double swing,
exact-T touch") plus the P4 simultaneous-trigger case, worked out by hand
against the spec's reference pseudocode so the expected pivots below are known
-correct, not just "whatever the code produces."

Only close_to_close mode is covered here; extreme_to_extreme gets its own
tests (bar_direction tie-break, "strictly more legs") once that mode exists.
"""

import pytest

from gyrations.detect import detect_legs_close_to_close, detect_pivots_close_to_close


def _pivot_pairs(pivots):
    return [(p.index, p.price) for p in pivots]


# ---- P1-P6: pivot-level properties, one named fixture each ----

def test_rise_then_fall():
    series = [100, 110, 120, 130, 100, 90, 80]
    pivots = detect_pivots_close_to_close(series, threshold=20)
    assert _pivot_pairs(pivots) == [(0, 100), (3, 130), (6, 80)]


def test_fall_then_rise():
    """P3: two-sided seeding — symmetric to rise-then-fall, no directional bias."""
    series = [100, 90, 80, 70, 100, 110, 120]
    pivots = detect_pivots_close_to_close(series, threshold=20)
    assert _pivot_pairs(pivots) == [(0, 100), (3, 70), (6, 120)]


def test_no_trigger_emits_zero_pivots():
    """P6: no reversal, no legs — never synthesise one."""
    series = [100, 105, 95, 102, 98, 103]
    pivots = detect_pivots_close_to_close(series, threshold=50)
    assert pivots == []
    legs = detect_legs_close_to_close(series, threshold=50)
    assert legs == []


def test_monotone_drop():
    """P5: degenerate seed (seed index == confirm index) emits only one pivot."""
    series = [100, 90, 80, 70, 60, 50, 40]
    pivots = detect_pivots_close_to_close(series, threshold=20)
    assert _pivot_pairs(pivots) == [(0, 100), (6, 40)]


def test_double_swing():
    series = [100, 125, 95, 120, 90]
    pivots = detect_pivots_close_to_close(series, threshold=20)
    assert _pivot_pairs(pivots) == [(0, 100), (1, 125), (2, 95), (3, 120), (4, 90)]


def test_exact_threshold_touch_triggers():
    """A move of exactly T must trigger (>=, not strictly >)."""
    series = [100, 120]
    pivots = detect_pivots_close_to_close(series, threshold=20)
    assert _pivot_pairs(pivots) == [(0, 100), (1, 120)]


# ---- P4: simultaneous triggers (unseeded range > 2T) ----

def test_simultaneous_trigger_high_after_low():
    """i_hi > i_lo -> the leg that just ended is 'up', new direction 'down'."""
    series = [100, 50, 150, 100]  # low at i=1, high at i=2 (later) -> down_trig wins
    pivots = detect_pivots_close_to_close(series, threshold=20)
    # hi confirmed as pivot (150 @ i=2), dirn flips to 'down'
    assert (2, 150) in _pivot_pairs(pivots)


def test_simultaneous_trigger_low_after_high():
    """i_lo > i_hi -> the leg that just ended is 'down', new direction 'up'."""
    series = [100, 150, 50, 100]  # high at i=1, low at i=2 (later) -> up_trig wins
    pivots = detect_pivots_close_to_close(series, threshold=20)
    assert (2, 50) in _pivot_pairs(pivots)


# ---- P1, P2, P7, P8: leg-level properties across all fixtures ----

FIXTURES = {
    "rise_then_fall": [100, 110, 120, 130, 100, 90, 80],
    "fall_then_rise": [100, 90, 80, 70, 100, 110, 120],
    "monotone_drop": [100, 90, 80, 70, 60, 50, 40],
    "double_swing": [100, 125, 95, 120, 90],
    "exact_touch": [100, 120],
}


@pytest.mark.parametrize("series", FIXTURES.values(), ids=FIXTURES.keys())
def test_p1_pivots_strictly_increasing_in_time(series):
    pivots = detect_pivots_close_to_close(series, threshold=20)
    indices = [p.index for p in pivots]
    assert indices == sorted(set(indices)), "pivot indices must be strictly increasing"


@pytest.mark.parametrize("series", FIXTURES.values(), ids=FIXTURES.keys())
def test_p2_pivot_is_extreme_not_confirming_bar(series):
    """Assert end_price == max/min of the leg's own bar range, i.e. the pivot
    recorded is the running extreme, not wherever the reversal was confirmed."""
    legs = detect_legs_close_to_close(series, threshold=20)
    for leg in legs:
        bar_range = series[leg.start_index:leg.end_index + 1]
        if leg.direction == "up":
            assert leg.end_price == max(bar_range)
        else:
            assert leg.end_price == min(bar_range)


@pytest.mark.parametrize("series", FIXTURES.values(), ids=FIXTURES.keys())
def test_p7_confirmed_leg_magnitudes_meet_threshold(series):
    threshold = 20
    legs = detect_legs_close_to_close(series, threshold=threshold)
    for leg in legs:
        if leg.confirmed:
            assert leg.magnitude_pts >= threshold


def test_p8_leg_count_non_increasing_in_threshold():
    series = FIXTURES["double_swing"] * 3  # repeat to get more structure
    counts = [len(detect_legs_close_to_close(series, threshold=t)) for t in [5, 10, 20, 40, 80]]
    assert counts == sorted(counts, reverse=True)


def test_seed_leg_confirmed_when_magnitude_meets_threshold():
    """The seed (first) leg's start is unvalidated two-sided bookkeeping, not
    a confirmed pivot — but when its magnitude already clears the threshold,
    that's still a real, already-observed move (see _legs_from_pivots
    docstring, revisited): symmetric to the trailing leg, confirmed by size
    like any other leg, not excluded by position."""
    legs = detect_legs_close_to_close(FIXTURES["rise_then_fall"], threshold=20)
    assert len(legs) == 2  # 3 pivots -> 2 legs
    assert legs[0].magnitude_pts == 30
    assert legs[0].confirmed


def test_seed_leg_can_be_unconfirmed_when_its_own_magnitude_is_below_threshold():
    """Unlike interior/trailing legs (whose start is always a validated
    reversal, so magnitude >= threshold is structurally guaranteed), the seed
    leg's start is *not* a validated reversal — its own magnitude can
    genuinely fall short even though a threshold-sized move is what ended the
    unseeded phase. Here the reversal that ends the unseeded phase fires on
    130 - lo(90) = 40 >= 20, but the seed leg itself (the high-before-the-low
    bookkeeping, 100 -> the low 90) only spans 10 points — correctly left
    unconfirmed by the magnitude check, not by position."""
    series = [100, 90, 130, 85]
    legs = detect_legs_close_to_close(series, threshold=20)
    assert legs[0].magnitude_pts == 10
    assert not legs[0].confirmed


def test_trailing_leg_confirmed_when_magnitude_meets_threshold():
    """The trailing (last) leg's start IS a validated pivot; if its magnitude
    already clears the threshold, it's confirmed like any interior leg —
    whether the market kept moving after the scope ended is out of scope."""
    legs = detect_legs_close_to_close(FIXTURES["rise_then_fall"], threshold=20)
    assert legs[-1].magnitude_pts == 50
    assert legs[-1].confirmed


def test_single_leg_run_confirmed_when_magnitude_meets_threshold():
    """A run with only one leg total (2 pivots) is simultaneously the seed
    AND the trailing leg — both concerns apply to the same one rule now
    (`magnitude >= threshold`), so no position-based special-casing is needed
    even for this degenerate case."""
    series = [100, 90, 80, 70, 85]  # single leg, magnitude 30 (>= threshold)
    legs = detect_legs_close_to_close(series, threshold=20)
    assert len(legs) == 1
    assert legs[0].magnitude_pts == 30
    assert legs[0].confirmed


def test_nondegenerate_trailing_leg_always_meets_threshold():
    """A trailing leg distinct from the seed (n_legs >= 2) is structurally
    guaranteed magnitude >= threshold: the instant a reversal fires, the new
    leg's initial span already equals the triggering difference, which is by
    definition >= threshold, and it can only grow from there before the scope
    ends. Verified analytically and with a 2000-trial randomized check (see
    dev notes) — zero counterexamples. So in practice the magnitude check on
    the trailing leg always passes for genuine multi-leg runs; it remains an
    explicit condition in the code anyway, as documentation of intent and a
    safety net rather than a bet on this always holding forever."""
    for series in FIXTURES.values():
        legs = detect_legs_close_to_close(series, threshold=20)
        if len(legs) >= 2:
            assert legs[-1].magnitude_pts >= 20
            assert legs[-1].confirmed


def test_all_legs_confirmed_when_magnitude_meets_threshold_double_swing():
    legs = detect_legs_close_to_close(FIXTURES["double_swing"], threshold=20)
    assert len(legs) == 4
    assert legs[0].confirmed   # seed, magnitude 25 >= 20
    assert legs[1].confirmed   # interior
    assert legs[2].confirmed   # interior
    assert legs[-1].confirmed  # trailing, magnitude 30 >= 20


def test_trailing_leg_retracement_scan_extends_past_last_extreme():
    """A small pullback after the last recorded extreme, but before the scope
    ends, must still be captured once the trailing leg is confirmed — the
    scan can't stop at the extreme itself and silently ignore it."""
    # leg0: 100 -> 70 (seed, magnitude 30 >= 20, confirmed too). leg1
    # (trailing, distinct from seed): 70 -> 160 (extreme at index 4),
    # confirmed (magnitude 90 >= 20), then drifts down to 145 by the close
    # (15-point pullback, under threshold, so no new leg fires).
    series = [100, 70, 130, 140, 160, 155, 150, 145]
    legs = detect_legs_close_to_close(series, threshold=20)
    assert len(legs) == 2
    trailing = legs[-1]
    assert trailing.end_price == 160  # pivot itself unchanged: still the true peak
    assert trailing.confirmed         # magnitude 90 >= 20
    assert trailing.deepest_retr_pts == pytest.approx(15.0)  # 160 -> 145, captured
    assert trailing.deepest_retr_pts < 20  # invariant still holds


# ---- §2.8: THE INVARIANT (mandatory) ----

@pytest.mark.parametrize("series", FIXTURES.values(), ids=FIXTURES.keys())
@pytest.mark.parametrize("threshold", [5, 10, 20, 40])
def test_invariant_deepest_retracement_below_threshold(series, threshold):
    """For every confirmed leg at threshold T: deepest_retr_pts < T."""
    legs = detect_legs_close_to_close(series, threshold=threshold)
    for leg in legs:
        if leg.confirmed:
            assert leg.deepest_retr_pts < threshold, (
                f"invariant violated: leg {leg} has deepest_retr_pts="
                f"{leg.deepest_retr_pts} >= threshold={threshold}"
            )
