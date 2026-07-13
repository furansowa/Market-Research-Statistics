"""Tests for extreme_to_extreme mode (Phase 2 spec §2.2, §2.6, §8).

Complements test_gyrations.py (close_to_close). Covers the acceptance tests
named explicitly in spec §8:
  - bar_direction tie-break produces different pivots for the same bar
    depending on close>=open vs close<open.
  - extreme_to_extreme yields strictly more legs than close_to_close at the
    same (scope, threshold).
  - P1-P8 re-verified with highs/lows substituted for closes.
  - The invariant, re-verified (requires the §2.6 terminal-bar rule).
"""

import pytest

from gyrations.detect import (
    detect_legs_close_to_close,
    detect_legs_extreme_to_extreme,
    detect_pivots_extreme_to_extreme,
)


def _pivot_pairs(pivots):
    return [(p.index, p.price) for p in pivots]


# ---- bar_direction tie-break: hand-derived, produces different pivots ----
#
# bar0: (100,100,100,100) — initial point.
# bar1: (100,140,100,140) — bullish, unseeded phase: low=100 (no-op), then
#       high=140 triggers up_trig (140-100=40>=20). dirn='up', ext=140 @ i=1.
#       Identical in both variants (unambiguous bar, order doesn't matter).
# bar2: high=165, low=115 (ext-low=140-115=25>=20 would reverse; high=165>140
#       would extend) — the ambiguous bar. o/c differ between variants to
#       flip bar_direction's low-first vs high-first rule.

def test_bar_direction_bullish_reverses_before_extending():
    """close >= open -> low tested first -> reversal fires using the OLD
    ext (140 from bar1), the bar's own high (165) never gets a chance to
    extend first."""
    bars = [
        (100, 100, 100, 100),
        (100, 140, 100, 140),
        (120, 165, 115, 160),  # o=120, c=160: c>=o, bullish
    ]
    pivots = detect_pivots_extreme_to_extreme(bars, threshold=20, tiebreak="bar_direction")
    assert (1, 140) in _pivot_pairs(pivots), "bar1's peak must be locked in before the reversal"


def test_bar_direction_bearish_extends_before_reversing():
    """close < open -> high tested first -> the bar's own high (165) extends
    ext BEFORE the low triggers the reversal, so 140 is superseded and never
    becomes a pivot; 165 is the true peak instead."""
    bars = [
        (100, 100, 100, 100),
        (100, 140, 100, 140),
        (160, 165, 115, 120),  # o=160, c=120: c<o, bearish
    ]
    pivots = detect_pivots_extreme_to_extreme(bars, threshold=20, tiebreak="bar_direction")
    assert (1, 140) not in _pivot_pairs(pivots), "140 must be superseded by the higher 165"
    assert (2, 165) in _pivot_pairs(pivots), "165 (extended before reversing) is the true peak"


def test_bar_direction_variants_produce_different_pivots():
    """The same OHLC high/low, only open/close (hence bar direction) differ,
    must produce genuinely different pivot sets — this is the whole point
    of needing a tiebreak at all."""
    common = [(100, 100, 100, 100), (100, 140, 100, 140)]
    bullish = common + [(120, 165, 115, 160)]
    bearish = common + [(160, 165, 115, 120)]

    pivots_bullish = _pivot_pairs(detect_pivots_extreme_to_extreme(bullish, 20, "bar_direction"))
    pivots_bearish = _pivot_pairs(detect_pivots_extreme_to_extreme(bearish, 20, "bar_direction"))
    assert pivots_bullish != pivots_bearish


# ---- extreme_to_extreme yields strictly more legs than close_to_close ----

