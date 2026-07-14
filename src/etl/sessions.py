"""Minute bars -> per-session feature table (SPEC.md sections 6 & 8).

A session is valid iff RTH bars exist for that (instrument, date); the RTH
aggregation is therefore the base, left-joined with the full-day (ETH)
aggregation. Derived features (gap, rel/abs close, SB/BS, prev-* context,
combos) are applied per-instrument, sorted by date, so `.shift(1)` always
means "the previous trading session for this instrument" — never leaking
across instruments.
"""

from __future__ import annotations

import sys
from pathlib import Path

import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.definitions import is_half_day_expr, minute_of_session, time_bucket_30min
from features.registry import derived_features, WINDOW_NAMES

_WEEKDAY_NAMES = {
    1: "Monday", 2: "Tuesday", 3: "Wednesday", 4: "Thursday",
    5: "Friday", 6: "Saturday", 7: "Sunday",
}


def _weekday_name_expr(date_col: str) -> pl.Expr:
    expr = pl.lit(None, dtype=pl.Utf8)
    for num, name in _WEEKDAY_NAMES.items():
        expr = pl.when(pl.col(date_col).dt.weekday() == num).then(pl.lit(name)).otherwise(expr)
    return expr


def _build_rth_base(minutes: pl.DataFrame, half_day_flag_before: str) -> pl.DataFrame:
    rth = minutes.filter(pl.col("session") == "RTH").sort(["instrument", "date", "ts"])
    # Continuous per-instrument count of RTH bars only — never resets per
    # session, only increments on bars that actually exist. Used below to
    # pull each session's high/low bar position, which the Gyration Legs
    # page's HtimePrevHtime/LtimePrevLtime derived features (registry.py)
    # diff against the previous session's — this makes that diff correct on
    # half-days/holidays for free, with no fixed-bars-per-day assumption.
    rth = rth.with_columns(pl.int_range(1, pl.len() + 1).over("instrument").alias("_rth_bar_seq"))

    agg = rth.group_by(["instrument", "date"], maintain_order=True).agg(
        pl.col("open").first().alias("rth_open"),
        pl.col("close").last().alias("rth_close"),
        pl.col("high").max().alias("rth_high"),
        pl.col("low").min().alias("rth_low"),
        pl.col("ts").sort_by("high", descending=True).first().alias("rth_high_time"),
        pl.col("ts").sort_by("low", descending=False).first().alias("rth_low_time"),
        pl.col("_rth_bar_seq").sort_by("high", descending=True).first().alias("rth_high_bar_seq"),
        pl.col("_rth_bar_seq").sort_by("low", descending=False).first().alias("rth_low_bar_seq"),
        # close-based twins (Phase 2 spec §3.4) — the gyrations detector runs on
        # closes, not highs/lows, so these reconcile sessions with the leg table.
        pl.col("close").max().alias("rth_high_close"),
        pl.col("close").min().alias("rth_low_close"),
        pl.col("ts").sort_by("close", descending=True).first().alias("rth_high_close_ts"),
        pl.col("ts").sort_by("close", descending=False).first().alias("rth_low_close_ts"),
        pl.col("ts").last().alias("_last_rth_ts"),
    )

    agg = agg.with_columns(
        _weekday_name_expr("date").alias("weekday"),
        is_half_day_expr("_last_rth_ts", half_day_flag_before).alias("is_half_day"),
    )

    agg = agg.with_columns(
        minute_of_session("rth_high_time").alias("rth_high_minute"),
        minute_of_session("rth_low_time").alias("rth_low_minute"),
        time_bucket_30min("rth_high_time").alias("rth_high_bucket"),
        time_bucket_30min("rth_low_time").alias("rth_low_bucket"),
        minute_of_session("rth_high_close_ts").alias("rth_high_close_minute"),
        minute_of_session("rth_low_close_ts").alias("rth_low_close_minute"),
        time_bucket_30min("rth_high_close_ts").alias("rth_high_close_bucket"),
        time_bucket_30min("rth_low_close_ts").alias("rth_low_close_bucket"),
    )

    return agg.drop("_last_rth_ts")


