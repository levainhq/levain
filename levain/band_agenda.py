"""levain.band_agenda: THE LIST's "make this an agenda" (row 183; flow D_list_intention_object section 9, Phill 11-10 / 11-11).

A machine-filed band row the operator wants pushed past the snooze window is not a band row: it becomes a dated agenda that
wakes back into the band on its day. Two acts in a fixed order:

1. plant the agenda (the row's words, the day, target ``band``, the question id in its ``pointer``) in ONE anneal ``apply``
   add under a key derived from the question id alone, so a retry lands the same spore whatever day it names, and exactly
   one of two racing calls is told it created it;
2. answer the row with the deferral words, so the stopped seat reads that it must wait.

Then the row is READ BACK, and that read alone decides: only a row that records this call's deferral (text, these words, the
cockpit channel) gets the receipt, the delivery to the stopped agent and the audit line. A row answered otherwise, gone, or
refused while still open gets a refusal, and an agenda THIS call created is composted first (a note names why) unless it was
changed since, which the refusal names; an agenda this call did not create is never composted. Every refusal that can be known
beforehand is raised before act 1: a row settled by signing, a row that rings a reminder, a host that cannot take a text
answer. A crash between the acts (a store error with the row still open) leaves the agenda planted; the retry finds the key,
keeps the day already planted and goes on to act 2. Each act is its own function so a test can fail the seam between them."""

from __future__ import annotations

import hashlib
import inspect
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, NoReturn

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
        return target_block(questions, questions.get(question_id))
    except Exception:  # noqa: BLE001 - a row that cannot be read is not offered a write
        return BLOCK_UNREADABLE


def target_block(questions: Any, q: Any) -> str | None:
    """:func:`question_block` for a row read by ``get``, judged as the List judges it. A host store's ``get`` may project less
    than its ``open_questions`` (flow's ``NeedsQuestions.get`` never carries the ``sign_request`` its ``open_questions``
    attaches), so on a host store a row ``get`` shows unsigned is judged again on its ``open_questions`` entry. A listing that
    cannot be read, or that does not list the row, blocks it (:data:`BLOCK_UNREADABLE`)."""
    code = question_block(q)
    if code is not None:
        return code
    from levain import band_view

    if not band_view._foreign(questions):          # levain's own store: get and the band read share one projection
        return None
    try:
        listed = next((o for o in questions.open_questions() if getattr(o, "id", None) == q.id), None)
    except Exception:  # noqa: BLE001 - the signing facts cannot be read, so the row is not offered a write
        return BLOCK_UNREADABLE
    if listed is None:      # get shows it but the listing omits it: its signing facts are unknown, so it fails closed
        return BLOCK_UNREADABLE
    return question_block(listed)


def agenda_origin_key(question_id: str) -> str:
    """The agenda's spore key: ``band-agenda:`` + the sha256 hex of the question id, always inside anneal's key grammar
    (``[0-9A-Za-z._:-]{1,128}``). The day is not in it, so one row has one agenda."""
    return KEY_PREFIX + hashlib.sha256(question_id.encode("utf-8")).hexdigest()


def day_refusal(day: Any, today: date) -> str | None:
    """Why ``day`` cannot be the agenda's day, in the words the operator reads, or None: a real ``YYYY-MM-DD`` strictly after
    the snooze window's last day. A nearer day is a snooze's, and the words say so."""
    parsed = _day_of(day)
    if parsed is None:
        return f"an agenda's day is written YYYY-MM-DD: {day!r} is not a date"
    today = _date_of(today)
    latest = today + timedelta(days=SNOOZE_MAX_DAYS)
    if parsed <= today:      # a snooze refuses this day too, so the words do not point at one
        return (f"{day} is not after today ({today.isoformat()}): an agenda is for a day after {latest.isoformat()}")
    if parsed <= latest:
        return (f"{day} is within the snooze window (a snooze is at most {SNOOZE_MAX_DAYS} days, so {latest.isoformat()} at "
                f"the latest): snooze the row instead; an agenda is for a day after {latest.isoformat()}")
    return None


