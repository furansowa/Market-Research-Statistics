# DOW Session Lookup Engine — Build Specification

**Purpose of this document:** a complete, buildable spec to hand to Claude Code. It fixes the data model, pipeline, parameters, and dashboard so the builder does not have to re-derive design decisions. Read it top to bottom before writing code.

---

## 1. What this is (and is not)

This is a **historical lookup / analytics engine**, not a strategy backtester. There is no order simulation, no P&L, no live data, no server. Everything runs locally against historical 1-minute OHLC data.

The engine must support three core workflows:

1. **Forward lookup ("what happened next").** Filter sessions by a set of parameters (e.g. *gap-down Mondays with prior close down*) and see the distribution of outcomes on the same session and/or the next session/next hour — e.g. relative close, time-of-high, SB vs BS share.
2. **Backward lookup ("find analogs").** Given a reference session's parameters (or the parameters observed so far today), find prior sessions that matched similar conditions and inspect them.
3. **Filtered statistics.** On any filtered set, produce counts and distributions: % SB vs BS, gap-up vs gap-down share, time-of-high histogram, range quantiles, up-leg/down-leg stats, etc.

The long-term aim is to characterise each session into a **template** (session shape: A / V / W / N …) driven mainly by the intraday up/down gyration structure, supported by gap and close-difference context, so that at the open a day can be matched to a historical template.

Design guiding principle: **the size of moves is treated as noisy; the timing of turning points is the object of study.** Time is a first-class dimension everywhere.

---

## 2. Tech stack

- **Python 3.11+**
- **Polars** for the ETL / parsing (fast, handles the European number format cleanly). DuckDB is an acceptable alternative for the heavy aggregations later.
- **SQLite** as the persistent store — single portable file, ships trivially, indexed lookups are sub-second at this scale. (DuckDB may be added later purely as a faster read-side query engine over the same data; not required for v1.)
- **Streamlit** for the dashboard.
- **Plotly** for candlestick charts and histograms.
- **TOML** for config (`tomllib` is stdlib for reading).
- pandas only at the Streamlit boundary (Streamlit renders pandas DataFrames naturally).

No VPS needed. The only slow step is the one-time ETL; interactive queries are fast.

---

## 3. Repository layout

```
dow-lookup/
├── config.toml                 # all definitions / settings (see §5)
├── data/
│   ├── raw/                    # original CSVs, immutable, never written to
│   └── db/lookup.sqlite        # generated store
├── src/
│   ├── etl/
│   │   ├── parse.py            # CSV → normalized minute bars
│   │   ├── sessions.py         # minute bars → per-session features
│   │   └── load.py             # write to SQLite, build indexes
│   ├── features/
│   │   ├── registry.py         # feature registry (the extensibility core, §8)
│   │   └── definitions.py      # one function per feature
│   ├── gyrations/
│   │   └── gyrations.py           # threshold gyration/leg detector, both modes (§9)
│   ├── query/
│   │   ├── filters.py          # build WHERE clauses from feature registry
│   │   ├── forward.py          # workflow 1
│   │   ├── analog.py           # workflow 2
│   │   └── stats.py            # workflow 3
│   └── app/
│       └── dashboard.py        # Streamlit entry point
└── README.md
```

**Keep `data/raw/` immutable.** The CSVs are the source of truth. Every derived value is computed downstream so definitions can change and the DB be rebuilt without touching raw data. Do **not** fatten the raw CSVs with computed columns.

---

## 4. Input data — exact format

All instrument files share this format (verified against the DOW sample):

- Delimiter: `;`
- Header: `Date;US30(Open, Ask)*;US30(High, Ask)*;US30(Low, Ask)*;US30(Close, Ask)*`
- Datetime: `DD/MM/YYYY HH:MM` (e.g. `01/07/2026 23:59` = 1 July 2026).
- Decimal separator is **comma**: `52400,5` = 52400.5; some values have no decimal (`52400`).
- Rows are sorted **newest-first (descending)**.
- Prices are **Ask only** (no bid). Fine for point-difference work.
- Timestamps are already **US Eastern time**, DST-aware (confirmed: the 1-minute range spike sits exactly at 09:30 across the whole sample). No timezone conversion is required — filter by clock time.
- Instrument here is spot/CFD **US30** (not YM futures), trading ~24h on weekdays. Overnight is continuous, so gaps are smaller/different from futures — acceptable, just noted.

**Parsing steps:** strip header; parse datetime with `%d/%m/%Y %H:%M`; replace decimal comma; cast to float; **sort ascending**; dedupe on `(instrument, timestamp)` keeping one row (files may overlap at edges); tag each bar `RTH`/`ETH`.

