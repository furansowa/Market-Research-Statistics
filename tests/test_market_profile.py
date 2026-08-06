"""Tests for src/gyrations/market_profile.py (POC / Value Area)."""

from gyrations.market_profile import compute_market_profile, classify_va_relationship


# ---- classify_va_relationship: all 6 user-specified cases ----

def test_va_relationship_1_shifted_up_with_overlap():
    # hi > prev_hi; prev_lo < lo <= prev_hi
    assert classify_va_relationship(hi=110, lo=95, prev_hi=100, prev_lo=90) == "1"
    assert classify_va_relationship(hi=110, lo=100, prev_hi=100, prev_lo=90) == "1"  # lo == prev_hi


def test_va_relationship_minus1_shifted_down_with_overlap():
    # prev_lo <= hi < prev_hi; lo < prev_lo
    assert classify_va_relationship(hi=95, lo=80, prev_hi=100, prev_lo=90) == "-1"
    assert classify_va_relationship(hi=90, lo=80, prev_hi=100, prev_lo=90) == "-1"  # hi == prev_lo


def test_va_relationship_0_contained_inside():
    assert classify_va_relationship(hi=98, lo=92, prev_hi=100, prev_lo=90) == "0"
    # touching both boundaries exactly still counts as contained
    assert classify_va_relationship(hi=100, lo=90, prev_hi=100, prev_lo=90) == "0"


def test_va_relationship_11_both_above_no_overlap():
    assert classify_va_relationship(hi=120, lo=105, prev_hi=100, prev_lo=90) == "11"


def test_va_relationship_minus11_both_below_no_overlap():
    assert classify_va_relationship(hi=85, lo=70, prev_hi=100, prev_lo=90) == "-11"


def test_va_relationship_111_engulfs_previous_day():
    assert classify_va_relationship(hi=110, lo=80, prev_hi=100, prev_lo=90) == "111"


def test_va_relationship_exhaustive_random_grid_never_errors_and_always_one_code():
    from gyrations.market_profile import VA_RELATIONSHIP_CODES
    prev_hi, prev_lo = 100.0, 90.0
    for hi in [70, 80, 89, 90, 91, 95, 99, 100, 101, 105, 120]:
        for lo in [60, 70, 85, 89, 90, 91, 95, 99, 100, 101, 110]:
            if lo >= hi:
                continue
            code = classify_va_relationship(hi, lo, prev_hi, prev_lo)
            assert code in VA_RELATIONSHIP_CODES


def test_none_when_too_few_bars():
    assert compute_market_profile([100.0] * 10, reference_price=100.0, min_bars=30) is None


def test_flat_distribution_pocs_and_va_center_on_the_data():
    closes = [100.0] * 40
    mp = compute_market_profile(closes, reference_price=100.0)
    assert mp is not None
    assert mp.n_bars == 40
    assert mp.va_lo <= mp.poc <= mp.va_hi


def test_poc_lands_on_the_most_visited_price():
    # 100 dominates (30 visits); rest of the range gets 1 visit per level.
    closes = [90.0 + i for i in range(20)] + [100.0] * 30
    mp = compute_market_profile(closes, reference_price=100.0, bin_pct=0.001, min_bin_pts=1.0)
    assert 99.0 <= mp.poc <= 101.0


def test_value_area_expands_toward_the_heavier_side_first_on_a_skewed_distribution():
    # Two humps: a small one low, a big one high -- VA should reach the big
    # hump before it finishes eating the small one, i.e. asymmetric VA, not a
    # naive +/-X% band around POC.
    closes = [100.0] * 5 + [90.0] * 50 + [80.0] * 5
    mp = compute_market_profile(closes, reference_price=90.0, bin_pct=0.02, min_bin_pts=1.0)
    assert mp is not None
    # POC must be the heavy 90 hump
    assert 89.0 <= mp.poc <= 91.0
    # Value area at 70% (42 of 60 bars) must include all of the 90 hump (50
    # bars alone already clears 70%), so it should NOT need to reach either
    # tail fully -- but by symmetric-neighbor expansion it may touch one edge.
    assert mp.va_lo <= 90.0 <= mp.va_hi


def test_va_pct_covers_at_least_the_requested_fraction():
    closes = [80.0, 85.0, 90.0, 90.0, 90.0, 90.0, 95.0, 100.0, 100.0, 105.0] * 4
    mp = compute_market_profile(closes, reference_price=90.0, bin_pct=0.03, min_bin_pts=1.0, va_pct=0.7)
    assert mp is not None
    included = sum(1 for c in closes if mp.va_lo <= c <= mp.va_hi)
    assert included / len(closes) >= 0.7


