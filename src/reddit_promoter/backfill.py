"""Recover the history of codes handed out by hand, before this tool existed.

Without this the duplicate check is blind to everyone served manually: they
comment again, look brand new, and get a second code. The sent-messages folder
is the record of what actually went out, so read it back rather than trusting
memory.

Nothing is written unless `apply=True`. The default is a preview.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

import prawcore

from .db import log, transaction, utcnow
from . import store

# Play Store promo codes are 23 characters, uppercase letters and digits.
CODE_RE = re.compile(r"\b[A-Z0-9]{23}\b")

# A sent message mentioning this is the lifetime reward, not the weekly code.
LIFETIME_HINTS = ("lifetime", "life time", "life-time")


@dataclass
class Found:
    username: str
    code: str | None
    pool: str
    sent_at: str
    subject: str
    in_pool: bool = False          # the code is one of ours, still unused
    already_used_by: str | None = None
    already_known: str | None = None   # existing state for this user


@dataclass
class BackfillReport:
    scanned: int = 0
    found: list[Found] = field(default_factory=list)
    applied: int = 0
    codes_marked_used: int = 0
    conflicts: list[str] = field(default_factory=list)
    skipped_no_code: int = 0

    @property
    def users(self) -> set[str]:
        return {f.username for f in self.found}


def _classify_pool(subject: str, body: str) -> str:
    text = f"{subject}\n{body}".lower()
    return "lifetime" if any(h in text for h in LIFETIME_HINTS) else "weekly"


def scan_sent(reddit, conn: sqlite3.Connection, app_id: str,
              limit: int = 500) -> BackfillReport:
    """Read the sent folder and work out who was already given a code."""
    report = BackfillReport()

    try:
        sent = list(reddit.inbox.sent(limit=limit))
    except prawcore.exceptions.PrawcoreException as exc:
        raise RuntimeError(f"could not read the sent folder: {exc}") from exc

    # Oldest first, so the earliest code a person was given is the one kept.
    sent.sort(key=lambda m: getattr(m, "created_utc", 0))

    seen_pairs: set[tuple[str, str]] = set()

    for message in sent:
        report.scanned += 1
        recipient = getattr(message, "dest", None)
        if not recipient:
            continue
        recipient = str(recipient)
        if recipient.startswith("#"):        # a subreddit, not a person
            continue

        body = message.body or ""
        subject = message.subject or ""
        codes = CODE_RE.findall(body)
        if not codes:
            report.skipped_no_code += 1
            continue

        pool = _classify_pool(subject, body)
        code = codes[0]
        if (recipient, pool) in seen_pairs:
            continue
        seen_pairs.add((recipient, pool))

        row = conn.execute(
            "SELECT used_by FROM codes WHERE app_id = ? AND code = ?",
            (app_id, code),
        ).fetchone()
        existing_user = store.get_user(conn, app_id, recipient)

        found = Found(
            username=recipient,
            code=code,
            pool=pool,
            sent_at=_iso(getattr(message, "created_utc", None)),
            subject=subject,
            in_pool=row is not None,
            already_used_by=row["used_by"] if row else None,
            already_known=existing_user["state"] if existing_user else None,
        )
        if row is not None and row["used_by"] and row["used_by"] != recipient:
            report.conflicts.append(
                f"{code} is recorded as used by u/{row['used_by']} but was "
                f"sent to u/{recipient}"
            )
        report.found.append(found)

    return report


def _iso(created_utc) -> str:
    from datetime import datetime, timezone
    if not created_utc:
        return utcnow()
    return datetime.fromtimestamp(created_utc, tz=timezone.utc).isoformat(
        timespec="seconds")


def apply_backfill(conn: sqlite3.Connection, app_id: str,
                   report: BackfillReport) -> BackfillReport:
    """Record the recovered history. Safe to re-run: it never downgrades.

    A user already further along (lifetime sent, or blocked) is left alone,
    and a code already attributed to someone else is reported, not stolen.
    """
    for found in report.found:
        user = store.get_user(conn, app_id, found.username)
        if user and user["state"] in (store.BLOCKED, store.LIFETIME_SENT):
            continue
        if found.already_used_by and found.already_used_by != found.username:
            continue

        with transaction(conn):
            store.ensure_user(conn, app_id, found.username)

            if found.pool == "lifetime":
                store.set_user_state(conn, app_id, found.username,
                                     store.LIFETIME_SENT,
                                     lifetime_code=found.code,
                                     lifetime_sent_at=found.sent_at,
                                     notes="backfilled from sent messages")
            else:
                store.set_user_state(conn, app_id, found.username,
                                     store.AWAITING_PROOF,
                                     weekly_code=found.code,
                                     weekly_sent_at=found.sent_at,
                                     notes="backfilled from sent messages")

            # If that code is one of ours and still free, retire it so it can
            # never be handed to a second person.
            if found.in_pool and not found.already_used_by:
                conn.execute(
                    "UPDATE codes SET used_by = ?, used_at = ? "
                    "WHERE app_id = ? AND code = ? AND used_by IS NULL",
                    (found.username, found.sent_at, app_id, found.code),
                )
                report.codes_marked_used += 1

            log(conn, "backfilled", app_id=app_id, username=found.username,
                detail=f"{found.pool}:{found.code} sent {found.sent_at}")
            report.applied += 1

    return report


def normalise_usernames(raw: list[str]) -> list[str]:
    """Accept alice, u/alice or /u/alice and return the bare name, in order."""
    out: list[str] = []
    for item in raw:
        name = item.strip().lstrip("/")
        if name.lower().startswith("u/"):
            name = name[2:]
        name = name.strip().lstrip("/")
        if name and name not in out:
            out.append(name)
    return out


def mark_users_served(conn: sqlite3.Connection, app_id: str,
                      usernames: list[str]) -> int:
    """Record people as already served without knowing which code they got.

    For anyone the sent folder cannot account for - handed out in a comment,
    a chat, or before the messages aged out.
    """
    done = 0
    for username in normalise_usernames(usernames):
        user = store.get_user(conn, app_id, username)
        if user and user["state"] in (store.BLOCKED, store.LIFETIME_SENT):
            continue
        with transaction(conn):
            store.ensure_user(conn, app_id, username)
            store.set_user_state(conn, app_id, username, store.AWAITING_PROOF,
                                 notes="marked as already served by hand")
            log(conn, "marked_served", app_id=app_id, username=username)
            done += 1
    return done


# Marks a code as spent when the recipient is not known.
SPENT_UNKNOWN = "(spent before tracking)"


@dataclass
class RetireReport:
    retired: list[tuple[str, str]] = field(default_factory=list)
    already_retired: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    not_in_pool: list[str] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)


def parse_codes_file(text: str) -> list[tuple[str, str | None]]:
    """Read `CODE` or `CODE username` per line. Comments and blanks ignored."""
    out: list[tuple[str, str | None]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.replace(",", " ").split()
        code = parts[0].strip().upper()
        user = None
        if len(parts) > 1:
            names = normalise_usernames([parts[1]])
            user = names[0] if names else None
        out.append((code, user))
    return out


def retire_codes(conn: sqlite3.Connection, app_id: str,
                 pairs: list[tuple[str, str | None]],
                 apply: bool = False) -> RetireReport:
    """Mark specific codes as already spent, so they are never handed out.

    Deleting a code from the CSV does NOT do this: import is additive, so a
    code already in the database stays there and stays issuable. This is the
    only way to retire one.
    """
    report = RetireReport()

    for code, user in pairs:
        if not CODE_RE.fullmatch(code):
            report.malformed.append(code)
            continue

        row = conn.execute(
            "SELECT used_by FROM codes WHERE app_id = ? AND code = ?",
            (app_id, code),
        ).fetchone()

        if row is None:
            report.not_in_pool.append(code)
            continue
        if row["used_by"]:
            if user and row["used_by"] != user and row["used_by"] != SPENT_UNKNOWN:
                report.conflicts.append(
                    f"{code} is recorded against u/{row['used_by']}, "
                    f"not u/{user}")
            else:
                report.already_retired.append(code)
            continue

        holder = user or SPENT_UNKNOWN
        report.retired.append((code, holder))

        if apply:
            with transaction(conn):
                conn.execute(
                    "UPDATE codes SET used_by = ?, used_at = ? "
                    "WHERE app_id = ? AND code = ? AND used_by IS NULL",
                    (holder, utcnow(), app_id, code),
                )
                if user:
                    store.ensure_user(conn, app_id, user)
                    existing = store.get_user(conn, app_id, user)
                    if existing["state"] in (store.NEW, store.AWAITING_PROOF):
                        store.set_user_state(
                            conn, app_id, user, store.AWAITING_PROOF,
                            weekly_code=code,
                            weekly_sent_at=existing["weekly_sent_at"] or utcnow())
                log(conn, "code_retired", app_id=app_id, username=user,
                    detail=code)

    return report


def codes_missing_from_files(conn: sqlite3.Connection, cfg) -> list[sqlite3.Row]:
    """Codes in the database that are no longer in the app's CSV files.

    Deleting a used code from a CSV is a natural way to track spend by hand,
    but import is additive: the code stays in the database and stays
    issuable. This finds that gap so it can be reported rather than silently
    handing out a dead code.
    """
    on_disk: set[str] = set()
    for code_file in cfg.code_files:
        if code_file.path.exists():
            on_disk.update(store.read_codes_csv(code_file.path, code_file.column))

    rows = conn.execute(
        "SELECT code, pool, source_file, used_by FROM codes WHERE app_id = ? "
        "ORDER BY pool, priority, rowid",
        (cfg.app_id,),
    ).fetchall()
    return [r for r in rows if r["code"] not in on_disk]
