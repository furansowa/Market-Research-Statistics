"""Pivot time-map: WHERE IN THE SESSION do turning points happen?

Takes threshold-detected legs (gyrations.detect) and re-keys them by the
time-of-day slot their pivots land in, so the question stops being "how big
are the legs" and becomes "which 5-minute candle of the session tends to be
a turning point, and how far does price travel afterwards".

Two pieces, deliberately split so the Streamlit page can re-filter without
re-detecting:

- `extract_pivots` — runs the detector once per session and emits ONE ROW PER
  PIVOT (not per leg), carrying both adjoining legs' magnitude/duration. This
  is the expensive half (a few seconds over a full DAX history) and is what
  the page caches per (instrument, basis, timeframe, mode, threshold).
- `slot_table` — cheap aggregation of those pivot rows into per-slot counts,
  rates and magnitude percentiles. Re-runs freely on every widget change.

SESSION-BOUNDARY PIVOTS ARE NOT TURNING POINTS
-----------------------------------------------
A per-session detector run always produces a first pivot at (or very near)
the session's opening bar and a last pivot at its final bar. Neither is a
reversal the market made: the first is the seed leg's origin — wherever the
run happened to start — and the last is just where the data ran out. Counted
naively they put an enormous spike on slot 0 and on the final slot, ~1.0
pivots per session each, purely from detector geometry. `is_first`/`is_last`
flag them so the page can drop them (it does, by default). This is the same
class of artifact as the alternation-lookahead and gapless-null traps
documented elsewhere in this repo — the spike is real in the data and means
nothing about the market.

The contamination does not stop at the boundary pivot. Measured on DAX RTH at
threshold 40, the opening 5-minute candle still holds 1,526 "interior" pivots
after `is_first` is dropped — and 60% of all pivots in the first half hour are
pivot #1, the seed leg's far end. Excluding pivot #1 and #n-1 as well takes
that first candle from 1,526 to 209, while a mid-session candle barely moves
(193 -> 173). Pivot #1 IS a real reversal, so dropping it is not obviously
correct; but a reader comparing the opening candle to midday needs to know
that most of the difference is the seed leg finishing, not extra turns.
`pivot_ord`/`pivot_rord` carry each pivot's rank from both ends so the page
can offer that view instead of picking one silently.

The session's END has its own, opposite bias: a non-last pivot can only land
in the closing candles if a further threshold-sized leg still fitted before
the close, so those slots retain only the fastest legs. Measured on DAX at
threshold 40, mean next-leg duration falls from ~65 min mid-session to ~2 min
in the final candle while mean magnitude stays above 55 points. Those rows'
counts are usable; their duration and magnitude statistics are not comparable
with mid-session ones.

Slots are keyed off the ET clock (`ts` is stored in ET for every instrument),
not off bar sequence, so a session with missing minutes doesn't shift its
later pivots into the wrong slot. Turning that ET slot into an
exchange-local label is the page's job, not this module's.
"""

from __future__ import annotations

import polars as pl

from gyrations.detect import detect_legs_close_to_close, detect_legs_extreme_to_extreme

# "p% of legs reached AT LEAST X points" — X is the (100-p)th percentile of
# the magnitude distribution, by definition. Order matches how the numbers
# are read out loud, biggest coverage first.
REACH_PCTS = (90, 80, 70, 50)


def aggregate_to_tf(df: pl.DataFrame, tf_min: int) -> pl.DataFrame:
    """1-minute OHLC -> `tf_min`-minute OHLC, truncating on the ET clock.

    Bucketing is per-instrument-agnostic wall-clock truncation, which lines up
    with the session open only because every RTH window in this repo starts on
    a multiple of 30 minutes. Grouped WITH `date` so a bucket can never span
    two sessions.
    """
    if tf_min <= 1:
        return df
    return (
        df.with_columns(pl.col("ts").dt.truncate(f"{tf_min}m").alias("_bucket"))
        .group_by(["date", "_bucket"])
        .agg(
            pl.col("open").first(),
            pl.col("high").max(),
            pl.col("low").min(),
            pl.col("close").last(),
        )
        .rename({"_bucket": "ts"})
        .sort("ts")
    )


def _minute_of_day(ts) -> int:
    return ts.hour * 60 + ts.minute


