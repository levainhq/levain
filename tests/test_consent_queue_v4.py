"""The consent queue's /4 additions (Q14 B1): the ``undecided`` state and its gap numbers, the late ruling (its own
record and namespace), the consumed-once ruled allow, snoozes, session halts, and the in-place migration of a /3
store.

The /3 store is built by the /3 code itself: ``tests/fixtures/consent_queue_v3_d430b804.py`` is
``git show d430b804:levain/consent/queue.py`` saved unchanged, loaded here as its own module.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sqlite3
import sys
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from levain.authority import ReplayCache
from levain.cockpit.consent import utc_stamp
from levain.consent import ANSWER_NAMESPACE, AnswerRefused, ConsentQueue, QueueStoreError, answer_message
from levain.consent.listing import read_listing
from levain.consent.queue import (
    PRIOR_FORMAT, RULED_ALLOW_LAPSE, RULING_SKEW, SNOOZE_MAX_MINUTES, STORE_FORMAT, UNDECIDED_GRACE, Decision,
    Ruling,
)
from levain.consent.receipt_log import verify as verify_receipts
from levain.consent.record import (RULING_FORMAT, RULING_FORMAT_UNCHAINED, RULING_NAMESPACE, ruling_message,
                                   ruling_payload)
from tests.test_consent_queue import NOW, Clock, FakeAgent, Log, _raw, signed
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures
from tests._h4 import h4_home

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "consent_queue_v3_d430b804.py"
CWD = "/work/proj"


def ruled(q, request_id, key, decision="allow", **override):
    payload = q.ruling_payload(request_id, decision, key.fp)
    payload.update(override)
    return payload, key.sign(ruling_message(payload), RULING_NAMESPACE)


def ask(q, command="touch /tmp/x", *, session="sess-1", agent="claude_code", tier="T2",
        hold=timedelta(minutes=5), tool_input=None):
    return q.file(agent=agent, agent_session_id=session, tool_name="Edit",
                  tool_input=tool_input if tool_input is not None else {"command": command}, cwd="/work/proj",
                  hold=hold, tier=tier)


def undecided(q, *a, **kw):
    request = ask(q, *a, **kw)
    marked = q.mark_undecided(request.id)
    assert marked.state == "undecided" and isinstance(marked.gap, int)
    return request, marked.gap


def _own_log(tmp_path, keys, *more):
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    for name, cls in more:
        log.enrol(name, cls)
    return log


# --- undecided and gap numbers ------------------------------------------------------------------------------

def test_the_deadline_moves_a_pending_request_to_undecided_with_gap_and_receipt(queue, keys):
    request = ask(queue)
    marked = queue.mark_undecided(request.id)
    assert (marked.state, marked.gap, marked.halted, marked.decision) == ("undecided", 1, False, None)
    assert queue.get(request.id)[1] == "undecided"
    assert queue.gap(request.id) == 1
    assert queue.receipt(request.id)["state"] == "undecided"
    assert queue.receipt(request.id)["face"]["terminal_state"] == "timed_out"
    assert queue.decision(request.id) is None
    assert queue.await_decision(request.id, 0.05, poll=0.01) is None
    again = queue.mark_undecided(request.id)               # a retry of the hook: the same gap, nothing new
    assert (again.state, again.gap, again.reason) == ("undecided", 1, "the request was already undecided")
    assert len(queue.receipts(request.id)) == 1
    payload, sig = signed(queue, request, keys["phone"])
    with pytest.raises(AnswerRefused, match="already undecided"):
        queue.answer(request.id, payload, sig)             # an answer is for a pending request only
    assert request not in queue.pending()


def test_gap_numbers_count_per_agent_and_session(queue):
    a1, g1 = undecided(queue, "a1")
    a2, g2 = undecided(queue, "a2")
    b1, h1 = undecided(queue, "b1", session="sess-2")
    c1, k1 = undecided(queue, "c1", agent="codex")         # the same session id under another agent
    a3, g3 = undecided(queue, "a3")
    assert (g1, g2, g3, h1, k1) == (1, 2, 3, 1, 1)


def test_gap_numbers_are_unique_under_two_connections(tmp_path, authority):
    """Two queue objects (two connections, as two hook processes) mark one session's requests at once."""
    d = tmp_path / "consent"
    q1, q2 = ConsentQueue(d, authority=authority, home=h4_home()), ConsentQueue(d, authority=authority, home=h4_home())
    requests = [ask(q1, f"c{i}", hold=timedelta(hours=1)) for i in range(24)]
    got: list[int] = []
    lock = threading.Lock()

    def mark(q, chunk):
        for r in chunk:
            g = q.mark_undecided(r.id).gap
            with lock:
                got.append(g)

    threads = [threading.Thread(target=mark, args=(q, requests[i::2])) for i, q in enumerate((q1, q2))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == list(range(1, 25))


def test_a_request_decided_before_the_deadline_returns_its_decision(queue, keys):
    request = ask(queue)
    payload, sig = signed(queue, request, keys["phone"], "deny")
    queue.answer(request.id, payload, sig)
    got = queue.mark_undecided(request.id)
    assert got.state == "denied" and got.gap is None and "answered deny" in got.reason
    assert got.decision.decision == "deny" and got.decision.via == "signature"
    assert queue.get(request.id)[1] == "denied" and queue.gap(request.id) is None


def test_the_hook_marks_undecided_at_the_real_deadline(queue, clock):
    """L3 r1 codex MED: at and after expires_at (the hold's end) the hook's mark wins; no answer applies then."""
    at = ask(queue, "at", hold=timedelta(seconds=30))
    past = ask(queue, "past", hold=timedelta(seconds=30))
    clock.now = NOW + timedelta(seconds=30)                                # exactly the deadline
    assert queue.get(at.id)[1] == "pending" and queue.await_decision(at.id, 5, poll=0.01) is None
    assert queue.mark_undecided(at.id).state == "undecided"
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE * 2          # no sweep ran: still the hook's
    assert queue.mark_undecided(past.id).state == "undecided"


def test_a_sweeper_racing_the_hook_at_the_deadline_expires_nothing(queue, clock):
    request = ask(queue, hold=timedelta(seconds=30))
    clock.now = NOW + timedelta(seconds=31)
    assert queue.sweep() == 0 and queue.pending() == [request]
    got = queue.mark_undecided(request.id, halt=True)
    assert (got.state, got.gap, got.halted) == ("undecided", 1, True)
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE
    assert queue.sweep() == 0 and queue.get(request.id)[1] == "undecided"


def test_an_expired_or_withdrawn_request_is_never_undecided(queue, clock):
    late = ask(queue, "late", hold=timedelta(seconds=30))
    gone = ask(queue, "gone")
    queue.withdraw(gone.id)
    clock.now = NOW + timedelta(seconds=30) + UNDECIDED_GRACE   # unmarked past the grace: a sweep expires it
    assert queue.sweep() == 1
    got = queue.mark_undecided(late.id, halt=True)
    assert (got.state, got.gap, got.halted, got.decision) == ("expired", None, False, None)
    assert "expired" in got.reason
    assert queue.get(late.id)[1] == "expired"
    got = queue.mark_undecided(gone.id)
    assert (got.state, got.gap) == ("withdrawn", None) and "withdrawn" in got.reason
    got = queue.mark_undecided("no-such-request")
    assert (got.state, got.gap) == (None, None) and "no sealed request" in got.reason


# --- the trigger refuses every other move ----------------------------------------------------------------------

def _refused(q, sql, args=()):
    with pytest.raises(sqlite3.IntegrityError):
        _raw(q, (sql, args))


@pytest.mark.parametrize("sql", [
    "UPDATE requests SET state = 'undecided', via = 'ruling' WHERE id = ?",
    "UPDATE requests SET state = 'undecided', via = 'signature' WHERE id = ?",
    "UPDATE requests SET state = 'undecided', answer = '{}' WHERE id = ?",
    "UPDATE requests SET state = 'undecided', signer_fp = 'SHA256:x' WHERE id = ?",
    "UPDATE requests SET state = 'allowed', via = 'ruling' WHERE id = ?",
    "UPDATE requests SET state = 'denied', via = 'ruling' WHERE id = ?",
    "UPDATE requests SET state = 'pending' WHERE id = ?",
])
def test_the_trigger_refuses_bad_moves_out_of_pending(queue, sql):
    request = ask(queue)
    _refused(queue, sql, (request.id,))
    assert queue.get(request.id)[1] == "pending"


@pytest.mark.parametrize("sql", [
    "UPDATE requests SET state = 'expired' WHERE id = ?",
    "UPDATE requests SET state = 'withdrawn' WHERE id = ?",
    "UPDATE requests SET state = 'pending' WHERE id = ?",
    "UPDATE requests SET note = 'edited' WHERE id = ?",               # undecided -> undecided
    "UPDATE requests SET state = 'allowed', via = 'signature' WHERE id = ?",
    "UPDATE requests SET state = 'allowed', via = 'grant' WHERE id = ?",
    "UPDATE requests SET state = 'allowed', via = NULL WHERE id = ?",
    "UPDATE requests SET state = 'denied' WHERE id = ?",
    "UPDATE requests SET state = 'allowed', via = 'ruling', tool_name = 'Bash' WHERE id = ?",
    "DELETE FROM requests WHERE id = ?",
    "DELETE FROM gaps WHERE request_id = ?",
    "UPDATE gaps SET gap = 9 WHERE request_id = ?",
    "INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, 'undecided', 'x', '{}')",
])
def test_the_trigger_refuses_every_move_out_of_undecided_but_a_ruling(queue, sql):
    request, _ = undecided(queue)
    _refused(queue, sql, (request.id,))
    assert queue.get(request.id)[1] == "undecided" and queue.gap(request.id) == 1


def test_a_raw_ruling_move_is_allowed_by_the_trigger_and_delivers_nothing(queue):
    """The trigger guards the shape of a move; authority is the signature: a raw undecided -> allowed by 'ruling'
    with no verifying ruling is no decision and no take."""
    request, _ = undecided(queue)
    _raw(queue, ("UPDATE requests SET state = 'allowed', via = 'ruling', decided_at = ? WHERE id = ?",
                 (utc_stamp(NOW), request.id)))
    assert queue.decision(request.id) is None
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd=CWD) is None


