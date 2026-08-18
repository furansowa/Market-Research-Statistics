"""Gyrational Range v1.0 — multi-timeframe range/volatility state.

Studies RANGE rather than direction. At any sample point the market has a
multi-timeframe range state (trailing 1m/5m/10m/15m/30m/1h/4h/1d), each
converted to a CAUSAL bucket (rank inside a trailing reference distribution).
The page answers: given that state, what does the forward range do?

Foundation result this page is built on (DAX hourly, 2026-08-12): trailing
60min range bucket -> forward 60min range bucket has 35.0% persistence
accuracy vs 20% random, while a bootstrap random walk gives 19.8% (i.e.
exactly nothing). Currently-loudest-quintile -> 72.6% chance the next hour
stays in the top two; currently-quietest -> 72.6% chance it stays in the
bottom two. That is volatility clustering, the most documented stylized fact
in empirical finance — real, strong, and unlike direction it survives its
null decisively.

Data comes from the precomputed `range_state` table (run_range_state.py),
which carries per-instrument session windows — US30 09:30-16:00 ET and DAX
Xetra 03:00-11:30 ET — so ETH/RTH means the right thing for each market.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import polars as pl
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import COLOR_POS, COLOR_NEG, COLOR_ZERO, COLOR_BS, COLOR_SB
from gyrations.range_state import (
    TRAIL_HORIZONS,
    FWD_HORIZONS,
    BUCKET_SCHEMES,
    add_causal_buckets,
)
from query.range_state import available_instruments, date_bounds, load_state

from dashboard import get_connection, inject_shared_css

HORIZON_LABEL = {1: "1 min", 5: "5 min", 10: "10 min", 15: "15 min",
                 30: "30 min", 60: "1 hour", 240: "4 hours", 1440: "1 day"}
SEED = 42


@st.cache_data(show_spinner=False)
def _load(_conn, instrument: str, rth_only: bool) -> pl.DataFrame:
    return load_state(_conn, instrument, rth_only)


@st.cache_data(show_spinner=False)
def _bucketed(_conn, instrument: str, rth_only: bool, scheme: str, ref_window: int) -> pl.DataFrame:
    """Trailing + forward buckets. Forward columns are ranked against their
    TRAILING counterpart's reference distribution, so no future value ever
    enters a quantile estimate."""
    df = _load(_conn, instrument, rth_only)
    if df.is_empty():
        return df
    cuts = BUCKET_SCHEMES[scheme]
    trail_cols = [f"tr{h}_pct" for h in TRAIL_HORIZONS]
    fwd_cols = [f"fw{h}_pct" for h in FWD_HORIZONS]
    ref_map = {f"fw{h}_pct": f"tr{h}_pct" for h in FWD_HORIZONS if h in TRAIL_HORIZONS}
    return add_causal_buckets(df, trail_cols + fwd_cols, ref_window, cuts, ref_col_map=ref_map)


def _bucket_color(b: int, n: int) -> str:
    if b <= n / 3:
        return COLOR_BS       # quiet  -> blue
    if b >= n - n / 3 + 0.001:
        return COLOR_SB       # loud   -> orange
    return COLOR_ZERO


def _render_current_state(df: pl.DataFrame, n_buckets: int) -> None:
    row = df.filter(pl.col("tr1440_pct").is_not_null()).tail(1)
    if row.is_empty():
        st.info("No complete state available.")
        return
    r = row.to_dicts()[0]
    st.markdown(
        f'<div style="font-size:0.85rem; margin-bottom:0.6rem;">Latest complete sample: '
        f'<b>{r["ts"]}</b> &nbsp; close <b>{r["close"]:,.1f}</b></div>',
        unsafe_allow_html=True,
    )
    cells = []
    for h in TRAIL_HORIZONS:
        b = r.get(f"b_tr{h}_pct")
        pts, pctv = r.get(f"tr{h}_pts"), r.get(f"tr{h}_pct")
        if b is None or pts is None:
            continue
        col = _bucket_color(int(b), n_buckets)
        cells.append(
            f'<div style="flex:1; min-width:92px; border:1px solid #333; border-radius:6px; '
            f'padding:0.5rem 0.4rem; text-align:center;">'
            f'<div style="font-size:0.72rem; opacity:0.65;">{HORIZON_LABEL[h]}</div>'
            f'<div style="font-size:1.15rem; font-weight:700; color:{col};">Q{int(b)}</div>'
            f'<div style="font-size:0.72rem;">{pts:,.0f} pts</div>'
            f'<div style="font-size:0.72rem; opacity:0.65;">{pctv:.3f}%</div>'
            f'</div>'
        )
    st.markdown(
        '<div style="display:flex; gap:0.4rem; flex-wrap:wrap;">' + "".join(cells) + "</div>",
        unsafe_allow_html=True,
    )
    st.caption(f"Bucket = rank of that horizon's range within its own trailing reference "
               f"distribution. Blue = quiet, orange = loud.")


def _transition_df(cur: np.ndarray, fwd: np.ndarray, n_buckets: int):
    rows, counts = [], []
    for a in range(1, n_buckets + 1):
        m = cur == a
        n = int(m.sum())
        counts.append(n)
        rows.append([float((fwd[m] == b).mean()) * 100 if n else np.nan
                     for b in range(1, n_buckets + 1)])
    idx = [f"now Q{a}" for a in range(1, n_buckets + 1)]
    cols = [f"→Q{b}" for b in range(1, n_buckets + 1)]
    out = pd.DataFrame(rows, index=idx, columns=cols)
    out.insert(0, "n", counts)
    return out


@st.cache_data(show_spinner=False)
def _null_transition(_conn, instrument: str, rth_only: bool, scheme: str,
                     ref_window: int, horizon: int) -> float:
    """Persistence accuracy of the same pipeline on a bootstrap random walk
    built from this instrument's own sampled range series — shuffling the
    sample order destroys clustering while preserving the distribution."""
    df = _bucketed(_conn, instrument, rth_only, scheme, ref_window)
    col_t, col_f = f"tr{horizon}_pct", f"fw{horizon}_pct"
    sub = df.select([col_t, col_f]).drop_nulls()
    if sub.is_empty():
        return float("nan")
    rng = np.random.default_rng(SEED)
    vals = sub[col_t].to_numpy().copy()
    rng.shuffle(vals)
    shuffled = pl.DataFrame({col_t: vals, col_f: np.roll(vals, -1)})
    cuts = BUCKET_SCHEMES[scheme]
    b = add_causal_buckets(shuffled, [col_t, col_f], ref_window, cuts,
                           ref_col_map={col_f: col_t}).drop_nulls()
    if b.is_empty():
        return float("nan")
    return float((b[f"b_{col_t}"].to_numpy() == b[f"b_{col_f}"].to_numpy()).mean() * 100)


def main(standalone: bool = True) -> None:
    if standalone:
        st.set_page_config(page_title="Market Statistics Research v2.0", layout="wide")
    inject_shared_css()
    conn = get_connection()

    st.markdown(
        '<h2 style="font-size:1.4rem; font-weight:700; margin:0 0 0.4rem 0;">Gyrational Range v1.0</h2>',
        unsafe_allow_html=True,
    )
    st.caption(
        "Multi-timeframe range/volatility state. Buckets are causal (ranked against a trailing "
        "reference window only). Forward ranges are ranked against the trailing distribution, "
        "so no future value ever enters a quantile estimate."
    )

    try:
        instruments = available_instruments(conn)
    except Exception:
        instruments = []
    if not instruments:
        st.error("No `range_state` table found. Run:  .venv/Scripts/python.exe run_range_state.py")
        return

    c1, c2, c3, c4 = st.columns(4)
    with c1:
        instrument = st.selectbox("Instrument", instruments, key="rg_instrument")
    with c2:
        scope = st.radio("Scope", ["ETH (all hours)", "RTH (cash session)"],
                         index=0, horizontal=True, key="rg_scope")
    with c3:
        scheme = st.selectbox("Bucket scheme", list(BUCKET_SCHEMES), key="rg_scheme")
    with c4:
        ref_window = st.select_slider(
            "Reference window (samples)", options=[250, 500, 1000, 1500, 2500, 5000],
            value=1500, key="rg_ref",
        )

    rth_only = scope.startswith("RTH")
    n_buckets = len(BUCKET_SCHEMES[scheme]) + 1

    with st.spinner("Loading range state..."):
        df = _bucketed(conn, instrument, rth_only, scheme, ref_window)
    if df.is_empty():
        st.warning("No data for this instrument/scope.")
        return

    lo, hi = date_bounds(conn, instrument)
    st.caption(f"{df.height:,} sample points  |  {lo} → {hi}  |  15-minute sampling grid")

    st.markdown("---")
    st.markdown('<h3 style="font-size:1.1rem; font-weight:700;">Current multi-timeframe state</h3>',
                unsafe_allow_html=True)
    _render_current_state(df, n_buckets)

    st.markdown("---")
    st.markdown('<h3 style="font-size:1.1rem; font-weight:700;">Transition: trailing bucket → forward bucket</h3>',
                unsafe_allow_html=True)
    horizon = st.radio("Forward horizon", FWD_HORIZONS, index=1, horizontal=True,
                       format_func=lambda h: HORIZON_LABEL[h], key="rg_horizon")

    col_t, col_f = f"b_tr{horizon}_pct", f"b_fw{horizon}_pct"
    sub = df.select([col_t, col_f]).drop_nulls()
    cur = sub[col_t].to_numpy()
    fwd = sub[col_f].to_numpy()

    tdf = _transition_df(cur, fwd, n_buckets)
    pct_cols = [c for c in tdf.columns if c != "n"]

    def _heat(v):
        # manual shading -- Styler.background_gradient needs matplotlib, which
        # isn't a dependency of this project and isn't worth adding for this.
        if v != v:
            return ""
        span = max(tdf[pct_cols].to_numpy().max() - 100 / n_buckets, 1e-9)
        t = max(0.0, min(1.0, (v - 100 / n_buckets) / span))
        return f"background-color: rgba(126, 200, 227, {t * 0.55:.3f})"

    styled = (
        tdf.style
        .format({c: "{:.1f}%" for c in pct_cols})
        .format({"n": "{:,.0f}"})
        .map(_heat, subset=pct_cols)
    )
    st.dataframe(styled, use_container_width=True)

    persistence = float((cur == fwd).mean() * 100)
    random_rate = 100 / n_buckets
    null_rate = _null_transition(conn, instrument, rth_only, scheme, ref_window, horizon)
    m1, m2, m3 = st.columns(3)
    m1.metric("Persistence accuracy", f"{persistence:.2f}%")
    m2.metric("Random baseline", f"{random_rate:.2f}%")
    m3.metric("Shuffled null", f"{null_rate:.2f}%" if null_rate == null_rate else "—")

    st.markdown("---")
    st.markdown('<h3 style="font-size:1.1rem; font-weight:700;">Expansion / contraction probabilities</h3>',
                unsafe_allow_html=True)
    top, bot = n_buckets, 1
    lines = []
    for label, mask, targets, tlabel in (
        (f"currently Q{top} (loudest)", cur == top, {top, top - 1}, f"Q{top-1} or Q{top}"),
        (f"currently Q{top} (loudest)", cur == top, {bot}, f"Q{bot}"),
        (f"currently Q{bot} (quietest)", cur == bot, {bot, bot + 1}, f"Q{bot} or Q{bot+1}"),
        (f"currently Q{bot} (quietest)", cur == bot, {top}, f"Q{top}"),
    ):
        n = int(mask.sum())
        if not n:
            continue
        p = float(np.isin(fwd[mask], list(targets)).mean() * 100)
        color = COLOR_POS if p >= 60 else (COLOR_NEG if p <= 15 else COLOR_ZERO)
        lines.append(
            f'<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.8rem;">'
            f'{label} &nbsp;→&nbsp; next {HORIZON_LABEL[horizon]} in {tlabel} : '
            f'<b style="color:{color};">{p:.1f}%</b> '
            f'<span style="opacity:0.55;">(n={n:,})</span></div>'
        )
    st.markdown("".join(lines), unsafe_allow_html=True)

    st.markdown("---")
    st.markdown('<h3 style="font-size:1.1rem; font-weight:700;">Pattern search</h3>',
                unsafe_allow_html=True)
    st.caption("Pick a bucket per horizon (Any = ignore). Finds every historical sample matching "
               "the pattern and shows what the forward range did.")

    pat_cols = st.columns(len(TRAIL_HORIZONS))
    pattern: dict[int, int] = {}
    for i, h in enumerate(TRAIL_HORIZONS):
        with pat_cols[i]:
            choice = st.selectbox(
                HORIZON_LABEL[h], ["Any"] + [f"Q{b}" for b in range(1, n_buckets + 1)],
                key=f"rg_pat_{h}",
            )
            if choice != "Any":
                pattern[h] = int(choice[1:])

    mask = pl.lit(True)
    for h, b in pattern.items():
        mask = mask & (pl.col(f"b_tr{h}_pct") == b)
    matches = df.filter(mask).drop_nulls([f"b_fw{horizon}_pct"])

    if not pattern:
        st.info("Select at least one horizon bucket to search.")
    elif matches.is_empty():
        st.warning("No historical matches for that pattern.")
    else:
        fwd_b = matches[f"b_fw{horizon}_pct"].to_numpy()
        fwd_pct = matches[f"fw{horizon}_pct"].to_numpy()
        base_b = df.drop_nulls([f"b_fw{horizon}_pct"])[f"b_fw{horizon}_pct"].to_numpy()

        st.markdown(f"**{len(matches):,} matches** "
                    f"({len(matches) / df.height * 100:.2f}% of all samples)")
        dist = pd.DataFrame({
            "forward bucket": [f"Q{b}" for b in range(1, n_buckets + 1)],
            "this pattern": [float((fwd_b == b).mean()) * 100 for b in range(1, n_buckets + 1)],
            "all samples": [float((base_b == b).mean()) * 100 for b in range(1, n_buckets + 1)],
        })
        dist["lift"] = dist["this pattern"] - dist["all samples"]
        st.dataframe(
            dist.style.format({"this pattern": "{:.1f}%", "all samples": "{:.1f}%",
                               "lift": "{:+.1f}pp"}),
            use_container_width=True, hide_index=True,
        )

        fig = go.Figure()
        fig.add_histogram(x=fwd_pct, nbinsx=60, name="matches",
                          marker_color=COLOR_BS, histnorm="probability density")
        fig.add_histogram(x=df.drop_nulls([f"fw{horizon}_pct"])[f"fw{horizon}_pct"].to_numpy(),
                          nbinsx=60, name="all samples",
                          marker_color=COLOR_ZERO, opacity=0.45,
                          histnorm="probability density")
        fig.update_layout(barmode="overlay", height=320, margin=dict(t=10, b=10),
                          xaxis_title=f"forward {HORIZON_LABEL[horizon]} range (%)",
                          legend=dict(orientation="h", y=1.1))
        st.plotly_chart(fig, use_container_width=True)

        q = np.percentile(fwd_pct, [10, 25, 50, 75, 90])
        qa = np.percentile(df.drop_nulls([f"fw{horizon}_pct"])[f"fw{horizon}_pct"].to_numpy(),
                           [10, 25, 50, 75, 90])
        st.markdown(
            '<div style="font-family: ui-monospace, Consolas, monospace; font-size:0.78rem;">'
            + "<br>".join(
                f"{lbl:>4s}  pattern {a:7.3f}%   all {b:7.3f}%   ratio {a/b:5.2f}x"
                for lbl, a, b in zip(["p10", "p25", "p50", "p75", "p90"], q, qa)
            )
            + "</div>",
            unsafe_allow_html=True,
        )


if __name__ == "__main__":
    main()
