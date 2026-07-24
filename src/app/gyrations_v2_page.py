"""Gyrations v2.0 — extreme-to-extreme leg analysis at 3 fixed sizes.

Third page of the multipage app (run via src/app/app.py). Shares the same
backend (registry, DB, gyration detector, config) as Day Session/Gyration
Legs via direct imports — see app.py for why function-ref st.Page (not
path-string) is used, so all three pages share one cached DB connection
instead of silently duplicating it.

Unlike the Gyration Legs page (close_to_close, user-selectable thresholds),
this page is fixed to exactly 3 sizes — 40/120/200 points — using
extreme_to_extreme detection (highs/lows, not just closes). Both are now
precomputed in the `gyrations` table (see query/legs.py's module docstring
and config.toml's [gyrations] precompute).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import COLOR_BS, REGISTRY_BY_NAME
from query.filters import query_sessions
from query.legs import leg_aggregates_by_date, leg_pair_aggregates_by_date, leg_detail_rows

from dashboard import (
    GYR_DEFAULT_COLORS,
    _apply_lookback,
    build_display_table,
    get_config,
    get_connection,
    get_instruments,
    inject_shared_css,
    read_minutes,
    render_filters,
    render_global_controls,
    render_session_chart,
)

MAX_CHARTS = 20
FIXED_THRESHOLDS = (40, 120, 200)

# Per-leg filters (Leg Detail Filters section): (field, label, decimals) for
# range sliders, (field, label) for multiselects. "start_time_min"/
# "end_time_min" aren't real leg_detail_rows() fields -- derived on the fly
# (see _enrich_leg_for_filtering) as minutes-since-09:30, matching how the
# rest of the app filters time-of-day (rth_high_minute etc.), not raw
# timestamps.
LEG_SELECT_FIELDS = [("direction", "Direction"), ("pattern", "Pattern")]
LEG_RANGE_FIELDS = [
    ("start_time_min", "Start time (min since 09:30)", 0),
    ("end_time_min", "End time (min since 09:30)", 0),
    ("start_price_rel_open", "Start price rel. open (pts)", 1),
    ("end_price_rel_open", "End price rel. open (pts)", 1),
    ("duration_min", "Duration (min)", 0),
    ("size_pts", "Size (pts)", 0),
    ("size_pct", "Size (%)", 2),
    ("time_ratio", "Time ratio", 2),
    ("size_ratio", "Size ratio", 2),
    ("gyration_size_pts", "Gyration size (pts)", 0),
]
# Leg-count filters (registry.py) and Leg Detail Filters (this file, live
# computed) are both scoped to confirmed legs unconditionally -- matching the
# 9 leg-count columns, which are baked confirmed=True at ETL time regardless
# of the "Confirmed legs only" checkbox (that checkbox only affects the
# gyration overlay/RHLW/Gyra columns). Kept fixed here too, rather than
# threading gyr_settings["confirmed_only"] through, since that control isn't
# rendered until after this section (see main()).
LEG_DETAIL_CONFIRMED_ONLY = True

# Base session-table columns for this page, in the requested order: Day
# Session's own base columns, then the 5 HLtime/HTvsLT metrics (already
# show_in_table=False in registry.py), then the 9 leg-count-in-window columns
# (between HTvsLT and Range), then RelHigh/RelLow (after RelClose) — all
# v2.0-only, kept off Day Session's table the same way.
BASE_COLUMNS = [
    "date", "weekday", "moon_age_days", "moon_phase", "bs_sb", "shape_40", "shape_120", "shape_200",
    "pivot_pattern_40", "pivot_pattern_120", "pivot_pattern_200",
    "rth_high_time", "rth_low_time",
    "hl_time_diff", "hl_time_vs_prev", "h_time_prev_h_time", "l_time_prev_l_time", "ht_vs_lt",
    "bs_sb_legs_40", "first_legs_40", "last_legs_40",
    "bs_sb_legs_120", "first_legs_120", "last_legs_120",
    "bs_sb_legs_200", "first_legs_200", "last_legs_200",
    "rth_range", "rth_range_ma20", "range_vs_ma20_pts",
    "gap_pts", "rel_close_pts", "rel_high_pts", "rel_low_pts", "abs_close_pts",
]


def render_v2_gyration_controls(gyr_config: dict) -> dict:
    """Same returned shape as dashboard.render_gyration_controls (so
    render_session_chart needs no changes), but: mode is hardcoded to
    extreme_to_extreme (no selectbox at all — this page has no other use for
    close_to_close), and there are exactly 3 fixed-threshold expanders
    (40/120/200, no threshold slider) instead of 3 user-configurable slots."""
    st.markdown(
        '<h3 style="font-size:1.1rem; font-weight:700; margin:0.5rem 0 0.4rem 0;">Gyration overlay</h3>',
        unsafe_allow_html=True,
    )
    show_gyrations = st.checkbox("Show gyrations", value=False)

    mode_col, confirmed_col, closehilo_col = st.columns(3)
    with mode_col:
        st.markdown("Mode: **extreme_to_extreme**")
    with confirmed_col:
        confirmed_only = st.checkbox("Confirmed legs only", value=True)
    with closehilo_col:
        show_close_hilo = st.checkbox("Show close-based HOD/LOD", value=False)

    sizes = []
    size_cols = st.columns(len(FIXED_THRESHOLDS))
    for i, threshold in enumerate(FIXED_THRESHOLDS):
        with size_cols[i]:
            with st.expander(f"Size {i + 1} (T={threshold})", expanded=(i == 0)):
                enabled = st.checkbox("Enabled", value=(i == 0), key=f"v2_size_{i}_enabled")
                color_box_col, color_label_col = st.columns([1, 8], vertical_alignment="center")
                with color_box_col:
                    color = st.color_picker(
                        "Color", value=GYR_DEFAULT_COLORS[i], key=f"v2_size_{i}_color",
                        label_visibility="collapsed",
                    )
                with color_label_col:
                    st.markdown("Color")
                show_retracement = st.checkbox(
                    "Show retracement zone", value=False, key=f"v2_size_{i}_retracement"
                )
                sizes.append({
                    "enabled": enabled, "threshold": threshold, "color": color,
                    "show_retracement": show_retracement,
                })

    return {
        "show": show_gyrations,
        "mode": "extreme_to_extreme",
        "confirmed_only": confirmed_only,
        "show_close_hilo": show_close_hilo,
        "tiebreak": gyr_config["intrabar_tiebreak"],
        "sizes": sizes,
    }


def _leg_extra_columns(conn, instrument: str, rows: list[dict], confirmed_only: bool) -> list[dict]:
    """RHLW{t}#/Sum/Avg/Avg%/AvgT (same computation as Gyration Legs' RHLW
    block but mode="extreme_to_extreme") immediately followed by
    Gyra{t}Avg/Gyra{t}AvgT (pair-of-legs averages), for each of the 3 fixed
    thresholds — always all 3, independent of which sizes are enabled in the
    gyration overlay controls (that only controls the charts)."""
    dates = [r["date"] for r in rows]
    extra_columns: list[dict] = []

    for threshold in FIXED_THRESHOLDS:
        agg = leg_aggregates_by_date(
            conn, instrument, threshold, dates, mode="extreme_to_extreme", confirmed_only=confirmed_only,
        )
        prefix = f"RHLW{threshold}"
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

        pair_agg = leg_pair_aggregates_by_date(
            conn, instrument, threshold, dates, mode="extreme_to_extreme", confirmed_only=confirmed_only,
        )
        gyra_avg, gyra_avg_t = [], []
        for r in rows:
            p = pair_agg.get(r["date"])
            if p is None:
                gyra_avg.append(None)
                gyra_avg_t.append(None)
            else:
                gyra_avg.append(p["avg_pair_pts"])
                gyra_avg_t.append(p["avg_pair_duration_min"])
        extra_columns += [
            {"label": f"Gyra{threshold}Avg", "values": gyra_avg, "decimals": 0},
            {"label": f"Gyra{threshold}AvgT", "values": gyra_avg_t, "decimals": 0},
        ]

    return extra_columns


def _enrich_leg_for_filtering(leg: dict) -> dict:
    """Adds the two derived minute-of-day fields Leg Detail Filters needs —
    leg_detail_rows() itself returns raw start_time/end_time timestamps.
    Uses _minute_of_day, defined further down in this file — fine, since
    this is only ever called at runtime (from main()), by which point the
    whole module is loaded."""
    leg = dict(leg)
    leg["start_time_min"] = _minute_of_day(leg["start_time"])
    leg["end_time_min"] = _minute_of_day(leg["end_time"])
    return leg


@st.cache_data
def _leg_detail_bounds(_conn, instrument: str, threshold: float) -> dict:
    """Min/max per range field and distinct values per select field, from
    this instrument's FULL history at this threshold (not just the currently
    filtered rows) -- same convention as dashboard.py's cached_column_range/
    cached_distinct_values. Cached since this scans every leg at this
    threshold across all history; only needs recomputing once per
    instrument/threshold per session, not on every rerun."""
    all_dates = [r[0] for r in _conn.execute(
        "SELECT date FROM sessions WHERE instrument = ? ORDER BY date", (instrument,)
    ).fetchall()]
    legs = [
        _enrich_leg_for_filtering(leg)
        for leg in leg_detail_rows(_conn, instrument, threshold, all_dates, confirmed_only=LEG_DETAIL_CONFIRMED_ONLY)
    ]

    bounds: dict = {}
    for field, _label, _decimals in LEG_RANGE_FIELDS:
        values = [leg[field] for leg in legs if leg.get(field) is not None]
        bounds[field] = (min(values), max(values)) if values else (0.0, 1.0)
    for field, _label in LEG_SELECT_FIELDS:
        bounds[field] = sorted({leg[field] for leg in legs if leg.get(field) is not None})
    return bounds


def render_leg_detail_filters(conn, instrument: str) -> dict[float, dict]:
    """Filters for the per-leg dataset (leg_detail_rows): direction, pattern,
    duration, size, size%, ratios, gyration size, start/end price-relative-
    to-open, start/end time-of-day — tripled per fixed threshold, same as the
    9 leg-count columns. A session shows up if it has AT LEAST ONE leg (at
    that threshold) satisfying each active filter independently — filters
    don't have to all be satisfied by the same single leg."""
    st.markdown(
        '<h3 style="font-size:1.05rem; font-weight:700; margin:0.5rem 0 0.4rem 0;">Leg Detail Filters</h3>',
        unsafe_allow_html=True,
    )
    leg_filters: dict[float, dict] = {}
    with st.expander("Leg Detail Filters", expanded=False):
        for threshold in FIXED_THRESHOLDS:
            st.markdown(f"**T={threshold}**")
            bounds = _leg_detail_bounds(conn, instrument, threshold)
            field_filters: dict = {}

            widgets = [(f, l, None) for f, l in LEG_SELECT_FIELDS] + [(f, l, d) for f, l, d in LEG_RANGE_FIELDS]
            for i in range(0, len(widgets), 3):
                cols = st.columns(3)
                for col, (field, label, decimals) in zip(cols, widgets[i:i + 3]):
                    with col:
                        if decimals is None:
                            options = bounds.get(field, [])
                            if not options:
                                field_filters[field] = None
                                continue
                            sel = st.multiselect(label, options, default=[], key=f"legfilt_{threshold}_{field}")
                            field_filters[field] = sel if sel else None
                        else:
                            lo, hi = bounds.get(field, (0.0, 1.0))
                            if lo == hi:
                                field_filters[field] = None
                                continue
                            lo, hi = float(lo), float(hi)
                            val = st.slider(label, lo, hi, (lo, hi), key=f"legfilt_{threshold}_{field}")
                            field_filters[field] = val if val != (lo, hi) else None
                st.markdown("<div style='margin-bottom: 0.6rem'></div>", unsafe_allow_html=True)

            leg_filters[threshold] = field_filters
    return leg_filters


