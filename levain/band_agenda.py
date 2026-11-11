"""levain.band_agenda: THE LIST's "make this an agenda" (row 183; flow D_list_intention_object section 9, Phill 11-10 / 11-11).

A machine-filed band row the operator wants pushed past the snooze window is not a band row: it becomes a dated agenda that
wakes back into the band on its day. Two acts in a fixed order:

1. plant the agenda (the row's words, the day, target ``band``, the question id in its ``pointer``) in ONE ``add`` under a
   key derived from the question id alone, so a retry lands the same spore whatever day it names;
2. answer the row with the deferral words, so the stopped seat reads that it must wait, then deliver the answer as any
   cockpit answer is delivered.

The audit line is written after both. Every refusal that can be known beforehand is raised before act 1: a row settled by
signing, a row that rings a reminder, a host that cannot take a text answer. A crash between the acts leaves the row open with
its agenda planted; the retry finds the key, keeps the day already planted and goes on to act 2. When the host REFUSES act 2
(a host may refuse an unsigned answer by its own setting, which levain cannot ask beforehand) and this call planted the
agenda, the agenda is composted with the refusal in its note and an audit line records the aborted attempt, so a refused
deferral leaves nothing planted. Each act is its own function so a test can fail the seam between them."""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
import threading
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from levain.consent.questions import SNOOZE_MAX_DAYS, valid_date, valid_intention_id

KEY_PREFIX = "band-agenda:"
AGENDA_TYPE = "task"
AGENDA_TARGET = "band"

# why a band row is not offered the verb (the List row's ``agenda_blocked`` facet)
BLOCK_SIGNS = "signs"            # a question with a ``link`` or ``sign_request``: it is settled by signing on its own sheet
BLOCK_RINGS = "rings"            # it rings a reminder, whether or not this host can act on it
BLOCK_UNREADABLE = "unreadable"  # the band item could not be read
BLOCK_NO_ANSWER = "no_answer"    # the host store has no ``answer`` that takes a text answer on the cockpit channel


def answer_refusal(questions: Any) -> str | None:
    """Why ``questions`` cannot take act 2 (a text answer on the cockpit channel), or None. Checked before the plant, so a host
    that could never answer leaves no agenda behind."""
    fn = getattr(questions, "answer", None)
    if not callable(fn):
        return "this band's question store cannot record an answer, so an agenda could not close the row"
    try:
        inspect.signature(fn).bind("q", text="t", channel="cockpit")
    except TypeError:
        return "this band's question store takes no text answer on the cockpit channel, so an agenda could not close the row"
    except ValueError:      # no signature to read (a builtin): the call itself decides
        pass
    return None


def question_block(q: Any) -> str | None:
    """The reason code an open question is not offered the verb, read off the question alone (a reminder the host projects
    through ``intention_of`` is the card's ``intention`` facet, judged beside this)."""
    if q is None:
        return BLOCK_UNREADABLE
    if getattr(q, "link", None) is not None or getattr(q, "sign_request", None) is not None:
        return BLOCK_SIGNS
    if valid_intention_id(getattr(q, "intention", None)):
        return BLOCK_RINGS
    return None


def offer_blocks(questions: Any) -> tuple[str | None, dict[str, str]]:
    """For the List: (a code that blocks every band row of this store, else None; the code per open question id that is
    blocked). One read of the band, through the reader the board uses; a read that fails blocks every row."""
    if questions is None:
        return None, {}
    if answer_refusal(questions):
        return BLOCK_NO_ANSWER, {}
    from levain import band_view

    try:
        items, _numbers, _closed = band_view._read(questions)
    except Exception:  # noqa: BLE001 - the band itself reports the read fault; no row is offered a write meanwhile
        return BLOCK_UNREADABLE, {}
    return None, {ref: code for ref, q in items if (code := question_block(q)) is not None}


def offer_block(questions: Any, question_id: str) -> str | None:
    """:func:`offer_blocks` for one row (a write's read of its target)."""
    if answer_refusal(questions):
        return BLOCK_NO_ANSWER
    try:
        return question_block(questions.get(question_id))
    except Exception:  # noqa: BLE001 - a row that cannot be read is not offered a write
        return BLOCK_UNREADABLE


