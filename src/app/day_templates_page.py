"""Day Templates v1.0 — classify RTH sessions by what the chart LOOKS LIKE.

Bryce Gilmore's A / V / N / reversed-N / M / W day types, made scale-free in
both axes so a 100pt V day and a 350pt V day are the same template (see
gyrations/day_templates.py for the normalisation and the main-leg walk).

Two classification modes:
  Gilmore templates  -- main legs from a zigzag whose threshold is a
                        PERCENTAGE of the session's own range, named by the
                        resulting swing-direction string. Interpretable.
  Discovered (k-means) -- clusters the normalised close paths directly and
                        lets the data pick its own templates. Useful as a
                        cross-check that the named set isn't missing a
                        common shape.

This is the only page with a working instrument selector: it reads
`day_profiles` and `bars`, both of which cover DAX as well as US30, rather
than `sessions`/`minutes`, which are US30-only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import COLOR_POS, COLOR_NEG, COLOR_BS, COLOR_SB
from gyrations.day_templates import K_BUCKETS, TEMPLATE_ORDER, classify
from query.day_templates import (
    available_instruments, date_bounds, load_profiles, load_day_bars,
)

from dashboard import get_connection, inject_shared_css

MAX_CHARTS = 20
CARDS_PER_ROW = 4
CHART_TF = 5


# ---------------------------------------------------------------- shapes ---

def _shape_svg(paths: np.ndarray, color: str, width: int = 210, height: int = 96) -> str:
    """Inline SVG of the mean normalised path with a +/-1 sd band.

    The band is the point: it shows how tight the template is. A tight band
    means every day in the bucket really does look alike; a fat one means the
    label is covering a lot of visual variety.
    """
    if len(paths) == 0:
        return ""
    mean = paths.mean(axis=0)
    sd = paths.std(axis=0) if len(paths) > 1 else np.zeros_like(mean)
    n = len(mean)
    pad = 6

    def pt(i, v):
        x = pad + (width - 2 * pad) * i / max(n - 1, 1)
        y = height - pad - (height - 2 * pad) * float(np.clip(v, 0, 1))
        return f"{x:.1f},{y:.1f}"

    upper = " ".join(pt(i, mean[i] + sd[i]) for i in range(n))
    lower = " ".join(pt(i, mean[i] - sd[i]) for i in range(n - 1, -1, -1))
    line = " ".join(pt(i, mean[i]) for i in range(n))
    return (
        f'<svg viewBox="0 0 {width} {height}" width="100%" '
        f'style="display:block; max-width:{width}px; margin:0 auto;">'
        f'<rect x="0" y="0" width="{width}" height="{height}" fill="none" '
        f'stroke="#333" stroke-width="1" rx="4"/>'
        f'<polygon points="{upper} {lower}" fill="{color}" opacity="0.16"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" stroke-width="2"/>'
        f'</svg>'
    )


# ------------------------------------------------------------ classify -----

@st.cache_data(show_spinner=False)
def _load(_conn, instrument: str) -> list[dict]:
    return load_profiles(_conn, instrument)


def _dir3(v: float | None, eps: float = 0.0) -> str:
    if v is None:
        return "n/a"
    if v > eps:
        return "up"
    if v < -eps:
        return "down"
    return "flat"


def _add_context(rows: list[dict]) -> list[dict]:
    """Attach previous-session context to each row.

    Definitions match features/registry.py exactly so numbers here agree with
    the other pages:
        gap        = today's RTH open  - previous RTH close
        rel close  = a day's own close - its own open
        abs close  = a day's close     - the prior day's close

    Everything is also expressed as a % of the PREVIOUS day's range. Raw
    points are not comparable across a 2009 DAX session and a 2026 one, let
    alone across instruments -- the whole page is built on scale invariance,
    and the context columns have to honour that too or the template
    comparisons below are just reading era and volatility.

    The prev_open/prev_close/prev_range/prev2_close fields come from the
    STORED columns, not from the preceding row of this list. They were
    computed in run_day_templates.py against the unfiltered session sequence,
    which matters: ~44 early-2009 US30 half-sessions are too short to get a
    shape profile, and reading "previous day" off the previous profiled row
    made the 42 days following one of those measure their gap against a close
    two sessions back (verified against the `sessions` table -- errors up to
    166 points). prev_template and prev_close_pos are looked up by prev_date
    and are simply absent when that session has no profile of its own.
    """
    by_date = {r["date"]: r for r in rows}
    out = []
    for r in rows:
        prev_close, prev_open = r["prev_close"], r["prev_open"]
        prev_range, prev2_close = r["prev_range"], r["prev2_close"]

        gap = rel = abs_c = None
        gap_pct = rel_pct = None
        if prev_close is not None:
            gap = r["rth_open"] - prev_close
            pr = prev_range or 1.0
            gap_pct = gap / pr * 100
            if prev_open is not None:
                rel = prev_close - prev_open
                rel_pct = rel / pr * 100
            if prev2_close is not None:
                abs_c = prev_close - prev2_close

        prev_row = by_date.get(r["prev_date"]) if r["prev_date"] else None
        out.append({
            **r,
            "gap_pts": gap, "gap_pct": gap_pct, "gap_dir": _dir3(gap),
            "prev_rel_pts": rel, "prev_rel_pct": rel_pct, "prev_rel_dir": _dir3(rel),
            "prev_abs_pts": abs_c, "prev_abs_dir": _dir3(abs_c),
            "prev_range_pts": prev_range,
            "prev_close_pos": prev_row["close_pos"] if prev_row else None,
            "prev_template": prev_row["template"] if prev_row else None,
            "range_ratio": (r["range_pts"] / prev_range) if prev_range else None,
        })
    return out


@st.cache_data(show_spinner=False)
def _classified(_conn, instrument: str, threshold: float) -> list[dict]:
    """Every session with its template at this threshold, plus previous-session
    context. Cached per (instrument, threshold) — the zigzag is cheap but this
    runs on every widget interaction otherwise."""
    rows = _load(_conn, instrument)
    out = []
    for r in rows:
        res = classify(r, threshold)
        out.append({**r, **{k: res[k] for k in ("template", "dirs", "n_legs",
                                                "pivots", "cleanliness")}})
    # context needs each row's template, so it runs after classification
    return _add_context(out)


@st.cache_data(show_spinner=False)
def _kmeans(_conn, instrument: str, k: int) -> tuple[list[int], np.ndarray]:
    from sklearn.cluster import KMeans
    rows = _load(_conn, instrument)
    X = np.array([r["c"] for r in rows])
    km = KMeans(n_clusters=k, n_init=10, random_state=42).fit(X)
    # order clusters by mean net move so the labels read sensibly (most
    # bearish shape first) instead of in arbitrary k-means order
    order = np.argsort([X[km.labels_ == i][:, -1].mean() for i in range(k)])
    remap = {old: new for new, old in enumerate(order)}
    return [remap[int(v)] for v in km.labels_], km.cluster_centers_[order]


# --------------------------------------------------------------- charts ----

def _render_day_chart(conn, instrument: str, row: dict) -> None:
    date = row["date"]
    st.markdown(
        f"**{instrument} — {date} ({row['weekday']})** &nbsp;·&nbsp; "
        f"{row['template']} &nbsp;·&nbsp; legs `{row['dirs'] or '—'}` &nbsp;·&nbsp; "
        f"range {row['range_pts']:.0f} pts &nbsp;·&nbsp; "
        f"cleanliness {row['cleanliness']:.2f}"
    )
    bars = load_day_bars(conn, instrument, date, CHART_TF, rth_only=True)
    if bars.is_empty():
        st.warning("No bars found for this session.")
        return

    ts = bars["ts"].to_list()
    fig = go.Figure(data=[go.Candlestick(
        x=ts, open=bars["open"].to_list(), high=bars["high"].to_list(),
        low=bars["low"].to_list(), close=bars["close"].to_list(), name=instrument,
    )])

    # main-leg overlay: pivots are (bucket index, normalised price) -- map the
    # bucket back to a bar via its fraction of the session, and the normalised
    # price back to points via the session's own low/range.
    lo, rng = row["rth_low"], row["range_pts"]
    n_bars = len(ts)
    px, py = [], []
    for b_idx, norm_p in row["pivots"]:
        frac = b_idx / max(K_BUCKETS - 1, 1)
        px.append(ts[min(int(round(frac * (n_bars - 1))), n_bars - 1)])
        py.append(lo + norm_p * rng)
    fig.add_scatter(
        x=px, y=py, mode="lines+markers",
        line=dict(color=COLOR_SB, width=2), marker=dict(size=8, color=COLOR_SB),
        hovertemplate="%{y:.1f}<extra></extra>", showlegend=False,
    )

    fig.update_yaxes(tickformat=".0f")
    fig.update_layout(xaxis_rangeslider_visible=False, height=460, showlegend=False,
                      margin=dict(t=10, b=10))
    st.plotly_chart(fig, use_container_width=True)


# ----------------------------------------------------------------- cards ---

def _card_key(name: str) -> str:
    return f"dt_show_{name}"


def _on_toggle(name: str, all_names: list[str]) -> None:
    """One template's days shown at a time — same interaction as the other
    pages' pattern cards."""
    if st.session_state.get(_card_key(name)):
        for other in all_names:
            if other != name:
                st.session_state[_card_key(other)] = False
        st.session_state["dt_selected"] = name
    elif st.session_state.get("dt_selected") == name:
        st.session_state["dt_selected"] = None


