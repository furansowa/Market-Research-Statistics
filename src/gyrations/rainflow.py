"""Rainflow cycle counting (ASTM E1049 4-point method) -- pure, dependency-free.

Standard metal-fatigue algorithm (Matsuishi & Endo 1968), applied here to
price paths. Takes an irregular series, reduces it to turning points, and
decomposes it into closed cycles each with a RANGE (peak-to-valley size) and
a MEAN (midpoint level).

Its defining property is NESTING: a small oscillation riding on top of a large
excursion is extracted as its own small cycle while the large excursion still
survives intact as one large cycle. Naive peak-to-peak counting destroys the
large move; rainflow preserves the whole hierarchy at every scale at once --
which is why it needs no size threshold, unlike the zigzag leg detector in
detect.py (that one requires a fixed `threshold` and so sees only one scale
per run).

4-point rule: over four consecutive turning points p1..p4 with ranges
r1=|p2-p1|, r2=|p3-p2|, r3=|p4-p3|, if r2 <= r1 AND r2 <= r3 then the inner
pair (p2,p3) is a closed full cycle -- record it, delete p2 and p3, re-check.
Whatever cannot be extracted is the `residue`, counted as half-cycles.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Cycle:
    rng: float      # peak-to-valley size
    mean: float     # midpoint level of the two reversals
    close_index: int  # series index at which this cycle CLOSED (its 4th point)
    full: bool      # True = full cycle (weight 1.0), False = residue half-cycle (0.5)

    @property
    def weight(self) -> float:
        return 1.0 if self.full else 0.5


def turning_points(series: Sequence[float]) -> list[tuple[int, float]]:
    """(index, value) of the first point, every direction change, and the last
    point. Consecutive equal values are collapsed first so a flat spot can't
    produce a spurious non-alternating 'turn'."""
    if not series:
        return []
    pts = [(0, series[0])]
    for i in range(1, len(series)):
        if series[i] != pts[-1][1]:
            pts.append((i, series[i]))
    if len(pts) < 3:
        return pts

    out = [pts[0]]
    for a, b, c in zip(pts, pts[1:], pts[2:]):
        if (b[1] - a[1]) * (c[1] - b[1]) < 0:
            out.append(b)
    out.append(pts[-1])
    return out


def count_cycles(series: Sequence[float]) -> list[Cycle]:
    """Full rainflow decomposition. Returns closed cycles (full=True) followed
    by the residue as half-cycles (full=False)."""
    tps = turning_points(series)
    if len(tps) < 2:
        return []

    stack: list[tuple[int, float]] = []
    cycles: list[Cycle] = []

    for point in tps:
        stack.append(point)
        while len(stack) >= 4:
            r1 = abs(stack[-3][1] - stack[-4][1])
            r2 = abs(stack[-2][1] - stack[-3][1])
            r3 = abs(stack[-1][1] - stack[-2][1])
            if r2 <= r1 and r2 <= r3:
                p2, p3 = stack[-3], stack[-2]
                cycles.append(Cycle(
                    rng=r2,
                    mean=(p2[1] + p3[1]) / 2.0,
                    close_index=stack[-1][0],
                    full=True,
                ))
                del stack[-3:-1]
            else:
                break

    for a, b in zip(stack, stack[1:]):
        cycles.append(Cycle(
            rng=abs(b[1] - a[1]),
            mean=(a[1] + b[1]) / 2.0,
            close_index=b[0],
            full=False,
        ))
    return cycles


def miner_damage(cycles: Sequence[Cycle], m: float, capacity: float = 1.0,
                 up_to_index: int | None = None) -> float:
    """Palmgren-Miner accumulated damage under a Basquin S-N curve
    N(S) = capacity * S**-m, so damage per cycle = weight * S**m / capacity.
    Failure in the engineering model is D >= 1.

    `up_to_index`: only count cycles that had closed by that series index --
    this is what makes damage a running quantity through the session rather
    than a single end-of-day total.
    """
    total = 0.0
    for c in cycles:
        if up_to_index is not None and c.close_index > up_to_index:
            continue
        total += c.weight * (c.rng ** m)
    return total / capacity
