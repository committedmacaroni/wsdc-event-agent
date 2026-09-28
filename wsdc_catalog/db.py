"""SQLite schema and connection helpers."""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 4

SCHEMA = """
CREATE TABLE IF NOT EXISTS event_series (
    id              TEXT PRIMARY KEY,
    canonical_name  TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    source          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_series_norm ON event_series(normalized_name);

CREATE TABLE IF NOT EXISTS events (
    id                  TEXT PRIMARY KEY,
    series_id           TEXT NOT NULL REFERENCES event_series(id),
    name                TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    start_date          TEXT,
    end_date            TEXT,
    year                INTEGER,
    city                TEXT,
    region              TEXT,
    country             TEXT,
    country_raw         TEXT,
    location_raw        TEXT,
    event_status        TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (event_status IN ('registry','trial','unknown')),
    confirmation_status TEXT NOT NULL DEFAULT 'unknown'
                        CHECK (confirmation_status IN ('confirmed','unconfirmed','unknown')),
    hiatus              INTEGER NOT NULL DEFAULT 0,
    active              INTEGER NOT NULL DEFAULT 1,
    lifecycle_status    TEXT NOT NULL DEFAULT 'listed'
                        CHECK (lifecycle_status IN ('listed','past','missing_from_source','historical')),
    source              TEXT NOT NULL,
    website_url         TEXT,
    wsdc_source_url     TEXT,
    raw_payload         TEXT,
    content_hash        TEXT,
    first_seen_at       TEXT,
    last_seen_at        TEXT,
    last_synced_at      TEXT,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_series ON events(series_id);
CREATE INDEX IF NOT EXISTS idx_events_year ON events(year);
CREATE INDEX IF NOT EXISTS idx_events_country ON events(country);
CREATE INDEX IF NOT EXISTS idx_events_start ON events(start_date);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source, active);

CREATE TABLE IF NOT EXISTS event_aliases (
    id               TEXT PRIMARY KEY,
    series_id        TEXT NOT NULL REFERENCES event_series(id),
    alias            TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    UNIQUE (series_id, normalized_alias)
);
CREATE INDEX IF NOT EXISTS idx_aliases_norm ON event_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS event_observations (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT NOT NULL REFERENCES events(id),
    sync_run_id     INTEGER REFERENCES sync_runs(id),
    source          TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    content_hash    TEXT,
    raw_payload     TEXT,
    previous_values TEXT
);
CREATE INDEX IF NOT EXISTS idx_obs_event ON event_observations(event_id);

CREATE TABLE IF NOT EXISTS event_external_refs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL REFERENCES events(id),
    source      TEXT NOT NULL,
    external_id TEXT,
    url         TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (source, external_id)
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    source              TEXT NOT NULL,
    source_url          TEXT,
    started_at          TEXT NOT NULL,
    completed_at        TEXT,
    status              TEXT NOT NULL
                        CHECK (status IN ('running','success','failed','aborted_guard')),
    records_found       INTEGER NOT NULL DEFAULT 0,
    records_added       INTEGER NOT NULL DEFAULT 0,
    records_updated     INTEGER NOT NULL DEFAULT 0,
    records_unchanged   INTEGER NOT NULL DEFAULT 0,
    records_deactivated INTEGER NOT NULL DEFAULT 0,
    records_skipped     INTEGER NOT NULL DEFAULT 0,
    errors              TEXT NOT NULL DEFAULT '[]',
    warnings            TEXT NOT NULL DEFAULT '[]',
    changes             TEXT NOT NULL DEFAULT '[]',
    snapshot_path       TEXT
);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Autocommit connection; callers manage transactions with explicit BEGIN."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    init_schema(conn)
    return conn


MIGRATIONS = {
    2: """
    ALTER TABLE events ADD COLUMN date_precision TEXT NOT NULL DEFAULT 'day';
    CREATE TABLE IF NOT EXISTS series_external_refs (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        series_id   TEXT NOT NULL REFERENCES event_series(id),
        source      TEXT NOT NULL,
        external_id TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (source, external_id)
    );
    CREATE TABLE IF NOT EXISTS registry_cache (
        query      TEXT PRIMARY KEY,
        payload    TEXT NOT NULL,
        fetched_at TEXT NOT NULL
    );
    """,
    3: """
    CREATE TABLE IF NOT EXISTS scoresheet_sheets (
        url                TEXT PRIMARY KEY,
        provider           TEXT NOT NULL,
        provider_event_key TEXT,
        event_id           TEXT REFERENCES events(id),
        title              TEXT,
        status             TEXT NOT NULL,
        entries            INTEGER NOT NULL DEFAULT 0,
        content_hash       TEXT,
        error              TEXT,
        fetched_at         TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS scoresheet_entries (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        sheet_url       TEXT NOT NULL REFERENCES scoresheet_sheets(url),
        event_id        TEXT REFERENCES events(id),
        provider        TEXT NOT NULL,
        section         TEXT,
        division        TEXT,
        round           TEXT,
        role            TEXT,
        competitor_name TEXT NOT NULL,
        normalized_name TEXT NOT NULL,
        partner_name    TEXT,
        bib             TEXT,
        place           INTEGER,
        rank            INTEGER,
        marks           TEXT,
        counts          TEXT,
        score           REAL,
        advanced        INTEGER,
        alternate       TEXT,
        competed        INTEGER
    );
    CREATE INDEX IF NOT EXISTS idx_sse_name ON scoresheet_entries(normalized_name);
    CREATE INDEX IF NOT EXISTS idx_sse_event ON scoresheet_entries(event_id);
    CREATE INDEX IF NOT EXISTS idx_sse_sheet ON scoresheet_entries(sheet_url);
    """,
    4: """
    CREATE TABLE IF NOT EXISTS provider_events (
        provider     TEXT NOT NULL,
        provider_key TEXT NOT NULL,
        event_id     TEXT REFERENCES events(id),
        name         TEXT,
        start_date   TEXT,
        end_date     TEXT,
        index_url    TEXT,
        links        TEXT NOT NULL,
        fetched_at   TEXT NOT NULL,
        PRIMARY KEY (provider, provider_key)
    );
    CREATE INDEX IF NOT EXISTS idx_pe_event ON provider_events(event_id);
    CREATE INDEX IF NOT EXISTS idx_pe_start ON provider_events(start_date);
    """,
}


def init_schema(conn: sqlite3.Connection) -> None:
    """Create the v1 schema if needed, then apply migrations in order."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.executescript(SCHEMA)
        conn.execute("PRAGMA user_version = 1")
        version = 1
    for target in sorted(MIGRATIONS):
        if version < target:
            conn.executescript(MIGRATIONS[target])
            conn.execute(f"PRAGMA user_version = {target}")
            version = target


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