def agenda_origin_key(question_id: str) -> str:
    """The agenda's spore key: ``band-agenda:`` + the sha256 hex of the question id, always inside anneal's key grammar
    (``[0-9A-Za-z._:-]{1,128}``). The day is not in it, so one row has one agenda."""
    return KEY_PREFIX + hashlib.sha256(question_id.encode("utf-8")).hexdigest()


def day_refusal(day: Any, today: date) -> str | None:
    """Why ``day`` cannot be the agenda's day, in the words the operator reads, or None: a real ``YYYY-MM-DD`` strictly after
    the snooze window's last day. A nearer day is a snooze's, and the words say so."""
    if not valid_date(day):
        return f"an agenda's day is written YYYY-MM-DD: {day!r} is not a date"
    latest = today + timedelta(days=SNOOZE_MAX_DAYS)
    if date.fromisoformat(day) <= today:      # a snooze refuses this day too, so the words do not point at one
        return (f"{day} is not after today ({today.isoformat()}): an agenda is for a day after {latest.isoformat()}")
    if date.fromisoformat(day) <= latest:
        return (f"{day} is within the snooze window (a snooze is at most {SNOOZE_MAX_DAYS} days, so {latest.isoformat()} at "
                f"the latest): snooze the row instead; an agenda is for a day after {latest.isoformat()}")
    return None


def deferral_words(day: str, agenda_id: str) -> str:
    return f"deferred: comes back {day} as {agenda_id}; do not proceed before then"


def _plant(store: Any, *, text: str, day: str, question_id: str, today: date) -> dict[str, Any]:
    """Act 1: the one ``add``. Its own seam so a test can fail it; a key already held returns that spore unchanged."""
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


def defer(*, questions: Any, spores_json: Path, ledger_root: Path, question_id: str, day: Any, today: date,
          now: str | None = None) -> dict[str, Any]:
    """Make the open band row ``question_id`` an agenda for ``day``. Refusals raise :class:`levain.writes.EditError` with
    nothing written."""
    from anneal_memory.spores import SporeError

    from levain.cockpit.consent import ConsentError
    from levain.consent.questions import QuestionStoreError, intention_of
    from levain.spores import guarded_spore_store
    from levain.writes import EditError, _audit_verb

    refused = day_refusal(day, today)
    if refused:
        raise EditError("bad_request", 400, refused)
    cannot = answer_refusal(questions)
    if cannot:
        raise EditError("not_built", 501, f"{cannot}: nothing was planted")
    try:
        q = questions.get(question_id)
    except (QuestionStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503, f"the question store cannot be read now: {exc}") from None
    if q is None or q.answer is not None:
        raise EditError("stale", 409, "the row was answered or is gone since it was read")
    if question_block(q) == BLOCK_SIGNS:
        raise EditError("needs_signature", 409, "this row is settled by signing: sign it, or answer it on its own sheet in "
                                                "Decisions; nothing was planted")
    if intention_of(questions, q) is not None:
        raise EditError("rings_a_reminder", 409, "this row rings a reminder: its Snooze moves it any distance, so it needs no agenda")
    if question_block(q) == BLOCK_RINGS:        # a reminder this host offers no controls for: still a reminder (Q1)
        raise EditError("rings_a_reminder", 409, f"this row rings a reminder ({q.intention}): change the reminder where it was "
                                                 "set; the row needs no agenda")
    text = str(q.what or "").strip()
    if not text:
        raise EditError("bad_verb_arg", 422, "an agenda needs the row's words: this row has none")

    key = agenda_origin_key(q.id)
    try:
        store = guarded_spore_store(spores_json)
        created = store.get_by_origin_key(key) is None      # whether THIS call plants it (a crash's retry finds it planted)
        agenda = _plant(store, text=text, day=day, question_id=q.id, today=today)
    except (SporeError, ValueError) as exc:
        raise EditError("verb_failed", 422, f"the agenda could not be planted: {exc}") from None
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from None
    if agenda.get("status") == "resolved":
        raise EditError("agenda_resolved", 409,
                        f"this row's agenda ({agenda.get('id')}) was already resolved, so it cannot wake the row: "
                        "answer the row in words")
    planted = str(agenda.get("next") or day)
    earlier = planted != day                  # the key alone decides: an earlier plant's day stands
    words = deferral_words(planted, str(agenda["id"]))
    warning: str | None = None
    try:
        _answer_deferred(questions, q.id, words)
    except (QuestionStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503,
                        f"agenda {agenda['id']} is planted for {planted} but the row was not answered ({exc}): retry") from None
    except Exception as exc:  # noqa: BLE001 - the host refused act 2 (a ConsentError) or failed in a way a retry repeats
        if isinstance(exc, ConsentError) and "already answered" in str(exc):
            raise EditError("stale", 409, f"the row was answered meanwhile; its agenda {agenda['id']} stands for {planted}") from None
        state = _answered_now(questions, q.id)
        if state is None:
            raise EditError("source_unavailable", 503,
                            f"agenda {agenda['id']} is planted for {planted}; the answer failed ({exc}) and whether the row "
                            "closed cannot be read now: retry") from None
        if state:
            warning = str(exc)          # the host closed the row, then raised: act 2 landed, and its words are kept
        else:
            _abort(store, agenda, created=created, why=str(exc), q_id=q.id, planted=planted, today=today,
                   ledger_root=ledger_root, now=now)
    edit_id = _audit_verb(ledger_root, kind="band_agenda", action="band_agenda", target=f"spore:{agenda['id']}", now=now,
                          extra={"question": q.id, "day": planted, "origin_key": key})
    line = f"-> Later, wakes {planted} · band ({agenda['id']})"
    if earlier:
        line += f"; planted earlier, so that day stands (you asked for {day})"
    return {"ok": True, "id": edit_id, "source": f"spore:{agenda['id']}", "action": "band_agenda", "agenda": agenda["id"],
            "day": planted, "question": q.id, "receipt_line": line, **({"earlier_day_kept": True} if earlier else {}),
            **({"warning": warning} if warning else {})}


