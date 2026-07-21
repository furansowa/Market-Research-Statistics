r"""Tests for src/gyrations/shapes.py (macro shape templates) plus acceptance
checks on the session_shapes / shape_swings tables built by run_shapes.py.
"""

from gyrations.shapes import classify_session, macro_pivots, shape_name

THRESHOLD = 120


def _leg(direction, start_price, end_price, start_ts, end_ts):
    return {
        "direction": direction,
        "start_price": start_price,
        "end_price": end_price,
        "start_ts": start_ts,
        "end_ts": end_ts,
    }


def _ts(i):
    return f"2026-01-05T{9 + i // 60:02d}:{i % 60:02d}:00"


# ---- synthetic unit tests ----

def test_flat_no_legs():
    res = classify_session([])
    assert res["shape"] == "flat"
    assert res["swings"] == ""
    assert res["pivots"] == []
    assert res["path_start"] is None


def test_one_leg_up_is_slash():
    res = classify_session([_leg("up", 100, 300, _ts(0), _ts(60))])
    assert res["shape"] == "/"
    assert res["swings"] == "U"


def test_v_day():
    legs = [
        _leg("down", 100, -100, _ts(0), _ts(60)),
        _leg("up", -100, 150, _ts(60), _ts(180)),
    ]
    res = classify_session(legs)
    assert res["shape"] == "V"
    assert res["swings"] == "DU"


def test_slash_day_with_interior_dips():
    # 5 legs, but the down legs never take out a running low -> still "/"
    legs = [
        _leg("up", 0, 300, _ts(0), _ts(30)),
        _leg("down", 300, 150, _ts(30), _ts(60)),
        _leg("up", 150, 400, _ts(60), _ts(90)),
        _leg("down", 400, 250, _ts(90), _ts(120)),
        _leg("up", 250, 500, _ts(120), _ts(180)),
    ]
    res = classify_session(legs)
    assert res["shape"] == "/"
    assert res["swings"] == "U"
    # the single macro up-swing must end at the final HOD
    assert res["pivots"][-1][1] == 500


def test_n_day_from_user_chart_example():
    # The user's 2026-07 chart: 5 legs but an N shape, because the pivots at
    # 50970 (never a running high vs 51030) and 50830 (never a running low
    # vs 50760) are absorbed into one macro up-swing.
    legs = [
        _leg("up", 50850, 51030, _ts(0), _ts(30)),
        _leg("down", 51030, 50760, _ts(30), _ts(90)),
        _leg("up", 50760, 50970, _ts(90), _ts(120)),
        _leg("down", 50970, 50830, _ts(120), _ts(150)),
        _leg("up", 50830, 51120, _ts(150), _ts(240)),
    ]
    res = classify_session(legs)
    assert res["shape"] == "N"
    assert res["swings"] == "UDU"
    assert [p[1] for p in res["pivots"]] == [51030, 50760, 51120]
    # dropped leading pivot = first leg start
    assert res["path_start"][1] == 50850


def test_w_day():
    # a true W needs the middle top to be a new running high (above the
    # leading pivot) and the second valley a new running low
    legs = [
        _leg("down", 0, -200, _ts(0), _ts(30)),
        _leg("up", -200, 50, _ts(30), _ts(60)),
        _leg("down", 50, -300, _ts(60), _ts(120)),
        _leg("up", -300, 100, _ts(120), _ts(210)),
    ]
    res = classify_session(legs)
    assert res["shape"] == "W"
    assert res["swings"] == "DUDU"


def test_contracting_double_bottom_is_v_not_w():
    # if the mid rally never exceeds the running high and the second valley
    # holds above the first low, both get absorbed -> V by HOD/LOD logic
    legs = [
        _leg("down", 0, -200, _ts(0), _ts(30)),
        _leg("up", -200, -50, _ts(30), _ts(60)),
        _leg("down", -50, -180, _ts(60), _ts(120)),
        _leg("up", -180, 100, _ts(120), _ts(210)),
    ]
    res = classify_session(legs)
    assert res["shape"] == "V"
    assert res["swings"] == "DU"


