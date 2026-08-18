"""Time Waves v1.0 -- Merrill M/W patterns over TIME-defined waves.

Same 32 M1-16/W1-16 classification and the same per-pattern statistics as
Gyrational Waves, but the underlying legs come from `gyrations.time_waves`
(a leg ends when its running extreme has stood unbeaten for `min_bars` bars)
instead of a points threshold. That makes "wave size" a consequence rather
than an input, which is the whole point: it lets the patterns be studied as
structures in TIME.

The pattern-statistics helpers are imported from `gyr_waves_page` rather than
copied, so both pages compute the N-vs-P comparisons, next-pattern
distribution and next-leg breakout identically and can never drift apart.
Session-state keys are prefixed `tw_` so the two pages' "Show days" selections
stay independent.

Legs are computed live and cached (a full 6-year 1-minute pass takes well
under a second), so unlike the threshold legs there is no precomputed table to
keep in sync.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gyrations.merrill import M_LABELS, W_LABELS, build_patterns
from gyrations.time_waves import detect_time_legs, legs_to_rows
from query.time_waves import available_instruments, load_minutes, load_day_minutes

from dashboard import get_connection, inject_shared_css
from gyr_waves_page import (
    NP_COMPARISON_GROUPS, _pattern_stats, _dir_line, _yn_stats,
    _load_pattern_image_b64, _add_stored_leg_overlay,
)

CARDS_PER_ROW = 2
MAX_CHARTS = 20
MIN_BARS_CHOICES = [5, 10, 15, 20, 30, 45, 60, 90, 120]
YEARS = 6


@st.cache_data(show_spinner="Detecting time waves and building patterns…")
def _load(_conn, instrument: str, rth_only: bool, min_bars: int,
          use_close: bool, mode: str, years: int):
    df = load_minutes(_conn, instrument, rth_only, years)
    if df.is_empty():
        return [], {}, {}
    legs = detect_time_legs(
        df["high"].to_numpy(), df["low"].to_numpy(), df["close"].to_numpy(),
        min_bars=min_bars, use_close=use_close, mode=mode,
    )
    rows = legs_to_rows(legs, df["ts"].to_list())
    patterns = build_patterns(rows)
    meta = {
        "n_bars": len(df),
        "first": str(df["ts"].min())[:10],
        "last": str(df["ts"].max())[:10],
    }
    return rows, patterns, meta


def _showdays_key(family: str, label: str) -> str:
    return f"tw_{family}_{label}_showdays"


def _on_showdays_toggle(family: str, label: str, all_labels: dict) -> None:
    key = _showdays_key(family, label)
    if st.session_state.get(key):
        for fam, labels in all_labels.items():
            for lbl in labels:
                other = _showdays_key(fam, lbl)
                if other != key:
                    st.session_state[other] = False
        st.session_state["tw_selected"] = (family, label)
    elif st.session_state.get("tw_selected") == (family, label):
        st.session_state["tw_selected"] = None


def _render_card(family, label, indices, patterns, legs, family_total, all_labels) -> None:
    n = len(indices)
    pct = f"{n / family_total * 100:.1f}%" if family_total else "—"

    with st.container(border=True):
        st.markdown(
            f'<div style="position:relative; margin-bottom:1.1rem;">'
            f'<div style="text-align:center; font-size:1.05rem; font-weight:700;">{label}</div>'
            f'<div style="position:absolute; top:0; right:0; font-weight:400; opacity:0.6; '
            f'font-size:0.8rem;">n={n} ({pct})</div></div>',
            unsafe_allow_html=True,
        )
        img = _load_pattern_image_b64(label)
        if img:
            st.markdown(
                '<div style="text-align:center; margin-bottom:1rem;">'
                f'<img src="data:image/png;base64,{img}" style="width:150px; max-width:100%;" />'
                '</div>', unsafe_allow_html=True,
            )
        if n == 0:
            st.caption("No occurrences in range.")
            return

        stats = _pattern_stats(patterns, legs, indices, family)

        # time-specific: how long this pattern's 4 legs took, in bars
        spans = [patterns[i].legs[-1]["end_ts"] - patterns[i].legs[0]["start_ts"] for i in indices]
        span_min = np.array([s.total_seconds() / 60 for s in spans])
        st.markdown(
            f'<div style="font-size:0.76rem; margin-bottom:0.6rem; color:#9ecbff;">'
            f'Duration: median {np.median(span_min):,.0f} min &nbsp;|&nbsp; '
            f'p25 {np.percentile(span_min, 25):,.0f} &nbsp;|&nbsp; '
            f'p75 {np.percentile(span_min, 75):,.0f}</div>',
            unsafe_allow_html=True,
        )

        st.markdown('<div style="font-size:0.76rem;">Next Pattern :</div>', unsafe_allow_html=True)
        for gi, (n_idx, p_indices) in enumerate(NP_COMPARISON_GROUPS):
            lines = [
                _dir_line(f"N{n_idx} vs P{p_idx} :", stats["next_vs"][(n_idx, p_idx)],
                          stats["next_dir_total"])
                for p_idx in p_indices
            ]
            mt = "0.3rem" if gi == 0 else "1.8rem"
            st.markdown(
                f'<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.76rem; '
                f'white-space:nowrap; margin-top:{mt};">' + "<br>".join(lines) + "</div>",
                unsafe_allow_html=True,
            )

        sub = "> pattern high :" if family == "M" else "< pattern low :"
        st.markdown(
            '<div style="font-size:0.76rem; margin-top:1.8rem;">Next leg :</div>'
            '<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.76rem; '
            'white-space:nowrap; margin-top:0.3rem;">'
            + _yn_stats(sub, stats["breakout"], stats["breakout_total"]) + "</div>",
            unsafe_allow_html=True,
        )

        dist, dtot = stats["next_label_dist"], stats["label_total"]
        colour = "#e0803c" if family == "M" else "#3ca0e0"
        if dtot:
            items = "".join(
                f'<div><span style="color:{colour};">{lbl}</span> {c} ({c / dtot * 100:.1f}%)</div>'
                for lbl, c in sorted(dist.items(), key=lambda kv: -kv[1])
            )
            dist_html = (f'<div style="display:grid; grid-template-columns:repeat(4, 1fr); '
                         f'gap:0.5rem 1rem; color:#888;">{items}</div>')
        else:
            dist_html = '<div style="color:#888;">n/a</div>'
        st.markdown(
            '<div style="font-size:0.78rem; margin-top:2.4rem;">'
            '<span style="color:#fff;">Next pattern distribution:</span>'
            f'{dist_html}</div>', unsafe_allow_html=True,
        )

        st.markdown("<div style='margin-top:2rem'></div>", unsafe_allow_html=True)
        st.checkbox("Show days", value=False, key=_showdays_key(family, label),
                    on_change=_on_showdays_toggle, args=(family, label, all_labels))


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">'
        'Time Waves v1.0</h2>', unsafe_allow_html=True,
    )
    st.caption(
        "Arthur Merrill M/W patterns over waves defined by TIME, not size: a leg ends once "
        "its running extreme has stood unbeaten for the chosen number of bars. "
        "1-minute bars, last 6 years. Port of the TimeWaves v1.1 indicator — no lookahead, "
        "so every leg is final when emitted."
    )

    c1, c2, c3, c4, c5 = st.columns(5)
    with c1:
        instrument = st.selectbox("Instrument", available_instruments(conn), key="tw_instrument")
    with c2:
        basis = st.radio("Chart basis", ["ETH", "RTH"], index=0, horizontal=True, key="tw_basis")
    with c3:
        det = st.radio("Leg detection", ["Extremum (High/Low)", "Close"], index=0,
                       horizontal=True, key="tw_det")
    with c4:
        min_bars = st.selectbox("Min bars per leg", MIN_BARS_CHOICES,
                                index=MIN_BARS_CHOICES.index(20), key="tw_minbars")
    with c5:
        seed = st.radio("Seeding", ["PRT (faithful)", "Scan-back"], index=0,
                        horizontal=True, key="tw_seed",
                        help="PRT reproduces the indicator exactly, including its blind spot: "
                             "a new leg's extreme is seeded from the confirmation bar, so a more "
                             "extreme price between the pivot and that bar is missed. Scan-back "
                             "seeds from the true extreme since the pivot — still uses only "
                             "already-closed bars, so it stays non-repainting.")

    rth_only = basis == "RTH"
    use_close = det == "Close"
    mode = "prt" if seed.startswith("PRT") else "scan_back"

    legs, patterns, meta = _load(conn, instrument, rth_only, int(min_bars), use_close, mode, YEARS)
    if not legs:
        st.warning("No minute bars for this instrument.")
        return

    date_range = st.date_input(
        "Date range",
        value=(pd.to_datetime(meta["first"]), pd.to_datetime(meta["last"])),
        min_value=pd.to_datetime(meta["first"]), max_value=pd.to_datetime(meta["last"]),
        key="tw_daterange",
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        date_from, date_to = str(date_range[0]), str(date_range[1])
    else:
        date_from, date_to = meta["first"], meta["last"]

    st.markdown("---")

    cur = [i for i, p in patterns.items() if date_from <= p.end_date <= date_to]
    m_idx = [i for i in cur if patterns[i].family == "M"]
    w_idx = [i for i in cur if patterns[i].family == "W"]
    total, tm, tw = len(cur), len(m_idx), len(w_idx)

    dur = np.array([leg["duration_bars"] for leg in legs])
    mag = np.array([leg["magnitude_pts"] for leg in legs])
    st.markdown(
        f'<div style="font-size:0.9rem; margin-bottom:0.4rem;">'
        f'Total patterns: <b>{total:,}</b> &nbsp;|&nbsp; '
        f'M: <b>{tm:,}</b> ({tm/total*100:.1f}%) &nbsp;|&nbsp; '
        f'W: <b>{tw:,}</b> ({tw/total*100:.1f}%)</div>'
        f'<div style="font-size:0.82rem; opacity:0.75;">'
        f'{len(legs):,} legs from {meta["n_bars"]:,} 1-min bars '
        f'({meta["first"]} → {meta["last"]}) &nbsp;|&nbsp; '
        f'leg duration median <b>{np.median(dur):.0f}</b> bars '
        f'(p90 {np.percentile(dur, 90):.0f}) &nbsp;|&nbsp; '
        f'leg size median <b>{np.median(mag):.1f}</b> pts '
        f'(p90 {np.percentile(mag, 90):.0f})</div>',
        unsafe_allow_html=True,
    )
    if total == 0:
        st.info("No patterns in the selected date range.")
        return

    m_by = {lbl: [] for lbl in M_LABELS}
    for i in m_idx:
        m_by[patterns[i].label].append(i)
    w_by = {lbl: [] for lbl in W_LABELS}
    for i in w_idx:
        w_by[patterns[i].label].append(i)
    all_labels = {"M": M_LABELS, "W": W_LABELS}
    by_label = {"M": m_by, "W": w_by}

    for title, labels, by, tot, fam in (
        ("M patterns", M_LABELS, m_by, tm, "M"),
        ("W patterns", W_LABELS, w_by, tw, "W"),
    ):
        st.markdown(
            f'<h3 style="font-size:1.1rem; font-weight:700; margin:1.2rem 0 0.4rem 0;">{title}</h3>',
            unsafe_allow_html=True,
        )
        for row_start in range(0, len(labels), CARDS_PER_ROW):
            cols = st.columns(CARDS_PER_ROW)
            for col, label in zip(cols, labels[row_start:row_start + CARDS_PER_ROW]):
                with col:
                    _render_card(fam, label, by[label], patterns, legs, tot, all_labels)

    st.markdown("---")
    selected = st.session_state.get("tw_selected")
    if not selected:
        st.caption('Check "Show days" on a pattern card above to see its days here.')
        return

    fam, lbl = selected
    sel = by_label[fam][lbl]
    dates = sorted({d for i in sel for leg in patterns[i].legs
                    for d in (leg["start_date"], leg["end_date"])})
    st.markdown(
        f'<h3 style="font-size:1.1rem; font-weight:700; margin:0 0 0.6rem 0;">'
        f'Days for {lbl} ({len(dates):,})</h3>', unsafe_allow_html=True,
    )
    if not dates:
        st.caption("No days to show.")
        return

    pick = st.multiselect("Days to chart", dates, default=dates[:1], key=f"tw_days_{fam}_{lbl}")
    if len(pick) > MAX_CHARTS:
        st.warning(f"{len(pick)} days selected — showing the first {MAX_CHARTS}.")
        pick = pick[:MAX_CHARTS]

    for date in pick:
        bars = load_day_minutes(conn, instrument, date, rth_only)
        if bars.is_empty():
            st.warning(f"No minute bars for {date}.")
            continue
        st.markdown(f"**{instrument} — {date}**")
        pdf = bars.to_pandas()
        fig = go.Figure(data=[go.Candlestick(
            x=pdf["ts"], open=pdf["open"], high=pdf["high"],
            low=pdf["low"], close=pdf["close"], name=instrument,
        )])
        day_legs = [leg for leg in legs if leg["start_date"] <= date <= leg["end_date"]]
        _add_stored_leg_overlay(fig, day_legs)
        fig.update_yaxes(tickformat=".0f")
        fig.update_layout(xaxis_rangeslider_visible=False, height=520,
                          showlegend=False, margin=dict(t=10, b=10))
        st.plotly_chart(fig, use_container_width=True)
        st.divider()


if __name__ == "__main__":
    main()