def _day_of(day: Any) -> date | None:
    """``day`` parsed once: a ``YYYY-MM-DD`` string that is a real date, else None (never raises)."""
    if not valid_date(day):
        return None
    try:
        return date.fromisoformat(day)
    except (TypeError, ValueError):
        return None


def _date_of(today: date) -> date:
    """The day of ``today``: a ``datetime`` (a ``date`` subclass that refuses to compare with one) is cut to its date."""
    return today.date() if isinstance(today, datetime) else today


def deferral_words(day: str, agenda_id: str) -> str:
    return f"deferred: comes back {day} as {agenda_id}; do not proceed before then"


def _plant(store: Any, *, text: str, day: str, question_id: str, today: date) -> tuple[dict[str, Any], bool, str | None]:
    """Act 1: ONE ``apply`` add under the row's key. anneal holds its lock across the key check and the write, so exactly one
    of two racing calls is told it created the spore. Returns (the spore carrying the key; a key held by a deleted spore is
    ``{"id": ..., "status": "deleted"}``), whether THIS call created it, and the version it was created at (None unless it
    was). A key already held writes nothing. Its own seam so a test can fail it."""
    from anneal_memory.spores import SporeApply

    fields: dict[str, Any] = {"type": AGENDA_TYPE, "text": text, "disposition": "agenda", "next": day, "target": AGENDA_TARGET,
                              "pointer": f"question:{question_id}"}
    got = store.apply(SporeApply(op="add", origin_key=agenda_origin_key(question_id), args={**fields, "today": today},
                                 postcondition=fields))
    created = got.outcome == "applied"
    spore = got.spore if got.spore is not None else {"id": got.spore_id, "status": "deleted"}
    return spore, created, (got.version if created else None)


def _answer_deferred(questions: Any, question_id: str, words: str) -> None:
    """Act 2: record the answer (channel ``cockpit``, unsigned: it authorises nothing). Whether it landed is judged afterwards
    by what the row records (:func:`_row_state`), never by this call returning or by an error's words. Its own seam so a test
    can fail it after the agenda landed."""
    questions.answer(question_id, text=words, channel="cockpit")


def _deliver(questions: Any, question_id: str) -> None:
    """Deliver the recorded answer to the agent's session on a daemon thread, as ``question_answer`` does. A delivery error
    never undoes the answer (``levain question deliver`` retries it), and a second delivery of one answer starts nothing
    (``deliver_answer`` reads the delivery state under its session lock)."""
    from levain.consent import band

    def deliver() -> None:
        try:
            band.deliver_answer(questions, question_id)
        except Exception:  # noqa: BLE001 - the answer stands
            pass

    threading.Thread(target=deliver, name="levain-answer-delivery", daemon=True).start()


# what the row records after act 2 (:func:`_row_state`)
ROW_OURS = "ours"      # answered with exactly this call's deferral: kind text, these words, on the cockpit channel
ROW_OTHER = "other"    # answered with anything else (another surface, other words)
ROW_OPEN = "open"      # still unanswered
ROW_GONE = "gone"      # the store no longer has it


def _row_state(questions: Any, question_id: str, words: str) -> tuple[str | None, Any]:
    """(what the row records now, its answer), or (None, None) when it cannot be read. Only an answer that IS this call's
    deferral (text kind, the same words, the cockpit channel) counts as the deferral landing."""
    try:
        q = questions.get(question_id)
    except Exception:  # noqa: BLE001 - unknown: the caller neither composts nor claims the deferral
        return None, None
    if q is None:
        return ROW_GONE, None
    ans = getattr(q, "answer", None)
    if ans is None:
        return ROW_OPEN, None
    value = getattr(ans, "value", None)
    if (getattr(ans, "kind", None) == "text" and getattr(ans, "channel", None) == "cockpit" and isinstance(value, str)
            and value == words):        # exact: the native store keeps answer text verbatim, so padded words are another answer
        return ROW_OURS, ans
    return ROW_OTHER, ans