def _render_card(name: str, rows: list[dict], total: int, all_names: list[str],
                 color: str) -> None:
    n = len(rows)
    pct = f"{n / total * 100:.1f}%" if total else "—"
    with st.container(border=True):
        st.markdown(
            f'<div style="position:relative; margin-bottom:0.5rem;">'
            f'<div style="text-align:center; font-size:0.95rem; font-weight:700;">{name}</div>'
            f'<div style="position:absolute; top:0; right:0; font-size:0.72rem; '
            f'opacity:0.6;">n={n} ({pct})</div></div>',
            unsafe_allow_html=True,
        )
        if n:
            st.markdown(_shape_svg(np.array([r["c"] for r in rows]), color),
                        unsafe_allow_html=True)
            clean = np.mean([r["cleanliness"] for r in rows])
            up = sum(1 for r in rows if r["rth_close"] > r["rth_open"])
            rng = np.median([r["range_pts"] for r in rows])
            st.markdown(
                f'<div style="font-family: ui-monospace, Consolas, monospace; '
                f'font-size:0.72rem; margin-top:0.45rem; line-height:1.5;">'
                f'clean {clean:.2f} &nbsp; med rng {rng:.0f}pt<br>'
                f'close <span style="color:{COLOR_POS}">up {up}</span> '
                f'<span style="color:{COLOR_NEG}">dn {n - up}</span> '
                f'({up / n * 100:.0f}% up)</div>',
                unsafe_allow_html=True,
            )
        else:
            st.caption("None in range.")
            return
        st.markdown("<div style='margin-top:0.6rem'></div>", unsafe_allow_html=True)
        st.checkbox("Show days", value=False, key=_card_key(name),
                    on_change=_on_toggle, args=(name, all_names))


