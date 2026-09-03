"""Case database: SQLite schema and connection helpers.

Everything in this database is *derived*: each row traces back to an evidence
item and a recorded parser run, and the whole database must be reproducible
from the evidence store plus the recorded parser runs (ARCHITECTURE.md §3).
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases (
    id            TEXT PRIMARY KEY,
    case_number   TEXT NOT NULL,
    created_utc   TEXT NOT NULL,
    meta_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evidence_items (
    id                  TEXT PRIMARY KEY,
    sha256              TEXT NOT NULL UNIQUE,
    md5                 TEXT NOT NULL,
    size_bytes          INTEGER NOT NULL,
    original_name       TEXT NOT NULL,
    added_utc           TEXT NOT NULL,
    actor               TEXT NOT NULL,
    description         TEXT,
    acquisition_method  TEXT NOT NULL,
    data_origin         TEXT NOT NULL,
    source_device       TEXT,
    manifest_json       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custody_events (
    id           TEXT PRIMARY KEY,
    evidence_id  TEXT NOT NULL REFERENCES evidence_items(id),
    ts_utc       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    action       TEXT NOT NULL,
    details      TEXT
);

CREATE TABLE IF NOT EXISTS artifacts (
    id                  TEXT PRIMARY KEY,
    evidence_id         TEXT NOT NULL REFERENCES evidence_items(id),
    parent_artifact_id  TEXT REFERENCES artifacts(id),
    path_within         TEXT NOT NULL,
    artifact_type       TEXT NOT NULL,
    sha256              TEXT,
    detected_by         TEXT
);

CREATE TABLE IF NOT EXISTS parser_runs (
    id             TEXT PRIMARY KEY,
    artifact_id    TEXT NOT NULL REFERENCES artifacts(id),
    parser_name    TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    settings_json  TEXT NOT NULL DEFAULT '{}',
    input_sha256   TEXT NOT NULL,
    started_utc    TEXT NOT NULL,
    ended_utc      TEXT,
    status         TEXT NOT NULL,          -- running | completed | failed
    event_count    INTEGER,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id               TEXT PRIMARY KEY,
    artifact_id      TEXT NOT NULL REFERENCES artifacts(id),
    parser_run_id    TEXT NOT NULL REFERENCES parser_runs(id),
    source_offset    TEXT,
    ts_utc           TEXT,
    ts_original      TEXT,
    ts_source        TEXT,
    clock_confidence TEXT,
    event_type       TEXT NOT NULL,
    lat REAL, lon REAL, alt_msl REAL, alt_agl REAL, h_acc REAL, v_acc REAL,
    pitch REAL, roll REAL, yaw REAL, speed REAL, heading REAL,
    confidence       REAL NOT NULL DEFAULT 1.0,
    payload_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_events_ts    ON events(ts_utc);
CREATE INDEX IF NOT EXISTS idx_events_type  ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_run   ON events(parser_run_id);

CREATE TABLE IF NOT EXISTS annotations (
    id           TEXT PRIMARY KEY,
    target_type  TEXT NOT NULL,   -- evidence | artifact | event | case
    target_id    TEXT NOT NULL,
    ts_utc       TEXT NOT NULL,
    actor        TEXT NOT NULL,
    text         TEXT NOT NULL
);
"""


def new_id() -> str:
    return uuid.uuid4().hex


def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