def _answered_now(questions: Any, question_id: str) -> bool | None:
    """Whether the row is closed now (a row that is gone counts as not answered), or None when it cannot be read."""
    try:
        q = questions.get(question_id)
    except Exception:  # noqa: BLE001 - unknown: the caller neither composts nor claims the deferral
        return None
    return q is not None and getattr(q, "answer", None) is not None


def _abort(store: Any, agenda: dict[str, Any], *, created: bool, why: str, q_id: str, planted: str, today: date,
           ledger_root: Path, now: str | None) -> None:
    """Act 2 was refused with the row still open. An agenda THIS call planted is composted (a note names the refusal) and the
    aborted attempt gets its audit line; an agenda an earlier call planted stands, as its key's day does. Always raises."""
    from anneal_memory.spores import SporeError, spore_version

    from levain.writes import EditError, _audit_verb

    aid = str(agenda["id"])
    if not created:
        raise EditError("answer_refused", 409, f"the row was not answered ({why}); agenda {aid}, planted by an earlier "
                                               f"attempt, stands for {planted}")
    try:
        cur = store.update(aid, add_note=f"band_agenda aborted: the row {q_id} refused the deferral answer ({why})",
                           expected_version=spore_version(agenda), today=today)
        store.descend(aid, kind="composted", expect_disposition="agenda", expected_version=spore_version(cur), today=today)
    except (SporeError, ValueError, OSError) as exc:
        raise EditError("answer_refused", 409, f"the row was not answered ({why}); the agenda this attempt planted ({aid}) "
                                               f"could not be composted ({exc}): compost it") from None
    _audit_verb(ledger_root, kind="band_agenda", action="band_agenda", target=f"spore:{aid}", now=now,
                extra={"question": q_id, "day": planted, "origin_key": agenda_origin_key(q_id), "outcome": "aborted",
                       "refused": why})
    raise EditError("answer_refused", 409, f"the row was not answered ({why}); the agenda this attempt planted ({aid}) was "
                                           "composted, so nothing remains planted")
