"""Gyrational Stats v1.0 -- projected 123 retracement zones and F231 zones.

Detection runs on DAX 5-minute ETH bars: fast legs (default 80pt) are the
intraday swings, slow legs (default 160pt) supply the trend filter. A setup is
three consecutive fast legs X-A-B-C whose first leg agrees with the slow
trend; it becomes DRAWABLE the moment swing2 has travelled the fast threshold
back from A, which is the earliest bar at which A is confirmed as a pivot.

What is drawn per setup, all anchored at that drawable moment:
  - a green horizontal line at the projected bottom of swing2 (the entry zone)
  - a red X at the 2-sigma stop
  - an amber X at the F231 stop, but only from the bar at which swing3's
    failure is actually knowable

Statistics come from the last 3 years; the chart shows a movable window inside
the last 2. Rendering is windowed on purpose -- 2 years of 24h 5-minute bars
is ~150k OHLC bars, which no browser will pan smoothly, so the window is
capped and moved with the date control rather than streamed all at once.
"""

from __future__ import annotations

import sys
from datetime import datetime, time, timedelta
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gyrations.gyr_stats import (
    find_setups, summarise_123, measure_f231, summarise_f231, slow_direction,
    rolling_vol5,
)
from gyrations.detect import detect_legs_extreme_to_extreme
from query.gyr_stats import load_eth_bars, load_session_ranges

from dashboard import get_connection, inject_shared_css

STATS_YEARS = 3
CHART_YEARS = 2
BAR_COLOUR = "#4C8DD9"
ENTRY_COLOUR = "#2FBF71"
STOP_COLOUR = "#E5484D"
F231_COLOUR = "#F5A524"
SLOW_COLOUR = "#9B8AFB"

WINDOW_CHOICES = {"3 days": 3, "1 week": 7, "2 weeks": 14, "1 month": 30}


@st.cache_data(show_spinner="Detecting legs and gathering zone statistics…")
def _compute(_conn, instrument: str, t_fast: float, t_slow: float) -> dict:
    df = load_eth_bars(_conn, instrument, STATS_YEARS)
    ranges = load_session_ranges(_conn, instrument, STATS_YEARS)
    vol5 = rolling_vol5(ranges)

    ts_list = df["ts"].to_list()
    o = df["open"].to_numpy(); h = df["high"].to_numpy()
    lo = df["low"].to_numpy(); c = df["close"].to_numpy()
    bars = list(zip(o, h, lo, c))

    setups = find_setups(bars, ts_list, t_fast, t_slow, vol5)
    st123 = summarise_123(setups)
    dirs = slow_direction(bars, t_slow)
    f_rows = measure_f231(bars, setups, st123, vol5, dirs) if st123 else []
    stf = summarise_f231(f_rows)

    slow_legs = [
        {"s": L.start_index, "e": L.end_index, "sp": L.start_price, "ep": L.end_price}
        for L in detect_legs_extreme_to_extreme(bars, t_slow) if L.confirmed
    ]

    return {
        "ts": ts_list, "o": o, "h": h, "l": lo, "c": c,
        "vol5": vol5,
        "setups": [s.__dict__ for s in setups],
        "st123": st123, "stf": stf,
        "slow_legs": slow_legs,
    }


