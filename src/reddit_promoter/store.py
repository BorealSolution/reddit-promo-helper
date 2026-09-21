"""Deterministic state operations: apps, code import/allocation, users, queue.

Nothing here calls Reddit or Gemini. Every decision about who gets which code
is made by this module, in SQLite, inside a transaction.
"""

from __future__ import annotations

import csv
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from .config import AppConfig
from .db import log, transaction, utcnow

# User states, per app.
NEW = "new"
WEEKLY_CODE_SENT = "weekly_code_sent"
AWAITING_PROOF = "awaiting_proof"
DUPLICATE_QUERY_SENT = "duplicate_query_sent"
LIFETIME_SENT = "lifetime_sent"
BLOCKED = "blocked"

# Queue statuses.
PENDING = "pending"
SENT = "sent"
SKIPPED = "skipped"
DROPPED = "dropped"
NEEDS_RETRY = "needs_retry"


class OutOfCodes(Exception):
    """A pool has no unused codes left; sending must halt for that app."""


# ---------------------------------------------------------------------------
# apps
# ---------------------------------------------------------------------------

def register_app(conn: sqlite3.Connection, cfg: AppConfig) -> bool:
    """Insert or refresh the apps row. Returns True if newly added."""
    existing = conn.execute("SELECT 1 FROM apps WHERE app_id = ?", (cfg.app_id,)).fetchone()
    with transaction(conn):
        conn.execute(
            "INSERT INTO apps (app_id, name, store, config_path, added_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(app_id) DO UPDATE SET "
            "  name = excluded.name, store = excluded.store, "
            "  config_path = excluded.config_path",
            (cfg.app_id, cfg.name, cfg.store, str(cfg.config_path), utcnow()),
        )
        log(conn, "app_registered" if not existing else "app_updated",
            app_id=cfg.app_id, detail=cfg.name)
    return existing is None


def list_apps(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM apps ORDER BY app_id").fetchall()


# ---------------------------------------------------------------------------
# codes
# ---------------------------------------------------------------------------

@dataclass
class ImportReport:
    source_file: str
    pool: str
    rows_read: int = 0
    imported: int = 0
    duplicates_in_file: int = 0
    already_present: int = 0
    blank_skipped: int = 0
    duplicate_codes: list[str] = field(default_factory=list)


def read_codes_csv(path: Path, column: str) -> list[str]:
    """Read one code column out of a CSV, preserving file order."""
    with path.open(newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return []
        # Tolerate whitespace and case differences in the header.
        match = next(
            (f for f in reader.fieldnames
             if f and f.strip().lower() == column.strip().lower()),
            None,
        )
        if match is None:
            raise ValueError(
                f"{path.name}: no column named '{column}' (found: {reader.fieldnames})"
            )
        return [(row.get(match) or "").strip() for row in reader]


def import_codes(conn: sqlite3.Connection, cfg: AppConfig,
                 dry_run: bool = False) -> list[ImportReport]:
    """Idempotent import of every code file for an app.

    Re-running never duplicates a code and never resets a used one: the insert
    conflicts on (app_id, code) and is ignored.
    """
    reports: list[ImportReport] = []
    for code_file in cfg.code_files:
        report = ImportReport(source_file=code_file.path.name, pool=code_file.pool)
        if not code_file.path.exists():
            raise FileNotFoundError(f"code file missing: {code_file.path}")

        raw = read_codes_csv(code_file.path, code_file.column)
        report.rows_read = len(raw)

        seen: set[str] = set()
        to_insert: list[str] = []
        for code in raw:
            if not code:
                report.blank_skipped += 1
                continue
            if code in seen:
                report.duplicates_in_file += 1
                report.duplicate_codes.append(code)
                continue
            seen.add(code)
            to_insert.append(code)

        present = {
            r[0] for r in conn.execute(
                "SELECT code FROM codes WHERE app_id = ?", (cfg.app_id,)
            )
        }
        fresh = [c for c in to_insert if c not in present]
        report.already_present = len(to_insert) - len(fresh)
        report.imported = len(fresh)

        if not dry_run and fresh:
            now = utcnow()
            with transaction(conn):
                conn.executemany(
                    "INSERT OR IGNORE INTO codes "
                    "(app_id, code, pool, source_file, priority, imported_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(cfg.app_id, c, code_file.pool, code_file.path.name,
                      code_file.priority, now) for c in fresh],
                )
                log(conn, "codes_imported", app_id=cfg.app_id,
                    detail=f"{report.imported} from {code_file.path.name} "
                           f"({code_file.pool})")
        reports.append(report)
    return reports


