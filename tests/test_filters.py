"""Acceptance tests for offset-based session lookup (Phase 2 spec §4.4, §8)."""

from query.filters import query_sessions, distinct_values

# (base column, legacy prev_* convenience column that aliases it at offset -1)
PREV_COLUMN_PAIRS = [
    ("rel_close_dir", "prev_rel_close_dir"),
    ("abs_close_dir", "prev_abs_close_dir"),
    ("bs_sb", "prev_bs_sb"),
]


def test_offset_minus1_matches_legacy_prev_column(conn):
    """§8: filtering at offset -1 on X == filtering at offset 0 on legacy prev_X."""
    for base_col, prev_col in PREV_COLUMN_PAIRS:
        values = distinct_values(conn, base_col)
        assert values, f"no distinct values found for {base_col}"
        value = values[0]

        via_offset = query_sessions(conn, filters={(base_col, -1): [value]}, instrument="US30")
        via_legacy = query_sessions(conn, filters={(prev_col, 0): [value]}, instrument="US30")

        dates_offset = {r["date"] for r in via_offset}
        dates_legacy = {r["date"] for r in via_legacy}

        assert len(dates_offset) > 0, f"{base_col}@-1 matched zero sessions for value={value}"
        assert dates_offset == dates_legacy, (
            f"{base_col}@offset-1 (n={len(dates_offset)}) != "
            f"{prev_col}@offset0 (n={len(dates_legacy)}) for value={value!r}"
        )


def test_display_offset_returns_the_preceding_session(conn):
    """display_offset=-1 must return, for each anchor, the session immediately before it by seq."""
    all_rows = query_sessions(conn, instrument="US30", order_by="date")
    all_dates = [r["date"] for r in all_rows]
    date_index = {d: i for i, d in enumerate(all_dates)}

    anchor_rows = query_sessions(
        conn, instrument="US30", date_range=("2020-01-01", "2020-02-01"), order_by="date"
    )
    shifted_rows = query_sessions(
        conn, instrument="US30", date_range=("2020-01-01", "2020-02-01"),
        display_offset=-1, order_by="date",
    )

    # anchors whose predecessor exists in history (drops the very first session, if present)
    eligible_anchors = [r for r in anchor_rows if date_index[r["date"]] > 0]
    assert len(eligible_anchors) == len(shifted_rows)

    for anchor, shifted in zip(eligible_anchors, shifted_rows):
        expected_prev_date = all_dates[date_index[anchor["date"]] - 1]
        assert shifted["date"] == expected_prev_date


def test_no_join_for_same_session_query(conn):
    """The common case (no offset filters, display_offset=0) must not touch other rows."""
    rows = query_sessions(conn, instrument="US30", date_range=("2020-01-01", "2020-01-10"))
    assert len(rows) > 0
    for r in rows:
        assert "2020-01-01" <= r["date"] <= "2020-01-10"