def extract_pivots(
    df: pl.DataFrame,
    threshold: float,
    mode: str = "extreme_to_extreme",
    tiebreak: str = "bar_direction",
) -> pl.DataFrame:
    """One row per pivot, for every session in `df`.

    `df` needs columns ts/open/high/low/close/date and must already be
    restricted to the bars that define the session (RTH-only, say) and
    aggregated to the detection timeframe. The detector is run per `date` —
    legs never cross a session boundary, which is what makes "minute 45 of
    the session" a meaningful thing to count.

    A pivot with index p in a session of n legs is the join between leg p-1
    (`prev_*`) and leg p (`next_*`); the first and last pivot have only one
    side, and `prev_mag`/`next_mag` are null there. `kind` is read off the
    NEXT leg when there is one (a top is where a down-leg starts) and off the
    previous leg otherwise — for interior pivots the two agree by
    construction, since legs strictly alternate.
    """
    rows: list[dict] = []
    if df.is_empty():
        return pl.DataFrame(schema=_PIVOT_SCHEMA)

    for (date,), part in df.sort("ts").partition_by("date", as_dict=True, maintain_order=True).items():
        closes = part["close"].to_list()
        if mode == "close_to_close":
            legs = detect_legs_close_to_close(closes, threshold)
        else:
            bars = list(zip(part["open"], part["high"], part["low"], closes))
            legs = detect_legs_extreme_to_extreme(bars, threshold, tiebreak=tiebreak)
        if not legs:
            continue

        ts_list = part["ts"].to_list()
        n = len(legs)
        for p in range(n + 1):
            prev_leg = legs[p - 1] if p > 0 else None
            next_leg = legs[p] if p < n else None

            if next_leg is not None:
                bar_index = next_leg.start_index
                price = next_leg.start_price
                kind = "top" if next_leg.direction == "down" else "bottom"
            else:
                bar_index = prev_leg.end_index
                price = prev_leg.end_price
                kind = "top" if prev_leg.direction == "up" else "bottom"

            ts = ts_list[bar_index]
            rows.append({
                "date": str(date),
                "ts": ts,
                "mod": _minute_of_day(ts),
                "kind": kind,
                "price": float(price),
                "pivot_ord": p,
                "pivot_rord": n - p,
                "is_first": p == 0,
                "is_last": p == n,
                "prev_mag": float(prev_leg.magnitude_pts) if prev_leg else None,
                "prev_dur": _duration_min(ts_list, prev_leg) if prev_leg else None,
                "prev_dir": prev_leg.direction if prev_leg else None,
                "prev_confirmed": bool(prev_leg.confirmed) if prev_leg else None,
                "next_mag": float(next_leg.magnitude_pts) if next_leg else None,
                "next_dur": _duration_min(ts_list, next_leg) if next_leg else None,
                "next_dir": next_leg.direction if next_leg else None,
                "next_confirmed": bool(next_leg.confirmed) if next_leg else None,
            })

    return pl.DataFrame(rows, schema=_PIVOT_SCHEMA)


_PIVOT_SCHEMA = {
    "date": pl.Utf8,
    "ts": pl.Datetime,
    "mod": pl.Int32,
    "kind": pl.Utf8,
    "price": pl.Float64,
    "pivot_ord": pl.Int32,
    "pivot_rord": pl.Int32,
    "is_first": pl.Boolean,
    "is_last": pl.Boolean,
    "prev_mag": pl.Float64,
    "prev_dur": pl.Int32,
    "prev_dir": pl.Utf8,
    "prev_confirmed": pl.Boolean,
    "next_mag": pl.Float64,
    "next_dur": pl.Int32,
    "next_dir": pl.Utf8,
    "next_confirmed": pl.Boolean,
}