---

## 5. Configuration (`config.toml`)

All *definitions and settings* live here — never hardcoded. Example:

```toml
[session]
timezone            = "America/New_York"   # data is already ET; used for labelling
rth_start           = "09:30"
rth_end             = "16:00"              # last RTH minute bar is 15:59
rth_close_bar       = "15:59"              # close = close of this bar (configurable)
# ETH day = first bar → last bar of the ET calendar date (no fixed clock window needed)
ignore_weekends     = true                 # skip Saturday and Sunday entirely
half_day_flag_before = "14:00"             # sessions whose last RTH bar precedes this = half day

[gap]
# gap = rth_open(today) - rth_close(previous SESSION), skipping weekends/holidays
basis = "prev_session_rth_close"

[gyrations]
default_threshold_points = 35              # Ch*** used 35 pts bar-close-to-bar-close
default_mode             = "close_to_close" # or "extreme_to_extreme"
thresholds_to_precompute = [20, 35, 50]    # optional batch precompute set

[data]
instruments = ["US30"]                     # add more later; schema is instrument-aware
```

The RTH/ETH switch on the dashboard reads these boundaries. Session boundaries, gyration thresholds, gap definition, and half-day rule are all config, not code.

---

## 6. Session & edge-case definitions (lock these)

- **RTH window:** the 390 one-minute bars `09:30`…`15:59` ET on a weekday, when bars exist.
  - `rth_open` = open of the 09:30 bar.
  - `rth_close` = close of the 15:59 bar (configurable via `rth_close_bar`).
  - `rth_high`/`rth_low` = extremes over the window; record the **timestamp** of each.
- **ETH / full-day window:** simply the **first bar → last bar of the ET calendar date**. `eth_open` = open of the first bar of the date, `eth_close` = close of the last bar of the date, extremes over all of that date's bars. No fixed clock window; this is robust to the intraday break and to Friday's early stop, so we don't have to special-case anything.
- **Trading calendar (verified against the DOW sample — the loader must expect this):**
  - **Ignore Saturday and Sunday entirely** — no session rows on either. (Saturday has no bars; Sunday has only a partial evening session. Dropping both removes the only partial-day headache, which is why the plain first-bar→last-bar ETH rule above is safe.)
  - The tradable week is **Mon → Fri**, with a **daily 17:00–18:00 ET maintenance gap** (no bars in the 17:00 hour — normal, not missing data; the 18:00 jump is the reopen). Friday has no evening reopen. Because the break is intraday, each Mon–Fri date is self-contained.
- **Previous session** = the previous row in the `sessions` table for the same instrument (skips weekends/holidays). All "previous close / previous day" logic uses this, **not** the calendar prior day.
- **Half days / early closes** (e.g. day after Thanksgiving, Christmas Eve → ~13:00 close): flag `is_half_day` so they can be included/excluded. Detect via last RTH bar earlier than `half_day_flag_before`.
- **Missing minutes:** do not forward-fill. Work with bars present. A session is valid if RTH bars exist that day. Distinguish *expected* absences (the 17:00 maintenance hour, weekends, holidays, half-day afternoons) from genuine data holes; only the latter are worth logging.
- **SB/BS tie-break:** if the RTH high and low fall in the *same* minute bar (order unknowable from OHLC), set `bs_sb = "TIE"` rather than guessing. Document this; such rows can be filtered out.

---

## 7. Database schema (SQLite)

All tables carry `instrument` so a second instrument is just another ETL run into the same schema. Index `(instrument, date)` and `(instrument, ts)`.

**`minutes`** — one row per bar (the raw grid):

| column | type | notes |
|---|---|---|
| instrument | TEXT | e.g. "US30" |
| ts | TEXT/INTEGER | ET timestamp (ISO or epoch) |
| date | TEXT | session date (ET calendar day) |
| open, high, low, close | REAL | Ask prices |
| session | TEXT | "RTH" or "ETH" |

**`sessions`** — one row per trading day (the table filtered 95% of the time). Columns = the feature set in §8. Sketch:

```sql
CREATE TABLE sessions (
  instrument TEXT, date TEXT, weekday TEXT, is_half_day INTEGER,
  rth_open REAL, rth_high REAL, rth_low REAL, rth_close REAL,
  rth_high_time TEXT, rth_low_time TEXT, rth_range REAL,
  eth_open REAL, eth_high REAL, eth_low REAL, eth_close REAL, eth_range REAL,
  gap_pts REAL, gap_dir TEXT,
  rel_close_pts REAL, rel_close_dir TEXT,      -- close - open (same day)
  abs_close_pts REAL, abs_close_dir TEXT,      -- close - prev session close
  bs_sb TEXT,                                  -- "SB" | "BS" | "TIE"
  prev_rel_close_dir TEXT, prev_abs_close_dir TEXT, prev_bs_sb TEXT,
  gap_prevclose_combo TEXT,                     -- e.g. "gapdown_prevclosedown"
  open_vs_prev_range TEXT,                      -- below / inside / above
  template TEXT,                                -- reserved, filled later
  PRIMARY KEY (instrument, date)
);
```

