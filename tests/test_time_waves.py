"""Verification for the TimeWaves port.

Case 1 is a full hand-trace of the ProRealTime state machine; cases 2-3 pin
the known seeding blind spot and the strict variant that removes it, so that
"fixing" the port silently would fail a test rather than quietly change every
statistic built on top of it.
"""

import numpy as np
import pytest

from gyrations.time_waves import detect_time_legs


def _flat(series):
    """high == low == close, so the detector reduces to a 1-D series."""
    a = np.asarray(series, dtype=float)
    return a, a, a


def test_hand_traced_state_machine():
    # Hand trace, min_bars=3, series [10,11,12,11,10,9,8,9,10,11,12,13]:
    #   bar0 seed ext=10@0; bar1 ext=11@1; bar2 ext=12@2
    #   bars 3,4,5 -> elapsed 1,2,3 -> confirm at bar5: leg (0,10)->(2,12) up
    #     pivot=(2,12); trend down; ext seeded from bar5 low = 9 @5
    #   bar6 ext=8@6; bars 7,8,9 -> elapsed 1,2,3 -> confirm at bar9:
    #     leg (2,12)->(6,8) down; pivot=(6,8); trend up; ext = high[9]=11 @9
    #   bar10 ext=12@10; bar11 ext=13@11 -> never confirmed, not emitted
    h, l, c = _flat([10, 11, 12, 11, 10, 9, 8, 9, 10, 11, 12, 13])
    legs = detect_time_legs(h, l, c, min_bars=3)

    assert len(legs) == 2
    assert (legs[0].start_index, legs[0].end_index) == (0, 2)
    assert (legs[0].start_price, legs[0].end_price) == (10.0, 12.0)
    assert legs[0].direction == "up"
    assert (legs[1].start_index, legs[1].end_index) == (2, 6)
    assert (legs[1].start_price, legs[1].end_price) == (12.0, 8.0)
    assert legs[1].direction == "down"


def test_every_leg_spans_at_least_min_bars():
    rng = np.random.default_rng(7)
    price = 100 + np.cumsum(rng.normal(0, 1, 4000))
    h, l, c = price + 0.5, price - 0.5, price
    for min_bars in (5, 10, 25):
        legs = detect_time_legs(h, l, c, min_bars=min_bars)
        assert legs, "detector produced no legs on a 4000-bar random walk"
        # the seed leg starts at bar 0 and can be shorter; every later leg
        # is bounded below by construction
        assert all(leg.duration_bars >= min_bars for leg in legs[1:])


def test_legs_strictly_alternate_direction():
    """Merrill classification requires alternation; without it the 5-pivot
    rank strings stop matching the 32 valid patterns."""
    rng = np.random.default_rng(11)
    price = 100 + np.cumsum(rng.normal(0, 1, 5000))
    legs = detect_time_legs(price + 0.5, price - 0.5, price, min_bars=8)
    dirs = [leg.direction for leg in legs]
    assert all(a != b for a, b in zip(dirs, dirs[1:]))


def test_legs_are_chained_end_to_start():
    rng = np.random.default_rng(3)
    price = 100 + np.cumsum(rng.normal(0, 1, 3000))
    legs = detect_time_legs(price + 0.5, price - 0.5, price, min_bars=12)
    for a, b in zip(legs, legs[1:]):
        assert a.end_index == b.start_index
        assert a.end_price == b.start_price


def test_prt_mode_preserves_the_known_seeding_blind_spot():
    """The genuine low of 5 at bar 2 is invisible to the new down-leg, because
    the PRT source reseeds from the confirmation bar. Documented, not a bug."""
    h, l, c = _flat([10, 12, 5, 11, 11, 11, 11])
    legs = detect_time_legs(h, l, c, min_bars=3, mode="prt")

    assert legs[0].end_index == 1 and legs[0].end_price == 12.0
    # the down-leg that follows never sees the 5
    assert all(leg.end_price != 5.0 for leg in legs[1:])


def test_scan_back_mode_recovers_the_missed_extreme():
    h, l, c = _flat([10, 12, 5, 11, 11, 11, 11])
    legs = detect_time_legs(h, l, c, min_bars=3, mode="scan_back")

    assert legs[0].end_index == 1 and legs[0].end_price == 12.0
    # with scan_back the pending down-leg is seeded at the true low (5 @ bar 2)
    assert len(legs) >= 2
    assert legs[1].end_index == 2 and legs[1].end_price == 5.0


def test_scan_back_still_alternates_and_chains():
    rng = np.random.default_rng(23)
    price = 100 + np.cumsum(rng.normal(0, 1, 5000))
    legs = detect_time_legs(price + 0.5, price - 0.5, price, min_bars=10,
                            mode="scan_back")
    dirs = [leg.direction for leg in legs]
    assert all(a != b for a, b in zip(dirs, dirs[1:]))
    for a, b in zip(legs, legs[1:]):
        assert a.end_index == b.start_index and a.end_price == b.start_price


def test_use_close_ignores_wicks():
    """With UseClose the detector must never pick up a high/low that the close
    series doesn't contain."""
    close = np.array([10, 11, 12, 11, 10, 9, 8, 9, 10, 11], dtype=float)
    high = close + 50.0
    low = close - 50.0
    legs = detect_time_legs(high, low, close, min_bars=3, use_close=True)
    prices = [p for leg in legs for p in (leg.start_price, leg.end_price)]
    assert all(p in set(close.tolist()) for p in prices)


def test_empty_and_short_input():
    empty = np.array([], dtype=float)
    assert detect_time_legs(empty, empty, empty, min_bars=5) == []
    h, l, c = _flat([1.0, 2.0])
    assert detect_time_legs(h, l, c, min_bars=50) == []


def test_unknown_mode_rejected():
    h, l, c = _flat([1.0, 2.0, 3.0])
    with pytest.raises(ValueError):
        detect_time_legs(h, l, c, min_bars=2, mode="nope")