# -------------------------------------------------- previous-session view ---

def _pct_up(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r[key] in ("up", "down")]
    if not vals:
        return None
    return sum(v == "up" for v in vals) / len(vals) * 100


def _mean(rows: list[dict], key: str) -> float | None:
    vals = [r[key] for r in rows if r[key] is not None]
    return float(np.mean(vals)) if vals else None


def _render_context_table(buckets: dict, names: list[str], rows: list[dict]) -> None:
    """One row per template: what the PREVIOUS session looked like.

    Every percentage carries its deviation from the all-templates baseline in
    brackets. That's the whole point of the table -- an absolute "54% gapped
    up" means nothing on its own, but "54% (+9)" against a 45% baseline is a
    lead worth chasing. Percentages are computed over rows that actually have
    a direction, so the first session of the history (no predecessor) never
    silently counts as "down".
    """
    st.markdown(
        '<h3 style="font-size:1.05rem; font-weight:700; margin:0 0 0.2rem 0;">'
        'Previous session, by template</h3>', unsafe_allow_html=True,
    )
    st.caption(
        "How the day BEFORE each template looked. Bracketed numbers are the "
        "deviation from the all-templates baseline (bottom row) — that's where "
        "a real common point would show up. Points are normalised to the "
        "previous day's range so eras and instruments stay comparable."
    )

    base = {
        "rel": _pct_up(rows, "prev_rel_dir"),
        "abs": _pct_up(rows, "prev_abs_dir"),
        "gap": _pct_up(rows, "gap_dir"),
        "gap_pct": _mean(rows, "gap_pct"),
        "rel_pct": _mean(rows, "prev_rel_pct"),
        "close_pos": _mean(rows, "prev_close_pos"),
        "ratio": _mean(rows, "range_ratio"),
    }

    def cell(v, b, fmt="{:.1f}%", d="{:+.0f}"):
        if v is None:
            return "—"
        if b is None:
            return fmt.format(v)
        return f"{fmt.format(v)} ({d.format(v - b)})"

    table = []
    for name in names:
        sub = buckets[name]
        prev_t = [r["prev_template"] for r in sub if r["prev_template"]]
        common = max(set(prev_t), key=prev_t.count) if prev_t else "—"
        share = (prev_t.count(common) / len(prev_t) * 100) if prev_t else 0
        table.append({
            "Template": name,
            "n": len(sub),
            "Prev RelClose up": cell(_pct_up(sub, "prev_rel_dir"), base["rel"]),
            "Prev AbsClose up": cell(_pct_up(sub, "prev_abs_dir"), base["abs"]),
            "Gap up": cell(_pct_up(sub, "gap_dir"), base["gap"]),
            "Mean gap (% prev rng)": cell(_mean(sub, "gap_pct"), base["gap_pct"],
                                          "{:+.1f}", "{:+.1f}"),
            "Mean prev RelClose (% prev rng)": cell(_mean(sub, "prev_rel_pct"),
                                                    base["rel_pct"], "{:+.1f}", "{:+.1f}"),
            "Prev close pos": cell(_mean(sub, "prev_close_pos"), base["close_pos"],
                                   "{:.2f}", "{:+.2f}"),
            "Range vs prev": cell(_mean(sub, "range_ratio"), base["ratio"],
                                  "{:.2f}x", "{:+.2f}"),
            "Most common prev template": f"{common} ({share:.0f}%)",
        })

    table.append({
        "Template": "— ALL (baseline) —", "n": len(rows),
        "Prev RelClose up": f"{base['rel']:.1f}%" if base["rel"] is not None else "—",
        "Prev AbsClose up": f"{base['abs']:.1f}%" if base["abs"] is not None else "—",
        "Gap up": f"{base['gap']:.1f}%" if base["gap"] is not None else "—",
        "Mean gap (% prev rng)": f"{base['gap_pct']:+.1f}" if base["gap_pct"] is not None else "—",
        "Mean prev RelClose (% prev rng)": f"{base['rel_pct']:+.1f}" if base["rel_pct"] is not None else "—",
        "Prev close pos": f"{base['close_pos']:.2f}" if base["close_pos"] is not None else "—",
        "Range vs prev": f"{base['ratio']:.2f}x" if base["ratio"] is not None else "—",
        "Most common prev template": "—",
    })

    st.dataframe(pd.DataFrame(table), use_container_width=True, hide_index=True)


