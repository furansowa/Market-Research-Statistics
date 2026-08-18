"""Gyrational Time v1.0 — duration-rank 4-leg pattern classification.

Sibling to Gyrational Waves v1.0: same underlying legs (real, threshold-
filtered, alternating up/down/up/down or down/up/down/up), same M/W family
split (from the observed/4th leg's actual price direction) — but instead of
ranking the 4-leg window's 5 pivot PRICES, this page ranks its 4 leg
DURATIONS (minutes). See gyrations/time_patterns.py for why that means 24
patterns per family (M1-M24/W1-W24) rather than 16 — duration has no
sign/direction, so there's no alternation constraint to cut permutations
down the way price pivots had one.

Deliberately has none of the other pages' session-level filters — this
page's own instrument/chart-basis/date-range/threshold/leg-detection
controls are its only inputs, identical in shape to Gyrational Waves'.

Every Streamlit widget key on this page is prefixed "gt_" (Gyrational Time)
-- session_state is shared across every page in this multipage app, and
Gyrational Waves already uses "gw_"-prefixed keys with overlapping label
strings (both pages have "M1".."M16"-shaped labels) that would otherwise
collide.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import REGISTRY_BY_NAME, COLOR_POS, COLOR_NEG, COLOR_SB, COLOR_BS
from gyrations.time_patterns import M_LABELS, W_LABELS, build_time_patterns
from query.gyr_waves import fetch_legs, fetch_session_rows, fetch_full_session_row

from dashboard import (
    get_config,
    get_connection,
    get_instruments,
    get_date_bounds,
    inject_shared_css,
    read_minutes,
    build_display_table,
    _add_rth_open_line,
)

MAX_CHARTS = 20
CARDS_PER_ROW = 2
CONFIRMED_ONLY = True  # pattern classification always excludes the still-forming trailing leg

DAY_TABLE_COLUMNS = ["date", "weekday", "bs_sb", "rth_range", "gap_pts", "rel_close_pts", "abs_close_pts"]

# "p% at least X min" lines, in the exact order requested — X is the
# (100-p)th percentile of the total-pattern-duration distribution (90% of
# occurrences take *at least* the 10th percentile's value, by definition).
DISTRIBUTION_PCTS = [90, 80, 75, 50, 25, 20, 10, 1]


def _tight(text, color=None, margin_right="0") -> str:
    """Inline span sized to its own content (not a fixed column width) with
    an explicit right margin — groups a line's tag/count/pct closely while
    still controlling the gap to the next group precisely."""
    style = f"margin-right:{margin_right};"
    if color:
        style += f" color:{color};"
    return f'<span style="{style}">{text}</span>'


def _cmp_line(label: str, counts: dict, total: int) -> str:
    """"<label> :  shorter N pct%    longer N pct%  (flat: n)" — same
    tight-grouped, sign-colored style as Gyrational Waves' up/down lines,
    just relabeled for a shorter/longer duration comparison."""
    shorter, longer, flat = counts.get("shorter", 0), counts.get("longer", 0), counts.get("flat", 0)
    pct = (lambda n: f"{n / total * 100:.1f}%") if total else (lambda n: "—")
    shorter_c = COLOR_POS if shorter >= longer else None
    longer_c = COLOR_NEG if longer > shorter else None
    line = (
        _tight(label, margin_right="0.6em")
        + _tight("shorter", margin_right="0.5em")
        + _tight(str(shorter), shorter_c, margin_right="0.45em")
        + _tight(pct(shorter), shorter_c, margin_right="2.2em")
        + _tight("longer", margin_right="0.5em")
        + _tight(str(longer), longer_c, margin_right="0.45em")
        + _tight(pct(longer), longer_c, margin_right="0.6em" if flat else "0")
    )
    if flat:
        line += _tight(f"(flat: {flat})")
    return line


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile (numpy's default method) over an
    already-ascending-sorted list. `pct` in [0, 100]."""
    n = len(sorted_vals)
    if n == 1:
        return sorted_vals[0]
    k = (pct / 100) * (n - 1)
    f, c = int(k), min(int(k) + 1, n - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


@st.cache_data
def _load_time_patterns(_conn, instrument: str, scope: str, threshold: float, mode: str):
    """(legs, patterns) for one (instrument, scope, threshold, mode) — legs is
    the full chronological leg list, patterns is the dict[int, TimePattern]
    built from it. Cached: walks the whole leg history (up to a few million
    rows at low thresholds), doesn't depend on anything session-local."""
    legs = fetch_legs(_conn, instrument, scope, threshold, mode, confirmed_only=CONFIRMED_ONLY)
    patterns = build_time_patterns(legs)
    return legs, patterns


# Every "N{n_idx} vs P{p_idx}" comparison the user asked for, n_idx/p_idx in
# 1..4 (the 4 legs of the next / current pattern respectively), plus one
# sentinel (0, 0) for the total-duration-sum comparison.
CMP_KEYS: list[tuple[int, int]] = [(0, 0)] + [(n, p) for n in range(1, 5) for p in range(1, 5)]


def _time_pattern_stats(patterns: dict, indices: list[int]) -> dict:
    """For the given pattern indices (all sharing one M{n}/W{n} label): the
    shorter/longer duration comparisons in CMP_KEYS between the next
    pattern's leg durations (N1-N4, plus their sum) and this pattern's own
    (P1-P4, plus their sum), and the next-pattern label distribution. "Next
    pattern" is the simple index lookup `patterns[i + 4]` — see
    time_patterns.py."""
    next_cmp: dict[tuple[int, int], dict] = {key: {"shorter": 0, "longer": 0, "flat": 0} for key in CMP_KEYS}
    next_total = 0
    next_label_dist: dict[str, int] = {}
    label_total = 0

    for i in indices:
        cur = patterns[i]
        nxt = patterns.get(i + 4)
        if nxt is None:
            continue
        next_total += 1

        def _bump(bucket: dict, n_val: float, p_val: float) -> None:
            if n_val < p_val:
                bucket["shorter"] += 1
            elif n_val > p_val:
                bucket["longer"] += 1
            else:
                bucket["flat"] += 1

        _bump(next_cmp[(0, 0)], sum(nxt.durations), sum(cur.durations))
        for n_idx in range(1, 5):
            for p_idx in range(1, 5):
                _bump(next_cmp[(n_idx, p_idx)], nxt.durations[n_idx - 1], cur.durations[p_idx - 1])

        label_total += 1
        next_label_dist[nxt.label] = next_label_dist.get(nxt.label, 0) + 1

    return {
        "next_cmp": next_cmp, "next_total": next_total,
        "next_label_dist": next_label_dist, "label_total": label_total,
    }


def _pattern_dates(patterns: dict, indices: list[int]) -> list[str]:
    dates = set()
    for i in indices:
        for leg in patterns[i].legs:
            dates.add(leg["start_date"])
            dates.add(leg["end_date"])
    return sorted(dates)


def _add_stored_leg_overlay(fig: go.Figure, day_legs: list[dict], color: str = "#7EC8E3") -> None:
    """Draws the ACTUAL stored legs touching this day (not a live recompute
    from that day's bars alone) — the only way to show the real legs for
    scope="continuous", where a leg can start on a previous day."""
    for leg in day_legs:
        x = [pd.to_datetime(leg["start_ts"]), pd.to_datetime(leg["end_ts"])]
        y = [leg["start_price"], leg["end_price"]]
        dash = "solid" if leg["confirmed"] else "dash"
        fig.add_scatter(
            x=x, y=y, mode="lines+markers",
            line=dict(color=color, width=1.5, dash=dash),
            marker=dict(size=5, color=color),
            hovertemplate="%{y:.1f}<br>%{x|%Y-%m-%d %H:%M}<extra></extra>",
            showlegend=False,
        )
        fig.add_annotation(
            x=x[0] + (x[1] - x[0]) / 2, y=(y[0] + y[1]) / 2,
            text=f"{leg['magnitude_pts']:.0f}", showarrow=False,
            font=dict(size=12, color=color),
            yshift=14 if leg["direction"] == "up" else -14,
        )


def _render_day_chart(conn, instrument: str, date: str, chart_basis: str, day_legs: list[dict]) -> None:
    row = fetch_full_session_row(conn, instrument, date)
    if row is None:
        st.warning(f"No session data for {date}.")
        return
    st.markdown(f"**{instrument} — {date} ({row.get('weekday')})**")

    bars = read_minutes(conn, instrument, date, chart_basis)
    if bars.empty:
        st.warning("No minute bars found for this session/basis.")
        return

    fig = go.Figure(data=[go.Candlestick(
        x=bars["ts"], open=bars["open"], high=bars["high"], low=bars["low"], close=bars["close"],
        name=instrument,
    )])
    _add_rth_open_line(fig, pd.Series(row))
    _add_stored_leg_overlay(fig, day_legs)
    fig.update_yaxes(tickformat=".0f")
    fig.update_layout(xaxis_rangeslider_visible=False, height=520, showlegend=False, margin=dict(t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)


def _showdays_key(family: str, label: str) -> str:
    return f"gt_{family}_{label}_showdays"


def _on_showdays_toggle(family: str, label: str, all_labels: dict[str, tuple]) -> None:
    """Enforces "only one pattern's days shown at a time": when a card's
    checkbox is switched on, every other card's checkbox is force-reset to
    False (via session_state, before they're instantiated later this same
    rerun) and `gt_selected` records which one is active. Switching the
    active one off just clears the selection."""
    key = _showdays_key(family, label)
    if st.session_state.get(key):
        for fam, labels in all_labels.items():
            for lbl in labels:
                other_key = _showdays_key(fam, lbl)
                if other_key != key:
                    st.session_state[other_key] = False
        st.session_state["gt_selected"] = (family, label)
    elif st.session_state.get("gt_selected") == (family, label):
        st.session_state["gt_selected"] = None


def _render_card(
    family: str, label: str, indices: list[int], patterns: dict,
    family_total: int, all_labels: dict[str, tuple],
) -> None:
    n = len(indices)
    pct = f"{n / family_total * 100:.1f}%" if family_total else "—"

    with st.container(border=True):
        st.markdown(
            f'<div style="position:relative; margin-bottom:1.1rem;">'
            f'<div style="text-align:center; font-size:1.05rem; font-weight:700;">{label}</div>'
            f'<div style="position:absolute; top:0; right:0; font-weight:400; opacity:0.6; '
            f'font-size:0.8rem;">n={n} ({pct})</div>'
            f'</div>',
            unsafe_allow_html=True,
        )

        if n == 0:
            st.caption("No occurrences in range.")
            return

        # No pattern-diagram image (there is none for a duration pattern) —
        # the duration-rank digit string itself stands in for it, bold and
        # centered where the image sits on Gyrational Waves' cards.
        ranks = patterns[indices[0]].ranks
        st.markdown(
            f'<div style="text-align:center; font-weight:700; font-size:1.3rem; '
            f'margin-bottom:1rem;">{ranks}</div>',
            unsafe_allow_html=True,
        )

        total_durations = sorted(sum(patterns[i].durations) for i in indices)
        dist_lines = [
            f'<div>{p}% at least {_percentile(total_durations, 100 - p):.0f} min</div>'
            for p in DISTRIBUTION_PCTS
        ]
        st.markdown(
            '<div style="font-size:0.76rem;">Pattern duration distribution :</div>'
            '<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.76rem; '
            'margin-top:0.3rem;">' + "".join(dist_lines) + "</div>",
            unsafe_allow_html=True,
        )

        stats = _time_pattern_stats(patterns, indices)
        next_total = stats["next_total"]

        st.markdown('<div style="font-size:0.76rem; margin-top:1.8rem;">Next Pattern :</div>', unsafe_allow_html=True)
        st.markdown(
            '<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.76rem; '
            'white-space:nowrap; margin-top:0.3rem;">'
            + _cmp_line("N1+N2+N3+N4 vs P1+P2+P3+P4 :", stats["next_cmp"][(0, 0)], next_total)
            + "</div>",
            unsafe_allow_html=True,
        )
        for n_idx in range(1, 5):
            group_lines = [
                _cmp_line(f"N{n_idx} vs P{p_idx} :", stats["next_cmp"][(n_idx, p_idx)], next_total)
                for p_idx in range(1, 5)
            ]
            st.markdown(
                '<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.76rem; '
                'white-space:nowrap; margin-top:1.8rem;">' + "<br>".join(group_lines) + "</div>",
                unsafe_allow_html=True,
            )

        dist, dtot = stats["next_label_dist"], stats["label_total"]
        dist_color = COLOR_SB if family == "M" else COLOR_BS
        if dtot:
            all_sorted = sorted(dist.items(), key=lambda kv: -kv[1])
            dist_items = "".join(
                f'<div><span style="color:{dist_color};">{lbl}</span> {c} ({c / dtot * 100:.1f}%)</div>'
                for lbl, c in all_sorted
            )
            dist_html = (
                f'<div style="display:grid; grid-template-columns:repeat(4, 1fr); '
                f'gap:0.5rem 1rem; color:#888;">{dist_items}</div>'
            )
        else:
            dist_html = '<div style="color:#888;">n/a</div>'
        st.markdown(
            '<div style="font-size:0.78rem; margin-top:2.4rem;">'
            '<span style="color:#fff;">Next pattern distribution:</span>'
            f'{dist_html}</div>',
            unsafe_allow_html=True,
        )

        st.markdown("<div style='margin-top:2rem'></div>", unsafe_allow_html=True)
        st.checkbox(
            "Show days", value=False, key=_showdays_key(family, label),
            on_change=_on_showdays_toggle, args=(family, label, all_labels),
        )


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()
    instruments = get_instruments(conn)

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">Gyrational Time v1.0</h2>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Duration-rank 4-leg pattern classification (M1-M24/W1-W24 — same legs and M/W family "
        "as Gyrational Waves, ranked by how long each leg took instead of by pivot price). No "
        "session filters here — just instrument / basis / date range / threshold / leg detection below."
    )

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        instrument = st.selectbox("Instrument", instruments, key="gt_instrument")
    with c2:
        chart_basis = st.radio("Chart basis", ["ETH", "RTH"], index=0, horizontal=True, key="gt_basis")
    with c3:
        mode_label = st.radio(
            "Leg detection", ["Extremum (High/Low)", "Close"], index=0, horizontal=True, key="gt_mode",
        )
    with c4:
        gyr_config = get_config()["gyrations"]
        thresholds = gyr_config["thresholds"]
        default_idx = thresholds.index(40) if 40 in thresholds else 0
        threshold = st.selectbox("Threshold", thresholds, index=default_idx, key="gt_threshold")

    scope = "continuous" if chart_basis == "ETH" else "rth"
    mode = "extreme_to_extreme" if mode_label.startswith("Extremum") else "close_to_close"

    min_date, max_date = get_date_bounds(conn, instrument)
    date_range = st.date_input(
        "Date range",
        value=(pd.to_datetime(min_date), pd.to_datetime(max_date)),
        min_value=pd.to_datetime(min_date), max_value=pd.to_datetime(max_date),
        key="gt_daterange",
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        date_from, date_to = str(date_range[0]), str(date_range[1])
    else:
        date_from, date_to = str(min_date), str(max_date)

    st.markdown("---")

    with st.spinner("Loading legs and building patterns..."):
        legs, patterns = _load_time_patterns(conn, instrument, scope, threshold, mode)

    current_indices = [i for i, p in patterns.items() if date_from <= p.end_date <= date_to]
    m_indices = [i for i in current_indices if patterns[i].family == "M"]
    w_indices = [i for i in current_indices if patterns[i].family == "W"]

    total = len(current_indices)
    total_m, total_w = len(m_indices), len(w_indices)
    m_pct = f"{total_m / total * 100:.1f}%" if total else "—"
    w_pct = f"{total_w / total * 100:.1f}%" if total else "—"

    st.markdown(
        f'<div style="font-size:0.9rem; margin-bottom:1rem;">'
        f'Total patterns: <b>{total:,}</b> &nbsp;|&nbsp; '
        f'M patterns: <b>{total_m:,}</b> ({m_pct}) &nbsp;|&nbsp; '
        f'W patterns: <b>{total_w:,}</b> ({w_pct})'
        f'</div>',
        unsafe_allow_html=True,
    )

    if total == 0:
        st.info("No patterns in the current date range at this threshold.")
        return

    m_by_label: dict[str, list[int]] = {label: [] for label in M_LABELS}
    for i in m_indices:
        m_by_label[patterns[i].label].append(i)
    w_by_label: dict[str, list[int]] = {label: [] for label in W_LABELS}
    for i in w_indices:
        w_by_label[patterns[i].label].append(i)

    all_labels = {"M": M_LABELS, "W": W_LABELS}
    by_label = {"M": m_by_label, "W": w_by_label}

    st.markdown(
        '<h3 style="font-size:1.1rem; font-weight:700; margin:0.8rem 0 0.4rem 0;">M patterns</h3>',
        unsafe_allow_html=True,
    )
    for row_start in range(0, len(M_LABELS), CARDS_PER_ROW):
        cols = st.columns(CARDS_PER_ROW)
        for col, label in zip(cols, M_LABELS[row_start:row_start + CARDS_PER_ROW]):
            with col:
                _render_card("M", label, m_by_label[label], patterns, total_m, all_labels)

    st.markdown(
        '<h3 style="font-size:1.1rem; font-weight:700; margin:1.2rem 0 0.4rem 0;">W patterns</h3>',
        unsafe_allow_html=True,
    )
    for row_start in range(0, len(W_LABELS), CARDS_PER_ROW):
        cols = st.columns(CARDS_PER_ROW)
        for col, label in zip(cols, W_LABELS[row_start:row_start + CARDS_PER_ROW]):
            with col:
                _render_card("W", label, w_by_label[label], patterns, total_w, all_labels)

    st.markdown("---")

    selected = st.session_state.get("gt_selected")
    if not selected:
        st.caption('Check "Show days" on a pattern card above to see its days here.')
        return

    sel_family, sel_label = selected
    sel_indices = by_label[sel_family][sel_label]
    dates = _pattern_dates(patterns, sel_indices)

    st.markdown(
        f'<h3 style="font-size:1.1rem; font-weight:700; margin:0 0 0.6rem 0;">'
        f'Days for {sel_label}</h3>',
        unsafe_allow_html=True,
    )
    if not dates:
        st.caption("No days to show.")
        return

    session_rows = fetch_session_rows(conn, instrument, dates)
    specs = [REGISTRY_BY_NAME[c] for c in DAY_TABLE_COLUMNS]
    styled, column_config = build_display_table(session_rows, specs=specs)
    event = st.dataframe(
        styled, use_container_width=True, hide_index=True, column_config=column_config,
        on_select="rerun", selection_mode="multi-row", key=f"gt_days_table_{sel_family}_{sel_label}",
    )
    st.markdown("<div style='margin-bottom:2rem'></div>", unsafe_allow_html=True)

    selected_rows = event.selection.rows if event and event.selection else []
    if not selected_rows:
        return
    if len(selected_rows) > MAX_CHARTS:
        st.warning(f"{len(selected_rows)} rows selected — showing charts for the first {MAX_CHARTS}.")
        selected_rows = selected_rows[:MAX_CHARTS]

    for idx in selected_rows:
        date = session_rows[idx]["date"]
        day_legs = [leg for leg in legs if leg["start_date"] <= date <= leg["end_date"]]
        _render_day_chart(conn, instrument, date, chart_basis, day_legs)
        st.divider()


if __name__ == "__main__":
    main()
