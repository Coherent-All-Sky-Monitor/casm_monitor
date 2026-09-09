"""SQLite schema for the monitor store.

One file, ``store_root/monitor.sqlite``, in WAL mode so the collector can write
while the web process reads. ``scalars.value`` is deliberately declared without
a type: SQLite's dynamic typing keeps a REAL as a REAL and a TEXT as a TEXT in
the same column, which is what the collectors need (thresholds and UTC_START
strings live side by side).
"""

from __future__ import annotations

SCHEMA_VERSION = 3

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

-- Kafka bandpass row -> correlator input mapping (M1). The formula
-- ``row = 2 * packet_idx`` (isig=packet_idx, pol 0) is the PRIMARY mapping for
-- every wired input; this table holds the daily/obs-restart VALIDATION result
-- against the measured shape correlation. One row per wired input's formula
-- row; ``status`` is 'formula' (not yet validated / vis unavailable),
-- 'formula+verified' (validation's argmax agrees) or 'mismatch' (it does
-- not). The correlation scores are kept for the API even though they no
-- longer gate the mapping.
CREATE TABLE IF NOT EXISTS kafka_row_map (
    row        INTEGER PRIMARY KEY,
    packet_idx INTEGER,           -- NULL when unmapped
    corr       REAL,
    runner_up  REAL,
    status     TEXT NOT NULL,
    ts         REAL NOT NULL,
    source     TEXT               -- json: obs, file index, n inputs, thresholds
);

-- Audit trail of weights uploads performed from the Calibration tab (M3).
-- One row per deploy_upload job that actually ran the deploy tool, written
-- whatever the exit code was: the row is the record that a human clicked
-- Upload, so a failed upload must leave one too
-- (casm-wiki decisions/2026-09-09-monitor-upload-button.md).
CREATE TABLE IF NOT EXISTS uploads (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL NOT NULL,
    build_tag     TEXT NOT NULL,
    job_id        INTEGER,
    note          TEXT,           -- operator's note from the confirm dialog
    md5s          TEXT,           -- json {filename: md5} of the staged dada files
    command       TEXT,           -- json array: the exact argv that was run
    exit_code     INTEGER,
    output_tail   TEXT,
    product_id    TEXT,           -- weights registry product id, when known
    save_defaults INTEGER NOT NULL DEFAULT 0,
    scale         INTEGER,
    ib_scale      INTEGER
);
CREATE INDEX IF NOT EXISTS uploads_tag_ts ON uploads (build_tag, ts);

CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""
