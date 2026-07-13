"""Acceptance tests for the `gyrations` table against the real built DB
(Phase 2 spec §8). Complements the fixture-based property tests in
test_gyrations.py, which check algorithmic correctness on small hand-built
series; these check the invariants actually hold at full-history scale.
"""


def test_invariant_holds_across_full_history(conn):
    """§2.8: every confirmed leg's deepest_retr_pts < threshold. Non-negotiable,
    across all scopes/thresholds/modes present in the table."""
    row = conn.execute(
        "SELECT COUNT(*) FROM gyrations WHERE confirmed = 1 AND deepest_retr_pts >= threshold"
    ).fetchone()
    assert row[0] == 0


def test_p7_confirmed_leg_magnitudes_meet_threshold(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM gyrations WHERE confirmed = 1 AND magnitude_pts < threshold"
    ).fetchone()
    assert row[0] == 0


def test_p8_leg_count_non_increasing_in_threshold(conn):
    for scope in ("rth", "eth"):
        rows = conn.execute(
            "SELECT threshold, COUNT(*) FROM gyrations WHERE scope = ? "
            "GROUP BY threshold ORDER BY threshold", (scope,)
        ).fetchall()
        counts = [n for _, n in rows]
        assert counts == sorted(counts, reverse=True), f"{scope}: leg count not monotonic in T"


def test_eth_yields_more_legs_than_rth_at_same_threshold(conn):
    rows = conn.execute(
        "SELECT r.threshold, "
        "(SELECT COUNT(*) FROM gyrations WHERE scope='rth' AND threshold=r.threshold) as rth_n, "
        "(SELECT COUNT(*) FROM gyrations WHERE scope='eth' AND threshold=r.threshold) as eth_n "
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
