import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("HOUSEKEEPING_DB") or Path(__file__).resolve().parent.parent / "housekeeping.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Everything you own that needs care: appliances, plants, outdoor features, vehicles, the house itself.
CREATE TABLE IF NOT EXISTS assets (
    id           INTEGER PRIMARY KEY,
    category     TEXT NOT NULL,          -- appliance | outdoor | plant | vehicle | home
    name         TEXT NOT NULL,
    details      TEXT DEFAULT '',        -- brand / model / species / location in yard
    year         INTEGER,                -- model year or install year
    mileage      INTEGER,                -- vehicles only: current odometer
    notes        TEXT DEFAULT '',
    created_at   TEXT DEFAULT (date('now'))
);

-- A recurring maintenance rule. status='suggested' rows await your approval.
CREATE TABLE IF NOT EXISTS tasks (
    id               INTEGER PRIMARY KEY,
    asset_id         INTEGER REFERENCES assets(id) ON DELETE CASCADE,
    title            TEXT NOT NULL,
    description      TEXT DEFAULT '',
    recurrence       TEXT NOT NULL,      -- interval | seasonal | mileage
    interval_days    INTEGER,            -- interval, or time cap for mileage tasks
    months           TEXT,               -- seasonal: comma-separated months, e.g. "10,11"
    mileage_interval INTEGER,            -- mileage
    priority         INTEGER DEFAULT 2,  -- 1 high, 2 normal, 3 low
    est_minutes      INTEGER,
    status           TEXT DEFAULT 'active', -- suggested | active | paused
    source           TEXT DEFAULT 'manual', -- ai | guide | manual
    snoozed_until    TEXT,
    created_at       TEXT DEFAULT (date('now'))
);

CREATE TABLE IF NOT EXISTS completions (
    id       INTEGER PRIMARY KEY,
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    done_on  TEXT NOT NULL,
    mileage  INTEGER,
    notes    TEXT DEFAULT ''
);

-- Manuals, guides and photos. Files live in documents/ next to the database.
CREATE TABLE IF NOT EXISTS documents (
    id                INTEGER PRIMARY KEY,
    asset_id          INTEGER REFERENCES assets(id) ON DELETE CASCADE,  -- NULL = general guide
    title             TEXT NOT NULL,
    kind              TEXT DEFAULT 'manual',  -- manual | maintenance | guide | photo | other
    source            TEXT DEFAULT 'uploaded', -- uploaded | found | link
    source_url        TEXT,
    filename          TEXT NOT NULL,
    mime              TEXT NOT NULL,
    size_bytes        INTEGER,
    pages             INTEGER,
    notes             TEXT,                   -- JSON: what Claude extracted when it read the document
    read_mode         TEXT,                   -- full | text: how it was sent to Claude
    anthropic_file_id TEXT,                   -- Files API id, reused for re-reads
    created_at        TEXT DEFAULT (date('now'))
);

-- The tasks assigned to a given day or week, so the list stays stable once generated.
CREATE TABLE IF NOT EXISTS plan_items (
    period   TEXT NOT NULL,              -- e.g. "D2026-10-13" or "W2026-10-12"
    task_id  INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    PRIMARY KEY (period, task_id)
);
"""

DEFAULT_SETTINGS = {
    "location": "",
    "climate_notes": "",       # e.g. "USDA zone 6b, humid, snowy winters"
    "hemisphere": "north",
    "plan_mode": "weekly",     # daily | weekly
    "plan_count": "5",
}


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))


def get_settings(conn) -> dict:
    return {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM settings")}
