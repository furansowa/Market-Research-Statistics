# Code Structure — DOW Session Lookup Engine

Context for another Claude session that needs to add features to this codebase.
There are two design docs — `Gyrations_lookup_engine_SPEC.md` (Phase 1, all-phase
overview) and `Gyrations_lookup_engine_SPEC_phase2.md` (gyrations, windows, offset
lookup — companion to this file). Read both first for the "why" behind decisions.
This file describes what's actually built and how the pieces fit together.

## What's built so far

**Phase 1**: ETL pipeline (raw CSV → SQLite) + Streamlit Panel A (filter/browse +
click-to-chart). Complete.

**Phase 2, work-order items 1–3** (of 8 — see Phase 2 spec §7): `seq` + offset-based
lookup, `FeatureSpec.timing`/`show_in_table` driving a fully registry-driven
sidebar and results table, close-based session extremes. Complete, tested,
browser-verified.

**Phase 2, work-order item 4** (complete, both modes): the leg detector
(`src/gyrations/detect.py`) — `close_to_close` (default, precomputed via ETL
for `rth`+`eth` scopes across all 14 thresholds) and `extreme_to_extreme`
(bar-direction tiebreak, on-demand only — not in `[gyrations].precompute`,
computed live wherever it's needed instead). Property tests P1–P8 + the §2.8
invariant written *before* the detector, per spec, for both modes. 99 tests
total, including full-history acceptance checks against the real ~2.3M-row
`gyrations` table. Verified against the spec's own §9 sanity numbers (exact
match on `max deepest_retr_pts` and median magnitudes) and its §8 acceptance
tests (`bar_direction` produces different pivots depending on close vs open;
extreme mode yields strictly more legs than close mode).

**Phase 2, work-order item 5** (complete): the gyration chart overlay on
`render_session_chart` — polyline through pivots, magnitude annotations,
dashed unconfirmed legs, optional retracement-zone shading, optional
close-based HOD/LOD comparison markers. Computed **live** per displayed
session (not read from the precomputed table), so it works for both modes
and any threshold with an instant redraw, per spec's "verification tool, not
decoration" framing.

