"""OpenNormalisation v1.0 -- continuous, gap-free RTH price chart with every session's
own Open normalized to 0.

Fourth page of the multipage app (run via src/app/app.py). Concatenates the last
DISPLAY_YEARS years of RTH-only 1min bars back-to-back (no overnight/weekend gaps --
each session's bars simply follow the previous session's), so scrolling/zooming
horizontally browses every session continuously. Every bar's O/H/L/C has that
session's own rth_open subtracted, so each session's open sits at 0 regardless of
the instrument's price level at the time.

v1 scope, per explicit user request: chart only, no filters/stats. Optional overlays:
SowaDonchian (5min, period 20 -- src/indicators/sowa_donchian.py, ported from the
sibling Sowa_donchian_app) and the 3 fixed-threshold (40/120/200pt) extreme_to_extreme
gyration legs, reusing the same precomputed `gyrations` table Gyrations v2.0 uses.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicators.sowa_donchian import compute_sowa_donchian

from dashboard import (
    GYR_DEFAULT_COLORS,
    get_connection,
    get_instruments,
    inject_shared_css,
)

DISPLAY_YEARS = 1  # trimmed from 3 -- was heavy to scroll/pan with 3 years of 1min candles
DONCHIAN_PERIOD = 20
DONCHIAN_BAR_MINUTES = 5
GYR_THRESHOLDS = (40, 120, 200)
CHART_TYPES = ["Candlestick", "Bar", "Line"]
DEFAULT_CANDLE_COLORS = {"up": "#8BC98F", "down": "#E38B8B"}
DEFAULT_DONCHIAN_COLORS = {"up_line": "#4CAF50", "dn_line": "#E35D5D"}
# Faint white overlay on every other session -- this app's dark theme (.streamlit/
# config.toml) makes a low-alpha white read as alternating light/dark bands.
SESSION_SHADE_COLOR = "rgba(255,255,255,0.035)"
INITIAL_VISIBLE_SESSIONS = 10  # sessions shown before the user scrolls/zooms out
Y_AXIS_RANGE = (-500, 500)  # fixed vertical window -- pan manually to see excursions beyond it


@st.cache_data(show_spinner=f"Loading last {DISPLAY_YEARS} {'year' if DISPLAY_YEARS == 1 else 'years'} of RTH bars...")
def _load_display_bars(_conn: sqlite3.Connection, instrument: str) -> list[dict]:
    cutoff = _conn.execute(
        "SELECT date(MAX(date), ?) FROM sessions WHERE instrument = ?",
        (f"-{DISPLAY_YEARS} years", instrument),
    ).fetchone()[0]
    rows = _conn.execute(
        "SELECT ts, open, high, low, close, date FROM minutes "
        "WHERE instrument = ? AND session = 'RTH' AND date >= ? ORDER BY ts",
        (instrument, cutoff),
    ).fetchall()
    return [
        {"ts": ts, "open": o, "high": h, "low": l, "close": c, "date": d}
        for ts, o, h, l, c, d in rows
    ]


@st.cache_data
def _load_session_opens(_conn: sqlite3.Connection, instrument: str) -> dict[str, float]:
    rows = _conn.execute(
        "SELECT date, rth_open FROM sessions WHERE instrument = ?", (instrument,)
    ).fetchall()
    return {d: o for d, o in rows if o is not None}


@st.cache_resource(show_spinner="Computing SowaDonchian over full history (one-time)...")
def _load_donchian(instrument: str) -> dict[str, tuple[float, float]]:
    """SowaDonchian(period=20) on 5min bars built from this instrument's FULL RTH
    history, not just the displayed window -- the indicator's adaptive lookback needs
    real history to behave like the reference implementation; only the chart itself
    slices to DISPLAY_YEARS. Computed on RTH-only bars (unlike the sibling app's
    ETH-inclusive version) so its bar sequence exactly matches this page's own
    gap-free RTH sequence -- no timestamp-alignment ambiguity between chart and
    overlay. Cached process-wide: a one-time ~10-15s cost, not worth a DB precompute
    step for a v1 view (see module docstring)."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT ts, high, low, close, date FROM minutes "
        "WHERE instrument = ? AND session = 'RTH' ORDER BY ts", (instrument,)
    ).fetchall()

    bars5: list[list[tuple]] = []
    bucket: list[tuple] = []
    cur_date = None
    for ts, h, l, c, date in rows:
        if date != cur_date:
            if bucket:
                bars5.append(bucket)
            bucket, cur_date = [], date
        bucket.append((ts, h, l, c))
        if len(bucket) == DONCHIAN_BAR_MINUTES:
            bars5.append(bucket)
            bucket = []
    if bucket:
        bars5.append(bucket)

    ts5 = [b[0][0] for b in bars5]
    hi5 = [max(x[1] for x in b) for b in bars5]
    lo5 = [min(x[2] for x in b) for b in bars5]
    cl5 = [b[-1][3] for b in bars5]

    result = compute_sowa_donchian(hi5, lo5, cl5, period=DONCHIAN_PERIOD)
    return {
        ts: (u, d)
        for ts, u, d in zip(ts5, result.up_avg, result.dn_avg)
        if u is not None and d is not None
    }