**`gyrations`** — one row per detected leg (computed on demand and cached; §9):

| column | type | notes |
|---|---|---|
| instrument, date | | session key |
| basis | TEXT | "RTH" or "ETH" |
| threshold | REAL | points |
| mode | TEXT | "close_to_close" / "extreme_to_extreme" |
| leg_index | INTEGER | order within session |
| start_time, end_time | TEXT | turning-point times |
| start_price, end_price | REAL | |
| direction | TEXT | "up" / "down" |
| magnitude_pts | REAL | abs(end - start) |
| duration_min | INTEGER | end_time - start_time |
| midprice | REAL | (start_price + end_price) / 2 |

---

## 8. Parameter organisation — the core design pattern

"Parameter" means three different things; keep them in three different places so the system stays manageable as parameters multiply.

1. **Config / definitions** (§5): session boundaries, RTH/ETH switch, gyration threshold, gap definition, lookback window. → `config.toml`.
2. **Derived features**: computed *per session*. → columns in `sessions` (or aggregates from `gyrations`). Produced by the ETL.
3. **Filters**: interactive query criteria the user picks ("Mondays / gap < −50 / prior close down"). → dynamic WHERE-clauses over features, **not** a fixed schema.

### Feature registry (the extensibility core)

Define each feature **once** in a registry. Each entry declares:

- `name`
- `compute(session_ctx) -> value` — how it's derived
- `dtype` — numeric / categorical / boolean / time
- `basis` — "RTH" / "ETH" / "context" (so the RTH/ETH switch and mixed-basis context features work)
- `filter_kind` — `range` (numeric), `select` (categorical), `bool`

Adding a new parameter later = add one registry entry + re-run ETL. It then becomes **automatically filterable and displayable** in the dashboard with no plumbing changes. This is the "track every metric, discard the useless ones" workflow made cheap.

### Feature list for v1

**Day-level (RTH basis):** rth_open, rth_high, rth_low, rth_close, rth_high_time, rth_low_time, rth_range, gap_pts/gap_dir, rel_close_pts/dir (close−open), abs_close_pts/dir (close−prev close), bs_sb, weekday, is_half_day.

**Context / cross-session:** prev_rel_close_dir, prev_abs_close_dir, prev_bs_sb, gap_prevclose_combo, open_vs_prev_range (open vs prior RTH high/low: below/inside/above + normalised position).