def _apply_leg_detail_filters(conn, instrument: str, rows: list[dict], leg_filters: dict[float, dict]) -> list[dict]:
    """Post-filter (Python-side, after query_sessions/_apply_lookback) --
    per-leg attributes can't be persisted as a single sessions column the way
    the 9 leg-count columns are, since the filter bounds are arbitrary/
    interactive, not a fixed precomputed threshold. AND across thresholds and
    across fields; "at least one leg matches" within each field."""
    dates = [r["date"] for r in rows]
    candidate_dates = set(dates)

    for threshold, field_filters in leg_filters.items():
        active = {f: v for f, v in field_filters.items() if v is not None}
        if not active:
            continue
        legs = [
            _enrich_leg_for_filtering(leg)
            for leg in leg_detail_rows(conn, instrument, threshold, dates, confirmed_only=LEG_DETAIL_CONFIRMED_ONLY)
        ]
        for field, value in active.items():
            if isinstance(value, list):
                matching = {leg["date"] for leg in legs if leg.get(field) in value}
            else:
                lo, hi = value
                matching = {leg["date"] for leg in legs if leg.get(field) is not None and lo <= leg[field] <= hi}
            candidate_dates &= matching

    return [r for r in rows if r["date"] in candidate_dates]


def _color_rel_hilo(row: pd.Series) -> pd.Series:
    """RelHigh/RelLow are colored relative to each other: whichever has the
    larger ABSOLUTE value (moved further from open) gets blue-ish
    (COLOR_BS), the other stays neutral. Chained onto build_display_table's
    Styler the same way _color_relative_time_diff already does on the
    Gyration Legs page."""
    styles = pd.Series("", index=row.index)
    rel_high, rel_low = row.get("RelHigh"), row.get("RelLow")
    if rel_high is None or rel_low is None or pd.isna(rel_high) or pd.isna(rel_low):
        return styles
    if abs(rel_high) > abs(rel_low):
        styles["RelHigh"] = f"color: {COLOR_BS}"
    elif abs(rel_low) > abs(rel_high):
        styles["RelLow"] = f"color: {COLOR_BS}"
    return styles


