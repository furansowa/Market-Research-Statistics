# Phase 2 Spec — Gyrations, Windows, Offset Lookup

Companion to `Gyrations_lookup_engine_SPEC.md` (design rationale, all phases) and
`CODE_STRUCTURE.md` (what Phase 1 built). Read both first. This document specifies
exactly what Phase 2 adds and how it fits the existing code.

**Phase 2 delivers four things:**

1. **Gyration/leg detection** with retracement ("elasticity") metrics — multi-scale.
2. **Config-declared windows** — the same metric bundle over any clock range.
3. **Offset-based lookup** — forward and backward analog search via one mechanism.
4. **Conditioning/outcome separation** + registry-driven results table.

**Explicit non-goals for Phase 2.** No template classification (`template` stays
NULL). No A/V/W/N. No normalisation (`_pct` twins may be stored, nothing reads
them). No clustering, no nearest-neighbour ranking, no live data. No assumptions
about where in the session the "important" moves live — windows are declared, not
privileged.

---

## 0. Ground rules carried forward

- **Points only.** Every threshold, magnitude, and retracement is in index points.
  Fixed across eras by deliberate choice. A `_pct` twin may be computed and stored
  but no filter or display reads it by default.
- **Raw CSVs stay immutable.** All derived values are recomputed from raw + config.
- **Registry order matters.** A derived `FeatureSpec.compute` lambda may only
  reference base columns or *earlier* registry entries.
- **The Int8 trap.** Polars `.dt.hour()` / `.dt.minute()` return `Int8` here.
  `hour() * 60` silently overflows. Every new time-arithmetic helper must
  `.cast(pl.Int32)` before multiplying. This bit us once already.
- **Don't assume, measure.** Where this spec states a market fact, it is either
  quoted from the source corpus or measured from the data. Everything else is a
  parameter.

---

## 1. Config additions (`config.toml`)

```toml
[windows]
# Inclusive of the end bar. Clock ranges (not bar counts), so half-days
# naturally yield shorter windows for any window ending after the early close.
# Column prefix is "win_<name>_" to avoid colliding with the base rth_* / eth_*
# columns. Do NOT define a window named "rth" — the full-session bundle already
# exists as the base RTH columns.
first_30m = ["09:30", "09:59"]
first_60m = ["09:30", "10:29"]
first_90m = ["09:30", "10:59"]
last_90m  = ["14:30", "15:59"]

[gyrations]
# Bars are always 1-minute. Ch*** identifies swings "as on a 1 minute chart"
# and calls the 5-minute bar "very blunt". There is no bar-resolution parameter.
mode       = "close_to_close"       # default; "extreme_to_extreme" available, see 2.2
scopes     = ["rth", "eth", "continuous"]

# Thresholds in points. Deliberately dense at the low end — fine thresholds are
# what resolve the interior of coarse legs (see 4.3).
thresholds = [10, 15, 20, 30, 40, 50, 60, 70, 80, 90, 100, 120, 150, 200]

# Which (scope, mode) combinations to precompute at ETL time. Everything else is
# computed on demand and cached. See 3.2 for row-count arithmetic.
precompute = [
  { scope = "rth", mode = "close_to_close" },
  { scope = "eth", mode = "close_to_close" },
]

# Only consulted when mode = "extreme_to_extreme". See 2.2.
# bar_direction is ~90% accurate against observed intrabar paths (see 2.2), and is
# the only option that uses information actually present in the bar.
intrabar_tiebreak = "bar_direction"   # "bar_direction" | "adverse_first" | "favourable_first"
```

**There is no `micro` / `macro` config key, deliberately.** "Micro" and "macro" are
*relative roles*, not fixed values — any pair `(fine, coarse)` drawn from
`thresholds` where `fine < coarse`. The nesting view (§4.3) takes the pair as a UI
selection.

This is faithful to the source. Ch*** used **15 micro / 45 macro** on CL in 2008;
counted YM legs at a single **15**-point scale; used **35 as the *macro*** threshold
in other posts of the same period; and stated the choice is arbitrary twice:

> "Generally I use, **arbitrarily**, 35 points, bar close to bar close, as the
> minimum for a swing (ie CL, NG, YM)."

> "The gyrations may conveniently be seen as two patterns, the larger over the
> smaller… you can **arbitrarily** set the magnitude… over this you can see the
> larger gyrations which again you may set arbitrarily at, say, 45 points."

There is no canonical ratio. Do not encode one.

**Density calibration.** Ch*** counted ~21 legs/session on YM at `T = 15`, and
39–48 micro legs on CL at `T = 15`. On 2026 DOW (index ~52,000) `T = 15` yields
~77 legs/session; matching his *leg density* needs `T ≈ 55–60`. At `T = 10–15` on
today's index you are resolving noise — which is precisely what you want for
interiors, but do not read session structure into those thresholds.

---

## 2. The gyration detector — `src/gyrations/detect.py`

### 2.1 What a leg is

A **leg** is one directional move between two consecutive pivots. A **gyration**
is one up+down cycle = two legs. The table stores **one row per leg**.

Threshold-based reversal detection (zigzag-family), **no minimum-bar requirement** —
only a minimum move size in points.

- **`close_to_close`** (default): runs over each bar's `close`. A 1-D series.
- **`extreme_to_extreme`**: tracks bar `high` for up-legs, `low` for down-legs.

### 2.2 Why `close_to_close` is the default — and the cost of the alternative

Wherever Ch*** states the measurement basis, it is "bar close to bar close" — and
he ties swing identification to a **1-minute chart**:

