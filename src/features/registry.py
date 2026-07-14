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
# Highest/lowest-of-N flags reuse the BS/SB blue/orange pair (1 = highest =
# blue, -1 = lowest = orange) instead of the pts green/red, per user request.
_HILO_COLOR_MAP = {1: COLOR_BS, -1: COLOR_SB, 0: COLOR_ZERO}
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


def _hilo_flag_expr(col: str, n: int) -> pl.Expr:
    """1 if this session's value is the highest of the trailing n sessions
    (itself included), -1 if the lowest, 0 if in between. Null until n
    sessions of history exist (min_samples=n — a partial window can't
    answer "highest of n"). A fully flat window (max == min == value)
    resolves to 1, since the highest check runs first; negligible for
    continuous price data."""
    roll_max = pl.col(col).rolling_max(window_size=n, min_samples=n)
    roll_min = pl.col(col).rolling_min(window_size=n, min_samples=n)
    flag = (
        pl.when(pl.col(col) == roll_max).then(1)
        .when(pl.col(col) == roll_min).then(-1)
        .otherwise(0)
    )
    return pl.when(roll_max.is_null()).then(None).otherwise(flag)


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
    # filter_kind=None on the four price-level fields below: filtering on an
    # absolute price is close to meaningless across 17 years of history (DOW
    # ~7000 in 2009 vs ~52000 in 2026) — kept as table columns only, removed
    # from the filter UI (2026-07-14).
    FeatureSpec(
        "rth_open", "numeric", "RTH", None, "RTH Open",
        timing="pre_open", show_in_table=True, decimals=1, table_label="Open",
    ),
    FeatureSpec(
        "rth_high", "numeric", "RTH", None, "RTH High",
        timing="outcome", show_in_table=True, decimals=1, table_label="High",
    ),
    FeatureSpec(
        "rth_low", "numeric", "RTH", None, "RTH Low",
        timing="outcome", show_in_table=True, decimals=1, table_label="Low",
    ),
    FeatureSpec(
        "rth_close", "numeric", "RTH", None, "RTH Close",
        timing="outcome", show_in_table=True, decimals=1, table_label="Close",
    ),
    FeatureSpec(
        "bs_sb", "categorical", "RTH", "select", "BS/SB",
        compute=lambda df: _bs_sb_expr("rth_high_time", "rth_low_time"),
        timing="outcome", show_in_table=True, color_kind="enum", color_map=_BS_SB_COLOR_MAP,
    ),
    FeatureSpec(
        "rth_high_time", "time", "RTH", None, "RTH High Time",
        timing="outcome", show_in_table=True, formatter=_hhmm, table_label="Htime",
    ),
    FeatureSpec(
        "rth_low_time", "time", "RTH", None, "RTH Low Time",
        timing="outcome", show_in_table=True, formatter=_hhmm, table_label="Ltime",
    ),
    FeatureSpec(
        "rth_high_minute", "numeric", "RTH", "range", "RTH High Bar",
        timing="outcome", show_in_table=True, table_label="Hbar",
    ),
    FeatureSpec(
        "rth_low_minute", "numeric", "RTH", "range", "RTH Low Bar",
        timing="outcome", show_in_table=True, table_label="Lbar",
    ),
    FeatureSpec(
        "rth_high_bucket", "categorical", "RTH", "select", "RTH High (30-min bucket)",
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "rth_low_bucket", "categorical", "RTH", "select", "RTH Low (30-min bucket)",
        timing="outcome", show_in_table=False,
    ),
    # Position of the high/low bar within a continuous, per-instrument count of
    # RTH bars only (never resets per session, only increments on bars that
    # actually exist — so it naturally handles half-days/holidays/gaps without
    # any fixed-per-day assumption). Computed in etl/sessions.py's
    # _build_rth_base. Internal plumbing for h_time_prev_h_time/
    # l_time_prev_l_time below — not shown/filterable on its own.
    FeatureSpec(
        "rth_high_bar_seq", "numeric", "RTH", None, "RTH High Bar (continuous seq)",
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "rth_low_bar_seq", "numeric", "RTH", None, "RTH Low Bar (continuous seq)",
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "rth_range", "numeric", "RTH", "range", "RTH Range",
        compute=lambda df: pl.col("rth_high") - pl.col("rth_low"),
        timing="outcome", show_in_table=True, decimals=1, table_label="Range",
    ),
    FeatureSpec(
        "rth_range_ma20", "numeric", "RTH", None, "Range MA20 (prior 20 sessions)",
        # Trailing 20-session average, strictly *before* today (shift(1) then
        # a 20-window mean) — deliberately excludes today's own range so this
        # is knowable pre-open, unlike rth_range/range_vs_ma20_pts below.
        # min_samples=1 (not 20): a session with zero bars in scope (holiday,
        # feed gap) leaves a null in the trailing 20, and rolling_mean already
        # ignores nulls when averaging — the *count* requirement is what
        # matters. Requiring exactly 20 non-null would poison the average for
        # ~20 sessions after every gap; averaging over whatever's actually
        # present is what was asked for instead.
        compute=lambda df: pl.col("rth_range").shift(1).rolling_mean(window_size=20, min_samples=1),
        timing="pre_open", show_in_table=True, decimals=0, table_label="RgeMA20",
    ),
    FeatureSpec(
        "range_vs_ma20_pts", "numeric", "RTH", "range", "Range vs MA20 (pts)",
        compute=lambda df: pl.col("rth_range") - pl.col("rth_range_ma20"),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="RgeVsMA20",
    ),
    FeatureSpec(
        "range_vs_ma20_dir", "categorical", "RTH", "select", "Range vs MA20 direction",
        compute=lambda df: _dir3("range_vs_ma20_pts"),
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "abs_range_diff_pts", "numeric", "RTH", "range", "Abs. Range Diff (vs prev range, pts)",
        compute=lambda df: pl.col("rth_range") - pl.col("rth_range").shift(1),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="AbsRgeDiff",
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
    # filter_kind=None: the dedicated "ETH" filter group was removed from the
    # dashboard (2026-07-14) with no replacement destination, so these are no
    # longer filterable — still computed/available as table columns.
    FeatureSpec(
        "eth_open", "numeric", "ETH", None, "Full-day Open",
        timing="pre_open",
    ),
    FeatureSpec(
        "eth_high", "numeric", "ETH", None, "Full-day High",
        timing="outcome",
    ),
    FeatureSpec(
        "eth_low", "numeric", "ETH", None, "Full-day Low",
        timing="outcome",
    ),
    FeatureSpec(
        "eth_close", "numeric", "ETH", None, "Full-day Close",
        timing="outcome",
    ),
    FeatureSpec(
        "eth_range", "numeric", "ETH", None, "Full-day Range",
        compute=lambda df: pl.col("eth_high") - pl.col("eth_low"),
        timing="outcome",
    ),

    # ---- gap / close-difference derived ----
    FeatureSpec(
        "gap_pts", "numeric", "RTH", None, "Gap (pts)",
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
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="RelClose",
    ),
    FeatureSpec(
        "rel_close_dir", "categorical", "RTH", "select", "Rel. close direction",
        compute=lambda df: _dir3("rel_close_pts"),
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "abs_close_pts", "numeric", "RTH", "range", "Abs. close (vs prev close, pts)",
        compute=lambda df: pl.col("rth_close") - pl.col("rth_close").shift(1),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="AbsClose",
    ),
    FeatureSpec(
        "abs_close_dir", "categorical", "RTH", "select", "Abs. close direction",
        compute=lambda df: _dir3("abs_close_pts"),
        timing="outcome", show_in_table=False,
    ),

    # ---- highest/lowest-of-N flags (1 = highest, -1 = lowest, 0 = in
    # between, over the trailing N sessions including today) ----
    # Open only needs today's own open + history, so it's knowable pre-open
    # like rth_open/gap_pts; High/Low/Close need today's outcome data.
    FeatureSpec(
        "rth_open_loc3", "numeric", "RTH", "select", "Open highest/lowest of last 3",
        compute=lambda df: _hilo_flag_expr("rth_open", 3),
        timing="pre_open", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Oloc3",
    ),
    FeatureSpec(
        "rth_high_loc3", "numeric", "RTH", "select", "High highest/lowest of last 3",
        compute=lambda df: _hilo_flag_expr("rth_high", 3),
        timing="outcome", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Hloc3",
    ),
    FeatureSpec(
        "rth_low_loc3", "numeric", "RTH", "select", "Low highest/lowest of last 3",
        compute=lambda df: _hilo_flag_expr("rth_low", 3),
        timing="outcome", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Lloc3",
    ),
    FeatureSpec(
        "rth_close_loc3", "numeric", "RTH", "select", "Close highest/lowest of last 3",
        compute=lambda df: _hilo_flag_expr("rth_close", 3),
        timing="outcome", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Cloc3",
    ),
    FeatureSpec(
        "rth_open_loc5", "numeric", "RTH", "select", "Open highest/lowest of last 5",
        compute=lambda df: _hilo_flag_expr("rth_open", 5),
        timing="pre_open", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Oloc5",
    ),
    FeatureSpec(
        "rth_high_loc5", "numeric", "RTH", "select", "High highest/lowest of last 5",
        compute=lambda df: _hilo_flag_expr("rth_high", 5),
        timing="outcome", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Hloc5",
    ),
    FeatureSpec(
        "rth_low_loc5", "numeric", "RTH", "select", "Low highest/lowest of last 5",
        compute=lambda df: _hilo_flag_expr("rth_low", 5),
        timing="outcome", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Lloc5",
    ),
    FeatureSpec(
        "rth_close_loc5", "numeric", "RTH", "select", "Close highest/lowest of last 5",
        compute=lambda df: _hilo_flag_expr("rth_close", 5),
        timing="outcome", show_in_table=True, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="Cloc5",
    ),

    # ---- Gyration Legs page: timing-comparison metrics ----
    # filter_kind=None / show_in_table=False on all 5: these are only ever
    # displayed on the separate "Gyration Legs" page (via an explicit `specs`
    # list passed to build_display_table), never on Day Session's default
    # table_features() output, and never reachable from render_filters — this
    # is what keeps them off Day Session structurally, not by convention.
    FeatureSpec(
        "hl_time_diff", "numeric", "RTH", None, "High/Low time diff (min)",
        compute=lambda df: (pl.col("rth_high_minute") - pl.col("rth_low_minute")).abs(),
        timing="outcome", show_in_table=False, decimals=0, table_label="HLtimeDiff",
    ),
    FeatureSpec(
        "hl_time_vs_prev", "numeric", "RTH", None, "HLtimeDiff vs prev session",
        compute=lambda df: (
            pl.when(pl.col("hl_time_diff").shift(1).is_null()).then(pl.lit(None, dtype=pl.Int32))
            .when(pl.col("hl_time_diff") >= pl.col("hl_time_diff").shift(1)).then(1)
            .otherwise(-1)
        ),
        timing="outcome", show_in_table=False, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="HLtimeVsPrevHLtime",
    ),
    FeatureSpec(
        "h_time_prev_h_time", "numeric", "RTH", None, "RTH High bar-seq vs prev session's",
        compute=lambda df: pl.col("rth_high_bar_seq") - pl.col("rth_high_bar_seq").shift(1),
        timing="outcome", show_in_table=False, decimals=0, color_kind="pts", table_label="HtimePrevHtime",
    ),
    FeatureSpec(
        "l_time_prev_l_time", "numeric", "RTH", None, "RTH Low bar-seq vs prev session's",
        compute=lambda df: pl.col("rth_low_bar_seq") - pl.col("rth_low_bar_seq").shift(1),
        timing="outcome", show_in_table=False, decimals=0, color_kind="pts", table_label="LtimePrevLtime",
    ),
    FeatureSpec(
        "ht_vs_lt", "numeric", "RTH", None, "HtimePrevHtime vs LtimePrevLtime",
        compute=lambda df: (
            pl.when(pl.col("h_time_prev_h_time").is_null() | pl.col("l_time_prev_l_time").is_null())
            .then(pl.lit(None, dtype=pl.Int32))
            .when(pl.col("h_time_prev_h_time") >= pl.col("l_time_prev_l_time")).then(1)
            .otherwise(-1)
        ),
        timing="outcome", show_in_table=False, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="HTvsLT",
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
# the whole session. Session-level facts shared regardless of window
# (date/weekday/is_half_day) are declared once above with
# `shared_across_tabs=True` instead of being duplicated here.
#
# Rework (2026-07-14): dropped the per-window dashboard tabs — window data now
# lives as extra columns/filters in the single Day Session table instead, so
# only a deliberately small subset of each window's fields is surfaced
# (`show_in_table`/filter visibility below), not full OHLC parity. A window
# with no entry in WINDOW_DISPLAY (first_60m) stays fully dormant — computed,
# available, just not shown — exactly like last_90m already is.
WINDOW_NAMES = ["first_30m", "first_60m", "hour_10_11", "first_90m", "last_90m"]

# concept name -> abbreviated label suffix, concatenated directly onto the
# window's clock-range prefix (e.g. prefix "930-10" + "Rge" -> "930-10Rge"),
# except bs_sb which gets a space before it ("930-10 BS/SB") per user request.
_WINDOW_LABEL_SUFFIX = {
    "bs_sb": " BS/SB",
    "range": "Rge",
    "range_ma20": "RgeMA20",
    "range_vs_ma20_pts": "RgeVsMA20",
    "rel_close_pts": "RelClose",
    "abs_close_pts": "AbsClose",
}

WINDOW_DISPLAY = {
    "first_30m": {
        "prefix": "930-10",
        "show": {"bs_sb", "range", "range_ma20", "range_vs_ma20_pts", "rel_close_pts", "abs_close_pts"},
    },
    "hour_10_11": {
        "prefix": "10-11",
        "show": {"bs_sb", "range", "range_ma20", "range_vs_ma20_pts", "rel_close_pts"},
    },
    "first_90m": {
        "prefix": "930-11",
        "show": {"bs_sb", "range", "range_ma20", "range_vs_ma20_pts", "rel_close_pts"},
    },
    "last_90m": {
        "prefix": "1430-16",
        "show": {"bs_sb", "range", "range_ma20", "range_vs_ma20_pts", "rel_close_pts"},
    },
}


def _window_bundle_specs(name: str) -> list[FeatureSpec]:
    p = f"win_{name}_"
    active = name in WINDOW_DISPLAY
    display = WINDOW_DISPLAY.get(name, {})
    shown = display.get("show", set())
    prefix = display.get("prefix")

    def col(c: str) -> str:
        return p + c

    def show(concept: str) -> bool:
        return concept in shown

    def label(concept: str) -> str | None:
        return f"{prefix}{_WINDOW_LABEL_SUFFIX[concept]}" if show(concept) else None

    def fk(concept: str, kind: str) -> str | None:
        """filter_kind, gated the same way as show_in_table — only the fields
        actually surfaced as columns get a corresponding filter, so the
        "930-1000 filters" etc. groups don't fill up with filters for columns
        that aren't even shown (Open/High/Low/Close/Gap/etc. for windows)."""
        return kind if show(concept) else None

    return [
        FeatureSpec(
            col("open"), "numeric", "RTH", None, f"{name} Open",
            timing="outcome", show_in_table=False, decimals=1, window=name,
        ),
        FeatureSpec(
            col("high"), "numeric", "RTH", None, f"{name} High",
            timing="outcome", show_in_table=False, decimals=1, window=name,
        ),
        FeatureSpec(
            col("low"), "numeric", "RTH", None, f"{name} Low",
            timing="outcome", show_in_table=False, decimals=1, window=name,
        ),
        FeatureSpec(
            col("close"), "numeric", "RTH", None, f"{name} Close",
            timing="outcome", show_in_table=False, decimals=1, window=name,
        ),
        FeatureSpec(
            col("bs_sb"), "categorical", "RTH", fk("bs_sb", "select"), f"{name} BS/SB",
            compute=lambda df, h=col("high_time"), l=col("low_time"): _bs_sb_expr(h, l, nullable=True),
            timing="outcome", show_in_table=show("bs_sb"), color_kind="enum", color_map=_BS_SB_COLOR_MAP,
            window=name, table_label=label("bs_sb"),
        ),
        FeatureSpec(
            col("high_time"), "time", "RTH", None, f"{name} High Time",
            timing="outcome", show_in_table=False, formatter=_hhmm, window=name,
        ),
        FeatureSpec(
            col("low_time"), "time", "RTH", None, f"{name} Low Time",
            timing="outcome", show_in_table=False, formatter=_hhmm, window=name,
        ),
        FeatureSpec(
            col("high_minute"), "numeric", "RTH", None, f"{name} High Bar",
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("low_minute"), "numeric", "RTH", None, f"{name} Low Bar",
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("high_bucket"), "categorical", "RTH", None, f"{name} High (30-min bucket)",
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("low_bucket"), "categorical", "RTH", None, f"{name} Low (30-min bucket)",
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("range"), "numeric", "RTH", fk("range", "range"), f"{name} Range",
            compute=lambda df, h=col("high"), l=col("low"): pl.col(h) - pl.col(l),
            timing="outcome", show_in_table=show("range"), decimals=1, window=name, table_label=label("range"),
        ),
        FeatureSpec(
            col("range_ma20"), "numeric", "RTH", fk("range_ma20", "range"),
            f"{name} Range MA20 (prior 20 sessions)",
            # min_samples=1: a session with zero bars in this window (holiday,
            # feed gap) leaves a null range for that day — average over
            # whichever of the trailing 20 sessions actually have data instead
            # of requiring all 20 (see rth_range_ma20 above for the full note).
            compute=lambda df, r=col("range"): pl.col(r).shift(1).rolling_mean(window_size=20, min_samples=1),
            timing="outcome", show_in_table=show("range_ma20"), decimals=0, window=name,
            table_label=label("range_ma20"),
        ),
        FeatureSpec(
            col("range_vs_ma20_pts"), "numeric", "RTH", fk("range_vs_ma20_pts", "range"),
            f"{name} Range vs MA20 (pts)",
            compute=lambda df, r=col("range"), m=col("range_ma20"): pl.col(r) - pl.col(m),
            timing="outcome", show_in_table=show("range_vs_ma20_pts"), decimals=1, color_kind="pts",
            window=name, table_label=label("range_vs_ma20_pts"),
        ),
        FeatureSpec(
            col("range_vs_ma20_dir"), "categorical", "RTH", fk("range_vs_ma20_pts", "select"),
            f"{name} Range vs MA20 direction",
            compute=lambda df, c=col("range_vs_ma20_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("abs_range_diff_pts"), "numeric", "RTH", None,
            f"{name} Abs. Range Diff (vs prev range, pts)",
            compute=lambda df, r=col("range"): pl.col(r) - pl.col(r).shift(1),
            timing="outcome", show_in_table=False, decimals=1, color_kind="pts", window=name,
        ),
        FeatureSpec(
            col("abs_range_diff_dir"), "categorical", "RTH", None, f"{name} Abs. Range Diff direction",
            compute=lambda df, c=col("abs_range_diff_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("gap_pts"), "numeric", "RTH", None, f"{name} Gap (pts)",
            compute=lambda df, o=col("open"), c=col("close"): pl.col(o) - pl.col(c).shift(1),
            timing="outcome", show_in_table=False, decimals=1, color_kind="pts", window=name,
        ),
        FeatureSpec(
            col("gap_dir"), "categorical", "RTH", None, f"{name} Gap direction",
            compute=lambda df, c=col("gap_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("rel_close_pts"), "numeric", "RTH", fk("rel_close_pts", "range"),
            f"{name} Rel. close (close-open, pts)",
            compute=lambda df, c=col("close"), o=col("open"): pl.col(c) - pl.col(o),
            timing="outcome", show_in_table=show("rel_close_pts"), decimals=1, color_kind="pts",
            window=name, table_label=label("rel_close_pts"),
        ),
        FeatureSpec(
            col("rel_close_dir"), "categorical", "RTH", fk("rel_close_pts", "select"),
            f"{name} Rel. close direction",
            compute=lambda df, c=col("rel_close_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        FeatureSpec(
            col("abs_close_pts"), "numeric", "RTH", fk("abs_close_pts", "range"),
            f"{name} Abs. close (vs prev close, pts)",
            compute=lambda df, c=col("close"): pl.col(c) - pl.col(c).shift(1),
            timing="outcome", show_in_table=show("abs_close_pts"), decimals=1, color_kind="pts",
            window=name, table_label=label("abs_close_pts"),
        ),
        FeatureSpec(
            col("abs_close_dir"), "categorical", "RTH", fk("abs_close_pts", "select"),
            f"{name} Abs. close direction",
            compute=lambda df, c=col("abs_close_pts"): _dir3(c),
            timing="outcome", show_in_table=False, window=name,
        ),
        # ---- context: previous-session (same window) shifts ----
        # Gated on the whole window being "active" (has a WINDOW_DISPLAY entry)
        # rather than a specific concept — these are filter-only companions,
        # not tied to one particular shown column — so a dormant window
        # (first_60m) ends up with these fully non-filterable too, matching
        # last_90m's precedent, instead of leaking through unconditionally.
        FeatureSpec(
            f"prev_{p}bs_sb", "categorical", "context", "select" if active else None, f"Prev. {name} BS/SB",
            compute=lambda df, c=col("bs_sb"): pl.col(c).shift(1),
            timing="pre_open", window=name,
        ),
        FeatureSpec(
            f"prev_{p}range_vs_ma20_dir", "categorical", "context", "select" if active else None,
            f"Prev. {name} RangeMA20 difference",
            compute=lambda df, c=col("range_vs_ma20_dir"): pl.col(c).shift(1),
            timing="pre_open", window=name,
        ),
        FeatureSpec(
            f"prev_{p}range_vs_ma20_pts", "numeric", "context", "range" if active else None,
            f"Prev. {name} RangeMA20 diff (pts)",
            compute=lambda df, c=col("range_vs_ma20_pts"): pl.col(c).shift(1),
            timing="pre_open", decimals=1, window=name,
        ),
        FeatureSpec(
            f"prev_{p}abs_range_diff_dir", "categorical", "context", "select" if active else None,
            f"Prev. {name} Abs. Range difference",
            compute=lambda df, c=col("abs_range_diff_dir"): pl.col(c).shift(1),
            timing="pre_open", window=name,
        ),
        FeatureSpec(
            f"prev_{p}abs_range_diff_pts", "numeric", "context", "range" if active else None,
            f"Prev. {name} Abs. Range diff (pts)",
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


def filterable_features() -> list[FeatureSpec]:
    return [f for f in REGISTRY if f.filter_kind is not None]


def table_features() -> list[FeatureSpec]:
    """Features shown in the results table, in registry order.

    Rework (2026-07-14): reverted to the single-view form — window bundles
    are now just extra columns/filters in the one Day Session table rather
    than separate per-window tabs, so `show_in_table`/`filter_kind` alone
    decide inclusion; `window`/`shared_across_tabs` are kept as metadata
    (used by dashboard.py to group filters into sub-areas) but no longer
    gate these two functions.
    """
    return [f for f in REGISTRY if f.show_in_table]
