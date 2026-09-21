"""SQLite schema, connection handling, and run-start backups.

SQLite is the single source of truth once codes are imported. The original
CSVs are read-only inputs and are never written to.
"""

from __future__ import annotations

import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .config import BACKUP_DIR, BACKUPS_TO_KEEP, DATA_DIR, DB_PATH

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS apps (
    app_id      TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    store       TEXT NOT NULL,
    config_path TEXT NOT NULL,
    added_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS codes (
    app_id      TEXT NOT NULL REFERENCES apps(app_id),
    code        TEXT NOT NULL,
    pool        TEXT NOT NULL CHECK (pool IN ('weekly', 'lifetime')),
    source_file TEXT NOT NULL,
    priority    INTEGER NOT NULL DEFAULT 1,
    used_by     TEXT,
    used_at     TEXT,
    imported_at TEXT NOT NULL,
    PRIMARY KEY (app_id, code)
);

-- Narrows allocation to the unused codes of one pool; the query then orders
-- by priority then rowid (rowid is usable in ORDER BY but not in an index).
CREATE INDEX IF NOT EXISTS idx_codes_alloc
    ON codes (app_id, pool, used_by, priority);

CREATE TABLE IF NOT EXISTS users (
    app_id           TEXT NOT NULL REFERENCES apps(app_id),
    reddit_username  TEXT NOT NULL,
    state            TEXT NOT NULL,
    weekly_code      TEXT,
    weekly_sent_at   TEXT,
    lifetime_code    TEXT,
    lifetime_sent_at TEXT,
    notes            TEXT,
    first_seen_at    TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    PRIMARY KEY (app_id, reddit_username)
);

CREATE TABLE IF NOT EXISTS watched_posts (
    post_id   TEXT PRIMARY KEY,
    app_id    TEXT NOT NULL REFERENCES apps(app_id),
    subreddit TEXT NOT NULL,
    url       TEXT NOT NULL,
    title     TEXT,
    added_at  TEXT NOT NULL,
    active    INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS processed_items (
    item_id      TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    app_id       TEXT,
    username     TEXT,
    seen_at      TEXT NOT NULL,
    action_taken TEXT
);

CREATE TABLE IF NOT EXISTS classifications (
    item_id    TEXT PRIMARY KEY,
    intent     TEXT NOT NULL,
    confidence REAL NOT NULL,
    reason     TEXT,
    model      TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS queue (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id         TEXT,
    username       TEXT NOT NULL,
    action         TEXT NOT NULL,
    pool           TEXT,
    trigger_type   TEXT NOT NULL,
    trigger_id     TEXT NOT NULL,
    trigger_body   TEXT,
    trigger_url    TEXT,
    parent_id      TEXT,
    subject        TEXT,
    draft_body     TEXT NOT NULL,
    ack_body       TEXT,
    preview_code   TEXT,
    allocated_code TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    error          TEXT,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    resolved_at    TEXT
);

CREATE INDEX IF NOT EXISTS idx_queue_status ON queue (status, app_id, id);

CREATE TABLE IF NOT EXISTS audit_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    at        TEXT NOT NULL,
    app_id    TEXT,
    username  TEXT,
    event     TEXT NOT NULL,
    detail    TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at);
"""


def utcnow() -> str:
    """Timestamps are ISO-8601 UTC strings throughout."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection):
    """All-or-nothing unit of work: allocate + mark used + state + audit."""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def log(conn: sqlite3.Connection, event: str, *, app_id=None, username=None, detail=None) -> None:
    """Append to the audit log. Caller controls the surrounding transaction."""
    conn.execute(
        "INSERT INTO audit_log (at, app_id, username, event, detail) VALUES (?, ?, ?, ?, ?)",
        (utcnow(), app_id, username, event, detail),
    )


def backup_db(db_path: Path | None = None, keep: int = BACKUPS_TO_KEEP) -> Path | None:
    """Copy the db aside at the start of a run, pruning to the last `keep`."""
    path = Path(db_path) if db_path else DB_PATH
    if not path.exists():
        return None
    backup_dir = (path.parent / "backups") if db_path else BACKUP_DIR
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    dest = backup_dir / f"{path.stem}-{stamp}.db"

    # Use SQLite's own backup API so a WAL mid-write can't produce a torn copy.
    src = sqlite3.connect(path)
    try:
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

    existing = sorted(backup_dir.glob(f"{path.stem}-*.db"))
    for stale in existing[:-keep] if keep > 0 else []:
        stale.unlink(missing_ok=True)
    return dest


def ensure_data_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
