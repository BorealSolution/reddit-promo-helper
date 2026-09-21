"""The real Reddit boundary: authentication, polling, and sending via PRAW.

Everything that can fail against a live API lives here, behind the same
`Source` and `Sender` shapes the fake source uses, so the engine, the queue,
and the dashboard never know which one they are talking to.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import praw
import prawcore
from praw.exceptions import RedditAPIException

from ..config import Secrets
from ..senders import SendError
from .base import IncomingComment, IncomingMessage

log = logging.getLogger(__name__)

# Transient conditions worth retrying; anything else is a real error.
RETRYABLE = (
    prawcore.exceptions.RequestException,     # connection dropped / DNS / timeout
    prawcore.exceptions.ServerError,          # 5xx
    prawcore.exceptions.TooManyRequests,      # 429
)

MAX_ATTEMPTS = 4
BASE_BACKOFF = 2.0


class RedditAuthError(Exception):
    pass


def build_reddit(secrets: Secrets, *, read_only: bool = False) -> praw.Reddit:
    """Authenticate a script app via the password flow."""
    missing = secrets.missing_reddit()
    if missing:
        raise RedditAuthError(
            "missing from .env: " + ", ".join(missing)
        )

    reddit = praw.Reddit(
        client_id=secrets.reddit_client_id,
        client_secret=secrets.reddit_client_secret,
        username=secrets.reddit_username,
        password=secrets.reddit_password,
        user_agent=secrets.reddit_user_agent,
        # Let PRAW sleep through Reddit's own rate limiting rather than
        # raising, up to ten minutes.
        ratelimit_seconds=600,
    )
    reddit.read_only = read_only
    return reddit


def verify_auth(reddit: praw.Reddit) -> str:
    """Confirm the credentials work. Returns the authenticated username."""
    try:
        me = reddit.user.me()
    except prawcore.exceptions.OAuthException as exc:
        raise RedditAuthError(
            f"Reddit rejected the credentials: {exc}. Check the client id and "
            "secret came from a 'script' app, and that the username and "
            "password are for the account that owns it. If the account has "
            "2FA enabled, the password flow will not work."
        ) from exc
    except prawcore.exceptions.ResponseException as exc:
        raise RedditAuthError(f"Reddit returned {exc}") from exc
    if me is None:
        raise RedditAuthError("authenticated read-only; cannot act as a user")
    return str(me)


def with_retry(what: str, fn, *args, **kwargs):
    """Call a PRAW operation, backing off on transient failures."""
    delay = BASE_BACKOFF
    last: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except RETRYABLE as exc:
            last = exc
            if attempt == MAX_ATTEMPTS:
                break
            wait = delay
            if isinstance(exc, prawcore.exceptions.TooManyRequests):
                wait = max(wait, float(getattr(exc, "retry_after", 0) or 0))
            log.warning("%s failed (%s); retrying in %.0fs (attempt %d/%d)",
                        what, exc, wait, attempt, MAX_ATTEMPTS)
            time.sleep(wait)
            delay *= 2
        except RedditAPIException as exc:
            # RATELIMIT here means Reddit wants us to slow down; the rest are
            # real refusals (banned, blocked, deleted thread) and must surface.
            if any(item.error_type == "RATELIMIT" for item in exc.items):
                last = exc
                if attempt == MAX_ATTEMPTS:
                    break
                log.warning("%s rate limited; waiting %.0fs", what, delay)
                time.sleep(delay)
                delay *= 2
                continue
            raise

    raise SendError(f"{what} failed after {MAX_ATTEMPTS} attempts: {last}")


@dataclass
class RedditSource:
    """Reads comments on watched posts and messages from the inbox."""

    reddit: praw.Reddit
    my_username: str = ""

    def new_comments(self, post_id: str) -> list[IncomingComment]:
        """Every comment on a post, flattened. The engine filters and dedupes."""
        submission = self.reddit.submission(id=post_id)
        with_retry(f"loading comments for {post_id}",
                   submission.comments.replace_more, limit=None)

        top_level_ids = {c.fullname for c in submission.comments}
        out: list[IncomingComment] = []

        for comment in submission.comments.list():
            author = getattr(comment, "author", None)
            out.append(IncomingComment(
                item_id=comment.fullname,
                post_id=post_id,
                author=str(author) if author else None,
                body=comment.body or "",
                permalink=f"https://www.reddit.com{comment.permalink}",
                is_top_level=comment.fullname in top_level_ids,
                parent_id=comment.parent_id,
                created_utc=comment.created_utc,
            ))
        return out

    def new_messages(self, limit: int = 100) -> list[IncomingMessage]:
        """Recent private messages, read or unread.

        Deliberately not `inbox.unread()`: opening a message in the Reddit app
        marks it read, and it would then never be seen here. Deduplication is
        the `processed_items` table's job, so re-reading is harmless.

        Comment replies and mentions also land in the inbox; those belong to
        the comment flow, so they are filtered out.
        """
        out: list[IncomingMessage] = []
        items = with_retry("reading inbox",
                           lambda: list(self.reddit.inbox.messages(limit=limit)))
        for item in items:
            if not isinstance(item, praw.models.Message):
                continue
            if getattr(item, "was_comment", False):
                continue
            if self.my_username and str(getattr(item, "author", "")) == self.my_username:
                continue   # our own side of the thread
            author = getattr(item, "author", None)
            out.append(IncomingMessage(
                item_id=item.fullname,
                author=str(author) if author else None,
                subject=item.subject or "",
                body=item.body or "",
                parent_id=item.fullname,
                created_utc=item.created_utc,
            ))
        return out

    def mark_read(self, item_ids: list[str]) -> None:
        """Clear handled messages so the next poll does not re-read them."""
        if not item_ids:
            return
        messages = [self.reddit.inbox.message(i.removeprefix("t4_")) for i in item_ids]
        with_retry("marking messages read", self.reddit.inbox.mark_read, messages)


@dataclass
class RedditSender:
    """The only place in the tool that writes to Reddit."""

    reddit: praw.Reddit

    def send_pm(self, username: str, subject: str, body: str) -> None:
        redditor = self.reddit.redditor(username)
        try:
            # PRAW 8 made these keyword-only.
            with_retry(f"PM to u/{username}", redditor.message,
                       subject=subject, message=body)
        except RedditAPIException as exc:
            raise SendError(f"Reddit refused the PM to u/{username}: {exc}") from exc
        except prawcore.exceptions.Forbidden as exc:
            raise SendError(
                f"not allowed to message u/{username} - they may have blocked "
                f"you or restricted messages: {exc}"
            ) from exc
        except prawcore.exceptions.NotFound as exc:
            raise SendError(f"u/{username} no longer exists: {exc}") from exc

    def reply_to_message(self, message_id: str, body: str) -> None:
        message = self.reddit.inbox.message(message_id.removeprefix("t4_"))
        try:
            with_retry(f"reply to message {message_id}", message.reply, body)
        except RedditAPIException as exc:
            raise SendError(f"Reddit refused the reply: {exc}") from exc
        except prawcore.exceptions.Forbidden as exc:
            raise SendError(f"not allowed to reply to {message_id}: {exc}") from exc

    def reply_to_comment(self, comment_id: str, body: str) -> None:
        comment = self.reddit.comment(comment_id.removeprefix("t1_"))
        try:
            with_retry(f"reply to comment {comment_id}", comment.reply, body)
        except RedditAPIException as exc:
            raise SendError(f"Reddit refused the comment reply: {exc}") from exc
        except prawcore.exceptions.Forbidden as exc:
            raise SendError(
                f"not allowed to reply to {comment_id} - the thread may be "
                f"locked or archived: {exc}"
            ) from exc


def resolve_post(reddit: praw.Reddit, post_id: str) -> dict:
    """Fetch a post's subreddit and title so `watch` can record them."""
    submission = reddit.submission(id=post_id)
    return {
        "subreddit": str(submission.subreddit),
        "title": submission.title,
        "author": str(submission.author) if submission.author else None,
    }