**Phase 2, work-order item 6** (complete, scoped): config-declared window
bundles (spec §3.3) — full column parity with the whole-RTH "Day Session"
columns (open/high/low/close/bs_sb/times/buckets/range/range_ma20/range vs
MA20/abs range diff/gap/rel close/abs close, all with `_dir` companions, plus
`prev_*` cross-session shifts using *that window's own* prior-day value), for
four configured windows (`first_30m`, `first_60m`, `first_90m`, `hour_10_11`).
Delivered as one dashboard tab per window (`dashboard.py`'s `WINDOW_TABS`),
each an independent clone of the whole Panel A experience (own gyration-overlay
controls, own filters, own table, own chart with swings computed only over
that window's own bars) rather than a shared control surface — see "Dashboard"
below. Leg aggregates over windows (item 7) are explicitly **not** built yet
(needs items 6+4, deferred per the user: "this is before working on legs,
stats etc... we'll see this later").

**Not yet built**: leg aggregates over windows (item 7), and the remaining
dashboard tabs beyond the per-window Panel A clones (Stats/Legs/Lookup —
item 8's *other* tabs; item 8's "introduce `st.tabs()`" prerequisite is now
done, just not yet used for anything beyond the per-window views).

## File tree

```
Gyrations_app/
├── config.toml                 # session/gap/windows/gyrations/net_points/data settings
├── run_etl.py                  # ETL entry point: raw CSVs -> data/db/lookup.sqlite
├── .streamlit/config.toml      # Streamlit theme (dark base + pastel-blue accent)
├── .claude/launch.json         # dev-server launch config for the Preview tool
├── data/
│   └── db/lookup.sqlite        # generated store (minutes + sessions tables)
├── src/
│   ├── etl/
│   │   ├── parse.py            # CSV -> normalized minute bars, RTH/ETH tagging
│   │   ├── sessions.py         # minute bars -> one row per session (the feature table)
│   │   ├── gyrations.py        # minute bars -> gyrations-table rows, per scope (rth/eth/continuous)
│   │   └── load.py             # write minutes+sessions+gyrations to SQLite
│   ├── features/
│   │   ├── registry.py         # FeatureSpec registry — THE extensibility point (see below)
│   │   └── definitions.py      # time-of-day helper expressions used by sessions.py
│   ├── gyrations/
│   │   └── detect.py           # pure zigzag leg detector — no Polars/SQLite/timestamp deps
│   ├── query/
│   │   └── filters.py          # registry -> offset-aware SQL joins + WHERE clauses
│   └── app/
│       └── dashboard.py        # Streamlit Panel A (filters, table, charts)
├── tests/
│   ├── conftest.py             # session-scoped sqlite3 connection fixture to the built DB
│   ├── test_sessions.py        # acceptance tests: invariants over the built `sessions` table
│   ├── test_filters.py         # acceptance tests: offset-join mechanism correctness
│   ├── test_gyrations.py       # detector property tests (P1-P8, §2.8 invariant) on hand-built fixtures
│   ├── test_etl_gyrations.py   # tests for the ETL glue (leg_index uniqueness across sessions)
│   └── test_gyrations_db.py    # same acceptance properties, checked against the full real table
├── Gyrations_lookup_engine_SPEC.md          # original design spec, all phases
└── Gyrations_lookup_engine_SPEC_phase2.md   # Phase 2 spec: gyrations, windows, offset lookup
```

Raw CSVs are **not** copied into the repo — `config.toml`'s `[data].raw_dir` points
directly at the external data folder
(`D:/Trading/Research-Project-2026-07/2026 - Instruments Data (2008-2026)/DOW-Data`)
to avoid duplicating ~270MB. They're read fresh from disk on every ETL run.

## Data flow (ETL)

```
raw CSVs (semicolon-delimited, DD/MM/YYYY HH:MM, decimal comma)
  → parse.parse_all()      concat all CSVs, sort by (instrument, ts), dedupe on ts
  → parse.tag_sessions()   add `date` (ET calendar date) + `session` ("RTH"/"ETH") columns
  → sessions.build_sessions()   aggregate minute bars into one row per (instrument, date)
  → gyrations.compute_session_scope_legs() / compute_continuous_scope_legs()
        run the detector per configured (scope, threshold) combo
  → load.write_minutes() / write_sessions() / write_gyrations_rows()   SQLite
```

Run via: `.venv/Scripts/python.exe run_etl.py` — always does a **full rebuild**
(`DELETE ... WHERE instrument = ?` then reinsert) since it always re-reads every
raw CSV fresh. Currently: 5.7M minute bars → 4,461 sessions for US30, 2009-03-11
through 2026-07-01, 45 columns in `sessions`; ~2.3M rows in `gyrations`
(803,891 `rth` + 1,496,748 `eth`, `close_to_close` mode, 14 thresholds each).
Full ETL including gyrations precompute: ~100s.

⚠️ **Schema changes need a DB delete, not just a rerun.** `load.py`'s
`create_*_table` uses `CREATE TABLE IF NOT EXISTS`, which is a no-op on an
existing table — it will NOT add new columns or rename/drop old ones. Whenever
you add/rename a registry field that changes the `sessions` or `minutes` schema,
`rm data/db/lookup.sqlite` before rerunning `run_etl.py`, or you'll get silent
schema drift (stale columns hanging around, new ones missing).

### `src/etl/parse.py`
- `parse_csv(path)` — one file → DataFrame with `instrument, ts, open, high, low, close`.
  Instrument name is regex-extracted from the header (e.g. `US30(Open, Ask)*` → `US30`).
- `parse_all(raw_dir)` — globs `*.csv`, concatenates, sorts, dedupes on `(instrument, ts)`
  (files can overlap at edges).
- `tag_sessions(df, rth_start, rth_close_bar, ignore_weekends)` — adds `date` and
  `session` ("RTH" if bar's clock time is in `[09:30, 15:59]`, else "ETH"). Drops
  Sat/Sun bars entirely if `ignore_weekends`.

### `src/etl/sessions.py`
The core aggregation. A session is valid iff RTH bars exist for that date — RTH
aggregation is the base, left-joined with the full-day (ETH) aggregation.

- `_build_rth_base()` — groups RTH bars by `(instrument, date)`: open of first bar,
  close of last bar, max high / min low **and the timestamp of each extreme**
  (via `sort_by(...).first()` — ties go to the earliest occurrence), weekday name,
  `is_half_day` (last RTH bar's clock time < `half_day_flag_before`). Also computes
  the **close-based twins** (Phase 2 spec §3.4) the same way but over bar `close`
  instead of `high`/`low`: `rth_high_close`, `rth_low_close`, and their
  timestamps/minute/bucket — these exist because the gyrations detector runs on
  closes, and these columns let the sessions table reconcile with the leg
  table (`gyrations`, see below). The extreme-based columns remain canonical for
  display; `rth_range_close <= rth_range` always (tested).
- `_build_eth_base()` — same idea but over **all** bars for the date (RTH+ETH),
  giving `eth_open/high/low/close` = the full-day OHLC.
- `_apply_derived(df)` — walks `derived_features()` from the registry **in
  registry order** and applies each one's `compute(df)` as a `with_columns` call.
  This is run **per-instrument** (looped, sorted by date) so that `.shift(1)`
  (used for gap/prev-session features) never leaks across instruments once a
  second instrument is added. `seq` (a dense 1..N per-instrument row number,
  used by the offset-join mechanism in `filters.py`) is a derived FeatureSpec
  computed this same way: `pl.int_range(1, pl.len() + 1)` inside the
  per-instrument loop — no special-cased Python code needed for it.

⚠️ **Known gotcha** (already hit and fixed once): Polars' `.dt.hour()` /
`.dt.minute()` return **Int8** in this environment. `hour() * 60` silently
overflows that 8-bit range. Any new time-arithmetic helper in `definitions.py`
must `.cast(pl.Int32)` before multiplying.

### `src/gyrations/detect.py` — the leg detector (Phase 2 spec §2)

Pure algorithm module — **no Polars, no SQLite, no timestamp/datetime
dependency at all**, deliberately. Operates on a plain `list[float]` (closes)
and returns `Pivot`/`Leg` objects addressed by integer index into that list.
Keeping it dependency-free is what makes it directly unit-testable against
small hand-built fixture series (`tests/test_gyrations.py`) without any ETL
plumbing in the way.

- `detect_pivots_close_to_close(series, threshold)` — the zigzag pivot
  detector, a direct translation of the spec §2.4 reference pseudocode.
  Two-sided seeding (tracks a running high *and* low until the first
  threshold-sized reversal establishes direction), P4 simultaneous-trigger
  tie-break (whichever extreme occurred later), P5 degenerate-seed handling
  (skip emitting a duplicate pivot when seed index == confirm index).
- `_legs_from_pivots(pivots, threshold)` — consecutive pivot pairs → `Leg`s.
  §2.5 revised twice from the original "first/last leg always unconfirmed"
  spec text: **every** leg's `confirmed` flag is now just
  `magnitude_pts >= threshold`, no position special-casing at all. Interior
  legs' start is always a validated reversal, so their magnitude is
  structurally guaranteed >= threshold (the check is a no-op safety net for
  them). The trailing leg's start is likewise validated — only its end was
  ever in question (scope ran out before a further reversal), so if its
  magnitude already clears threshold that's confirmed, full stop (2026-07-12
  fix). The seed leg is the one genuinely interesting case: its start is
  two-sided unseeded-phase bookkeeping (`lo_at_hi`/`hi_at_lo`), not a
  validated pivot, so unlike interior/trailing legs its magnitude is *not*
  structurally guaranteed >= threshold — constructed a counterexample series
  ([100,90,130,85], T=20) where the seed leg's own span is only 10 points even
  though the move that ended the unseeded phase was 40 points (see
  `test_seed_leg_can_be_unconfirmed_when_its_own_magnitude_is_below_threshold`
  in `tests/test_gyrations.py`) — the magnitude check on the seed leg is doing
  real work, not just documenting intent (2026-07-13 fix, close_to_close mode
  only — see extreme_to_extreme caveat below). Verified via 9000+ randomized
  trials that confirming the seed leg by magnitude never violates the §2.8
  invariant in close_to_close mode: the retracement scan starts fresh at the
  seed's own `start_index`/`start_price`, so the same "would have already
  ended the leg" argument that protects interior legs applies to the seed leg
  too.
  ⚠️ **`extreme_to_extreme` mode has a separate, pre-existing §2.8 invariant
  bug**, unrelated to either of the above fixes and NOT yet fixed: randomized
  stress-testing found the retracement invariant already breaks for
  interior/trailing legs (not just the seed leg) in that mode — badly with
  the `adverse_first`/`favourable_first` tiebreaks (~64% of trials), and at a
  low but nonzero rate (~0.5-0.6%) even with the default `bar_direction`
  tiebreak actually used by the dashboard. Root cause not yet diagnosed. Since
  `extreme_to_extreme` is on-demand only (never precomputed into the
  `gyrations` table — see below), this doesn't affect any stored data, only
  what the dashboard shows live when a user switches Mode to
  `extreme_to_extreme`. Flagged to the user 2026-07-13; not yet scheduled.
- `_compute_retracements_close_to_close(series, legs)` — deepest retracement
  ("elasticity", §2.6), one O(n) pass per leg. **Holds the §2.8 invariant
  (`deepest_retr_pts < threshold` for every confirmed leg) by construction**:
  the running-extreme tracking here is exactly what the detector itself uses
  to decide when a leg ends, so an interior drawdown reaching `threshold`
  would already have ended the leg at that point instead of showing up later
  as a "retracement." No terminal-bar special-casing needed for close mode —
  each bar contributes exactly one value, so there's no separate high/low of
  the terminal bar to misattribute (unlike `extreme_to_extreme`, next).
- `detect_legs_close_to_close(series, threshold)` — the public entry point:
  pivots → legs → retracements filled in, in one call.

**`extreme_to_extreme` mode** (`detect_pivots_extreme_to_extreme`,
`_compute_retracements_extreme_to_extreme`, `detect_legs_extreme_to_extreme`):
tracks bar **highs** for up-leg extension, **lows** for down-leg extension.
A bar contributes *two* ticks (high and low), and a single bar can extend the
current leg *and* fall `threshold` past it in the opposite sense — OHLC alone
can't say which happened first, so `_ordered_ticks(o, h, l, c, dirn, tiebreak)`
resolves the ambiguity before either tick is processed:
- `bar_direction` (default): `close >= open` ⇒ low tested before high, else
  high before low. The only rule using information actually in the bar.
- `adverse_first` / `favourable_first`: order relative to the *current*
  direction (the extreme that would reverse it, or extend it, tested first).
  Undefined without an established direction, so both fall back to
  `bar_direction` during the unseeded phase.

Only one reversal is resolved per bar (processing stops once a reversal
fires) — the unprocessed second tick "belongs" to the next leg, mirroring the
§2.6 terminal-bar rule below. This is also *why* extreme mode never needs
close-mode's P4 simultaneous-trigger handling: a tick is single-typed (high
XOR low), so it can only ever test one direction; the tiebreak has already
resolved the bar-level ambiguity by imposing a sequential order.

`_compute_retracements_extreme_to_extreme` implements the **§2.6 terminal-bar
rule** (required, or the invariant fails): on a leg's first and last bars,
only the extreme that *is* that leg's pivot participates in the retracement
scan — the opposite extreme belongs to the *adjacent* leg (it's what
seeded/triggered the transition). Interior bars scan both ticks, in the same
tiebreak order used during pivot detection, so the invariant holds by the
same "would have ended the leg" argument as close mode.

**Verified against the spec's own §9 sanity numbers** on the 2026 H1 sample:
`max deepest_retr_pts` matches *exactly* (49.6 @ T=50, 99.5 @ T=100), median
leg magnitude matches exactly (98 @ T=50, 182 @ T=100) — for `close_to_close`.
Also spot-checked `extreme_to_extreme` against real session data: yields
~1.6-3x more legs than `close_to_close` at the same threshold (spec measured
~1.3-2.9x depending on threshold — same order, same decreasing-with-T
pattern). High confidence the translation from pseudocode to code is
faithful, not just "tests I wrote happen to pass."

### `src/etl/gyrations.py` — ETL glue, per scope (spec §2.9)

Bridges the pure detector to Polars/SQLite/real timestamps. Three scopes:
- `compute_session_scope_legs(minutes, instrument, scope, threshold, mode)` —
  `rth` or `eth`: partitions `minutes` by `date` and runs one independent
  detector pass per session. `minutes` must already be pre-filtered to the
  scope's bars (RTH-only for `rth`, all bars for `eth`) for a single instrument.
- `compute_continuous_scope_legs(minutes, instrument, threshold, mode)` —
  one detector pass over the *entire* per-instrument series, never resetting
  at date boundaries (so a leg can span multiple calendar dates).

⚠️ **Real bug caught here, not hypothetical**: the `gyrations` table's
PRIMARY KEY is `(instrument, scope, threshold, mode, leg_index)` — no `date`
component. The detector numbers legs `0..N` *within a single call* (i.e.
within one session, for rth/eth). Writing each session's legs with that
per-session numbering directly caused every session's leg 0 to collide with
every other session's leg 0 on the primary key, and `INSERT OR REPLACE`
silently overwrote almost the entire table down to a few hundred surviving
rows out of an expected ~800K — with **no error, no exception, just quietly
wrong data**. `compute_session_scope_legs` now renumbers `leg_index` to
increment globally across all sessions in chronological order before
returning. Regression test: `tests/test_etl_gyrations.py`. Lesson: after any
bulk ETL write, spot-check row *counts* against an independent estimate
(session count × expected legs/session) — a successful-looking run with no
exceptions is not evidence the data is right.

**A separate, non-bug surprise worth knowing about**: fixed point-thresholds
produce noticeably fewer legs/session on the full 17-year history (2009-2026)
than on a 2026-only sample — roughly 40-50% of the 2026-only density at every
threshold. This is **expected and spec-acknowledged**, not something to "fix":
spec §9 says outright "these ... will not match the 17-year set," and the
"Density calibration" discussion (spec §1) explains why — a fixed point
threshold represents a much larger relative move against DOW's much lower
2009-era index level than against 2026's ~52,000 level. This is exactly what
spec §5's planned "drift diagnostic" (not yet built) exists to visualize.

### `src/etl/load.py`
Schema for `minutes`/`sessions` is **derived from the Polars DataFrame's own
dtypes**, not hand-typed — add a column anywhere upstream (registry/sessions.py)
and the `CREATE TABLE` DDL picks it up automatically (Float→REAL, Boolean/Int→
INTEGER, else TEXT). Date/Datetime columns are stringified to ISO text before
insert since Python's sqlite3 no longer auto-adapts those types. Writes are
`DELETE` + `INSERT OR REPLACE`, scoped to the instruments present in the
incoming DataFrame. Also creates `ix_sessions_seq (instrument, seq)` for the
offset-join mechanism.

`gyrations` is a **fixed, hand-written schema** (`create_gyrations_table`),
not derived from a DataFrame sample — unlike `sessions`, it isn't
registry-driven, so there's no dtype source to derive from. `write_gyrations_rows`
takes a plain `list[dict]` (not a Polars DataFrame — the detector's output is
inherently row-by-row) and inserts via `executemany`. `delete_gyrations_scope`
clears one `(instrument, scope, mode)` combination before a rebuild (all
thresholds for that combination get recomputed together).

## The feature registry — the extensibility core

**`src/features/registry.py`** is the single place that defines every column in
the `sessions` table, and is what makes new features "just work" across the ETL,
filters, and dashboard (sidebar **and now the results table**) without touching
plumbing elsewhere.

Each feature is a `FeatureSpec`:
```python
FeatureSpec(
    name, dtype, basis, filter_kind, label,
    compute=None,           # lambda df: pl.Expr, for derived (non-base) features
    timing="outcome",       # "pre_open" | "outcome" — see below
    show_in_table=False,    # include as a column in the results grid?
    formatter=None,         # lambda raw_value: display_value, e.g. HH:MM from a timestamp
    decimals=None,          # fixed decimal places for numeric display (None = don't force)
    color_kind=None,        # "pts" (sign-based green/red/grey) | "enum" (value->color map) | None
    color_map=None,         # required when color_kind="enum", e.g. {"SB": COLOR_SB, "BS": COLOR_BS}
    table_label=None,       # short header for the grid; falls back to `label` if unset
    window=None,            # None = whole-RTH "Day Session" tab; else the window tab this belongs to
    shared_across_tabs=False,  # True = appears in every tab regardless of `window` (date, weekday, is_half_day)
)
```
`window`/`shared_across_tabs` (item 6 addition): `table_features(window=...)` /
`filterable_features(window=...)` filter to `shared_across_tabs or window ==
<arg>`, so each dashboard tab gets its own column/filter set from the *same*
registry list, no duplication of the underlying list itself — see "Window
bundles" below.
- `dtype`: `"numeric" | "categorical" | "boolean" | "time"`
- `basis`: `"RTH" | "ETH" | "context"` — used to group filters in the sidebar
- `filter_kind`: `"range" | "select" | "bool" | None` (`None` = not filterable,
  e.g. raw `date` and the raw `rth_high_time`/`rth_low_time` timestamps)
- `compute`: **only set for derived features** — applied in `sessions._apply_derived()`.
  Features without `compute` ("base" features) come from the RTH/ETH aggregation
  in `sessions.py` directly.
- `timing`: **Phase 2 addition (spec §3.6).** `"pre_open"` = knowable before the
  session opens (e.g. `weekday`, `gap_pts`, `rth_open`, every `prev_*` context
  column — a prior session is always-already-closed history). `"outcome"` =
  only knowable at/after the close (e.g. `rth_high`, `bs_sb`, `rel_close_pts`).
  This is intrinsic to the feature at offset 0 — a negative-offset view of an
  "outcome" feature is still safe (it's about an already-closed prior session);
  that distinction is handled at query time (see `render_filters` below), not
  by changing `timing` per offset.
- `show_in_table` / `formatter` / `decimals` / `color_kind` / `color_map` /
  `table_label`: **Phase 2 additions beyond what spec §3.6 literally asked for**
  (which only specified `timing` + `show_in_table`). Driving the results grid
  fully from the registry needed per-column display formatting (a raw sqlite
  timestamp string → `"11:42"`), fixed decimal precision (`172.0` not
  `172.000000` — Styler needs an explicit format string regardless of the
  underlying value already being rounded), coloring, and a short label distinct
  from the (often long) filter-widget label. This was an implementation
  necessity, not scope creep — flagged here since it's beyond the spec's literal
  text.

**Registry order matters** for derived features: each `compute` lambda can only
reference columns already present, i.e. either a base column or an *earlier*
registry entry.

**To add a new feature:**
1. Append a `FeatureSpec` to `REGISTRY` in `registry.py`, in the right position
   if it depends on another derived feature. Classify `timing` correctly —
   default is `"outcome"` (conservative: triggers the lookahead warning unless
   you deliberately mark it `"pre_open"`).
2. If it's derived from other sessions columns, give it a `compute` lambda.
3. If it needs new *base* aggregation from raw minute bars (not just algebra on
   existing session columns), add that aggregation in `sessions.py`'s
   `_build_rth_base` / `_build_eth_base` first, then reference it.
4. Set `show_in_table=True` (+ `table_label`/`decimals`/`formatter`/`color_kind`
   as needed) if it should appear in the browse table — this is now fully
   automatic, no `dashboard.py` edits required.
5. `rm data/db/lookup.sqlite` (schema change — see warning above) and re-run
   `run_etl.py`. It's now a column in `sessions`, filterable in the sidebar
   (grouped by `timing` then `basis`), and in the results table if flagged.

### Current registry contents
- **Identity/calendar** (`context`): `instrument`, `date`, `weekday` (has a
  `value_order` — see below), `is_half_day`, `seq`
- **RTH base**: `rth_open/high/low/close`, then **`bs_sb`** (whether the RTH high
  or low came first — "SB"/"BS"/"TIE" if same minute; SB = high before low, BS =
  low before high, matching the Buy-Sell/Sell-Buy trading-corpus convention;
  deliberately positioned right after `rth_close` in the registry so it lands
  between "Close" and "High Time" in the results table — table column order
  follows registry declaration order, see `table_features()`), then
  `rth_high_time/low_time` (raw timestamps, not filterable),
  `rth_high_minute/low_minute` (0-389 bar number since 09:30),
  `rth_high_bucket/low_bucket` (30-min bucket label), `rth_range` (derived: high-low),
  then **`rth_range_ma20`** (trailing 20-session average of `rth_range`,
  `.shift(1).rolling_mean(window_size=20, min_samples=20)` — deliberately
  *excludes* today's own range so it's `pre_open`-knowable, unlike `rth_range`
  itself; `decimals=0`, no fractional pts), **`range_vs_ma20_pts`** (today's
  range minus that MA20, `outcome` since it needs today's close) + companion
  `range_vs_ma20_dir` (`up`/`down`/`flat` via `_dir3`, `show_in_table=False`),
  **`abs_range_diff_pts`** (today's range minus *yesterday's* range, `outcome`)
  + companion `abs_range_diff_dir`. All four numeric additions inserted
  immediately after `rth_range` in registry order so they land right after
  "Range" in the results table (Range → Range MA20 → Range vs MA20 →
  Abs. Range Diff), ahead of Gap/Rel Close/Abs Close.
- **RTH close-based twins** (Phase 2 §3.4): `rth_high_close/low_close`,
  `rth_high_close_ts/low_close_ts` + `_minute`/`_bucket`, `rth_range_close`,
  `bs_sb_close`. Not shown in the default table (`show_in_table=False`) —
  additional filter surface, secondary to the extreme-based canonical columns.
- **ETH base**: `eth_open/high/low/close`, `eth_range` (derived)
- **Gap/close derived**: `gap_pts` (today's open − prev close, `pre_open`), `gap_dir`,
  `rel_close_pts` (close−open same day, `outcome`) + `rel_close_dir`,
  `abs_close_pts` (close vs prev close, `outcome`) + `abs_close_dir`
- **Context/cross-session derived** (all `pre_open`): `prev_rel_close_dir`,
  `prev_abs_close_dir`, `prev_bs_sb` (all `.shift(1)` of the same-named
  non-prev column), then **`prev_range_vs_ma20_dir`** + **`prev_range_vs_ma20_pts`**
  and **`prev_abs_range_diff_dir`** + **`prev_abs_range_diff_pts`** (same
  `.shift(1)`-of-the-outcome-column pattern — each is *yesterday's* fully-known
  `range_vs_ma20_pts`/`abs_range_diff_pts` value, which is why it's legitimately
  `pre_open` for today despite the underlying same-day column being `outcome`;
  each dir/pts pair is declared consecutively so the pulldown and its points
  slider render back-to-back in the same sidebar expander), then
  `gap_prevclose_combo`, `open_vs_prev_range` + `open_vs_prev_range_pct`
- **Reserved**: `template` (always NULL — Phase 4 will fill this with session-shape codes)
- **Window bundles** (item 6, spec §3.3), generated by a loop over
  `WINDOW_NAMES = ["first_30m", "first_60m", "first_90m", "hour_10_11"]`
  (`_window_bundle_specs(name)`), appended to `REGISTRY` after everything
  above — **not hand-listed**, so registry and config can't drift, per the
  spec's explicit instruction. Each window gets the RTH block mirrored 1:1,
  prefixed `win_<name>_`: `open/high/low/close`, `bs_sb` (via `_bs_sb_expr(...,
  nullable=True)` — see the null-guard note below), `high_time/low_time`,
  `high_minute/low_minute`, `high_bucket/low_bucket`, `range`, `range_ma20`,
  `range_vs_ma20_pts`+`_dir`, `abs_range_diff_pts`+`_dir`, `gap_pts`+`_dir`
  (that window's own open − **that window's own close, previous session** —
  not the whole session's prev close), `rel_close_pts`+`_dir`,
  `abs_close_pts`+`_dir`, then `prev_win_<name>_bs_sb` /
  `prev_win_<name>_range_vs_ma20_dir`+`_pts` /
  `prev_win_<name>_abs_range_diff_dir`+`_pts` (same `.shift(1)`-of-the-
  window's-own-outcome-column pattern as the whole-RTH `prev_*` block).
  `minute_of_session` keeps its default `session_start="09:30"` (bar count
  anchored to the whole session, not each window's own start), so "High Bar"
  stays comparable across tabs. Every window field is `timing="outcome"`
  uniformly (even a window's own "open," which for `first_30m`/`first_60m`/
  `first_90m` happens to equal `rth_open` and could arguably be `pre_open` —
  simplified to `outcome` uniformly across all four windows rather than
  special-casing the ones that start at 09:30).

  **Closure trap, avoided deliberately**: each window's compute lambdas bind
  their column names as **default arguments**
  (`lambda df, h=col("high_time"): ...`), evaluated eagerly inside
  `_window_bundle_specs(name)` at the time it's called for *that* window — not
  captured by reference to the loop variable, which would make every window's
  lambdas silently resolve to the *last* window in `WINDOW_NAMES`.

  **Null-guard**: `_bs_sb_expr` gained a `nullable=False` param. Its default
  `otherwise("TIE")` is correct when both times are genuinely equal, but wrong
  when both are null (a zero-bar window on a truncated half-day) — window
  `bs_sb` passes `nullable=True`, which wraps the expression so all-null times
  produce `None` instead of `"TIE"`.

  `date`/`weekday`/`is_half_day` are declared once (top of `REGISTRY`, not
  duplicated per window) with `shared_across_tabs=True` instead — they're
  session-level facts true regardless of which tab you're viewing.

  **ETL side** (`src/etl/sessions.py`): `_build_window_base(minutes, name,
  start, end)` mirrors `_build_rth_base`'s aggregation shape exactly, filtered
  to `(session == "RTH") & _in_window(ts, start, end)` instead of just RTH.
  `_in_window` compares `.dt.hour()*60 + .dt.minute()` (both `.cast(pl.Int32)`
  — the usual Int8-overflow guard) against the window's start/end in minutes,
  `closed="both"`. `build_sessions(minutes, half_day_flag_before, windows)`
  takes a new `windows` dict (from `config.toml`'s `[windows]`, passed by
  `run_etl.py`) and left-joins one window base per `WINDOW_NAMES` entry onto
  the RTH+ETH base — `windows[name]` is a direct dict lookup, not a
  `.get(name, default)`: a `KeyError` here means `WINDOW_NAMES` and
  `config.toml` have drifted, and that should fail loudly, not silently. A
  session with zero bars in a window's clock range (e.g. a half-day ending
  before `hour_10_11` even starts) simply produces no row in that window
  base's `group_by` output — the left join (RTH base always on the left)
  correctly nulls out just that window's columns for that session rather than
  dropping the row. Verified directly against the rebuilt DB: a half-day
  session ending at 09:49 has non-null `win_first_30m_*` but null
  `win_hour_10_11_*`, exactly as expected.

  **Config addition**: `hour_10_11 = ["10:00", "10:59"]` added to
  `config.toml`'s `[windows]` (the spec's own example only had `first_30m`/
  `first_60m`/`first_90m`/`last_90m`). `last_90m` stays configured but has no
  registry entries or dashboard tab — declared, not privileged, per spec, and
  simply unused for now per the user's explicit "no need for last 90 for now."

  **Explicitly out of scope for item 6** (deferred to item 7, which needs both
  this and the leg detector): leg aggregates per window
  (`num_legs`/`largest_leg_pts`/etc.), and close-based twins per window
  (`win_<name>_high_close` and friends) — the whole-RTH close-based twins
  (§3.4) were not mirrored per window, so window tabs have no "Show
  close-based HOD/LOD" toggle (see Dashboard section below).

**`value_order`** (Phase 2 addition, small): an optional explicit list on a
`FeatureSpec` overriding the default alphabetical ordering of a `"select"`
filter's dropdown options. `weekday` uses it (`Monday..Sunday`, not
`Friday, Monday, ...`). Applied client-side in `dashboard.py`'s
`_render_one_filter`, not at the SQL level.

## Query layer — `src/query/filters.py`

**Phase 2 rewrite (spec §4.4): offset-based lookup, one mechanism for both
directions.** `query_sessions(conn, filters, instrument, date_range,
display_offset, order_by)`:

- `filters` is now keyed by **`(feature_name, offset)`** tuples, not just
  `feature_name`. `offset` is an integer session count relative to an anchor
  session `s0`, resolved via `seq` (0 = anchor, -1 = previous session, +1 =
  next session, ...).
- Only the offsets actually referenced (by a filter or by `display_offset`) get
  a `LEFT JOIN sessions s_mN/s_pN ON ....seq = s0.seq + N`. The common case
  (no offset filters, `display_offset=0`) is a single-table query with zero joins.
- `instrument` and `date_range` scope the **anchor** `s0` — they're global
  params, not registry-driven filters.
- The `SELECT` returns the row at `display_offset` (default 0 = the anchor
  itself). Rows where the requested offset falls outside recorded history
  (e.g. `display_offset=+1` for the most recent session) are silently dropped.
- Filtering at offset -1 on feature `X` is exactly equivalent to filtering at
  offset 0 on the legacy `prev_X` convenience column, where one exists — tested
  directly in `tests/test_filters.py`.

`build_where()` still looks up each key's `filter_kind` in `REGISTRY_BY_NAME`,
now qualifying the column with its offset's table alias.

## Dashboard — `src/app/dashboard.py`

**One `st.tabs()` row, five tabs** (`WINDOW_TABS`): `(None, "Day Session")` —
the original whole-RTH page, unchanged in behavior — then one tab per
window-bundle tab, `("first_30m", "9:30 - 10:00 Session")`,
`("first_60m", "9:30 - 10:30 Session")`, `("hour_10_11", "10:00 - 11:00
Session")`, `("first_90m", "9:30 - 11:00 Session")`. Each tab is an
**independent clone of the whole Panel A experience** — own gyration-overlay
controls (all 3 size slots), own filters, own results table, own chart — not a
shared control surface, per explicit user choice. `main()` builds only the
**genuinely global** sidebar controls once (Instrument, Chart basis — RTH/ETH,
only meaningful for the Day Session tab — Date range, Lookback, Display
offset, "Hide HalfDay /O/H/L/C columns"), then loops `zip(WINDOW_TABS,
st.tabs(...))` calling `_render_tab(..., window, title)` inside each `with
tab:` block for everything else.

**Why nothing tab-specific lives in `st.sidebar`**: Streamlit has one global
sidebar and `st.tabs()` gives no way to know which tab is currently active
from Python — every tab's body executes on *every* rerun regardless of which
one is visually selected, only display differs. So sidebar content can't vary
by active tab; anything that needs to differ per tab (filters, gyration
settings, table, chart) has to live inside that tab's own body instead.

- **`_col(window, concept)`** — the join between "what to display" and "which
  column that is for this tab." Maps a concept name (`"open"`, `"gap_pts"`,
  `"bs_sb"`, ...) to `rth_open`/`gap_pts`/`bs_sb`/... when `window is None`, or
  `win_<window>_<concept>` otherwise. Used everywhere the chart/info-line code
  needs a column name that depends on which tab is rendering — nothing else
  in the file hardcodes `"rth_open"` anymore except inside `_col` itself.
- **Cached helpers** (`@st.cache_resource` / `@st.cache_data`): DB connection,
  config, instrument list, date bounds, per-column min/max and distinct values
  (all scoped by instrument).
- **`render_filters(conn, instrument, window=None)`** — **Phase 2: fully
  reworked, then made tab-aware for window bundles.** Groups
  `filterable_features(window=window)` by `timing` first (`"Conditioning —
  known at the open"` then `"Outcome — known only at the close ⚠️"`). For the
  Day Session tab (`window=None`), then by `basis` (context/RTH/ETH) as
  sidebar-style expanders, unchanged from before. For a window tab, there's no
  RTH/ETH concept, so all specs for that timing bucket render flattened inside
  one `"Filters"` expander instead of a per-basis split. Renders into whatever
  container is currently active (`st.markdown`/`st.expander`, not
  `st.sidebar.*`) — a plain function call, so it works identically whether
  invoked at top level or inside a `with tab:` block. Each individual filter
  widget is paired with its own compact offset selector (`D-2/D-1/D/D+1`,
  defaulting to `D`), rendered via `st.columns([3, 1])` with the **value
  widget first (left) and offset selector second (right)**. This was a
  deliberate UX call: the spec's "each filter group carries an offset
  selector" was read as *per-filter* (matching §4.4's general mechanism), not
  one offset for a whole basis group, since users need to mix offsets within
  a group (e.g. filter `gap_dir@D` and `bs_sb@D-1` together). Returns
  `(filters, lookahead_active)` — the caller shows a non-blocking warning
  banner if any `"outcome"`-timing filter is active at offset ≥ 0 (spec §6.2).

  ⚠️ **Alignment gotcha**: the offset `st.selectbox` must use
  `label_visibility="hidden"`, **not** `"collapsed"`. `"collapsed"` removes the
  label's space entirely, while the value widget beside it (slider/multiselect/
  selectbox) shows a real visible label above its input — so with `"collapsed"`
  the two controls start at different vertical heights in the same row.
  `"hidden"` reserves the same label height without rendering text, which is
  what actually lines them up.

  ⚠️ **Widget-key collision, hit during this build**: `_render_one_filter`
  takes a `key_suffix` param (`f"_{window or 'day'}"` from `render_filters`)
  folded into every widget key. Without it, `shared_across_tabs` specs
  (`weekday`, `is_half_day`) — which use the *same* `spec.name` in all 5 tabs
  — crashed Streamlit with `StreamlitDuplicateElementKey` the instant a
  second tab's body executed (all tab bodies run every rerun, so the
  collision is immediate, not just-on-visiting). Window-scoped spec names
  (`win_first_30m_gap_pts` etc.) are already unique across tabs for free; this
  only bit the shared ones. Same story for `sessions_table` (now
  `sessions_table_{window or 'day'}`) and every `gyr_size_{i}_*` key (now
  suffixed `_{window or 'day'}` too) — anything with a fixed key string needs
  the tab folded in.
- **`_render_tab(conn, instrument, basis, date_from, date_to, lookback_mode,
  lookback_n, display_offset, hide_ohlc_cols, window, title)`** — one full tab
  body: title → gyration-overlay controls (own copy of the show/mode/
  confirmed-only/close-hilo checkboxes and all 3 size-slot expanders, every
  widget keyed with `tab_key = window or "day"`) → `render_filters(...,
  window=window)` → `query_sessions()` (same call shape as before — `filters`
  is the only thing that differs per tab, since `SELECT s.*` already returns
  every column regardless of which tab asked) → `_apply_lookback()` →
  `build_display_table(rows, hidden_names, window=window)` →
  `st.dataframe(..., key=f"sessions_table_{tab_key}")` → for each selected row
  (capped at `MAX_CHARTS = 20`), `render_session_chart(..., window=window)`.
  Close-based HOD/LOD toggle only renders for `window is None` — window
  bundles have no close-based twins (see registry.py section above).
- **`build_display_table(rows, hidden_names=None, window=None)`** — **Phase 2:
  registry-driven, replaces the old hand-built `display` DataFrame; made
  tab-aware for window bundles.** Iterates `table_features(window=window)`
  (i.e. `show_in_table=True` and `shared_across_tabs or window == window`, in
  registry order), applies each spec's `formatter` to the raw sqlite value,
  uses `spec.display_label` (= `table_label` or `label`) as the column header,
  and builds the `pandas.Styler` format/color rules from
  `decimals`/`color_kind`/`color_map` generically — no per-column-name special
  casing. `_hidden_column_names(window)` maps the "Hide HalfDay/O/H/L/C
  columns" checkbox to that tab's own column names via `_col()`. Column
  alignment is still a small targeted override (`weekday` → left; anything
  `dtype="time"` or `color_kind="enum"` → right) via `column_config`, driven
  by existing metadata rather than hardcoded label strings.
- **`render_session_chart(conn, instrument, row, basis, gyr_settings,
  window=None)`** — compact HTML info line (gap/rel-close/range/BS-SB, small
  font, color-coded via `REGISTRY_BY_NAME["bs_sb"].color_map`, values looked
  up via `_col(window, ...)`) + Plotly candlestick + y-axis forced to plain
  decimal format + a "Full session row" JSON expander (shows **every**
  column regardless of tab — `row` always has the full session, see below).
  For `window is None`: `read_minutes(conn, instrument, date, basis)` (RTH or
  full-day, per the sidebar toggle), HOD/LOD from `rth_high_time`/`rth_low_time`,
  optionally also close-based HOD/LOD. For a window tab: `read_minutes(...,
  "RTH", time_range=config["windows"][window])` always (the RTH/ETH toggle is
  ignored — swings inside a window are always an RTH subset), HOD/LOD from
  that window's own `win_<name>_high_time`/`low_time`, **guarded against
  null** (`if high_time is not None and pd.notna(high_time):` — a window with
  zero bars on a truncated half-day has null times, unlike the whole RTH
  session which always has ≥1 bar). The grey dotted RTH-open reference line
  (`_add_rth_open_line`) is unaffected by `window` — it always shows the
  session's true 09:30 open, which stays a meaningful reference even when
  looking at a narrower window's chart.

  Operates on the **full raw row** (all columns, from the
  `display_offset`-selected session, via `query_sessions`'s `SELECT s.*`) —
  not the table-only subset, and not scoped to the current tab either; only
  *which* columns get read out of it varies by `window`.
- **`read_minutes(conn, instrument, date, basis, time_range=None)`** — added
  `time_range: tuple[str, str] | None`. When given, adds `AND substr(ts, 12,
  5) BETWEEN ? AND ?` to the SQL instead of the `basis` RTH/ETH branch — `ts`
  is TEXT `'YYYY-MM-DD HH:MM:SS'`, and `substr(ts, 12, 5)` pulls the zero-padded
  `"HH:MM"` slice, which compares correctly lexicographically against another
  `"HH:MM"` string. Window tabs always pass their own `(start, end)` from
  `config["windows"][window]`.
- **Gyration overlay** (`_add_gyration_overlay`, `_legs_for_bars`) — Phase 2
  spec §6.3, now **duplicated once per tab** (independent `gyr_settings` dict
  built inside `_render_tab`, not shared): show/hide, mode
  (`close_to_close`/`extreme_to_extreme`), confirmed-only, close-based HOD/LOD
  (Day Session tab only) — plus `gyr_settings["sizes"]`, a list of
  `GYR_N_SIZES` (3) independent layers, each with its own `enabled`
  (`st.checkbox`, only size 1 on by default), `threshold` (`st.select_slider`
  over `config["gyrations"]["thresholds"]`), `color` (`st.color_picker`,
  defaults `GYR_DEFAULT_COLORS` = blue/orange/green), and `show_retracement`
  toggle, each in its own expander ("Size 1/2/3") inside that tab's body.
  `_legs_for_bars` itself needs **no `window` param at all** — it only ever
  sees whatever `bars` DataFrame `render_session_chart` already fetched for
  that tab (window-scoped or whole-RTH), so leg/retracement computation is
  automatically restricted to the tab's own bars with zero changes to the
  detector-calling code. It takes explicit `(bars, mode, threshold, confirmed_only, tiebreak)` params
  (not a whole settings dict) so `_add_gyration_overlay` can call it once per
  enabled size with that size's own threshold. Legs are computed **live** for
  the currently-displayed session's bars — deliberately *not* read from the
  precomputed `gyrations` table (that table only has `close_to_close`/`rth`+`eth`
  precomputed; recomputing over ~390-1400 bars is fast enough to do on every
  rerun regardless of mode/threshold, satisfying spec's "changing the
  threshold must redraw immediately"). Each leg is its own Plotly `Scatter`
  trace (not one combined polyline) specifically so unconfirmed legs can get
  `line=dict(dash="dash")` — Plotly line dash is per-trace, not per-segment.
  Confirmed vs. unconfirmed is now distinguished by dash style alone (solid
  vs dashed) rather than also swapping color, since color is per-size and
  user-chosen — reusing one fixed grey for "unconfirmed" across arbitrary
  per-size colors would be confusing. Magnitude annotation font is `size=13`
  (bumped from 9 for legibility). Retracement zones are reconstructed from
  `leg.start_price`/`deepest_retr_progress`/`deepest_retr_pts` directly (no
  need to re-look-up bar data). Verified via `.js-plotly-plot` element
  inspection in-browser: trace/annotation/shape counts and values move in
  lockstep with leg counts as each toggle/threshold is exercised — confirmed
  three simultaneous layers at different thresholds (e.g. T=120/20/50) render
  distinct, independently-correct leg sets matching direct Python computation.
  **Browser-automation gotcha**: `st.select_slider` is a BaseWeb slider whose
  underlying `input[type=range]` is an ARIA proxy only — dispatching synthetic
  `input`/`change` events on it via raw JS updates the DOM/visual label but
  does **not** reliably commit the new value to Streamlit's session state (the
  chart silently keeps using the old threshold). Click the thumb to focus it,
  then drive it with real `KeyboardEvent`s (`Home`/`ArrowRight`/`ArrowLeft`) —
  that path exercises the component's actual `onKeyDown` handler and commits
  correctly. The `form_input` tool's reliability on this widget was
  inconsistent in testing; keyboard nav after a focusing click was the only
  approach that worked every time.
- **RTH-open reference line** (`_add_rth_open_line`) — grey dotted
  `fig.add_hline` at `row["rth_open"]` spanning the full chart width, price
  labelled top-right via `annotation_position="top right"`. Independent of
  the gyration overlay and of the RTH/ETH basis toggle — always drawn when
  `rth_open` is known, added early in `render_session_chart` (right after the
  candlestick trace, before HOD/LOD markers) so it sits behind them.

### UI conventions established (matters for consistency in new features)
- Color palette now lives in **`features/registry.py`** (`COLOR_POS`,
  `COLOR_NEG`, `COLOR_ZERO`, `COLOR_SB`, `COLOR_BS`) — single source of truth,
  imported by `dashboard.py` rather than duplicated as local constants. Theme
  accent (sliders, radios) is pastel blue `#7EC8E3` via `.streamlit/config.toml`.
- `st.dataframe` + Styler + `column_config` all compose together fine,
  including with `on_select`/multi-row selection — verified working.
- Column alignment in the grid needs **explicit** `column_config` overrides —
  Streamlit's default alignment heuristic isn't reliably guessable from dtype
  alone, and narrow auto-width columns make left vs. right align hard to
  eyeball; widen the column (`width="small"`) when testing alignment changes.
- Adding a theme section to `.streamlit/config.toml` resets unset theme keys
  to Streamlit's light-theme defaults — always set `base = "dark"` explicitly
  alongside any accent color override, or dark mode silently breaks.
- **Streamlit's `st.selectbox` in this version (1.59.0) renders as a react-aria
  `ComboBox`, not a BaseWeb `Select`** — `[data-baseweb="select"]` won't match
  it in browser automation/testing; query `[data-testid="stSelectbox"]` and
  its inner `input[role="combobox"]` / trigger `<button>` instead.
- **Editing `src/features/registry.py` (or other non-main-script modules)
  sometimes doesn't hot-reload cleanly in a running Streamlit dev server** —
  saw a stale-module `AttributeError` for a freshly-added dataclass property
  that was definitely on disk. Full server restart (stop + start, not just
  browser reload) fixed it. If a code change "isn't taking effect," restart
  the server before assuming the edit is wrong.

## Tests — `tests/`

`pytest` (installed in `.venv`, not yet in a requirements file — there isn't
one; deps were installed ad hoc into the venv). Run: `.venv/Scripts/python.exe
-m pytest tests/ -v`. 99 tests, two complementary styles:

- **Fixture-based** (`test_gyrations.py`): small hand-built series, worked out
  by hand against the spec's reference pseudocode so expected outputs are
  known-correct, not just "whatever the code produces." Best for algorithmic
  properties (P1-P8) where you need precise control over the input.
- **Built-DB acceptance tests** (`test_sessions.py`, `test_filters.py`,
  `test_gyrations_db.py`): run against the **real built DB**
  (`data/db/lookup.sqlite`), via a session-scoped `conn` fixture in
  `conftest.py`. Best for invariants that should hold at full-history scale,
  and for catching bugs that only manifest with realistic data volume — the
  `leg_index` collision bug (see `etl/gyrations.py` above) produced a
  **perfectly-passing** fixture-level test suite and only showed up when
  checking row counts against the real 4,461-session table. Neither test style
  alone would have caught everything; both are load-bearing.

## Things a new feature is likely to touch

- **New session-level metric** → `registry.py` (+ `sessions.py` if it needs new
  base aggregation) → delete DB, re-run ETL. Fully automatic in filters +
  table now (no `dashboard.py` edits needed) as long as you set
  `show_in_table`/`decimals`/`formatter`/`color_kind` appropriately.
- **New filter only** (no table column needed) → just the registry entry with
  a `filter_kind` set; `render_filters()` picks it up automatically, grouped
  by its `timing`/`basis`.
- **Window bundles** (item 6, **done** — see registry.py / sessions.py /
  dashboard.py sections above) → adding a **new** window means: add it to
  `config.toml`'s `[windows]`, add its name to `registry.py`'s `WINDOW_NAMES`,
  add its `(window, title)` tuple to `dashboard.py`'s `WINDOW_TABS`, delete DB,
  re-run ETL. Everything else (registry entries, filters, table, chart,
  swings-scoped-to-the-window) follows automatically from those three edits —
  don't hand-list new `FeatureSpec` entries per window, `_window_bundle_specs`
  already loops over `WINDOW_NAMES`.
- **Leg aggregates over windows** (item 7, needs item 6 — now available) →
  `num_legs`, `largest_leg_pts`, etc. per window, computed on demand from
  `gyrations` (or, for `extreme_to_extreme`/other on-demand modes, from a live
  detector call matching the pattern already established in
  `_legs_for_bars`) scoped to the window's bar range — spec explicitly says
  don't flatten these onto `sessions` (too many threshold × window
  combinations).
- **New dashboard tab beyond the per-window views** (Stats/Legs/Lookup, Phase
  2 spec §6.3) → `st.tabs()` now exists (`WINDOW_TABS`), so the prerequisite
  is done; a genuinely new tab (not another window bundle) means extending
  `WINDOW_TABS`'s shape or adding a second `st.tabs()` row, and `_render_tab`
  currently assumes every tab is a Panel-A-style window view — a Stats/Legs/
  Lookup tab would need its own render function, not a `window=` variant of
  `_render_tab`. The "Lookup" tab in particular is mostly wiring the existing
  offset-based `query_sessions` mechanism to a dedicated forward/backward-lookup
  view rather than new query logic.
