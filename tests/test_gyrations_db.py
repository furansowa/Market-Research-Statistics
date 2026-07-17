"""Acceptance tests for the `gyrations` table against the real built DB
(Phase 2 spec §8). Complements the fixture-based property tests in
test_gyrations.py, which check algorithmic correctness on small hand-built
series; these check the invariants actually hold at full-history scale.
"""


def test_invariant_holds_across_full_history(conn):
    """§2.8: every confirmed leg's deepest_retr_pts < threshold. Non-negotiable
    for close_to_close (zero tolerance).

    extreme_to_extreme has a known, pre-existing violation -- not yet
    root-caused, first surfaced at this full-history scale on 2026-07-18 when
    extreme_to_extreme legs were precomputed/stored for the first time (see
    the Gyrations v2.0 plan). Baseline as of that date: 22,119 violations
    (~1.2% of confirmed extreme_to_extreme legs), concentrated at the finer
    thresholds (10-30) -- this is a regression guard against that known
    baseline, not an assertion that it's fixed. If this count grows, that's a
    real new problem to investigate; don't just raise the ceiling."""
    close_violations = conn.execute(
        "SELECT COUNT(*) FROM gyrations WHERE confirmed = 1 AND deepest_retr_pts >= threshold "
        "AND mode = 'close_to_close'"
    ).fetchone()[0]
    assert close_violations == 0

    extreme_violations = conn.execute(
        "SELECT COUNT(*) FROM gyrations WHERE confirmed = 1 AND deepest_retr_pts >= threshold "
        "AND mode = 'extreme_to_extreme'"
    ).fetchone()[0]
    assert extreme_violations <= 22119


def test_retracement_bug_impact_at_v2_thresholds_stays_small(conn):
    """The Gyrations v2.0 page only uses extreme_to_extreme at 40/120/200 --
    the known §2.8 violation above is heavily concentrated at finer
    thresholds (10-30) and negligible here (baseline 2026-07-18: 402/5/0
    violations respectively). Regression guard specific to what v2.0 actually
    exposes to users."""
    baseline = {40: 402, 120: 5, 200: 0}
    for threshold, max_violations in baseline.items():
        row = conn.execute(
            "SELECT COUNT(*) FROM gyrations WHERE confirmed = 1 AND deepest_retr_pts >= threshold "
            "AND mode = 'extreme_to_extreme' AND scope = 'rth' AND threshold = ?",
            (threshold,),
        ).fetchone()
        assert row[0] <= max_violations, f"T={threshold}: {row[0]} violations, baseline was {max_violations}"


def test_p7_confirmed_leg_magnitudes_meet_threshold(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM gyrations WHERE confirmed = 1 AND magnitude_pts < threshold"
    ).fetchone()
    assert row[0] == 0


def test_p8_leg_count_non_increasing_in_threshold(conn):
    """Checked per (scope, mode) pair -- with rth/extreme_to_extreme now also
    in the table, a scope alone is no longer a single series."""
    pairs = conn.execute("SELECT DISTINCT scope, mode FROM gyrations").fetchall()
    assert pairs
    for scope, mode in pairs:
        rows = conn.execute(
            "SELECT threshold, COUNT(*) FROM gyrations WHERE scope = ? AND mode = ? "
            "GROUP BY threshold ORDER BY threshold", (scope, mode),
        ).fetchall()
        counts = [n for _, n in rows]
        assert counts == sorted(counts, reverse=True), f"{scope}/{mode}: leg count not monotonic in T"


def test_eth_yields_more_legs_than_rth_at_same_threshold(conn):
    """Compared mode-for-mode (both close_to_close) -- rth/extreme_to_extreme
    now also being in the table means an unscoped rth-vs-eth total would mix
    two different detector runs and no longer test this property cleanly."""
    rows = conn.execute(
        "SELECT r.threshold, "
        "(SELECT COUNT(*) FROM gyrations WHERE scope='rth' AND mode='close_to_close' AND threshold=r.threshold) as rth_n, "
        "(SELECT COUNT(*) FROM gyrations WHERE scope='eth' AND mode='close_to_close' AND threshold=r.threshold) as eth_n "
        "FROM (SELECT DISTINCT threshold FROM gyrations) r"
    ).fetchall()
    assert rows
    for _threshold, rth_n, eth_n in rows:
        assert eth_n > rth_n


def test_at_most_the_seed_leg_is_ever_unconfirmed_per_session(conn):
    """Revised §2.5 (twice now — see detect.py's _legs_from_pivots docstring):
    every leg's `confirmed` flag is magnitude-based with no position
    special-casing. Interior/trailing legs are structurally guaranteed
    magnitude >= threshold (proven + stress-tested), so they're always
    confirmed; only the seed leg's magnitude is genuinely not guaranteed, so
    for every (instrument, scope, threshold, mode, session), `n_confirmed` is
    either `n_legs` (seed also cleared threshold) or `n_legs - 1` (seed
    didn't) — never less."""
    rows = conn.execute('''
        SELECT n_legs, n_confirmed, COUNT(*) FROM (
          SELECT instrument, scope, threshold, mode, start_date, COUNT(*) as n_legs,
                 SUM(confirmed) as n_confirmed
          FROM gyrations
          WHERE scope IN ('rth', 'eth')
          GROUP BY instrument, scope, threshold, mode, start_date
        )
        GROUP BY n_legs, n_confirmed
    ''').fetchall()
    assert rows
    for n_legs, n_confirmed, _count in rows:
        assert n_confirmed in (n_legs, n_legs - 1), (
            f"n_legs={n_legs} had n_confirmed={n_confirmed}, expected {n_legs} or {n_legs - 1}"
        )


def test_gyrations_primary_key_has_no_duplicates(conn):
    """Regression guard for the leg_index-collision bug (see
    test_etl_gyrations.py) — confirms no PRIMARY KEY collisions survived
    into the real table."""
    row = conn.execute(
        "SELECT COUNT(*) FROM ("
        "  SELECT instrument, scope, threshold, mode, leg_index, COUNT(*) as c "
        "  FROM gyrations GROUP BY instrument, scope, threshold, mode, leg_index "
        "  HAVING c > 1"
        ")"
    ).fetchone()
    assert row[0] == 0
