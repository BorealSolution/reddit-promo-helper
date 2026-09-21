"""Outbound side: the thing that actually talks (or pretends to talk) to Reddit.

Swapping the sender is what makes --dry-run and the fake-source dashboard test
exercise the identical approve/send path as the real thing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class SendError(Exception):
    """A send failed. The caller keeps the code reserved and marks needs_retry."""


@dataclass
class SentRecord:
    kind: str
    target: str
    subject: str | None
    body: str


class Sender(Protocol):
    def send_pm(self, username: str, subject: str, body: str) -> None:
        ...

    def reply_to_message(self, message_id: str, body: str) -> None:
        ...

    def reply_to_comment(self, comment_id: str, body: str) -> None:
        ...


@dataclass
class RecordingSender:
    """Sends nothing, records everything. Backs --dry-run and the fake source."""

    label: str = "dry-run"
    sent: list[SentRecord] = field(default_factory=list)
    fail_next: bool = False

    def _record(self, kind: str, target: str, body: str, subject=None) -> None:
        if self.fail_next:
            self.fail_next = False
            raise SendError(f"{self.label}: simulated failure")
        self.sent.append(SentRecord(kind=kind, target=target, subject=subject, body=body))

    def send_pm(self, username: str, subject: str, body: str) -> None:
        self._record("pm", username, body, subject)

    def reply_to_message(self, message_id: str, body: str) -> None:
        self._record("pm_reply", message_id, body)

    def reply_to_comment(self, comment_id: str, body: str) -> None:
        self._record("comment_reply", comment_id, body)
