"""Acceptance tests for src/query/legs.py against the real built DB (Gyration
Legs page). Cross-checks the query module's output against raw SQL run
directly in the test, in the same spirit as test_filters.py/test_gyrations_db.py.
"""

from query.legs import leg_aggregates_by_date, leg_pivots, leg_pair_aggregates_by_date, leg_detail_rows
from query.legs import _full_leg_rows

INSTRUMENT = "US30"
THRESHOLD = 50
V2_THRESHOLD = 40


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
    assert leg_pair_aggregates_by_date(conn, INSTRUMENT, V2_THRESHOLD, []) == {}
    assert leg_detail_rows(conn, INSTRUMENT, V2_THRESHOLD, []) == []


def test_leg_pair_aggregates_cross_checked_against_manual_pairing(conn):
    """Gyrations v2.0: build the expected 0&1/2&3/... pairing manually from
    the full leg rows and compare against leg_pair_aggregates_by_date."""
    dates = _sample_dates(conn)
    result = leg_pair_aggregates_by_date(conn, INSTRUMENT, V2_THRESHOLD, dates)
    assert result, "expected at least one date with a complete pair in the sample"

    legs = _full_leg_rows(conn, INSTRUMENT, V2_THRESHOLD, dates)
    by_date = {}
    for leg in legs:
        by_date.setdefault(leg["start_date"], []).append(leg)

    for date, expected in result.items():
        date_legs = by_date[date]
        pair_pts = [
            date_legs[i]["magnitude_pts"] + date_legs[i + 1]["magnitude_pts"]
            for i in range(0, len(date_legs) - 1, 2)
        ]
        pair_durations = [
            date_legs[i]["duration_min"] + date_legs[i + 1]["duration_min"]
            for i in range(0, len(date_legs) - 1, 2)
        ]
        assert pair_pts, f"{date}: expected at least one complete pair"
        assert expected["avg_pair_pts"] == sum(pair_pts) / len(pair_pts)
        assert expected["avg_pair_duration_min"] == sum(pair_durations) / len(pair_durations)


def test_leg_pair_aggregates_drops_trailing_unpaired_leg(conn):
    """A session with an odd leg count must exclude the trailing leg from
    the average, not silently pair it with something from the next date."""
    dates = _sample_dates(conn)
    legs = _full_leg_rows(conn, INSTRUMENT, V2_THRESHOLD, dates)
    by_date = {}
    for leg in legs:
        by_date.setdefault(leg["start_date"], []).append(leg)

    odd_dates = [d for d, date_legs in by_date.items() if len(date_legs) % 2 == 1 and len(date_legs) >= 3]
    assert odd_dates, "expected at least one session with an odd leg count in the sample"

    result = leg_pair_aggregates_by_date(conn, INSTRUMENT, V2_THRESHOLD, dates)
    date = odd_dates[0]
    date_legs = by_date[date]
    n_pairs = len(date_legs) // 2
    expected_avg = sum(
        date_legs[i]["magnitude_pts"] + date_legs[i + 1]["magnitude_pts"]
        for i in range(0, len(date_legs) - 1, 2)
    ) / n_pairs
    assert result[date]["avg_pair_pts"] == expected_avg


def test_leg_detail_rows_first_leg_of_each_session_is_1st(conn):
    dates = _sample_dates(conn, n=50)
    rows = leg_detail_rows(conn, INSTRUMENT, V2_THRESHOLD, dates)
    assert rows

    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    for date, date_rows in by_date.items():
        first = date_rows[0]
        assert first["pattern"] == "1st"
        assert first["time_ratio"] is None
        assert first["size_ratio"] is None
        assert first["gyration_size_pts"] is None


def test_leg_detail_rows_pattern_classification(conn):
    dates = _sample_dates(conn, n=50)
    rows = leg_detail_rows(conn, INSTRUMENT, V2_THRESHOLD, dates)

    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    checked_v_or_a = 0
    for date_rows in by_date.values():
        for i in range(1, len(date_rows)):
            row, prev = date_rows[i], date_rows[i - 1]
            bigger = row["size_pts"] > prev["size_pts"]
            if row["direction"] == "up":
                expected = "V1" if bigger else "V2"
            else:
                expected = "A1" if bigger else "A2"
            assert row["pattern"] == expected
            expected_time_ratio = (row["duration_min"] / prev["duration_min"]) if prev["duration_min"] else None
            expected_size_ratio = (row["size_pts"] / prev["size_pts"]) if prev["size_pts"] else None
            assert row["time_ratio"] == expected_time_ratio
            assert row["size_ratio"] == expected_size_ratio
            checked_v_or_a += 1
    assert checked_v_or_a > 0, "expected at least one non-first leg in the sample"


def test_leg_detail_rows_gyration_size_only_on_second_of_pair(conn):
    dates = _sample_dates(conn, n=50)
    rows = leg_detail_rows(conn, INSTRUMENT, V2_THRESHOLD, dates)

    by_date = {}
    for row in rows:
        by_date.setdefault(row["date"], []).append(row)

    checked = 0
    for date_rows in by_date.values():
        for i, row in enumerate(date_rows):
            if i % 2 == 1:
                assert row["gyration_size_pts"] == row["size_pts"] + date_rows[i - 1]["size_pts"]
                checked += 1
            else:
                assert row["gyration_size_pts"] is None
    assert checked > 0, "expected at least one second-of-pair leg in the sample"
