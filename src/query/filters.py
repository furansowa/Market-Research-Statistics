"""Offset-based session lookup over the `sessions` table (Phase 2 spec §4.4).

One mechanism serves both forward and backward lookup: filters are keyed by
`(feature_name, offset)`, where offset is an integer session count relative to
an anchor session `s0` (0 = the anchor itself, -1 = the previous session,
+1 = the next session, etc.), joined via `seq` (a dense per-instrument row
number — see `features.registry`'s `seq` FeatureSpec and `etl.sessions`).

- Filter at offset -1, display offset 0  -> forward lookup ("after days like
  yesterday, what happened today?")
- Filter at offset 0, display offset -1  -> backward lookup ("on days like
  today, what did the day before look like?")
- Filter at offsets -1 and 0 together    -> conditional analogs

`instrument` and `date_range` are global params applied to the anchor `s0`,
not registry-driven filters — they scope *which anchor sessions* are
considered, independent of which offset ends up filtered/displayed.

Only the offsets actually referenced (by a filter or by `display_offset`) are
joined, so a plain same-session query (the common case) is a single-table
SELECT with no joins at all.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Union

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from features.registry import REGISTRY_BY_NAME, filterable_features

RangeValue = tuple[float, float]
SelectValue = list[str]
BoolValue = bool
FilterValue = Union[RangeValue, SelectValue, BoolValue]
FilterKey = tuple[str, int]  # (feature_name, offset)


@dataclass
class WhereClause:
    sql: str
    params: list


def _alias(offset: int) -> str:
    if offset == 0:
        return "s0"
    return f"s_m{abs(offset)}" if offset < 0 else f"s_p{offset}"


def column_range(conn: sqlite3.Connection, column: str, table: str = "sessions") -> tuple[float, float]:
    row = conn.execute(f'SELECT MIN("{column}"), MAX("{column}") FROM {table}').fetchone()
    return (row[0], row[1])


def distinct_values(conn: sqlite3.Connection, column: str, table: str = "sessions") -> list[str]:
    rows = conn.execute(
        f'SELECT DISTINCT "{column}" FROM {table} WHERE "{column}" IS NOT NULL ORDER BY "{column}"'
    ).fetchall()
    return [r[0] for r in rows]


def build_where(filters: dict[FilterKey, FilterValue]) -> WhereClause:
    """filters: {(feature_name, offset): value}. Empty/None entries are skipped."""
    clauses: list[str] = []
    params: list = []

    for (name, offset), value in filters.items():
        if value is None:
            continue
        spec = REGISTRY_BY_NAME.get(name)
        if spec is None:
            raise KeyError(f"Unknown feature: {name}")

        col = f'{_alias(offset)}."{name}"'

        if spec.filter_kind == "range":
            lo, hi = value
            clauses.append(f'{col} BETWEEN ? AND ?')
            params.extend([lo, hi])
        elif spec.filter_kind == "select":
            values = list(value)
            if not values:
                continue
            placeholders = ", ".join("?" for _ in values)
            clauses.append(f'{col} IN ({placeholders})')
            params.extend(values)
        elif spec.filter_kind == "bool":
            clauses.append(f'{col} = ?')
            params.append(1 if value else 0)
        else:
            raise ValueError(f"Feature '{name}' is not filterable (filter_kind={spec.filter_kind})")

    sql = " AND ".join(clauses) if clauses else "1=1"
    return WhereClause(sql=sql, params=params)


def query_sessions(
    conn: sqlite3.Connection,
    filters: dict[FilterKey, FilterValue] | None = None,
    instrument: str | None = None,
    date_range: tuple[str, str] | None = None,
    display_offset: int = 0,
    order_by: str = "date",
) -> list[dict]:
    filters = filters or {}

    offsets_needed = {offset for (_, offset) in filters.keys()}
    offsets_needed.add(display_offset)
    offsets_needed.discard(0)

    join_sql = " ".join(
        f'LEFT JOIN sessions {_alias(off)} '
        f'ON {_alias(off)}.instrument = s0.instrument AND {_alias(off)}.seq = s0.seq + ({off})'
        for off in sorted(offsets_needed, key=abs)
    )

    where = build_where(filters)
    clauses = [where.sql]
    params = list(where.params)

    if instrument is not None:
        clauses.append('s0."instrument" = ?')
        params.append(instrument)
    if date_range is not None:
        clauses.append('s0."date" BETWEEN ? AND ?')
        params.extend(date_range)

    display_alias = _alias(display_offset)
    sql = (
        f'SELECT {display_alias}.* FROM sessions s0 {join_sql} '
        f'WHERE {" AND ".join(clauses)} ORDER BY s0."{order_by}"'
    )

    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    results = [dict(r) for r in rows]
    # Drop anchors where the displayed offset falls outside recorded history
    # (e.g. display_offset=+1 for the most recent session in the DB).
    return [r for r in results if r.get("date") is not None]
