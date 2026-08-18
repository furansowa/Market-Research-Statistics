"""Multi-timeframe RANGE state -- pure computation, no SQLite/Streamlit deps.

At each sample point t the state is the trailing range over several horizons
(1m .. 1 day), in points and as % of price, plus the forward range over
several horizons. A "bucket" is that value's rank inside a CAUSAL trailing
reference distribution of the same measure (rolling quantiles over the
previous `ref_window` samples) -- never the full history, which would be
lookahead.

Design note (matters, learned the hard way 2026-08-12): the target must be
the forward range's LEVEL against a trailing reference, NOT "forward range >
current range". The latter is ~74% predictable in a pure random walk purely
because a noisy range estimate mean-reverts, so it measures the estimator,
not the market.

Second design note: the forward range is bucketed against the TRAILING
reference distribution (the `ref_col` argument), not against a rolling window
of forward values -- the latter lets a couple of samples of future data touch
the quantile estimate. Same question, zero lookahead.

Trailing windows are real clock time: at 10:00 the "last 4 hours" genuinely
includes 06:00-10:00 even if that was outside the cash session. Scope
filtering selects which SAMPLE POINTS are kept, it does not re-time the
windows.
"""

from __future__ import annotations

import polars as pl

TRAIL_HORIZONS = [1, 5, 10, 15, 30, 60, 240, 1440]
FWD_HORIZONS = [30, 60, 240]

# name -> the quantile cut points that define its buckets
BUCKET_SCHEMES: dict[str, list[float]] = {
    "Quintiles (20/40/60/80)": [0.2, 0.4, 0.6, 0.8],
    "Quartiles (25/50/75)": [0.25, 0.5, 0.75],
    "Low/Mid/High (20/80)": [0.2, 0.8],
    "Deciles-ends (10/25/50/75/90)": [0.1, 0.25, 0.5, 0.75, 0.9],
}


def compute_range_state(
    minutes: pl.DataFrame,
    trail_horizons: list[int] | None = None,
    fwd_horizons: list[int] | None = None,
) -> pl.DataFrame:
    """`minutes` needs ts/open/high/low/close, 1-minute, sorted, gap-free in
    the sense of being one continuous series per instrument. Returns the same
    frame plus tr{h}_pts / tr{h}_pct and fw{h}_pts / fw{h}_pct columns."""
    trail_horizons = trail_horizons or TRAIL_HORIZONS
    fwd_horizons = fwd_horizons or FWD_HORIZONS

    exprs = []
    for h in trail_horizons:
        rng = (
            pl.col("high").rolling_max(window_size=h, min_samples=h)
            - pl.col("low").rolling_min(window_size=h, min_samples=h)
        )
        exprs.append(rng.alias(f"tr{h}_pts"))
        exprs.append((rng / pl.col("close") * 100).alias(f"tr{h}_pct"))

    for h in fwd_horizons:
        rng = (
            pl.col("high").rolling_max(window_size=h, min_samples=h)
            - pl.col("low").rolling_min(window_size=h, min_samples=h)
        )
        exprs.append(rng.shift(-h).alias(f"fw{h}_pts"))
        exprs.append((rng / pl.col("close") * 100).shift(-h).alias(f"fw{h}_pct"))

    return minutes.with_columns(exprs)


def add_causal_buckets(
    df: pl.DataFrame,
    value_cols: list[str],
    ref_window: int,
    cuts: list[float],
    ref_col_map: dict[str, str] | None = None,
) -> pl.DataFrame:
    """Adds `b_<col>` (1..len(cuts)+1) for each of `value_cols`.

    The reference distribution is a rolling quantile over the previous
    `ref_window` samples of `ref_col_map.get(col, col)` -- so a forward column
    can be (and should be) ranked against its TRAILING counterpart rather than
    against future values of itself.
    """
    ref_col_map = ref_col_map or {}
    out = df
    for col in value_cols:
        ref = ref_col_map.get(col, col)
        q_names = []
        q_exprs = []
        for q in cuts:
            qn = f"__q{int(round(q * 1000))}_{ref}"
            q_names.append(qn)
            if qn not in out.columns:
                q_exprs.append(
                    pl.col(ref)
                    .rolling_quantile(quantile=q, window_size=ref_window,
                                      min_samples=max(ref_window // 3, 20))
                    .alias(qn)
                )
        if q_exprs:
            out = out.with_columns(q_exprs)

        bucket = pl.lit(1, dtype=pl.Int8)
        for qn in q_names:
            bucket = bucket + (pl.col(col) > pl.col(qn)).cast(pl.Int8)
        out = out.with_columns(
            pl.when(pl.col(col).is_null() | pl.col(q_names[0]).is_null())
            .then(None)
            .otherwise(bucket)
            .alias(f"b_{col}")
        )

    drop = [c for c in out.columns if c.startswith("__q")]
    return out.drop(drop)


def transition_matrix(cur_bucket, fwd_bucket, n_buckets: int) -> list[list]:
    """[from_bucket][to_bucket] -> (count, pct_of_row). Rows are 1..n_buckets."""
    rows = []
    for a in range(1, n_buckets + 1):
        mask = cur_bucket == a
        n = int(mask.sum())
        row = []
        for b in range(1, n_buckets + 1):
            c = int((fwd_bucket[mask] == b).sum()) if n else 0
            row.append((c, (c / n * 100) if n else 0.0))
        rows.append((n, row))
    return rows