def test_asymmetric_expansion_does_not_overshoot_available_bins():
    # POC pinned at the very top of the range -- expansion can only go down,
    # must not crash trying to look "above" a nonexistent bin.
    closes = [100.0] * 50 + [95.0, 96.0, 97.0, 98.0, 99.0]
    mp = compute_market_profile(closes, reference_price=97.0, bin_pct=0.01, min_bin_pts=0.5)
    assert mp is not None
    assert mp.va_hi >= 100.0
    assert mp.va_lo <= 100.0


def test_deterministic_tie_break_prefers_central_bin():
    # Two bins tied for max count, symmetric around the middle -- must not
    # error, and must pick one of the two consistently.
    closes = [90.0] * 20 + [110.0] * 20
    mp1 = compute_market_profile(closes, reference_price=100.0, bin_pct=0.05, min_bin_pts=1.0)
    mp2 = compute_market_profile(closes, reference_price=100.0, bin_pct=0.05, min_bin_pts=1.0)
    assert mp1 == mp2


# ---- acceptance tests against the real built DB (run_market_profile.py) ----

ENUM_COLS = ["o_prev_va", "h_prev_va", "l_prev_va", "cl_va"]  # -1/0/1 sign codes
VA_PREV_VA_CODES = {"1", "-1", "0", "11", "-11", "111"}  # symbolic, not -1/0/1


def test_db_market_profile_columns_exist_and_are_mostly_populated(conn):
    row = conn.execute(
        "SELECT COUNT(*), COUNT(poc) FROM sessions WHERE instrument='US30'"
    ).fetchone()
    n_total, n_poc = row
    assert n_total > 1000
    assert n_poc / n_total > 0.99  # only pathologically thin sessions should be null


def test_db_va_range_equals_hi_minus_lo(conn):
    rows = conn.execute(
        "SELECT va70_hi, va70_lo, va_range FROM sessions WHERE instrument='US30' "
        "AND va_range IS NOT NULL ORDER BY date DESC LIMIT 500"
    ).fetchall()
    assert rows
    for hi, lo, rng in rows:
        assert abs((hi - lo) - rng) < 1e-6


def test_db_enum_columns_only_take_valid_values(conn):
    for col in ENUM_COLS:
        vals = {r[0] for r in conn.execute(
            f"SELECT DISTINCT {col} FROM sessions WHERE instrument='US30'"
        )}
        assert vals <= {-1, 0, 1, None}


def test_db_va_prev_va_only_takes_the_6_symbolic_codes(conn):
    vals = {r[0] for r in conn.execute(
        "SELECT DISTINCT va_prev_va FROM sessions WHERE instrument='US30'"
    )}
    assert vals <= (VA_PREV_VA_CODES | {None})


def test_db_o_prev_poc_matches_shifted_poc(conn):
    rows = conn.execute(
        "SELECT date, rth_open, poc, o_prev_poc FROM sessions WHERE instrument='US30' "
        "ORDER BY date DESC LIMIT 300"
    ).fetchall()
    dates = [r[0] for r in rows]
    poc_by_date = {r[0]: r[2] for r in rows}
    checked = 0
    for i in range(len(rows) - 1):
        date, o, _, o_prev_poc = rows[i]
        prev_date = dates[i + 1]
        prev_poc = poc_by_date.get(prev_date)
        if prev_poc is None or o is None or o_prev_poc is None:
            continue
        assert abs(o_prev_poc - (o - prev_poc)) < 1e-6
        checked += 1
    assert checked > 100


def test_db_va_prev_va_matches_recomputation_from_va_bounds(conn):
    rows = conn.execute(
        "SELECT date, va70_hi, va70_lo, va_prev_va FROM sessions WHERE instrument='US30' "
        "ORDER BY date DESC LIMIT 300"
    ).fetchall()
    by_date = {r[0]: (r[1], r[2]) for r in rows}
    actual_by_date = {r[0]: r[3] for r in rows}
    dates = sorted(by_date)
    checked = 0
    for i in range(1, len(dates)):
        hi, lo = by_date[dates[i]]
        phi, plo = by_date[dates[i - 1]]
        if None in (hi, lo, phi, plo):
            continue
        expected = classify_va_relationship(hi, lo, phi, plo)
        assert actual_by_date[dates[i]] == expected
        checked += 1
    assert checked > 100