def _duration_min(ts_list, leg) -> int:
    delta = ts_list[leg.end_index] - ts_list[leg.start_index]
    return int(delta.total_seconds() // 60)


def session_spans(df: pl.DataFrame, open_mod: int, slot_min: int) -> pl.DataFrame:
    """Per-session first/last occupied slot — the denominator's raw material.

    A slot's "number of sessions studied" is not simply the session count:
    early closes and data gaps mean the 17:20 slot has fewer sessions behind
    it than the 10:00 slot. Sessions are contiguous, so first/last slot is
    enough to reconstruct coverage without carrying a 460k-row per-bar table.
    """
    return (
        df.with_columns(
            ((pl.col("ts").dt.hour().cast(pl.Int32) * 60
              + pl.col("ts").dt.minute().cast(pl.Int32) - open_mod) // slot_min).alias("_slot")
        )
        .group_by("date")
        .agg(
            pl.col("_slot").min().alias("first_slot"),
            pl.col("_slot").max().alias("last_slot"),
        )
        .sort("date")
    )


def slot_coverage(spans: pl.DataFrame, n_slots: int) -> list[int]:
    """How many sessions actually reach each slot, from `session_spans` output."""
    counts = [0] * n_slots
    for first, last in zip(spans["first_slot"], spans["last_slot"]):
        for s in range(max(0, first), min(n_slots - 1, last) + 1):
            counts[s] += 1
    return counts


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolation percentile over an ascending list, matching numpy's
    default — same implementation the Gyrational Time page uses, so the
    "p% reached at least X" numbers are comparable across pages."""
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    if n == 1:
        return sorted_vals[0]
    k = (pct / 100) * (n - 1)
    f = int(k)
    c = min(f + 1, n - 1)
    if f == c:
        return sorted_vals[f]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def slot_table(
    pivots: pl.DataFrame,
    coverage: list[int],
    open_mod: int,
    slot_min: int,
    n_slots: int,
    leg_side: str = "next",
) -> pl.DataFrame:
    """Per-slot counts, rates and leg-reach percentiles.

    `pivots` should already be filtered to what the caller wants counted
    (boundary pivots dropped, date range applied, tops/bottoms selected).
    `coverage[s]` is the session denominator for slot s.

    `leg_side="next"` measures the leg that STARTS at the pivot — "a top forms
    at 10:15, how far does the ensuing down-leg run" — which is the
    forward-looking reading the percentiles are phrased for. `"prev"`
    measures the leg that ENDED there instead.

    Rates come in two flavours because they answer different questions and
    only one of them is a probability:
    - `rate` / `rate_pct` = pivots ÷ sessions. Can exceed 1 (a slot can hold
      two pivots in one session at low thresholds).
    - `hit_pct` = share of sessions with AT LEAST ONE pivot in the slot. This
      is the honest "how often does this candle turn" number, capped at 100%.
    """
    mag_col = f"{leg_side}_mag"
    dur_col = f"{leg_side}_dur"

    if pivots.is_empty():
        binned = pl.DataFrame(schema={"slot": pl.Int32, "kind": pl.Utf8, "date": pl.Utf8,
                                      mag_col: pl.Float64, dur_col: pl.Int32})
    else:
        binned = pivots.with_columns(
            ((pl.col("mod") - open_mod) // slot_min).cast(pl.Int32).alias("slot")
        ).filter((pl.col("slot") >= 0) & (pl.col("slot") < n_slots))

    by_slot: dict[int, dict] = {}
    if not binned.is_empty():
        for (slot,), part in binned.partition_by("slot", as_dict=True).items():
            mags = sorted(v for v in part[mag_col].to_list() if v is not None)
            durs = [v for v in part[dur_col].to_list() if v is not None]
            by_slot[int(slot)] = {
                "n": part.height,
                "tops": int((part["kind"] == "top").sum()),
                "bottoms": int((part["kind"] == "bottom").sum()),
                "sessions_hit": part["date"].n_unique(),
                "mags": mags,
                "durs": durs,
            }

    rows = []
    for s in range(n_slots):
        d = by_slot.get(s)
        sessions = coverage[s] if s < len(coverage) else 0
        n = d["n"] if d else 0
        mags = d["mags"] if d else []
        durs = d["durs"] if d else []
        row = {
            "slot": s,
            "time": _slot_label(open_mod, slot_min, s),
            "sessions": sessions,
            "pivots": n,
            "tops": d["tops"] if d else 0,
            "bottoms": d["bottoms"] if d else 0,
            "rate": (n / sessions) if sessions else None,
            "rate_pct": (n / sessions * 100) if sessions else None,
            "hit_pct": (d["sessions_hit"] / sessions * 100) if (d and sessions) else 0.0,
            "n_legs": len(mags),
            "avg_leg": (sum(mags) / len(mags)) if mags else None,
            "avg_dur": (sum(durs) / len(durs)) if durs else None,
        }
        for p in REACH_PCTS:
            row[f"p{p}"] = _percentile(mags, 100 - p) if mags else None
        rows.append(row)

    return pl.DataFrame(rows)


def _slot_label(open_mod: int, slot_min: int, slot: int) -> str:
    """Clock label for a slot, in whatever frame `open_mod` was given in."""
    total = open_mod + slot * slot_min
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"