def _minute_of_day(ts) -> float:
    """Minutes since 09:30 -- duplicated from legs_page.py rather than
    hoisted into dashboard.py; a 3-line helper isn't worth coupling this page
    to that one for."""
    ts = pd.Timestamp(ts)
    return (ts.hour * 60 + ts.minute) - (9 * 60 + 30)


def _minute_to_clock(m: float) -> str:
    total = int(round(m)) + 9 * 60 + 30
    return f"{total // 60:02d}:{total % 60:02d}"


def _chart_relative_to_open(conn, instrument: str, selected_rows: list[dict]) -> go.Figure:
    """One line per selected session: that session's own intraday close
    price path, normalized so its own RTH open sits at 0 -- lets several
    days' shapes be compared directly on one chart."""
    fig = go.Figure()
    for r in selected_rows:
        date = r["date"]
        open_ = r.get("rth_open")
        if open_ is None:
            continue
        bars = read_minutes(conn, instrument, date, "RTH")
        if bars.empty:
            continue
        xs = [_minute_of_day(ts) for ts in bars["ts"]]
        ys = (bars["close"] - open_).tolist()
        fig.add_scatter(x=xs, y=ys, mode="lines", name=str(date))

    fig.add_hline(y=0, line_dash="dot", line_color="grey", annotation_text="Open")
    ticks = list(range(0, 391, 30))
    fig.update_xaxes(
        title="Time of day", tickmode="array", tickvals=ticks,
        ticktext=[_minute_to_clock(t) for t in ticks], range=[-5, 395],
    )
    fig.update_yaxes(title="Distance from open (pts)")
    fig.update_layout(height=480, margin=dict(t=10, b=10))
    return fig


