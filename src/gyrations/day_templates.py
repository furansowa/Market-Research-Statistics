r"""Day Templates: classify each RTH session by what its chart LOOKS LIKE.

Bryce Gilmore's A / V / N / reversed-N / M / W day types, generalised. The
point of this module (and what makes it different from `shapes.py`) is
SCALE INVARIANCE in both axes:

  price -- every session is min-max normalised to [0,1] over its OWN high/low,
           so a 100pt V day and a 350pt V day produce the same profile and
           land in the same template.
  time  -- every session is resampled to a fixed K buckets of equal elapsed
           time, so US30's 390-minute RTH and DAX's 510-minute RTH are
           directly comparable, and so is a half-day holiday session.

`shapes.py` classifies from the stored gyration legs at a FIXED point
threshold (40/120/200pts). That makes a quiet 60pt day and a wild 400pt day
incomparable -- the quiet day has no legs at all at a 120pt threshold. This
module deliberately re-derives the main legs from the normalised profile at a
threshold expressed as a PERCENTAGE of the session's own range, which is what
"visually similar" requires. Both views are useful; they answer different
questions, so neither replaces the other.

Why a purpose-built zigzag rather than reusing detect.detect_pivots_*:
that detector is built for a continuous multi-session scan with confirmation
semantics and its own unseeded-phase bookkeeping, and it deliberately does
not anchor to a session open. A day's visual shape must start AT THE OPEN
(that is where the chart starts and where the eye starts), so the walk here
seeds from the open price and always emits the open as pivot 0. It is ~40
lines and fully self-contained.

The "cleanliness" score is the day's real path measured against the
piecewise-linear path through its own main legs -- i.e. how well those legs
actually describe the chart. A textbook V scores near 1.0; a V whose two legs
hide a lot of internal chop scores lower. That is a better notion of
"looks like the template" than distance to a rigid ideal shape, because real
V days put their low anywhere from 20% to 80% of the way through the session
and should not be penalised for it.
"""

from __future__ import annotations

import math

K_BUCKETS = 48

# Direction string (U = swing up, D = swing down) -> template name.
# Strings are read left to right in time. Anything longer than 5 swings is
# bucketed as Complex, keyed on which way it started.
TEMPLATE_NAMES: dict[str, str] = {
    "": "Flat / Range",
    "U": "/  Trend Up",
    "D": "\\  Trend Down",
    "UD": "A",
    "DU": "V",
    "UDU": "N",
    "DUD": "N reversed",
    "UDUD": "M",
    "DUDU": "W",
    "UDUDU": "M extended",
    "DUDUD": "W extended",
}

# Display order for the page's card grid.
TEMPLATE_ORDER: tuple[str, ...] = (
    "/  Trend Up", "\\  Trend Down",
    "A", "V",
    "N", "N reversed",
    "M", "W",
    "M extended", "W extended",
    "Complex (up first)", "Complex (down first)",
    "Flat / Range",
)


def template_for(dirs: str) -> str:
    """Template name for a swing-direction string."""
    if dirs in TEMPLATE_NAMES:
        return TEMPLATE_NAMES[dirs]
    return "Complex (up first)" if dirs.startswith("U") else "Complex (down first)"


# ---------------------------------------------------------------------------
# profile construction
# ---------------------------------------------------------------------------

