# Code Structure & Features — Market Statistics Research v2.0

Single reference document: what the codebase does, how the pieces connect, and
every feature/column/filter exposed by each of the app's 11 pages. Written so
that if this project needs to be reconstructed or debugged from scratch one
day, everything needed is in this one file rather than scattered across
session memory.

The two SPEC files (`Gyrations_lookup_engine_SPEC.md`,
`Gyrations_lookup_engine_SPEC_phase2.md`) remain useful as *design intent*
documents — read them for the "why" behind early decisions — but they are not
kept in sync with what's actually built; this file is.

> **PART 2 coverage status.** Part 1 (architecture, file tree, database,
> shared modules) is current. Part 2 documents pages 1-5 in full; pages 6-12
> (Gyrational Time, Gyrational Range, Day Templates, Hourly Composite,
> Gyrational Stats, Time Waves, Pivots TimeMap) are **not yet written up
> here** — their module docstrings are the reference for now and are unusually
> detailed, including the known-limitation notes that matter for interpreting
> their output.

Each page's feature list below is **fully self-contained** — a column or
concept that's shared across pages (e.g. "BS/SB", "Gap", the gyration
detector) is described again in full under every page that uses it, rather
than pointing elsewhere. The intent is that any single page's section can be
read in isolation.

---

# PART 1 — Code structure and how it's tied together

## High-level architecture

```
Raw per-instrument minute-bar CSVs (external, not in repo)
        │
        ▼
   ETL pipeline (run_etl.py + several standalone run_*.py precompute steps)
        │
        ▼
   data/db/lookup.sqlite  (10 tables — see "Database" below)
        │
        ▼
   src/features/registry.py   <- the single source of truth for every
        │                        `sessions` column: name, dtype, whether/how
        │                        it's filterable, whether/how it's shown
        ▼
   src/query/*.py   <- turns registry metadata + user filter state into SQL
        │
        ▼
   src/app/*.py     <- 11 Streamlit pages (app.py wires them into one
                        multipage app), all reading the same DB/connection
```

The registry is the load-bearing idea in this codebase: adding a new
`sessions` column to `registry.py` (with the right metadata flags) makes it
automatically filterable and/or displayable on **every** page that asks for
`table_features()`/`filterable_features()`, with no page-specific code
changes. Pages that want something registry-driven columns can't express
(per-leg statistics, images, pattern classification, live-recomputed chart
overlays) read directly from the DB or from small page-local query modules
instead — the registry is not used for everything, just for the "one row per
session, one column per fact" majority of the data.

## File tree

```
Gyrations_app/
├── config.toml                     # session/gap/windows/gyrations/net_points/data settings
├── run_etl.py                      # main ETL entry point (raw CSVs -> minutes+sessions+gyrations)
├── run_shapes.py                   # post-ETL: macro shape + pivot-pattern columns (called by run_etl)
├── run_market_profile.py           # post-ETL: POC/Value Area columns (called by run_etl)
├── run_gyrations_continuous.py     # incremental: adds `continuous`-scope gyrations without a full rebuild
├── run_dax_minutes.py              # ingests DAX 1-min into `minutes` (per-instrument RTH tagging)
├── run_bars.py                     # `bars`: clock-aligned 5/10/15/30/60/240-min OHLC
├── run_hour_bars.py                # `session_hour_bars`: RTH-open-ANCHORED hourly bars
├── run_range_bars.py               # `range_bars`: Renko-style fixed-increment bricks
├── run_range_state.py              # `range_state`: multi-timeframe trailing/forward ranges
├── run_day_templates.py            # `day_profiles`: normalised per-day RTH shape profiles
├── requirements.txt
├── .streamlit/config.toml          # Streamlit theme (dark base + pastel-blue accent)
├── .claude/launch.json             # dev-server launch config for the Preview tool
├── data/
│   └── db/lookup.sqlite            # generated store — 10 tables, see "Database" below
├── img/
│   └── MW_patterns/                # M1-M16.png, W1-W16.png — Merrill pattern diagrams
├── src/
│   ├── etl/
│   │   ├── parse.py                # CSV -> normalized minute bars, RTH/ETH tagging
│   │   ├── sessions.py             # minute bars -> one row per session (the registry-driven table)
│   │   ├── gyrations.py            # minute bars -> gyrations-table rows, per scope (rth/eth/continuous)
│   │   ├── leg_windows.py          # per-session leg-count-within-a-window columns (Gyrations v2.0)
│   │   └── load.py                 # write minutes+sessions+gyrations to SQLite
│   ├── features/
│   │   ├── registry.py             # FeatureSpec registry — THE extensibility point (see below)
│   │   └── definitions.py          # time-of-day helper expressions used by sessions.py
│   ├── gyrations/
│   │   ├── detect.py               # pure zigzag leg detector (point threshold) — no Polars/SQLite deps
│   │   ├── time_waves.py           # TIME-based leg detector (bar count) — port of a ProRealTime indicator
│   │   ├── shapes.py                # macro shape classifier (running-HOD/LOD templates)
│   │   ├── day_templates.py        # scale-invariant day-profile builder + template classifier
│   │   ├── gyr_stats.py            # 123 / F231 retracement-zone statistics (causal trend filter)
│   │   ├── range_bars.py           # Renko-style range-bar construction
│   │   ├── range_state.py          # multi-timeframe range state + causal quantile bucketing
│   │   ├── time_patterns.py        # time-of-day pattern helpers (Gyrational Time page)
│   │   ├── pivot_timemap.py        # pivots re-keyed by candle-of-session + slot stats (Pivots TimeMap)
│   │   ├── market_profile.py       # POC / Value Area (time-at-price) algorithm
│   │   ├── merrill.py              # Arthur Merrill M/W 4-leg pattern classification
│   │   └── rainflow.py             # rainflow cycle counting — standalone research module, NOT wired
│   │                                # into any page/ETL step yet (see "Unwired research modules")
│   ├── indicators/
│   │   ├── ma.py                   # pure EMA / DEMA (Mulloy) implementations
│   │   └── sowa_donchian.py        # SowaDonchian indicator, ported from a sibling repo
│   ├── query/
│   │   ├── filters.py              # registry -> offset-aware SQL joins + WHERE clauses (session-level)
│   │   ├── legs.py                 # per-leg aggregate queries (Gyration Legs / Gyrations v2.0 pages)
│   │   ├── gyr_waves.py            # full-history leg + session queries (Gyrational Waves page)
│   │   ├── bars.py                 # `bars` table queries (higher-timeframe OHLC)
│   │   ├── hour_bars.py            # `session_hour_bars` queries (Hourly Composite)
│   │   ├── range_state.py          # `range_state` queries (Gyrational Range)
│   │   ├── day_templates.py        # `day_profiles` queries (Day Templates)
│   │   ├── gyr_stats.py            # ETH bars + RTH session ranges (Gyrational Stats)
│   │   ├── time_waves.py           # minute-bar loading, instrument-agnostic (Time Waves)
│   │   └── pivot_timemap.py        # minute-bar loading w/ date column + session open (Pivots TimeMap)
│   └── app/
│       ├── app.py                  # multipage entry point — wires all 12 pages together
│       ├── dashboard.py            # Day Session page + shared helpers used by every other page
│       ├── legs_page.py            # Gyration Legs page
│       ├── gyrations_v2_page.py    # Gyrations v2.0 page
│       ├── open_normalization_page.py  # OpenNormalisation v1.0 page
│       ├── gyr_waves_page.py       # Gyrational Waves v1.0 page
│       ├── gyr_time_page.py        # Gyrational Time v1.0 page
│       ├── range_page.py           # Gyrational Range v1.0 page
│       ├── day_templates_page.py   # Day Templates v1.0 page
│       ├── hour_composite_page.py  # Hourly Composite v1.0 page
│       ├── gyr_stats_page.py       # Gyrational Stats v1.0 page
│       ├── time_waves_page.py      # Time Waves v1.0 page
│       └── pivots_timemap_page.py  # Pivots TimeMap 1.0 page
├── tests/
│   ├── conftest.py                 # session-scoped sqlite3 connection fixture to the built DB
│   ├── test_sessions.py            # acceptance tests: invariants over the built `sessions` table
│   ├── test_filters.py             # acceptance tests: offset-join mechanism correctness
│   ├── test_gyrations.py           # detector property tests (P1-P8, §2.8 invariant) on hand-built fixtures
│   ├── test_gyrations_extreme.py   # extreme_to_extreme-mode detector tests
│   ├── test_etl_gyrations.py       # tests for the ETL glue (leg_index uniqueness across sessions)
│   ├── test_gyrations_db.py        # same acceptance properties, checked against the full real table
│   ├── test_shapes.py              # macro shape classifier tests
│   ├── test_legs.py                # per-leg query module tests
│   ├── test_market_profile.py      # POC/Value Area algorithm tests
│   ├── test_time_waves.py          # time-based detector: hand-traced state machine + pinned known behaviour
│   └── test_rainflow.py            # rainflow module tests (module itself is unwired — see above)
├── README.md                                # setup, raw-data format, build order
├── Gyrations_lookup_engine_SPEC.md          # original design spec, all phases (historical)
└── Gyrations_lookup_engine_SPEC_phase2.md   # Phase 2 spec: gyrations, windows, offset lookup (historical)
```

