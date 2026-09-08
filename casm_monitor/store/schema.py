"""SQLite schema for the monitor store.

One file, ``store_root/monitor.sqlite``, in WAL mode so the collector can write
while the web process reads. ``scalars.value`` is deliberately declared without
a type: SQLite's dynamic typing keeps a REAL as a REAL and a TEXT as a TEXT in
the same column, which is what the collectors need (thresholds and UTC_START
strings live side by side).
"""

from __future__ import annotations

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS scalars (
    ts    REAL NOT NULL,
    name  TEXT NOT NULL,
    tags  TEXT,           -- json object or NULL
    value                 -- REAL or TEXT (no declared affinity, on purpose)
);
CREATE INDEX IF NOT EXISTS scalars_name_ts ON scalars (name, ts);

CREATE TABLE IF NOT EXISTS events (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL NOT NULL,
    kind     TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('info', 'warn', 'error')),
    subject  TEXT,
    detail   TEXT          -- json object or NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_kind_ts ON events (kind, ts);

-- Manifest of immutable array shards. A row exists only once the shard
-- directory is complete, fsynced and renamed into place, so readers that go
-- through this table never see a partial shard.
CREATE TABLE IF NOT EXISTS shards (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    stream       TEXT NOT NULL,
    t0           REAL NOT NULL,
    t1           REAL NOT NULL,
    path         TEXT NOT NULL UNIQUE,
    dtype        TEXT NOT NULL,
    shape        TEXT NOT NULL,   -- json array
    meta         TEXT,            -- json object or NULL
    committed_ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS shards_stream_t0 ON shards (stream, t0);

CREATE TABLE IF NOT EXISTS watermarks (
    stream TEXT NOT NULL,
    key    TEXT NOT NULL,
    value  TEXT,                  -- json
    PRIMARY KEY (stream, key)
);

CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL,
    params      TEXT,             -- json object
    state       TEXT NOT NULL CHECK (state IN
                    ('queued', 'running', 'done', 'failed', 'cancelled')),
    created     REAL NOT NULL,
    started     REAL,
    finished    REAL,
    lease_until REAL,
    worker      TEXT,
    log_path    TEXT,
    result      TEXT              -- json object
);
CREATE INDEX IF NOT EXISTS jobs_state_id ON jobs (state, id);

CREATE TABLE IF NOT EXISTS collector_heartbeat (
    name        TEXT PRIMARY KEY,
    last_ok     REAL,
    last_err    TEXT,
    last_err_ts REAL
);

CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""
