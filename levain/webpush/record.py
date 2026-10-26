"""levain.webpush.record: the notification record (P4), every ping Levain sent and what happened to it.

Phill ruled the shape 10-19 (project_memory/1019-app/SHAPE.md, c1, *"that is approved"*): each ping with when and why, where
it went (each phone's push service, and whether the notify command started: a ping only the command carried is a row
with no deliveries), whether it was opened and on which phone, and
its OUTCOME, which is read from the consent queue and the question store at display time and never copied into the
record. The record says what Levain did; the queue says what came of it; a second copy of the queue's state would be a
second truth that drifts.

A ping's ``ref`` names what it was about: ``request:<consent request id>`` or ``question:<n>``; anything else (a test, a
ping sent before refs existed) has no outcome to read and says so.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["SHOWN", "outcome_of", "record"]

# the newest pings a view shows; the store keeps every ping (it is an append-only record)
SHOWN = 500

_REQUEST_WORDS = {
    "pending": "waiting",
    "undecided": "undecided, waiting for a late ruling",
    "allowed": "allowed",
    "denied": "denied",
    "expired": "expired unanswered",
    "withdrawn": "withdrawn by the agent",
}


def outcome_of(queue_dir: Path, ref: Any, *, _queue: Any = None, _questions: Any = None) -> dict[str, Any]:
    """``{"state", "words"}`` for one ping's ref, read now. ``state`` is the queue's own word (or ``answered`` /
    ``open`` for a question, ``none`` when the ping names nothing, ``unread`` when the store could not be read)."""
    if not isinstance(ref, str) or ":" not in ref:
        return {"state": "none", "words": "nothing to follow up"}
    kind, _, key = ref.partition(":")
    try:
        if kind == "request":
            if _queue is None:
                from levain.consent.queue import ConsentQueue, authority_source

                _queue = ConsentQueue(queue_dir, home=Path.home(), authority=authority_source(queue_dir, anchor=None))
            got = _queue.get(key)
            if got is None:
                return {"state": "gone", "words": "no longer in the queue"}
            _request, state = got
            return {"state": state, "words": _REQUEST_WORDS.get(state, state)}
        if kind == "question":
            if _questions is None:
                from levain.consent.questions import QuestionStore

                _questions = QuestionStore(queue_dir)
            q = _questions.get(int(key)) if key.isdigit() else None
            if q is None:
                return {"state": "gone", "words": "no such question"}
            if q.answer is None:
                return {"state": "open", "words": "not answered yet"}
            return {"state": "answered", "words": f"answered from the {q.answer.channel}"}
    except Exception as exc:  # noqa: BLE001 - an unread store is said, never drawn as an outcome
        return {"state": "unread", "words": f"outcome could not be read: {type(exc).__name__}"}
    return {"state": "none", "words": "nothing to follow up"}


def record(queue_dir: Path, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The store's newest ping rows (each with its deliveries and opens, as :mod:`levain.webpush.store` lists them),
    each joined with its outcome read now. One queue and one question store serve the whole listing."""
    queue = questions = None
    out = []
    for row in rows[:SHOWN]:
        ref = row.get("ref")
        kind = ref.partition(":")[0] if isinstance(ref, str) else ""
        if kind == "request" and queue is None:
            try:
                from levain.consent.queue import ConsentQueue, authority_source

                queue = ConsentQueue(queue_dir, home=Path.home(), authority=authority_source(queue_dir, anchor=None))
            except Exception:  # noqa: BLE001 - outcome_of says "unread" per row
                queue = None
        if kind == "question" and questions is None:
            try:
                from levain.consent.questions import QuestionStore

                questions = QuestionStore(queue_dir)
            except Exception:  # noqa: BLE001
                questions = None
        out.append({**row, "outcome": outcome_of(queue_dir, ref, _queue=queue, _questions=questions)})
    return out
