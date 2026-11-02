"""The count of open Needs-you items, for the app-icon badge (1101-6 lane B).

The cockpit's Needs-you strip counts a consent request in state ``pending`` (filed, not yet answered, not expired) plus a
question that is open (no answer): ``needsItems`` in ``dashboard_core.js`` over the decisions panel's rows, which the
engine builds from exactly these two sources (``levain.consent.listing.read_listing`` and
``QuestionStore.open_questions``) with exactly these two predicates. This is the same count read at the same two sources,
for a caller with no page (the push sender, the unread-count route). A notice never counts: it is not a needs item
(Phill's N3 ruling), and nothing here reads the notices table.

``None`` means unknown (a store that could not be read), never zero: a wrong 0 would clear a real badge.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

__all__ = ["BADGE_MAX", "open_needs_count", "badge_value"]

BADGE_MAX = 99      # payload room is tight and a badge past two digits says nothing more; the page shows the exact count


def open_needs_count(queue_dir: "Path | str") -> "int | None":
    """Pending consent requests plus open questions in ``queue_dir`` as of now; None when either store cannot be read.
    Creates nothing (a queue with no ``questions.db`` has no open questions)."""
    try:
        from levain.consent.listing import read_listing
        from levain.consent.questions import QuestionStore

        got = read_listing(queue_dir, now=datetime.now(timezone.utc))
        consents = sum(1 for e in got.pending if e.state == "pending")
        return consents + len(QuestionStore(queue_dir).open_questions())
    except Exception:  # noqa: BLE001 - unknown is an answer; the badge then stays as it is
        return None


def badge_value(queue_dir: "Path | str") -> "int | None":
    """The int a push carries as ``badge``: the open count clipped to :data:`BADGE_MAX`, None when unknown."""
    n = open_needs_count(queue_dir)
    return None if n is None else min(max(n, 0), BADGE_MAX)