Raw CSVs are **not** copied into the repo, and neither is the built database —
`config.toml`'s `[data].raw_dir` points at an external data folder to avoid
duplicating hundreds of MB, and `data/` is gitignored. See README.md for the
expected CSV format.

**Both US30 and DAX are now ingested.** `config.toml`'s `raw_dir` covers US30
(DOW); DAX comes from the sibling `.../DAX-Data` folder via `run_dax_minutes.py`
(the raw CSVs carry the vendor ticker `GER30`, relabelled to `DAX` on ingest —
`run_bars.py` does the same thing when building bars straight from raw).

RTH windows are **per-instrument**, defined in ET: US30 09:30–16:00 (NYSE cash),
DAX 03:00–11:30 (Xetra cash = 09:00–17:30 CET). `config.toml`'s single
`[session]` window is US30's and is used by `etl.parse.tag_sessions`; the
per-instrument map lives in `run_bars.py`'s `RTH_WINDOWS` and is applied at
ingest time by `run_dax_minutes.py`. **`tag_sessions` must not be used for DAX** —
it would apply the NYSE window and mislabel the wrong 6.5 hours as RTH.

## Data flow (ETL)

```
raw CSVs (semicolon-delimited, DD/MM/YYYY HH:MM, decimal comma)
  → parse.parse_all()            concat all CSVs, sort by (instrument, ts), dedupe on ts
  → parse.tag_sessions()         add `date` (ET calendar date) + `session` ("RTH"/"ETH") columns
  → sessions.build_sessions()    aggregate minute bars into one row per (instrument, date),
                                  apply every registry.py derived FeatureSpec in order
  → leg_windows.attach_leg_count_columns()   9 leg-count-in-window columns (needs legs, see below)
  → gyrations.compute_session_scope_legs() / compute_continuous_scope_legs()
        run the pure detector (gyrations/detect.py) per configured (scope, mode, threshold) combo
  → load.write_minutes() / write_sessions() / write_gyrations_rows()   → SQLite
  → run_shapes.main()             (separate script, own connection) macro shapes + pivot patterns
  → run_market_profile.main()     (separate script, own connection) POC / Value Area columns
```

Run via `.venv/Scripts/python.exe run_etl.py` — always a **full rebuild**
(re-reads every raw CSV fresh, `DELETE ... WHERE instrument = ?` then
reinsert for `minutes`/`sessions`; `gyrations` is deleted+reinserted per
`(instrument, scope, mode)` combination it precomputes). `run_shapes.py` and
`run_market_profile.py` run automatically at the end of `run_etl.py` but can
also be run standalone (they only need the DB, not the raw CSVs) — useful
when only their own logic changed.

`run_gyrations_continuous.py` is a **separate, incremental** script (added
2026-08-02) that adds `continuous`-scope gyration legs without repeating the
whole pipeline — it reads the already-parsed `minutes` table straight from
the DB (skipping re-parse of 5.7M raw rows and skipping `run_shapes`/
`run_market_profile`, neither of which are affected by a new gyration scope).
Use this instead of a full `run_etl.py` rerun whenever only `config.toml`'s
`[gyrations].precompute` list gained new scope/mode entries.

⚠️ **Schema changes need a DB delete, not just a rerun.** `load.py`'s
`create_*_table` uses `CREATE TABLE IF NOT EXISTS`, a no-op on an existing
table — it will NOT add new columns or rename/drop old ones. Whenever a
registry field is added/renamed that changes the `sessions` or `minutes`
schema, delete `data/db/lookup.sqlite` before rerunning `run_etl.py`, or
you'll get silent schema drift (stale columns hanging around, new ones
missing). `run_market_profile.py` works around this itself for its own 12
columns by `DROP COLUMN` + `ADD COLUMN` every run (SQLite ≥ 3.35 required) —
that pattern is safe to reuse for any future post-ETL column-adding script.

Current scale: **9.73M minute bars** (US30 5,456,962 from 2009-03-11 to
2026-07-01; DAX 4,268,532 from 2008-09-11 to 2026-07-10) → 4,461 sessions,
260 columns in `sessions`. `gyrations` has ~8.8M rows across all precomputed
(scope, mode, threshold) combinations (`rth`+`eth` `close_to_close`, `rth`
`extreme_to_extreme`, and — as of 2026-08-02 — `continuous` in both modes;
`eth`/`extreme_to_extreme` is the one combination still not precomputed
anywhere).