> "measure the swings in CL using a minimum span of 40 points to identify each swing
> (**as on a 1 minute chart**)"

> "The **5 min bar** if used (eg YM, ES, CL) is **very blunt**"

(The one 5-minute chart in the corpus is Bl***'s, not Ch***'s.) Bars are therefore
always 1-minute; there is no bar-resolution parameter.

Measurement on the 2026 sample shows close-to-close is a substantive choice, not a
convention:

| T | legs/session (close) | legs/session (extreme) | ratio |
|---|---|---|---|
| 10 | 103.3 | 301.0 | 2.91× |
| 20 | 59.9 | 176.6 | 2.95× |
| 50 | 21.5 | 46.6 | 2.16× |
| 100 | 7.6 | 12.8 | 1.67× |
| 200 | 2.6 | 3.3 | 1.28× |

Extreme mode produces ~3× the legs at fine thresholds, with *smaller* median
magnitudes. That is the signature of **wick noise** — reversals triggering on
1-minute high/low spikes rather than on price actually travelling. On Ask-only CFD
data those wicks contain spread artifacts and thin-liquidity spikes, so they are
partly synthetic. Close-to-close filters them.

Three further costs of extreme mode:

1. **Intrabar path ambiguity.** A single bar can make a new high *and* fall `T`
   below the running high. OHLC cannot say which came first — both orderings produce
   an identical bar, but different pivot prices, different pivot **timestamps**, and
   sometimes different leg counts. A tie-break is therefore *required*:
   - `bar_direction` (**default**): if `close >= open`, assume the low came before the
     high; otherwise the high came before the low.
   - `adverse_first`: test the reversal before the extension. Legs end earlier.
   - `favourable_first`: test the extension before the reversal. Legs run longer.

   `bar_direction` is the default because it is the only rule that uses information
   actually present in the bar, and because it is measurably right. Reconstructing
   k-minute bars from the 1-minute series (where the true intrabar path *is*
   observed) gives:

   | bar size | 2m | 3m | 5m | 10m | 15m |
   |---|---|---|---|---|---|
   | rule correct | **90.1%** | 89.3% | 87.9% | 88.5% | 88.5% |

   Accuracy improves as bars get finer, so at 1-minute resolution it is likely ≥90%.
   It is also symmetric (~89% on up bars, ~90% on down bars), so it does not buy
   accuracy in one direction at the other's expense.

   Whatever is chosen, the rule is an **assumption baked into every leg** for
   seventeen years. It is config, and visible, for that reason.
2. **The invariant (§2.8) needs an explicit scan rule** in extreme mode — see §2.6.
3. **Storage:** 4.7M vs 1.8M RTH leg-rows across the configured thresholds.

If genuine intrabar movement is the concern, high/low on 1-minute bars is the wrong
instrument — it is a lossy two-point proxy for the path. The correct fix is finer
bars (tick or second data), not wicks.

`mode` is part of the `gyrations` primary key, so the two modes can never be
silently pooled. **Never compare leg counts, magnitudes, or retracements across
modes.**

### 2.3 A consequence of `close_to_close` worth expecting

A close-mode leg's extreme is a *close*, whereas `rth_high` / `rth_low` on the
`sessions` table come from bar *highs* and *lows*. So `largest_leg_pts` will not
exactly equal `rth_range`, and the largest leg's turning points will not exactly
coincide with `rth_high_time` / `rth_low_time`. This is correct behaviour, not a
bug. The two are measured on different series.

### 2.4 Algorithm — required properties

The implementation is the builder's choice. What is **not** negotiable are the
properties below. Test each one; several are cheap and catch real bugs.

**P1 — Pivots are strictly increasing in time.** `pivot[k].index < pivot[k+1].index`
for all k. **Assert this; do not "deduplicate" violations away.** A silent dedupe
converts an ordering bug into invisible data loss (the real high pivot gets dropped).
This assertion caught a genuine seeding bug during design.

**P2 — A pivot is an extreme, never a confirming bar.** When a reversal of size `T` is
detected at bar `i`, the pivot recorded is the running extreme `(ei, ext)` reached
*before* `i`, not `(i, close[i])`. This is the classic zigzag bug.

**P3 — Two-sided seeding.** At the start of a scope the direction is unknown and must
not be biased. The detector must track a running high *and* a running low until the
first `T`-sized reversal establishes direction. The naive approach — letting the
running low keep updating past the running high — produces time-reversed pivots on a
simple rise-then-fall series. Snapshot the opposite extreme whenever a new running
extreme is set.

**P4 — Simultaneous triggers.** If the unseeded range exceeds `2T`, both
`hi − c ≥ T` and `c − lo ≥ T` can fire on the same bar. Tie-break on whichever
extreme occurred **later** (if `i_hi > i_lo`, the market was rising into the high, so
the leg that just ended is up, and direction becomes `down`).

**P5 — Degenerate seed.** If the seed pivot's index equals the first confirmed pivot's
index (e.g. price drops monotonically from bar 0), emit only one pivot, not two.

**P6 — No reversal, no legs.** If no `T`-sized reversal ever occurs in the scope,
emit **zero** legs. Never synthesise one.

**P7 — Leg magnitudes.** Every *confirmed* leg has `magnitude_pts >= T`. Unconfirmed
legs may be smaller.

**P8 — Monotonicity in T.** For a fixed scope instance, leg count is non-increasing
as `T` increases. A cheap regression test across the whole threshold list.

<details>
<summary>Reference implementation (close_to_close) — verified against P1–P8</summary>

