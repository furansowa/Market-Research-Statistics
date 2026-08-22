"""Pivots TimeMap 1.0 — which candle of the session turns, and how far it runs.

For every gyration size, every turning point is dropped into the 5-minute
candle it occurred in (09:00 = slot 0, 09:05 = slot 1, … on DAX) and counted
across all sessions. Each slot then reports how often it holds a pivot and
how big the leg leaving that pivot turns out to be.

Built for DAX, which has NO rows in the `gyrations` table — that precompute
covers US30 only. Legs are therefore detected live from `minutes` and cached
per (instrument, basis, timeframe, mode, threshold), the same live-compute
approach the Time Waves page takes; a full DAX history costs ~2.5s per
threshold, so there is nothing to precompute and nothing to keep in sync.

THREE READINGS OF "HOW OFTEN", and only one is a probability
------------------------------------------------------------
- **Rate** = pivots ÷ sessions. Exceeds 100% wherever a slot routinely holds
  more than one pivot per session (common at threshold 40).
- **Hit%** = share of sessions with at least one pivot in the slot. Capped at
  100%, and the number to quote when asking "does this candle turn?".
- **Base%** = the flat rate this slot would show if the session's pivots were
  spread evenly over its candles. Every slot's Hit% has to be read against
  it: with ~4 pivots a session over 102 candles, 4% is not a hot slot, it's
  the average, and the page prints the excess rather than leaving the reader
  to divide in their head.

The default view drops each session's first and last pivot — see
`gyrations.pivot_timemap`'s module docstring for why they are detector
geometry rather than turning points. The toggle exists so the artifact can be
seen; it is not a neutral choice.

Widget keys are prefixed `pt_` — session_state is shared across every page in
this multipage app.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import polars as pl
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import COLOR_POS, COLOR_NEG
from gyrations.pivot_timemap import (
    REACH_PCTS,
    aggregate_to_tf,
    extract_pivots,
    session_spans,
    slot_coverage,
    slot_table,
)
from query.pivot_timemap import (
    available_instruments,
    date_bounds,
    load_session_minutes,
    session_open_mod,
)

from dashboard import get_connection, inject_shared_css

# Sizes present in the `gyrations` precompute, from 40 up — the set this page
# was asked for. The detector runs live so any value would work, but keeping
# to the stored ladder means numbers here are directly comparable with the
# US30 pages built on that table.
THRESHOLDS = [40, 50, 60, 70, 80, 90, 100, 120, 150, 200]
DEFAULT_THRESHOLDS = [40, 80, 120, 200]

SLOT_CHOICES = [5, 10, 15, 30]
TF_CHOICES = [1, 5]

# `minutes.ts` is stored in ET for every instrument (see run_dax_minutes), but
# "the 09:00 candle" means the exchange's own clock. Offset in hours from ET
# to exchange-local, plus the label to print.
#
# CAVEAT: a fixed offset, because the RTH window itself is a fixed ET window
# in this repo. ET and CET switch DST on different dates, so for ~2 weeks a
# year the true CET clock is one hour off these labels. Slot ORDER and every
# count are unaffected — only the printed label drifts, and only in those weeks.
EXCHANGE_CLOCK = {
    "DAX": (6, "CET"),
    "US30": (0, "ET"),
}

HEAT_COLOR = "126, 200, 227"  # the blue used for manual shading elsewhere in the app


def _clock(instrument: str) -> tuple[int, str]:
    return EXCHANGE_CLOCK.get(instrument, (0, "ET"))


@st.cache_data(show_spinner="Loading minute bars…")
def _load_bars(_conn, instrument: str, rth_only: bool, tf_min: int):
    """Session-tagged OHLC at the detection timeframe, plus the session open.

    Cached separately from detection so that changing the threshold (or any
    display control) never re-reads the 2M-row minute table.
    """
    df = load_session_minutes(_conn, instrument, rth_only)
    if df.is_empty():
        return df, 0
    open_mod = session_open_mod(df)
    return aggregate_to_tf(df, tf_min), open_mod


@st.cache_data(show_spinner="Detecting legs…")
def _pivots_for(_bars: pl.DataFrame, threshold: float, mode: str):
    return extract_pivots(_bars, float(threshold), mode=mode)


@st.cache_data
def _coverage_for(_bars: pl.DataFrame, open_mod: int, slot_min: int, n_slots: int,
                  date_from: str, date_to: str):
    spans = session_spans(
        _bars.filter((pl.col("date") >= date_from) & (pl.col("date") <= date_to)),
        open_mod, slot_min,
    )
    return slot_coverage(spans, n_slots), spans.height


def _filtered_pivots(pivots: pl.DataFrame, date_from: str, date_to: str, kinds: list[str],
                     drop_boundary: bool, drop_seed: bool, confirmed_only: bool,
                     leg_side: str) -> pl.DataFrame:
    if pivots.is_empty():
        return pivots
    out = pivots.filter(
        (pl.col("date") >= date_from) & (pl.col("date") <= date_to)
        & pl.col("kind").is_in(kinds)
    )
    if drop_boundary:
        out = out.filter(~pl.col("is_first") & ~pl.col("is_last"))
    if drop_seed:
        out = out.filter((pl.col("pivot_ord") >= 2) & (pl.col("pivot_rord") >= 2))
    if confirmed_only:
        # Null (no leg on that side) is kept: the pivot itself still counts as
        # an occurrence, it just contributes no magnitude. Only an explicitly
        # UNconfirmed leg is dropped.
        out = out.filter(pl.col(f"{leg_side}_confirmed") != False)  # noqa: E712
    return out


def _render_controls(conn) -> dict:
    instruments = available_instruments(conn)
    default_ix = instruments.index("DAX") if "DAX" in instruments else 0

    c1, c2, c3, c4, c5 = st.columns([1.1, 1, 1, 1, 1.4])
    with c1:
        instrument = st.selectbox("Instrument", instruments, index=default_ix, key="pt_inst")
    with c2:
        basis = st.radio("Session", ["RTH", "All bars"], horizontal=True, key="pt_basis")
    with c3:
        tf_min = st.selectbox("Detect on", TF_CHOICES, index=0,
                              format_func=lambda v: f"{v}-min bars", key="pt_tf")
    with c4:
        slot_min = st.selectbox("Candle grid", SLOT_CHOICES, index=0,
                                format_func=lambda v: f"{v} min", key="pt_slot")
    with c5:
        mode = st.radio("Leg mode", ["extreme_to_extreme", "close_to_close"],
                        horizontal=True, key="pt_mode")

    lo, hi = date_bounds(conn, instrument)
    d1, d2, d3, d4 = st.columns([1.6, 1, 1, 1.2])
    with d1:
        rng = st.date_input(
            "Date range", value=(pd.to_datetime(lo), pd.to_datetime(hi)),
            min_value=pd.to_datetime(lo), max_value=pd.to_datetime(hi), key="pt_dates",
        )
        if isinstance(rng, tuple) and len(rng) == 2:
            date_from, date_to = str(rng[0]), str(rng[1])
        else:
            date_from, date_to = lo, hi
    with d2:
        kind_label = st.radio("Pivots", ["Both", "Tops", "Bottoms"], horizontal=True, key="pt_kind")
    with d3:
        leg_label = st.radio("Measure leg", ["Next (from pivot)", "Previous (into pivot)"],
                             key="pt_side")
    with d4:
        drop_boundary = st.checkbox("Drop session-boundary pivots", value=True,
                                    key="pt_boundary",
                                    help="The detector's first and last pivot of a session sit at "
                                         "the open and close by construction, not because the "
                                         "market turned. Unticking puts a ~100% artifact spike on "
                                         "the first and last slot.")
        drop_seed = st.checkbox("Also drop seed/trailing-leg pivots (#1, #n−1)",
                                value=False, key="pt_seed",
                                help="Pivot #1 is where the session's FIRST threshold-sized move "
                                     "ended. It is a real reversal, but it is also why the opening "
                                     "candles look hot: at 40pt on DAX it is 60% of all pivots in "
                                     "the first half hour, and ticking this takes the 09:00 candle "
                                     "from 1,526 pivots to 209 while midday barely moves. Tick it "
                                     "to see the opening cluster with that mechanic removed.")
        confirmed_only = st.checkbox("Confirmed legs only", value=True, key="pt_conf")

    thresholds = st.multiselect("Gyration sizes (points)", THRESHOLDS, default=DEFAULT_THRESHOLDS,
                                key="pt_thr")

    kinds = {"Both": ["top", "bottom"], "Tops": ["top"], "Bottoms": ["bottom"]}[kind_label]
    return {
        "instrument": instrument,
        "rth_only": basis == "RTH",
        "tf_min": tf_min,
        "slot_min": slot_min,
        "mode": mode,
        "date_from": date_from,
        "date_to": date_to,
        "kinds": kinds,
        "kind_label": kind_label,
        "leg_side": "next" if leg_label.startswith("Next") else "prev",
        "drop_boundary": drop_boundary,
        "drop_seed": drop_seed,
        "confirmed_only": confirmed_only,
        "thresholds": sorted(thresholds),
    }


def _label_for(row_time: str, offset_h: int) -> str:
    """Shift an ET "HH:MM" slot label into exchange-local time."""
    h, m = int(row_time[:2]), int(row_time[3:])
    return f"{(h + offset_h) % 24:02d}:{m:02d}"


def _to_display(tbl: pl.DataFrame, offset_h: int, base_pct: float) -> pd.DataFrame:
    df = tbl.to_pandas()
    df["time"] = df["time"].map(lambda t: _label_for(t, offset_h))
    df["excess"] = df["hit_pct"] - base_pct
    cols = {
        "time": "Candle",
        "sessions": "Sess",
        "pivots": "Pivots",
        "tops": "Tops",
        "bottoms": "Bots",
        "rate": "Rate",
        "rate_pct": "Rate %",
        "hit_pct": "Hit %",
        "excess": "vs base",
        "n_legs": "Legs",
        "avg_leg": "Avg pts",
        "avg_dur": "Avg min",
    }
    for p in REACH_PCTS:
        cols[f"p{p}"] = f"{p}% ≥"
    return df[list(cols)].rename(columns=cols)


def _style(df: pd.DataFrame):
    hit = df["Hit %"]
    span = max(float(hit.max()) - float(hit.min()), 1e-9)
    lo = float(hit.min())

    def _heat(v):
        if pd.isna(v):
            return ""
        t = max(0.0, min(1.0, (v - lo) / span))
        return f"background-color: rgba({HEAT_COLOR}, {t * 0.55:.3f})"

    def _excess(v):
        if pd.isna(v):
            return ""
        return f"color: {COLOR_POS}" if v > 0 else (f"color: {COLOR_NEG}" if v < 0 else "")

    fmt = {
        "Rate": "{:.3f}", "Rate %": "{:.1f}%", "Hit %": "{:.1f}%", "vs base": "{:+.1f}pp",
        "Avg pts": "{:.1f}", "Avg min": "{:.0f}",
        "Sess": "{:,.0f}", "Pivots": "{:,.0f}", "Tops": "{:,.0f}", "Bots": "{:,.0f}",
        "Legs": "{:,.0f}",
    }
    fmt.update({f"{p}% ≥": "{:.0f}" for p in REACH_PCTS})
    return (
        df.style.format(fmt, na_rep="—")
        .map(_heat, subset=["Hit %"])
        .map(_excess, subset=["vs base"])
    )


def _profile_chart(tbl: pl.DataFrame, offset_h: int, base_count: float, title: str) -> go.Figure:
    labels = [_label_for(t, offset_h) for t in tbl["time"]]
    fig = go.Figure()
    fig.add_bar(x=labels, y=tbl["tops"].to_list(), name="Tops", marker_color=COLOR_NEG)
    fig.add_bar(x=labels, y=tbl["bottoms"].to_list(), name="Bottoms", marker_color=COLOR_POS)
    # Counts, so the baseline is the flat per-candle mean — NOT the Hit%
    # baseline, which is a P(at-least-one) and would sit lower.
    fig.add_scatter(
        x=labels, y=[base_count] * tbl.height,
        name="even-spread baseline", mode="lines",
        line=dict(color="#888", width=1, dash="dot"),
    )
    fig.update_layout(
        barmode="stack", height=300, title=title,
        margin=dict(l=10, r=10, t=40, b=10),
        legend=dict(orientation="h", y=1.12, x=0),
        xaxis=dict(tickangle=-90, tickfont=dict(size=9)),
        yaxis_title="pivots",
    )
    return fig


def _heatmap(rows: dict[int, pl.DataFrame], offset_h: int, clock_name: str) -> go.Figure:
    thresholds = sorted(rows)
    any_tbl = rows[thresholds[0]]
    labels = [_label_for(t, offset_h) for t in any_tbl["time"]]
    z = [rows[t]["hit_pct"].to_list() for t in thresholds]
    fig = go.Figure(go.Heatmap(
        z=z, x=labels, y=[f"{t}pt" for t in thresholds],
        colorscale="Blues", colorbar=dict(title="Hit %"),
        hovertemplate="%{y} · %{x}<br>%{z:.1f}% of sessions<extra></extra>",
    ))
    fig.update_layout(
        height=90 + 34 * len(thresholds),
        margin=dict(l=10, r=10, t=10, b=10),
        xaxis=dict(tickangle=-90, tickfont=dict(size=9), title=f"session candle ({clock_name})"),
        yaxis=dict(autorange="reversed"),
    )
    return fig


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Pivots TimeMap 1.0", layout="wide")
    inject_shared_css()
    st.markdown('<h2 style="font-size:1.3rem; font-weight:700;">Pivots TimeMap 1.0</h2>',
                unsafe_allow_html=True)

    conn = get_connection()
    cfg = _render_controls(conn)
    if not cfg["thresholds"]:
        st.info("Pick at least one gyration size.")
        return

    bars, open_mod = _load_bars(conn, cfg["instrument"], cfg["rth_only"], cfg["tf_min"])
    if bars.is_empty():
        st.warning(f"No minute bars for {cfg['instrument']}.")
        return

    slot_min = cfg["slot_min"]
    span = (
        bars["ts"].dt.hour().cast(pl.Int32) * 60 + bars["ts"].dt.minute().cast(pl.Int32)
    )
    n_slots = int((int(span.max()) - open_mod) // slot_min) + 1
    coverage, n_sessions = _coverage_for(bars, open_mod, slot_min, n_slots,
                                         cfg["date_from"], cfg["date_to"])
    if not n_sessions:
        st.warning("No sessions in the selected date range.")
        return

    offset_h, clock_name = _clock(cfg["instrument"])
    open_label = _label_for(
        f"{open_mod // 60:02d}:{open_mod % 60:02d}", offset_h
    )

    occupied = max(sum(1 for c in coverage if c), 1)
    tables: dict[int, pl.DataFrame] = {}
    bases: dict[int, float] = {}
    totals: dict[int, int] = {}
    for thr in cfg["thresholds"]:
        pivots = _pivots_for(bars, thr, cfg["mode"])
        kept = _filtered_pivots(pivots, cfg["date_from"], cfg["date_to"], cfg["kinds"],
                                cfg["drop_boundary"], cfg["drop_seed"],
                                cfg["confirmed_only"], cfg["leg_side"])
        tbl = slot_table(kept, coverage, open_mod, slot_min, n_slots,
                         leg_side=cfg["leg_side"])
        tables[thr] = tbl
        totals[thr] = kept.height
        # Even-spread baseline for Hit%, which is a P(at-least-one), not a rate:
        # scattering k pivots uniformly over m candles leaves a given candle
        # empty with probability (1-1/m)^k. Dividing k by m instead would
        # overstate the bar wherever k is large (threshold 40 runs ~7/session).
        per_session = (kept.height / n_sessions) if n_sessions else 0.0
        bases[thr] = (1 - (1 - 1 / occupied) ** per_session) * 100

    st.markdown(
        f'<div style="font-size:0.82rem; opacity:0.75; margin:0.4rem 0 0.2rem;">'
        f'{n_sessions:,} sessions &nbsp;|&nbsp; {cfg["date_from"]} → {cfg["date_to"]} '
        f'&nbsp;|&nbsp; {n_slots} × {slot_min}-min candles from {open_label} {clock_name} '
        f'&nbsp;|&nbsp; {cfg["kind_label"].lower()} pivots, measuring the '
        f'{"leg starting at" if cfg["leg_side"] == "next" else "leg ending at"} each pivot'
        f'</div>', unsafe_allow_html=True,
    )
    if not cfg["drop_boundary"]:
        st.warning(
            "Session-boundary pivots are included: the first and last slot will show a large "
            "spike that is the detector's seeding/truncation, not market behaviour."
        )

    st.markdown("---")
    st.markdown('<h3 style="font-size:1.05rem; font-weight:700;">Hit rate by candle and size</h3>',
                unsafe_allow_html=True)
    st.caption("Share of sessions with at least one pivot in that candle. Read a row against its "
               "own even-spread baseline, printed per size in the tabs below.")
    st.plotly_chart(_heatmap(tables, offset_h, clock_name), use_container_width=True)

    st.markdown("---")
    tabs = st.tabs([f"{t} pt" for t in cfg["thresholds"]])
    for tab, thr in zip(tabs, cfg["thresholds"]):
        with tab:
            tbl, base = tables[thr], bases[thr]
            st.markdown(
                f'<div style="font-size:0.82rem; opacity:0.8;">'
                f'{totals[thr]:,} pivots &nbsp;|&nbsp; '
                f'{totals[thr] / n_sessions:.2f} per session &nbsp;|&nbsp; '
                f'even-spread baseline <b>{base:.2f}%</b> per candle</div>',
                unsafe_allow_html=True,
            )
            st.plotly_chart(
                _profile_chart(tbl, offset_h, totals[thr] / occupied,
                               f"{thr}pt pivots per candle"),
                use_container_width=True,
            )
            st.dataframe(
                _style(_to_display(tbl, offset_h, base)),
                use_container_width=True, hide_index=True, height=520,
            )
            st.caption(
                f"“{REACH_PCTS[0]}% ≥” reads: {REACH_PCTS[0]}% of the legs measured from pivots in "
                f"that candle travelled at least that many points. Legs are threshold-filtered, so "
                f"the deepest column sits near {thr} by construction — the spread between it and "
                f"“{REACH_PCTS[-1]}% ≥” is where the information is. "
                "**The last few candles are survivorship-selected:** a pivot only appears there if "
                "another whole leg still fitted before the close, so those rows keep just the "
                "fastest moves — their Avg min collapses toward zero and their point figures are "
                "not comparable with mid-session rows."
            )


if __name__ == "__main__":
    main()
