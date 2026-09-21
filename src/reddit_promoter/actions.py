"""Approving a queue item and carrying it out.

The safety rule the whole tool hangs on: allocate the code, mark it used,
move the user's state and write the audit entry in ONE transaction, commit,
and only then call Reddit. If Reddit fails, the code stays reserved for that
user and the item becomes needs_retry - so retrying re-uses the same code
rather than burning a second one, and no code is ever issued twice.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

from .config import AppConfig
from .db import log, transaction, utcnow
from .engine import (DUPLICATE_QUERY, LIFETIME_CODE, NO_CODE_PLACEHOLDER,
                     NO_DRAFT_PREFIX, REVIEW_ONLY, WEEKLY_CODE)
from . import store
from .senders import SendError, Sender


class ActionError(Exception):
    pass


class DryRunLedger:
    """Tracks the codes a dry run *would* have consumed, without writing any.

    Without this every dry-run item would preview the same code, which hides
    exactly the pool-exhaustion behaviour a rehearsal is meant to reveal.
    """

    def __init__(self):
        self.consumed: dict[tuple[str, str], set[str]] = {}

    def take(self, conn, app_id: str, pool: str, username: str) -> str:
        key = (app_id, pool)
        used = self.consumed.setdefault(key, set())
        code = store.peek_next_code(conn, app_id, pool, exclude=used)
        if code is None:
            raise store.OutOfCodes(
                f"no unused '{pool}' codes left for app '{app_id}'"
            )
        used.add(code)
        return code


def send_item(conn: sqlite3.Connection, cfg: AppConfig, item: sqlite3.Row,
              sender: Sender, *, body_override: str | None = None,
              my_username: str = "", dry_run: bool = False,
              ledger: "DryRunLedger | None" = None) -> dict:
    """Execute one approved queue item. Returns a summary for display."""
    action = item["action"]
    username = item["username"]
    queue_id = item["id"]
    body = body_override if body_override is not None else item["draft_body"]

    if action == REVIEW_ONLY and body.strip().startswith(NO_DRAFT_PREFIX):
        raise ActionError(
            "nothing is written for this one yet - press [e] to write a "
            "reply, or [d] to drop it"
        )

    if dry_run:
        return _rehearse(conn, cfg, item, sender, body, username,
                         ledger or DryRunLedger())

    if action == WEEKLY_CODE:
        return _send_weekly(conn, cfg, item, sender, body, queue_id, username)
    if action == LIFETIME_CODE:
        return _send_lifetime(conn, cfg, item, sender, body, queue_id, username)
    if action == DUPLICATE_QUERY:
        return _send_duplicate_query(conn, cfg, item, sender, body, queue_id, username)
    if action == REVIEW_ONLY:
        return _send_freeform(conn, cfg, item, sender, body, queue_id, username)

    raise ActionError(f"unknown action '{action}'")


def _send_freeform(conn, cfg, item, sender, body, queue_id, username):
    """A reply I wrote myself, usually answering someone who needed help.

    Carries no code and moves nobody's state: answering a question should
    not make someone look served, or served twice.
    """
    try:
        if item["trigger_type"] == "message" and item["parent_id"]:
            sender.reply_to_message(item["parent_id"], body)
        elif item["trigger_type"] == "comment" and item["parent_id"]:
            sender.reply_to_comment(item["parent_id"], body)
        else:
            subject = item["subject"] or f"About {cfg.name}"
            sender.send_pm(username, subject, body)
    except BaseException as exc:
        with transaction(conn):
            store.set_queue_status(conn, queue_id, store.NEEDS_RETRY,
                                   error=str(exc) or type(exc).__name__)
        raise

    with transaction(conn):
        store.set_queue_status(conn, queue_id, store.SENT)
        log(conn, "freeform_reply_sent", app_id=cfg.app_id, username=username,
            detail=f"queue:{queue_id}")

    return {"code": None, "channel": "private message", "note": None}


def _rehearse(conn, cfg, item, sender, body, username, ledger) -> dict:
    """The --dry-run path: hand the message to the recording sender and touch
    nothing. No allocation, no state change, no audit entry, no queue update.
    """
    action = item["action"]
    code = None

    if action in (WEEKLY_CODE, LIFETIME_CODE) and item["pool"]:
        code = item["allocated_code"] or ledger.take(
            conn, cfg.app_id, item["pool"], username
        )
        preview = item["preview_code"]
        if preview and preview != code:
            body = body.replace(preview, code)
        body = body.replace(NO_CODE_PLACEHOLDER, code)

    if action == WEEKLY_CODE:
        sender.send_pm(username, item["subject"] or f"Your {cfg.name} promo code", body)
        if item["ack_body"] and item["trigger_type"] == "comment" and item["parent_id"]:
            sender.reply_to_comment(item["parent_id"], item["ack_body"])
    elif item["parent_id"] and item["trigger_type"] == "message":
        sender.reply_to_message(item["parent_id"], body)
    else:
        sender.send_pm(username, item["subject"] or f"About your {cfg.name} code", body)

    return {"code": code, "channel": "private message", "note": None}


def _reserve(conn, cfg, pool, username, queue_id, body, preview_code):
    """Allocate the real code and put it in the body. Committed before any
    network call, so a crash mid-send leaves the code reserved, not lost.

    The draft was rendered against `preview_code`, which can go stale if the
    pool moved between drafting and approval. Substitution is exact, and the
    result is verified: rather than send a message with a wrong code or a
    leftover placeholder, we refuse and hand the item back.
    """
    with transaction(conn):
        code = store.allocate_code(conn, cfg.app_id, pool, username)
        store.set_queue_status(conn, queue_id, store.PENDING, allocated_code=code)

    if preview_code and preview_code != code:
        body = body.replace(preview_code, code)
    body = body.replace(NO_CODE_PLACEHOLDER, code)

    if code not in body:
        raise ActionError(
            f"the draft does not contain the allocated code {code} - it was "
            f"probably edited. Fix the draft with [e] (code {code} is reserved "
            f"for u/{username} and will be reused)."
        )
    return code, body


def _send_weekly(conn, cfg, item, sender, body, queue_id, username):
    code, body = _reserve(conn, cfg, "weekly", username, queue_id, body,
                          item["preview_code"])

    subject = item["subject"] or f"Your {cfg.name} promo code"
    try:
        sender.send_pm(username, subject, body)
    except BaseException as exc:
        # Deliberately broad. A code is already reserved at this point, so
        # ANY escape - a refusal, a crash, Ctrl-C, stdin closing mid-prompt -
        # must leave the item clearly marked for retry rather than pending
        # with a quietly reserved code.
        with transaction(conn):
            store.set_queue_status(conn, queue_id, store.NEEDS_RETRY,
                                   error=str(exc) or type(exc).__name__)
            log(conn, "send_failed", app_id=cfg.app_id, username=username,
                detail=f"weekly_code queue:{queue_id} code:{code} reserved; {exc!r}")
        raise

    with transaction(conn):
        store.set_user_state(conn, cfg.app_id, username, store.AWAITING_PROOF,
                             weekly_code=code, weekly_sent_at=utcnow())
        store.set_queue_status(conn, queue_id, store.SENT, allocated_code=code)
        log(conn, "weekly_code_sent", app_id=cfg.app_id, username=username,
            detail=code)

    # The public acknowledgement is best-effort: the code is already delivered,
    # so a failure here must not mark the item for retry (that would re-PM).
    ack_note = None
    ack_body = item["ack_body"]
    if ack_body and item["trigger_type"] == "comment" and item["parent_id"]:
        try:
            sender.reply_to_comment(item["parent_id"], ack_body)
            with transaction(conn):
                log(conn, "public_ack_posted", app_id=cfg.app_id, username=username,
                    detail=item["parent_id"])
        except SendError as exc:
            ack_note = f"PM sent, but the public reply failed: {exc}"
            with transaction(conn):
                log(conn, "public_ack_failed", app_id=cfg.app_id, username=username,
                    detail=str(exc))

    return {"code": code, "channel": "private message", "note": ack_note}


def _send_lifetime(conn, cfg, item, sender, body, queue_id, username):
    code, body = _reserve(conn, cfg, "lifetime", username, queue_id, body,
                          item["preview_code"])

    try:
        if item["parent_id"]:
            sender.reply_to_message(item["parent_id"], body)
        else:
            subject = item["subject"] or f"Your lifetime {cfg.name} code"
            sender.send_pm(username, subject, body)
    except BaseException as exc:
        with transaction(conn):
            store.set_queue_status(conn, queue_id, store.NEEDS_RETRY,
                                   error=str(exc) or type(exc).__name__)
            log(conn, "send_failed", app_id=cfg.app_id, username=username,
                detail=f"lifetime_code queue:{queue_id} code:{code} reserved; {exc!r}")
        raise

    with transaction(conn):
        store.set_user_state(conn, cfg.app_id, username, store.LIFETIME_SENT,
                             lifetime_code=code, lifetime_sent_at=utcnow())
        store.set_queue_status(conn, queue_id, store.SENT, allocated_code=code)
        log(conn, "lifetime_code_sent", app_id=cfg.app_id, username=username,
            detail=code)

    return {"code": code, "channel": "private message", "note": None}


def _send_duplicate_query(conn, cfg, item, sender, body, queue_id, username):
    """A question, never a code. State moves so a second ask can't slip through."""
    try:
        if item["trigger_type"] == "message" and item["parent_id"]:
            sender.reply_to_message(item["parent_id"], body)
        else:
            subject = item["subject"] or f"About your {cfg.name} code"
            sender.send_pm(username, subject, body)
    except BaseException as exc:
        with transaction(conn):
            store.set_queue_status(conn, queue_id, store.NEEDS_RETRY,
                                   error=str(exc) or type(exc).__name__)
        raise

    with transaction(conn):
        store.set_user_state(conn, cfg.app_id, username, store.DUPLICATE_QUERY_SENT)
        store.set_queue_status(conn, queue_id, store.SENT)
        log(conn, "duplicate_query_sent", app_id=cfg.app_id, username=username)

    return {"code": None, "channel": "private message", "note": None}