def peek_next_code(conn: sqlite3.Connection, app_id: str, pool: str,
                   exclude: set[str] | None = None) -> str | None:
    """The code that *would* be allocated next. Reserves nothing.

    Drafts show this so the reviewer sees a real code, but a skipped or
    cancelled draft burns nothing: allocation happens only at send time.
    `exclude` lets a dry run walk down the pool without writing to it.
    """
    rows = conn.execute(
        "SELECT code FROM codes WHERE app_id = ? AND pool = ? AND used_by IS NULL "
        "ORDER BY priority, rowid LIMIT ?",
        (app_id, pool, 1 + len(exclude or ())),
    ).fetchall()
    for row in rows:
        if not exclude or row[0] not in exclude:
            return row[0]
    return None


def allocate_code(conn: sqlite3.Connection, app_id: str, pool: str,
                  username: str) -> str:
    """Claim the next code for a user. Must run inside an open transaction.

    Idempotent per (app, pool, user): if this user already holds a code from
    this pool, that same code is returned rather than a second one issued.
    That is what makes a needs_retry item safe to retry.
    """
    held = conn.execute(
        "SELECT code FROM codes WHERE app_id = ? AND pool = ? AND used_by = ? "
        "ORDER BY priority, rowid LIMIT 1",
        (app_id, pool, username),
    ).fetchone()
    if held:
        return held[0]

    row = conn.execute(
        "SELECT code FROM codes WHERE app_id = ? AND pool = ? AND used_by IS NULL "
        "ORDER BY priority, rowid LIMIT 1",
        (app_id, pool),
    ).fetchone()
    if row is None:
        raise OutOfCodes(f"no unused '{pool}' codes left for app '{app_id}'")

    code = row[0]
    updated = conn.execute(
        "UPDATE codes SET used_by = ?, used_at = ? "
        "WHERE app_id = ? AND code = ? AND used_by IS NULL",
        (username, utcnow(), app_id, code),
    ).rowcount
    if updated != 1:
        raise OutOfCodes(f"code {code} was claimed concurrently; retry")
    log(conn, "code_allocated", app_id=app_id, username=username,
        detail=f"{pool}:{code}")
    return code


def release_code(conn: sqlite3.Connection, app_id: str, code: str,
                 username: str) -> None:
    """Un-claim a reserved code. Only for codes that were never actually sent."""
    conn.execute(
        "UPDATE codes SET used_by = NULL, used_at = NULL "
        "WHERE app_id = ? AND code = ? AND used_by = ?",
        (app_id, code, username),
    )
    log(conn, "code_released", app_id=app_id, username=username, detail=code)


def pool_counts(conn: sqlite3.Connection, app_id: str) -> dict[str, dict[str, int]]:
    """Per pool: total, used, remaining."""
    rows = conn.execute(
        "SELECT pool, COUNT(*) AS total, "
        "       SUM(CASE WHEN used_by IS NOT NULL THEN 1 ELSE 0 END) AS used "
        "FROM codes WHERE app_id = ? GROUP BY pool",
        (app_id,),
    ).fetchall()
    out: dict[str, dict[str, int]] = {}
    for r in rows:
        used = r["used"] or 0
        out[r["pool"]] = {
            "total": r["total"],
            "used": used,
            "remaining": r["total"] - used,
        }
    return out