def build_profile(highs, lows, closes, opens, k: int = K_BUCKETS) -> dict | None:
    """Resample one RTH session's bars into a scale-invariant profile.

    Returns None when the session is unusable (too few bars, or zero range --
    a zero-range session has no shape and would divide by zero).

    The returned h/l/c arrays are normalised so 0.0 is the session low and
    1.0 the session high. Bucket highs/lows are kept (not just closes) so the
    main-leg walk still sees the real extremes after resampling.
    """
    n = len(closes)
    if n < k or n == 0:
        return None
    hi, lo = max(highs), min(lows)
    rng = hi - lo
    if rng <= 0:
        return None

    def norm(v):
        return (v - lo) / rng

    bh, bl, bc = [], [], []
    hi_idx = lo_idx = 0
    for b in range(k):
        i0 = (b * n) // k
        i1 = max(((b + 1) * n) // k, i0 + 1)
        bh.append(norm(max(highs[i0:i1])))
        bl.append(norm(min(lows[i0:i1])))
        bc.append(norm(closes[i1 - 1]))

    for i in range(n):
        if highs[i] >= highs[hi_idx]:
            hi_idx = i
        if lows[i] <= lows[lo_idx]:
            lo_idx = i

    return {
        "h": bh, "l": bl, "c": bc,
        "open_pos": norm(opens[0]),
        "close_pos": norm(closes[-1]),
        "high_frac": hi_idx / max(n - 1, 1),
        "low_frac": lo_idx / max(n - 1, 1),
        "range_pts": rng,
        "n_bars": n,
    }


# ---------------------------------------------------------------------------
# main legs
# ---------------------------------------------------------------------------

def main_legs(h: list[float], l: list[float], open_pos: float, threshold: float
              ) -> list[tuple[int, float]]:
    """Zigzag over the normalised profile, anchored at the session open.

    `threshold` is in normalised units, so 0.25 means "a swing must retrace
    25% of the session's own range to count" -- the scale-invariant knob.

    Returns pivots as (bucket_index, normalised_price), starting with the
    open at index 0 and ending at the final running extreme.
    """
    n = len(h)
    pivots: list[tuple[int, float]] = [(0, open_pos)]
    if n == 0:
        return pivots

    dirn: str | None = None
    ext_i, ext_p = 0, open_pos

    for i in range(n):
        if dirn == "up":
            if h[i] >= ext_p:
                ext_i, ext_p = i, h[i]
            elif ext_p - l[i] >= threshold:
                pivots.append((ext_i, ext_p))
                dirn, ext_i, ext_p = "down", i, l[i]
        elif dirn == "down":
            if l[i] <= ext_p:
                ext_i, ext_p = i, l[i]
            elif h[i] - ext_p >= threshold:
                pivots.append((ext_i, ext_p))
                dirn, ext_i, ext_p = "up", i, h[i]
        else:
            # unseeded: whichever side clears the threshold from the OPEN
            # first sets the initial direction, and the extreme carried
            # forward is that side's running extreme, not the triggering bar.
            if h[i] - open_pos >= threshold:
                dirn = "up"
                ext_i, ext_p = max(
                    ((j, h[j]) for j in range(i + 1)), key=lambda t: t[1]
                )
            elif open_pos - l[i] >= threshold:
                dirn = "down"
                ext_i, ext_p = min(
                    ((j, l[j]) for j in range(i + 1)), key=lambda t: t[1]
                )

    if dirn is not None:
        pivots.append((ext_i, ext_p))
    return pivots


def directions(pivots: list[tuple[int, float]]) -> str:
    """U/D string for consecutive pivots (flat steps are dropped)."""
    out = []
    for (_, a), (_, b) in zip(pivots, pivots[1:]):
        if b > a:
            out.append("U")
        elif b < a:
            out.append("D")
    return "".join(out)


def cleanliness(c: list[float], pivots: list[tuple[int, float]]) -> float:
    """1 - RMS deviation of the real close path from the piecewise-linear path
    through the main-leg pivots. 1.0 = the legs describe the chart perfectly;
    lower = more internal chop hidden inside the legs.

    Clamped at 0. Prices are already normalised to [0,1], so the RMS is
    directly a fraction of the session range and needs no further scaling.
    """
    n = len(c)
    if n == 0 or len(pivots) < 2:
        return 0.0
    recon = [0.0] * n
    for (i0, p0), (i1, p1) in zip(pivots, pivots[1:]):
        span = max(i1 - i0, 1)
        for i in range(i0, min(i1 + 1, n)):
            recon[i] = p0 + (p1 - p0) * (i - i0) / span
    last_i = min(pivots[-1][0], n - 1)
    for i in range(last_i, n):
        recon[i] = pivots[-1][1]
    sq = sum((c[i] - recon[i]) ** 2 for i in range(n)) / n
    return max(0.0, 1.0 - math.sqrt(sq) * 2.0)


def classify(profile: dict, threshold: float) -> dict:
    """Full classification of one profile at one threshold."""
    pivots = main_legs(profile["h"], profile["l"], profile["open_pos"], threshold)
    dirs = directions(pivots)
    return {
        "template": template_for(dirs),
        "dirs": dirs,
        "n_legs": len(dirs),
        "pivots": pivots,
        "cleanliness": cleanliness(profile["c"], pivots),
    }