def skip_item(conn: sqlite3.Connection, queue_id: int) -> None:
    """Set it aside for now: the item stays pending and comes back next time.

    Deliberately does not move the queue status - "skip" means "not now",
    whereas "drop" means "handled, never show me this again". Burns nothing
    either way.
    """
    with transaction(conn):
        log(conn, "queue_skipped", detail=str(queue_id))


def drop_item(conn: sqlite3.Connection, queue_id: int, item: sqlite3.Row) -> None:
    """Mark handled with no reply, releasing any code that was reserved."""
    with transaction(conn):
        if item["allocated_code"]:
            store.release_code(conn, item["app_id"], item["allocated_code"],
                               item["username"])
        store.set_queue_status(conn, queue_id, store.DROPPED)
        log(conn, "queue_dropped", app_id=item["app_id"], username=item["username"],
            detail=str(queue_id))


def block_user(conn: sqlite3.Connection, app_id: str, username: str) -> None:
    """Block per app. Future items from them are ignored at poll time."""
    with transaction(conn):
        store.ensure_user(conn, app_id, username)
        store.set_user_state(conn, app_id, username, store.BLOCKED)
        log(conn, "user_blocked", app_id=app_id, username=username)


# ---------------------------------------------------------------------------
# Two-phase send, for a UI where the operator sends the message themselves.
#
# The terminal dashboard can block on a prompt; a web page cannot. So the work
# splits: prepare_send allocates the code and renders every message, then
# record_sent or record_not_sent closes it out once the operator says what
# actually happened. The safety rule is unchanged - the code is reserved
# before the operator sees it, and nothing counts as sent until they say so.
# ---------------------------------------------------------------------------