⚠️ **`sessions` and `gyrations` are US30-only.** The DAX ingest added 1-minute
bars and higher-timeframe `bars`, but not the session-level or leg-level
derived tables. Anything routed through `dashboard.get_instruments()`,
`get_date_bounds()` or `query.gyr_waves.fetch_session_rows()` therefore
silently excludes DAX — those read `sessions`. Pages that must support both
instruments read `minutes`/`bars` directly instead (that is why
`query/time_waves.py` and `query/gyr_stats.py` exist).

## Database — `data/db/lookup.sqlite`

Ten tables. `minutes` and `sessions` have Polars-DataFrame-derived schemas
(any new registry column just works, no DDL to write); everything else is a
hand-written fixed schema.

| table | rows | built by |
|---|---|---|
| `minutes` | 9,725,494 | `run_etl.py` (US30), `run_dax_minutes.py` (DAX) |
| `sessions` | 4,461 | `run_etl.py` (+ `run_shapes`, `run_market_profile`) |
| `gyrations` | 8,811,697 | `run_etl.py`, `run_gyrations_continuous.py` |
| `bars` | 4,301,276 | `run_bars.py` |
| `range_state` | 654,348 | `run_range_state.py` |
| `range_bars` | 367,147 | `run_range_bars.py` |
| `session_hour_bars` | 56,508 | `run_hour_bars.py` |
| `shape_swings` | 17,697 | `run_shapes.py` |
| `session_shapes` | 13,383 | `run_shapes.py` |
| `day_profiles` | 8,944 | `run_day_templates.py` |

### `minutes` (8 columns, 9.73M rows — US30 + DAX)
One row per (instrument, timestamp) raw bar. `instrument, ts, open, high,
low, close, date, session`. `session` is `"RTH"` or `"ETH"` (everything that
isn't RTH), tagged with **that instrument's own** RTH window. `date` is the ET
calendar date. Primary key `(instrument, ts)`.

### `bars` (9 columns, 4.30M rows)
Clock-aligned higher-timeframe OHLC, one row per (instrument, tf_min, bar
start), for tf_min ∈ {5, 10, 15, 30, 60, 240}. Carries `n_min` (how many
1-minute bars actually went into it) and `n_rth_min` (how many of those were
inside RTH), so "complete bars only" and "RTH only" are WHERE clauses rather
than post-filtering. Covers both instruments.

### `sessions` (260 columns, 4,461 rows — US30 only)
One row per (instrument, date) — the whole app's central table, entirely
driven by `src/features/registry.py` (see "Feature registry" below) except
for a handful of columns bolted on by post-ETL scripts (`shape_*`,
`pivot_pattern_*`, `poc`/`va70_hi`/`va70_lo`/etc., and the 9
leg-count-in-window columns). Every column is documented in Part 2 below,
under whichever page(s) actually surface it — this table itself has no
"page," it's the shared substrate every page queries.

### `gyrations` (21 columns, ~8.8M rows)
One row per detected leg. Fixed schema (`etl/load.py`'s
`create_gyrations_table`), **not** registry-driven:
`instrument, scope, threshold, mode, leg_index, confirmed, start_ts, end_ts,
start_date, end_date, start_price, end_price, direction, magnitude_pts,
duration_min, midprice, deepest_retr_pts, deepest_retr_pct_final,
deepest_retr_progress, deepest_retr_start_ts, deepest_retr_end_ts`.

- `scope`: `"rth"` (per-session, resets at the RTH boundary), `"eth"`
  (per-session, resets at the calendar-day boundary but uses all bars), or
  `"continuous"` (one detector pass over the *entire* history — a leg can
  span multiple calendar dates; `start_date`/`end_date` can differ, unlike
  `rth`/`eth` where they're always equal).
- `mode`: `"close_to_close"` (zigzag on 1-min closes only) or
  `"extreme_to_extreme"` (zigzag on bar highs/lows, needs a `tiebreak` rule
  for ambiguous bars — see detector section below).
- `threshold`: one of the 14 configured point-thresholds (10 through 200).
- `confirmed`: whether this leg's magnitude already clears `threshold` (every
  leg does, by construction, except possibly the still-forming trailing leg
  at the end of the series, and — for `close_to_close` mode only — the very
  first/seed leg in rare cases).
- `deepest_retr_*`: the deepest adverse retracement ("elasticity") that
  occurred *during* this leg without breaking it — points, % of the leg's
  final magnitude, and progress-through-the-leg fraction, plus the ts range
  it happened in.

PRIMARY KEY `(instrument, scope, threshold, mode, leg_index)` — no `date`
component, which is why `leg_index` must be globally unique across sessions
within one `(scope, threshold, mode)` series (see the "leg_index collision"
gotcha further down).

### `session_shapes` (9 columns, 13,383 rows)
One row per (instrument, date, threshold) for threshold ∈ {40, 120, 200}:
`instrument, date, threshold, shape, swings, n_swings, n_legs, fade_pts,
last_extreme_ts`. Written by `run_shapes.py` from confirmed
`rth`/`extreme_to_extreme` legs — see `gyrations/shapes.py`'s macro-shape
algorithm, described under Gyrations v2.0 in Part 2.

### `shape_swings` (11 columns, 17,697 rows)
One row per macro swing (the "yellow-line" segments feeding
`session_shapes`): `instrument, date, threshold, swing_index, direction,
start_ts, end_ts, start_price, end_price, size_pts, duration_min`.

## The feature registry — `src/features/registry.py`

The single place that defines every column in `sessions`, and what makes new
features "just work" across the ETL, filters, and every page's results table
without touching page code. Each feature is a `FeatureSpec`:

```python
FeatureSpec(
    name, dtype, basis, filter_kind, label,
    compute=None,           # lambda df: pl.Expr, for derived (non-base) features
    timing="outcome",       # "pre_open" | "outcome"
    show_in_table=False,    # include as a column in a page's results grid?
    formatter=None,         # lambda raw_value: display_value (e.g. HH:MM from a timestamp)
    decimals=None,          # fixed decimal places for numeric display
    color_kind=None,        # "pts" (sign-based green/red/grey) | "enum" (value->color map) | None
    color_map=None,         # required when color_kind="enum"
    table_label=None,       # short header for the grid; falls back to `label`
    value_order=None,       # explicit dropdown order for "select" filters (else alphabetical)
    value_labels=None,      # raw stored value -> full display text, dropdown only
    value_sort_key=None,    # computed sort key for domains too large/irregular for value_order
    window=None,            # None = whole-RTH; else which config-declared window this belongs to
    shared_across_tabs=False,
    max_col_width=None,     # caps the auto-sized results-grid column width (px)
)
```

- **`dtype`**: `"numeric" | "categorical" | "boolean" | "time"`
- **`basis`**: `"RTH" | "ETH" | "context"` — groups filters in the sidebar
- **`filter_kind`**: `"range" | "select" | "bool" | None` (`None` = not
  filterable — e.g. raw price levels, which are close to meaningless as an
  absolute filter across 17 years of a 7×+ price-level change)
- **`compute`**: only set for *derived* features, applied in
  `sessions.py`'s `_apply_derived()` in registry order (each `compute` can
  reference any base column or any *earlier* derived column). Features
  without `compute` ("base" features) come straight from the RTH/ETH/window
  aggregations in `sessions.py`.