def _said(ans: Any) -> str:
    """An answer the operator did not give through this call, in a few words: its kind, channel and the start of its value."""
    value = str(getattr(ans, "value", "") or "")
    short = value if len(value) <= 60 else value[:57] + "..."
    return f"{getattr(ans, 'kind', '?')} on {getattr(ans, 'channel', '?')}: {short!r}"


def defer(*, questions: Any, spores_json: Path, ledger_root: Path, question_id: str, day: Any, today: date,
          now: str | None = None) -> dict[str, Any]:
    """Make the open band row ``question_id`` an agenda for ``day``. Refusals raise :class:`levain.writes.EditError`.

    THE INVARIANT (a consent boundary): the operator is told a deferral landed only when the row, read back after act 2,
    records THIS call's deferral (:data:`ROW_OURS`); an agenda this call planted is composted whenever the row ends answered
    otherwise, gone or refused (unless it was changed since this call planted it, which the refusal names); and an agenda
    this call did not plant (an earlier attempt's, a racing call's) is never composted."""
    from anneal_memory.spores import PostconditionFailed, SporeError

    from levain.consent.questions import QuestionStoreError, intention_of
    from levain.spores import guarded_spore_store
    from levain.writes import EditError, _audit_verb

    today = _date_of(today)
    refused = day_refusal(day, today)
    if refused:
        raise EditError("bad_request", 400, refused)
    cannot = answer_refusal(questions)
    if cannot:
        raise EditError("not_built", 501, f"{cannot}: nothing was planted")
    try:
        q = questions.get(question_id)
    except (QuestionStoreError, OSError, sqlite3.Error, ValueError) as exc:   # flow's reader raises ValueError on a bad line
        raise EditError("source_unavailable", 503, f"the question store cannot be read now: {exc}") from None
    if q is None or getattr(q, "answer", None) is not None:
        raise EditError("stale", 409, "the row was answered or is gone since it was read")
    if not isinstance(getattr(q, "id", None), str) or not q.id:
        raise EditError("source_unavailable", 503, "the row could not be read as a question (it has no id): nothing was planted")
    block = target_block(questions, q)
    if block == BLOCK_UNREADABLE:
        raise EditError("source_unavailable", 503, "the row's signing facts cannot be read now (the band listing cannot be read, "
                                                   "or does not list this row): nothing was planted")
    if block == BLOCK_SIGNS:
        raise EditError("needs_signature", 409, "this row is settled by signing: sign it, or answer it on its own sheet in "
                                                "Decisions; nothing was planted")
    if intention_of(questions, q) is not None:
        raise EditError("rings_a_reminder", 409, "this row rings a reminder: its Snooze moves it any distance, so it needs no agenda")
    if block == BLOCK_RINGS:        # a reminder this host offers no controls for: still a reminder (Q1)
        raise EditError("rings_a_reminder", 409, f"this row rings a reminder ({getattr(q, 'intention', None)}): change the "
                                                 "reminder where it was set; the row needs no agenda")
    text = str(getattr(q, "what", None) or "").strip()
    if not text:
        raise EditError("bad_verb_arg", 422, "an agenda needs the row's words: this row has none")

    key = agenda_origin_key(q.id)
    try:
        store = guarded_spore_store(spores_json)
        agenda, created, version = _plant(store, text=text, day=day, question_id=q.id, today=today)
    except (SporeError, ValueError, PostconditionFailed) as exc:
        raise EditError("verb_failed", 422, f"the agenda could not be planted: {exc}") from None
    except OSError as exc:
        raise EditError("store_unavailable", 503, f"spore store unavailable: {exc}") from None
    aid = str(agenda.get("id"))
    if agenda.get("status") != "open":          # composted, done, dropped or deleted: it can never wake the row
        raise EditError("agenda_resolved", 409,
                        f"this row's agenda ({aid}) is {agenda.get('status')}, so it cannot wake the row: answer the row in "
                        "words")
    planted = str(agenda.get("next") or day)
    earlier = planted != day                  # the key alone decides: an earlier plant's day stands
    words = deferral_words(planted, aid)
    raised: Exception | None = None
    try:
        _answer_deferred(questions, q.id, words)
    except Exception as exc:  # noqa: BLE001 - judged below by what the row records, never by the words
        raised = exc
    state, found = _row_state(questions, q.id, words)
    after = _After(store=store, key=key, aid=aid, created=created, version=version, q_id=q.id, planted=planted, today=today,
                   ledger_root=ledger_root, now=now)
    if state is None:
        if raised is None:      # the host took the answer; the read-back failed, so the deferral is not claimed
            after.audit("unconfirmed", None)
            raise EditError("source_unavailable", 503,
                            f"agenda {aid} is planted for {planted} and the host took the deferral answer, but the row could "
                            "not be read back to confirm it: reload the List (if the row is gone, the deferral landed)")
        raise EditError("source_unavailable", 503,
                        f"agenda {aid} is planted for {planted}; the answer failed ({raised}) and whether the row closed "
                        "cannot be read now: retry") from None
    if state in (ROW_OTHER, ROW_GONE):
        why = (f"the row was answered otherwise ({_said(found)})" if state == ROW_OTHER else "the row is gone")
        after.refuse("stale", f"{why}, so this deferral did not land", why)
    if state == ROW_OPEN:
        if isinstance(raised, (QuestionStoreError, OSError, sqlite3.Error)):     # a crash's shape: the retry finishes it
            raise EditError("source_unavailable", 503,
                            f"agenda {aid} is planted for {planted} but the row was not answered ({raised}): retry") from None
        why = str(raised) if raised is not None else "the host returned, but the row is still open"
        after.refuse("answer_refused", f"the row was not answered ({why})", why)
    _deliver(questions, q.id)                 # ROW_OURS: the deferral landed (its host may still have raised after it)
    warnings: list[str] = []
    if raised is not None:
        warnings.append(f"the host raised after recording this deferral ({raised}); delivery to the agent was started")
    try:            # the deferral landed and delivery started: a lost audit line is named, never a 500 in place of the receipt
        edit_id = _audit_verb(ledger_root, kind="band_agenda", action="band_agenda", target=f"spore:{aid}", now=now,
                              extra={"question": q.id, "day": planted, "origin_key": key})
    except Exception as exc:  # noqa: BLE001
        edit_id = ""
        warnings.append(f"the audit line could not be written ({type(exc).__name__}: {exc})")
    line = f"-> Later, wakes {planted} · band ({aid})"
    if earlier:
        line += f"; planted earlier, so that day stands (you asked for {day})"
    warning = "; ".join(warnings) or None
    return {"ok": True, "id": edit_id, "source": f"spore:{aid}", "action": "band_agenda", "agenda": aid,
            "day": planted, "question": q.id, "receipt_line": line, **({"earlier_day_kept": True} if earlier else {}),
            **({"warning": warning} if warning else {})}


