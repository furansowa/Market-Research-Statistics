"""Write minutes + sessions to SQLite, build indexes (SPEC.md section 7).

Table DDL is derived from the actual Polars schema of what's being written
(after stringifying Date/Datetime columns to ISO text) rather than hand-typed,
so adding a column to the sessions feature set doesn't require touching this
file — only sessions.py / the registry.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import polars as pl

_SQLITE_TYPE = {
    pl.Float32: "REAL",
    pl.Float64: "REAL",
    pl.Boolean: "INTEGER",
    pl.Int8: "INTEGER", pl.Int16: "INTEGER", pl.Int32: "INTEGER", pl.Int64: "INTEGER",
    pl.UInt8: "INTEGER", pl.UInt16: "INTEGER", pl.UInt32: "INTEGER", pl.UInt64: "INTEGER",
}


def _stringify_temporal(df: pl.DataFrame) -> pl.DataFrame:
    """Cast Date/Datetime columns to ISO text so sqlite3 can bind them directly."""
    exprs = []
    for name, dtype in zip(df.columns, df.dtypes):
        if dtype == pl.Date:
            exprs.append(pl.col(name).dt.strftime("%Y-%m-%d").alias(name))
        elif isinstance(dtype, pl.Datetime):
            exprs.append(pl.col(name).dt.strftime("%Y-%m-%d %H:%M:%S").alias(name))
        else:
            exprs.append(pl.col(name))
    return df.select(exprs)


def _column_ddl(df: pl.DataFrame) -> str:
    parts = []
    for name, dtype in zip(df.columns, df.dtypes):
        sql_type = _SQLITE_TYPE.get(dtype, "TEXT")
        parts.append(f'"{name}" {sql_type}')
    return ", ".join(parts)


def get_connection(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(db_path)


def create_minutes_table(conn: sqlite3.Connection, sample: pl.DataFrame) -> None:
    sample = _stringify_temporal(sample)
    cols_ddl = _column_ddl(sample)
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS minutes ({cols_ddl}, PRIMARY KEY (instrument, ts))'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS idx_minutes_instrument_date ON minutes (instrument, date)')


def create_sessions_table(conn: sqlite3.Connection, sample: pl.DataFrame) -> None:
    sample = _stringify_temporal(sample)
    cols_ddl = _column_ddl(sample)
    conn.execute(
        f'CREATE TABLE IF NOT EXISTS sessions ({cols_ddl}, PRIMARY KEY (instrument, date))'
    )
    conn.execute('CREATE INDEX IF NOT EXISTS ix_sessions_seq ON sessions (instrument, seq)')


_GYRATIONS_COLUMNS = [
    "instrument", "scope", "threshold", "mode", "leg_index", "confirmed",
    "start_ts", "end_ts", "start_date", "end_date", "start_price", "end_price",
    "direction", "magnitude_pts", "duration_min", "midprice",
    "deepest_retr_pts", "deepest_retr_pct_final", "deepest_retr_progress",
    "deepest_retr_start_ts", "deepest_retr_end_ts",
]


def create_gyrations_table(conn: sqlite3.Connection) -> None:
    """Fixed schema per Phase 2 spec §3.2 — not registry-driven (unlike sessions),
    so DDL is hand-written rather than derived from a Polars sample. Keyed on
    timestamps, not `date`: a `continuous`-scope leg isn't owned by one session.
    """
    conn.execute('''
        CREATE TABLE IF NOT EXISTS gyrations (
          instrument    TEXT NOT NULL,
          scope         TEXT NOT NULL,
          threshold     REAL NOT NULL,
          mode          TEXT NOT NULL,
          leg_index     INTEGER NOT NULL,
          confirmed     INTEGER NOT NULL,
          start_ts      TEXT NOT NULL,
          end_ts        TEXT NOT NULL,
          start_date    TEXT NOT NULL,
          end_date      TEXT NOT NULL,
          start_price   REAL NOT NULL,
          end_price     REAL NOT NULL,
          direction     TEXT NOT NULL,
          magnitude_pts REAL NOT NULL,
          duration_min  INTEGER NOT NULL,
          midprice      REAL NOT NULL,
          deepest_retr_pts       REAL,
          deepest_retr_pct_final REAL,
          deepest_retr_progress  REAL,
          deepest_retr_start_ts  TEXT,
          deepest_retr_end_ts    TEXT,
          PRIMARY KEY (instrument, scope, threshold, mode, leg_index)
        )
    ''')
    conn.execute(
        'CREATE INDEX IF NOT EXISTS ix_gyr_lookup ON gyrations(instrument, scope, threshold, mode, start_date)'
    )
    conn.execute(
        'CREATE INDEX IF NOT EXISTS ix_gyr_ts ON gyrations(instrument, scope, threshold, mode, start_ts)'
    )


def delete_gyrations_scope(conn: sqlite3.Connection, instrument: str, scope: str, mode: str) -> None:
    conn.execute(
        'DELETE FROM gyrations WHERE instrument = ? AND scope = ? AND mode = ?',
        (instrument, scope, mode),
    )


def write_gyrations_rows(conn: sqlite3.Connection, rows: list[dict]) -> None:
    if not rows:
        return
    col_names = ", ".join(f'"{c}"' for c in _GYRATIONS_COLUMNS)
    placeholders = ", ".join("?" for _ in _GYRATIONS_COLUMNS)

    def _to_param(v):
        if hasattr(v, "isoformat"):
            return v.isoformat(sep=" ") if hasattr(v, "hour") else v.isoformat()
        if isinstance(v, bool):
            return int(v)
        return v

    values = [tuple(_to_param(row[c]) for c in _GYRATIONS_COLUMNS) for row in rows]
    conn.executemany(
        f'INSERT OR REPLACE INTO gyrations ({col_names}) VALUES ({placeholders})', values
    )


def _write(conn: sqlite3.Connection, table: str, df: pl.DataFrame, instruments: list[str]) -> None:
    df = _stringify_temporal(df)
    placeholders = ", ".join("?" for _ in df.columns)
    col_names = ", ".join(f'"{c}"' for c in df.columns)

    qmarks = ", ".join("?" for _ in instruments)
    conn.execute(f'DELETE FROM {table} WHERE instrument IN ({qmarks})', instruments)

    conn.executemany(
        f'INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})',
        df.rows(),
    )


def write_minutes(conn: sqlite3.Connection, df: pl.DataFrame) -> None:
    create_minutes_table(conn, df.head(0))
    instruments = df["instrument"].unique().to_list()
    _write(conn, "minutes", df, instruments)
    conn.commit()


def write_sessions(conn: sqlite3.Connection, df: pl.DataFrame) -> None:
    create_sessions_table(conn, df.head(0))
    instruments = df["instrument"].unique().to_list()
    _write(conn, "sessions", df, instruments)
    conn.commit()
