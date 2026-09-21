"""Turns incoming comments and messages into pending queue drafts.

Every decision here is deterministic. Gemini is consulted only to *label* a
message or a nested comment; the label never chooses a code, changes a user
state, or decides a duplicate. No code is allocated at this stage - drafts
show `peek_next_code`, and the real allocation happens at send time.
"""

from __future__ import annotations

import sqlite3

from .config import AppConfig
from .db import transaction
from .sources.base import IncomingComment, IncomingMessage
from . import store

# Queue actions.
WEEKLY_CODE = "weekly_code"
LIFETIME_CODE = "lifetime_code"
DUPLICATE_QUERY = "duplicate_query"
REVIEW_ONLY = "review_only"

NO_CODE_PLACEHOLDER = "(OUT OF CODES)"


def _draft_weekly(conn, cfg: AppConfig, username: str) -> tuple[str | None, str, str | None]:
    """Render the weekly-code PM with the code that would be used next."""
    preview = store.peek_next_code(conn, cfg.app_id, "weekly")
    template = cfg.template("weekly_code")
    subject, body = template.render(
        username=username,
        code=preview or NO_CODE_PLACEHOLDER,
        app_name=cfg.name,
    )
    return subject, body, preview


def _draft_lifetime(conn, cfg: AppConfig, username: str) -> tuple[str | None, str, str | None]:
    preview = store.peek_next_code(conn, cfg.app_id, "lifetime")
    template = cfg.template("lifetime_code")
    subject, body = template.render(
        username=username,
        code=preview or NO_CODE_PLACEHOLDER,
        app_name=cfg.name,
    )
    return subject, body, preview


def process_comment(conn: sqlite3.Connection, cfg: AppConfig, item: IncomingComment,
                    my_username: str, classifier=None) -> int | None:
    """Handle one comment on a watched post. Returns a queue id, or None.

    Top-level comments are code requests by definition. Anything nested under
    one of my own replies is not assumed to be a request: it gets labelled and
    surfaced for me to decide.
    """
    if item.author is None:
        return None
    if my_username and item.author.lower() == my_username.lower():
        return None
    if store.is_processed(conn, item.item_id):
        return None

    username = item.author

    with transaction(conn):
        user = store.ensure_user(conn, cfg.app_id, username)

        if user["state"] == store.BLOCKED:
            store.mark_processed(conn, item.item_id, "comment", app_id=cfg.app_id,
                                 username=username, action="ignored_blocked")
            return None

        if not item.is_top_level:
            # A nested reply is ambiguous - label it, never auto-allocate.
            label = classifier.classify(conn, item.item_id, item.body) if classifier else None
            queue_id = store.enqueue(
                conn,
                app_id=cfg.app_id,
                username=username,
                action=REVIEW_ONLY,
                pool=None,
                draft_body="(no draft - nested reply, decide what to do)",
                trigger_type="comment",
                trigger_id=item.item_id,
                trigger_body=item.body,
                trigger_url=item.permalink,
                parent_id=item.parent_id,
            )
            store.mark_processed(conn, item.item_id, "comment", app_id=cfg.app_id,
                                 username=username, action="queued_review")
            return queue_id

        # A repeat asker is anyone who already holds a code, has moved past
        # 'new', or already has a weekly-code draft waiting in the queue. That
        # last case is what catches two comments inside the same poll.
        already_has_code = (
            user["weekly_code"] is not None
            or user["state"] in (
                store.WEEKLY_CODE_SENT, store.AWAITING_PROOF,
                store.DUPLICATE_QUERY_SENT, store.LIFETIME_SENT,
            )
            or store.has_code_item(conn, cfg.app_id, username, WEEKLY_CODE)
        )

        if already_has_code:
            subject, body = cfg.template("duplicate_query").render(
                username=username, app_name=cfg.name, code="",
            )
            queue_id = store.enqueue(
                conn,
                app_id=cfg.app_id,
                username=username,
                action=DUPLICATE_QUERY,
                pool=None,
                subject=subject,
                draft_body=body,
                trigger_type="comment",
                trigger_id=item.item_id,
                trigger_body=item.body,
                trigger_url=item.permalink,
                parent_id=item.parent_id,
            )
            store.mark_processed(conn, item.item_id, "comment", app_id=cfg.app_id,
                                 username=username, action="queued_duplicate_query")
            return queue_id

        subject, body, preview = _draft_weekly(conn, cfg, username)
        queue_id = store.enqueue(
            conn,
            app_id=cfg.app_id,
            username=username,
            action=WEEKLY_CODE,
            pool="weekly",
            subject=subject,
            draft_body=body,
            trigger_type="comment",
            trigger_id=item.item_id,
            trigger_body=item.body,
            trigger_url=item.permalink,
            parent_id=item.item_id,   # the public ack replies to this comment
            preview_code=preview,
        )
        store.mark_processed(conn, item.item_id, "comment", app_id=cfg.app_id,
                             username=username, action="queued_weekly_code")
        return queue_id


def attribute_app(conn: sqlite3.Connection, username: str,
                  known_app_ids: list[str]) -> str | None:
    """Which app a stray message belongs to, or None if ambiguous.

    Because the tool always initiates the PM thread, almost every incoming
    message is a reply to something we sent, so this usually resolves. When it
    cannot, the item is queued with app_id NULL for me to assign.
    """
    apps = store.apps_known_for_user(conn, username)
    if len(apps) == 1:
        return apps[0]
    if not apps and len(known_app_ids) == 1:
        return known_app_ids[0]
    return None


def process_message(conn: sqlite3.Connection, cfg: AppConfig | None,
                    item: IncomingMessage, classifier=None,
                    app_id: str | None = None) -> int | None:
    """Handle one private message. Gemini labels it; I decide what happens."""
    if item.author is None:
        return None
    if store.is_processed(conn, item.item_id):
        return None

    username = item.author
    resolved_app = app_id or (cfg.app_id if cfg else None)

    label = classifier.classify(conn, item.item_id, item.body) if classifier else None
    intent = label.intent if label else "unclear"

    with transaction(conn):
        if resolved_app:
            user = store.ensure_user(conn, resolved_app, username)
            if user["state"] == store.BLOCKED:
                store.mark_processed(conn, item.item_id, "message",
                                     app_id=resolved_app, username=username,
                                     action="ignored_blocked")
                return None
        else:
            user = None

        # Only a proof submission on a known app gets a lifetime-code draft.
        # Everything else is surfaced without a draft.
        if intent == "proof_submission" and resolved_app and cfg:
            subject, body, preview = _draft_lifetime(conn, cfg, username)
            action, pool = LIFETIME_CODE, "lifetime"
        else:
            subject, body, preview = None, "(no draft - review and decide)", None
            action, pool = REVIEW_ONLY, None

        queue_id = store.enqueue(
            conn,
            app_id=resolved_app,
            username=username,
            action=action,
            pool=pool,
            subject=subject,
            draft_body=body,
            trigger_type="message",
            trigger_id=item.item_id,
            trigger_body=item.body,
            trigger_url=None,
            parent_id=item.item_id,   # replies go back into this thread
            preview_code=preview,
        )
        store.mark_processed(conn, item.item_id, "message", app_id=resolved_app,
                             username=username, action=f"queued_{action}")
        return queue_id