```
T = threshold
hi = lo = series[0];  i_hi = i_lo = 0
lo_at_hi, i_lo_at_hi = series[0], 0     # running min as of the last new high
hi_at_lo, i_hi_at_lo = series[0], 0     # running max as of the last new low
dirn = None;  pivots = []

for i, c in enumerate(series):
    if dirn is None:
        if c > hi: hi, i_hi = c, i;  lo_at_hi, i_lo_at_hi = lo, i_lo
        if c < lo: lo, i_lo = c, i;  hi_at_lo, i_hi_at_lo = hi, i_hi

        down_trig = (hi - c) >= T
        up_trig   = (c - lo) >= T
        if down_trig and up_trig:              # P4
            down_trig = i_hi > i_lo;  up_trig = not down_trig

        if   down_trig: seed, i_seed, conf, i_conf = lo_at_hi, i_lo_at_hi, hi, i_hi; dirn='down'
        elif up_trig:   seed, i_seed, conf, i_conf = hi_at_lo, i_hi_at_lo, lo, i_lo; dirn='up'
        else:           continue

        if i_seed < i_conf: pivots.append((i_seed, seed))   # P5
        pivots.append((i_conf, conf))
        ext, ei = c, i

    elif dirn == 'up':
        if   c > ext:        ext, ei = c, i
        elif ext - c >= T:   pivots.append((ei, ext)); dirn='down'; ext, ei = c, i
    else:  # 'down'
        if   c < ext:        ext, ei = c, i
        elif c - ext >= T:   pivots.append((ei, ext)); dirn='up';   ext, ei = c, i

if dirn and (not pivots or ei > pivots[-1][0]):
    pivots.append((ei, ext))            # final running extreme closes the last leg
```

Verified time-ordered on: rise-then-fall, fall-then-rise, no-trigger, monotone drop,
double swing, exact-`T` touch. For `extreme_to_extreme`, substitute highs for up-leg
extension and lows for down-leg extension, and apply `intrabar_tiebreak` (§2.2).
</details>

### 2.5 The `confirmed` flag — critical

- The **first leg** of a scope starts at a seed extreme whose status as a true
  pivot was never established (nothing precedes it inside the scope).
- The **last leg** of a scope is still open when the scope ends — its terminal
  pivot was never confirmed by a `T` reversal.
- **Every interior leg is confirmed.**

Set `confirmed = FALSE` on the first and last leg of each scope, `TRUE` otherwise.

A scope with fewer than **4 pivots** therefore yields **zero confirmed legs**
(3 pivots = 2 legs = first + last). This is correct, not an error.

**Default all leg statistics and filters to `confirmed = TRUE`.** This excludes two
legs per session (one at each end). Left unflagged, these phantom legs corrupt every
retracement aggregate — measured, not hypothesised: an early draft of the detector
reported retracements of 424 points at `T = 50`, all of them traceable to the
unconfirmed seed leg.

For `continuous` scope there is exactly one unconfirmed leg at each end of the
entire 2009–2026 series. Negligible.

### 2.6 Deepest retracement ("elasticity")

Computed in the same O(n) pass, per leg, over the leg's bar range `[i0, i1]`
inclusive. Track a running extreme from the leg's start price `p0`; the drawdown
is the distance from that running extreme *against* the leg's direction.

```
run = p0;  best = 0;  progress_at_best = 0
for j in i0..i1:
    c = series[j]
    if up:   run = max(run, c);  dd = run - c
    else:    run = min(run, c);  dd = c - run
    if dd > best:
        best = dd
        progress_at_best = abs(run - p0)     # how far the leg had travelled
```

Stored per leg:

| column | meaning |
|---|---|
| `deepest_retr_pts` | `best` — exact, threshold-free, any size |
| `deepest_retr_pct_final` | `best / magnitude_pts` — **this is `elasticity`** |
| `deepest_retr_progress` | `progress_at_best`. Not read by default; stored so a give-back ratio can be reconstructed later without recomputing. |
| `deepest_retr_start_ts` | timestamp of the running extreme the retracement fell from |
| `deepest_retr_end_ts` | timestamp of the retracement's trough |

`deepest_retr_pts` and `deepest_retr_pct_final` share a denominator that is constant
per leg, so **they identify the same event**. One argmax, no ambiguity, no guard.

Worked example: a leg that ends +30, having retraced 10 points after being up 15 →
`deepest_retr_pts = 10`, `deepest_retr_pct_final = 33%`, `deepest_retr_progress = 15`.

**In `extreme_to_extreme` mode**, the series differ by direction: for an up leg the
running extreme is the running max of **highs** and the drawdown is measured to the
bar's **low** (`run_high − low`); for a down leg, running min of **lows**, drawdown to
the bar's **high**. Same code, two series.

**Terminal-bar rule (required, or the invariant fails).** On the leg's **first and
last** bars, only the extreme that *is* the pivot participates in the scan. The
opposite extreme of the terminal bar belongs to the **next** leg, not this one.

Why: under `bar_direction`, an up leg ending on a sharp reversal bar (`close < open`,
so high-then-low) has its pivot at that bar's high — and that same bar's low sits
`>= T` beneath it, because that is what fired the reversal. Including it in the scan
would report `deepest_retr_pts >= T` on every reversal bar, breaking §2.8 by
construction. Interior bars are still bounded by `T` (any interior bar reaching `T`
would have ended the leg there), so with this rule the invariant holds in extreme mode
exactly as it does in `close_to_close`.

Do not "fix" a failing invariant test by deleting the test. Check this rule first.

