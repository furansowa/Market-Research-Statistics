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