def test_a_ruled_request_is_terminal(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"])
    queue.answer_late(request.id, payload, sig)
    for sql in ("UPDATE requests SET state = 'denied' WHERE id = ?",
                "UPDATE requests SET state = 'undecided', via = NULL, answer = NULL, signature = NULL, "
                "signer_fp = NULL WHERE id = ?",
                "UPDATE requests SET state = 'allowed', via = 'ruling', note = 'again' WHERE id = ?"):
        _refused(queue, sql, (request.id,))
    with pytest.raises(AnswerRefused, match="allowed, not undecided"):
        queue.answer_late(request.id, payload, sig)


def test_a_terminal_request_never_becomes_undecided(queue, keys):
    request = ask(queue)
    queue.withdraw(request.id)
    _refused(queue, "UPDATE requests SET state = 'undecided' WHERE id = ?", (request.id,))


# --- the late ruling -------------------------------------------------------------------------------------------

def test_a_signed_ruling_decides_an_undecided_request(queue, keys):
    request, gap = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"])
    assert payload["format"] == RULING_FORMAT and payload["gap"] == gap
    d = queue.answer_late(request.id, payload, sig)
    assert isinstance(d, Ruling) and not isinstance(d, Decision)    # information, never a deliverable decision
    assert (d.ruling, d.gap, d.signer_fp, d.taken) == ("allow", gap, keys["phone"].fp, False)
    again = queue.decision(request.id)
    assert isinstance(again, Ruling) and (again.ruling, again.gap) == ("allow", gap)
    assert [r["state"] for r in queue.receipts(request.id)] == ["undecided", "allowed"]
    assert f"gap=#{gap}" in queue.receipt(request.id)["face"]["authority_scope"]["grant"]


def test_a_ruling_deny(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["presence"], "deny")   # a deny needs only an enrolled key
    assert queue.answer_late(request.id, payload, sig).ruling == "deny"
    assert queue.decision(request.id).ruling == "deny"


def test_a_ruling_is_only_for_an_undecided_request(queue, keys):
    request = ask(queue)
    with pytest.raises(AnswerRefused, match="pending, not undecided"):
        queue.ruling_payload(request.id, "allow", keys["phone"].fp)
    other, _ = undecided(queue, "other")
    payload, sig = ruled(queue, other.id, keys["phone"])
    with pytest.raises(AnswerRefused, match="pending, not undecided"):
        queue.answer_late(request.id, payload, sig)
    with pytest.raises(AnswerRefused, match="no sealed request"):
        queue.answer_late("nope", payload, sig)


def test_an_answer_signature_never_verifies_as_a_ruling(queue, keys):
    """The ruling's own bytes signed in the ANSWER namespace are no ruling; an answer record is no ruling."""
    request, _ = undecided(queue)
    payload = queue.ruling_payload(request.id, "allow", keys["phone"].fp)
    with pytest.raises(AnswerRefused, match="ruling's signature does not verify"):
        queue.answer_late(request.id, payload, keys["phone"].sign(ruling_message(payload), ANSWER_NAMESPACE))
    pending = ask(queue, "pending")
    answer, answer_sig = signed(queue, pending, keys["phone"])
    with pytest.raises(AnswerRefused, match="not a ruling record"):
        queue.answer_late(request.id, answer, answer_sig)
    assert queue.get(request.id)[1] == "undecided"


def test_a_ruling_signature_never_verifies_as_an_answer(queue, keys):
    """An answer's own bytes signed in the RULING namespace are no answer; a ruling record is no answer."""
    request = ask(queue)
    payload, _ = signed(queue, request, keys["phone"])
    with pytest.raises(AnswerRefused, match="answer's signature does not verify"):
        queue.answer(request.id, payload, keys["phone"].sign(answer_message(payload), RULING_NAMESPACE))
    other, _ = undecided(queue, "other")
    ruling, ruling_sig = ruled(queue, other.id, keys["phone"])
    with pytest.raises(AnswerRefused, match="not an answer record"):
        queue.answer(request.id, ruling, ruling_sig)
    assert queue.get(request.id)[1] == "pending"


@pytest.mark.parametrize("override, why", [
    ({"gap": 2}, "not this request's gap"),
    ({"gap": True}, "not this request's gap"),
    ({"request_digest": "0" * 64}, "another version"),
    ({"queue_id": "cq-other"}, "another queue"),
    ({"signed_at": utc_stamp(NOW - timedelta(seconds=1))}, "before the request went undecided"),
    ({"format": "levain-consent-ruling/3"}, "ruling format"),
    ({"decision": "maybe"}, "is not one of"),
])
def test_a_ruling_binds_its_request_gap_and_time(queue, keys, override, why):
    request, _ = undecided(queue)
    payload = {**queue.ruling_payload(request.id, "allow", keys["phone"].fp), **override}
    sig = keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE)
    with pytest.raises(AnswerRefused, match=why):
        queue.answer_late(request.id, payload, sig)


