"""Arthur Merrill M/W 4-leg pattern classification (Gyrational Waves v1.0).

A pattern is 4 consecutive legs (5 pivots p0..p4, p0 = start of the 1st leg,
p4 = end of the 4th/"observed" leg). Pivots are ranked by price: 1 = highest,
5 = lowest (ties -- exact float equality -- broken by chronological order,
negligible in practice). The resulting 5-digit rank string (e.g. "21435") is
looked up in `LABELS` below to get the pattern's name.

"M" family = patterns whose observed (4th) leg is a down move; "W" family =
observed leg is an up move (verified self-consistent with the tables below:
every M rank-string decodes to up/down/up/down, every W to down/up/down/up --
legs always alternate direction in this app's leg detector, so checking
either the 1st or 4th leg's direction is equivalent; the 4th leg's `direction`
field is used directly in `build_patterns` since it's already on hand there).
"""

from __future__ import annotations

from dataclasses import dataclass

_M_LABELS = {
    "21435": "M1", "21534": "M2", "31425": "M3", "31524": "M4",
    "32415": "M5", "32514": "M6", "41325": "M7", "41523": "M8",
    "42315": "M9", "42513": "M10", "43512": "M11", "51324": "M12",
    "51423": "M13", "52314": "M14", "52413": "M15", "53412": "M16",
}
_W_LABELS = {
    "13254": "W1", "14253": "W2", "14352": "W3", "15243": "W4",
    "15342": "W5", "23154": "W6", "24153": "W7", "24351": "W8",
    "25143": "W9", "25341": "W10", "34152": "W11", "34251": "W12",
    "35142": "W13", "35241": "W14", "45132": "W15", "45231": "W16",
}
LABELS: dict[str, str] = {**_M_LABELS, **_W_LABELS}
M_LABELS: tuple[str, ...] = tuple(_M_LABELS.values())
W_LABELS: tuple[str, ...] = tuple(_W_LABELS.values())
ALL_LABELS: tuple[str, ...] = M_LABELS + W_LABELS


def _rank_string(pivots: list[float]) -> str:
    order = sorted(range(len(pivots)), key=lambda i: (-pivots[i], i))
    ranks = [0] * len(pivots)
    for rank, idx in enumerate(order, start=1):
        ranks[idx] = rank
    return "".join(str(r) for r in ranks)


def classify_pivots(pivots: list[float]) -> tuple[str, str]:
    """`pivots`: [p0, p1, p2, p3, p4] prices. Returns (family, label), e.g.
    ("M", "M7"). Raises ValueError if the 5 pivots don't rank into one of the
    32 valid alternating-zigzag patterns -- shouldn't happen for legs coming
    straight out of this app's leg detector (consecutive legs always
    alternate direction), so a mismatch here would mean the caller passed
    something other than 4 consecutive legs' pivots.
    """
    rank_str = _rank_string(pivots)
    label = LABELS.get(rank_str)
    if label is None:
        raise ValueError(f"Pivot ranks '{rank_str}' don't match any Merrill M/W pattern")
    family = "M" if label[0] == "M" else "W"
    return family, label


@dataclass
class Pattern:
    leg_index: int  # position of the observed (4th) leg within the leg list build_patterns was given
    legs: list[dict]  # the 4 underlying leg rows, chronological
    pivots: list[float]  # [p0, p1, p2, p3, p4]
    ranks: str
    family: str  # "M" | "W"
    label: str  # e.g. "M7" / "W12"
    start_date: str
    end_date: str


def build_patterns(legs: list[dict]) -> dict[int, Pattern]:
    """`legs`: chronologically ordered leg rows (dicts with start_price,
    end_price, start_date, end_date -- see query.gyr_waves.fetch_legs), all
    the same (instrument, scope, threshold, mode). One `Pattern` per leg index
    i >= 3, keyed by i -- that leg is the window's observed/4th leg, using
    legs[i-3:i+1]. Sliding, one pattern per leg (not non-overlapping blocks):
    this makes "the next pattern" (starts at this pattern's last pivot, i.e.
    ends 4 legs later) simply `patterns[i + 4]`, and "the next leg" simply
    `legs[i + 1]` -- both plain index lookups against this same numbering.
    """
    patterns: dict[int, Pattern] = {}
    for i in range(3, len(legs)):
        window = legs[i - 3:i + 1]
        pivots = [window[0]["start_price"]] + [leg["end_price"] for leg in window]
        rank_str = _rank_string(pivots)
        label = LABELS.get(rank_str)
        if label is None:
            continue  # malformed window (shouldn't happen); skip rather than crash a whole page
        family = "M" if label[0] == "M" else "W"
        patterns[i] = Pattern(
            leg_index=i, legs=window, pivots=pivots, ranks=rank_str,
            family=family, label=label,
            start_date=window[0]["start_date"], end_date=window[-1]["end_date"],
        )
    return patterns