### 2.7 Why there is no `pct_sofar` column

An earlier draft carried a second metric, `retracement ÷ leg-travel-at-that-moment`
("give-back"). It is **not** stored, for three reasons:

1. Its argmax is a **different event** from the deepest retracement in 21% of legs
   (measured, T=50), so it is a genuinely separate statistic requiring its own scan,
   its own columns, and a noise guard (a leg 2 points up that gives back 1 point
   registers 50%).
2. Everything it describes is **recoverable from nested legs** (§4.3), with far more
   information: sub-legs give the sizes, times, and *sequence* of the interior moves,
   not one scalar. Ch*** himself enumerates interiors this way — *"there are 2 small
   valid reversals within this upswing"*.
3. `deepest_retr_progress` is stored anyway, so the give-back ratio *at the deepest
   retracement* is one division away if ever wanted.

The one thing nesting cannot see is a retracement **smaller than the fine
threshold**. That is exactly what `deepest_retr_pts` covers — exact at any size, free
in the same pass. The two together are complete.

*(For the record, should it ever be revived: give-back is provably bounded to
[0, 100%]. Inside a confirmed leg, price can never trade below the leg's origin —
doing so would require a drawdown exceeding `T` from the running extreme, which would
have ended the leg first. Verified: 0 violations in 2,424 legs.)*

### 2.8 THE INVARIANT — mandatory acceptance test

> **For every leg with `confirmed = TRUE` at threshold `T`: `deepest_retr_pts < T`.**

This is arithmetic, not an empirical claim: a drawdown of `T` from the running
extreme is precisely the event that terminates the leg. Assert it in tests. If it
fires, the detector is wrong. It caught a real seeding bug during design.

In `close_to_close` this holds by construction. In `extreme_to_extreme` it holds
**provided the terminal-bar scan rule of §2.6 is applied** — without it, every
reversal bar reports a retracement of at least `T`. The invariant is not weaker in
extreme mode; the scan is just easy to define wrongly.

**One consequence must be understood before plotting these columns.**
`deepest_retr_pct_final` is bounded above by `T / magnitude_pts`. A 500-point leg at
`T = 50` can never show more than 10% retracement — arithmetically, regardless of
market behaviour. So **elasticity histograms must be faceted by threshold, and within
a threshold you should not read a size-dependent ceiling as a market fact.** A naive
pooled histogram will "show" that large legs run cleaner than small ones. That is
arithmetic, not the market.

This is a caveat for the Stats tab (§6.3), not an architectural constraint. The leg
count at a given `T` already tells you no `T`-sized retracement occurred inside any
leg — that is what the threshold *means*. Nesting (§4.3) is worth having because
Ch*** described it (macro over micro) and because it reveals interior *structure*;
it is not required to make `elasticity` meaningful.

### 2.9 Scopes

- **`rth`** — detector runs over RTH bars of one session (09:30–15:59). First and
  last pivot both inside RTH. The first pivot generally will **not** be the 09:30
  bar; that is correct behaviour.
- **`eth`** — identical logic over all bars of the ET calendar date, so `rth` and
  `eth` are directly comparable. Note the date's bars themselves straddle the daily
  17:00–18:00 maintenance gap; traverse it **bar-to-bar**, as for `continuous`.
- **`continuous`** — the detector never resets. One pass over the entire minute
  series per instrument. A downmove starting Monday evening and continuing into
  Tuesday remains **one leg**. Traverse the daily 17:00–18:00 maintenance gap
  **bar-to-bar** (no special handling); the reopen is simply the next bar.

---

## 3. Schema changes

### 3.1 `sessions` — add `seq`

```sql
ALTER TABLE sessions ADD COLUMN seq INTEGER;   -- or just rebuild
CREATE INDEX ix_sessions_seq ON sessions(instrument, seq);
```

`seq` is a per-instrument row number ordered by `date`. Compute in `sessions.py`
after the per-instrument sort (the same loop that guards `.shift(1)`).

`seq` counts **sessions**, not calendar days, so `seq - 1` is "the previous
trading session" and weekends/holidays are handled for free. Every offset lookup
(§5) is built on it.

### 3.2 `gyrations` — new table

**Keyed on timestamps, not on `date`.** A continuous leg is not owned by a session;
if you key on `date` you cannot represent it and will refactor later.

```sql
CREATE TABLE gyrations (
  instrument   TEXT NOT NULL,
  scope        TEXT NOT NULL,     -- 'rth' | 'eth' | 'continuous'
  threshold    REAL NOT NULL,     -- points
  mode         TEXT NOT NULL,     -- 'close_to_close' | 'extreme_to_extreme'
  leg_index    INTEGER NOT NULL,  -- order within scope instance
  confirmed    INTEGER NOT NULL,  -- 0/1, see 2.3

  start_ts     TEXT NOT NULL,
  end_ts       TEXT NOT NULL,
  start_date   TEXT NOT NULL,     -- ET date of start_ts
  end_date     TEXT NOT NULL,     -- equals start_date for rth/eth; may differ for continuous
  start_price  REAL NOT NULL,
  end_price    REAL NOT NULL,

  direction    TEXT NOT NULL,     -- 'up' | 'down'
  magnitude_pts REAL NOT NULL,    -- abs(end_price - start_price)
  duration_min INTEGER NOT NULL,
  midprice     REAL NOT NULL,     -- (start_price + end_price) / 2

  deepest_retr_pts       REAL,
  deepest_retr_pct_final REAL,    -- elasticity
  deepest_retr_progress  REAL,
  deepest_retr_start_ts  TEXT,
  deepest_retr_end_ts    TEXT,

  PRIMARY KEY (instrument, scope, threshold, mode, leg_index)
);
CREATE INDEX ix_gyr_lookup ON gyrations(instrument, scope, threshold, mode, start_date);
CREATE INDEX ix_gyr_ts     ON gyrations(instrument, scope, threshold, mode, start_ts);
```