def test_a_ruling_for_another_request_is_refused(queue, keys):
    a, _ = undecided(queue, "a")
    b, _ = undecided(queue, "b")
    payload, sig = ruled(queue, a.id, keys["phone"])
    with pytest.raises(AnswerRefused, match="another request"):
        queue.answer_late(b.id, payload, sig)


def test_an_agent_config_ruling_allow_needs_the_laptop_key(queue, keys):
    """T3 is deny-only for a phone-class key, as an answer treats it."""
    request, _ = undecided(queue, tier="T3")
    payload, sig = ruled(queue, request.id, keys["phone"])
    with pytest.raises(AnswerRefused, match="does not cover T3"):
        queue.answer_late(request.id, payload, sig)
    payload, sig = ruled(queue, request.id, keys["phone"], "deny")
    assert queue.answer_late(request.id, payload, sig).ruling == "deny"
    other, _ = undecided(queue, "other", tier="T3")
    payload, sig = ruled(queue, other.id, keys["laptop"])
    assert queue.answer_late(other.id, payload, sig).ruling == "allow"


def test_unknown_and_revoked_signers_cannot_rule(queue, keys):
    request, _ = undecided(queue)
    for name, why in (("stranger", "not an enrolled key"), ("revoked", "revoked")):
        payload, sig = ruled(queue, request.id, keys[name])
        with pytest.raises(AnswerRefused, match=why):
            queue.answer_late(request.id, payload, sig)


def test_a_signer_revoked_after_ruling_delivers_nothing(tmp_path, keys, clock):
    log = _own_log(tmp_path, keys)
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request, _ = undecided(q)
    payload, sig = ruled(q, request.id, keys["phone"])
    assert q.answer_late(request.id, payload, sig).ruling == "allow"
    assert q.decision(request.id).ruling == "allow"
    log.revoke("phone")
    assert q.decision(request.id) is None
    assert q.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd=CWD) is None


def test_a_ruling_relabelled_by_a_raw_writer_delivers_nothing(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"], "deny")
    queue.answer_late(request.id, payload, sig)
    _raw(queue, ("DROP TRIGGER requests_frozen", ()),
         ("UPDATE requests SET state = 'allowed' WHERE id = ?", (request.id,)))
    assert queue.decision(request.id) is None
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd=CWD) is None


def test_the_record_helpers_refuse_a_bad_gap():
    from levain.cockpit.consent import ConsentError
    from levain.consent.record import ExternalGateRequest
    r = ExternalGateRequest.create(agent="claude_code", agent_session_id="s", event_id="rand:1", tool_name="Edit",
                                   tool_input_digest="jcs:x", display="d", cwd="/", tier="T2",
                                   received_at=utc_stamp(NOW), expires_at=utc_stamp(NOW + timedelta(minutes=1)))
    for gap in (0, True, "1", 1.0):
        with pytest.raises(ConsentError):
            ruling_payload(queue_id="cq-1", request=r, gap=gap, decision="allow", signer_fp="fp",
                           signed_at=utc_stamp(NOW), receipts_head={"size": 0, "root": hashlib.sha256().hexdigest()})


# --- a ruled allow pre-authorises the call once ----------------------------------------------------------------

def _unchained_ruling(q, request_id, key):
    payload = {k: v for k, v in q.ruling_payload(request_id, "allow", key.fp).items() if k != "receipts_head"}
    payload["format"] = RULING_FORMAT_UNCHAINED
    return payload, key.sign(ruling_message(payload), RULING_NAMESPACE)


def test_a_ruling_applied_before_the_chaining_is_still_taken_but_one_is_never_applied_now(queue, keys,
                                                                                          monkeypatch):
    # Q11 head call 1: the head is an APPLY-time rule. A ruled allow applied by the code before Q11 (a /1 ruling,
    # its 24 h lapse not yet run) is still taken once, its signature re-verified; a /1 ruling is refused if applied
    request, gap = undecided(queue)
    payload, sig = _unchained_ruling(queue, request.id, keys["phone"])
    with pytest.raises(AnswerRefused, match="signed before the queue's receipts were chained.*rule on the request "
                                            "again"):
        queue.answer_late(request.id, payload, sig)
    assert queue.get(request.id)[1] == "undecided"
    # the code before Q11 applied it: no format check of the head, no log check
    import levain.consent.queue as qmod
    real = qmod.ConsentQueue._ruling_refusal
    monkeypatch.setattr(qmod.ConsentQueue, "_ruling_refusal",
                        lambda self, *a, **kw: real(self, *a, **{**kw, "unchained_ok": True}))
    monkeypatch.setattr(qmod, "head_refusal", lambda *a, **kw: None)
    monkeypatch.setattr(qmod, "_detached", lambda payload: payload)      # nor copied the payload (Old stays Old)

    class Old(dict):                       # the old apply never read a head; the record stored is the /1 one
        def __missing__(self, key):
            return None

    queue.answer_late(request.id, Old(payload), sig)
    monkeypatch.undo()
    info = queue.decision(request.id)
    assert isinstance(info, Ruling) and (info.ruling, info.taken) == ("allow", False)
    got = queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd=CWD)
    assert isinstance(got, Decision) and (got.request_id, got.gap) == (request.id, gap)
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd=CWD) is None
    report = verify_receipts(queue.directory, authority_for=lambda r: queue._authority)
    assert report.ok and report.unchained == (request.id,) and report.last_signed is None



def test_a_ruled_allow_is_taken_once(queue, keys):
    request, gap = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"])
    queue.answer_late(request.id, payload, sig)
    args = ("claude_code", "sess-1", "Edit")
    x = {"command": "touch /tmp/x"}
    assert queue.take_ruled_allow(*args, {"command": "touch /tmp/other"}, cwd=CWD) is None     # another digest
    assert queue.take_ruled_allow("claude_code", "sess-2", "Edit", x, cwd=CWD) is None
    assert queue.take_ruled_allow("claude_code", "sess-1", "Write", x, cwd=CWD) is None
    assert queue.decision(request.id).taken is False
    # the working directory is shown, never keyed: a resumed session starts in its project directory
    got = queue.take_ruled_allow(*args, x, cwd="/elsewhere", event="hook:tu-1")
    assert isinstance(got, Decision)
    assert (got.request_id, got.decision, got.via, got.gap) == (request.id, "allow", "ruling", gap)
    assert queue.take_ruled_allow(*args, x, cwd=CWD) is None          # consumed
    info = queue.decision(request.id)                       # the request row itself is unchanged
    assert isinstance(info, Ruling) and (info.ruling, info.taken) == ("allow", True)
    _refused(queue, "DELETE FROM ruled_takes WHERE request_id = ?", (request.id,))
    _refused(queue, "UPDATE ruled_takes SET event = 'x' WHERE request_id = ?", (request.id,))


