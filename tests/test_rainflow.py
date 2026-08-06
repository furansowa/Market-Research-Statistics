"""Tests for src/gyrations/rainflow.py (ASTM E1049 4-point rainflow counting)."""

from gyrations.rainflow import Cycle, count_cycles, miner_damage, turning_points


def test_turning_points_keeps_ends_and_direction_changes_only():
    tps = turning_points([0, 1, 2, 1, 3])
    assert [v for _, v in tps] == [0, 2, 1, 3]


def test_turning_points_collapses_flats_so_they_never_look_like_turns():
    tps = turning_points([0, 1, 1, 1, 2])
    assert [v for _, v in tps] == [0, 2]  # strictly monotonic once flats collapse


def test_monotonic_series_yields_no_full_cycles():
    cycles = count_cycles([0, 1, 2, 3, 4])
    assert all(not c.full for c in cycles)


def test_small_oscillation_nests_inside_the_large_excursion():
    # THE defining property: the 3->1 wiggle is extracted as its own small
    # cycle, and the big 0->4 move survives whole in the residue.
    cycles = count_cycles([0, 3, 1, 4, 0])
    full = [c for c in cycles if c.full]
    assert len(full) == 1
    assert full[0].rng == 2          # the inner 3->1 oscillation
    assert full[0].mean == 2.0       # (3+1)/2
    residue = [c for c in cycles if not c.full]
    assert max(c.rng for c in residue) == 4  # the large excursion, undestroyed


def test_repeated_equal_swings_extract_a_full_cycle():
    cycles = count_cycles([0, 5, 0, 5, 0])
    full = [c for c in cycles if c.full]
    assert len(full) == 1
    assert full[0].rng == 5
    assert full[0].mean == 2.5


def test_cycle_close_index_is_where_the_fourth_point_landed():
    # series index 3 holds the value 4 that closes the inner cycle
    cycles = count_cycles([0, 3, 1, 4, 0])
    full = [c for c in cycles if c.full][0]
    assert full.close_index == 3


def test_nested_hierarchy_is_recovered_at_multiple_scales():
    # a big swing with two different-sized wiggles inside it
    series = [0, 10, 8, 9, 2, 4, 3, 20]
    cycles = count_cycles(series)
    ranges = sorted(c.rng for c in cycles if c.full)
    assert 1 in ranges   # the 9->8 / 4->3 scale
    assert len(ranges) >= 2
    # the dominant excursion is preserved in the residue, not shredded
    assert max(c.rng for c in cycles if not c.full) >= 18


def test_total_weight_is_conserved_against_turning_point_count():
    # every turning point transition is accounted for exactly once:
    # 2 reversals per full cycle + 1 per residue half-cycle
    series = [0, 3, 1, 4, 0, 6, 2, 5]
    cycles = count_cycles(series)
    n_tp = len(turning_points(series))
    used = 2 * sum(1 for c in cycles if c.full) + sum(1 for c in cycles if not c.full)
    assert used == n_tp - 1


def test_miner_damage_scales_with_exponent_and_capacity():
    cycles = [Cycle(rng=2.0, mean=0.0, close_index=1, full=True),
              Cycle(rng=4.0, mean=0.0, close_index=2, full=True)]
    assert miner_damage(cycles, m=1.0) == 6.0
    assert miner_damage(cycles, m=2.0) == 20.0          # 4 + 16
    assert miner_damage(cycles, m=2.0, capacity=10.0) == 2.0


def test_miner_damage_half_weights_the_residue():
    cycles = [Cycle(rng=2.0, mean=0.0, close_index=1, full=False)]
    assert miner_damage(cycles, m=1.0) == 1.0  # 0.5 weight * 2.0


def test_miner_damage_up_to_index_only_counts_closed_cycles():
    cycles = [Cycle(rng=2.0, mean=0.0, close_index=5, full=True),
              Cycle(rng=3.0, mean=0.0, close_index=50, full=True)]
    assert miner_damage(cycles, m=1.0, up_to_index=10) == 2.0
    assert miner_damage(cycles, m=1.0, up_to_index=100) == 5.0
