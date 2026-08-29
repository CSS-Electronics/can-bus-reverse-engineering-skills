"""SQLite storage for driver profiles and session metadata.

Per CLAUDE.md: SQLite holds small relational metadata (drivers, sessions).
Bulk readings live in per-session Parquet files, linked via
sessions.parquet_path — not handled by this module.
"""

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def init_db(path):
    """Open (creating if needed) the SQLite file at `path` and ensure tables exist.

    Returns an open connection with foreign key enforcement turned on.
    """
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.executescript(SCHEMA_PATH.read_text())
    conn.commit()
    return conn