**Full-day (ETH basis):** eth_open/high/low/close, eth_range, plus ETH versions of gap/close if wanted (available even when RTH is the primary switch — that's the "RTH primary but keep some ETH context" case).

**Time-bucketed:** time_of_high and time_of_low as minute-of-session and as 30-minute bucket (for clean histograms).

**Per-minute series (derived on demand, not stored as columns):** for each RTH bar, `close − rth_open`. Compute by joining `minutes` to `sessions.rth_open`. Used for the intraday shape overlay/plots.

**Leg-derived aggregates (once §9 exists):** num_legs, first_leg_direction, first_leg_magnitude, first_leg_end_time (the "fastest 20 points at the open" idea), up-leg min/mean/max, down-leg min/mean/max, mean duration, which leg made HOD / LOD.

---

## 9. Gyration detection

A threshold-based reversal detector (zigzag-style algorithm) with **no minimum-bar requirement** — only a minimum move size in points. Build **both** modes; `close_to_close` is the default and the simpler logic.

Terminology (Ch***'s usage): a **leg** is one directional move (up or down); a **gyration** is one up+down cycle = 2 legs. The detector emits **legs**, and the `gyrations` table stores **one row per leg** (`leg_index`), so counting or pairing them into gyrations is trivial downstream.

- **close_to_close:** run over each bar's **close**. Track a running extreme; when price reverses from that extreme by ≥ `threshold` points (close-to-close), the extreme is confirmed as a turning point and a new leg begins. (Ch***'s "minimum 35 points bar-close-to-bar-close.")
- **extreme_to_extreme:** same logic but track the bar **highs** for up-legs and **lows** for down-legs.

Pseudocode (close-to-close):

```
threshold = T
pivots = [first_bar]
dir = None                 # "up" or "down", unknown at start
ext = first_close; ext_i = 0
for i, c in enumerate(closes):
    if dir in (None, "up"):
        if c > ext: ext, ext_i = c, i          # extend up
        elif ext - c >= T:                      # reversal down confirmed
            pivots.append((ext_i, ext)); dir = "down"; ext, ext_i = c, i
    if dir in (None, "down"):
        if c < ext: ext, ext_i = c, i           # extend down
        elif c - ext >= T:                       # reversal up confirmed
            pivots.append((ext_i, ext)); dir = "up"; ext, ext_i = c, i
append final ext as last pivot
```

The pseudocode is illustrative — the two correctness points to preserve are: (a) the pivot recorded is the **extreme** `(ext_i, ext)`, not the current bar where the reversal is confirmed; and (b) the initial direction is unknown, so the first confirmed reversal seeds it (the first bar acts as the first provisional pivot). Verify both on a couple of real sessions before trusting the output.

Each consecutive pivot pair → a `gyrations` (leg) row (direction, magnitude, duration, midprice, start/end time & price). Compute per session over the chosen basis window; a 390-bar RTH session is microseconds, so compute **on demand** for the current view and cache to the `gyrations` table keyed by `(date, basis, threshold, mode)`. Batch-precompute the `thresholds_to_precompute` set for full-history studies.

---

## 10. Dashboard (Streamlit)

Global controls (sidebar):

- **Basis switch:** RTH ⇄ ETH (drives which session-window features are primary).
- **Instrument** selector (US30 only for now).
- **Gyration threshold + mode** controls.
- **Date range** limiter.

### Panel A — Filter & browse
- Dynamic filters generated from the feature registry (range sliders for numeric, multiselect for categorical, toggles for boolean). Because filters come from the registry, new features appear here automatically.
- Results **table**: one row per matching session with key context (date, weekday, gap, rel_close, abs_close, SB/BS, time-of-high, range…).
- **Click a row → candlestick chart** of that session (Plotly), with gyration/leg pivots overlaid and HOD/LOD marked.

### Panel B — Statistics on the filtered set
- Counts and %: SB vs BS, gap-up vs gap-down, prior-close-direction breakdown.
- Distributions: time-of-high and time-of-low histograms (30-min buckets), rel_close quantiles, range quantiles, up-leg/down-leg magnitude and duration stats.
- Everything recomputes live as filters change. This is the primary edge-finding surface.

### Panel C — Forward lookup ("what happened next")
- Take the filtered set and report same-session outcomes **and** next-session (and later, next-hour) outcome distributions.

### Panel D — Analog lookup ("find similar days")
- v1: seed the filters from a chosen reference session and return the matching set (reuses the filter engine).
- v3: optional nearest-neighbour ranking on a normalised, weighted feature vector for a "most similar N days" list.

---

## 11. Build phases (order for Claude Code)

**Phase 1 — Pipeline + browse (the usable core).**
ETL (`parse` → `sessions` → `load`) with the §4 parsing and §6 definitions; `sessions` table with all §8 day-level + context features; Streamlit Panel A (registry-driven filters, results table, click-to-chart). This alone reproduces the classic Ch***/Zr*** spreadsheet and is immediately useful.

**Phase 2 — Statistics + forward lookup.** Panels B and C.

**Phase 3 — Gyrations.** `gyrations.py` (both modes), `gyrations` table, gyration overlay on charts, leg-derived aggregate features, analog lookup Panel D (rule-based).

**Phase 4 — Templates + advanced analogs.** Derive session-shape codes (A/V/W/N…) from the gyration sequence into the `template` column; nearest-neighbour analog ranking; intraday-anchored features ("parameters as of 10:30 → outcome by 12:00").

**Later — packaging & multi-instrument.** Add instruments via extra ETL runs (schema already supports it). Package to a standalone app (PyInstaller / Tauri shell) once the logic is stable — the DB + Python core carries over unchanged, so nothing is wasted by staying in the browser now.

**Later — Excel export.** One-click export of the current filtered set / stats to `.xlsx`. Export only; never the compute engine.

---

## 12. Open decisions to revisit (don't block Phase 1)

- **RESOLVED — RTH open/close:** open = open of the 09:30 bar; close = close of the 15:59 bar.
- **RESOLVED — ETH day:** first bar → last bar of the ET calendar date; weekends ignored entirely.
- **Gyration threshold measurement:** confirm close-to-close as primary once we eyeball both modes on real sessions.
- **Template taxonomy:** define A/V/W/N… only after we can see gyration sequences (Phase 3 output informs Phase 4 definitions).
- **Intraday/hourly lookups:** schema and registry should not preclude sub-session windows; treat as Phase 4 features computed over a bar sub-range.