@dataclass
class OutgoingMessage:
    kind: str                 # pm | pm_reply | comment_reply
    label: str                # what to call it in the UI
    to: str                   # username, or the thing being replied to
    body: str
    subject: str | None = None
    url: str | None = None    # where the operator should go to send it
    carries_code: bool = False


@dataclass
class PreparedSend:
    code: str | None
    messages: list[OutgoingMessage]


def compose_url(username: str, subject: str, body: str) -> str:
    """A Reddit compose link with the message already filled in."""
    from urllib.parse import quote
    return ("https://www.reddit.com/message/compose/"
            f"?to={quote(username)}&subject={quote(subject or '')}"
            f"&message={quote(body)}")


def prepare_send(conn: sqlite3.Connection, cfg: AppConfig, item: sqlite3.Row,
                 body_override: str | None = None) -> PreparedSend:
    """Allocate the code and render what must go out. Sends nothing.

    Marks the item awaiting_confirm so that if the operator wanders off, it
    reappears in the queue with its code still reserved rather than being
    lost or silently counted as sent.
    """
    action = item["action"]
    username = item["username"]
    queue_id = item["id"]
    body = body_override if body_override is not None else item["draft_body"]

    if action == REVIEW_ONLY and body.strip().startswith(NO_DRAFT_PREFIX):
        raise ActionError("nothing is written for this one yet - write a "
                          "reply first, or drop it")

    code = None
    if action in (WEEKLY_CODE, LIFETIME_CODE):
        code, body = _reserve(conn, cfg, item["pool"], username, queue_id,
                              body, item["preview_code"])

    messages = render_messages(cfg, item, body)

    with transaction(conn):
        store.set_queue_status(conn, queue_id, store.AWAITING_CONFIRM,
                               allocated_code=code)
    return PreparedSend(code=code, messages=messages)