Storing both `start_date` and `end_date` keeps session attribution a trivial query
for `rth`/`eth` while remaining correct for `continuous` legs that straddle days.

**Sizing (measured, 2026 sample, extrapolated to 4,461 sessions).** Leg count scales
roughly as `1/T`: ~103 legs/session at `T=10`, ~21 at `T=50`, ~2.6 at `T=200`. Summed
across the 14 configured thresholds:

| scope × mode | RTH leg-rows |
|---|---|
| `rth` × `close_to_close` | ~1.8M |
| `rth` × `extreme_to_extreme` | ~4.7M |

`eth` yields more legs than `rth` (24h of bars). `continuous` produces one long chain
per instrument. Precomputing every scope × threshold × mode is therefore **not**
advised — hence the `[gyrations].precompute` matrix in §1. Default: `close_to_close`
over `rth` and `eth` only. `continuous` and `extreme_to_extreme` are computed on
demand and cached.

The detector is a single O(n) pass; a full precompute is minutes, not hours. If it
becomes annoying, the inner loop is a textbook numba/Rust candidate — but it is a
one-time ETL cost, not an interactive one.

### 3.3 `sessions` — windowed metric bundles

For each window in `[windows]`, produce a prefixed column group, prefix `win_<name>_`.
`first_90m` yields `win_first_90m_open`, `win_first_90m_high`, … The `win_` prefix
prevents collision with the base `rth_*` / `eth_*` columns. The bundle:

`open, high, low, close, range, high_ts, low_ts, high_minute, low_minute,
high_bucket, low_bucket, bs_sb, rel_close`

where `bs_sb` = `"BS"` if the window's low precedes its high, `"SB"` if the high
precedes the low, `"TIE"` if same bar (see §3.5).

Once gyrations exist, extend each window bundle with leg aggregates, computed **per
threshold** (not for a privileged pair): `num_legs, largest_leg_pts, largest_leg_dir,
sum_leg_pts, net_points`.

Since `thresholds` × `windows` would produce an unwieldy number of session columns,
do **not** flatten these onto `sessions`. Compute them on demand from `gyrations` via
`leg_aggregates()` (§4.1), scoped to the window's bar range, and join at query time.
Cache per `(instrument, window, scope, threshold, mode)`.

`net_points` uses Fu***'s definition, which is his and should be labelled as
such: sum of leg magnitudes, **excluding legs ≤ 5 points**, **minus 5 points per
leg** for dealing cost. Both constants go in config (`[net_points] min_move`,
`cost_per_move`); they are his 2003 spread-bet numbers, not laws.

**Implementation.** These need base aggregation from minute bars, not algebra on
existing session columns, so they cannot be `compute` lambdas. Add
`_build_window_base(minutes_df, name, start, end)` to `sessions.py`, called once
per configured window; left-join each result onto the RTH base. Generate the
corresponding `FeatureSpec` entries in a **loop over `[windows]`**, appended after
the RTH base entries — do not hand-list them, or the registry and config will drift.

Window boundaries are clock ranges, inclusive of the end bar. On half-days a window
whose end lies past the early close simply contains fewer bars; no special case.
Windows with **zero** bars for a session (possible on a heavily truncated half-day)
yield NULLs, not zeros.

### 3.4 Close-based session extremes — new columns

The `gyrations` detector runs on **closes**; the existing `rth_high` / `rth_low` come
from bar **highs** / **lows**. Two different series. Add close-based twins so the
session table can be reconciled with the leg table.

For both RTH and ETH bases, and for each window bundle:

| column | definition |
|---|---|
| `rth_high_close` | max of RTH bar closes |
| `rth_low_close` | min of RTH bar closes |
| `rth_range_close` | `rth_high_close − rth_low_close` |
| `rth_high_close_ts` / `_minute` / `_bucket` | when the highest close occurred |
| `rth_low_close_ts` / `_minute` / `_bucket` | when the lowest close occurred |
| `bs_sb_close` | `"BS"` if lowest close precedes highest close, else `"SB"`, `"TIE"` if same bar |

**Canonical choice.** The extreme-based columns (`rth_high`, `rth_low`, `rth_range`,
`bs_sb`) remain **canonical** — they are what a chart shows and what "the
day's range" means. The close-based twins are provided alongside, and the **legs use
closes**. Consequently `largest_leg_pts` and `rth_range` are measured on different
series *by design* (see §2.3). Do not "fix" this.

**These twins are not cosmetic.** Measured on the 2026 sample (124 sessions):

- `bs_sb_close` disagrees with `bs_sb` on **1.6% of days** (2 of 124). The day's
  classification can flip depending on the series.
- The **time** of the high differs on **60% of days**. Median shift 1 minute — but the
  **maximum observed shift is 265 minutes.** For an engine indexed on timing, this is
  material.
- `rth_range_close` is median **94.2%** of `rth_range` (min 67%).

**Acceptance test:** `rth_range_close <= rth_range` always. Likewise per window.

### 3.5 `bs_sb` — already done, ahead of schedule

