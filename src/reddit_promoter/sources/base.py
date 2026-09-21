"""Shape of the items a source feeds into the engine.

Both the fake source (for testing the review flow offline) and the PRAW source
produce these, so the engine never knows which it is talking to.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

URL_RE = re.compile(r"https?://[^\s<>\)\]\"']+")


def extract_urls(text: str | None) -> list[str]:
    """Every URL in a message, de-duplicated, in order of appearance.

    Used for proof submissions so the reviewer can open each screenshot link
    themselves before approving a lifetime code.
    """
    if not text:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for match in URL_RE.findall(text):
        url = match.rstrip(".,;:")
        if url not in seen:
            seen.add(url)
            out.append(url)
    return out


@dataclass(frozen=True)
class IncomingComment:
    item_id: str           # PRAW fullname, e.g. t1_abc123
    post_id: str           # submission id the comment sits under
    author: str | None     # None for deleted accounts
    body: str
    permalink: str
    is_top_level: bool
    parent_id: str
    created_utc: float


@dataclass(frozen=True)
class IncomingMessage:
    item_id: str           # PRAW fullname, e.g. t4_abc123
    author: str | None
    subject: str
    body: str
    parent_id: str | None  # set when this is a reply within a thread we started
    created_utc: float

    @property
    def urls(self) -> list[str]:
        return extract_urls(self.body)


class Source(Protocol):
    """Where new comments and messages come from."""

    def new_comments(self, post_id: str) -> list[IncomingComment]:
        ...

    def new_messages(self) -> list[IncomingMessage]:
        ...
