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
from datetime import date
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
# macro shape templates, simple -> complex; M+k/W+k variants fall past the end
_SHAPE_ORDER = ["/", "\\", "V", "A", "N", "\\/\\", "M", "W",
                "M+1", "W+1", "M+2", "W+2", "M+3", "W+3", "flat"]


# va_prev_va's 6 symbolic codes (not magnitudes -- see
# gyrations/market_profile.py's classify_va_relationship), in the order the
# user introduced them.
_VA_PREV_VA_ORDER = ["1", "-1", "0", "11", "-11", "111"]


def _pivot_pattern_sort_key(v: str) -> tuple:
    """PivotPattern dropdown order (user spec): grouped by digit-count
    ascending, then within a length ranked by descending binary value --
    1,0 | 11,10,01,00 | 111,110,101,100,011,010,001,000 | ..."""
    return (len(v), -int(v, 2))


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
    value_labels: Optional[dict] = None  # raw stored value -> full display text in the filter dropdown only
    value_sort_key: Optional[Callable[[Any], Any]] = None  # computed sort key, for domains too large/unbounded
    # for an explicit `value_order` list (e.g. variable-length pattern strings); takes precedence over value_order.
    window: Optional[str] = None  # None = the whole-RTH "Day Session" tab; else the window tab this belongs to
    shared_across_tabs: bool = False  # True = appears in every tab regardless of `window` (date, weekday, ...)
    max_col_width: Optional[int] = None  # caps the auto-sized results-grid column width (px); forces truncation

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


def _prev_n_low(n_sessions: int) -> pl.Expr:
    """Lowest RTH low across the `n_sessions` immediately before today (today
    itself excluded). n_sessions=1 is exactly rth_low.shift(1) -- the
    existing single-prior-session "prev range" behavior; n_sessions>1 rolls
    that back further (e.g. "prev3" per the user's own definition = the
    previous *2* sessions combined, n_sessions=2). min_samples=n_sessions
    requires the full window -- null with insufficient history, same
    convention as _hilo_flag_expr above."""
    return pl.col("rth_low").shift(1).rolling_min(window_size=n_sessions, min_samples=n_sessions)


def _prev_n_high(n_sessions: int) -> pl.Expr:
    return pl.col("rth_high").shift(1).rolling_max(window_size=n_sessions, min_samples=n_sessions)


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


# Lunar phase (pure astronomical calendar arithmetic -- no external data or
# network dependency, and knowable arbitrarily far in advance, so always
# timing="pre_open" regardless of offset). Reference epoch: the New Moon
# instant of 2000-01-06 18:14 UTC, a standard fixed point for this kind of
# approximation. Evaluated at midnight of each session's calendar `date` --
# the few-hour gap to the actual RTH open is immaterial at this resolution.
_SYNODIC_MONTH = 29.530588853  # mean days from new moon to new moon
_MOON_EPOCH = date(2000, 1, 6)
_MOON_EPOCH_FRAC_DAY = (18 * 60 + 14) / 1440  # fraction of that calendar day already elapsed at 18:14 UTC

_MOON_PHASE_ORDER = ["NM", "WxC", "FQ", "WxG", "FM", "WnG", "LQ", "WnC"]
_MOON_PHASE_LABELS = {
    "NM": "New Moon", "WxC": "Waxing Crescent", "FQ": "First Quarter", "WxG": "Waxing Gibbous",
    "FM": "Full Moon", "WnG": "Waning Gibbous", "LQ": "Last Quarter", "WnC": "Waning Crescent",
}


def _moon_age_expr() -> pl.Expr:
    """Days since the most recent new moon, 0 <= age < 29.53 (continuous,
    monotonic within a cycle -- unlike illumination fraction, this
    distinguishes waxing from waning at the same amount of light)."""
    days_since_epoch = (pl.col("date") - pl.lit(_MOON_EPOCH)).dt.total_days().cast(pl.Float64)
    return (days_since_epoch - _MOON_EPOCH_FRAC_DAY) % _SYNODIC_MONTH


