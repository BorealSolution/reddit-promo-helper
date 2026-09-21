"""Message labelling.

The classifier's only job is to attach a label to a piece of text. It never
chooses a code, never changes a user's state, never decides a duplicate, and
never writes a reply - all of that is deterministic code in `store` and
`engine`. A wrong label costs a misfiled queue entry, nothing more.

Results are cached in the `classifications` table so re-running `poll` does
not re-bill the API for text that has already been seen.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from . import store

INTENTS = ("code_request", "proof_submission", "question", "other")
UNCLEAR = "unclear"
MIN_CONFIDENCE = 0.7


@dataclass(frozen=True)
class Label:
    intent: str
    confidence: float
    reason: str
    model: str

    @property
    def is_confident(self) -> bool:
        return self.intent != UNCLEAR and self.confidence >= MIN_CONFIDENCE


def _cached(conn: sqlite3.Connection, item_id: str) -> Label | None:
    row = store.get_classification(conn, item_id)
    if row is None:
        return None
    return Label(
        intent=row["intent"],
        confidence=row["confidence"],
        reason=row["reason"] or "",
        model=row["model"],
    )


class BaseClassifier:
    model_name = "none"

    def _label(self, text: str) -> Label:
        raise NotImplementedError

    def classify(self, conn: sqlite3.Connection, item_id: str, text: str) -> Label:
        """Cached classify. Anything below the confidence floor becomes unclear."""
        hit = _cached(conn, item_id)
        if hit is not None:
            return hit

        label = self._label(text)
        if label.confidence < MIN_CONFIDENCE and label.intent != UNCLEAR:
            label = Label(UNCLEAR, label.confidence,
                          f"below confidence floor ({label.confidence:.2f}): {label.reason}",
                          label.model)

        store.save_classification(conn, item_id, label.intent, label.confidence,
                                  label.reason, label.model)
        conn.commit()
        return label


class OfflineClassifier(BaseClassifier):
    """Keyword stand-in used by the fake source and --dry-run.

    Crude on purpose: it exists so the review flow can be exercised without an
    API key, not to be accurate. It is never used when a real key is present.
    """

    model_name = "offline-keywords"

    PROOF = ("review", "reviewed", "screenshot", "rated", "5 star", "five star",
             "imgur", "proof")
    REQUEST = ("code", "promo", "can i get", "me please", "interested")
    QUESTION = ("?", "how do", "does it", "can it", "what ")

    def _label(self, text: str) -> Label:
        low = (text or "").lower()
        has_link = "http" in low

        if any(k in low for k in self.PROOF) and has_link:
            return Label("proof_submission", 0.85,
                         "mentions a review and includes a link", self.model_name)
        if any(k in low for k in self.PROOF):
            return Label("proof_submission", 0.6,
                         "mentions a review but has no link", self.model_name)
        if any(k in low for k in self.REQUEST):
            return Label("code_request", 0.75, "asks for a code", self.model_name)
        if any(k in low for k in self.QUESTION):
            return Label("question", 0.7, "reads as a question", self.model_name)
        return Label("other", 0.5, "no keyword matched", self.model_name)