def render_open_normalization_controls() -> dict:
    st.markdown(
        '<h3 style="font-size:1.1rem; font-weight:700; margin:0.5rem 0 0.4rem 0;">Chart settings</h3>',
        unsafe_allow_html=True,
    )
    c1, c2, c3 = st.columns(3)
    with c1:
        chart_type = st.selectbox("Chart type", CHART_TYPES)
    with c2:
        up_color = st.color_picker("Up color", DEFAULT_CANDLE_COLORS["up"])
    with c3:
        down_color = st.color_picker("Down color", DEFAULT_CANDLE_COLORS["down"])

    d1, d2, d3 = st.columns(3)
    with d1:
        show_donchian = st.checkbox("Show SowaDonchian (5min, period 20)", value=False)
    with d2:
        donchian_up_color = st.color_picker(
            "UpAvg color", DEFAULT_DONCHIAN_COLORS["up_line"], disabled=not show_donchian,
        )
    with d3:
        donchian_dn_color = st.color_picker(
            "DnAvg color", DEFAULT_DONCHIAN_COLORS["dn_line"], disabled=not show_donchian,
        )

    st.markdown("**Gyrations (extreme_to_extreme)**")
    gyr_cols = st.columns(len(GYR_THRESHOLDS))
    gyr_sizes = []
    for i, threshold in enumerate(GYR_THRESHOLDS):
        with gyr_cols[i]:
            enabled = st.checkbox(
                f"T={threshold}", value=False, key=f"opennorm_gyr_{threshold}_enabled"
            )
            color = st.color_picker(
                "Color", GYR_DEFAULT_COLORS[i], key=f"opennorm_gyr_{threshold}_color",
                label_visibility="collapsed", disabled=not enabled,
            )
            gyr_sizes.append({"threshold": threshold, "enabled": enabled, "color": color})

    return {
        "chart_type": chart_type,
        "candle_colors": {"up": up_color, "down": down_color},
        "show_donchian": show_donchian,
        "donchian_colors": {"up_line": donchian_up_color, "dn_line": donchian_dn_color},
        "gyr_sizes": gyr_sizes,
    }


def _add_price_trace(fig: go.Figure, bars: list[dict], x: list[int], chart_type: str, candle_colors: dict) -> None:
    if chart_type == "Line":
        # Scattergl (WebGL), not Scatter -- this can be 50-100k+ points, and only
        # Candlestick/Ohlc lack a WebGL variant in Plotly (they stay SVG regardless).
        fig.add_trace(go.Scattergl(
            x=x, y=[b["close"] for b in bars], mode="lines",
            line=dict(color=candle_colors["up"], width=1.3), name="Close (rel. Open)",
        ))
        return

    kwargs = dict(
        x=x,
        open=[b["open"] for b in bars], high=[b["high"] for b in bars],
        low=[b["low"] for b in bars], close=[b["close"] for b in bars],
        name="Price (rel. Open)",
        increasing_line_color=candle_colors["up"], decreasing_line_color=candle_colors["down"],
    )
    if chart_type == "Bar":
        fig.add_trace(go.Ohlc(**kwargs))
    else:
        fig.add_trace(go.Candlestick(**kwargs))


def _add_gyration_overlay_normalized(
    conn: sqlite3.Connection, fig: go.Figure, instrument: str,
    date_from: str, date_to: str, ts_to_x: dict, opens: dict, size: dict,
) -> None:
    """One trace per confirmed/unconfirmed state (not per leg -- thousands of legs
    over 3 years would mean thousands of add_trace calls otherwise): each leg's
    2-point segment is appended with a None separator, the standard Plotly technique
    for many disjoint line segments in a single trace."""
    threshold, color = size["threshold"], size["color"]
    legs = conn.execute(
        "SELECT start_ts, end_ts, start_price, end_price, confirmed, start_date FROM gyrations "
        "WHERE instrument = ? AND scope = 'rth' AND mode = 'extreme_to_extreme' "
        "AND threshold = ? AND start_date >= ? AND start_date <= ? ORDER BY start_ts",
        (instrument, threshold, date_from, date_to),
    ).fetchall()

    segments: dict[bool, tuple[list, list]] = {True: ([], []), False: ([], [])}
    for start_ts, end_ts, start_price, end_price, confirmed, date in legs:
        x0, x1 = ts_to_x.get(start_ts), ts_to_x.get(end_ts)
        o = opens.get(date)
        if x0 is None or x1 is None or o is None:
            continue
        xs, ys = segments[bool(confirmed)]
        xs += [x0, x1, None]
        ys += [start_price - o, end_price - o, None]

    for confirmed, (xs, ys) in segments.items():
        if not xs:
            continue
        fig.add_trace(go.Scattergl(
            x=xs, y=ys, mode="lines",
            line=dict(color=color, width=1.3, dash="solid" if confirmed else "dash"),
            name=f"T={threshold}" + ("" if confirmed else " (unconfirmed)"),
            showlegend=False, connectgaps=False, hoverinfo="skip",
        ))


