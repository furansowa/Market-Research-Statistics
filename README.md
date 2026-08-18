# Market Statistics Research v2.0

A Streamlit research workbench for studying intraday market structure on index futures
(US30/DOW and DAX). It ingests raw 1-minute bars, derives per-session statistics and
"gyration" (swing/leg) decompositions, and exposes eleven analysis pages for filtering
history, classifying day shapes, and measuring how swings behave in price and in time.

Built as a personal research tool — the emphasis is on **measurement honesty** rather than
signal generation: several pages exist specifically to test whether an apparent pattern
survives a matched null, and the module docstrings record where effects turned out to be
detector geometry rather than market behaviour.

## What's in it

| Page | What it does |
|---|---|
| **Day Session** | Filter 4,400+ sessions on ~260 derived columns (gap, range, close relatives, timing, market profile, macro shape…), then chart any day with swing overlays |
| **Gyration Legs** | Per-leg aggregates and timing metrics at a chosen point threshold |
| **Gyrations v2.0** | Extreme-to-extreme legs at fixed 40/120/200pt sizes, with per-session cards |
| **OpenNormalisation v1.0** | Sessions normalised to their RTH open, plus moon phase and pivot-pattern columns |
| **Gyrational Waves v1.0** | Arthur Merrill M/W 4-leg pattern classification over point-defined legs |
| **Gyrational Time v1.0** | When swings happen — time-of-day distributions of leg starts/ends |
| **Gyrational Range v1.0** | Multi-timeframe trailing/forward range state with causal quantile bucketing |
| **Day Templates v1.0** | Scale-invariant day-shape classification (Gilmore A/V/N/M/W templates + k-means clusters) |
| **Hourly Composite v1.0** | One session-hour, every day, chained gap-free — isolates what a given hour does |
| **Gyrational Stats v1.0** | 123 retracement zones and Figure-2.31 continuation zones, normalised by 5-day volatility |
| **Time Waves v1.0** | Merrill patterns over waves defined by **bar count** instead of point size |

## Requirements

- Python 3.12+
- Raw 1-minute CSVs (see below) — **not included in this repository**

```sh
python -m venv .venv
.venv/Scripts/activate          # Windows; use source .venv/bin/activate elsewhere
pip install -r requirements.txt
```

## Data — you must supply your own

**No price data and no database are included in this repo.** `data/` is gitignored: the
built SQLite store is several GB, and the underlying price history isn't mine to
redistribute. You need your own 1-minute bars before anything will run.

### Expected CSV format

`src/etl/parse.py` expects one or more `.csv` files per instrument, in a single folder:

- **Delimiter:** `;`
- **Header:** `Date;<TICKER>(Open, Ask)*;<TICKER>(High, Ask)*;<TICKER>(Low, Ask)*;<TICKER>(Close, Ask)*`
  — the instrument name is parsed out of the first column header, so it must match this shape
- **Timestamps:** `DD/MM/YYYY HH:MM`, already in **US Eastern Time**, DST-aware
- **Decimal separator:** comma (`12228,5`) — converted to `.` on parse
- **Row order:** newest-first (files are re-sorted ascending during ETL)
- **Overlaps between files are fine** — rows are deduplicated on `(instrument, ts)`

Example:

```
Date;US30(Open, Ask)*;US30(High, Ask)*;US30(Low, Ask)*;US30(Close, Ask)*
30/12/2011 15:59;12228;12229;12225;12225
30/12/2011 15:58;12234;12236;12228;12228
```

Then point `config.toml`'s `[data] raw_dir` at your folder. That path is machine-specific —
change it to yours before running the ETL.

### Regular trading hours are per-instrument

RTH windows are defined in ET and differ by instrument — US30 is NYSE cash (09:30–16:00),
DAX is Xetra cash (09:00–17:30 CET = 03:00–11:30 ET). `config.toml` holds the US30 window;
per-instrument windows live in `run_bars.py`'s `RTH_WINDOWS` and `run_dax_minutes.py`.
Adding a new instrument means adding its window there too, not just pointing at a CSV folder.

## Building the database

The ETL is idempotent — delete `data/db/lookup.sqlite` and re-run any time the schema
changes. Run in this order:

```sh
python run_etl.py                  # raw CSVs -> minutes + sessions + gyrations
                                   # (also runs run_shapes.py and run_market_profile.py)
python run_dax_minutes.py          # DAX 1-min into `minutes` (per-instrument RTH tagging)
python run_gyrations_continuous.py # continuous-scope legs (no full rebuild needed)
python run_bars.py                 # clock-aligned 5/10/15/30/60/240-min `bars`
python run_hour_bars.py            # RTH-open-anchored hourly bars
python run_range_bars.py           # Renko-style fixed-increment range bars
python run_range_state.py          # multi-timeframe trailing/forward range state
python run_day_templates.py        # normalised per-day RTH shape profiles
```

`run_etl.py` builds the core three tables and calls the two `sessions`-column post-steps
itself; the rest are independent precomputes, each feeding specific pages. Skip any whose
page you don't need.

## Running

```sh
.venv/Scripts/streamlit.exe run src/app/app.py
```

`src/app/app.py` is the entry point and wires all eleven pages into one multipage app.
Individual page modules can also be run standalone (`streamlit run src/app/dashboard.py`)
for quicker iteration on a single page.

## Tests

```sh
pytest
```

The detector tests are the ones that matter: `tests/test_gyrations.py` and
`test_gyrations_extreme.py` assert the zigzag detector's formal properties (P1–P8) and the
retracement invariant on hand-built fixtures, and `test_time_waves.py` hand-traces the
time-based detector's state machine — including two cases that deliberately **pin known
imperfect behaviour** so it can't be silently "fixed" out from under the statistics built
on it.

## Documentation

- **`CODE_STRUCTURE_AND_FEATURES.md`** — the authoritative reference: architecture, file
  tree, every database table, and every feature/column/filter of every page. Read this
  before changing anything.
- **`Gyrations_lookup_engine_SPEC.md`** / **`_phase2.md`** — original design specs. Useful
  for the *why* behind early decisions; not kept in sync with what's actually built.

## Notes

- Trading concepts taken from public forum sources are credited inline with abbreviated
  handles; the specs paraphrase ideas rather than reproducing anyone's material.
- Some `src/gyrations/` modules (e.g. `rainflow.py`) are standalone research code not wired
  into any page — see the "Unwired research modules" section of the structure doc.