# Morning card: character forecast for the next session, using ONLY info
# known at the latest session's close (user constraint 2026-07-21: no
# overnight, no intraday; the gap is the only extra at-open variable and is
# not needed here). Rules and their probabilities were validated
# out-of-sample (2023-26 in-sample, 2020-23 OOS, both confirming z>=3):
# choppiness/character is predictable from the prior day; direction is not.
CARD_THRESHOLD = 120
CARD_WINDOW_SESSIONS = 756  # ~3 trading years, per Ch*** "1y enough, 2-3 to be precise"
CHOP_LEGS, CHOP_SWING = 10, 450
TREND_LEGS, TREND_SWING = 3, 300


def _card_bucket(n_legs: int, last_swing_pts: float | None) -> str:
    if last_swing_pts is not None:
        if n_legs >= CHOP_LEGS and last_swing_pts >= CHOP_SWING:
            return "STRONG-CHOP"
        if 1 <= n_legs <= TREND_LEGS and last_swing_pts < TREND_SWING:
            return "STRONG-TREND"
    return "NEUTRAL"


@st.cache_data
def _morning_card_stats(_conn, instrument: str, latest_date: str) -> dict:
    """Latest session's own stats + trailing-window conditional character
    probabilities for the bucket it falls in. latest_date in the cache key
    invalidates on DB refresh."""
    rows = _conn.execute(
        """SELECT s.date, s.shape, s.n_swings, s.n_legs, s.fade_pts,
                  (SELECT w.size_pts FROM shape_swings w
                   WHERE w.instrument = s.instrument AND w.date = s.date
                     AND w.threshold = s.threshold
                   ORDER BY w.swing_index DESC LIMIT 1) AS last_swing_pts,
                  (SELECT w.direction FROM shape_swings w
                   WHERE w.instrument = s.instrument AND w.date = s.date
                     AND w.threshold = s.threshold
                   ORDER BY w.swing_index DESC LIMIT 1) AS last_swing_dir
           FROM session_shapes s
           WHERE s.instrument = ? AND s.threshold = ? AND s.date <= ?
           ORDER BY s.date DESC LIMIT ?""",
        (instrument, CARD_THRESHOLD, latest_date, CARD_WINDOW_SESSIONS + 1),
    ).fetchall()
    rows = [dict(zip(
        ("date", "shape", "n_swings", "n_legs", "fade_pts", "last_swing_pts", "last_swing_dir"), r
    )) for r in rows]
    rows.reverse()  # oldest -> newest
    latest = rows[-1]

    by_bucket: dict[str, list[int]] = {"STRONG-CHOP": [], "STRONG-TREND": [], "NEUTRAL": []}
    all_swings = []
    for prev, today in zip(rows, rows[1:]):
        by_bucket[_card_bucket(prev["n_legs"], prev["last_swing_pts"])].append(today["n_swings"])
        all_swings.append(today["n_swings"])

    def stats(swing_counts):
        n = len(swing_counts)
        if n == 0:
            return {"n": 0, "p_oneway": None, "p_multi": None, "avg_swings": None}
        oneway = sum(1 for s in swing_counts if s == 1) / n
        return {"n": n, "p_oneway": oneway, "p_multi": 1 - oneway,
                "avg_swings": sum(swing_counts) / n}

    bucket = _card_bucket(latest["n_legs"], latest["last_swing_pts"])
    return {
        "latest": latest,
        "bucket": bucket,
        "bucket_stats": stats(by_bucket[bucket]),
        "base_stats": stats(all_swings),
    }