def render_open_normalization_chart(conn: sqlite3.Connection, instrument: str, settings: dict) -> None:
    raw_bars = _load_display_bars(conn, instrument)
    if not raw_bars:
        st.warning("No RTH bars found for this instrument.")
        return
    opens = _load_session_opens(conn, instrument)

    bars = []
    for b in raw_bars:
        o = opens.get(b["date"])
        if o is None:
            continue
        bars.append({
            "ts": b["ts"], "date": b["date"],
            "open": b["open"] - o, "high": b["high"] - o,
            "low": b["low"] - o, "close": b["close"] - o,
        })
    if not bars:
        st.warning("No sessions with a known RTH open in range.")
        return

    x = list(range(len(bars)))
    ts_to_x = {b["ts"]: i for i, b in enumerate(bars)}

    fig = go.Figure()

    # Alternating per-session background + one tick per first-seen month.
    starts = [0] + [i for i in range(1, len(bars)) if bars[i]["date"] != bars[i - 1]["date"]]
    tick_x, tick_text, last_month = [], [], None
    for si, start in enumerate(starts):
        end = (starts[si + 1] - 1) if si + 1 < len(starts) else len(bars) - 1
        if si % 2 == 1:
            fig.add_vrect(
                x0=start - 0.5, x1=end + 0.5,
                fillcolor=SESSION_SHADE_COLOR, line_width=0, layer="below",
            )
        month = bars[start]["date"][:7]
        if month != last_month:
            tick_x.append(start)
            tick_text.append(bars[start]["date"])
            last_month = month

    _add_price_trace(fig, bars, x, settings["chart_type"], settings["candle_colors"])
    fig.add_hline(y=0, line_dash="dot", line_color="grey", annotation_text="Open")

    if settings["show_donchian"]:
        donchian = _load_donchian(instrument)
        dxs, ups, dns = [], [], []
        for i, b in enumerate(bars):
            hit = donchian.get(b["ts"])
            if hit is None:
                continue
            o = opens[b["date"]]
            dxs.append(i)
            ups.append(hit[0] - o)
            dns.append(hit[1] - o)
        if dxs:
            colors = settings["donchian_colors"]
            fig.add_trace(go.Scattergl(
                x=dxs, y=ups, mode="lines", line=dict(color=colors["up_line"], width=1.4, shape="hv"),
                name="UpAvg", connectgaps=False,
            ))
            fig.add_trace(go.Scattergl(
                x=dxs, y=dns, mode="lines", line=dict(color=colors["dn_line"], width=1.4, shape="hv"),
                name="DnAvg", connectgaps=False,
            ))

    date_from, date_to = bars[0]["date"], bars[-1]["date"]
    for size in settings["gyr_sizes"]:
        if size["enabled"]:
            _add_gyration_overlay_normalized(conn, fig, instrument, date_from, date_to, ts_to_x, opens, size)

    n_show = min(INITIAL_VISIBLE_SESSIONS, len(starts))
    initial_start = starts[-n_show]
    fig.update_layout(
        height=640,
        margin=dict(l=10, r=10, t=20, b=10),
        dragmode="pan",
        xaxis=dict(
            rangeslider_visible=False, tickmode="array", tickvals=tick_x, ticktext=tick_text,
            range=[initial_start - 2, len(bars) + 2],
        ),
        yaxis=dict(
            title="Distance from session Open (pts)", tickformat=",.0f",
            range=list(Y_AXIS_RANGE), autorange=False,
        ),
        legend=dict(orientation="h", y=1.05),
    )
    st.plotly_chart(fig, use_container_width=True, config={"scrollZoom": True})


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()
    instruments = get_instruments(conn)

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">OpenNormalisation v1.0</h2>',
        unsafe_allow_html=True,
    )
    year_word = "year" if DISPLAY_YEARS == 1 else "years"
    st.caption(
        f"Last {DISPLAY_YEARS} {year_word} of RTH sessions, back-to-back with no overnight/weekend "
        "gaps -- every session's own Open sits at 0. Drag to pan, scroll/pinch to zoom, double-click "
        "to reset."
    )

    instrument = st.selectbox("Instrument", instruments)
    settings = render_open_normalization_controls()
    st.markdown("---")
    render_open_normalization_chart(conn, instrument, settings)


if __name__ == "__main__":
    main()
