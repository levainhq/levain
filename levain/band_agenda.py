"""levain.band_agenda: THE LIST's "make this an agenda" (row 183; flow D_list_intention_object section 9, Phill 11-10 / 11-11).

A machine-filed band row the operator wants pushed past the snooze window is not a band row: it becomes a dated agenda that
wakes back into the band on its day. Two acts in a fixed order:

1. plant the agenda (the row's words, the day, target ``band``, the question id in its ``pointer``) in ONE ``add`` under a
   key derived from the question id alone, so a retry lands the same intention whatever day it names;
2. answer the row with the deferral words, so the stopped seat reads that it must wait, then deliver the answer as any
   cockpit answer is delivered.

The audit line is written after both. Every refusal that can be known beforehand is raised before act 1. A crash between the
acts leaves the row open with its agenda planted; the retry finds the key, keeps the day already planted and goes on to act 2.
Each act is its own function so a test can fail the seam between them."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from levain.consent.questions import SNOOZE_MAX_DAYS, valid_date

KEY_PREFIX = "band-agenda:"
AGENDA_TYPE = "task"
AGENDA_TARGET = "band"


def agenda_origin_key(question_id: str) -> str:
    """The agenda's intention key:``band-agenda:`` + the sha256 hex of the question id, always inside anneal's key grammar
    (``[0-9A-Za-z._:-]{1,128}``). The day is not in it, so one row has one agenda."""
    return KEY_PREFIX + hashlib.sha256(question_id.encode("utf-8")).hexdigest()


def day_refusal(day: Any, today: date) -> str | None:
    """Why ``day`` cannot be the agenda's day, in the words the operator reads, or None: a real ``YYYY-MM-DD`` strictly after
    the snooze window's last day. A nearer day is a snooze's, and the words say so."""
    if not valid_date(day):
        return f"an agenda's day is written YYYY-MM-DD: {day!r} is not a date"
    latest = today + timedelta(days=SNOOZE_MAX_DAYS)
    if date.fromisoformat(day) <= latest:
        return (f"{day} is within the snooze window (a snooze is at most {SNOOZE_MAX_DAYS} days, so {latest.isoformat()} at "
                f"the latest): snooze the row instead; an agenda is for a day after {latest.isoformat()}")
    return None


def deferral_words(day: str, agenda_id: str) -> str:
    return f"deferred: comes back {day} as {agenda_id}; do not proceed before then"


def _plant(store: Any, *, text: str, day: str, question_id: str, today: date) -> dict[str, Any]:
    """Act 1: the one ``add``. Its own seam so a test can fail it; a key already held returns that intention unchanged."""
    return store.add(type=AGENDA_TYPE, text=text, disposition="agenda", next=day, target=AGENDA_TARGET,
                     pointer=f"question:{question_id}", origin_key=agenda_origin_key(question_id), today=today)


def _answer_deferred(questions: Any, question_id: str, words: str) -> None:
    """Act 2: record the answer (channel ``cockpit``, unsigned: it authorises nothing), then deliver it to the agent's session
    on a daemon thread, as ``question_answer`` does. A delivery error never undoes the answer: ``levain question deliver``
    retries it. Its own seam so a test can fail it after the agenda landed."""
    from levain.consent import band

    questions.answer(question_id, text=words, channel="cockpit")

    def deliver() -> None:
        try:
            band.deliver_answer(questions, question_id)
        except Exception:  # noqa: BLE001 - the answer stands
            pass

    threading.Thread(target=deliver, name="levain-answer-delivery", daemon=True).start()


def defer(*, questions: Any, intentions_json: Path, ledger_root: Path, question_id: str, day: Any, today: date,
          now: str | None = None) -> dict[str, Any]:
    """Make the open band row ``question_id`` an agenda for ``day``. Refusals raise :class:`levain.writes.EditError` with
    nothing written."""
    from anneal_memory.spores import IntentionError

    from levain.cockpit.consent import ConsentError
    from levain.consent.questions import QuestionStoreError, intention_of
    from levain.intentions import guarded_intention_store
    from levain.writes import EditError, _audit_verb

    refused = day_refusal(day, today)
    if refused:
        raise EditError("bad_request", 400, refused)
    try:
        q = questions.get(question_id)
    except (QuestionStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503, f"the question store cannot be read now: {exc}") from None
    if q is None or q.answer is not None:
        raise EditError("stale", 409, "the row was answered or is gone since it was read")
    if intention_of(questions, q) is not None:
        raise EditError("rings_a_reminder", 409, "this row rings a reminder: its Snooze moves it any distance, so it needs no agenda")
    text = str(q.what or "").strip()
    if not text:
        raise EditError("bad_verb_arg", 422, "an agenda needs the row's words: this row has none")

    try:
        agenda = _plant(guarded_intention_store(intentions_json), text=text, day=day, question_id=q.id, today=today)
    except (IntentionError, ValueError) as exc:
        raise EditError("verb_failed", 422, f"the agenda could not be planted: {exc}") from None
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"intention store unavailable: {exc}") from None
    if agenda.get("status") == "resolved":
        raise EditError("agenda_resolved", 409,
                        f"this row's agenda ({agenda.get('id')}) was already resolved, so it cannot wake the row: "
                        "answer the row in words")
    planted = str(agenda.get("next") or day)
    earlier = planted != day                  # the key alone decides: an earlier plant's day stands
    words = deferral_words(planted, str(agenda["id"]))
    try:
        _answer_deferred(questions, q.id, words)
    except ConsentError as exc:
        if "already answered" in str(exc):
            raise EditError("stale", 409, f"the row was answered meanwhile; its agenda {agenda['id']} stands for {planted}") from None
        raise EditError("bad_request", 400, str(exc)) from None
    except (QuestionStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503,
                        f"agenda {agenda['id']} is planted for {planted} but the row was not answered ({exc}): retry") from None
    edit_id = _audit_verb(ledger_root, kind="band_agenda", action="band_agenda", target=f"intention:{agenda['id']}", now=now,
                          extra={"question": q.id, "day": planted, "origin_key": agenda_origin_key(q.id)})
    line = f"-> Later, wakes {planted} · band ({agenda['id']})"
    if earlier:
        line += f"; planted earlier, so that day stands (you asked for {day})"
    return {"ok": True, "id": edit_id, "source": f"intention:{agenda['id']}", "action": "band_agenda", "agenda": agenda["id"],
            "day": planted, "question": q.id, "receipt_line": line, **({"earlier_day_kept": True} if earlier else {})}
