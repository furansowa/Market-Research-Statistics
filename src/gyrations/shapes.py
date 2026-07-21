r"""Macro shape templates: classify each session by HOW its running HOD/LOD
was built chronologically, from the confirmed extreme_to_extreme legs.

Core idea (user spec, 2026-07-21): a leg pivot only matters for the session's
"shape" if it was the running session extreme at its time. Counter-legs that
never take out a running high/low get absorbed into the bigger macro swing —
so a 10-leg day whose down legs never break a prior swing low is still a "/"
day, and a 5-leg day can be an N day (see the user's chart example: legs
up 51030 / down 50760 / up 50970 / down 50830 / up 51120 -> N, because 50970
and 50830 were never running extremes).

Algorithm:
1. Pivot sequence = first leg's start + each leg's end (alternating H/L).
2. A pivot is a MACRO pivot iff its price is >= the running max of prior
   pivots (for H) or <= the running min (for L). Consecutive same-side macro
   pivots merge, keeping the later (more extreme) one.
3. The first-leg-start pivot is dropped: it is always <120pts from the open
   by construction (a bigger excursion would itself have been a leg), so the
   shape path effectively starts at the open.
4. Shape = the direction string of macro swings (U = swing up to a new
   running high, D = swing down to a new running low). The path ends at the
   final session extreme; the fade into the close after it adds no swing and
   is reported separately.

Names: U=/  D=\  UD=A  DU=V  UDU=N  DUD=\/\  UDUD=M  DUDU=W;
longer strings are M+k (starts up) / W+k (starts down), k = swings - 4.
A session with no confirmed leg is "flat".
"""

from __future__ import annotations

_SHAPE_NAMES = {
    "": "flat",
    "U": "/",
    "D": "\\",
    "UD": "A",
    "DU": "V",
    "UDU": "N",
    "DUD": "\\/\\",
    "UDUD": "M",
    "DUDU": "W",
}


def macro_pivots(legs: list[dict]) -> list[tuple]:
    """Alternating running-extreme pivots as (ts, price, side) with side
    "H"/"L". Includes the leading first-leg-start pivot (index 0); callers
    that want the shape path should drop it (see `classify_session`).

    `legs` must be the session's confirmed legs ordered by start_ts, each
    with keys direction/start_price/end_price/start_ts/end_ts.
    """
    if not legs:
        return []
    first = legs[0]
    pivots = [(first["start_ts"], first["start_price"],
               "L" if first["direction"] == "up" else "H")]
    for leg in legs:
        pivots.append((leg["end_ts"], leg["end_price"],
                       "H" if leg["direction"] == "up" else "L"))
    run_hi = -float("inf")
    run_lo = float("inf")
    macro: list[tuple] = []
    for ts, price, side in pivots:
        if side == "H":
            if price >= run_hi:
                run_hi = price
                if macro and macro[-1][2] == "H":
                    macro[-1] = (ts, price, "H")
                else:
                    macro.append((ts, price, "H"))
        else:
            if price <= run_lo:
                run_lo = price
                if macro and macro[-1][2] == "L":
                    macro[-1] = (ts, price, "L")
                else:
                    macro.append((ts, price, "L"))
    return macro


def shape_name(swings: str) -> str:
    if swings in _SHAPE_NAMES:
        return _SHAPE_NAMES[swings]
    k = len(swings) - 4
    return f"M+{k}" if swings[0] == "U" else f"W+{k}"


def classify_session(legs: list[dict]) -> dict:
    """Classify one session's confirmed legs (ordered by start_ts).

    Returns a dict with:
    - shape: template name ("/", "\\", "V", "A", "N", "\\/\\", "M", "W",
      "M+k", "W+k", or "flat")
    - swings: the U/D macro swing string ("" when flat)
    - n_swings, n_legs
    - pivots: the retained macro pivots [(ts, price, side), ...] — these are
      the yellow-line vertices; the last one is the later of HOD/LOD
    - path_start: the dropped leading pivot (ts, price, side), or None —
      the true price-path origin, useful as the first swing's start point
    """
    macro_full = macro_pivots(legs)
    macro = macro_full[1:]
    swings = "".join("U" if side == "H" else "D" for _, _, side in macro)
    return {
        "shape": shape_name(swings),
        "swings": swings,
        "n_swings": len(swings),
        "n_legs": len(legs),
        "pivots": macro,
        "path_start": macro_full[0] if macro_full else None,
    }
