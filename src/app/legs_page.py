"""Gyration Legs — aggregate leg/pivot analysis across many sessions.

Second page of the multipage app (run via src/app/app.py). Shares the same
backend (registry, DB, gyration detector, config) as the Day Session page
(src/app/dashboard.py) via direct imports — see app.py for why function-ref
st.Page (not path-string) is used, so both pages share one cached DB
connection instead of silently duplicating it.

Only scope="rth"/mode="close_to_close" gyrations are used here (the only
combination precomputed in the `gyrations` table at useful scale — see
query/legs.py and config.toml's [gyrations] precompute) — the Mode selector
is hidden (fixed_mode="close_to_close") specifically so this page can't show
RHLW columns/pivots that quietly don't match what's selected.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import COLOR_POS, REGISTRY_BY_NAME
from query.filters import query_sessions
from query.legs import leg_aggregates_by_date, leg_pivots

from dashboard import (
    GYR_N_SIZES,
    _apply_lookback,
    build_display_table,
    get_config,
    get_connection,
    get_instruments,
    inject_shared_css,
    render_filters,
    render_global_controls,
    render_gyration_controls,
    render_session_chart,
)

MAX_CHARTS = 20

# RHLW table columns always cover every stored threshold from 30 up to 200
# (skipping the finer 10/15/20 ones) -- independent of the Size 1/2/3
# selector, which only controls what's plotted on the charts.
RHLW_MIN_THRESHOLD = 30

# Base session-table columns for this page: Day Session's own base columns +
# the 5 new HLtime/HTvsLT metrics (deliberately show_in_table=False in
# registry.py so they never appear on Day Session's default table).
BASE_COLUMNS = [
    "date", "weekday", "bs_sb", "rth_high_time", "rth_low_time",
    "hl_time_diff", "hl_time_vs_prev", "h_time_prev_h_time", "l_time_prev_l_time", "ht_vs_lt",
    "rth_range", "rth_range_ma20", "range_vs_ma20_pts", "gap_pts", "rel_close_pts", "abs_close_pts",
]


def _leg_extra_columns(
    conn, instrument: str, rows: list[dict], thresholds: list[float], confirmed_only: bool
) -> list[dict]:
    """RHLW{threshold}#/Sum/Avg/Avg%/AvgT columns, one block per threshold in
    `thresholds` — always all of them, independent of the Size 1/2/3 selector
    (that only controls what's plotted on the charts). Not FeatureSpec-backed
    (the gyrations table, not sessions) — see build_display_table's
    `extra_columns` param in dashboard.py."""
    dates = [r["date"] for r in rows]
    extra_columns: list[dict] = []
    for threshold in thresholds:
        agg = leg_aggregates_by_date(conn, instrument, threshold, dates, confirmed_only=confirmed_only)
        prefix = f"RHLW{int(threshold)}"
        counts, sums, avgs, avg_pcts, avg_ts = [], [], [], [], []
        for r in rows:
            a = agg.get(r["date"])
            open_ = r.get("rth_open")
            if a is None:
                counts.append(0)
                sums.append(None)
                avgs.append(None)
                avg_pcts.append(None)
                avg_ts.append(None)
            else:
                counts.append(a["count"])
                sums.append(a["sum_pts"])
                avgs.append(a["avg_pts"])
                avg_pcts.append(a["avg_pts"] / open_ * 100 if open_ else None)
                avg_ts.append(a["avg_duration_min"])
        extra_columns += [
            {"label": f"{prefix}#", "values": counts, "decimals": 0},
            {"label": f"{prefix}Sum", "values": sums, "decimals": 0},
            {"label": f"{prefix}Avg", "values": avgs, "decimals": 0},
            {"label": f"{prefix}Avg%", "values": avg_pcts, "decimals": 2},
            {"label": f"{prefix}AvgT", "values": avg_ts, "decimals": 0},
        ]
    return extra_columns


def _color_relative_time_diff(row: pd.Series) -> pd.Series:
    """HtimePrevHtime/LtimePrevLtime are colored relative to each other (via
    the already-computed HTvsLT sign), not independently: green only for
    whichever one is currently the larger of the pair, the other stays
    neutral. Chained onto build_display_table's returned Styler with
    `.apply(..., axis=1)` — pandas accumulates styles per-cell across chained
    calls, so this doesn't disturb any of that Styler's other coloring."""
    styles = pd.Series("", index=row.index)
    flag = row.get("HTvsLT")
    if flag == 1:
        styles["HtimePrevHtime"] = f"color: {COLOR_POS}"
    elif flag == -1:
        styles["LtimePrevLtime"] = f"color: {COLOR_POS}"
    return styles


def _minute_of_day(ts) -> float:
    """Minutes since 09:30 -- the shared x-axis for both time/distance charts."""
    ts = pd.Timestamp(ts)
    return (ts.hour * 60 + ts.minute) - (9 * 60 + 30)


def _minute_to_clock(m: float) -> str:
    total = int(round(m)) + 9 * 60 + 30
    return f"{total // 60:02d}:{total % 60:02d}"


def _style_time_axis(fig: go.Figure, pct: bool) -> None:
    ticks = list(range(0, 391, 30))
    fig.update_xaxes(
        title="Time of day", tickmode="array", tickvals=ticks,
        ticktext=[_minute_to_clock(t) for t in ticks], range=[-5, 395],
    )
    fig.update_yaxes(title="Distance from open (%)" if pct else "Distance from open (pts)")
    fig.update_layout(height=480, margin=dict(t=10, b=10))


def _chart_session_hilo(rows: list[dict], pct: bool) -> go.Figure:
    """Chart A: only each session's own RTH High/Low -- no new query, uses
    the sessions rows already fetched via query_sessions."""
    xs_hi, ys_hi, xs_lo, ys_lo = [], [], [], []
    for r in rows:
        open_ = r.get("rth_open")
        if open_ is None:
            continue
        if r.get("rth_high_time") is not None and r.get("rth_high") is not None:
            dist = (r["rth_high"] - open_) / open_ * 100 if pct else (r["rth_high"] - open_)
            xs_hi.append(_minute_of_day(r["rth_high_time"]))
            ys_hi.append(dist)
        if r.get("rth_low_time") is not None and r.get("rth_low") is not None:
            dist = (r["rth_low"] - open_) / open_ * 100 if pct else (r["rth_low"] - open_)
            xs_lo.append(_minute_of_day(r["rth_low_time"]))
            ys_lo.append(dist)

    fig = go.Figure()
    fig.add_scatter(
        x=xs_hi, y=ys_hi, mode="markers", name="Session High",
        marker=dict(color="#8BC98F", size=5, opacity=0.55),
    )
    fig.add_scatter(
        x=xs_lo, y=ys_lo, mode="markers", name="Session Low",
        marker=dict(color="#E38B8B", size=5, opacity=0.55),
    )
    _style_time_axis(fig, pct)
    return fig


def _chart_leg_pivots(
    conn, instrument: str, rows: list[dict], sizes: list[dict], show_flags: list[bool],
    confirmed_only: bool, pct: bool,
) -> go.Figure:
    """Chart B: every leg's start+end pivot for the enabled AND shown sizes
    -- by construction this includes each session's own high/low too, since
    the largest legs' pivots are the session extremes."""
    dates = [r["date"] for r in rows]
    open_by_date = {r["date"]: r.get("rth_open") for r in rows}

    fig = go.Figure()
    for size, show in zip(sizes, show_flags):
        if not size["enabled"] or not show:
            continue
        threshold = size["threshold"]
        pivots = leg_pivots(conn, instrument, threshold, dates, confirmed_only=confirmed_only)
        xs, ys = [], []
        for leg in pivots:
            open_ = open_by_date.get(leg["start_date"])
            if open_ is None:
                continue
            for ts, price in ((leg["start_ts"], leg["start_price"]), (leg["end_ts"], leg["end_price"])):
                dist = (price - open_) / open_ * 100 if pct else (price - open_)
                xs.append(_minute_of_day(ts))
                ys.append(dist)
        fig.add_scatter(
            x=xs, y=ys, mode="markers", name=f"T={threshold:.0f}",
            marker=dict(color=size["color"], size=5, opacity=0.5),
        )
    _style_time_axis(fig, pct)
    return fig


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="DOW Session Lookup Engine", layout="wide")
    inject_shared_css()
    conn = get_connection()

    instruments = get_instruments(conn)
    controls = render_global_controls(conn, instruments)
    instrument = controls["instrument"]
    basis = controls["basis"]
    date_from, date_to = controls["date_from"], controls["date_to"]
    lookback_mode, lookback_n = controls["lookback_mode"], controls["lookback_n"]
    display_offset = controls["display_offset"]

    st.markdown("---")
    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">Gyration Legs</h2>',
        unsafe_allow_html=True,
    )

    filters, lookahead_active = render_filters(
        conn, instrument, groups=["context", "rth_filters", "legs_filters"]
    )

    gyr_config = get_config()["gyrations"]
    gyr_settings = render_gyration_controls(gyr_config, fixed_mode="close_to_close")

    show_col1, show_col2, show_col3 = st.columns(GYR_N_SIZES)
    show_on_chart = []
    for i, col in enumerate((show_col1, show_col2, show_col3)):
        with col:
            show_on_chart.append(
                st.checkbox(f"Show Size {i + 1} on charts", value=True, key=f"legs_chart_show_{i}")
            )

    pct = st.toggle("Show distance-from-open in percent (instead of points)", value=False)

    st.markdown("<div style='margin-top: 2rem'></div>", unsafe_allow_html=True)
    st.markdown("---")
    st.markdown("<div style='margin-bottom: 0.5rem'></div>", unsafe_allow_html=True)

    if lookahead_active:
        st.warning(
            "An outcome-timing filter is active at offset D or later — this is legitimate "
            "research on outcomes, but using it to *predict* outcomes is lookahead bias.",
            icon="⚠️",
        )

    rows = query_sessions(
        conn, filters=filters, instrument=instrument,
        date_range=(str(date_from), str(date_to)), display_offset=display_offset,
    )
    rows = _apply_lookback(rows, lookback_mode, lookback_n)
    st.caption(f"{len(rows)} matching sessions")

    if not rows:
        st.info("No sessions match the current filters.")
        return

    df = pd.DataFrame(rows)
    rhlw_thresholds = [t for t in gyr_config["thresholds"] if t >= RHLW_MIN_THRESHOLD]
    extra_columns = _leg_extra_columns(conn, instrument, rows, rhlw_thresholds, gyr_settings["confirmed_only"])
    specs = [REGISTRY_BY_NAME[name] for name in BASE_COLUMNS]
    styled, column_config = build_display_table(rows, specs=specs, extra_columns=extra_columns)
    styled = styled.apply(_color_relative_time_diff, axis=1)

    event = st.dataframe(
        styled, use_container_width=True, hide_index=True, column_config=column_config,
        on_select="rerun", selection_mode="multi-row", key="legs_sessions_table",
    )

    st.markdown("<div style='margin-top: 1.5rem'></div>", unsafe_allow_html=True)
    st.markdown(
        '<h3 style="font-size:1.05rem; font-weight:700;">Session High/Low vs time of day</h3>',
        unsafe_allow_html=True,
    )
    st.plotly_chart(_chart_session_hilo(rows, pct), use_container_width=True)

    st.markdown(
        '<h3 style="font-size:1.05rem; font-weight:700;">Leg pivots vs time of day</h3>',
        unsafe_allow_html=True,
    )
    st.plotly_chart(
        _chart_leg_pivots(
            conn, instrument, rows, gyr_settings["sizes"], show_on_chart,
            gyr_settings["confirmed_only"], pct,
        ),
        use_container_width=True,
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if not selected_rows:
        return

    if len(selected_rows) > MAX_CHARTS:
        st.warning(f"{len(selected_rows)} rows selected — showing charts for the first {MAX_CHARTS}.")
        selected_rows = selected_rows[:MAX_CHARTS]

    for idx in selected_rows:
        render_session_chart(conn, instrument, df.iloc[idx], basis, gyr_settings)
        st.divider()


if __name__ == "__main__":
    main()