def rendered_messages(conn: sqlite3.Connection, cfg: AppConfig,
                      item: sqlite3.Row) -> list["OutgoingMessage"]:
    """What an already-prepared item's messages look like. Writes nothing.

    Used to redraw a page without re-running the allocation.
    """
    body = item["draft_body"]
    code = item["allocated_code"]
    if code:
        preview = item["preview_code"]
        if preview and preview != code:
            body = body.replace(preview, code)
        body = body.replace(NO_CODE_PLACEHOLDER, code)
    return render_messages(cfg, item, body)


def render_messages(cfg: AppConfig, item: sqlite3.Row,
                    body: str) -> list["OutgoingMessage"]:
    """Which messages this item turns into, and where each one goes.

    One definition, shared by the terminal and the web UI, so the two cannot
    drift apart about what gets sent.
    """
    action = item["action"]
    username = item["username"]
    messages: list[OutgoingMessage] = []

    if action == WEEKLY_CODE:
        subject = item["subject"] or f"Your {cfg.name} promo code"
        messages.append(OutgoingMessage(
            kind="pm", label="Private message with the code", to=username,
            subject=subject, body=body,
            url=compose_url(username, subject, body), carries_code=True))
        if item["ack_body"] and item["trigger_type"] == "comment":
            messages.append(OutgoingMessage(
                kind="comment_reply",
                label="Public reply on their comment (no code)",
                to=item["parent_id"] or "", body=item["ack_body"],
                url=item["trigger_url"] or None))

    elif action == LIFETIME_CODE:
        subject = item["subject"] or f"Your lifetime {cfg.name} code"
        if item["trigger_type"] == "message":
            messages.append(OutgoingMessage(
                kind="pm_reply", label="Reply in your message thread",
                to=username, subject=subject, body=body,
                url=compose_url(username, subject, body), carries_code=True))
        else:
            messages.append(OutgoingMessage(
                kind="pm", label="Private message with the lifetime code",
                to=username, subject=subject, body=body,
                url=compose_url(username, subject, body), carries_code=True))

    elif action == DUPLICATE_QUERY:
        subject = item["subject"] or f"About your {cfg.name} code"
        messages.append(OutgoingMessage(
            kind="pm", label="Question about their earlier code", to=username,
            subject=subject, body=body,
            url=compose_url(username, subject, body)))

    elif action == REVIEW_ONLY:
        subject = item["subject"] or f"About {cfg.name}"
        if item["trigger_type"] == "comment":
            messages.append(OutgoingMessage(
                kind="comment_reply", label="Reply to their comment",
                to=item["parent_id"] or "", body=body,
                url=item["trigger_url"] or None))
        else:
            messages.append(OutgoingMessage(
                kind="pm", label="Reply to them", to=username,
                subject=subject, body=body,
                url=compose_url(username, subject, body)))
    else:
        raise ActionError(f"unknown action '{action}'")

    return messages


def record_sent(conn: sqlite3.Connection, cfg: AppConfig, item: sqlite3.Row,
                code: str | None = None) -> None:
    """The operator says it went out. Move state and close the item."""
    action = item["action"]
    username = item["username"]
    code = code or item["allocated_code"]

    with transaction(conn):
        if action == WEEKLY_CODE:
            store.set_user_state(conn, cfg.app_id, username,
                                 store.AWAITING_PROOF, weekly_code=code,
                                 weekly_sent_at=utcnow())
            log(conn, "weekly_code_sent", app_id=cfg.app_id,
                username=username, detail=code)
        elif action == LIFETIME_CODE:
            store.set_user_state(conn, cfg.app_id, username,
                                 store.LIFETIME_SENT, lifetime_code=code,
                                 lifetime_sent_at=utcnow())
            log(conn, "lifetime_code_sent", app_id=cfg.app_id,
                username=username, detail=code)
        elif action == DUPLICATE_QUERY:
            store.set_user_state(conn, cfg.app_id, username,
                                 store.DUPLICATE_QUERY_SENT)
            log(conn, "duplicate_query_sent", app_id=cfg.app_id,
                username=username)
        else:
            log(conn, "freeform_reply_sent", app_id=cfg.app_id,
                username=username, detail=f"queue:{item['id']}")
        store.set_queue_status(conn, item["id"], store.SENT,
                               allocated_code=code)


def record_not_sent(conn: sqlite3.Connection, item: sqlite3.Row,
                    reason: str = "operator said it was not sent") -> None:
    """It did not go out. Keep the code reserved so a retry reuses it."""
    with transaction(conn):
        store.set_queue_status(conn, item["id"], store.NEEDS_RETRY,
                               error=reason)
        log(conn, "send_not_confirmed", app_id=item["app_id"],
            username=item["username"], detail=f"queue:{item['id']}")