# ------------------------------------------------------------------ main ---

def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">'
        'Day Templates v1.0</h2>', unsafe_allow_html=True,
    )
    st.caption(
        "RTH sessions classified by chart shape. Both axes are normalised to the "
        "session's own range and length, so a 100pt V day and a 350pt V day are "
        "the same template."
    )

    instruments = available_instruments(conn)
    if not instruments:
        st.error("No `day_profiles` table — run `python run_day_templates.py` first.")
        return

    c1, c2, c3, c4 = st.columns([1, 1.4, 1, 1])
    with c1:
        instrument = st.selectbox("Instrument", instruments, key="dt_instrument")
    with c2:
        mode = st.radio("Classification", ["Gilmore templates", "Discovered (k-means)"],
                        horizontal=True, key="dt_mode")
    with c3:
        threshold = st.slider("Main-leg size (% of session range)", 10, 50, 30, 5,
                              key="dt_thr") / 100.0
    with c4:
        min_clean = st.slider("Min cleanliness", 0.0, 1.0, 0.0, 0.05, key="dt_clean")

    min_date, max_date = date_bounds(conn, instrument)
    date_range = st.date_input(
        "Date range",
        value=(pd.to_datetime(min_date), pd.to_datetime(max_date)),
        min_value=pd.to_datetime(min_date), max_value=pd.to_datetime(max_date),
        key="dt_daterange",
    )
    if isinstance(date_range, tuple) and len(date_range) == 2:
        date_from, date_to = str(date_range[0]), str(date_range[1])
    else:
        date_from, date_to = min_date, max_date

    with st.expander("Previous-session filters", expanded=False):
        f1, f2, f3, f4 = st.columns(4)
        with f1:
            f_rel = st.selectbox(
                "Prev. rel. close (prev close vs prev open)",
                ["Any", "Up", "Down"], key="dt_f_rel")
        with f2:
            f_abs = st.selectbox(
                "Prev. abs. close (prev close vs close before)",
                ["Any", "Up", "Down"], key="dt_f_abs")
        with f3:
            f_gap = st.selectbox(
                "Gap (today's open vs prev close)",
                ["Any", "Up", "Down"], key="dt_f_gap")
        with f4:
            f_gap_sz = st.selectbox(
                "Gap size (% of prev range)",
                ["Any", "Small (<10%)", "Medium (10-25%)", "Large (>=25%)"],
                key="dt_f_gapsz")

    st.markdown("---")

    all_rows = _classified(conn, instrument, threshold)
    rows = [r for r in all_rows
            if date_from <= r["date"] <= date_to and r["cleanliness"] >= min_clean]

    def _keep(r: dict) -> bool:
        if f_rel != "Any" and r["prev_rel_dir"] != f_rel.lower():
            return False
        if f_abs != "Any" and r["prev_abs_dir"] != f_abs.lower():
            return False
        if f_gap != "Any" and r["gap_dir"] != f_gap.lower():
            return False
        if f_gap_sz != "Any":
            g = r["gap_pct"]
            if g is None:
                return False
            a = abs(g)
            if f_gap_sz.startswith("Small") and not a < 10:
                return False
            if f_gap_sz.startswith("Medium") and not (10 <= a < 25):
                return False
            if f_gap_sz.startswith("Large") and not a >= 25:
                return False
        return True

    n_before = len(rows)
    rows = [r for r in rows if _keep(r)]
    total = len(rows)
    if total == 0:
        st.info("No sessions match the current filters.")
        return
    if total != n_before:
        st.caption(f"Previous-session filters: {total:,} of {n_before:,} sessions kept.")

    if mode == "Gilmore templates":
        buckets: dict[str, list[dict]] = {name: [] for name in TEMPLATE_ORDER}
        for r in rows:
            buckets[r["template"]].append(r)
        names = [n for n in TEMPLATE_ORDER if buckets[n]]
        colors = {n: (COLOR_POS if buckets[n] and
                      np.mean([x["c"][-1] for x in buckets[n]]) > 0.5 else COLOR_BS)
                  for n in names}
    else:
        k = st.slider("Number of clusters", 4, 12, 8, 1, key="dt_k")
        labels, _ = _kmeans(conn, instrument, k)
        by_date = {r["date"]: lbl for r, lbl in zip(_load(conn, instrument), labels)}
        buckets = {f"Cluster {i + 1}": [] for i in range(k)}
        for r in rows:
            lbl = by_date.get(r["date"])
            if lbl is not None:
                buckets[f"Cluster {lbl + 1}"].append(r)
        names = [n for n in buckets if buckets[n]]
        colors = {n: (COLOR_POS if np.mean([x["c"][-1] for x in buckets[n]]) > 0.5
                      else COLOR_BS) for n in names}

    st.markdown(
        f'<div style="font-size:0.9rem; margin-bottom:0.8rem;">'
        f'Sessions in range: <b>{total:,}</b> &nbsp;|&nbsp; '
        f'templates used: <b>{len(names)}</b> &nbsp;|&nbsp; '
        f'mean cleanliness: <b>{np.mean([r["cleanliness"] for r in rows]):.2f}</b>'
        f'</div>', unsafe_allow_html=True,
    )

    for start in range(0, len(names), CARDS_PER_ROW):
        cols = st.columns(CARDS_PER_ROW)
        for col, name in zip(cols, names[start:start + CARDS_PER_ROW]):
            with col:
                _render_card(name, buckets[name], total, names, colors[name])

    st.markdown("---")
    _render_context_table(buckets, names, rows)
    st.markdown("---")

    selected = st.session_state.get("dt_selected")
    if not selected or selected not in buckets:
        st.caption('Check "Show days" on a template above to list its sessions here.')
        return

    day_rows = buckets[selected]
    st.markdown(
        f'<h3 style="font-size:1.1rem; font-weight:700; margin:0 0 0.6rem 0;">'
        f'Days classified as {selected} &nbsp;<span style="opacity:0.55; '
        f'font-weight:400; font-size:0.85rem;">({len(day_rows):,} sessions)</span></h3>',
        unsafe_allow_html=True,
    )
    sort_by = st.radio("Sort by", ["Date", "Cleanliness (best first)", "Range (largest first)"],
                       horizontal=True, key="dt_sort")
    if sort_by.startswith("Cleanliness"):
        day_rows = sorted(day_rows, key=lambda r: -r["cleanliness"])
    elif sort_by.startswith("Range"):
        day_rows = sorted(day_rows, key=lambda r: -r["range_pts"])

    def _r(v, n=1):
        return round(v, n) if v is not None else None

    table = pd.DataFrame([{
        "date": r["date"], "weekday": r["weekday"], "legs": r["dirs"] or "—",
        "clean": round(r["cleanliness"], 2), "range": round(r["range_pts"], 1),
        "open→close": round(r["rth_close"] - r["rth_open"], 1),
        "close pos": round(r["close_pos"], 2),
        "high at": round(r["high_frac"], 2), "low at": round(r["low_frac"], 2),
        "gap": _r(r["gap_pts"]), "gap %prv": _r(r["gap_pct"]),
        "prev RelClose": _r(r["prev_rel_pts"]), "prev Rel %prv": _r(r["prev_rel_pct"]),
        "prev AbsClose": _r(r["prev_abs_pts"]),
        "prev close pos": _r(r["prev_close_pos"], 2),
        "prev range": _r(r["prev_range_pts"]),
        "prev template": r["prev_template"] or "—",
    } for r in day_rows])

    event = st.dataframe(
        table, use_container_width=True, hide_index=True,
        on_select="rerun", selection_mode="multi-row",
        key=f"dt_table_{selected}",
    )
    st.markdown("<div style='margin-bottom:1.5rem'></div>", unsafe_allow_html=True)

    picked = event.selection.rows if event and event.selection else []
    if not picked:
        st.caption("Select rows above to chart them.")
        return
    if len(picked) > MAX_CHARTS:
        st.warning(f"{len(picked)} rows selected — charting the first {MAX_CHARTS}.")
        picked = picked[:MAX_CHARTS]

    for i in picked:
        _render_day_chart(conn, instrument, day_rows[i])
        st.divider()


if __name__ == "__main__":
    main()