def test_two_ruled_allows_on_one_call_give_one_run(queue, keys, clock):
    """R-j: only the latest ruling on an identical call counts; the earlier untaken allow is void."""
    a, _ = undecided(queue)
    b, _ = undecided(queue)                                 # two identical calls, both ruled allow
    for r in (a, b):
        clock.now += timedelta(seconds=1)
        payload, sig = ruled(queue, r.id, keys["phone"])
        queue.answer_late(r.id, payload, sig)
    args = ("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"})
    assert queue.take_ruled_allow(*args, cwd=CWD).request_id == b.id
    assert queue.take_ruled_allow(*args, cwd=CWD) is None


def test_a_ruled_deny_pre_authorises_nothing(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"], "deny")
    queue.answer_late(request.id, payload, sig)
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}, cwd=CWD) is None


def test_a_ruled_allow_lapses_after_its_window(queue, keys, clock):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"])
    queue.answer_late(request.id, payload, sig)
    args = ("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"})
    clock.now = NOW + RULED_ALLOW_LAPSE
    assert queue.take_ruled_allow(*args, cwd=CWD) is None
    clock.now = NOW + RULED_ALLOW_LAPSE - timedelta(seconds=1)
    assert queue.take_ruled_allow(*args, cwd=CWD).request_id == request.id


# --- snooze ----------------------------------------------------------------------------------------------------

def test_snooze_records_when_to_ask_again(queue, clock):
    p = ask(queue, "p", hold=timedelta(hours=2))
    u, _ = undecided(queue, "u")
    done = ask(queue, "done")
    queue.withdraw(done.id)
    assert queue.snooze(p.id) == utc_stamp(NOW + timedelta(minutes=30))
    assert queue.snooze(u.id, minutes=10) == utc_stamp(NOW + timedelta(minutes=10))
    assert queue.snooze(done.id) is None
    assert queue.claim_repings() == []
    clock.now = NOW + timedelta(minutes=10)
    first = queue.claim_repings()
    assert [(r.request_id, r.state) for r in first] == [(u.id, "undecided")]
    assert queue.claim_repings() == []                     # claimed once
    assert [r.request_id for r in queue.claim_repings(NOW + timedelta(minutes=30))] == [p.id]
    queue.snooze(p.id, minutes=60)                         # a later snooze replaces the earlier one
    assert queue.claim_repings(NOW + timedelta(minutes=30)) == []
    assert [r.request_id for r in queue.claim_repings(NOW + timedelta(minutes=70))] == [p.id]   # snoozed at +10
    _refused(queue, "DELETE FROM snoozes WHERE request_id = ?", (p.id,))
    _refused(queue, "UPDATE repings SET repinged_at = 'x' WHERE snooze_seq = ?", (first[0].seq,))


def test_a_snooze_of_a_request_that_expired_or_was_ruled_is_not_due(queue, clock, keys):
    p = ask(queue, "p", hold=timedelta(minutes=20))
    u, _ = undecided(queue, "u")
    queue.snooze(p.id)
    queue.snooze(u.id)
    payload, sig = ruled(queue, u.id, keys["phone"])
    queue.answer_late(u.id, payload, sig)
    assert queue.claim_repings(NOW + timedelta(minutes=31)) == []


@pytest.mark.parametrize("minutes", [0, -5, True, float("nan"), float("inf"), "30", 1e12, 1e300,
                                     SNOOZE_MAX_MINUTES + 1])
def test_snooze_minutes_must_be_positive(queue, minutes):
    from levain.cockpit.consent import ConsentError
    request = ask(queue)
    with pytest.raises(ConsentError):
        queue.snooze(request.id, minutes=minutes)


# --- halts -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("decision", ["allow", "deny"])
def test_a_halt_holds_the_session_until_the_ruling_releases_it(queue, keys, decision):
    request, gap = undecided(queue, tier="T3")
    assert queue.is_halted("claude_code", "sess-1") is None
    assert queue.halt(request.id) == gap
    assert queue.halt(request.id) == gap                   # once
    assert queue.is_halted("claude_code", "sess-1") == gap
    assert queue.is_halted("claude_code", "sess-2") is None
    assert queue.is_halted("codex", "sess-1") is None
    payload, sig = ruled(queue, request.id, keys["laptop"], decision)
    queue.answer_late(request.id, payload, sig)
    assert queue.is_halted("claude_code", "sess-1") is None
    assert queue.halt(request.id) is None                  # a ruled request is never halted again


def test_halts_name_the_lowest_unreleased_gap_and_only_undecided_requests_halt(queue, keys):
    pending = ask(queue, "pending")
    assert queue.halt(pending.id) is None
    a, ga = undecided(queue, "a")
    b, gb = undecided(queue, "b")
    queue.halt(a.id)
    queue.halt(b.id)
    other, go = undecided(queue, "o", session="sess-2")
    assert queue.is_halted("claude_code", "sess-1") == ga
    payload, sig = ruled(queue, a.id, keys["phone"], "deny")
    queue.answer_late(a.id, payload, sig)
    assert queue.is_halted("claude_code", "sess-1") == gb
    assert queue.is_halted("claude_code", "sess-2") is None    # never halted
    assert go == 1


def test_a_refused_ruling_releases_nothing_and_halts_are_append_only(queue, keys):
    request, gap = undecided(queue, tier="T3")
    queue.halt(request.id)
    payload, sig = ruled(queue, request.id, keys["phone"])     # a phone allow of T3 is refused
    with pytest.raises(AnswerRefused):
        queue.answer_late(request.id, payload, sig)
    assert queue.is_halted("claude_code", "sess-1") == gap
    _refused(queue, "DELETE FROM halts WHERE request_id = ?", (request.id,))
    _refused(queue, "UPDATE halts SET agent_session_id = 'sess-9' WHERE request_id = ?", (request.id,))
    payload, sig = ruled(queue, request.id, keys["phone"], "deny")
    queue.answer_late(request.id, payload, sig)
    _refused(queue, "DELETE FROM halt_releases WHERE request_id = ?", (request.id,))
    _refused(queue, "UPDATE halt_releases SET released_at = 'x' WHERE request_id = ?", (request.id,))
    assert queue.is_halted("claude_code", "sess-1") is None


# --- migration of a real /3 store ------------------------------------------------------------------------------

