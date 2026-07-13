"""Panel A — Filter & Browse (SPEC.md section 10, Phase 2 spec §6).

Registry-driven filters (grouped by timing then basis, each with its own
offset selector) + registry-driven results table + click-to-chart candlestick.
Run with: .venv/Scripts/streamlit.exe run src/app/dashboard.py
"""

from __future__ import annotations

import sqlite3
import sys
import tomllib
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import (
    COLOR_NEG,
    COLOR_POS,
    COLOR_ZERO,
    REGISTRY_BY_NAME,
    filterable_features,
    table_features,
)
from query.filters import query_sessions
from gyrations.detect import detect_legs_close_to_close, detect_legs_extreme_to_extreme

ROOT = Path(__file__).resolve().parents[2]

MAX_CHARTS = 20
OFFSET_LABELS = ["D-2", "D-1", "D", "D+1"]
OFFSET_VALUES = {"D-2": -2, "D-1": -1, "D": 0, "D+1": 1}
TIMING_LABELS = {
    "pre_open": "Conditioning — known at the open",
    "outcome": "Outcome — known only at the close",
}
GYR_DEFAULT_COLORS = ["#7EC8E3", "#F2A65A", "#8BC98F"]  # blue / orange / green, one per size slot
GYR_N_SIZES = 3

# One dashboard tab per config-declared window (Phase 2 spec §3.3), plus the
# whole-RTH "Day Session" tab (window=None). Order here is the tab order.
WINDOW_TABS = [
    (None, "Day Session"),
    ("first_30m", "9:30 - 10:00 Session"),
    ("first_60m", "9:30 - 10:30 Session"),
    ("hour_10_11", "10:00 - 11:00 Session"),
    ("first_90m", "9:30 - 11:00 Session"),
]


def _col(window: str | None, concept: str) -> str:
    """Map a display concept ("open", "gap_pts", ...) to its actual
    sessions-table column name for the given tab. None = whole-RTH columns
    (rth_open, bs_sb, ...); a window key = that window's own win_<name>_*
    columns."""
    if window is None:
        return {
            "open": "rth_open", "high": "rth_high", "low": "rth_low", "close": "rth_close",
            "high_time": "rth_high_time", "low_time": "rth_low_time",
            "range": "rth_range", "bs_sb": "bs_sb",
            "gap_pts": "gap_pts", "rel_close_pts": "rel_close_pts", "abs_close_pts": "abs_close_pts",
        }[concept]
    return f"win_{window}_{concept}"


