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


class ManualSender:
    """Manual mode: the tool prepares the message, the operator sends it.

    This is a first-class path, not a fallback. It needs no Reddit
    credentials at all. For each message it copies the exact text to the
    clipboard, shows where to paste it, and asks for confirmation. Answering
    "no" raises SendError, which the normal machinery turns into needs_retry
    with the code still reserved - so nothing is marked sent that was not.
    """

    def __init__(self, console, confirm=None):
        self.console = console
        self._confirm = confirm          # injectable for tests
        self.item = None                 # set per queue item by the dashboard
        self.sent: list[SentRecord] = []

    # The dashboard calls this before each item so the instructions can
    # include the actual comment permalink.
    def begin(self, item) -> None:
        self.item = item

    # ------------------------------------------------------------------
    def _ask(self, question: str) -> bool:
        if self._confirm is not None:
            return self._confirm(question)
        from rich.prompt import Prompt
        try:
            answer = Prompt.ask(question, choices=["y", "n"], default="n")
        except EOFError:
            # Input closed mid-prompt: assume it was NOT sent. Claiming a
            # send that did not happen is the one unrecoverable mistake.
            return False
        return answer.strip().lower() == "y"

    def _hand_over(self, *, what: str, where: str, body: str,
                   url: str | None = None) -> None:
        from rich.panel import Panel
        from rich.text import Text
        from . import clipboard

        try:
            backend = clipboard.copy(body)
            self.console.print(f"[green]Copied to clipboard[/] [dim]({backend})[/]")
        except clipboard.ClipboardUnavailable as exc:
            self.console.print(f"[yellow]Could not reach the clipboard "
                               f"({exc}) - copy the text below by hand[/]")

        self.console.print(Panel(Text(body), title=f"[bold]{what} - paste this",
                                 border_style="green"))
        self.console.print(f"[bold]Where:[/] {where}")
        if url:
            self.console.print(f"[blue underline]{url}[/]")

        if not self._ask(f"Did you send the {what.lower()}?"):
            raise SendError(f"{what} was not sent (you answered no)")

        self.sent.append(SentRecord(kind=what, target=where, subject=None,
                                    body=body))

    # ------------------------------------------------------------------
    def send_pm(self, username: str, subject: str, body: str) -> None:
        # Reddit can prefill the compose form from a URL, which beats
        # pasting into the right boxes by hand.
        from urllib.parse import quote
        url = ("https://www.reddit.com/message/compose/"
               f"?to={quote(username)}&subject={quote(subject or '')}"
               f"&message={quote(body)}")
        self._hand_over(
            what="Private message",
            where=f"a private message to u/{username}  (subject: {subject})",
            body=body,
            url=url,
        )

    def reply_to_message(self, message_id: str, body: str) -> None:
        # Items entered by hand have synthetic ids; linking to them would
        # produce a dead URL, which is worse than no link.
        url = None
        if not message_id.startswith("manual_"):
            url = ("https://www.reddit.com/message/messages/"
                   + message_id.removeprefix("t4_"))
        who = ""
        if self.item is not None:
            try:
                who = f" with u/{self.item['username']}"
            except (KeyError, IndexError, TypeError):
                who = ""
        self._hand_over(
            what="Message reply",
            where=f"reply inside the existing message thread{who}"
                  + ("" if url else "  (open it from your Reddit inbox)"),
            body=body,
            url=url,
        )

    def reply_to_comment(self, comment_id: str, body: str) -> None:
        url = None
        if self.item is not None:
            try:
                url = self.item["trigger_url"] or None
            except (KeyError, IndexError, TypeError):
                url = None
        self._hand_over(
            what="Public reply",
            where="a reply to their comment on the post",
            body=body,
            url=url,
        )
