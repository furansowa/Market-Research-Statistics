"""CSV -> normalized minute bars.

Input format (per SPEC.md section 4):
  - delimiter ';'
  - header: Date;<INSTR>(Open, Ask)*;<INSTR>(High, Ask)*;<INSTR>(Low, Ask)*;<INSTR>(Close, Ask)*
  - datetime 'DD/MM/YYYY HH:MM', already ET, DST-aware
  - decimal separator is comma
  - rows sorted newest-first; must be re-sorted ascending
  - files may overlap at edges -> dedupe on (instrument, ts)
"""

from __future__ import annotations

import re
from pathlib import Path

import polars as pl

_HEADER_INSTRUMENT_RE = re.compile(r"^([A-Za-z0-9_]+)\(")


def _instrument_from_header(columns: list[str]) -> str:
    for col in columns:
        m = _HEADER_INSTRUMENT_RE.match(col)
        if m:
            return m.group(1)
    raise ValueError(f"Could not find instrument name in header columns: {columns}")


def parse_csv(path: Path) -> pl.DataFrame:
    """Parse a single raw CSV into a normalized minute-bar frame.

    Columns returned: instrument, ts (Datetime), open, high, low, close
    """
    raw = pl.read_csv(path, separator=";", infer_schema_length=0)
    instrument = _instrument_from_header(raw.columns)
    price_cols = raw.columns[1:5]

    df = raw.select(
        pl.col(raw.columns[0]).alias("date_str"),
        *[pl.col(c) for c in price_cols],
    )

    df = df.with_columns(
        pl.lit(instrument).alias("instrument"),
        pl.col("date_str").str.strptime(pl.Datetime, "%d/%m/%Y %H:%M").alias("ts"),
    )

    rename = dict(zip(price_cols, ["open", "high", "low", "close"]))
    df = df.rename(rename)

    for c in ["open", "high", "low", "close"]:
        df = df.with_columns(
            pl.col(c).str.replace(",", ".").cast(pl.Float64).alias(c)
        )

    return df.select("instrument", "ts", "open", "high", "low", "close")


def parse_all(raw_dir: Path, pattern: str = "*.csv") -> pl.DataFrame:
    """Parse every CSV in raw_dir, concat, sort ascending, and dedupe on (instrument, ts)."""
    paths = sorted(raw_dir.glob(pattern))
    if not paths:
        raise FileNotFoundError(f"No CSV files found in {raw_dir} matching {pattern}")

    frames = [parse_csv(p) for p in paths]
    combined = pl.concat(frames, how="vertical")

    combined = combined.sort(["instrument", "ts"])
    combined = combined.unique(subset=["instrument", "ts"], keep="first", maintain_order=True)

    return combined


def tag_sessions(df: pl.DataFrame, rth_start: str = "09:30", rth_close_bar: str = "15:59",
                  ignore_weekends: bool = True) -> pl.DataFrame:
    """Add `date` (ET calendar date) and `session` ('RTH'/'ETH') columns.

    RTH = bars within [rth_start, rth_close_bar] inclusive, Mon-Fri.
    ETH = every other bar (pre/post market, overnight). Weekend bars dropped if ignore_weekends.
    """
    df = df.with_columns(
        pl.col("ts").dt.date().alias("date"),
        pl.col("ts").dt.strftime("%H:%M").alias("_time_str"),
        pl.col("ts").dt.weekday().alias("_weekday"),  # 1=Mon ... 7=Sun
    )

    if ignore_weekends:
        df = df.filter(pl.col("_weekday") <= 5)

    df = df.with_columns(
        pl.when((pl.col("_time_str") >= rth_start) & (pl.col("_time_str") <= rth_close_bar))
        .then(pl.lit("RTH"))
        .otherwise(pl.lit("ETH"))
        .alias("session")
    )

    return df.drop(["_time_str", "_weekday"])