def test_extreme_yields_more_legs_than_close_with_wick_noise():
    """Constant closes (zero close-mode legs) but alternating high/low wicks
    that individually cross the threshold -> extreme mode must find legs
    close mode structurally cannot see."""
    threshold = 20
    bars = []
    closes = []
    for i in range(20):
        if i % 2 == 0:
            bar = (100, 100 + 30, 100, 100)  # upper wick
        else:
            bar = (100, 100, 100 - 30, 100)  # lower wick
        bars.append(bar)
        closes.append(bar[3])

    close_legs = detect_legs_close_to_close(closes, threshold)
    extreme_legs = detect_legs_extreme_to_extreme(bars, threshold, tiebreak="bar_direction")

    assert len(close_legs) == 0, "constant closes must never trigger close_to_close"
    assert len(extreme_legs) > len(close_legs)


# ---- P1-P8 adapted (highs/lows substituted for closes, spec §2.4 closing note) ----

def _bars_from_closes_flat(closes, wick=0.0):
    """OHLC bars with open==close==the given series value, high/low widened
    by `wick` on each side (0 = no wick, so extreme mode degenerates to
    close-like behavior for property-equivalence checks)."""
    return [(c, c + wick, c - wick, c) for c in closes]


FIXTURES = {
    "rise_then_fall": [100, 110, 120, 130, 100, 90, 80],
    "fall_then_rise": [100, 90, 80, 70, 100, 110, 120],
    "monotone_drop": [100, 90, 80, 70, 60, 50, 40],
    "double_swing": [100, 125, 95, 120, 90],
    "exact_touch": [100, 120],
}


@pytest.mark.parametrize("closes", FIXTURES.values(), ids=FIXTURES.keys())
def test_p1_pivots_strictly_increasing_in_time(closes):
    bars = _bars_from_closes_flat(closes)
    pivots = detect_pivots_extreme_to_extreme(bars, threshold=20)
    indices = [p.index for p in pivots]
    assert indices == sorted(set(indices))


@pytest.mark.parametrize("closes", FIXTURES.values(), ids=FIXTURES.keys())
def test_p7_confirmed_leg_magnitudes_meet_threshold(closes):
    bars = _bars_from_closes_flat(closes)
    threshold = 20
    legs = detect_legs_extreme_to_extreme(bars, threshold)
    for leg in legs:
        if leg.confirmed:
            assert leg.magnitude_pts >= threshold


def test_p8_leg_count_non_increasing_in_threshold():
    bars = _bars_from_closes_flat(FIXTURES["double_swing"] * 3, wick=3.0)
    counts = [len(detect_legs_extreme_to_extreme(bars, t)) for t in [5, 10, 20, 40, 80]]
    assert counts == sorted(counts, reverse=True)


# ---- §2.8: THE INVARIANT, re-verified for extreme_to_extreme ----
# Uses real wicks (not the degenerate wick=0 case) so the terminal-bar rule
# is actually exercised — without it, every reversal bar would report a
# retracement >= threshold.

def _bars_with_wicks(closes, wick):
    bars = []
    for i, c in enumerate(closes):
        o = closes[i - 1] if i > 0 else c
        bars.append((o, c + wick, c - wick, c))
    return bars


@pytest.mark.parametrize("closes", FIXTURES.values(), ids=FIXTURES.keys())
@pytest.mark.parametrize("threshold", [5, 10, 20, 40])
def test_invariant_deepest_retracement_below_threshold(closes, threshold):
    bars = _bars_with_wicks(closes, wick=threshold * 0.4)
    legs = detect_legs_extreme_to_extreme(bars, threshold, tiebreak="bar_direction")
    for leg in legs:
        if leg.confirmed:
            assert leg.deepest_retr_pts < threshold, (
                f"invariant violated: leg {leg} deepest_retr_pts="
                f"{leg.deepest_retr_pts} >= threshold={threshold}"
            )


@pytest.mark.parametrize("tiebreak", ["bar_direction", "adverse_first", "favourable_first"])
def test_invariant_holds_for_all_tiebreak_modes(tiebreak):
    bars = _bars_with_wicks(FIXTURES["double_swing"] * 4, wick=8.0)
    legs = detect_legs_extreme_to_extreme(bars, threshold=20, tiebreak=tiebreak)
    for leg in legs:
        if leg.confirmed:
            assert leg.deepest_retr_pts < 20