def _moon_phase_expr() -> pl.Expr:
    """8-way phase label from moon_age -- equal ~3.69-day slices, centered so
    New Moon (age wraps around 0) and Full Moon (age ~= 14.77) sit in the
    middle of their own slice rather than at a boundary."""
    age = _moon_age_expr()
    width = _SYNODIC_MONTH / 8
    idx = ((age + width / 2) / width).floor().cast(pl.Int64) % 8
    expr = pl.lit(_MOON_PHASE_ORDER[0])
    for i, code in enumerate(_MOON_PHASE_ORDER):
        expr = pl.when(idx == i).then(pl.lit(code)).otherwise(expr)
    return expr


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
        "moon_age_days", "numeric", "context", "range", "Moon age (days since new moon)",
        compute=lambda df: _moon_age_expr(),
        timing="pre_open", show_in_table=True, table_label="MoonAge", decimals=1,
        shared_across_tabs=True,
    ),
    FeatureSpec(
        "moon_phase", "categorical", "context", "select", "Moon phase",
        compute=lambda df: _moon_phase_expr(),
        timing="pre_open", show_in_table=True, table_label="Moon",
        value_order=_MOON_PHASE_ORDER, value_labels=_MOON_PHASE_LABELS,
        shared_across_tabs=True,
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
    # Actual timestamp of the session's last RTH bar (not a fixed 15:59 --
    # half-days end earlier). Plumbing for the Gyrations v2.0 page's leg-count
    # window columns (etl/leg_windows.py) -- not shown/filterable on its own.
    FeatureSpec(
        "rth_close_time", "time", "RTH", None, "RTH Close Time",
        timing="outcome", show_in_table=False, formatter=_hhmm,
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

    # ---- weekly close-difference derived (2026-08-02) ----
    # ISO (year, week) id -- internal only, not shown/filterable -- used to
    # group sessions into calendar weeks regardless of holidays/half-weeks.
    FeatureSpec(
        "wk_id", "numeric", "context", None, "Week ID (internal)",
        compute=lambda df: pl.col("date").dt.iso_year() * 100 + pl.col("date").dt.week(),
        timing="pre_open", show_in_table=False,
    ),
    FeatureSpec(
        "abs_wk_close", "numeric", "RTH", "range", "AbsWkClose",
        # Previous week's last close: at each week's first row, take the prior
        # row's close (the last actual trading day before this week, whatever
        # weekday that fell on), then forward-fill it through the rest of the
        # week so every session in the week carries the same reference value.
        compute=lambda df: pl.col("rth_close") - (
            pl.when(pl.col("wk_id") != pl.col("wk_id").shift(1))
            .then(pl.col("rth_close").shift(1))
            .otherwise(None)
            .forward_fill()
        ),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="AbsWkClose",
    ),
    FeatureSpec(
        "abs_wk_close_dir", "categorical", "RTH", "select", "AbsWkClose direction",
        compute=lambda df: _dir3("abs_wk_close"),
        timing="outcome", show_in_table=False,
    ),
    FeatureSpec(
        "rel_wk_close", "numeric", "RTH", "range", "RelWkClose",
        compute=lambda df: pl.col("rth_close") - pl.col("rth_open").first().over("wk_id"),
        timing="outcome", show_in_table=True, decimals=1, color_kind="pts", table_label="RelWkClose",
    ),
    FeatureSpec(
        "rel_wk_close_dir", "categorical", "RTH", "select", "RelWkClose direction",
        compute=lambda df: _dir3("rel_wk_close"),
        timing="outcome", show_in_table=False,
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
    # Gyrations v2.0 page only (show_in_table=False so Day Session's default
    # table_features() never includes them) -- no filters requested for
    # either. Coloring is comparative (whichever has the larger absolute
    # value wins), implemented page-locally, not via color_kind.
    FeatureSpec(
        "rel_high_pts", "numeric", "RTH", None, "Rel. high (high-open, pts)",
        compute=lambda df: pl.col("rth_high") - pl.col("rth_open"),
        timing="outcome", show_in_table=False, decimals=1, table_label="RelHigh",
    ),
    FeatureSpec(
        "rel_low_pts", "numeric", "RTH", None, "Rel. low (low-open, pts)",
        compute=lambda df: pl.col("rth_low") - pl.col("rth_open"),
        timing="outcome", show_in_table=False, decimals=1, table_label="RelLow",
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
    # show_in_table=False on all 5: these are only ever displayed on the
    # separate "Gyration Legs" page (via an explicit `specs` list passed to
    # build_display_table), never on Day Session's default table_features()
    # output. Their filters are filterable (filter_kind set below) but land
    # in the "legs_filters" group, which only the Legs page's render_filters
    # call requests — Day Session's default `groups=None` call never renders
    # that group, so they can't leak there either. See
    # dashboard.py's _filter_group_key/_LEGS_ONLY_FILTER_NAMES.
    FeatureSpec(
        "hl_time_diff", "numeric", "RTH", "range", "High/Low time diff (min)",
        compute=lambda df: (pl.col("rth_high_minute") - pl.col("rth_low_minute")).abs(),
        timing="outcome", show_in_table=False, decimals=0, table_label="HLtimeDiff",
    ),
    FeatureSpec(
        "hl_time_vs_prev", "numeric", "RTH", "select", "HLtimeDiff vs prev session",
        compute=lambda df: (
            pl.when(pl.col("hl_time_diff").shift(1).is_null()).then(pl.lit(None, dtype=pl.Int32))
            .when(pl.col("hl_time_diff") >= pl.col("hl_time_diff").shift(1)).then(1)
            .otherwise(-1)
        ),
        timing="outcome", show_in_table=False, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="HLtimeVsPrevHLtime",
    ),
    FeatureSpec(
        "h_time_prev_h_time", "numeric", "RTH", "range", "RTH High bar-seq vs prev session's",
        compute=lambda df: pl.col("rth_high_bar_seq") - pl.col("rth_high_bar_seq").shift(1),
        timing="outcome", show_in_table=False, decimals=0, table_label="HtimePrevHtime",
    ),
    FeatureSpec(
        "l_time_prev_l_time", "numeric", "RTH", "range", "RTH Low bar-seq vs prev session's",
        compute=lambda df: pl.col("rth_low_bar_seq") - pl.col("rth_low_bar_seq").shift(1),
        timing="outcome", show_in_table=False, decimals=0, table_label="LtimePrevLtime",
    ),
    FeatureSpec(
        "ht_vs_lt", "numeric", "RTH", "select", "HtimePrevHtime vs LtimePrevLtime",
        compute=lambda df: (
            pl.when(pl.col("h_time_prev_h_time").is_null() | pl.col("l_time_prev_l_time").is_null())
            .then(pl.lit(None, dtype=pl.Int32))
            .when(pl.col("h_time_prev_h_time") >= pl.col("l_time_prev_l_time")).then(1)
            .otherwise(-1)
        ),
        timing="outcome", show_in_table=False, decimals=0, color_kind="enum",
        color_map=_HILO_COLOR_MAP, table_label="HTvsLT",
    ),

    # ---- Gyrations v2.0 page: leg-count-in-window columns ----
    # Precomputed in etl/leg_windows.py (confirmed extreme_to_extreme legs,
    # thresholds 40/120/200) and persisted as real sessions columns -- 0 not
    # null when no legs match. show_in_table=False (v2.0-only, via its own
    # explicit specs list); filterable, but routed to a group ONLY the v2.0
    # page's render_filters call requests (see dashboard.py's
    # _LEGS_ONLY_V2_FILTER_NAMES/"legs_filters_v2") so this doesn't add
    # filters to the existing Gyration Legs page's "Timing filters" group.
    FeatureSpec(
        "bs_sb_legs_40", "numeric", "RTH", "range", "BS/SB-window legs (T=40, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="BS/SBLegs40",
    ),
    FeatureSpec(
        "first_legs_40", "numeric", "RTH", "range", "Open-to-first-extreme legs (T=40, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="FirstLegs40",
    ),
    FeatureSpec(
        "last_legs_40", "numeric", "RTH", "range", "Second-extreme-to-close legs (T=40, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="LastLegs40",
    ),
    FeatureSpec(
        "bs_sb_legs_120", "numeric", "RTH", "range", "BS/SB-window legs (T=120, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="BS/SBLegs120",
    ),
    FeatureSpec(
        "first_legs_120", "numeric", "RTH", "range", "Open-to-first-extreme legs (T=120, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="FirstLegs120",
    ),
    FeatureSpec(
        "last_legs_120", "numeric", "RTH", "range", "Second-extreme-to-close legs (T=120, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="LastLegs120",
    ),
    FeatureSpec(
        "bs_sb_legs_200", "numeric", "RTH", "range", "BS/SB-window legs (T=200, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="BS/SBLegs200",
    ),
    FeatureSpec(
        "first_legs_200", "numeric", "RTH", "range", "Open-to-first-extreme legs (T=200, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="FirstLegs200",
    ),
    FeatureSpec(
        "last_legs_200", "numeric", "RTH", "range", "Second-extreme-to-close legs (T=200, extreme)",
        timing="outcome", show_in_table=False, decimals=0, table_label="LastLegs200",
    ),

    # ---- Gyrations v2.0 page: macro shape templates ----
    # Persisted into sessions by run_shapes.py (post-ETL step) from the
    # session_shapes table -- how the running HOD/LOD was built from confirmed
    # extreme_to_extreme legs (src/gyrations/shapes.py). Same v2.0-only
    # routing as the leg-count columns above.
    FeatureSpec(
        "shape_40", "categorical", "RTH", "select", "Day shape (T=40, extreme)",
        timing="outcome", show_in_table=False, table_label="Shape40",
        value_order=_SHAPE_ORDER,
    ),
    FeatureSpec(
        "shape_120", "categorical", "RTH", "select", "Day shape (T=120, extreme)",
        timing="outcome", show_in_table=False, table_label="Shape120",
        value_order=_SHAPE_ORDER,
    ),
    FeatureSpec(
        "shape_200", "categorical", "RTH", "select", "Day shape (T=200, extreme)",
        timing="outcome", show_in_table=False, table_label="Shape200",
        value_order=_SHAPE_ORDER,
    ),

    # ---- Gyrations v2.0 page: pivot patterns ----
    # Persisted into sessions by run_shapes.py alongside the shapes above, but
    # a distinct, simpler concept: one '1'/'0' digit per CONFIRMED leg (not
    # the shape's merged macro swings) -- '1' if that leg's own end price is
    # >= the session's RTH open, else '0'. E.g. 4 legs ending at +30/-95/+150/
    # +20 relative to open -> "1011". NULL on a "flat" (zero-leg) session.
    # max_col_width caps the results-grid column at ~10 visible characters;
    # longer patterns are visually clipped by the grid, full value on hover.
    FeatureSpec(
        "pivot_pattern_40", "categorical", "RTH", "select", "Pivot pattern (T=40, extreme)",
        timing="outcome", show_in_table=False, table_label="PivotPattern40",
        value_sort_key=_pivot_pattern_sort_key, max_col_width=104,
    ),
    FeatureSpec(
        "pivot_pattern_120", "categorical", "RTH", "select", "Pivot pattern (T=120, extreme)",
        timing="outcome", show_in_table=False, table_label="PivotPattern120",
        value_sort_key=_pivot_pattern_sort_key, max_col_width=104,
    ),
    FeatureSpec(
        "pivot_pattern_200", "categorical", "RTH", "select", "Pivot pattern (T=200, extreme)",
        timing="outcome", show_in_table=False, table_label="PivotPattern200",
        value_sort_key=_pivot_pattern_sort_key, max_col_width=104,
    ),

    # ---- Point of Control / Value Area ----
    # Persisted by run_market_profile.py from the full session's 1-min RTH
    # closes (src/gyrations/market_profile.py -- real time-at-price POC/VA
    # expansion, not the ProRealTime script's fixed-%-of-range approximation
    # it was modeled on). basis="RTH", not v2-only-routed -- these fall
    # through _filter_group_key's default branch into "rth_filters", so they
    # appear on Day Session/Gyration Legs/Gyrations v2.0 like gap_pts does.
    # The -1/0/1 "vs value area" columns reuse color_kind="pts" coloring
    # (above=positive=green, below=negative=red, inside=neutral) since the
    # stored value already IS a sign.
    FeatureSpec(
        "poc", "numeric", "RTH", "range", "Point of Control (RTH)",
        timing="outcome", show_in_table=False, decimals=1, table_label="POC",
    ),
    FeatureSpec(
        "va70_hi", "numeric", "RTH", "range", "Value Area High (70%, RTH)",
        timing="outcome", show_in_table=False, decimals=1, table_label="VA70Hi",
    ),
    FeatureSpec(
        "va70_lo", "numeric", "RTH", "range", "Value Area Low (70%, RTH)",
        timing="outcome", show_in_table=False, decimals=1, table_label="VA70Lo",
    ),
    FeatureSpec(
        "o_prev_poc", "numeric", "RTH", "range", "Open minus previous day's POC",
        timing="pre_open", show_in_table=False, decimals=1, color_kind="pts", table_label="OPrevPOC",
    ),
    FeatureSpec(
        "o_prev_va", "numeric", "RTH", "select", "Open vs previous day's Value Area",
        timing="pre_open", show_in_table=False, decimals=0, color_kind="pts", table_label="OPrevVA",
    ),
    FeatureSpec(
        "h_prev_va", "numeric", "RTH", "select", "RTH High vs previous day's Value Area",
        timing="outcome", show_in_table=False, decimals=0, color_kind="pts", table_label="HPrevVA",
    ),
    FeatureSpec(
        "l_prev_va", "numeric", "RTH", "select", "RTH Low vs previous day's Value Area",
        timing="outcome", show_in_table=False, decimals=0, color_kind="pts", table_label="LPrevVA",
    ),
    FeatureSpec(
        "cl_poc", "numeric", "RTH", "range", "Close minus today's own POC",
        timing="outcome", show_in_table=False, decimals=1, color_kind="pts", table_label="ClPOC",
    ),
    FeatureSpec(
        "cl_va", "numeric", "RTH", "select", "Close vs today's own Value Area",
        timing="outcome", show_in_table=False, decimals=0, color_kind="pts", table_label="ClVA",
    ),
    FeatureSpec(
        "va_range", "numeric", "RTH", "range", "Value Area width (VA70Hi - VA70Lo)",
        timing="outcome", show_in_table=False, decimals=1, table_label="VArange",
    ),
    FeatureSpec(
        "va_range_diff", "numeric", "RTH", "range", "Value Area width vs previous day's",
        timing="outcome", show_in_table=False, decimals=1, color_kind="pts", table_label="VArangeDiff",
    ),
    FeatureSpec(
        "va_prev_va", "categorical", "RTH", "select",
        "Today's Value Area vs previous day's (1/-1 shifted-overlap, 0 contained, "
        "11/-11 shifted-no-overlap, 111 engulfs)",
        timing="outcome", show_in_table=False, table_label="VAPrevVA",
        value_order=_VA_PREV_VA_ORDER,
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
        "prev_abs_wk_close", "numeric", "context", "range", "Prev. AbsWkClose",
        compute=lambda df: pl.col("abs_wk_close").shift(1),
        timing="pre_open", decimals=1,
    ),
    FeatureSpec(
        "prev_abs_wk_close_dir", "categorical", "context", "select", "Prev. AbsWkClose direction",
        compute=lambda df: pl.col("abs_wk_close_dir").shift(1),
        timing="pre_open",
    ),
    FeatureSpec(
        "prev_rel_wk_close", "numeric", "context", "range", "Prev. RelWkClose",
        compute=lambda df: pl.col("rel_wk_close").shift(1),
        timing="pre_open", decimals=1,
    ),
    FeatureSpec(
        "prev_rel_wk_close_dir", "categorical", "context", "select", "Prev. RelWkClose direction",
        compute=lambda df: pl.col("rel_wk_close_dir").shift(1),
        timing="pre_open",
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

    # ---- Open/Close vs multi-session prior range ----
    # "prev3"/"prev5" per the user's own definition: prev3 = combined range of
    # the previous 2 sessions, prev5 = combined range of the previous 4
    # sessions ("prev" alone, above, is the previous 1 session). Close
    # variants are timing="outcome" (need today's own close); Open variants
    # are timing="pre_open" like the existing "prev" ones above.
    FeatureSpec(
        "open_vs_prev3_range", "categorical", "context", "select", "Open vs prev3 RTH range",
        # Explicit null guard: an unguarded when/otherwise chain would let a
        # null _prev_n_low(2)/_prev_n_high(2) (insufficient history) fall
        # through to "inside" instead of null -- the same latent quirk the
        # pre-existing open_vs_prev_range above has (flagged separately, not
        # touched here), avoided here the same way _hilo_flag_expr avoids it.
        compute=lambda df: (
            pl.when(_prev_n_low(2).is_null()).then(pl.lit(None, dtype=pl.Utf8))
            .when(pl.col("rth_open") < _prev_n_low(2)).then(pl.lit("below"))
            .when(pl.col("rth_open") > _prev_n_high(2)).then(pl.lit("above"))
            .otherwise(pl.lit("inside"))
        ),
        timing="pre_open",
    ),
    FeatureSpec(
        "open_vs_prev3_range_pct", "numeric", "context", "range",
        "Open position in prev3 range (0=low,1=high)",
        compute=lambda df: (pl.col("rth_open") - _prev_n_low(2)) / (_prev_n_high(2) - _prev_n_low(2)),
        timing="pre_open", decimals=2,
    ),
    FeatureSpec(
        "open_vs_prev5_range", "categorical", "context", "select", "Open vs prev5 RTH range",
        compute=lambda df: (
            pl.when(_prev_n_low(4).is_null()).then(pl.lit(None, dtype=pl.Utf8))
            .when(pl.col("rth_open") < _prev_n_low(4)).then(pl.lit("below"))
            .when(pl.col("rth_open") > _prev_n_high(4)).then(pl.lit("above"))
            .otherwise(pl.lit("inside"))
        ),
        timing="pre_open",
    ),
    FeatureSpec(
        "open_vs_prev5_range_pct", "numeric", "context", "range",
        "Open position in prev5 range (0=low,1=high)",
        compute=lambda df: (pl.col("rth_open") - _prev_n_low(4)) / (_prev_n_high(4) - _prev_n_low(4)),
        timing="pre_open", decimals=2,
    ),
    FeatureSpec(
        "close_vs_prev_range", "categorical", "context", "select", "Close vs prev RTH range",
        compute=lambda df: (
            pl.when(pl.col("rth_low").shift(1).is_null()).then(pl.lit(None, dtype=pl.Utf8))
            .when(pl.col("rth_close") < pl.col("rth_low").shift(1)).then(pl.lit("below"))
            .when(pl.col("rth_close") > pl.col("rth_high").shift(1)).then(pl.lit("above"))
            .otherwise(pl.lit("inside"))
        ),
        timing="outcome",
    ),
    FeatureSpec(
        "close_vs_prev_range_pct", "numeric", "context", "range",
        "Close position in prev range (0=low,1=high)",
        compute=lambda df: (
            (pl.col("rth_close") - pl.col("rth_low").shift(1))
            / (pl.col("rth_high").shift(1) - pl.col("rth_low").shift(1))
        ),
        timing="outcome", decimals=2,
    ),
    FeatureSpec(
        "close_vs_prev3_range", "categorical", "context", "select", "Close vs prev3 RTH range",
        compute=lambda df: (
            pl.when(_prev_n_low(2).is_null()).then(pl.lit(None, dtype=pl.Utf8))
            .when(pl.col("rth_close") < _prev_n_low(2)).then(pl.lit("below"))
            .when(pl.col("rth_close") > _prev_n_high(2)).then(pl.lit("above"))
            .otherwise(pl.lit("inside"))
        ),
        timing="outcome",
    ),
    FeatureSpec(
        "close_vs_prev3_range_pct", "numeric", "context", "range",
        "Close position in prev3 range (0=low,1=high)",
        compute=lambda df: (pl.col("rth_close") - _prev_n_low(2)) / (_prev_n_high(2) - _prev_n_low(2)),
        timing="outcome", decimals=2,
    ),
    FeatureSpec(
        "close_vs_prev5_range", "categorical", "context", "select", "Close vs prev5 RTH range",
        compute=lambda df: (
            pl.when(_prev_n_low(4).is_null()).then(pl.lit(None, dtype=pl.Utf8))
            .when(pl.col("rth_close") < _prev_n_low(4)).then(pl.lit("below"))
            .when(pl.col("rth_close") > _prev_n_high(4)).then(pl.lit("above"))
            .otherwise(pl.lit("inside"))
        ),
        timing="outcome",
    ),
    FeatureSpec(
        "close_vs_prev5_range_pct", "numeric", "context", "range",
        "Close position in prev5 range (0=low,1=high)",
        compute=lambda df: (pl.col("rth_close") - _prev_n_low(4)) / (_prev_n_high(4) - _prev_n_low(4)),
        timing="outcome", decimals=2,
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