**Status: complete as of 2026-07-08, before Phase 2 started.** This section
originally specified adding `bs_sb` as a second, aliasing column derived from
`hl_lh` (`compute` lambda: `LH → "BS"`, `HL → "SB"`, `TIE → "TIE"`). That is
**not** what happened. Instead `hl_lh` was renamed to `bs_sb` outright,
everywhere — registry, ETL, dashboard, docs, and the DB were all rebuilt.
`hl_lh` no longer exists anywhere in the codebase or schema; there is exactly
one column, `bs_sb`, values `"BS"` / `"SB"` / `"TIE"`. Nothing to build here.

The reasoning for the naming still stands and is worth keeping for context: every
conditional statement in the source corpus is phrased as BS/SB, and reading them
against the old geometric `hl_lh` naming inverted the mnemonic every time:

- **BS** (Buy-Sell) = day **low precedes high**
- **SB** (Sell-Buy) = day **high precedes low**

> "a BS day (Buy or low of the day before the Sell or high of the day)" — Ch***

**Do not store `day_leg_direction` or `day_leg_pts`.** They are identically
`bs_sb` and `rth_range`. Three columns that can silently disagree is worse than one.

`main_leg_pts` / `main_leg_dir` are **different quantities** — they come from the
largest leg in `gyrations` at a given threshold, and are functions of `T`. Keep
them strictly separate from `rth_range` / `bs_sb`. Whether and when they agree is
a finding the app produces, not a premise.

### 3.6 `FeatureSpec` — two new fields

```python
FeatureSpec(name, dtype, basis, filter_kind, label,
            compute=None,
            timing="outcome",        # NEW: "pre_open" | "outcome"
            show_in_table=False)     # NEW
```

**`timing`** is orthogonal to `basis` and separates what is knowable at 09:30 from
what is only knowable at 16:00.

- `pre_open`: `weekday`, `is_half_day`, `gap_pts`, `gap_dir`, `rth_open`,
  `open_vs_prev_range*`, and everything at a negative offset.
- `outcome`: `rth_high/low/close`, `rth_range`, `rel_close_*`, `abs_close_*`,
  `bs_sb`, `*_high_minute`, `*_low_minute`, all window bundles, all leg
  aggregates.