def codes_by_source(conn: sqlite3.Connection, app_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT source_file, pool, priority, COUNT(*) AS total, "
        "       SUM(CASE WHEN used_by IS NOT NULL THEN 1 ELSE 0 END) AS used "
        "FROM codes WHERE app_id = ? "
        "GROUP BY source_file, pool, priority ORDER BY pool, priority",
        (app_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------

def get_user(conn: sqlite3.Connection, app_id: str, username: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM users WHERE app_id = ? AND reddit_username = ?",
        (app_id, username),
    ).fetchone()


def ensure_user(conn: sqlite3.Connection, app_id: str, username: str) -> sqlite3.Row:
    """Create the user in state 'new' if this app has never seen them."""
    row = get_user(conn, app_id, username)
    if row:
        return row
    now = utcnow()
    conn.execute(
        "INSERT INTO users (app_id, reddit_username, state, first_seen_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (app_id, username, NEW, now, now),
    )
    log(conn, "user_created", app_id=app_id, username=username)
    return get_user(conn, app_id, username)


def set_user_state(conn: sqlite3.Connection, app_id: str, username: str,
                   state: str, **fields) -> None:
    """Move a user to a new state, optionally stamping code/sent_at fields."""
    allowed = {"weekly_code", "weekly_sent_at", "lifetime_code",
               "lifetime_sent_at", "notes"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"unknown user fields: {sorted(bad)}")

    assignments = ["state = ?", "updated_at = ?"]
    values: list = [state, utcnow()]
    for key, value in fields.items():
        assignments.append(f"{key} = ?")
        values.append(value)
    values += [app_id, username]

    conn.execute(
        f"UPDATE users SET {', '.join(assignments)} "
        "WHERE app_id = ? AND reddit_username = ?",
        values,
    )
    log(conn, "user_state", app_id=app_id, username=username, detail=state)


def user_counts_by_state(conn: sqlite3.Connection, app_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM users WHERE app_id = ? GROUP BY state",
        (app_id,),
    ).fetchall()
    return {r["state"]: r["n"] for r in rows}


def apps_known_for_user(conn: sqlite3.Connection, username: str) -> list[str]:
    """Which apps have a record for this user - used to attribute stray PMs."""
    return [
        r[0] for r in conn.execute(
            "SELECT app_id FROM users WHERE reddit_username = ? ORDER BY app_id",
            (username,),
        )
    ]


# ---------------------------------------------------------------------------
# processed items
# ---------------------------------------------------------------------------

def is_processed(conn: sqlite3.Connection, item_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM processed_items WHERE item_id = ?", (item_id,)
    ).fetchone() is not None


def mark_processed(conn: sqlite3.Connection, item_id: str, item_type: str, *,
                   app_id=None, username=None, action=None) -> None:
    conn.execute(
        "INSERT INTO processed_items "
        "(item_id, type, app_id, username, seen_at, action_taken) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET action_taken = excluded.action_taken",
        (item_id, item_type, app_id, username, utcnow(), action),
    )


# ---------------------------------------------------------------------------
# classifications (Gemini result cache)
# ---------------------------------------------------------------------------

def get_classification(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM classifications WHERE item_id = ?", (item_id,)
    ).fetchone()


def save_classification(conn: sqlite3.Connection, item_id: str, intent: str,
                        confidence: float, reason: str, model: str) -> None:
    conn.execute(
        "INSERT INTO classifications "
        "(item_id, intent, confidence, reason, model, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(item_id) DO NOTHING",
        (item_id, intent, confidence, reason, model, utcnow()),
    )


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------

def enqueue(conn: sqlite3.Connection, *, app_id, username, action, draft_body,
            trigger_type, trigger_id, pool=None, trigger_body=None,
            trigger_url=None, parent_id=None, subject=None,
            preview_code=None, ack_body=None) -> int:
    """Add a pending action for review. No code is allocated here."""
    existing = conn.execute(
        "SELECT id FROM queue WHERE trigger_id = ? AND action = ? AND status = ?",
        (trigger_id, action, PENDING),
    ).fetchone()
    if existing:
        return existing[0]

    now = utcnow()
    cur = conn.execute(
        "INSERT INTO queue (app_id, username, action, pool, trigger_type, "
        "  trigger_id, trigger_body, trigger_url, parent_id, subject, "
        "  draft_body, ack_body, preview_code, status, created_at, "
        "  updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (app_id, username, action, pool, trigger_type, trigger_id, trigger_body,
         trigger_url, parent_id, subject, draft_body, ack_body, preview_code,
         PENDING, now, now),
    )
    return cur.lastrowid


def has_code_item(conn: sqlite3.Connection, app_id: str, username: str,
                  action: str) -> bool:
    """Is there already a queued or sent code action for this user?

    User state alone is not enough to spot a repeat asker: within a single
    poll nothing has been approved yet, so two comments from the same person
    would both look like first-time requests.
    """
    return conn.execute(
        "SELECT 1 FROM queue WHERE app_id = ? AND username = ? AND action = ? "
        "  AND status IN (?, ?, ?) LIMIT 1",
        (app_id, username, action, PENDING, NEEDS_RETRY, SENT),
    ).fetchone() is not None


def pending_items(conn: sqlite3.Connection, app_id: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM queue WHERE status IN (?, ?)"
    params: list = [PENDING, NEEDS_RETRY]
    if app_id:
        sql += " AND app_id = ?"
        params.append(app_id)
    return conn.execute(sql + " ORDER BY id", params).fetchall()


def get_queue_item(conn: sqlite3.Connection, queue_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM queue WHERE id = ?", (queue_id,)).fetchone()


def set_queue_status(conn: sqlite3.Connection, queue_id: int, status: str, *,
                     error=None, allocated_code=None) -> None:
    resolved = utcnow() if status in (SENT, SKIPPED, DROPPED) else None
    conn.execute(
        "UPDATE queue SET status = ?, error = ?, updated_at = ?, resolved_at = ?, "
        "  allocated_code = COALESCE(?, allocated_code) "
        "WHERE id = ?",
        (status, error, utcnow(), resolved, allocated_code, queue_id),
    )


def update_draft(conn: sqlite3.Connection, queue_id: int, body: str) -> None:
    conn.execute(
        "UPDATE queue SET draft_body = ?, updated_at = ? WHERE id = ?",
        (body, utcnow(), queue_id),
    )


def update_ack(conn: sqlite3.Connection, queue_id: int, body: str) -> None:
    """Swap in a different wording for the public acknowledgement."""
    conn.execute(
        "UPDATE queue SET ack_body = ?, updated_at = ? WHERE id = ?",
        (body, utcnow(), queue_id),
    )


def reassign_app(conn: sqlite3.Connection, queue_id: int, app_id: str) -> None:
    conn.execute(
        "UPDATE queue SET app_id = ?, updated_at = ? WHERE id = ?",
        (app_id, utcnow(), queue_id),
    )
    log(conn, "queue_app_assigned", app_id=app_id, detail=f"queue:{queue_id}")


def assistance_count(conn: sqlite3.Connection, app_id: str | None = None) -> int:
    """Pending items whose message reads as a question or could not be read.

    These are the ones where somebody is probably stuck and waiting on a
    human, so the home screen calls them out separately from the routine
    code requests.
    """
    sql = ("SELECT COUNT(*) FROM queue q "
           "JOIN classifications c ON c.item_id = q.trigger_id "
           "WHERE q.status IN (?, ?) AND c.intent IN ('question', 'unclear')")
    params: list = [PENDING, NEEDS_RETRY]
    if app_id:
        sql += " AND q.app_id = ?"
        params.append(app_id)
    return conn.execute(sql, params).fetchone()[0]


def pending_count(conn: sqlite3.Connection, app_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM queue WHERE status IN (?, ?)"
    params: list = [PENDING, NEEDS_RETRY]
    if app_id:
        sql += " AND app_id = ?"
        params.append(app_id)
    return conn.execute(sql, params).fetchone()[0]


# ---------------------------------------------------------------------------
# watched posts
# ---------------------------------------------------------------------------

def add_watch(conn: sqlite3.Connection, post_id: str, app_id: str, subreddit: str,
              url: str, title: str | None = None) -> None:
    with transaction(conn):
        conn.execute(
            "INSERT INTO watched_posts "
            "(post_id, app_id, subreddit, url, title, added_at, active) "
            "VALUES (?, ?, ?, ?, ?, ?, 1) "
            "ON CONFLICT(post_id) DO UPDATE SET active = 1, app_id = excluded.app_id",
            (post_id, app_id, subreddit, url, title, utcnow()),
        )
        log(conn, "watch_added", app_id=app_id, detail=url)


def deactivate_watch(conn: sqlite3.Connection, post_id: str) -> bool:
    with transaction(conn):
        n = conn.execute(
            "UPDATE watched_posts SET active = 0 WHERE post_id = ? AND active = 1",
            (post_id,),
        ).rowcount
        if n:
            log(conn, "watch_removed", detail=post_id)
    return bool(n)


def active_watches(conn: sqlite3.Connection, app_id: str | None = None) -> list[sqlite3.Row]:
    sql = "SELECT * FROM watched_posts WHERE active = 1"
    params: list = []
    if app_id:
        sql += " AND app_id = ?"
        params.append(app_id)
    return conn.execute(sql + " ORDER BY added_at", params).fetchall()