def _v3_module():
    spec = importlib.util.spec_from_file_location("levain_consent_queue_v3_fixture", FIXTURE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod                           # dataclasses resolve the module's annotations by name
    spec.loader.exec_module(mod)
    return mod


def _dump(path):
    conn = sqlite3.connect(path)
    try:
        return {t: conn.execute(f"SELECT rowid, * FROM {t} ORDER BY rowid").fetchall()
                for t in ("requests", "receipts", "dialog_outcomes")}, \
            dict(conn.execute("SELECT key, value FROM meta").fetchall())
    finally:
        conn.close()


def test_a_v3_store_built_by_the_v3_code_migrates_in_place_row_for_row(tmp_path, keys):
    v3 = _v3_module()
    assert v3.STORE_FORMAT == PRIOR_FORMAT
    log = _own_log(tmp_path, keys)
    clock = Clock(NOW)
    d = tmp_path / "consent"
    old = v3.ConsentQueue(d, authority=v3.authority_source(log.path, anchor=None, cache=ReplayCache()), clock=clock)
    agent = FakeAgent(old)
    allowed, denied, gone, late, open_ = (agent.ask(c) for c in ("allow", "deny", "gone", "late", "open"))
    expiring = agent.ask("expiring", hold=timedelta(seconds=10))
    for r, d_ in ((allowed, "allow"), (denied, "deny")):
        payload, sig = signed(old, r, keys["phone"], d_)
        old.answer(r.id, payload, sig)
    old.withdraw(gone.id)
    old.record_ran(agent="claude_code", agent_session_id="sess-1", tool_name="Bash",
                   tool_input={"command": "late"}, event="post")      # ran in the agent's dialog: withdrawn + ran
    clock.now = NOW + timedelta(seconds=11)
    old.sweep()
    old.close_unrun(agent="claude_code", agent_session_id="sess-1", event="stop")
    before, meta_before = _dump(d / "queue.db")
    assert meta_before["format"] == PRIOR_FORMAT and len(before["receipts"]) == 5

    new = ConsentQueue(d, authority=log.source, clock=clock, home=h4_home())
    assert new.get(open_.id)[1] == "pending"                # the first open migrates
    after, meta_after = _dump(d / "queue.db")
    assert after == before                                  # every row, with its rowid / seq, unchanged
    assert meta_after == {**meta_before, "format": STORE_FORMAT}
    assert new.decision(allowed.id).decision == "allow"     # a /3 signed answer still delivers
    assert new.decision(denied.id).decision == "deny"
    assert new.get(expiring.id)[1] == "expired"
    assert new.dialog_outcome(late.id) == ("ran", utc_stamp(NOW))
    # the /4 machinery works on the migrated store, its triggers rebuilt
    assert new.mark_undecided(open_.id).gap == 1
    _refused(new, "UPDATE requests SET state = 'expired' WHERE id = ?", (open_.id,))
    _refused(new, "UPDATE requests SET state = 'denied' WHERE id = ?", (allowed.id,))
    _refused(new, "DELETE FROM receipts WHERE request_id = ?", (allowed.id,))
    payload, sig = ruled(new, open_.id, keys["phone"])
    assert new.answer_late(open_.id, payload, sig).gap == 1
    assert [r["state"] for r in new.receipts(open_.id)] == ["undecided", "allowed"]
    # Q11: the /3 answers are signed before chaining (their signatures re-verified, listed, never failed); the
    # ruling, signed now, covers every receipt before it
    report = verify_receipts(d, authority_for=lambda request: log.source)
    assert report.ok and set(report.unchained) == {allowed.id, denied.id} and not report.mismatches
    assert report.last_signed.request_id == open_.id and report.last_signed.size == 6
    # a levain that knows only /3 refuses the /4 store
    with pytest.raises(v3.QueueStoreError, match="levain-consent-queue/4"):
        v3.ConsentQueue(d, authority=v3.authority_source(log.path, anchor=None)).pending()


def test_a_failed_migration_leaves_the_v3_store_as_it_was(tmp_path, keys, monkeypatch):
    v3 = _v3_module()
    log = _own_log(tmp_path, keys)
    d = tmp_path / "consent"
    old = v3.ConsentQueue(d, authority=v3.authority_source(log.path, anchor=None), clock=Clock(NOW))
    r = FakeAgent(old).ask()
    payload, sig = signed(old, r, keys["phone"])
    old.answer(r.id, payload, sig)
    before = _dump(d / "queue.db")
    import levain.consent.queue as qmod
    monkeypatch.setattr(qmod, "_SCHEMA", qmod._SCHEMA + ("CREATE TABLE broken (",))
    with pytest.raises(sqlite3.Error):
        ConsentQueue(d, authority=log.source, home=h4_home()).pending()
    assert _dump(d / "queue.db") == before
    monkeypatch.undo()
    assert ConsentQueue(d, authority=log.source, clock=Clock(NOW), home=h4_home()).decision(r.id).decision == "allow"


def test_another_format_is_still_refused(tmp_path, authority):
    q = ConsentQueue(tmp_path / "consent", authority=authority, home=h4_home())
    q.pending()
    _raw(q, ("UPDATE meta SET value = 'levain-consent-queue/2' WHERE key = 'format'", ()))
    with pytest.raises(QueueStoreError, match="queue/2"):
        ConsentQueue(tmp_path / "consent", authority=authority, home=h4_home()).pending()


def test_the_listing_reads_a_v3_store_and_shows_an_undecided_request(tmp_path, keys):
    v3 = _v3_module()
    log = _own_log(tmp_path, keys)
    d = tmp_path / "consent"
    old = v3.ConsentQueue(d, authority=v3.authority_source(log.path, anchor=None), clock=Clock(NOW))
    r = FakeAgent(old).ask()
    assert [e.request.id for e in read_listing(d, now=NOW).pending] == [r.id]
    new = ConsentQueue(d, authority=log.source, clock=Clock(NOW), home=h4_home())
    new.mark_undecided(r.id)
    listing = read_listing(d, now=NOW)
    assert listing.pending == () and listing.recent == ()
    assert [(e.request.id, e.state, e.gap) for e in listing.undecided] == [(r.id, "undecided", 1)]


# --- B1 fix round: L1 + L2 (1017+30) ----------------------------------------------------------------------------

X = {"command": "touch /tmp/x"}
TAKE = ("claude_code", "sess-1", "Edit", X)


def _rule(q, request, key, decision="allow"):
    payload, sig = ruled(q, request.id, key, decision)
    return q.answer_late(request.id, payload, sig)


def test_a_later_deny_voids_an_earlier_untaken_allow(queue, keys, clock):
    """L2 H1's probe: gap #1 ALLOW, gap #2 on the same call DENY five minutes later -> the take gives nothing."""
    a, ga = undecided(queue)
    b, gb = undecided(queue)
    assert (ga, gb) == (1, 2)
    _rule(queue, a, keys["phone"])
    clock.now = NOW + timedelta(minutes=5)
    _rule(queue, b, keys["phone"], "deny")
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None
    assert queue.decision(a.id).ruling == "allow" and queue.decision(a.id).taken is False   # the row is unchanged


def test_a_deny_from_another_cwd_is_the_same_call(queue, keys, clock):
    a, _ = undecided(queue)
    other = queue.file(agent="claude_code", agent_session_id="sess-1", tool_name="Edit", tool_input=X,
                       cwd="/other/proj", hold=timedelta(minutes=5))
    queue.mark_undecided(other.id)
    _rule(queue, a, keys["phone"])
    clock.now = NOW + timedelta(minutes=5)
    _rule(queue, other, keys["phone"], "deny")
    # the working directory is shown, never keyed: the later deny voids the allow from either directory
    assert queue.take_ruled_allow(*TAKE, cwd="/other/proj") is None
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None


def test_a_later_allow_after_a_run_is_a_new_run(queue, keys, clock):
    a, _ = undecided(queue)
    _rule(queue, a, keys["phone"])
    assert queue.take_ruled_allow(*TAKE, cwd=CWD).request_id == a.id
    clock.now = NOW + timedelta(minutes=5)                                # the agent tried again: a new gap
    b, _ = undecided(queue)
    _rule(queue, b, keys["phone"])
    assert queue.take_ruled_allow(*TAKE, cwd=CWD).request_id == b.id
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None


def test_a_raw_reorder_of_decided_at_voids_never_revives(queue, keys, clock):
    """An earlier allow whose decided_at a raw writer moves past a later deny is still voided: the deny's signed
    signed_at is later."""
    a, _ = undecided(queue)
    b, _ = undecided(queue)
    _rule(queue, a, keys["phone"])
    clock.now = NOW + timedelta(minutes=5)
    _rule(queue, b, keys["phone"], "deny")
    _raw(queue, ("DROP TRIGGER requests_frozen", ()),
         ("UPDATE requests SET decided_at = ? WHERE id = ?", (utc_stamp(NOW + timedelta(minutes=9)), a.id)))
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None


@pytest.mark.parametrize("first,second,wins", [
    (("b", "allow"), ("a", "deny"), None), (("a", "allow"), ("b", "deny"), None),
    (("a", "deny"), ("b", "allow"), "b"), (("b", "deny"), ("a", "allow"), "a")])
def test_the_latest_ruling_is_the_last_applied_within_one_second(queue, keys, first, second, wins):
    """L3 r1 codex HIGH: two rulings in the same stamp second, in either order: the one the queue applied last
    counts (its receipt's seq), never the request filed later."""
    reqs = {"a": undecided(queue)[0], "b": undecided(queue)[0]}
    _rule(queue, reqs[first[0]], keys["phone"], first[1])
    _rule(queue, reqs[second[0]], keys["phone"], second[1])
    got = queue.take_ruled_allow(*TAKE, cwd=CWD)
    assert (got is None) if wins is None else (got.request_id == reqs[wins].id)


def test_the_latest_ruling_survives_a_clock_stepping_backwards(queue, keys, clock):
    a, _ = undecided(queue)
    b, _ = undecided(queue)
    clock.now = NOW + timedelta(minutes=5)
    _rule(queue, a, keys["phone"])
    clock.now = NOW                                                       # the clock steps back
    _rule(queue, b, keys["phone"], "deny")                                # applied last: the deny counts
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None


def test_claim_rulings_for_delivers_in_application_order_within_one_second(queue, keys):
    a, _ = undecided(queue, "a")
    b, _ = undecided(queue, "b")
    _rule(queue, b, keys["phone"], "deny")
    _rule(queue, a, keys["phone"])
    assert [r.request_id for r in queue.claim_rulings_for("claude_code", "sess-1")] == [b.id, a.id]


def _slow_verifier_leaves_the_lock_free(monkeypatch, q):
    """A verifier stub that sleeps and, while it sleeps, takes the queue's write lock from another connection."""
    import sqlite3
    import time as _time
    import levain.consent.queue as qmod
    real, seen = qmod.verify_link_sig, []

    def slow(*a, **kw):
        _time.sleep(0.2)
        other = sqlite3.connect(q.path, timeout=0.1)
        try:
            other.execute("BEGIN IMMEDIATE")
            other.execute("ROLLBACK")
            seen.append("free")
        except sqlite3.OperationalError:
            seen.append("locked")
        finally:
            other.close()
        return real(*a, **kw)
    monkeypatch.setattr(qmod, "verify_link_sig", slow)
    return seen


def test_no_signature_is_verified_under_the_write_lock(queue, keys, monkeypatch):
    """L3 r1 codex MED: take_ruled_allow and claim_rulings_for verify outside BEGIN IMMEDIATE."""
    a, _ = undecided(queue)
    _rule(queue, a, keys["phone"])
    seen = _slow_verifier_leaves_the_lock_free(monkeypatch, queue)
    assert [r.request_id for r in queue.claim_rulings_for("claude_code", "sess-1")] == [a.id]
    assert queue.take_ruled_allow(*TAKE, cwd=CWD).request_id == a.id
    assert seen and set(seen) == {"free"}


def test_a_ruling_applied_during_verification_is_selected_again(queue, keys, monkeypatch):
    """A deny applied while the allow is being verified moves the selection: the take gives nothing."""
    import levain.consent.queue as qmod
    a, _ = undecided(queue)
    b, _ = undecided(queue)
    _rule(queue, a, keys["phone"])
    deny = ruled(queue, b.id, keys["phone"], "deny")
    real, fired = qmod.verify_link_sig, []

    def racing(*args, **kw):
        if not fired:
            fired.append(1)
            queue.answer_late(b.id, *deny)
        return real(*args, **kw)
    monkeypatch.setattr(qmod, "verify_link_sig", racing)
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None
    assert fired and queue.decision(a.id).taken is False


def test_a_halt_retry_on_an_undecided_row_with_no_gap_raises(queue):
    """L3 r1 complement LOW: a halt is never skipped silently."""
    request = ask(queue)
    _raw(queue, ("UPDATE requests SET state = 'undecided' WHERE id = ?", (request.id,)))   # a raw move: no gap
    with pytest.raises(QueueStoreError, match="no gap number"):
        queue.mark_undecided(request.id, halt=True)
    got = queue.mark_undecided(request.id)
    assert (got.state, got.gap) == ("undecided", None)


def test_an_unreadable_other_ruling_on_the_call_fails_the_take_closed(queue, keys):
    """L3 r1 complement LOW: an earlier-applied ruling whose record a raw writer broke voids the take."""
    a, _ = undecided(queue)
    b, _ = undecided(queue)
    _rule(queue, a, keys["phone"], "deny")
    _rule(queue, b, keys["phone"])
    _raw(queue, ("DROP TRIGGER requests_frozen", ()), ("UPDATE requests SET answer = 'x' WHERE id = ?", (a.id,)))
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None


def test_only_the_take_delivers_a_ruled_allow(queue, keys):
    """L2 M2: decision() and mark_undecided() give a ruling as information (a Ruling, never a Decision), so the
    adapters' delivery check refuses it before and after the take."""
    from levain.consent.adapters.claude_code import _verified
    request, gap = undecided(queue)
    _rule(queue, request, keys["phone"])
    for got in (queue.decision(request.id), queue.mark_undecided(request.id).decision,
                queue.await_decision(request.id, 0.01, poll=0.01)):
        assert isinstance(got, Ruling) and not isinstance(got, Decision) and _verified(got, request) is None
    marked = queue.mark_undecided(request.id)
    assert (marked.state, marked.gap) == ("allowed", gap) and f"gap #{gap}" in marked.reason
    assert _verified(queue.take_ruled_allow(*TAKE, cwd=CWD), request) is not None
    after = (queue.decision(request.id), queue.mark_undecided(request.id).decision)
    assert all(isinstance(g, Ruling) and g.taken and _verified(g, request) is None for g in after)


def test_undecided_and_halt_are_one_transaction(queue, monkeypatch):
    """L2 M3: a hook killed between the gap and the halt leaves neither."""
    request = ask(queue, tier="T3")
    import levain.consent.queue as qmod

    def boom(*a, **kw):
        raise RuntimeError("killed")
    monkeypatch.setattr(qmod.ConsentQueue, "_insert_halt", staticmethod(boom))
    with pytest.raises(RuntimeError):
        queue.mark_undecided(request.id, halt=True)
    assert queue.get(request.id)[1] == "pending" and queue.gap(request.id) is None
    assert queue.receipts(request.id) == []
    monkeypatch.undo()
    marked = queue.mark_undecided(request.id, halt=True)
    assert (marked.state, marked.gap, marked.halted) == ("undecided", 1, True)
    assert queue.is_halted("claude_code", "sess-1") == 1


def test_a_retry_with_halt_halts_an_already_undecided_request(queue):
    request, gap = undecided(queue, tier="T3")
    assert queue.is_halted("claude_code", "sess-1") is None
    marked = queue.mark_undecided(request.id, halt=True)
    assert (marked.state, marked.gap, marked.halted) == ("undecided", gap, True)


def test_mark_undecided_says_why_an_answer_no_longer_verifies(tmp_path, keys, clock):
    log = _own_log(tmp_path, keys)
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = ask(q)
    payload, sig = signed(q, request, keys["phone"])
    q.answer(request.id, payload, sig)
    log.revoke("phone")
    marked = q.mark_undecided(request.id)
    assert (marked.state, marked.gap, marked.decision) == ("allowed", None, None)
    assert "no longer verifies" in marked.reason


def test_a_ruling_signed_ahead_of_the_queue_clock_is_refused(queue, keys):
    """L1 #2's probe: a ruling signed 30 days ahead was accepted and its lapse pushed out."""
    request, _ = undecided(queue)
    for ahead in (timedelta(days=30), RULING_SKEW + timedelta(seconds=1)):
        payload = {**queue.ruling_payload(request.id, "allow", keys["phone"].fp),
                   "signed_at": utc_stamp(NOW + ahead)}
        with pytest.raises(AnswerRefused, match="later than the queue's clock"):
            queue.answer_late(request.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
        assert queue.get(request.id)[1] == "undecided"
    payload = {**queue.ruling_payload(request.id, "allow", keys["phone"].fp), "signed_at": utc_stamp(NOW + RULING_SKEW)}
    assert queue.answer_late(request.id, payload,
                             keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE)).ruling == "allow"


def test_the_lapse_runs_from_when_the_queue_applied_the_ruling(queue, keys, clock):
    request, _ = undecided(queue)
    payload = queue.ruling_payload(request.id, "allow", keys["phone"].fp)    # signed at NOW
    clock.now = NOW + timedelta(hours=1)                                    # applied an hour later
    queue.answer_late(request.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    clock.now = NOW + timedelta(hours=1) + RULED_ALLOW_LAPSE - timedelta(seconds=1)
    assert queue.take_ruled_allow(*TAKE, cwd=CWD).request_id == request.id


def test_a_raw_stretch_of_decided_at_is_bounded_by_the_signature(queue, keys, clock):
    request, _ = undecided(queue)
    _rule(queue, request, keys["phone"])                                   # signed and applied at NOW
    _raw(queue, ("DROP TRIGGER requests_frozen", ()),
         ("UPDATE requests SET decided_at = ? WHERE id = ?", (utc_stamp(NOW + RULED_ALLOW_LAPSE), request.id)))
    clock.now = NOW + RULED_ALLOW_LAPSE + timedelta(hours=1)
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None
    _raw(queue, ("UPDATE requests SET decided_at = ? WHERE id = ?",          # decided before it was signed
                 (utc_stamp(NOW - RULING_SKEW - timedelta(seconds=1)), request.id)))
    assert queue.decision(request.id) is None


def test_claim_rulings_for_delivers_each_ruling_once(queue, keys, clock):
    a, ga = undecided(queue, "a")
    b, gb = undecided(queue, "b")
    o, _ = undecided(queue, "o", session="sess-2")
    c, _ = undecided(queue, "c")                                            # still undecided: no ruling yet
    _rule(queue, a, keys["phone"])
    clock.now = NOW + timedelta(seconds=1)
    _rule(queue, b, keys["phone"], "deny")
    _rule(queue, o, keys["phone"])
    got = queue.claim_rulings_for("claude_code", "sess-1")
    assert [(r.request_id, r.ruling, r.gap) for r in got] == [(a.id, "allow", ga), (b.id, "deny", gb)]
    assert all(isinstance(r, Ruling) for r in got)
    assert queue.claim_rulings_for("claude_code", "sess-1") == []
    assert [r.request_id for r in queue.claim_rulings_for("claude_code", "sess-2")] == [o.id]
    _rule(queue, c, keys["phone"])
    assert [r.request_id for r in queue.claim_rulings_for("claude_code", "sess-1")] == [c.id]
    _refused(queue, "DELETE FROM ruling_deliveries WHERE request_id = ?", (a.id,))
    _refused(queue, "UPDATE ruling_deliveries SET delivered_at = 'x' WHERE request_id = ?", (a.id,))


def test_a_ruling_that_no_longer_verifies_is_not_claimed(tmp_path, keys, clock):
    log = _own_log(tmp_path, keys)
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request, _ = undecided(q)
    _rule(q, request, keys["phone"])
    log.revoke("phone")
    assert q.claim_rulings_for("claude_code", "sess-1") == []


def test_undecided_for_and_request_by_gap(queue, keys):
    a, ga = undecided(queue, "a")
    b, gb = undecided(queue, "b", session="sess-2")
    c, gc = undecided(queue, "c", agent="codex")
    _rule(queue, a, keys["phone"], "deny")
    assert [g.request.id for g in queue.undecided_for()] == [b.id, c.id]
    assert [g.request.id for g in queue.undecided_for(agent="codex")] == [c.id]
    assert [g.request.id for g in queue.undecided_for("claude_code", "sess-2")] == [b.id]
    assert queue.undecided_for("claude_code", "sess-1") == []
    got = queue.request_by_gap("claude_code", "sess-1", ga)
    assert (got.request.id, got.gap, got.state) == (a.id, 1, "denied")    # a ruled one is still found by its gap
    assert queue.request_by_gap("claude_code", "sess-1", 2) is None
    assert queue.request_by_gap("codex", "sess-1", gc).request.id == c.id
    assert queue.request_by_gap("claude_code", "sess-1", True) is None


def test_resume_meta_is_written_with_the_request_and_read_back(queue):
    meta = {"permission_mode": "bypassPermissions", "invocation": "bg", "codex_pid": None, "cwd": "/work/proj"}
    r = queue.file(agent="claude_code", agent_session_id="s", tool_name="Edit", tool_input=X, cwd="/work/proj",
                   hold=timedelta(minutes=5), resume=meta)
    assert queue.resume_meta(r.id) == meta
    plain = ask(queue, "plain")
    assert queue.resume_meta(plain.id) is None
    from levain.cockpit.consent import ConsentError
    for bad in ({"invocation": "fork"}, {"permission_mode": "x"}, {"invocation": "-p", "codex_pid": True},
                {"invocation": "-p", "codex_pid": 0}, {"invocation": "-p", "extra": 1}, {"invocation": "-p", "cwd": ""},
                ["invocation"]):
        with pytest.raises(ConsentError):
            queue.file(agent="claude_code", agent_session_id="s", tool_name="Edit", tool_input={"bad": str(bad)},
                       cwd="/w", hold=timedelta(minutes=5), resume=bad)
    assert len(queue.pending()) == 2                                        # a refused resume filed nothing
    _refused(queue, "UPDATE resume_meta SET meta = '{}' WHERE request_id = ?", (r.id,))
    _refused(queue, "DELETE FROM resume_meta WHERE request_id = ?", (r.id,))


def test_two_sweepers_never_both_claim_a_reping(tmp_path, authority, clock):
    d = tmp_path / "consent"
    qs = [ConsentQueue(d, authority=authority, clock=clock, home=h4_home()) for _ in range(4)]
    for i in range(12):
        qs[0].snooze(ask(qs[0], f"r{i}", hold=timedelta(hours=2)).id, minutes=1)
    clock.now = NOW + timedelta(minutes=2)
    got: list[str] = []
    lock = threading.Lock()

    def sweep(q):
        for _ in range(3):
            claimed = q.claim_repings()
            with lock:
                got.extend(r.request_id for r in claimed)

    threads = [threading.Thread(target=sweep, args=(q,)) for q in qs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(got) == 12 and len(set(got)) == 12


def test_the_take_checks_cheaply_before_the_replay_and_the_lock(queue, keys, monkeypatch):
    calls = []
    real = ConsentQueue._fresh_authority
    monkeypatch.setattr(ConsentQueue, "_fresh_authority", lambda self: calls.append(1) or real(self))
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None
    assert calls == []                                                    # nothing ruled: no replay
    request, _ = undecided(queue)
    _rule(queue, request, keys["phone"])
    calls.clear()
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is not None and calls == [1]
    calls.clear()
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None and calls == []   # taken: no replay
    conn = sqlite3.connect(queue.path)
    try:
        plan = " ".join(str(r) for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM requests r WHERE agent = ? AND agent_session_id = ? AND tool_name = ? "
            "AND tool_input_digest = ? AND cwd = ? AND via = 'ruling' AND state = 'allowed'",
            ("a", "b", "c", "d", "e")).fetchall())
    finally:
        conn.close()
    assert "requests_ruled" in plan


def _take_in_a_process(qdir, cockpit_db, out):
    from levain.consent.queue import ConsentQueue as Q, authority_source as src
    q = Q(qdir, authority=src(cockpit_db, anchor=None), clock=lambda: NOW + timedelta(minutes=1), home=h4_home())
    got = q.take_ruled_allow(*TAKE, cwd=CWD)
    out.put(None if got is None else got.request_id)


def test_a_ruled_allow_is_taken_once_across_processes(tmp_path, keys, clock):
    import multiprocessing
    log = _own_log(tmp_path, keys)
    d = tmp_path / "consent"
    q = ConsentQueue(d, authority=log.source, clock=clock, home=h4_home())
    request, _ = undecided(q)
    _rule(q, request, keys["phone"])
    ctx = multiprocessing.get_context("spawn")
    out = ctx.Queue()
    procs = [ctx.Process(target=_take_in_a_process, args=(d, log.path, out)) for _ in range(3)]
    for p in procs:
        p.start()
    got = [out.get(timeout=60) for _ in procs]
    for p in procs:
        p.join(timeout=60)
    assert sorted(got, key=str) == [None, None, request.id]


# --- the surfaces --------------------------------------------------------------------------------------------

def test_the_listing_keeps_every_undecided_request_past_the_day(queue, keys, clock):
    old, gap = undecided(queue, "old")
    done, _ = undecided(queue, "done")
    _rule(queue, done, keys["phone"], "deny")
    clock.now = NOW + timedelta(days=3)
    listing = read_listing(queue.directory, now=clock.now)
    assert [(e.request.id, e.state, e.gap) for e in listing.undecided] == [(old.id, "undecided", gap)]
    assert listing.recent == ()                                           # the ruled one is past the day
    from levain.consent.listing import audit_line, find_entry
    assert find_entry(listing, old.id).gap == gap
    assert audit_line(listing.undecided[0]).startswith(f"undecided: ") and f"gap #{gap}" in \
        audit_line(listing.undecided[0])


def test_the_audit_line_names_a_ruling_as_a_ruling(queue, keys):
    a, ga = undecided(queue, "a")
    b, gb = undecided(queue, "b")
    _rule(queue, a, keys["phone"])
    _rule(queue, b, keys["phone"], "deny")
    from levain.consent.listing import audit_line
    lines = {e.request.id: audit_line(e) for e in read_listing(queue.directory, now=NOW).recent}
    assert lines[a.id] == f"allowed by a late ruling on gap #{ga}, key {keys['phone'].fp}"
    assert lines[b.id] == f"denied by a late ruling on gap #{gb}, key {keys['phone'].fp}"


def test_the_cockpit_groups_an_undecided_request_under_needs_input(tmp_path, keys):
    from levain.cockpit import Cockpit
    log = _own_log(tmp_path, keys)
    d = tmp_path / "consent"
    q = ConsentQueue(d, authority=log.source, clock=Clock(NOW), home=h4_home())
    waiting, _ = undecided(q)
    ck = Cockpit(clock=lambda: NOW + timedelta(days=2))
    ck.attach_consent_rows(lambda now: read_listing(d, now=now))
    p = ck.panel("decisions", credential_class="token")
    by_id = {r["id"]: r for r in p["rows"]}
    row = by_id[f"consent:{waiting.id}"]
    assert row["group"] == "needs_input" and row["facets"]["consent_state"] == "undecided"
    assert p["count"] == 1


def test_the_sessions_tile_counts_an_undecided_request_as_waiting(tmp_path, monkeypatch):
    from levain.cockpit.providers import build_default_cockpit
    from tests.test_cockpit_sessions import _fixture
    root, src = _fixture(tmp_path, monkeypatch)
    from levain.consent.queue import authority_source
    home = tmp_path / "levain_home" / "consent"
    q = ConsentQueue(home, authority=authority_source(home, anchor=None), home=h4_home())
    (filed,) = q.pending()
    assert q.mark_undecided(filed.id).state == "undecided"
    ck = build_default_cockpit(src)
    ck.attach_consent_rows(lambda now: read_listing(now=now))
    p = ck.panel("sessions", credential_class="none")
    meta = {r["facets"]["agent"] + ":" + r["facets"]["session_activity"]: r["facets"]["legacy_meta"] for r in p["rows"]}
    assert "1 waiting" in meta["claude_code:active"]


def test_a_raw_receipt_never_lifts_an_older_allow_over_a_newer_deny(queue, keys):
    """L3 r2 codex HIGH: allow A then deny B in one stamp second; a raw writer appends a second terminal receipt
    for A (a 'denied' one: the unique (request_id, state) index refuses a second 'allowed', no trigger refuses an
    insert) to give A the highest seq. A ruling has exactly one terminal receipt, naming its row's state, so A's
    sequence is void and the take fails closed instead of delivering A."""
    a, _ = undecided(queue)
    b, _ = undecided(queue)
    _rule(queue, a, keys["phone"])
    _rule(queue, b, keys["phone"], "deny")
    _raw(queue, ("INSERT INTO receipts (request_id, state, created_at, face) VALUES (?, 'denied', 'x', '{}')", (a.id,)))
    assert queue.take_ruled_allow(*TAKE, cwd=CWD) is None