def _fmt(v, nd=2, suffix=""):
    return "—" if v is None else f"{v:.{nd}f}{suffix}"


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">'
        'Gyrational Stats v1.0</h2>', unsafe_allow_html=True,
    )
    st.caption(
        "123 retracement zones and Figure-2.31 continuation zones on DAX 5-minute ETH bars. "
        "Statistics from the last 3 years, pooled (no time-of-day conditioning in v1.0). "
        "Depths are normalised by the mean range of the 5 prior RTH sessions."
    )

    c1, c2, c3, c4 = st.columns([1, 1, 1, 2])
    with c1:
        t_fast = st.selectbox("Fast leg (intraday)", [40, 60, 80, 100, 120], index=2)
    with c2:
        t_slow = st.selectbox("Slow leg (trend filter)", [120, 140, 160, 180, 200], index=2)
    with c3:
        win_label = st.selectbox("Window", list(WINDOW_CHOICES), index=1)

    data = _compute(conn, "DAX", float(t_fast), float(t_slow))
    st123, stf = data["st123"], data["stf"]
    if not st123:
        st.warning("Not enough confirmed 123 patterns at this combination.")
        return

    ts = data["ts"]
    last_day = ts[-1].date()
    first_day = (ts[-1] - timedelta(days=365 * CHART_YEARS)).date()
    with c4:
        win_end = st.date_input(
            "Window ends", value=last_day, min_value=first_day, max_value=last_day,
        )

    # ---- statistics panel -------------------------------------------------
    m = st.columns(6)
    m[0].metric("Setups (3y)", f"{st123['n_all_setups']:,}")
    m[1].metric("Became 123", f"{st123['success_rate']:.0f}%")
    m[2].metric("Entry depth", f"{st123['entry_norm']:.2f}×vol5")
    m[3].metric("Stop (2σ)", f"{st123['stop_norm']:.2f}×vol5")
    m[4].metric("Time stop (2σ)", f"{st123['time_stop_bars']:.0f} bars")
    m[5].metric("Median depth", f"{st123['median_pts']:.0f} pts")

    if stf:
        f = st.columns(6)
        f[0].metric("F231 pool", f"{stf['n']:,}")
        f[1].metric("Broke out later", f"{stf['break_rate']:.0f}%")
        f[2].metric("F231 stop (2σ)", f"{stf['stop_norm']:.2f}×vol5")
        f[3].metric("F231 time (2σ)", f"{stf['time_stop_bars']:.0f} bars")
        f[4].metric("Median extra depth", f"{stf['median_depth_norm']:.2f}×vol5")
        f[5].metric("Median bars to break", f"{stf['median_bars']:.0f}")

    o1, o2, o3 = st.columns([1, 1, 3])
    show_123 = o1.checkbox("123 zones", value=True)
    show_f231 = o2.checkbox("F231 zones", value=True)
    show_slow = o3.checkbox(f"{t_slow:.0f}pt trend legs", value=True)

    # ---- window slice -----------------------------------------------------
    win_days = WINDOW_CHOICES[win_label]
    end_dt = min(ts[-1], datetime.combine(win_end, time(23, 59, 59)))
    start_dt = end_dt - timedelta(days=win_days)
    idx = [i for i, t in enumerate(ts) if start_dt <= t <= end_dt]
    if not idx:
        st.warning("No bars in that window.")
        return
    i0, i1 = idx[0], idx[-1]
    n_win = i1 - i0 + 1

    x = np.arange(n_win)
    fig = go.Figure()
    fig.add_trace(go.Ohlc(
        x=x,
        open=data["o"][i0:i1 + 1], high=data["h"][i0:i1 + 1],
        low=data["l"][i0:i1 + 1], close=data["c"][i0:i1 + 1],
        increasing=dict(line=dict(color=BAR_COLOUR, width=1)),
        decreasing=dict(line=dict(color=BAR_COLOUR, width=1)),
        showlegend=False, name="",
        text=[t.strftime("%a %d %b %H:%M") for t in ts[i0:i1 + 1]],
        hoverinfo="text+y",
    ))

    if show_slow:
        sx, sy = [], []
        for L in data["slow_legs"]:
            if L["e"] < i0 or L["s"] > i1:
                continue
            sx += [L["s"] - i0, L["e"] - i0, None]
            sy += [L["sp"], L["ep"], None]
        if sx:
            fig.add_trace(go.Scatter(
                x=sx, y=sy, mode="lines", line=dict(color=SLOW_COLOUR, width=1.4),
                name=f"{t_slow:.0f}pt trend", hoverinfo="skip",
            ))

    vol5 = data["vol5"]
    ex, ey, stop_x, stop_y, stop_txt = [], [], [], [], []
    fx, fy, ftxt = [], [], []

    for s in data["setups"]:
        d = s["direction"]
        v = vol5.get(s["day"])
        if not v:
            continue
        dec = s["decision_index"]

        if show_123 and i0 <= dec <= i1:
            entry = s["a_price"] - d * st123["entry_norm"] * v
            stop = s["a_price"] - d * st123["stop_norm"] * v
            end = min(s["i_c"], i1)
            ex += [dec - i0, end - i0, None]
            ey += [entry, entry, None]
            stop_x.append(dec - i0)
            stop_y.append(stop)
            stop_txt.append(
                f"{'LONG' if d == 1 else 'SHORT'} setup<br>"
                f"entry {entry:,.0f}<br>stop {stop:,.0f}<br>"
                f"vol5 {v:,.0f} pts<br>"
                f"outcome: {'123 confirmed' if s['success'] else 'swing3 failed'}"
            )

        if show_f231 and stf and not s["success"] and s["f231_index"] is not None:
            fi = s["f231_index"]
            if i0 <= fi <= i1:
                flevel = s["b_price"] - d * stf["stop_norm"] * v
                fx.append(fi - i0)
                fy.append(flevel)
                ftxt.append(f"F231 stop {flevel:,.0f}<br>"
                            f"({stf['stop_norm']:.2f}×vol5 beyond B)")

    if ex:
        fig.add_trace(go.Scatter(
            x=ex, y=ey, mode="lines", line=dict(color=ENTRY_COLOUR, width=2),
            name="Projected entry", hoverinfo="skip",
        ))
    if stop_x:
        fig.add_trace(go.Scatter(
            x=stop_x, y=stop_y, mode="markers", name="123 stop (2σ)",
            marker=dict(symbol="x", size=9, color=STOP_COLOUR),
            text=stop_txt, hoverinfo="text",
        ))
    if fx:
        fig.add_trace(go.Scatter(
            x=fx, y=fy, mode="markers", name="F231 stop (2σ)",
            marker=dict(symbol="x", size=9, color=F231_COLOUR),
            text=ftxt, hoverinfo="text",
        ))

    step = max(1, n_win // 14)
    tickvals = list(range(0, n_win, step))
    fig.update_layout(
        height=820, margin=dict(l=10, r=10, t=30, b=10),
        xaxis=dict(
            rangeslider=dict(visible=False),
            tickmode="array", tickvals=tickvals,
            ticktext=[ts[i0 + i].strftime("%d %b %H:%M") for i in tickvals],
            tickangle=-45,
        ),
        yaxis=dict(side="right"),
        dragmode="pan", hovermode="closest",
        legend=dict(orientation="h", y=1.02, x=0),
    )
    st.plotly_chart(fig, use_container_width=True,
                    config={"scrollZoom": True, "displaylogo": False})
    st.caption(
        f"{n_win:,} bars shown ({start_dt:%d %b %Y} → {end_dt:%d %b %Y}). "
        "Drag to pan, scroll to zoom; move the window with the date control. "
        "Green line = projected swing2 bottom, red X = 2σ stop, amber X = F231 stop "
        "(drawn only once swing3's failure is knowable)."
    )

    with st.expander("How to read these numbers — and what they don't tell you"):
        st.markdown(f"""
**Depth percentiles of confirmed 123 retracements** (× vol5):
{"  ".join(f"`p{q}={v:.2f}`" for q, v in st123['pct'].items())}

**Known bias.** The zone is measured on confirmed 123s only, exactly as the
method specifies. Those are the cases where swing3 succeeded, and setups that
retraced deeper are disproportionately the ones that failed — so the 2σ stop
will be breached noticeably more often in live use than 2σ suggests. The F231
statistics cover part of that excluded population.

**Truncation.** A setup is only detectable once swing2 has travelled
{t_fast:.0f}pt, so every measured depth is ≥ {t_fast:.0f}pt. On quiet days the
projected entry lands above that confirmation threshold and is therefore already
passed the moment it can be drawn — that is a property of the construction, not
a bug.

**Time stops are heavily skewed.** Swing2 duration has mean
{st123['mean_bars']:.0f} bars against sd {st123['sd_bars']:.0f}, so mean+2σ
({st123['time_stop_bars']:.0f} bars) sits far above the typical case. Treat it
as an outer bound, not an expectation.
""")


if __name__ == "__main__":
    main()