class _After:
    """Act 2 did not land this call's deferral: undo what this call planted, record the attempt, and refuse in words."""

    def __init__(self, *, store: Any, key: str, aid: str, created: bool, version: str | None, q_id: str, planted: str,
                 today: date, ledger_root: Path, now: str | None) -> None:
        self.store, self.key, self.aid, self.created, self.version = store, key, aid, created, version
        self.q_id, self.planted, self.today, self.ledger_root, self.now = q_id, planted, today, ledger_root, now

    def audit(self, outcome: str, why: str | None) -> str | None:
        """The attempt's audit line; a failure to write it is returned in words and never masks what was done."""
        from levain.writes import _audit_verb

        try:
            _audit_verb(self.ledger_root, kind="band_agenda", action="band_agenda", target=f"spore:{self.aid}", now=self.now,
                        extra={"question": self.q_id, "day": self.planted, "origin_key": self.key, "outcome": outcome,
                               **({"refused": why} if why is not None else {})})
        except Exception as exc:  # noqa: BLE001 - the refusal below still says what happened to the agenda
            return f"{type(exc).__name__}: {exc}"
        return None

    def refuse(self, code: str, head: str, why: str) -> NoReturn:
        """Compost the agenda when this call planted it and nobody changed it since, then raise ``code`` with ``head`` and
        what became of the agenda. An agenda this call did not plant is named and left standing. Always raises."""
        from levain.writes import EditError

        if not self.created:
            tail = f"agenda {self.aid}, planted by an earlier attempt, stands for {self.planted}"
            if code == "stale":
                tail += ": compost it if it is not wanted"
            raise EditError(code, 409, f"{head}; {tail}")
        try:        # whatever the store raises, the refusal and its audit line still go out
            fate, detail = _compensate(self.store, self.key, self.version,
                                       f"band_agenda aborted: the row {self.q_id} did not take the deferral answer ({why})",
                                       self.today)
        except Exception as exc:  # noqa: BLE001
            fate, detail = "failed", f"{type(exc).__name__}: {exc}"
        mine = f"the agenda this attempt planted ({self.aid})"
        tail = {
            "composted": f"{mine} was composted, so nothing remains planted",
            "already": f"{mine} is already composted, so nothing remains planted",
            "deleted": f"{mine} was deleted meanwhile, so nothing remains planted",
            "resolved": f"{mine} was resolved meanwhile ({detail}), so it cannot wake the row",
            "touched": f"{mine} was changed since this attempt planted it, so it was left standing for {self.planted}: "
                       "compost it if it is not wanted",
            "failed": f"{mine} could not be composted ({detail}): compost it",
        }[fate]
        lost = self.audit("aborted", why)
        if lost:
            tail += f" (the audit line could not be written: {lost})"
        raise EditError(code, 409, f"{head}; {tail}")