def test_shape_name_extensions():
    assert shape_name("UDUDU") == "M+1"
    assert shape_name("DUDUD") == "W+1"
    assert shape_name("UDUDUDU") == "M+3"
    assert shape_name("DUDUDU") == "W+2"


def test_macro_pivots_merge_same_side():
    # successive higher highs with shallow dips merge into one H pivot
    legs = [
        _leg("up", 0, 200, _ts(0), _ts(30)),
        _leg("down", 200, 60, _ts(30), _ts(60)),
        _leg("up", 60, 350, _ts(60), _ts(120)),
    ]
    macro = macro_pivots(legs)
    # leading L(0), then a single merged H at 350
    assert [(p[1], p[2]) for p in macro] == [(0, "L"), (350, "H")]


# ---- acceptance tests against the built DB ----

def test_db_shapes_cover_all_sessions(conn):
    n_sessions = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    for threshold in (40, 120, 200):
        n = conn.execute(
            "SELECT COUNT(*) FROM session_shapes WHERE threshold=?", (threshold,)
        ).fetchone()[0]
        assert n == n_sessions


def test_db_swing_counts_match_n_swings(conn):
    mismatched = conn.execute(
        """
        SELECT COUNT(*) FROM session_shapes s
        WHERE s.n_swings != (
            SELECT COUNT(*) FROM shape_swings w
            WHERE w.instrument = s.instrument AND w.date = s.date
              AND w.threshold = s.threshold
        )
        """
    ).fetchone()[0]
    assert mismatched == 0


def test_db_swings_alternate_and_match_shape(conn):
    rows = conn.execute(
        "SELECT date, swings FROM session_shapes "
        "WHERE threshold=? AND n_swings > 0 ORDER BY date DESC LIMIT 300",
        (THRESHOLD,),
    ).fetchall()
    assert rows
    for date, swings in rows:
        # macro swings must strictly alternate
        assert all(a != b for a, b in zip(swings, swings[1:])), (date, swings)
        db_dirs = [
            r[0] for r in conn.execute(
                "SELECT direction FROM shape_swings "
                "WHERE date=? AND threshold=? ORDER BY swing_index",
                (date, THRESHOLD),
            )
        ]
        assert "".join(db_dirs) == swings


def test_db_last_pivot_is_final_session_extreme(conn):
    rows = conn.execute(
        """
        SELECT s.date, s.swings, w.end_price, sess.rth_high, sess.rth_low
        FROM session_shapes s
        JOIN sessions sess ON sess.instrument = s.instrument AND sess.date = s.date
        JOIN shape_swings w ON w.instrument = s.instrument AND w.date = s.date
             AND w.threshold = s.threshold AND w.swing_index = s.n_swings - 1
        WHERE s.threshold = ? AND s.n_swings > 0
        ORDER BY s.date DESC LIMIT 300
        """,
        (THRESHOLD,),
    ).fetchall()
    assert rows
    for date, swings, end_price, hi, lo in rows:
        target = hi if swings[-1] == "U" else lo
        assert abs(end_price - target) < 0.51, (date, swings, end_price, hi, lo)


def test_db_sessions_shape_columns_match_session_shapes(conn):
    for threshold in (40, 120, 200):
        mismatched = conn.execute(
            f"""
            SELECT COUNT(*) FROM sessions s
            WHERE COALESCE(s.shape_{threshold}, '') != COALESCE((
                SELECT shape FROM session_shapes ss
                WHERE ss.instrument = s.instrument AND ss.date = s.date
                  AND ss.threshold = ?), '')
            """,
            (threshold,),
        ).fetchone()[0]
        assert mismatched == 0


def test_db_user_example_date_is_n(conn):
    row = conn.execute(
        "SELECT shape, n_legs FROM session_shapes WHERE date='2026-07-01' AND threshold=120"
    ).fetchone()
    # tuple(): another test file may have set row_factory=sqlite3.Row on the
    # shared session-scoped conn, and sqlite3.Row never equals a plain tuple
    assert tuple(row) == ("N", 4)
