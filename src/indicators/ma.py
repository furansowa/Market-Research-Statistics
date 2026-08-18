"""EMA / DEMA -- pure, dependency-free (numpy only).

Mulloy's DEMA: EMA1 = EMA(price, n), EMA2 = EMA(EMA1, n), DEMA = 2*EMA1 - EMA2.
SMA-seeded warmup (the first `n` values average into the seed, matching the
convention used throughout this project's scratchpad DEMA/EMA work).
"""

from __future__ import annotations

import numpy as np


def ema(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < period:
        return out
    a = 2.0 / (period + 1)
    prev = float(np.mean(x[:period]))
    out[period - 1] = prev
    for i in range(period, len(x)):
        prev = a * x[i] + (1 - a) * prev
        out[i] = prev
    return out


def dema(x: np.ndarray, period: int) -> np.ndarray:
    e1 = ema(x, period)
    e2 = np.full(len(x), np.nan)
    start = period - 1
    e2[start:] = ema(e1[start:], period)
    return 2 * e1 - e2