def _in_window(ts_col: str, start: str, end: str) -> pl.Expr:
    """Bars whose time-of-day falls within [start, end] (inclusive), HH:MM clock
    range, not bar counts. `.cast(pl.Int32)` guards the known Int8-overflow trap
    on `.dt.hour()` in this environment."""
    s_h, s_m = (int(x) for x in start.split(":"))
    e_h, e_m = (int(x) for x in end.split(":"))
    minute_of_day = pl.col(ts_col).dt.hour().cast(pl.Int32) * 60 + pl.col(ts_col).dt.minute().cast(pl.Int32)
    return minute_of_day.is_between(s_h * 60 + s_m, e_h * 60 + e_m, closed="both")


def _build_window_base(minutes: pl.DataFrame, name: str, start: str, end: str) -> pl.DataFrame:
    """One config-declared window's own OHLC bundle (Phase 2 spec §3.3),
    mirroring `_build_rth_base` above but scoped to bars whose time-of-day falls
    within [start, end] instead of the whole RTH session. Column prefix
    `win_<name>_`. A session with zero bars in this clock range (possible on a
    heavily truncated half-day) simply produces no row here — left-joining onto
    the RTH base in `build_sessions` nulls out this window's columns for that
    session rather than dropping it.
    """
    p = f"win_{name}_"
    win = minutes.filter(
        (pl.col("session") == "RTH") & _in_window("ts", start, end)
    ).sort(["instrument", "date", "ts"])

    agg = win.group_by(["instrument", "date"], maintain_order=True).agg(
        pl.col("open").first().alias(p + "open"),
        pl.col("close").last().alias(p + "close"),
        pl.col("high").max().alias(p + "high"),
        pl.col("low").min().alias(p + "low"),
        pl.col("ts").sort_by("high", descending=True).first().alias(p + "high_time"),
        pl.col("ts").sort_by("low", descending=False).first().alias(p + "low_time"),
    )

    return agg.with_columns(
        minute_of_session(p + "high_time").alias(p + "high_minute"),
        minute_of_session(p + "low_time").alias(p + "low_minute"),
        time_bucket_30min(p + "high_time").alias(p + "high_bucket"),
        time_bucket_30min(p + "low_time").alias(p + "low_bucket"),
    )


def _build_eth_base(minutes: pl.DataFrame) -> pl.DataFrame:
    full = minutes.sort(["instrument", "date", "ts"])
    return full.group_by(["instrument", "date"], maintain_order=True).agg(
        pl.col("open").first().alias("eth_open"),
        pl.col("close").last().alias("eth_close"),
        pl.col("high").max().alias("eth_high"),
        pl.col("low").min().alias("eth_low"),
    )


def _apply_derived(df: pl.DataFrame) -> pl.DataFrame:
    out = df
    for spec in derived_features():
        out = out.with_columns(spec.compute(out).alias(spec.name))
    return out


def build_sessions(
    minutes: pl.DataFrame, half_day_flag_before: str = "14:00", windows: dict[str, list[str]] | None = None
) -> pl.DataFrame:
    rth_base = _build_rth_base(minutes, half_day_flag_before)
    eth_base = _build_eth_base(minutes)

    base = rth_base.join(eth_base, on=["instrument", "date"], how="left")

    windows = windows or {}
    for name in WINDOW_NAMES:
        start, end = windows[name]  # KeyError here means registry.py/config.toml have drifted — fail loud
        window_base = _build_window_base(minutes, name, start, end)
        base = base.join(window_base, on=["instrument", "date"], how="left")

    parts = []
    for instrument in base["instrument"].unique().sort().to_list():
        sub = base.filter(pl.col("instrument") == instrument).sort("date")
        sub = _apply_derived(sub)
        parts.append(sub)

    sessions = pl.concat(parts, how="vertical")
    sessions = sessions.with_columns(pl.lit(None, dtype=pl.Utf8).alias("template"))

    return sessions.sort(["instrument", "date"])