def _pts_color(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    if v > 0:
        return f"color: {COLOR_POS}"
    if v < 0:
        return f"color: {COLOR_NEG}"
    return f"color: {COLOR_ZERO}"


def _enum_color(v, color_map: dict) -> str:
    color = color_map.get(v)
    return f"color: {color}" if color else ""


@st.cache_resource
def get_config() -> dict:
    return tomllib.loads((ROOT / "config.toml").read_text())


@st.cache_resource
def get_connection() -> sqlite3.Connection:
    config = get_config()
    db_path = ROOT / config["data"]["db_path"]
    return sqlite3.connect(str(db_path), check_same_thread=False)


@st.cache_data
def get_instruments(_conn: sqlite3.Connection) -> list[str]:
    rows = _conn.execute("SELECT DISTINCT instrument FROM sessions ORDER BY instrument").fetchall()
    return [r[0] for r in rows]


@st.cache_data
def get_date_bounds(_conn: sqlite3.Connection, instrument: str) -> tuple[str, str]:
    row = _conn.execute(
        "SELECT MIN(date), MAX(date) FROM sessions WHERE instrument = ?", (instrument,)
    ).fetchone()
    return row[0], row[1]


@st.cache_data
def cached_column_range(_conn: sqlite3.Connection, column: str, instrument: str):
    row = _conn.execute(
        f'SELECT MIN("{column}"), MAX("{column}") FROM sessions WHERE instrument = ?', (instrument,)
    ).fetchone()
    return row[0], row[1]


@st.cache_data
def cached_distinct_values(_conn: sqlite3.Connection, column: str, instrument: str) -> list:
    rows = _conn.execute(
        f'SELECT DISTINCT "{column}" FROM sessions WHERE instrument = ? AND "{column}" IS NOT NULL '
        f'ORDER BY "{column}"',
        (instrument,),
    ).fetchall()
    return [r[0] for r in rows]


def read_minutes(
    conn: sqlite3.Connection, instrument: str, date: str, basis: str,
    time_range: tuple[str, str] | None = None,
) -> pd.DataFrame:
    if time_range is not None:
        # Window tabs: bars are always an RTH subset, further restricted to the
        # window's own clock range. `ts` is TEXT 'YYYY-MM-DD HH:MM:SS'; substr
        # at position 12 for 5 chars pulls "HH:MM", which compares correctly
        # lexicographically against another zero-padded "HH:MM".
        sql = (
            "SELECT ts, open, high, low, close FROM minutes "
            "WHERE instrument = ? AND date = ? AND session = 'RTH' "
            "AND substr(ts, 12, 5) BETWEEN ? AND ? ORDER BY ts"
        )
        params = (instrument, date, time_range[0], time_range[1])
    elif basis == "RTH":
        sql = (
            "SELECT ts, open, high, low, close FROM minutes "
            "WHERE instrument = ? AND date = ? AND session = 'RTH' ORDER BY ts"
        )
        params = (instrument, date)
    else:
        sql = (
            "SELECT ts, open, high, low, close FROM minutes "
            "WHERE instrument = ? AND date = ? ORDER BY ts"
        )
        params = (instrument, date)
    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close"])
    df["ts"] = pd.to_datetime(df["ts"])
    return df


def _render_one_filter(conn: sqlite3.Connection, instrument: str, spec, key_suffix: str = "") -> tuple:
    """Render an offset selector + value widget for one filterable feature.

    `key_suffix` disambiguates widget keys across tabs — most feature names are
    already tab-unique (window-scoped columns are prefixed `win_<name>_`), but
    `shared_across_tabs` specs (weekday, is_half_day) use the *same* `spec.name`
    in every tab, so without this their widgets would collide. This also means
    each tab gets its own independent weekday/half-day selection, consistent
    with every other per-tab control.

    Returns (value, offset). value is None if the filter is left unconstrained
    (or has a degenerate/empty domain, in which case nothing is rendered at all).
    """
    if spec.filter_kind == "range":
        lo, hi = cached_column_range(conn, spec.name, instrument)
        if lo is None or hi is None or lo == hi:
            return None, 0
        lo, hi = float(lo), float(hi)
    elif spec.filter_kind == "select":
        values = cached_distinct_values(conn, spec.name, instrument)
        if not values:
            return None, 0
        if spec.value_order:
            order = spec.value_order
            values = sorted(values, key=lambda v: order.index(v) if v in order else len(order))
    else:
        values = None

    key_base = f"{spec.name}{key_suffix}"
    col_val, col_off = st.columns([3, 1])

    with col_val:
        if spec.filter_kind == "range":
            val = st.slider(spec.label, lo, hi, (lo, hi), key=f"filt_{key_base}")
            value = val if val != (lo, hi) else None
        elif spec.filter_kind == "select":
            sel = st.multiselect(spec.label, values, default=[], key=f"filt_{key_base}")
            value = sel if sel else None
        elif spec.filter_kind == "bool":
            choice = st.selectbox(spec.label, ["Any", "Yes", "No"], key=f"filt_{key_base}")
            value = (choice == "Yes") if choice != "Any" else None
        else:
            value = None

    with col_off:
        # label_visibility="hidden" (not "collapsed") reserves the same label
        # height as the value widget's visible label above, so the two inputs
        # line up on the same row instead of the offset control floating higher.
        offset_label = st.selectbox(
            "offset", OFFSET_LABELS, index=2, key=f"off_{key_base}", label_visibility="hidden"
        )
    offset = OFFSET_VALUES[offset_label]

    return value, offset


def render_filters(conn: sqlite3.Connection, instrument: str, window: str | None = None) -> tuple[dict, bool]:
    """Registry-driven filters, grouped by timing (pre_open/outcome) then basis.

    Renders into whatever container is currently active (sidebar or a tab body)
    rather than hardcoding `st.sidebar` — window tabs render their own filters
    inline in the tab, not the (shared, global-controls-only) sidebar.

    Returns (filters, lookahead_active) where filters is keyed by
    (feature_name, offset) and lookahead_active is True if any outcome-timing
    filter is active at offset >= 0 (Phase 2 spec §6.2).
    """
    key_suffix = f"_{window or 'day'}"
    by_timing: dict[str, dict[str, list]] = {"pre_open": {}, "outcome": {}}
    for spec in filterable_features(window=window):
        if spec.name == "instrument":
            continue
        by_timing[spec.timing].setdefault(spec.basis, []).append(spec)

    filters: dict = {}
    lookahead_active = False

    for timing in ["pre_open", "outcome"]:
        groups = by_timing[timing]
        if not any(groups.values()):
            continue
        header = TIMING_LABELS[timing]
        if timing == "outcome":
            header += "  ⚠️"
        st.markdown(f"**{header}**")

        if window is None:
            # Day Session tab: keep the RTH/ETH/context 3-way basis split.
            for basis_name in ["context", "RTH", "ETH"]:
                specs = groups.get(basis_name, [])
                if not specs:
                    continue
                with st.expander(basis_name, expanded=False):
                    for spec in specs:
                        value, offset = _render_one_filter(conn, instrument, spec, key_suffix)
                        if value is not None:
                            filters[(spec.name, offset)] = value
                            if spec.timing == "outcome" and offset >= 0:
                                lookahead_active = True
        else:
            # Window tabs: no RTH/ETH concept — one flat group per timing bucket.
            all_specs = [s for specs in groups.values() for s in specs]
            with st.expander("Filters", expanded=False):
                for spec in all_specs:
                    value, offset = _render_one_filter(conn, instrument, spec, key_suffix)
                    if value is not None:
                        filters[(spec.name, offset)] = value
                        if spec.timing == "outcome" and offset >= 0:
                            lookahead_active = True

    return filters, lookahead_active


def _apply_lookback(rows: list[dict], mode: str, n) -> list[dict]:
    """Phase 2 spec §4.5: applied after filtering, before statistics/display."""
    if mode == "All" or not rows:
        return rows
    if mode == "Last N occurrences" and n:
        return rows[-int(n):]
    if mode == "Trailing months" and n:
        max_date = max(r["date"] for r in rows)
        cutoff = (pd.Timestamp(max_date) - pd.DateOffset(months=int(n))).strftime("%Y-%m-%d")
        return [r for r in rows if r["date"] >= cutoff]
    return rows


def _legs_for_bars(
    bars: pd.DataFrame, mode: str, threshold: float, confirmed_only: bool, tiebreak: str
) -> list:
    """Compute legs LIVE for the currently-displayed session's bars (Phase 2
    spec §6.3: "this is a verification tool... changing the threshold must
    redraw immediately"). Deliberately not read from the precomputed
    `gyrations` table — that table only has close_to_close/rth+eth
    precomputed, and re-running the detector over ~390-1400 bars is fast
    enough to do live regardless of mode/threshold.
    """
    if mode == "close_to_close":
        legs = detect_legs_close_to_close(bars["close"].tolist(), threshold)
    else:
        ohlc = list(zip(bars["open"], bars["high"], bars["low"], bars["close"]))
        legs = detect_legs_extreme_to_extreme(ohlc, threshold, tiebreak=tiebreak)

    if confirmed_only:
        legs = [leg for leg in legs if leg.confirmed]
    return legs


def _add_gyration_overlay(fig: go.Figure, bars: pd.DataFrame, gyr_settings: dict) -> None:
    """Draws one overlay layer per enabled size slot (up to GYR_N_SIZES), each
    with its own threshold/color/retracement-zone toggle, sharing the global
    mode/confirmed-only settings. Confirmed vs unconfirmed is distinguished by
    line dash (solid/dashed) rather than color, since color is now user-chosen
    per size and reusing a fixed grey for "unconfirmed" across arbitrary
    per-size colors would be confusing.
    """
    ts_list = bars["ts"].tolist()

    for size in gyr_settings["sizes"]:
        if not size["enabled"]:
            continue
        legs = _legs_for_bars(
            bars, gyr_settings["mode"], size["threshold"], gyr_settings["confirmed_only"],
            gyr_settings["tiebreak"],
        )
        color = size["color"]

        for leg in legs:
            dash = "solid" if leg.confirmed else "dash"
            x = [ts_list[leg.start_index], ts_list[leg.end_index]]
            y = [leg.start_price, leg.end_price]

            fig.add_scatter(
                x=x, y=y, mode="lines+markers",
                line=dict(color=color, width=1.5, dash=dash),
                marker=dict(size=5, color=color),
                hovertemplate=f"T={size['threshold']:.0f}<br>" + "%{y:.1f}<br>%{x|%H:%M}<extra></extra>",
                showlegend=False,
            )
            fig.add_annotation(
                x=x[0] + (x[1] - x[0]) / 2, y=(y[0] + y[1]) / 2,
                text=f"{leg.magnitude_pts:.0f}", showarrow=False,
                font=dict(size=13, color=color),
                yshift=16 if leg.direction == "up" else -16,
            )

            if size["show_retracement"] and leg.deepest_retr_pts:
                progress = leg.deepest_retr_progress or 0.0
                if leg.direction == "up":
                    run_price = leg.start_price + progress
                    trough_price = run_price - leg.deepest_retr_pts
                else:
                    run_price = leg.start_price - progress
                    trough_price = run_price + leg.deepest_retr_pts
                fig.add_shape(
                    type="rect",
                    x0=ts_list[leg.deepest_retr_start_index], x1=ts_list[leg.deepest_retr_end_index],
                    y0=min(run_price, trough_price), y1=max(run_price, trough_price),
                    fillcolor=color, opacity=0.15, line_width=0,
                )


def _add_rth_open_line(fig: go.Figure, row: pd.Series) -> None:
    """Grey dotted horizontal reference line at the RTH 09:30 open, spanning
    the full chart width, with the price labelled above the line on the
    right — independent of gyrations/basis, always shown when known.
    """
    open_price = row.get("rth_open")
    if open_price is None or pd.isna(open_price):
        return
    fig.add_hline(
        y=open_price, line_dash="dot", line_color="grey", line_width=1,
        annotation_text=f"{open_price:.1f}", annotation_position="top right",
        annotation_font=dict(color="grey", size=11),
    )


def render_session_chart(
    conn: sqlite3.Connection, instrument: str, row: pd.Series, basis: str, gyr_settings: dict,
    window: str | None = None,
) -> None:
    date = row["date"]
    st.markdown(f"**{instrument} — {date} ({row['weekday']})**")

    def _fmt(v) -> str:
        return f"{v:.1f}" if pd.notna(v) else "—"

    bs_sb_spec = REGISTRY_BY_NAME["bs_sb"]
    gap = row.get(_col(window, "gap_pts"))
    rel = row.get(_col(window, "rel_close_pts"))
    rng = row.get(_col(window, "range"))
    bs_sb = row.get(_col(window, "bs_sb"))
    st.markdown(
        f'<div style="display:flex; gap:20px; font-size:0.78rem; line-height:1.6; '
        f'margin:0 0 24px 0; padding-bottom:2px; align-items:baseline;">'
        f'<div><span style="color:#888;">Gap </span>'
        f'<b style="font-size:0.85rem; {_pts_color(gap)}">{_fmt(gap)}</b></div>'
        f'<div><span style="color:#888;">Rel Close </span>'
        f'<b style="font-size:0.85rem; {_pts_color(rel)}">{_fmt(rel)}</b></div>'
        f'<div><span style="color:#888;">Range </span>'
        f'<b style="font-size:0.85rem;">{_fmt(rng)}</b></div>'
        f'<div><span style="color:#888;">BS/SB </span>'
        f'<b style="font-size:0.85rem; {_enum_color(bs_sb, bs_sb_spec.color_map)}">{bs_sb or "—"}</b></div>'
        f'</div>',
        unsafe_allow_html=True,
    )

    if window is None:
        bars = read_minutes(conn, instrument, date, basis)
    else:
        start, end = get_config()["windows"][window]
        bars = read_minutes(conn, instrument, date, "RTH", time_range=(start, end))
    if bars.empty:
        st.warning("No minute bars found for this session/window.")
        return

    fig = go.Figure(
        data=[
            go.Candlestick(
                x=bars["ts"], open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"],
                name=instrument,
            )
        ]
    )

    _add_rth_open_line(fig, row)

    show_hod_lod = (basis == "RTH") if window is None else True
    if show_hod_lod:
        high_time, low_time = row.get(_col(window, "high_time")), row.get(_col(window, "low_time"))
        if high_time is not None and pd.notna(high_time):
            fig.add_scatter(
                x=[pd.to_datetime(high_time)], y=[row.get(_col(window, "high"))],
                mode="markers+text", text=["HOD"], textposition="top center",
                marker=dict(color="green", size=11, symbol="triangle-down"), name="HOD",
            )
        if low_time is not None and pd.notna(low_time):
            fig.add_scatter(
                x=[pd.to_datetime(low_time)], y=[row.get(_col(window, "low"))],
                mode="markers+text", text=["LOD"], textposition="bottom center",
                marker=dict(color="red", size=11, symbol="triangle-up"), name="LOD",
            )
        if window is None and gyr_settings["show_close_hilo"]:
            # Legs run on closes (spec §2.3/§3.4); these sit at candle bodies,
            # not wicks, unlike the extreme-based HOD/LOD above — expected,
            # not a bug. Shown together so the two can be compared directly.
            fig.add_scatter(
                x=[pd.to_datetime(row["rth_high_close_ts"])], y=[row["rth_high_close"]],
                mode="markers+text", text=["HOD (close)"], textposition="top center",
                marker=dict(color="green", size=9, symbol="triangle-down-open"), name="HOD (close)",
            )
            fig.add_scatter(
                x=[pd.to_datetime(row["rth_low_close_ts"])], y=[row["rth_low_close"]],
                mode="markers+text", text=["LOD (close)"], textposition="bottom center",
                marker=dict(color="red", size=9, symbol="triangle-up-open"), name="LOD (close)",
            )

    if gyr_settings["show"]:
        _add_gyration_overlay(fig, bars, gyr_settings)

    fig.update_yaxes(tickformat=".0f")
    fig.update_layout(
        xaxis_rangeslider_visible=False, height=600, showlegend=False,
        margin=dict(t=10, b=10),
    )
    st.plotly_chart(fig, use_container_width=True)

    with st.expander("Full session row"):
        st.json({k: (round(v, 3) if isinstance(v, float) else v) for k, v in row.items()})


OHLC_HALFDAY_COLUMNS = ["is_half_day", "rth_open", "rth_high", "rth_low", "rth_close"]


def _hidden_column_names(window: str | None) -> set[str]:
    if window is None:
        return set(OHLC_HALFDAY_COLUMNS)
    return {"is_half_day", _col(window, "open"), _col(window, "high"), _col(window, "low"), _col(window, "close")}


def build_display_table(
    rows: list[dict], hidden_names: set[str] | None = None, window: str | None = None
) -> tuple:
    """Registry-driven results table for a given tab (Phase 2 spec §3.6: show_in_table drives the grid)."""
    hidden_names = hidden_names or set()
    specs = [f for f in table_features(window=window) if f.name not in hidden_names]
    display = pd.DataFrame(index=range(len(rows)))
    column_config: dict = {}
    decimal_cols: list[tuple[str, int]] = []
    pts_cols: list[str] = []
    enum_cols: dict[str, dict] = {}

    for spec in specs:
        col_label = spec.display_label
        raw = [r.get(spec.name) for r in rows]
        values = [spec.formatter(v) if spec.formatter else v for v in raw]
        display[col_label] = values

        if spec.decimals is not None:
            decimal_cols.append((col_label, spec.decimals))
        if spec.color_kind == "pts":
            pts_cols.append(col_label)
        elif spec.color_kind == "enum" and spec.color_map:
            enum_cols[col_label] = spec.color_map

        if spec.name == "weekday":
            column_config[col_label] = st.column_config.TextColumn(col_label, alignment="left", width="small")
        elif spec.dtype == "time" or spec.color_kind == "enum":
            column_config[col_label] = st.column_config.TextColumn(col_label, alignment="right", width="small")

    styled = display.style
    for label, decimals in decimal_cols:
        styled = styled.format(f"{{:.{decimals}f}}", subset=[label], na_rep="—")
    for label in pts_cols:
        styled = styled.map(_pts_color, subset=[label])
    for label, color_map in enum_cols.items():
        styled = styled.map(lambda v, cm=color_map: _enum_color(v, cm), subset=[label])

    return styled, column_config


def _render_tab(
    conn: sqlite3.Connection, instrument: str, basis: str, date_from, date_to,
    lookback_mode: str, lookback_n, display_offset: int, hide_ohlc_cols: bool,
    window: str | None, title: str,
) -> None:
    """One full tab body: gyration-overlay controls (independent per tab) ->
    filters -> results table -> row selection -> candlestick chart(s)."""
    tab_key = window or "day"

    st.markdown(
        f'<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">{title}</h2>',
        unsafe_allow_html=True,
    )

    st.markdown(
        '<h3 style="font-size:1.1rem; font-weight:700; margin:0.5rem 0 0.4rem 0;">Gyration overlay</h3>',
        unsafe_allow_html=True,
    )
    gyr_config = get_config()["gyrations"]
    thresholds = gyr_config["thresholds"]
    default_threshold = 50 if 50 in thresholds else thresholds[len(thresholds) // 2]

    show_gyrations = st.checkbox("Show gyrations", value=False, key=f"gyr_show_{tab_key}")

    # Close-based HOD/LOD twins were only built for the whole-RTH session, not
    # per window (see registry.py's window bundle — no rth_*_close analog).
    show_close_hilo = False
    if window is None:
        mode_col, confirmed_col, closehilo_col = st.columns(3)
    else:
        mode_col, confirmed_col = st.columns(2)
    with mode_col:
        mode_label_col, mode_widget_col = st.columns([1, 3], vertical_alignment="center")
        with mode_label_col:
            st.markdown("Mode")
        with mode_widget_col:
            gyr_mode = st.selectbox(
                "Mode", ["close_to_close", "extreme_to_extreme"], index=0, key=f"gyr_mode_{tab_key}",
                label_visibility="collapsed",
            )
    with confirmed_col:
        confirmed_only = st.checkbox("Confirmed legs only", value=True, key=f"gyr_confonly_{tab_key}")
    if window is None:
        with closehilo_col:
            show_close_hilo = st.checkbox("Show close-based HOD/LOD", value=False, key=f"gyr_closehilo_{tab_key}")

    sizes = []
    size_cols = st.columns(GYR_N_SIZES)
    for i in range(GYR_N_SIZES):
        with size_cols[i]:
            with st.expander(f"Size {i + 1}", expanded=(i == 0)):
                enabled = st.checkbox("Enabled", value=(i == 0), key=f"gyr_size_{i}_enabled_{tab_key}")
                threshold = st.select_slider(
                    "Threshold", options=thresholds, value=default_threshold,
                    key=f"gyr_size_{i}_threshold_{tab_key}",
                )
                color_box_col, color_label_col = st.columns([1, 8], vertical_alignment="center")
                with color_box_col:
                    color = st.color_picker(
                        "Color", value=GYR_DEFAULT_COLORS[i], key=f"gyr_size_{i}_color_{tab_key}",
                        label_visibility="collapsed",
                    )
                with color_label_col:
                    st.markdown("Color")
                show_retracement = st.checkbox(
                    "Show retracement zone", value=False, key=f"gyr_size_{i}_retracement_{tab_key}"
                )
                sizes.append({
                    "enabled": enabled, "threshold": threshold, "color": color,
                    "show_retracement": show_retracement,
                })

    gyr_settings = {
        "show": show_gyrations,
        "mode": gyr_mode,
        "confirmed_only": confirmed_only,
        "show_close_hilo": show_close_hilo,
        "tiebreak": gyr_config["intrabar_tiebreak"],
        "sizes": sizes,
    }

    st.markdown("---")
    filters, lookahead_active = render_filters(conn, instrument, window=window)

    if lookahead_active:
        st.warning(
            "An outcome-timing filter is active at offset D or later — this is legitimate "
            "research on outcomes, but using it to *predict* outcomes is lookahead bias.",
            icon="⚠️",
        )

    rows = query_sessions(
        conn,
        filters=filters,
        instrument=instrument,
        date_range=(str(date_from), str(date_to)),
        display_offset=display_offset,
    )
    rows = _apply_lookback(rows, lookback_mode, lookback_n)
    st.caption(f"{len(rows)} matching sessions")

    if not rows:
        st.info("No sessions match the current filters.")
        return

    df = pd.DataFrame(rows)
    hidden_names = _hidden_column_names(window) if hide_ohlc_cols else set()
    styled, column_config = build_display_table(rows, hidden_names, window=window)

    event = st.dataframe(
        styled, use_container_width=True, hide_index=True, column_config=column_config,
        on_select="rerun", selection_mode="multi-row", key=f"sessions_table_{tab_key}",
    )

    selected_rows = event.selection.rows if event and event.selection else []
    if not selected_rows:
        st.info("Click a row above to see its candlestick chart (select multiple rows for several charts).")
        return

    if len(selected_rows) > MAX_CHARTS:
        st.warning(f"{len(selected_rows)} rows selected — showing charts for the first {MAX_CHARTS}.")
        selected_rows = selected_rows[:MAX_CHARTS]

    for idx in selected_rows:
        render_session_chart(conn, instrument, df.iloc[idx], basis, gyr_settings, window=window)
        st.divider()


def main() -> None:
    st.set_page_config(page_title="DOW Session Lookup Engine", layout="wide")
    st.markdown(
        '<style>div[data-testid="stVerticalBlock"] { gap: 0.4rem; }\n'
        'div[data-testid="stColorPicker"] button, div[data-testid="stColorPickerBlock"] {\n'
        '    width: 18px !important; height: 18px !important; min-width: 18px !important;\n'
        '    border-radius: 4px !important;\n'
        '}\n'
        '.block-container {\n'
        '    padding-top: 4rem !important;\n'
        '}</style>',
        unsafe_allow_html=True,
    )
    conn = get_connection()

    st.header("Global controls")
    instruments = get_instruments(conn)

    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        instrument = st.selectbox("Instrument", instruments)
    with row1_col2:
        basis = st.radio("Chart basis", ["RTH", "ETH"], horizontal=True)

    min_date, max_date = get_date_bounds(conn, instrument)

    row2_col1, row2_col2, row2_col3, row2_col4 = st.columns(4)
    with row2_col1:
        date_range = st.date_input(
            "Date range",
            value=(pd.to_datetime(min_date), pd.to_datetime(max_date)),
            min_value=pd.to_datetime(min_date),
            max_value=pd.to_datetime(max_date),
        )
        if isinstance(date_range, tuple) and len(date_range) == 2:
            date_from, date_to = date_range
        else:
            date_from, date_to = pd.to_datetime(min_date), pd.to_datetime(max_date)
    with row2_col2:
        lookback_mode = st.selectbox("Lookback", ["All", "Last N occurrences", "Trailing months"])
        lookback_n = None
        if lookback_mode == "Last N occurrences":
            lookback_n = st.number_input("N", min_value=1, value=4, step=1)
        elif lookback_mode == "Trailing months":
            lookback_n = st.number_input("Months", min_value=1, value=12, step=1)
    with row2_col3:
        display_offset_label = st.selectbox("Display offset", OFFSET_LABELS, index=2)
        display_offset = OFFSET_VALUES[display_offset_label]
    with row2_col4:
        hide_ohlc_cols = st.checkbox("Hide HalfDay /O/H/L/C columns", value=True)

    st.markdown("---")

    tabs = st.tabs([title for _, title in WINDOW_TABS])
    for (window, title), tab in zip(WINDOW_TABS, tabs):
        with tab:
            _render_tab(
                conn, instrument, basis, date_from, date_to, lookback_mode, lookback_n,
                display_offset, hide_ohlc_cols, window, title,
            )


if __name__ == "__main__":
    main()
