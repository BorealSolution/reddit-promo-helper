"""Answers, empirically, what the Reddit API can still do with direct messages.

Reddit announced in March 2025 that it is replacing classic Private Messages
with Reddit Chat plus inbox notifications. Chat has no public API, so anything
sent that way is invisible here. The whole proof loop depends on a reply to a
PM *we* sent coming back into the API-visible inbox, and that is not something
the documentation settles - so measure it instead of assuming it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import prawcore
from rich.console import Console
from rich.panel import Panel

from .sources.reddit_source import build_reddit, verify_auth


@dataclass
class DMReport:
    username: str = ""
    can_read_inbox: bool = False
    message_count: int = 0
    unread_count: int = 0
    can_read_sent: bool = False
    sent_count: int = 0
    recent_senders: list[str] = field(default_factory=list)
    threaded_replies_seen: int = 0
    errors: list[str] = field(default_factory=list)


def run_check(secrets) -> DMReport:
    report = DMReport()
    reddit = build_reddit(secrets)
    report.username = verify_auth(reddit)

    try:
        messages = list(reddit.inbox.messages(limit=25))
        report.can_read_inbox = True
        report.message_count = len(messages)
        report.recent_senders = [
            str(m.author) for m in messages[:10] if getattr(m, "author", None)
        ]
        # A message whose first_message is set is a reply inside a thread -
        # exactly the shape the proof loop relies on.
        report.threaded_replies_seen = sum(
            1 for m in messages if getattr(m, "first_message", None)
        )
    except prawcore.exceptions.PrawcoreException as exc:
        report.errors.append(f"could not read inbox messages: {exc}")

    try:
        report.unread_count = len(list(reddit.inbox.unread(limit=25)))
    except prawcore.exceptions.PrawcoreException as exc:
        report.errors.append(f"could not read unread: {exc}")

    try:
        sent = list(reddit.inbox.sent(limit=25))
        report.can_read_sent = True
        report.sent_count = len(sent)
    except prawcore.exceptions.PrawcoreException as exc:
        report.errors.append(f"could not read sent messages: {exc}")

    return report


def print_report(console: Console, report: DMReport) -> None:
    lines = [
        f"authenticated as      u/{report.username}",
        f"classic inbox readable  {'yes' if report.can_read_inbox else 'NO'}",
        f"  messages visible      {report.message_count}",
        f"  unread                {report.unread_count}",
        f"  replies within a thread {report.threaded_replies_seen}",
        f"sent folder readable    {'yes' if report.can_read_sent else 'NO'}"
        f"  ({report.sent_count} visible)",
    ]
    if report.recent_senders:
        lines.append("recent senders        "
                     + ", ".join(f"u/{s}" for s in report.recent_senders))

    console.print(Panel("\n".join(lines), title="[bold]Reddit DM capability",
                        border_style="blue"))

    for err in report.errors:
        console.print(f"[red]{err}[/]")

    console.print(Panel(
        "Reddit Chat has no public API. PRAW cannot read it, send to it, or "
        "see that a chat exists.\n\n"
        "This tool uses classic private messages, which do still work. The gap "
        "is one-way: if someone contacts you with the [bold]Chat[/] button "
        "instead of replying to the message thread the tool started, it will "
        "never appear here and you will not be told.\n\n"
        "[bold]To confirm the proof loop end to end:[/]\n"
        "  1. From a second Reddit account, or by asking a friend, have them "
        "receive a PM from this tool (or send one manually from "
        f"u/{report.username}).\n"
        "  2. Reply to that message from the other account, using the "
        "[bold]message thread[/], not chat.\n"
        "  3. Run this command again and check that 'unread' went up and the "
        "other account shows in 'recent senders'.\n\n"
        "If the reply does not appear, the proof-by-PM flow cannot be relied "
        "on, and proof should be collected as a public comment reply instead.",
        title="[bold yellow]What this means", border_style="yellow"))