- **`timing`**: `"pre_open"` = knowable before the session opens (e.g.
  `weekday`, `gap_pts`, every `prev_*` column). `"outcome"` = only knowable
  at/after the close. Intrinsic to the feature at offset 0 — a *prior*
  session viewed at a negative offset is always safe regardless of its own
  timing (that's a query-time concern, not registry metadata).
- **`show_in_table`/`formatter`/`decimals`/`color_kind`/`color_map`/
  `table_label`**: drive the results grid (a pandas Styler + `column_config`)
  fully from the registry — no per-page column-formatting code.

**Registry order matters**: a `compute` lambda can only reference columns
already present (an earlier registry entry or a base column).

**Window bundles**: `WINDOW_NAMES = ["first_30m", "first_60m", "hour_10_11",
"first_90m", "last_90m"]` (from `config.toml`'s `[windows]`) each get a
mirrored block of the whole-RTH fields, generated by a loop
(`_window_bundle_specs`), prefixed `win_<name>_` — not hand-listed, so the
registry can't drift from config. Only a curated subset of each window's
fields is actually surfaced as a table column/filter (`WINDOW_DISPLAY` dict);
`first_60m` has no display entry at all and stays fully dormant (computed,
stored, just never shown) — same as `last_90m`'s fields being computed but
window-tabs-never-built.

**Helper functions worth knowing**: `_dir3(pts_col)` → up/down/flat from a
signed points column; `_hilo_flag_expr(col, n)` → 1/-1/0 highest/lowest-of-
trailing-n-sessions flag; `_bs_sb_expr(high_ts, low_ts)` → SB/BS/TIE from
which extreme's timestamp came first; `_prev_n_low`/`_prev_n_high` → rolling
min/max over the *n* sessions before today (excluding today); moon-phase
helpers (`_moon_age_expr`/`_moon_phase_expr`) — pure calendar arithmetic, no
external data, always `pre_open`.

**To add a new feature**: append a `FeatureSpec` (right position if it
depends on another derived feature), classify `timing` correctly (default
`"outcome"` is conservative — triggers the lookahead warning unless
deliberately marked `pre_open`), add new base aggregation in `sessions.py`
first if needed, set `show_in_table=True` + display metadata if it should
appear in a table, delete the DB, rerun `run_etl.py`.

⚠️ **Known gotcha**: Polars' `.dt.hour()`/`.dt.minute()` return **Int8** in
this environment — `hour() * 60` silently overflows. Always `.cast(pl.Int32)`
before multiplying (see `_in_window` in `etl/sessions.py` for the pattern).

## The leg detector — `src/gyrations/detect.py`

Pure algorithm module — **no Polars, no SQLite, no timestamp dependency at
all**, deliberately, so it's directly unit-testable against small hand-built
fixture series. Operates on a plain `list[float]` (closes) or list of OHLC
tuples, returns `Pivot`/`Leg` objects addressed by integer index.

- **`close_to_close` mode** (`detect_legs_close_to_close`): zigzag on 1-min
  closes only. Two-sided seeding (tracks a running high *and* low until the
  first threshold-sized reversal establishes direction). A leg is
  `confirmed` iff `magnitude_pts >= threshold` — true by construction for
  every interior/trailing leg; the seed leg is the one genuinely interesting
  case where it can legitimately be `False`. Deepest-retracement tracking
  holds the invariant `deepest_retr_pts < threshold` for every confirmed leg
  by construction (the same running-extreme tracking that ends a leg would
  have already ended it at the retracement point otherwise).
- **`extreme_to_extreme` mode** (`detect_legs_extreme_to_extreme`): zigzag on
  bar highs (up-leg extension) / lows (down-leg extension). A single bar can
  extend the current leg *and* reverse it in the opposite sense — OHLC alone
  can't say which happened first, so an `intrabar_tiebreak` config setting
  resolves it: `"bar_direction"` (default — `close >= open` ⇒ low tested
  before high, else high before low), `"adverse_first"`, or
  `"favourable_first"` (both fall back to `bar_direction` while direction is
  unestablished). ⚠️ Known pre-existing bug (not yet fixed): the §2.8
  retracement invariant can break in this mode, ~64% of trials with
  `adverse_first`/`favourable_first`, ~0.5-0.6% even with the default
  `bar_direction` — doesn't affect any *stored* data since `eth`/
  `extreme_to_extreme` is the one scope×mode combination never precomputed,
  but does affect any page that live-recomputes legs in that mode.
- **ETL glue** (`src/etl/gyrations.py`): `compute_session_scope_legs` (rth/eth
  — one independent detector pass per session, `leg_index` renumbered
  globally across sessions afterward) and `compute_continuous_scope_legs`
  (one pass over the whole per-instrument series). ⚠️ Real bug caught here
  once: without the global renumbering, every session's leg 0 collided with
  every other session's leg 0 on the primary key and `INSERT OR REPLACE`
  silently overwrote most of the table — no exception, just quietly wrong
  data. Lesson still worth repeating: after any bulk ETL write, spot-check
  row *counts* against an independent estimate.
- **A separate, non-bug surprise**: fixed point-thresholds produce noticeably
  fewer legs/session on the full 17-year history than on a recent-only
  sample (DOW's price level went from ~7,000 in 2009 to ~52,000+ by 2026, so
  a fixed point threshold represents a much smaller relative move now than
  then) — expected, not something to fix.

## Other gyration-derived modules

- **`src/gyrations/shapes.py`** — macro shape classifier. Given a session's
  confirmed `rth`/`extreme_to_extreme` legs, walks the alternating pivot
  sequence and keeps only pivots that were a *running* session extreme at
  their time (a counter-swing that never breaks the prior running high/low
  gets absorbed into the bigger move). Names the resulting swing string:
  `/` (up only), `\` (down only), `A` (up-down), `V` (down-up), `N`
  (up-down-up), `\/\` (down-up-down), `M` (up-down-up-down), `W`
  (down-up-down-up), `M+k`/`W+k` for longer sequences, `flat` for zero legs.
  Run post-ETL by `run_shapes.py` at thresholds 40/120/200, written to
  `session_shapes`/`shape_swings` and flattened onto `sessions` as
  `shape_40/120/200`. The same run also derives **PivotPattern** — a
  *different*, simpler concept: one `'1'`/`'0'` digit per confirmed leg (not
  merged macro swings), `'1'` if that leg's own end price is ≥ the session's
  RTH open else `'0'` — written to `pivot_pattern_40/120/200`.
- **`src/gyrations/market_profile.py`** — Point of Control / Value Area, a
  real time-at-price expansion (not the fixed-±35%-of-range approximation
  from the ProRealTime script it was modeled on). Bins a session's 1-min
  closes into price buckets scaled to 0.05% of the session's own RTH open
  (keeps bucket granularity comparable across DOW's 7k–52k price history);
  POC = the busiest bucket; Value Area expands outward from POC one bucket
  at a time (whichever adjacent side has more bars) until ≥70% of the
  session's bars are covered. Also classifies today's Value Area against
  yesterday's into one of 6 symbolic (not magnitude) relationship codes:
  `"1"`/`"-1"` (shifted, overlapping), `"0"` (contained), `"11"`/`"-11"`
  (shifted, no overlap), `"111"` (engulfs). Run post-ETL by
  `run_market_profile.py`, columns flattened onto `sessions`.
- **`src/gyrations/merrill.py`** — Arthur Merrill M/W 4-leg pattern
  classification. Described in full under Gyrational Waves v1.0 in Part 2
  (its only consumer).
- **`src/etl/leg_windows.py`** — the 9 leg-count-in-window columns
  (`bs_sb_legs_*`, `first_legs_*`, `last_legs_*` × thresholds 40/120/200),
  described under Gyrations v2.0 in Part 2.

## Unwired research modules

- **`src/gyrations/rainflow.py`** — ASTM E1049 4-point rainflow cycle
  counting (metal-fatigue algorithm, applied to price paths). Its defining
  property vs. the zigzag detector: it needs no size threshold and preserves
  nested cycles at every scale simultaneously (a small oscillation riding on
  a large excursion is extracted as its own cycle while the large excursion
  survives intact). Has its own test file (`tests/test_rainflow.py`) but is
  **not called from any ETL script or page** — pure research module, kept
  for a possible future scale-free-decomposition feature.

## Query layer

- **`src/query/filters.py`** — `query_sessions(conn, filters, instrument,
  date_range, display_offset, order_by)`. `filters` is keyed by
  `(feature_name, offset)` tuples; `offset` is an integer session count
  relative to an anchor session `s0`, resolved via the `seq` registry column
  (0 = anchor, -1 = previous session, +1 = next session, …). Only the offsets
  actually referenced get a `LEFT JOIN sessions s_mN/s_pN ON
  ....seq = s0.seq + N` — the common case (no offset filters, default
  display) is a single-table query with zero joins. `instrument`/
  `date_range` scope the anchor `s0` directly, not via registry filters.
  `build_where()` looks up each filter's `filter_kind` in `REGISTRY_BY_NAME`
  to build the right SQL clause (`BETWEEN`, `IN (...)`, `= ?`).
- **`src/query/legs.py`** — per-leg queries scoped by an *explicit list of
  dates* (not a range), used by the Gyration Legs and Gyrations v2.0 pages:
  `leg_aggregates_by_date` (count/sum/avg/avg-duration per date),
  `leg_pivots` (raw start/end pivots per leg), `leg_pair_aggregates_by_date`
  (pairs consecutive legs 0&1/2&3/… into "gyrations", averages both legs'
  size/duration per pair), `leg_detail_rows` (one row per leg with
  direction/pattern classification V1/V2/A1/A2/ratios/gyration-size — backend
  only for the Leg Detail Filters on Gyrations v2.0). Only `rth`/
  `close_to_close` and `rth`/`extreme_to_extreme` are usable here — `eth` and
  `continuous` scope are not (not what this module was built for).
- **`src/query/gyr_waves.py`** — full-history queries for the Gyrational
  Waves page (no `dates` list — it studies the *entire* leg sequence for one
  `(instrument, scope, threshold, mode)` at a time): `fetch_legs` (every leg,
  oldest first), `fetch_session_rows` (lightweight per-day table columns),
  `fetch_full_session_row` (every `sessions` column for one date, feeds the
  candlestick chart).

## App layer — `src/app/app.py` + `dashboard.py`

`app.py` is the actual entry point (`.venv/Scripts/streamlit.exe run
src/app/app.py`). Uses function-reference `st.Page` (not path-string) so
every page module is imported once by plain top-level name — keeps
`st.cache_resource`'s cache key (keyed on function `__module__`) from
fragmenting into independently-cached DB connections per page. All 11 pages
therefore share **one** cached SQLite connection.

`dashboard.py` is both the Day Session page *and* the shared-helpers module
every other page imports from directly (`from dashboard import ...`) —
`get_connection`, `get_config`, `get_instruments`, `get_date_bounds`,
`inject_shared_css`, `read_minutes`, `render_global_controls`,
`render_filters`, `render_gyration_controls`, `build_display_table`,
`render_session_chart`, `_apply_lookback`, and a few underscore-prefixed
internals (`_add_rth_open_line`, `_pts_color`, `_enum_color`) that other page
modules import directly too — this codebase treats a leading underscore as
"not part of Day Session's own public page," not as a hard cross-module
privacy boundary.

### UI conventions established (matters for consistency in any new page)
- Color palette lives in `features/registry.py`: `COLOR_POS`/`COLOR_NEG`
  (soft green/red, sign-based), `COLOR_ZERO` (grey/neutral), `COLOR_SB`
  (yellow/orange), `COLOR_BS` (pastel blue) — single source of truth,
  reused verbatim on the Gyrational Waves page's pattern-name coloring too.
- `st.dataframe` + pandas Styler + `column_config`, with `on_select="rerun"`/
  multi-row selection, is the standard results-table pattern across every
  page with a table.
- Streamlit checkboxes/radios render their real `<input>` visually hidden
  (a clip-path accessibility trick) — coordinate-based browser-automation
  clicks on them are unreliable; a real `.click()` call on the actual input
  element works.
- Editing a non-main-script module (`registry.py`, a page module) sometimes
  doesn't hot-reload cleanly in a running dev server — a full restart
  (stop + start, not just browser reload) fixes a "change isn't taking
  effect" symptom before assuming the edit itself is wrong.

---

# PART 2 — Sub-apps and their features

Five pages, registered in `src/app/app.py`'s `st.navigation([...])` in this
order. Each section below is self-contained.

## 1. Day Session (`src/app/dashboard.py`, url path `day-session`)

The original page — filter/browse RTH sessions, click through to a
candlestick chart with a live gyration overlay. Every other page either
extends this page's filter/table machinery or borrows its chart-rendering
helpers.

**Global controls** (top of page, shared instrument/date-range state):
- **Instrument** — selectbox (currently only `US30`).
- **Chart basis** — RTH / ETH radio, controls which bars the candlestick
  chart shows (independent of any gyration scope).
- **Date range** — date-range picker, defaults to the instrument's full
  history.
- **Lookback** — "All" / "Last N occurrences" / "Trailing months", applied
  *after* filtering, before stats/display.
- **Display offset** — D-2/D-1/D/D+1: which session (relative to whatever
  session matched the filters) actually gets displayed/charted.
- **Hide HalfDay /O/H/L/C columns** — checkbox, hides half-day flag + raw
  OHLC columns from the results grid.

**Filters**, grouped first by timing (Conditioning — known at the open /
Outcome — known only at the close ⚠️), then by category:

*Session Context* (2-per-row): Weekday (select), Half day (bool), Moon age
in days since new moon (range slider), Moon phase (8-way select: New Moon,
Waxing Crescent, First Quarter, Waxing Gibbous, Full Moon, Waning Gibbous,
Last Quarter, Waning Crescent).

*RTH filters — known at the open*: Gap direction (up/down/flat, from today's
open vs. the previous session's close); Open highest/lowest of last 3 and of
last 5 sessions (3-way enum); Open minus previous day's Point of Control
(range); Open vs. previous day's Value Area (above/inside/below); Prev. rel.
close direction, Prev. abs. close direction, Prev. BS/SB (yesterday's values
of today's outcome-timing equivalents); Prev. RangeMA20 difference
(direction + points); Prev. Abs. Range difference (direction + points);
**Prev. AbsWkClose** (range) + **Prev. AbsWkClose direction** (up/down/flat)
— yesterday's value of "today's close minus the close of the last trading
day of the *previous calendar week*"; **Prev. RelWkClose** (range) +
**Prev. RelWkClose direction** — yesterday's value of "today's close minus
the *opening* price of the first trading day of *this* calendar week"; Gap ×
prev-close combo (categorical combination of the two); Open vs. previous
RTH range (below/inside/above) + Open position in that range (0=low,1=high,
continuous); same pair again for the previous **3** sessions' combined range
and the previous **5** sessions' combined range.

*RTH filters — known only at the close*: High/Low/Close highest-or-lowest of
last 3 and of last 5 sessions (3-way enum each); RTH Range (points); Range
vs. its own trailing-20-session MA (points, sign-colored); Abs. Range Diff
vs. the previous session's range (points, sign-colored); **AbsWkClose**
(range) + **AbsWkClose direction** — today's close minus the previous
calendar week's last close; **RelWkClose** (range) + **RelWkClose
direction** — today's close minus this calendar week's own opening price;
Rel. close (close − open, points); Abs. close (vs. previous close, points);
RTH High/Low (close-based twin, i.e. the highest/lowest *close* of the
session rather than wick extreme) + their times/bars/buckets; BS/SB
(close-based twin); RTH High/Low Bar (bar-of-session index of the extreme);
Point of Control / Value Area block: Close minus today's own POC, Close vs.
today's own Value Area (above/inside/below), Value Area width, Value Area
width vs. previous day's, today's Value Area vs. previous day's (6-way
symbolic code), plus RTH High/Low vs. *previous* day's Value Area; Close vs.
previous RTH range (and vs. previous-3 / previous-5 combined ranges) +
position-in-range percentage, mirroring the Open-side filters above but
using today's close instead of open.

*Window filter groups* (one expander each — "930-1000 filters", "1000-1100
filters", "930-1100 filters", "1430-1600 filters" — corresponding to
config-declared clock windows `first_30m`, `hour_10_11`, `first_90m`,
`last_90m`): each window exposes its own BS/SB (SB/BS/TIE, nullable on a
zero-bar truncated window), Range, Range vs. its own trailing-20-session MA,
Rel. close, and (930-1000 window only) Abs. close — plus, in the "known at
open" timing group, that window's own Prev. BS/SB / Prev. RangeMA20
difference (direction + points) / Prev. Abs. Range difference (direction +
points), each being *yesterday's* fully-known value of that window's own
outcome column.

**Results table columns** (in this order): Date, Day, MoonAge, Moon, Open,
High, Low, Close, BS/SB (color-coded orange=SB/blue=BS), High Time, Low
Time, High Bar, Low Bar, Range, Range MA20, Range vs MA20 (points,
sign-colored), Abs. Range Diff (points, sign-colored), Gap (points,
sign-colored), **AbsWkClose** (points, sign-colored), **RelWkClose**
(points, sign-colored), Rel. Close (points, sign-colored), Abs. Close
(points, sign-colored), Open/High/Low/Close highest-or-lowest-of-3 flags,
Open/High/Low/Close highest-or-lowest-of-5 flags (all enum-colored
blue=highest/orange=lowest), then whichever window columns each active
window's `WINDOW_DISPLAY` entry surfaces (BS/SB, Range, RangeMA20, Range vs
MA20, Rel. Close, and for the 930-1000 window only, Abs. Close).

**Stats block** below the table: three sections side by side per row (RTH,
930-1000, 1000-1100, 930-1100, 1430-1600 — whichever windows are active),
each showing BS/SB count+pct split, and sign-split (</≥0) count+pct lines for
Rel. Close, Abs. Close, Range-vs-MA20, and Gap — the larger side of each
line colored, the smaller left neutral.

**Gyration overlay controls**: Show gyrations toggle; Mode select
(close_to_close / extreme_to_extreme); Confirmed-legs-only toggle; Show
close-based HOD/LOD comparison markers toggle; three independent size slots
("Size 1/2/3"), each with its own Enabled checkbox, threshold slider (from
the 14 configured thresholds), color picker (default blue/orange/green), and
Show retracement zone toggle.

**Chart** (per selected row, up to 20 charts at once): compact info line
(Gap / Rel Close / Range / BS-SB, color-coded); Plotly candlestick from
`read_minutes` (RTH or ETH per Chart basis); grey dotted RTH-open reference
line; green/red HOD/LOD triangle markers (and, if toggled, close-based
HOD/LOD as hollow markers); the gyration overlay (one line+markers trace per
confirmed/unconfirmed leg per enabled size slot, magnitude annotated,
optional shaded retracement-zone rectangle) computed **live** from that
session's own bars (not read from the precomputed `gyrations` table, so mode/
threshold changes redraw instantly); a "Full session row" JSON expander
showing every column of that session regardless of what's shown in the table.

## 2. Gyration Legs (`src/app/legs_page.py`, url path `gyration-legs`)

Aggregate leg/pivot analysis layered on top of Day Session's own filters —
same global controls, same Session Context + RTH filters groups (all of Day
Session's filters described above apply here too), plus its own additional
filter group and its own table/charts. Only `scope="rth"`/
`mode="close_to_close"` legs are used (the only combination the page is built
around; the Mode selector is hidden/fixed for this page).

**Additional "Timing filters" group** (on top of everything Day Session
already offers): High/Low time diff in minutes (`|High bar − Low bar|`,
range); HLtimeDiff vs. previous session (did today's High/Low time-gap grow
or shrink vs. yesterday's, enum); RTH High bar-seq vs. previous session's
(range); RTH Low bar-seq vs. previous session's (range); HtimePrevHtime vs.
LtimePrevLtime (which one grew more, enum) — all `show_in_table=False` on Day
Session, surfaced only here.

**Gyration controls**: same 3-size-slot mechanism as Day Session (Enabled /
threshold / color / retracement toggle each), but Mode is fixed to
`close_to_close` (no selector) so the page's RHLW columns can't quietly
mismatch what's displayed. Plus: "Show Size 1/2/3 on charts" checkboxes (3,
one per slot) and a "Show distance-from-open in percent" toggle (switches
both scatter charts and the info line between points and percent).

**Results table columns**: Date, Day, MoonAge, Moon, BS/SB, High Time, Low
Time, High/Low time-diff metrics (the 5 Timing-filter columns above, all
shown here even though `show_in_table=False` on Day Session — this page
passes its own explicit column list), Range, Range MA20, Range vs MA20, Gap,
Rel. Close, Abs. Close — plus, for **every** configured threshold from 30pt
up to 200pt (11 thresholds: 30/40/50/60/70/80/90/100/120/150/200), a block of
5 dynamically-generated columns: `RHLW{T}#` (leg count that day at that
threshold), `RHLW{T}Sum` (total points), `RHLW{T}Avg` (average leg size),
`RHLW{T}Avg%` (average leg size as % of that day's open), `RHLW{T}AvgT`
(average leg duration in minutes) — 55 columns total from this one block,
independent of which size slots are enabled (that only controls the charts).

**Charts**: "Session High/Low vs time of day" — scatter of every filtered
session's own RTH High (green) and Low (red) plotted at their time-of-day,
distance from that session's open on the y-axis (points or %). "Leg pivots
vs time of day" — scatter of every leg's start+end pivot (one color per
enabled *and* "shown on chart" size slot) — by construction this includes
every session's own high/low too, since the largest legs' pivots are the
session extremes.

**Per-session chart** (row selection, up to 20 at once): identical
`render_session_chart` mechanism as Day Session (candlestick + open line +
HOD/LOD + live gyration overlay for the enabled size slots).

## 3. Gyrations v2.0 (`src/app/gyrations_v2_page.py`, url path `gyrations-v2`)

Extreme-to-extreme leg analysis at 3 fixed sizes (40/120/200 points) — no
mode selector, no threshold selector for the overlay (fixed sizes only).
Reuses Day Session's global controls + Session Context/RTH filter groups.

**Morning card** (always shown, above the filters): a character forecast for
the *next* session using only information known at the latest session's
close. Classifies the latest session into STRONG-CHOP (≥10 confirmed
`rth`/`extreme_to_extreme` legs at T=120 *and* last swing ≥450pts),
STRONG-TREND (≤3 legs and last swing <300pts), or NEUTRAL, then reports
P(next session is one-way) / P(next session has 2+ macro swings) / average
macro-swing count, conditioned on that bucket over a trailing ~3-year window
(756 sessions) vs. the unconditional base rate — with an explicit caption
that this is validated character-only forecasting, direction is not
predictable from prior-day info.

**Additional filters** (`legs_filters_v2` group, on top of everything Day
Session offers): `bs_sb_legs_40/120/200`, `first_legs_40/120/200`,
`last_legs_40/120/200` (range) — confirmed-leg counts fully contained within
three clock windows per session (the BS/SB window = between the session's
high and low, whichever came first to whichever came second; the "First"
window = open to whichever of high/low came first; the "Last" window =
whichever of high/low came second to the actual close); `shape_40/120/200`
(select, macro shape template — `/`, `\`, `V`, `A`, `N`, `\/\`, `M`, `W`,
`M+k`/`W+k`, `flat`); `pivot_pattern_40/120/200` (select, the per-leg 1/0
above-or-below-open digit string, custom sort by digit-count then descending
binary value).

**Leg Detail Filters** (own expander, tripled per fixed threshold 40/120/200,
computed live from `leg_detail_rows`, not persisted columns): Direction
(up/down select); Pattern (1st / V1 / V2 / A1 / A2 — V=up-leg
bigger/smaller-than-previous, A=down-leg bigger/smaller, "1st" = no previous
leg that session); Start/End time of day in minutes since 09:30 (range);
Start/End price relative to that session's open (range); Duration in minutes
(range); Size in points and in % of open (range each); Time ratio and size
ratio vs. the previous leg (range each); Gyration size in points — this
leg's magnitude plus the previous leg's, only defined for the *second* leg
of a 0&1/2&3/… pair (range). A session shows up if **at least one** leg at
that threshold satisfies each active filter independently.

**Gyration overlay controls**: Show gyrations toggle; Mode fixed to
`extreme_to_extreme` (no selector); Confirmed-legs-only toggle; Show
close-based HOD/LOD toggle; three **fixed**-threshold slots (T=40/120/200,
not user-selectable), each with Enabled / color / retracement-zone toggle.

**Results table columns**: Date, Day, MoonAge, Moon, BS/SB, Shape40/120/200,
PivotPattern40/120/200, High Time, Low Time, the 5 High/Low timing-metric
columns (same as Gyration Legs), the 9 leg-count-in-window columns
(BS/SBLegs40/FirstLegs40/LastLegs40, ×3 for 120/200), Range, Range MA20,
Range vs MA20, Gap, Rel. Close, Rel. High (high − open, points — v2-only
column), Rel. Low (low − open, points — v2-only column, comparative color
with Rel. High: whichever moved further from open in absolute terms gets the
blue accent), Abs. Close — plus, for each of the 3 fixed thresholds, the same
5-column RHLW block as Gyration Legs (`RHLW{T}#/Sum/Avg/Avg%/AvgT`)
immediately followed by `Gyra{T}Avg`/`Gyra{T}AvgT` (pair-of-legs average
size/duration, same 0&1/2&3/… pairing as the Leg Detail Filters' gyration
size).

**Chart**: "Selected sessions relative to open" — one line per selected
session, that session's own intraday close path normalized so its own RTH
open sits at 0, letting several days' shapes be compared directly.

**Per-session chart** (row selection, up to 20 at once): same
`render_session_chart` mechanism, gyration overlay restricted to the 3 fixed
sizes.

## 4. OpenNormalisation v1.0 (`src/app/open_normalization_page.py`, url path `open-normalisation`)

Deliberately v1/minimal, per explicit user request: **chart only, no filters,
no stats, no results table.** A continuous, gap-free RTH price chart with
every session's own Open normalized to 0.

**Controls**: Instrument selectbox. Chart settings: Chart type
(Candlestick/Bar/Line — Line uses WebGL `Scattergl` for performance since
this can be 50-100k+ points, since Candlestick/Ohlc have no WebGL variant in
Plotly); Up/Down colors; Show SowaDonchian toggle (period 20, built on 5-min
bars aggregated from RTH 1-min bars) + its own up/down average-line colors;
three gyration-overlay checkboxes for T=40/120/200 (`rth`/
`extreme_to_extreme`, reusing the same precomputed `gyrations` table
Gyrations v2.0 uses) + their colors.

**Chart mechanics**: concatenates the last 1 calendar year of RTH-only 1-min
bars back-to-back with **no** overnight/weekend gaps (each session's bars
just follow the previous session's on the x-axis, indexed by integer
position not real time) — scrolling/zooming horizontally browses every
session continuously. Every bar's O/H/L/C has that session's own `rth_open`
subtracted, so every session's open sits at 0 regardless of the instrument's
actual price level at the time (lets 2009-era and 2026-era sessions be
visually compared directly). Alternating faint per-session background shade;
x-axis ticks at each new calendar month. Fixed vertical range ±500 points
(pan manually to see excursions beyond it, not auto-ranged). Initial view
shows the last 10 sessions; `scrollZoom` enabled for pinch/scroll zooming.
Gyration overlay legs are drawn as `Scattergl` line segments (one trace per
confirmed/unconfirmed state across the whole displayed range, `None`-
separated segments — not one trace per leg, since there can be thousands).
SowaDonchian is computed once over the instrument's **full** RTH history
(not just the displayed year — its adaptive lookback needs real history to
behave correctly) and cached process-wide.

## 5. Gyrational Waves v1.0 (`src/app/gyr_waves_page.py`, url path `gyrational-waves`)

Arthur Merrill M/W 4-leg pattern classification, studied over the **full**
leg sequence (not per-session) for one `(instrument, scope, threshold,
mode)` combination at a time. Deliberately has **none** of the other pages'
session-level filters (no weekday/gap/BS-SB filters etc.) — only this page's
own controls.

**The pattern math** (`src/gyrations/merrill.py`): a "pattern" is 4
consecutive legs (5 pivots, P0…P4 — P0 the pattern's start, P4 the end of
the "observed" 4th leg). Every leg from index 3 onward is the observed leg
of its own sliding 4-leg window (not non-overlapping blocks — one pattern
per leg, so consecutive patterns share 3 legs). Pivots are ranked by price
(1 = highest … 5 = lowest); the resulting 5-digit rank string is looked up
in a fixed 32-entry table (16 "M" labels for windows whose 4th/observed leg
is a down move, 16 "W" labels for an up move — verified mathematically
self-consistent: every M rank-string decodes to up/down/up/down, every W to
down/up/down/up, matching this app's strict leg-alternation guarantee).
"The next pattern" for a pattern ending at leg *i* is simply the pattern
ending at leg *i+4* (a fresh, non-overlapping 4-leg block starting exactly
at P4) — its own 5 pivots are named N0…N4, where N0 == P4 by construction.
"The next leg" is simply leg *i+1*.

**Controls**: Instrument selectbox. **Chart basis** (ETH default / RTH) —
this single control drives *two* things at once: which gyration `scope` is
queried (`continuous` for ETH, `rth` for RTH) and which bars the per-day
candlestick charts show. **Leg detection** (Extremum High/Low default =
`extreme_to_extreme`, or Close = `close_to_close`). **Threshold** — single
select from the 14 configured point-thresholds, default 40. **Date range** —
filters which patterns count toward the totals/cards (by the observed leg's
end date), but does *not* clip the underlying leg sequence used for "next
pattern"/"next leg" lookups, which always use the true next leg/pattern
regardless of whether it falls outside the selected range.

**Top summary**: Total patterns / Total M patterns (count + % of total) /
Total W patterns (count + % of total).

**32 pattern cards** (M1–M16 then W1–W16, 2 per row, each a bordered
container), each showing:
- Pattern name centered at the top; occurrence count + % of that family in
  the top-right corner.
- The pattern's Merrill diagram (`img/MW_patterns/M{n}.png` /
  `W{n}.png` — a small reference image of the pivot shape with rank labels),
  centered, 150px wide.
- **"Next Pattern :"** stats — a cascading set of pivot-direction comparisons
  between the *next* pattern's pivots and the *current* pattern's pivots,
  each line showing up/down/flat count+% (sign-colored, green/red): N4 vs
  P0/P1/P2/P3; then (extra margin) N3 vs P0/P1/P2/P3/P4; then N2 vs
  P0/P1/P2/P3/P4; then N1 vs P0/P1/P2/P3/P4. (N4 vs P4, i.e. N4 vs N0, is not
  currently shown — it would just be "did the next pattern move net up or
  down overall," a different question from all the P-relative ones above.)
- **"Next leg :"** stats — one breakout check: does the very next leg's
  endpoint clear this pattern's own extreme pivot (`> pattern high` for M
  patterns, `< pattern low` for W patterns), yes/no count+% (sign-colored).
- **"Next pattern distribution :"** — the full (untruncated) count+%
  breakdown of which specific label (M1–M16/W1–W16) the next pattern turned
  out to be, laid out 4 per line, pattern names colored by family (M =
  `COLOR_SB` orange/yellow, W = `COLOR_BS` pastel blue, matching the same
  colors used for BS/SB elsewhere in the app).
- **"Show days"** checkbox — mutually exclusive across all 32 cards (checking
  one force-unchecks every other card's checkbox via a shared
  `st.session_state["gw_selected"]`, enforced in an `on_change` callback), so
  only one pattern's days can be shown at a time.

**Below all 32 cards**, separated by a horizontal rule: for whichever
pattern is currently selected via "Show days," a lightweight table (Date,
Day, BS/SB, Range, Gap, Rel. Close, Abs. Close — pulled straight from
`sessions`) listing every unique calendar date touched by any occurrence of
that pattern label in the current filtered set (a date counts as "touched"
if it's the start-date or end-date of any of that pattern's 4 legs, across
every occurrence). Row-selectable (up to 20 rows charted at once); each
selected date renders a candlestick chart (RTH or ETH per Chart basis) with
the grey RTH-open reference line and — critically — the **actual stored
legs** touching that date drawn as an overlay (looked up from the already-
fetched full leg list, not live-recomputed from that day's bars alone,
because a `continuous`-scope leg can start on a previous calendar day and a
live per-day recompute would show a different, wrong set of legs).
