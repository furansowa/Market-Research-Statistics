# DOW Session Lookup Engine

A research tool for studying DOW (US30) intraday session behavior: gap/range/close
statistics, "gyration" (swing/leg) detection with configurable point thresholds, and
a Streamlit dashboard for filtering historical sessions and browsing candlestick
charts with live swing overlays.

Built against the design in [`Gyrations_lookup_engine_SPEC.md`](Gyrations_lookup_engine_SPEC.md)
(all-phase overview) and [`Gyrations_lookup_engine_SPEC_phase2.md`](Gyrations_lookup_engine_SPEC_phase2.md)
(gyrations, windows, offset lookup). [`CODE_STRUCTURE.md`](CODE_STRUCTURE.md) has the
full technical detail of what's actually built, file by file — read that before making
changes.

## Setup

```bash
python -m venv .venv
.venv/Scripts/activate        # .venv\Scripts\activate on Windows cmd
pip install -r requirements.txt
```

Edit `config.toml`'s `[data] raw_dir` to point at your own directory of raw per-instrument
minute-bar CSVs — this path is machine-specific and isn't tracked in git.

## Running

```bash
python run_etl.py                                    # raw CSVs -> data/db/lookup.sqlite
.venv/Scripts/streamlit.exe run src/app/app.py        # launch the app (Day Session + Gyration Legs pages)
pytest                                                # run the test suite
```

`src/app/dashboard.py` (the Day Session page) can still be run standalone
(`.venv/Scripts/streamlit.exe run src/app/dashboard.py`) without the Gyration
Legs page, but `app.py` is the primary entry point.

The ETL is idempotent — delete `data/db/lookup.sqlite` and re-run `run_etl.py` any time
the schema changes (new registry columns, new gyration thresholds, etc.).

## Status

Phase 1 (ETL + filter/browse dashboard) and Phase 2 work-order items 1-6 of 8 (offset
lookup, registry-driven UI, gyration detector, chart overlay, per-window dashboard tabs)
are complete. See `CODE_STRUCTURE.md`'s "What's built so far" section for the current
state.
