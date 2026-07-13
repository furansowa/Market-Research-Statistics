"""Feature registry — the extensibility core (SPEC.md section 8, Phase 2 spec §3.6).

Each FeatureSpec declares metadata used to drive the dashboard's dynamic filters
and result table automatically — dtype/basis/filter_kind (Phase 1), plus
timing/show_in_table/formatter/decimals/color_kind (Phase 2).

Features fall into two groups:

- "base" features are aggregated directly from the `minutes` table in
  sessions.py (e.g. rth_open, rth_high_time) — they have no `compute` here,
  they exist purely so the registry has one entry per displayable/filterable
  column.
- "derived" features have a `compute(df) -> pl.Expr` that sessions.py applies
  with `with_columns` in registry order, so each derived feature can depend on
  any base feature or any derived feature defined earlier in the list.

To add a new parameter: append one FeatureSpec to REGISTRY (with a `compute`
if it's derived) and re-run the ETL. It becomes filterable/displayable with no
further plumbing changes.

`timing` separates what is knowable before the session opens ("pre_open") from
what is only knowable at/after the close ("outcome") — see Phase 2 spec §3.6.
This is intrinsic to the feature itself, at offset 0. A feature viewed at a
*negative* offset (a prior, already-closed session) is always safe regardless
of its own timing — that's a query-time concern (filters.py / dashboard.py),
not something encoded here.

`show_in_table`, `formatter`, `decimals`, and `color_kind`/`color_map` are
Phase 2 additions beyond what the spec's §3.6 literally asked for
(`timing` + `show_in_table`) — they exist because driving the results grid
fully from the registry needs per-column display formatting (e.g. a raw
timestamp string -> "11:42") and coloring, not just column selection.
`formatter` operates on the *raw* value as returned by sqlite3 (str/int/float/
None) — before any pandas conversion happens in dashboard.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Literal, Optional

import polars as pl

Dtype = Literal["numeric", "categorical", "boolean", "time"]
Basis = Literal["RTH", "ETH", "context"]
FilterKind = Literal["range", "select", "bool", None]
Timing = Literal["pre_open", "outcome"]
ColorKind = Literal["pts", "enum", None]

# Shared color palette — single source of truth for both the results table
# (Styler) and the chart info line in dashboard.py.
COLOR_POS = "#8BC98F"    # soft green — positive points
COLOR_NEG = "#E38B8B"    # soft red — negative points
COLOR_ZERO = "#9AA0A6"   # grey — zero/neutral
COLOR_SB = "#F2C464"     # yellow/orange — SB
COLOR_BS = "#9AC7E8"     # pastel blue — BS

_BS_SB_COLOR_MAP = {"SB": COLOR_SB, "BS": COLOR_BS}
_WEEKDAY_ORDER = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    dtype: Dtype
    basis: Basis
    filter_kind: FilterKind
    label: str
    compute: Optional[Callable[[pl.DataFrame], pl.Expr]] = None
    timing: Timing = "outcome"
    show_in_table: bool = False
    formatter: Optional[Callable[[Any], Any]] = None
    decimals: Optional[int] = None
    color_kind: ColorKind = None
    color_map: Optional[dict] = field(default=None)
    table_label: Optional[str] = None  # short header for the results grid; falls back to `label`
    value_order: Optional[list] = None  # explicit option order for "select" filters; falls back to alphabetical
    window: Optional[str] = None  # None = the whole-RTH "Day Session" tab; else the window tab this belongs to
    shared_across_tabs: bool = False  # True = appears in every tab regardless of `window` (date, weekday, ...)

    @property
    def display_label(self) -> str:
        return self.table_label or self.label


def _dir3(pts_col: str) -> pl.Expr:
    """up / down / flat classification from a points-difference column."""
    return (
        pl.when(pl.col(pts_col) > 0).then(pl.lit("up"))
        .when(pl.col(pts_col) < 0).then(pl.lit("down"))
        .otherwise(pl.lit("flat"))
    )


def _bs_sb_expr(high_ts_col: str, low_ts_col: str, nullable: bool = False) -> pl.Expr:
    expr = (
        pl.when(pl.col(high_ts_col) < pl.col(low_ts_col)).then(pl.lit("SB"))
        .when(pl.col(low_ts_col) < pl.col(high_ts_col)).then(pl.lit("BS"))
        .otherwise(pl.lit("TIE"))
    )
    if nullable:
        # Window bundles can have all-null high/low times on a zero-bar window
        # (half-day truncation) — "TIE" would be wrong there, it means unknown.
        expr = (
            pl.when(pl.col(high_ts_col).is_null() | pl.col(low_ts_col).is_null())
            .then(pl.lit(None, dtype=pl.Utf8))
            .otherwise(expr)
        )
    return expr


def _short_weekday(v):
    return v[:3] if isinstance(v, str) else v


def _hhmm(v):
    """'YYYY-MM-DD HH:MM:SS' (raw sqlite TEXT) -> 'HH:MM'."""
    return v[11:16] if isinstance(v, str) and len(v) >= 16 else v


REGISTRY: list[FeatureSpec] = [
    # ---- identity / calendar ----
    FeatureSpec(
        "instrument", "categorical", "context", "select", "Instrument",
        timing="pre_open", show_in_table=False,
    ),
    FeatureSpec(
        "date", "categorical", "context", None, "Date",
        timing="pre_open", show_in_table=True, shared_across_tabs=True,
    ),
    FeatureSpec(
        "weekday", "categorical", "context", "select", "Weekday",
        timing="pre_open", show_in_table=True, formatter=_short_weekday, table_label="Day",
        value_order=_WEEKDAY_ORDER, shared_across_tabs=True,
    ),
    FeatureSpec(
        "is_half_day", "boolean", "context", "bool", "Half day",
        timing="pre_open", show_in_table=True, table_label="Half Day", shared_across_tabs=True,
    ),
    FeatureSpec(
        "seq", "numeric", "context", None, "Seq",
        compute=lambda df: pl.int_range(1, pl.len() + 1),
        timing="pre_open", show_in_table=False,
    ),

    # ---- RTH base ----
    FeatureSpec(
        "rth_open", "numeric", "RTH", "range", "RTH Open",
        timing="pre_open", show_in_table=True, decimals=1, table_label="Open",
    ),
    FeatureSpec(
        "rth_high", "numeric", "RTH", "range", "RTH High",
        timing="outcome", show_in_table=True, decimals=1, table_label="High",
    ),
    FeatureSpec(
        "rth_low", "numeric", "RTH", "range", "RTH Low",
        timing="outcome", show_in_table=True, decimals=1, table_label="Low",
    ),
    FeatureSpec(
        "rth_close", "numeric", "RTH", "range", "RTH Close",
        timing="outcome", show_in_table=True, decimals=1, table_label="Close",
    ),
    FeatureSpec(
        "bs_sb", "categorical", "RTH", "select", "BS/SB",
        compute=lambda df: _bs_sb_expr("rth_high_time", "rth_low_time"),
        timing="outcome", show_in_table=True, color_kind="enum", color_map=_BS_SB_COLOR_MAP,
    ),
    FeatureSpec(
        "rth_high_time", "time", "RTH", None, "RTH High Time",
        timing="outcome", show_in_table=True, formatter=_hhmm, table_label="High Time",
    ),
    FeatureSpec(
        "rth_low_time", "time", "RTH", None, "RTH Low Time",
        timing="outcome", show_in_table=True, formatter=_hhmm, table_label="Low Time",
    ),
    FeatureSpec(
        "rth_high_minute", "numeric", "RTH", "range", "RTH High Bar",
        timing="outcome", show_in_table=True, table_label="High Bar",
    ),
    FeatureSpec(
        "rth_low_minute", "numeric", "RTH", "range", "RTH Low Bar",
        timing="outcome", show_in_table=True, table_label="Low Bar",
    ),
    FeatureSpec(
        "rth_high_bucket", "categorical", "RTH", "select", "RTH High (30-min bucket)",
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "rth_low_bucket", "categorical", "RTH", "select", "RTH Low (30-min bucket)",
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "rth_range", "numeric", "RTH", "range", "RTH Range",
        compute=lambda df: pl.col("rth_high") - pl.col("rth_low"),
        timing="outcome", show_in_table=True, decimals=1, table_label="Range",
    ),
    FeatureSpec(
        "rth_range_ma20", "numeric", "RTH", "range", "Range MA20 (prior 20 sessions)",
        # Trailing 20-session average, strictly *before* today (shift(1) then
        # a 20-window mean) — deliberately excludes today's own range so this
        # is knowable pre-open, unlike rth_range/range_vs_ma20_pts below.
        compute=lambda df: pl.col("rth_range").shift(1).rolling_mean(window_size=20, min_samples=20),
        timing="pre_open", show_in_table=True, decimals=0, table_label="Range MA20",
    ),
    FeatureSpec(
        "range_vs_ma20_pts", "numeric", "RTH", "range", "Range vs MA20 (pts)",
        compute=lambda df: pl.col("rth_range") - pl.col("rth_range_ma20"),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Range vs MA20",
    ),
    FeatureSpec(
        "range_vs_ma20_dir", "categorical", "RTH", "select", "Range vs MA20 direction",
        compute=lambda df: _dir3("range_vs_ma20_pts"),
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "abs_range_diff_pts", "numeric", "RTH", "range", "Abs. Range Diff (vs prev range, pts)",
        compute=lambda df: pl.col("rth_range") - pl.col("rth_range").shift(1),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Abs. Range Diff",
    ),
    FeatureSpec(
        "abs_range_diff_dir", "categorical", "RTH", "select", "Abs. Range Diff direction",
        compute=lambda df: _dir3("abs_range_diff_pts"),
        timing="outcome", show_in_table=False,
    ),

    # ---- RTH close-based twins (Phase 2 spec §3.4) ----
    # The gyrations detector runs on closes; these reconcile the sessions table
    # with the leg table. Extreme-based columns above remain canonical for
    # display (rth_high/rth_low/rth_range/bs_sb); these are additional filter
    # surface, not shown in the default table (show_in_table=False).
    FeatureSpec(
        "rth_high_close", "numeric", "RTH", "range", "RTH High (close)",
        timing="outcome", decimals=1,
    ),
    FeatureSpec(
        "rth_low_close", "numeric", "RTH", "range", "RTH Low (close)",
        timing="outcome", decimals=1,
    ),
    FeatureSpec(
        "rth_high_close_ts", "time", "RTH", None, "RTH High Time (close)",
        timing="outcome", formatter=_hhmm,
    ),
    FeatureSpec(
        "rth_low_close_ts", "time", "RTH", None, "RTH Low Time (close)",
        timing="outcome", formatter=_hhmm,
    ),
    FeatureSpec(
        "rth_high_close_minute", "numeric", "RTH", "range", "RTH High Bar (close)",
        timing="outcome",
    ),
    FeatureSpec(
        "rth_low_close_minute", "numeric", "RTH", "range", "RTH Low Bar (close)",
        timing="outcome",
    ),
    FeatureSpec(
        "rth_high_close_bucket", "categorical", "RTH", "select", "RTH High Bucket (close)",
        timing="outcome",
    ),
    FeatureSpec(
        "rth_low_close_bucket", "categorical", "RTH", "select", "RTH Low Bucket (close)",
        timing="outcome",
    ),
    FeatureSpec(
        "rth_range_close", "numeric", "RTH", "range", "RTH Range (close)",
        compute=lambda df: pl.col("rth_high_close") - pl.col("rth_low_close"),
        timing="outcome", decimals=1,
    ),
    FeatureSpec(
        "bs_sb_close", "categorical", "RTH", "select", "BS/SB (close)",
        compute=lambda df: _bs_sb_expr("rth_high_close_ts", "rth_low_close_ts"),
        timing="outcome", color_kind="enum", color_map=_BS_SB_COLOR_MAP,
    ),

    # ---- ETH / full-day base ----
    FeatureSpec(
        "eth_open", "numeric", "ETH", "range", "Full-day Open",
        timing="pre_open",
    ),
    FeatureSpec(
        "eth_high", "numeric", "ETH", "range", "Full-day High",
        timing="outcome",
    ),
    FeatureSpec(
        "eth_low", "numeric", "ETH", "range", "Full-day Low",
        timing="outcome",
    ),
    FeatureSpec(
        "eth_close", "numeric", "ETH", "range", "Full-day Close",
        timing="outcome",
    ),
    FeatureSpec(
        "eth_range", "numeric", "ETH", "range", "Full-day Range",
        compute=lambda df: pl.col("eth_high") - pl.col("eth_low"),
        timing="outcome",
    ),

    # ---- gap / close-difference derived ----
    FeatureSpec(
        "gap_pts", "numeric", "RTH", "range", "Gap (pts)",
        compute=lambda df: pl.col("rth_open") - pl.col("rth_close").shift(1),
        timing="pre_open", show_in_table=True, decimals=1, color_kind="pts", table_label="Gap",
    ),
    FeatureSpec(
        "gap_dir", "categorical", "RTH", "select", "Gap direction",
        compute=lambda df: _dir3("gap_pts"),
        timing="pre_open", show_in_table=False,
    ),
    FeatureSpec(
        "rel_close_pts", "numeric", "RTH", "range", "Rel. close (close-open, pts)",
        compute=lambda df: pl.col("rth_close") - pl.col("rth_open"),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Rel Close",
    ),
    FeatureSpec(
        "rel_close_dir", "categorical", "RTH", "select", "Rel. close direction",
        compute=lambda df: _dir3("rel_close_pts"),
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "abs_close_pts", "numeric", "RTH", "range", "Abs. close (vs prev close, pts)",
        compute=lambda df: pl.col("rth_close") - pl.col("rth_close").shift(1),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Abs Close",
    ),
    FeatureSpec(
        "abs_close_dir", "categorical", "RTH", "select", "Abs. close direction",
        compute=lambda df: _dir3("abs_close_pts"),
        timing="outcome", show_in_table=False,
    ),

    # ---- context / cross-session (previous-session shifts) ----
    # All pre_open: by the time today's session opens, the previous session is
    # fully closed history, regardless of the underlying column's own timing.
    FeatureSpec(
        "prev_rel_close_dir", "categorical", "context", "select", "Prev. rel. close direction",
        compute=lambda df: pl.col("rel_close_dir").shift(1),
        timing="pre_open",
    ),
    FeatureSpec(
        "prev_abs_close_dir", "categorical", "context", "select", "Prev. abs. close direction",
        compute=lambda df: pl.col("abs_close_dir").shift(1),
        timing="pre_open",
    ),
    FeatureSpec(
        "prev_bs_sb", "categorical", "context", "select", "Prev. BS/SB",
        compute=lambda df: pl.col("bs_sb").shift(1),
        timing="pre_open",
    ),
    FeatureSpec(
        "prev_range_vs_ma20_dir", "categorical", "context", "select", "Prev. RangeMA20 difference",
        compute=lambda df: pl.col("range_vs_ma20_dir").shift(1),
        timing="pre_open",
    ),
    FeatureSpec(
        "prev_range_vs_ma20_pts", "numeric", "context", "range", "Prev. RangeMA20 diff (pts)",
        compute=lambda df: pl.col("range_vs_ma20_pts").shift(1),
        timing="pre_open", decimals=1,
    ),
    FeatureSpec(
        "prev_abs_range_diff_dir", "categorical", "context", "select", "Prev. Abs. Range difference",
        compute=lambda df: pl.col("abs_range_diff_dir").shift(1),
        timing="pre_open",
    ),
    FeatureSpec(
        "prev_abs_range_diff_pts", "numeric", "context", "range", "Prev. Abs. Range diff (pts)",
        compute=lambda df: pl.col("abs_range_diff_pts").shift(1),
        timing="pre_open", decimals=1,
    ),
    FeatureSpec(
        "gap_prevclose_combo", "categorical", "context", "select", "Gap x prev-close combo",
        compute=lambda df: pl.concat_str(
            [pl.lit("gap"), pl.col("gap_dir"), pl.lit("_prevclose"), pl.col("prev_abs_close_dir")]
        ),
        timing="pre_open",
    ),
    FeatureSpec(
        "open_vs_prev_range", "categorical", "context", "select", "Open vs prev RTH range",
        compute=lambda df: (
            pl.when(pl.col("rth_open") < pl.col("rth_low").shift(1)).then(pl.lit("below"))
            .when(pl.col("rth_open") > pl.col("rth_high").shift(1)).then(pl.lit("above"))
            .otherwise(pl.lit("inside"))
        ),
        timing="pre_open",
    ),
    FeatureSpec(
        "open_vs_prev_range_pct", "numeric", "context", "range",
        "Open position in prev range (0=low,1=high)",
        compute=lambda df: (
            (pl.col("rth_open") - pl.col("rth_low").shift(1))
            / (pl.col("rth_high").shift(1) - pl.col("rth_low").shift(1))
        ),
        timing="pre_open", decimals=2,
    ),

    # ---- reserved ----
    FeatureSpec(
        "template", "categorical", "context", "select", "Session template",
        timing="outcome", show_in_table=False,
    ),
]

# ---- per-window bundles (Phase 2 spec §3.3) ----
# Generated in a loop over configured windows, not hand-listed, so the registry
# can't drift from config.toml's [windows] section (spec's explicit instruction).
# Mirrors the RTH block above 1:1, scoped to each window's own bars instead of
# the whole session — one dashboard tab per window (see dashboard.py's
# WINDOW_TABS). Session-level facts shared by every tab (date/weekday/
# is_half_day) are declared once above with `shared_across_tabs=True` instead
# of being duplicated here.
WINDOW_NAMES = ["first_30m", "first_60m", "first_90m", "hour_10_11"]


def _window_bundle_specs(name: str) -> list[FeatureSpec]:
    p = f"win_{name}_"

    def col(c: str) -> str:
        return p + c

    return [
        FeatureSpec(
            col("open"), "numeric", "RTH", "range", f"{name} Open",
            timing="outcome", show_in_table=True, decimals=1, table_label="Open", window=name,
        ),
        FeatureSpec(
            col("high"), "numeric", "RTH", "range", f"{name} High",
            timing="outcome", show_in_table=True, decimals=1, table_label="High", window=name,
        ),
        FeatureSpec(
            col("low"), "numeric", "RTH", "range", f"{name} Low",
            timing="outcome", show_in_table=True, decimals=1, table_label="Low", window=name,
        ),
        FeatureSpec(
            col("close"), "numeric", "RTH", "range", f"{name} Close",
            timing="outcome", show_in_table=True, decimals=1, table_label="Close", window=name,
        ),
        FeatureSpec(
            col("bs_sb"), "categorical", "RTH", "select", f"{name} BS/SB",
            compute=lambda df, h=col("high_time"), l=col("low_time"): _bs_sb_expr(h, l, nullable=True),
            timing="outcome", show_in_table=True, color_kind="enum", color_map=_BS_SB_COLOR_MAP, window=name,
            table_label="BS/SB",
        ),
        FeatureSpec(
            col("high_time"), "time", "RTH", None, f"{name} High Time",
            timing="outcome", show_in_table=True, formatter=_hhmm, table_label="High Time", window=name,
        ),
        FeatureSpec(
            col("low_time"), "time", "RTH", None, f"{name} Low Time",
            timing="outcome", show_in_table=True, formatter=_hhmm, table_label="Low Time", window=name,
        ),
        FeatureSpec(
            col("high_minute"), "numeric", "RTH", "range", f"{name} High Bar",
            timing="outcome", show_in_table=True, table_label="High Bar", window=name,
        ),
        FeatureSpec(
            col("low_minute"), "numeric", "RTH", "range", f"{name} Low Bar",
            timing="outcome", show_in_table=True, table_label="Low Bar", window=name,
        ),
        FeatureSpec(
            col("high_bucket"), "categorical", "RTH", "select", f"{name} High (30-min bucket)",
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("low_bucket"), "categorical", "RTH", "select", f"{name} Low (30-min bucket)",
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("range"), "numeric", "RTH", "range", f"{name} Range",
            compute=lambda df, h=col("high"), l=col("low"): pl.col(h) - pl.col(l),
            timing="outcome", show_in_table=True, decimals=1, table_label="Range", window=name,
        ),
        FeatureSpec(
            col("range_ma20"), "numeric", "RTH", "range", f"{name} Range MA20 (prior 20 sessions)",
            compute=lambda df, r=col("range"): pl.col(r).shift(1).rolling_mean(window_size=20, min_samples=20),
            timing="outcome", show_in_table=True, decimals=0, table_label="Range MA20", window=name,
        ),
        FeatureSpec(
            col("range_vs_ma20_pts"), "numeric", "RTH", "range", f"{name} Range vs MA20 (pts)",
            compute=lambda df, r=col("range"), m=col("range_ma20"): pl.col(r) - pl.col(m),
            timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Range vs MA20",
            window=name,
        ),
        FeatureSpec(
            col("range_vs_ma20_dir"), "categorical", "RTH", "select", f"{name} Range vs MA20 direction",
            compute=lambda df, c=col("range_vs_ma20_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("abs_range_diff_pts"), "numeric", "RTH", "range",
            f"{name} Abs. Range Diff (vs prev range, pts)",
            compute=lambda df, r=col("range"): pl.col(r) - pl.col(r).shift(1),
            timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Abs. Range Diff",
            window=name,
        ),
        FeatureSpec(
            col("abs_range_diff_dir"), "categorical", "RTH", "select", f"{name} Abs. Range Diff direction",
            compute=lambda df, c=col("abs_range_diff_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("gap_pts"), "numeric", "RTH", "range", f"{name} Gap (pts)",
            compute=lambda df, o=col("open"), c=col("close"): pl.col(o) - pl.col(c).shift(1),
            timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Gap", window=name,
        ),
        FeatureSpec(
            col("gap_dir"), "categorical", "RTH", "select", f"{name} Gap direction",
            compute=lambda df, c=col("gap_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("rel_close_pts"), "numeric", "RTH", "range", f"{name} Rel. close (close-open, pts)",
            compute=lambda df, c=col("close"), o=col("open"): pl.col(c) - pl.col(o),
            timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Rel Close",
            window=name,
        ),
        FeatureSpec(
            col("rel_close_dir"), "categorical", "RTH", "select", f"{name} Rel. close direction",
            compute=lambda df, c=col("rel_close_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("abs_close_pts"), "numeric", "RTH", "range", f"{name} Abs. close (vs prev close, pts)",
            compute=lambda df, c=col("close"): pl.col(c) - pl.col(c).shift(1),
            timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="Abs Close",
            window=name,
        ),
        FeatureSpec(
            col("abs_close_dir"), "categorical", "RTH", "select", f"{name} Abs. close direction",
            compute=lambda df, c=col("abs_close_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        # ---- context: previous-session (same window) shifts ----
        FeatureSpec(
            f"prev_{p}bs_sb", "categorical", "context", "select", f"Prev. {name} BS/SB",
            compute=lambda df, c=col("bs_sb"): pl.col(c).shift(1),
            timing="pre_open", window=name,
        ),
        FeatureSpec(
            f"prev_{p}range_vs_ma20_dir", "categorical", "context", "select",
            f"Prev. {name} RangeMA20 difference",
            compute=lambda df, c=col("range_vs_ma20_dir"): pl.col(c).shift(1),
            timing="pre_open", window=name,
        ),
        FeatureSpec(
            f"prev_{p}range_vs_ma20_pts", "numeric", "context", "range", f"Prev. {name} RangeMA20 diff (pts)",
            compute=lambda df, c=col("range_vs_ma20_pts"): pl.col(c).shift(1),
            timing="pre_open", decimals=1, window=name,
        ),
        FeatureSpec(
            f"prev_{p}abs_range_diff_dir", "categorical", "context", "select",
            f"Prev. {name} Abs. Range difference",
            compute=lambda df, c=col("abs_range_diff_dir"): pl.col(c).shift(1),
            timing="pre_open", window=name,
        ),
        FeatureSpec(
            f"prev_{p}abs_range_diff_pts", "numeric", "context", "range", f"Prev. {name} Abs. Range diff (pts)",
            compute=lambda df, c=col("abs_range_diff_pts"): pl.col(c).shift(1),
            timing="pre_open", decimals=1, window=name,
        ),
    ]


for _name in WINDOW_NAMES:
    REGISTRY.extend(_window_bundle_specs(_name))


REGISTRY_BY_NAME: dict[str, FeatureSpec] = {f.name: f for f in REGISTRY}


def base_features() -> list[FeatureSpec]:
    return [f for f in REGISTRY if f.compute is None]


def derived_features() -> list[FeatureSpec]:
    return [f for f in REGISTRY if f.compute is not None]


def filterable_features(window: Optional[str] = None) -> list[FeatureSpec]:
    return [f for f in REGISTRY if f.filter_kind is not None and (f.shared_across_tabs or f.window == window)]


def table_features(window: Optional[str] = None) -> list[FeatureSpec]:
    """Features shown in the results table for a given tab, in registry order."""
    return [f for f in REGISTRY if f.show_in_table and (f.shared_across_tabs or f.window == window)]
