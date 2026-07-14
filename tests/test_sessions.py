"""Acceptance tests for the `sessions` table (Phase 2 spec §8).

These run against the real built DB (data/db/lookup.sqlite), not synthetic
fixtures — the properties under test are about derived-data invariants across
the whole 17-year history, not algorithm unit correctness.
"""


def test_rth_range_close_le_rth_range(conn):
    """§3.4 acceptance test: close-based range can never exceed the extreme-based one."""
    row = conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE rth_range_close > rth_range"
    ).fetchone()
    assert row[0] == 0


def test_seq_dense_and_gapless_per_instrument(conn):
    """§3.1 / §8: seq is a dense per-instrument row number, no gaps, no duplicates."""
    rows = conn.execute(
        "SELECT instrument, COUNT(*), MIN(seq), MAX(seq), COUNT(DISTINCT seq) "
        "FROM sessions GROUP BY instrument"
    ).fetchall()
    assert rows, "no instruments found in sessions table"
    for instrument, count, min_seq, max_seq, distinct_seq in rows:
        assert min_seq == 1, f"{instrument}: seq should start at 1, got {min_seq}"
        assert max_seq == count, f"{instrument}: seq should end at row count {count}, got {max_seq}"
        assert distinct_seq == count, f"{instrument}: seq has duplicates or gaps"


def test_bs_sb_values_are_valid(conn):
    rows = conn.execute("SELECT DISTINCT bs_sb FROM sessions").fetchall()
    values = {r[0] for r in rows}
    assert values <= {"BS", "SB", "TIE"}


def test_no_legacy_hl_lh_columns(conn):
    """Regression guard for the hl_lh -> bs_sb rename: the old names must never reappear."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    assert "hl_lh" not in cols
    assert "prev_hl_lh" not in cols
    assert "bs_sb" in cols
    assert "prev_bs_sb" in cols


def test_close_based_columns_present(conn):
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    expected = {
        "rth_high_close", "rth_low_close", "rth_range_close", "bs_sb_close",
        "rth_high_close_ts", "rth_low_close_ts",
        "rth_high_close_minute", "rth_low_close_minute",
        "rth_high_close_bucket", "rth_low_close_bucket",
    }
    assert expected <= cols


def test_legs_page_columns_present(conn):
    """Gyration Legs page columns (2026-07-15): bar-seq plumbing + the 5
    timing-comparison derived columns."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()}
    expected = {
        "rth_high_bar_seq", "rth_low_bar_seq",
        "hl_time_diff", "hl_time_vs_prev",
        "h_time_prev_h_time", "l_time_prev_l_time", "ht_vs_lt",
    }
    assert expected <= cols


def test_hl_time_diff_matches_abs_minute_gap(conn):
    row = conn.execute(
        "SELECT COUNT(*) FROM sessions "
        "WHERE hl_time_diff IS NOT NULL "
        "AND hl_time_diff != ABS(rth_high_minute - rth_low_minute)"
    ).fetchone()
    assert row[0] == 0


def test_hl_time_vs_prev_and_ht_vs_lt_are_ternary(conn):
    for col in ("hl_time_vs_prev", "ht_vs_lt"):
        rows = conn.execute(f"SELECT DISTINCT {col} FROM sessions").fetchall()
        values = {r[0] for r in rows}
        assert values <= {1, -1, None}, f"{col} has an unexpected value: {values}"


def test_hl_time_vs_prev_and_ht_vs_lt_null_on_first_session(conn):
    """No prior session to compare against -> null, not a wrongly-defaulted -1."""
    rows = conn.execute(
        "SELECT instrument, MIN(seq) FROM sessions GROUP BY instrument"
    ).fetchall()
    for instrument, min_seq in rows:
        row = conn.execute(
            "SELECT hl_time_vs_prev, ht_vs_lt FROM sessions "
            "WHERE instrument = ? AND seq = ?",
            (instrument, min_seq),
        ).fetchone()
        assert tuple(row) == (None, None), f"{instrument} first session should be (None, None), got {tuple(row)}"


def test_h_time_prev_h_time_matches_bar_seq_diff(conn):
    """h_time_prev_h_time/l_time_prev_l_time are exactly rth_*_bar_seq diffed
    against the previous session (same instrument, adjacent seq) -- cross-check
    a sample of adjacent pairs directly rather than trusting the formula."""
    pairs = conn.execute(
        "SELECT a.instrument, a.rth_high_bar_seq, b.rth_high_bar_seq, a.h_time_prev_h_time, "
        "       a.rth_low_bar_seq, b.rth_low_bar_seq, a.l_time_prev_l_time "
        "FROM sessions a JOIN sessions b "
        "  ON a.instrument = b.instrument AND a.seq = b.seq + 1 "
        "WHERE a.h_time_prev_h_time IS NOT NULL "
        "ORDER BY a.instrument, a.seq LIMIT 500"
    ).fetchall()
    assert pairs, "expected at least one adjacent-session pair"
    for _instr, hi_today, hi_prev, h_diff, lo_today, lo_prev, l_diff in pairs:
        assert h_diff == hi_today - hi_prev
        assert l_diff == lo_today - lo_prev


def test_h_time_prev_h_time_handles_half_day_neighbor(conn):
    """Regression guard: the bar-seq diff must reflect actual RTH bars present,
    not a fixed 390-bars-per-day assumption, when either side of the pair is a
    half day."""
    pairs = conn.execute(
        "SELECT a.instrument, a.rth_high_bar_seq, b.rth_high_bar_seq, a.h_time_prev_h_time "
        "FROM sessions a JOIN sessions b "
        "  ON a.instrument = b.instrument AND a.seq = b.seq + 1 "
        "WHERE (a.is_half_day = 1 OR b.is_half_day = 1) "
        "  AND a.h_time_prev_h_time IS NOT NULL "
        "LIMIT 20"
    ).fetchall()
    assert pairs, "expected at least one half-day-adjacent session pair in 17 years of history"
    for _instr, hi_today, hi_prev, h_diff in pairs:
        assert h_diff == hi_today - hi_prev
        assert h_diff > 0, "bar-seq is strictly increasing across sessions"