def render_morning_card(conn, instrument: str) -> None:
    latest_date = conn.execute(
        "SELECT MAX(date) FROM session_shapes WHERE instrument=? AND threshold=?",
        (instrument, CARD_THRESHOLD),
    ).fetchone()[0]
    if latest_date is None:
        return
    card = _morning_card_stats(conn, instrument, latest_date)
    latest, bucket = card["latest"], card["bucket"]
    bs, base = card["bucket_stats"], card["base_stats"]

    color = {"STRONG-CHOP": "#d9534f", "STRONG-TREND": "#5cb85c", "NEUTRAL": "#999999"}[bucket]
    swing_txt = ("-" if latest["last_swing_pts"] is None
                 else f"{latest['last_swing_pts']:.0f} pts {'up' if latest['last_swing_dir'] == 'U' else 'down'}")
    fade_txt = "-" if latest["fade_pts"] is None else f"{latest['fade_pts']:+.0f} pts"
    if bs["n"]:
        forecast = (
            f"P(one-way day): <b>{bs['p_oneway']*100:.0f}%</b> &nbsp;|&nbsp; "
            f"P(2+ macro swings): <b>{bs['p_multi']*100:.0f}%</b> &nbsp;|&nbsp; "
            f"avg macro swings: <b>{bs['avg_swings']:.2f}</b> "
            f"<span style='opacity:0.6'>(n={bs['n']} similar days; "
            f"base {base['p_oneway']*100:.0f}% / {base['p_multi']*100:.0f}% / "
            f"{base['avg_swings']:.2f})</span>"
        )
    else:
        forecast = "not enough similar days in window"
    st.markdown(
        f'<div style="border:1px solid {color}; border-left:6px solid {color}; '
        'border-radius:6px; padding:0.6rem 1rem; margin:0.4rem 0 0.8rem 0; '
        'font-size:0.85rem;">'
        f'<b>Morning card</b> — last session {latest["date"]} (T={CARD_THRESHOLD}): '
        f'shape <b>{latest["shape"]}</b>, {latest["n_legs"]} legs, '
        f'last swing {swing_txt}, fade into close {fade_txt}<br>'
        f'Next session regime: <b style="color:{color}">{bucket}</b> &nbsp;→&nbsp; {forecast}<br>'
        '<span style="opacity:0.6">Character only — direction is NOT predictable from '
        'prior-day info (validated out-of-sample). Rules: STRONG-CHOP = 10+ legs & last swing '
        '≥450; STRONG-TREND = ≤3 legs & last swing <300.</span></div>',
        unsafe_allow_html=True,
    )


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
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">Gyrations v2.0</h2>',
        unsafe_allow_html=True,
    )

    render_morning_card(conn, instrument)

    filters, lookahead_active = render_filters(
        conn, instrument, groups=["context", "rth_filters", "legs_filters_v2"]
    )
    leg_filters = render_leg_detail_filters(conn, instrument)

    gyr_config = get_config()["gyrations"]
    gyr_settings = render_v2_gyration_controls(gyr_config)

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
    rows = _apply_leg_detail_filters(conn, instrument, rows, leg_filters)
    st.caption(f"{len(rows)} matching sessions")

    if not rows:
        st.info("No sessions match the current filters.")
        return

    df = pd.DataFrame(rows)
    extra_columns = _leg_extra_columns(conn, instrument, rows, gyr_settings["confirmed_only"])
    specs = [REGISTRY_BY_NAME[name] for name in BASE_COLUMNS]
    styled, column_config = build_display_table(rows, specs=specs, extra_columns=extra_columns)
    styled = styled.apply(_color_rel_hilo, axis=1)

    event = st.dataframe(
        styled, use_container_width=True, hide_index=True, column_config=column_config,
        on_select="rerun", selection_mode="multi-row", key="gyrations_v2_sessions_table",
    )

    selected_rows = event.selection.rows if event and event.selection else []

    st.markdown("<div style='margin-top: 1.5rem'></div>", unsafe_allow_html=True)
    st.markdown(
        '<h3 style="font-size:1.05rem; font-weight:700;">Selected sessions relative to open</h3>',
        unsafe_allow_html=True,
    )
    if selected_rows:
        selected_for_chart = [rows[idx] for idx in selected_rows[:MAX_CHARTS]]
        st.plotly_chart(_chart_relative_to_open(conn, instrument, selected_for_chart), use_container_width=True)
    else:
        st.caption("Select rows in the table above to plot them here.")

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