def _compensate(store: Any, key: str, version: str | None, note: str, today: date) -> tuple[str, str]:
    """Compost the spore carrying ``key`` only while it is open at ``version`` (the version this call created it at): a note
    names the refusal, then one versioned descend. Returns (fate, detail): ``composted``, ``already`` (composted by someone
    else), ``deleted``, ``resolved`` (detail: the kind), ``touched`` (open at another version: never composted) or ``failed``
    (detail: the error). Every write is compare-and-set on the version, so a spore another writer moved is never composted."""
    from anneal_memory.spores import PostconditionFailed, SporeApply, SporeError, spore_version

    def read(expect: str | None) -> tuple[tuple[str, str] | None, Any]:
        """(the fate, when the spore is not open at ``expect``; the spore read)."""
        cur = store.get_by_origin_key(key)
        if cur is None:
            return ("deleted", ""), None
        if cur.get("status") != "open":
            kind = str((cur.get("resolution") or {}).get("kind") or cur.get("status"))
            return (("already", "") if kind == "composted" else ("resolved", kind)), cur
        if expect is None or spore_version(cur) != expect:
            return ("touched", ""), cur
        return None, cur

    def where(expect: str | None) -> tuple[str, str] | None:
        return read(expect)[0]

    expect = version
    try:
        found, cur = read(expect)       # ONE read: the versioned update below catches any change after it
        if found is not None:
            return found
        noted = store.update(cur["id"], add_note=note, expected_version=expect, today=today)
        expect = spore_version(noted)
        got = store.apply(SporeApply(op="descend", origin_key=key, spore_id=cur["id"],
                                     args={"kind": "composted", "expect_disposition": "agenda", "today": today},
                                     expected_version=expect,
                                     postcondition={"status": "resolved", ("resolution", "kind"): "composted"}))
    except (SporeError, ValueError, OSError, PostconditionFailed) as exc:   # a CAS that lost a race: say what is there now
        try:
            found = where(expect)
        except Exception:  # noqa: BLE001 - the store cannot be read either: the first error is the one named
            found = None
        return found if found is not None else ("failed", str(exc))
    if got.outcome == "applied":
        return "composted", ""
    if got.outcome == "already":
        return "already", ""
    try:                                                  # precondition_lost: moved between the note and the descend
        found = where(expect)
    except Exception as exc:  # noqa: BLE001
        return "failed", str(exc)
    return found if found is not None else ("failed", "the descend found its precondition lost")

