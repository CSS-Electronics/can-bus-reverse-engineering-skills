-- SQLite schema for driver profiles and session metadata.
-- Bulk readings are NOT stored here — they live in per-session Parquet files,
-- linked via sessions.parquet_path. See CLAUDE.md's Storage / data model section.

CREATE TABLE IF NOT EXISTS drivers (
    driver_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    driver_id     INTEGER NOT NULL REFERENCES drivers(driver_id),
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    source_csv    TEXT,
    parquet_path  TEXT,
    notes         TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_driver_id ON sessions(driver_id);