Filtering on outcomes is legitimate and useful ("on SB days, when does the low
form?"). Filtering on outcomes and then *predicting* outcomes is lookahead bias.
The UI must make the distinction visible (§6.2); it does not forbid it.

**`show_in_table`** drives the results grid. With windowed bundles the session
table exceeds 40 columns and the hand-built `display` DataFrame in `dashboard.py`
becomes unmaintainable. Drive column selection, ordering, and renaming from the
registry now, while the count is still small.

---

## 4. Query layer

### 4.1 `src/query/legs.py`

```python
legs_for_session(conn, instrument, date, scope, threshold, mode,
                 confirmed_only=True) -> DataFrame
legs_for_sessions(conn, instrument, dates, ...) -> DataFrame     # batched
leg_aggregates(conn, instrument, dates, ...) -> DataFrame        # per-session rollup
```

`confirmed_only=True` is the default everywhere. Session attribution uses
`start_date` for `rth`/`eth`; for `continuous`, "legs overlapping session D" means
`start_date <= D <= end_date`.

### 4.2 Long vs wide

Store legs **long** (one row per leg). Pivot to **wide**
(`leg_1_dir, leg_1_pts, leg_1_end_ts, …`) only for display, because the leg count
varies per session. Never store wide.

### 4.3 Nesting — fine legs inside coarse legs

A coarse leg spans `[start_ts, end_ts]`. A fine leg is **nested inside** it iff
`coarse.start_ts <= fine.start_ts` **and** `fine.end_ts <= coarse.end_ts` — strict
containment, same scope and mode. Fine legs that straddle a coarse pivot (partial
overlap) belong to neither and are excluded from the nesting view; report their count
so they are not silently lost. Provide:

```python
nested_legs(conn, instrument, scope, mode,
            coarse_threshold, fine_threshold, coarse_leg_key) -> DataFrame
```

Any pair from `thresholds` with `fine < coarse` is valid; there is no privileged
micro/macro pair (§1). The UI picks the pair.

This is what maps the **interior structure** of a leg — the sizes, times, and
sequence of the moves inside it. Example: a 30-point up leg at `coarse = 30` that
contains a 20-point down move shows that move as an actual fine leg at `fine = 20`.
It works because a leg at `T = 30` can, by construction, contain counter-moves of up
to 29.99 points.

Ch***, on both the principle and a worked instance:

> "the smaller gyrations within the bigger gyrations"

> "In NG 10.54–15.29 there are 2 small valid reversals within this upswing"

**Limit.** Nesting cannot see a retracement smaller than `fine`. `deepest_retr_pts`
(§2.6) covers that case exactly and for free. Use both.

### 4.4 Offset-based lookup — `src/query/filters.py`

**One mechanism serves both directions.** The two workflows the user described —
"filter on yesterday, look at today" and "filter on today, look at yesterday" —
are the same query with the anchor moved.

Filters become `{(feature_name, offset): value}`, offset an integer in
`[-N … +N]` (UI exposes `D-2, D-1, D, D+1`; the mechanism is general).

```sql
FROM sessions s0
LEFT JOIN sessions sm1 ON sm1.instrument = s0.instrument AND sm1.seq = s0.seq - 1
LEFT JOIN sessions sm2 ON sm2.instrument = s0.instrument AND sm2.seq = s0.seq - 2
LEFT JOIN sessions sp1 ON sp1.instrument = s0.instrument AND sp1.seq = s0.seq + 1
WHERE  <predicates, each against its offset's alias>
```

Only join the aliases an active filter or the display actually needs.

- Filter at `-1`, display `s0` → **forward lookup** ("after days like yesterday…")
- Filter at `0`, display `s-1` → **backward lookup** ("on days like today, what
  did the day before look like?")
- Filter at `-1` **and** `0` → conditional analogs
- Display `s+1` → next-session outcomes

`instrument` and `date_range` remain global params applied to `s0`, not
registry-driven filters.

**Side effect:** `prev_rel_close_dir`, `prev_abs_close_dir`, `prev_bs_sb`, and
`gap_prevclose_combo` become redundant — each is an offset `-1` filter on an
existing feature. Keep them as conveniences (existing dashboards use them), but
mark them deprecated in the registry docstring and do not add more of that shape.
Ch***'s canonical query is then expressed directly:

> weekday = Monday @ offset 0 · gap_dir = down @ offset 0 · abs_close_dir = down @ offset −1

### 4.5 Lookback

Global control, applied after filtering, before statistics:

- `all` (default)
- `last_N occurrences` — the N most recent matching sessions. `N = 4` reproduces
  Ch***'s "last 4 occasions of Mondays being gap down with the previous close down".
- `trailing_months`

Corpus guidance on history length, for the tooltip: *"not less than the last 12
months to date. You may well need to apply the last 2 or 3 years"* (Ch***);
Bl*** suggests the last 1000 sessions; Ze*** gathered 4 years. Ch*** also
warns: *"raw data for any one market will not yield a reliable or playable result
for predictive purposes"* — length of history is not the constraint, derived
metrics are.

---

## 5. Drift diagnostic (replaces normalisation)

We use fixed point thresholds by choice. The one safeguard, requiring no theory:

**Plot legs-per-session at fixed `T`, over time.** If the count trends materially
across the working window, the threshold is silently drifting relative to
volatility and the window should be shortened. If flat, points are fine and the
question is closed empirically.

Pure counting — addition and filtering, nothing else. Put it on the Stats tab.

---

## 6. Dashboard

`dashboard.py` becomes a tabbed multi-panel app. Panel A is what exists today.

### 6.1 Global controls (sidebar, above filters)

- Instrument
- Chart basis: RTH / ETH  *(existing)*
- Date range  *(existing)*
- **Gyration controls:** scope, threshold (select from `thresholds`), mode
  (`close_to_close` default), `confirmed only` toggle (default on). For the nesting
  view, a second **fine threshold** selector, constrained to values below the primary.
- **Lookback:** all / last N / trailing months
- **Display offset:** which of `D-2 … D+1` the results table shows

### 6.2 Filter panel

Group filters by `timing` **first**, then by `basis`:

```
▾ Conditioning — known at the open
    ▾ context   ▾ RTH   ▾ ETH
▾ Outcome — known only at the close      ⚠ lookahead
    ▾ context   ▾ RTH   ▾ ETH   ▾ windows   ▾ legs
```

Each filter group carries an offset selector (`D-2 / D-1 / D / D+1`), defaulting
to `D`. When any `outcome` filter is active at offset ≥ 0, show a non-blocking
warning banner. Do not disable it — it is a legitimate research mode.

### 6.3 Tabs

1. **Browse** — the existing table, now registry-driven via `show_in_table`, plus
   `bs_sb` and per-row leg summary (`num_legs`, `largest_leg_pts`,
   `largest_leg_dir` at the selected threshold). Click-to-chart as today.

   **Gyration overlay — this is a verification tool, not decoration.** Toggle
   (default off) drawing, over the Plotly candlesticks:
   - a polyline through the pivots, so the detected legs are visually traceable;
   - a marker at every pivot, labelled with its price and time;
   - the leg magnitude annotated on each segment;
   - a shaded span over the deepest retracement of the selected leg;
   - unconfirmed legs (first/last) drawn **dashed**, so their exclusion from stats
     is visible rather than mysterious.

   Changing the threshold must redraw immediately. The invariants (§2.8, §2.4) prove
   the detector is *self-consistent*; only this overlay proves it is finding the
   swings a human would call swings. Build it early — it is the fastest way to catch
   a detector that is technically correct and practically wrong.

   Because legs run on closes and candles show highs/lows (§2.3, §3.4), pivots will
   sit at candle *bodies*, not wicks. Expected, not a bug. Offer a "show close-based
   HOD/LOD" toggle alongside the extreme-based HOD/LOD markers so the two can be
   compared on the same chart.

2. **Stats** — computed on the filtered set. The headline number is
   **`BS%` versus the 50% baseline.**

   In the 2026 sample, unconditional BS/SB is 61/63 — a coin flip. The entire edge
   therefore lives in conditioning that flip. If a filter does not move `BS%`,
   the filter is worthless. Show `n`, `BS%`, and a plain binomial interval so a
   move from 50% on `n = 12` is not mistaken for a discovery.

   Also: distributions of `rel_close`, `rth_range`, `high_minute` / `low_minute`
   (30-min buckets), leg counts, `magnitude_pts`, `duration_min`, `elasticity` —
   the last three **faceted by threshold**, never pooled across thresholds or modes (§2.8, §2.2).
   Plus the drift diagnostic (§5).

3. **Legs** — long-format leg table for the filtered sessions, and across-set
   aggregates. Nesting view: select a coarse leg, see the fine legs inside it (§4.3).

4. **Lookup** — forward/backward. Given the active filter at its offsets, show the
   matched set and the distribution of the displayed offset's outcomes. This is
   where "after days like yesterday, what happened?" is answered.

`template` column: present, NULL, not displayed. Phase 3+.

---

## 7. Work order

1. `seq` on `sessions` + offset joins in `filters.py`. Self-contained; unlocks the
   Lookup tab and immediately reproduces Ch***'s Monday query.
2. `FeatureSpec.timing` + `show_in_table`; regroup the sidebar; drive the results
   table from the registry. Cheap, and stops the display-column drift now.
3. Close-based session extremes (§3.4). Pure aggregation, no new machinery.
   (`bs_sb` itself is already done — see §3.5 — so this step is now just the
   close-based twins.)
4. `src/gyrations/detect.py` + `gyrations` table + ETL wiring. **Write the property
   tests P1–P8 (§2.4) and the invariant (§2.8) before the detector**, not after.
5. **The gyration overlay on the session chart (§6.3, Browse tab)** — immediately
   after the detector, before any statistics are computed on legs. Numbers derived
   from a subtly wrong detector are worse than no numbers, and the overlay is the
   only check that the legs match what a human would call swings.
6. Window bundles in `sessions.py` + generated registry entries.
7. Leg aggregates over window bundles, computed on demand (needs 4 and 6).
8. Remaining dashboard tabs; Stats; Legs; nesting view; drift diagnostic.

Steps 1–3 are independent of 4–7 and can land first.

---

## 8. Acceptance tests

- **Invariant:** every `confirmed` leg at threshold `T` has `deepest_retr_pts < T`.
  Run across all scopes, thresholds, and **both** modes. Non-negotiable. If it fails
  in `extreme_to_extreme`, the terminal-bar scan rule (§2.6) is missing — fix the
  scan, never the test.
- **`bar_direction` tie-break:** on a bar with `close >= open` the low is taken first;
  otherwise the high. Unit-test both orderings against a hand-built bar that would
  produce different pivots under each.
- **Properties P1–P8 (§2.4)**, each as its own test. P1 (strictly increasing pivot
  indices) and P3 (two-sided seeding on a rise-then-fall series) are the two that
  caught real bugs; do not skip them.
- `extreme_to_extreme` yields strictly more legs than `close_to_close` at the same
  `(scope, threshold)`. If it ever yields fewer, the tie-break is inverted.
- `rth_range_close <= rth_range` for every session, and likewise per window (§3.4).
- A scope with fewer than 4 pivots yields zero confirmed legs (§2.5).
- Pivots recorded are extremes, never the confirming bar. Assert
  `end_price == max(series[i0..i1])` for an up leg (`min` for down).
- Leg magnitudes at threshold `T` are all `>= T`, except on unconfirmed legs.
- Leg count is monotonically non-increasing in `T` for a fixed scope/session.
- `seq` is dense and gapless per instrument; `seq - 1` never crosses instruments.
- `continuous` scope produces exactly one unconfirmed leg at each end of the series.
- Window bundles: `first_30m` ⊂ `first_60m` ⊂ `first_90m` ⊂ `rth` for high/low
  (a wider window's high is `>=` a narrower one's).
- A session with no `T`-sized reversal yields zero legs, not one synthetic leg.
- Offset queries: filtering at offset `-1` on feature `X` returns the same set as
  filtering at offset `0` on the legacy `prev_X` column, where such a column exists.

---

## 9. Sanity numbers

Measured during design on **one 6-month sample** — 2026 H1 DOW, 124 RTH sessions,
1-minute bars, `close_to_close`, using the corrected detector of §2.4. These are
implementation checks, **not market truths**, and they will not match the 17-year set.

| | T = 50 | T = 100 |
|---|---|---|
| total legs | 2,748 | 1,028 |
| legs / session | 22.2 | 8.3 |
| confirmed legs | 2,500 | 780 |
| median leg magnitude | 98 pts | 182 pts |
| median `deepest_retr_pts` | 18 pts | 47 pts |
| median `deepest_retr_pct_final` | 14.5% | 22.0% |
| max `deepest_retr_pts` observed | **49.6** | **99.5** |
| legs with `deepest_retr_pts < 0.2T` | 35% | 19% |

The `max deepest_retr_pts` row is the invariant holding (§2.8) — it must always fall
strictly below `T`.

**Leg density by threshold** (mean legs/session, RTH, `close_to_close`, 1-min bars).
Use to sanity-check the detector and to size the tables:

| T | 10 | 15 | 20 | 30 | 40 | 50 | 60 | 70 | 80 | 90 | 100 | 120 | 150 | 200 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| legs/session | 103.3 | 77.0 | 59.9 | 39.7 | 28.5 | 22.2 | 16.8 | 13.4 | 11.0 | 9.3 | 8.3 | 5.7 | 4.0 | 2.6 |
| median magnitude | 31 | 40 | 49 | 66 | 82 | 98 | 112 | 125 | 142 | 156 | 175 | 200 | 235 | 276 |

Leg count is monotonically decreasing in `T` (property P8) — a cheap regression test.

**Close-vs-extreme session extremes** (§3.4): `bs_sb_close` disagrees with `bs_sb` on
1.6% of days; the high's time differs on 60% of days (median 1 min, max 265 min);
`rth_range_close` is median 94.2% of `rth_range`.

Unconditional `BS`/`SB` on the same sample: 61 / 63.
Median time of the first RTH extreme: **09:41** (BS days) / **09:40** (SB days).
Compare Bl***: *"the high or low of the day occurred within 8 minutes of the open
a considerable percentage of the time"* — an independent claim from ~2011 that the
data does not contradict.
