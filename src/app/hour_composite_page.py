"""Hourly Composite v1.0 -- one session-hour, every day, stitched gap-free.

Pick an instrument and a "bar" slot (bar1 = the session's first RTH hour,
bar2 = the second, ...). Every day contributes exactly one real hourly
candle for that slot; those candles are then chained with NO gap between
them -- day N's synthetic open is set to day N-1's synthetic close, so only
that one hour's own price action accumulates and the other 23 hours of each
day (and every overnight gap) are invisible. The very first candle opens at
0. This isolates what a specific hour of the day has been doing, day after
day, as if you only ever traded that single window and let the position
ride.

x-axis is a plain sequential index (not real calendar time -- the whole
point is that there are no gaps), with the real date carried in hover text
and a sparse tick label every ~20 candles for orientation.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from indicators.ma import ema, dema
from query.hour_bars import available_instruments, bar_slots, load_bar_series

from dashboard import get_connection, inject_shared_css

INITIAL_VIEW = 150  # candles shown by default before the user pans/zooms


@st.cache_data(show_spinner=False)
def _load(_conn, instrument: str, bar_num: int) -> list[dict]:
    return load_bar_series(_conn, instrument, bar_num)


def _build_composite(rows: list[dict]) -> dict:
    n = len(rows)
    o = np.empty(n)
    h = np.empty(n)
    l = np.empty(n)
    c = np.empty(n)
    base = 0.0
    for i, r in enumerate(rows):
        body_h = r["high"] - r["open"]
        body_l = r["low"] - r["open"]
        body_c = r["close"] - r["open"]
        o[i] = base
        h[i] = base + body_h
        l[i] = base + body_l
        c[i] = base + body_c
        base = c[i]
    return dict(o=o, h=h, l=l, c=c, dates=[r["date"] for r in rows],
               weekdays=[r["weekday"] for r in rows])


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">'
        'Hourly Composite v1.0</h2>', unsafe_allow_html=True,
    )
    st.caption(
        "One session-hour, every trading day, chained with no gap between "
        "candles -- isolates how that specific hour behaves over time."
    )

    instruments = available_instruments(conn)
    if not instruments:
        st.error("No `session_hour_bars` table -- run `python run_hour_bars.py` first.")
        return

    c1, c2 = st.columns([1, 2])
    with c1:
        instrument = st.selectbox("Instrument", instruments, key="hc_instrument")
    with c2:
        slots = bar_slots(instrument)
        labels = [f"bar{k} ({window})" for k, window in slots]
        sel = st.selectbox("Session hour", labels, key="hc_bar")
        bar_num = slots[labels.index(sel)][0]

    rows = _load(conn, instrument, bar_num)
    if not rows:
        st.info("No data for this selection.")
        return

    comp = _build_composite(rows)
    n = len(comp["c"])
    d5 = dema(comp["c"], 10)
    e10 = ema(comp["c"], 10)

    st.markdown(
        f'<div style="font-size:0.9rem; margin-bottom:0.6rem;">'
        f'{n:,} candles &nbsp;|&nbsp; {comp["dates"][0]} &rarr; {comp["dates"][-1]} '
        f'&nbsp;|&nbsp; net drift over the whole series: '
        f'<b>{comp["c"][-1]:+,.1f} pts</b></div>',
        unsafe_allow_html=True,
    )

    x = list(range(n))
    step = max(n // 20, 1)
    tick_idx = list(range(0, n, step))
    tick_text = [comp["dates"][i] for i in tick_idx]

    fig = go.Figure()
    fig.add_trace(go.Candlestick(
        x=x, open=comp["o"], high=comp["h"], low=comp["l"], close=comp["c"],
        name="composite",
        customdata=list(zip(comp["dates"], comp["weekdays"])),
        hovertemplate="%{customdata[0]} (%{customdata[1]})<br>"
                      "O %{open:.1f} H %{high:.1f} L %{low:.1f} C %{close:.1f}<extra></extra>",
    ))
    fig.add_scatter(x=x, y=d5, mode="lines", name="DEMA10",
                    line=dict(color="#F2C464", width=1.5))
    fig.add_scatter(x=x, y=e10, mode="lines", name="EMA10",
                    line=dict(color="#9AC7E8", width=1.5))

    fig.update_xaxes(tickmode="array", tickvals=tick_idx, ticktext=tick_text,
                     tickangle=-45, range=[max(0, n - INITIAL_VIEW), n - 1])
    fig.update_yaxes(tickformat=".0f")
    fig.update_layout(
        xaxis_rangeslider_visible=False, height=560,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
        margin=dict(t=30, b=10), dragmode="pan",
    )
    st.plotly_chart(fig, use_container_width=True,
                    config={"scrollZoom": True})
    st.caption("Drag to pan, scroll to zoom -- the x-axis is candle sequence, not real time.")


if __name__ == "__main__":
    main()
