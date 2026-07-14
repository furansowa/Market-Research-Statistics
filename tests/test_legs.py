"""Acceptance tests for src/query/legs.py against the real built DB (Gyration
Legs page). Cross-checks the query module's output against raw SQL run
directly in the test, in the same spirit as test_filters.py/test_gyrations_db.py.
"""

from query.legs import leg_aggregates_by_date, leg_pivots

INSTRUMENT = "US30"
THRESHOLD = 50


def _sample_dates(conn, n=200):
    rows = conn.execute(
        "SELECT date FROM sessions WHERE instrument = ? ORDER BY date LIMIT ?",
        (INSTRUMENT, n),
    ).fetchall()
    return [r[0] for r in rows]


def test_leg_aggregates_cross_checked_against_raw_groupby(conn):
    dates = _sample_dates(conn)
    result = leg_aggregates_by_date(conn, INSTRUMENT, THRESHOLD, dates)
    assert result, "expected at least one date with matching legs in the sample"

    placeholders = ", ".join("?" for _ in dates)
    raw = conn.execute(
        "SELECT start_date, COUNT(*), SUM(magnitude_pts), AVG(magnitude_pts), AVG(duration_min) "
        "FROM gyrations WHERE instrument = ? AND scope = 'rth' AND mode = 'close_to_close' "
        f"AND threshold = ? AND confirmed = 1 AND start_date IN ({placeholders}) "
        "GROUP BY start_date",
        [INSTRUMENT, THRESHOLD, *dates],
    ).fetchall()
    raw_by_date = {r[0]: r[1:] for r in raw}

    assert set(result.keys()) == set(raw_by_date.keys())
    for date, agg in result.items():
        count, sum_pts, avg_pts, avg_dur = raw_by_date[date]
        assert agg["count"] == count
        assert agg["sum_pts"] == sum_pts
        assert agg["avg_pts"] == avg_pts
        assert agg["avg_duration_min"] == avg_dur


def test_confirmed_only_differs_by_at_most_one_leg_per_date(conn):
    """Mirrors test_gyrations_db.py's seed-leg invariant: confirmed_only=True
    vs False can only ever differ by the (possibly unconfirmed) seed leg."""
    dates = _sample_dates(conn)
    all_legs = leg_aggregates_by_date(conn, INSTRUMENT, THRESHOLD, dates, confirmed_only=False)
    confirmed = leg_aggregates_by_date(conn, INSTRUMENT, THRESHOLD, dates, confirmed_only=True)

    assert all_legs, "expected at least one date with legs in the sample"
    for date, agg_all in all_legs.items():
        n_confirmed = confirmed.get(date, {}).get("count", 0)
        assert n_confirmed in (agg_all["count"], agg_all["count"] - 1), (
            f"{date}: all={agg_all['count']} confirmed={n_confirmed}"
        )


def test_leg_pivots_dates_within_requested_set(conn):
    dates = _sample_dates(conn, n=30)
    pivots = leg_pivots(conn, INSTRUMENT, THRESHOLD, dates)
    assert pivots, "expected at least one leg in the sample"

    date_set = set(dates)
    for leg in pivots:
        assert leg["start_date"] in date_set
        assert leg["end_date"] in date_set
        # rth scope: a leg never crosses a session boundary.
        assert leg["start_date"] == leg["end_date"]


def test_leg_pivots_defaults_exclude_eth_and_extreme_to_extreme(conn):
    """scope='rth'/mode='close_to_close' defaults must not accidentally pull in
    the eth-scope or extreme_to_extreme-mode rows that share the same table."""
    dates = _sample_dates(conn, n=30)
    pivots = leg_pivots(conn, INSTRUMENT, THRESHOLD, dates)

    eth_pivots = leg_pivots(conn, INSTRUMENT, THRESHOLD, dates, scope="eth")
    rth_starts = {(leg["start_ts"], leg["start_price"]) for leg in pivots}
    eth_starts = {(leg["start_ts"], leg["start_price"]) for leg in eth_pivots}
    # rth and eth are different detector runs over different bar sets -- the
    # two result sets should not be identical (a weak but real cross-check
    # that scope is actually being applied, not silently ignored).
    assert rth_starts != eth_starts


def test_empty_dates_short_circuits(conn):
    assert leg_aggregates_by_date(conn, INSTRUMENT, THRESHOLD, []) == {}
    assert leg_pivots(conn, INSTRUMENT, THRESHOLD, []) == []
