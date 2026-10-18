"""The consent queue's /4 additions (Q14 B1): the ``undecided`` state and its gap numbers, the late ruling (its own
record and namespace), the consumed-once ruled allow, snoozes, session halts, and the in-place migration of a /3
store.

The /3 store is built by the /3 code itself: ``tests/fixtures/consent_queue_v3_d430b804.py`` is
``git show d430b804:levain/consent/queue.py`` saved unchanged, loaded here as its own module.
"""
from __future__ import annotations

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
from levain.consent.queue import PRIOR_FORMAT, RULED_ALLOW_LAPSE, STORE_FORMAT
from levain.consent.record import RULING_FORMAT, RULING_NAMESPACE, ruling_message, ruling_payload
from tests.test_consent_queue import NOW, Clock, FakeAgent, Log, _raw, signed
from tests.test_consent_queue import authority, clock, keys, queue  # noqa: F401 - the shared fixtures

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "consent_queue_v3_d430b804.py"


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
    gap = q.mark_undecided(request.id)
    assert isinstance(gap, int)
    return request, gap


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
    assert queue.mark_undecided(request.id) == 1
    assert queue.get(request.id)[1] == "undecided"
    assert queue.gap(request.id) == 1
    assert queue.receipt(request.id)["state"] == "undecided"
    assert queue.receipt(request.id)["face"]["terminal_state"] == "timed_out"
    assert queue.decision(request.id) is None
    assert queue.await_decision(request.id, 0.05, poll=0.01) is None
    assert queue.mark_undecided(request.id) == 1           # a retry of the hook: the same gap, nothing new
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
    q1, q2 = ConsentQueue(d, authority=authority), ConsentQueue(d, authority=authority)
    requests = [ask(q1, f"c{i}", hold=timedelta(hours=1)) for i in range(24)]
    got: list[int] = []
    lock = threading.Lock()

    def mark(q, chunk):
        for r in chunk:
            g = q.mark_undecided(r.id)
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
    assert got.decision == "deny" and got.via == "signature"
    assert queue.get(request.id)[1] == "denied" and queue.gap(request.id) is None


def test_an_expired_or_withdrawn_request_is_never_undecided(queue, clock):
    late = ask(queue, "late", hold=timedelta(seconds=30))
    gone = ask(queue, "gone")
    queue.withdraw(gone.id)
    clock.now = NOW + timedelta(seconds=31)                # read under the lock: a due request expires first
    assert queue.mark_undecided(late.id) is None
    assert queue.get(late.id)[1] == "expired"
    assert queue.mark_undecided(gone.id) is None
    assert queue.mark_undecided("no-such-request") is None


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
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}) is None


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
    assert (d.decision, d.via, d.gap, d.signer_fp) == ("allow", "ruling", gap, keys["phone"].fp)
    again = queue.decision(request.id)
    assert (again.decision, again.via, again.gap) == ("allow", "ruling", gap)
    assert [r["state"] for r in queue.receipts(request.id)] == ["undecided", "allowed"]
    assert f"gap=#{gap}" in queue.receipt(request.id)["face"]["authority_scope"]["grant"]


def test_a_ruling_deny(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["presence"], "deny")   # a deny needs only an enrolled key
    assert queue.answer_late(request.id, payload, sig).decision == "deny"
    assert queue.decision(request.id).decision == "deny"


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
    ({"format": "levain-consent-ruling/2"}, "ruling format"),
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
    assert queue.answer_late(request.id, payload, sig).decision == "deny"
    other, _ = undecided(queue, "other", tier="T3")
    payload, sig = ruled(queue, other.id, keys["laptop"])
    assert queue.answer_late(other.id, payload, sig).decision == "allow"


def test_unknown_and_revoked_signers_cannot_rule(queue, keys):
    request, _ = undecided(queue)
    for name, why in (("stranger", "not an enrolled key"), ("revoked", "revoked")):
        payload, sig = ruled(queue, request.id, keys[name])
        with pytest.raises(AnswerRefused, match=why):
            queue.answer_late(request.id, payload, sig)


def test_a_signer_revoked_after_ruling_delivers_nothing(tmp_path, keys, clock):
    log = _own_log(tmp_path, keys)
    q = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock)
    request, _ = undecided(q)
    payload, sig = ruled(q, request.id, keys["phone"])
    assert q.answer_late(request.id, payload, sig).decision == "allow"
    assert q.decision(request.id).decision == "allow"
    log.revoke("phone")
    assert q.decision(request.id) is None
    assert q.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}) is None


def test_a_ruling_relabelled_by_a_raw_writer_delivers_nothing(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"], "deny")
    queue.answer_late(request.id, payload, sig)
    _raw(queue, ("DROP TRIGGER requests_frozen", ()),
         ("UPDATE requests SET state = 'allowed' WHERE id = ?", (request.id,)))
    assert queue.decision(request.id) is None
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}) is None


def test_the_record_helpers_refuse_a_bad_gap():
    from levain.cockpit.consent import ConsentError
    from levain.consent.record import ExternalGateRequest
    r = ExternalGateRequest.create(agent="claude_code", agent_session_id="s", event_id="rand:1", tool_name="Edit",
                                   tool_input_digest="jcs:x", display="d", cwd="/", tier="T2",
                                   received_at=utc_stamp(NOW), expires_at=utc_stamp(NOW + timedelta(minutes=1)))
    for gap in (0, True, "1", 1.0):
        with pytest.raises(ConsentError):
            ruling_payload(queue_id="cq-1", request=r, gap=gap, decision="allow", signer_fp="fp",
                           signed_at=utc_stamp(NOW))


# --- a ruled allow pre-authorises the call once ----------------------------------------------------------------

def test_a_ruled_allow_is_taken_once(queue, keys):
    request, gap = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"])
    queue.answer_late(request.id, payload, sig)
    args = ("claude_code", "sess-1", "Edit")
    assert queue.take_ruled_allow(*args, {"command": "touch /tmp/other"}) is None     # another digest
    assert queue.take_ruled_allow("claude_code", "sess-2", "Edit", {"command": "touch /tmp/x"}) is None
    assert queue.take_ruled_allow("claude_code", "sess-1", "Write", {"command": "touch /tmp/x"}) is None
    got = queue.take_ruled_allow(*args, {"command": "touch /tmp/x"}, event="hook:tu-1")
    assert (got.request_id, got.decision, got.via, got.gap) == (request.id, "allow", "ruling", gap)
    assert queue.take_ruled_allow(*args, {"command": "touch /tmp/x"}) is None          # consumed
    assert queue.decision(request.id).decision == "allow"   # the request row itself is unchanged
    _refused(queue, "DELETE FROM ruled_takes WHERE request_id = ?", (request.id,))
    _refused(queue, "UPDATE ruled_takes SET event = 'x' WHERE request_id = ?", (request.id,))


def test_the_oldest_unconsumed_ruled_allow_is_taken_first(queue, keys, clock):
    a, _ = undecided(queue)
    b, _ = undecided(queue)                                 # two identical calls, both ruled allow
    for r in (a, b):
        clock.now += timedelta(seconds=1)
        payload, sig = ruled(queue, r.id, keys["phone"])
        queue.answer_late(r.id, payload, sig)
    args = ("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"})
    assert queue.take_ruled_allow(*args).request_id == a.id
    assert queue.take_ruled_allow(*args).request_id == b.id
    assert queue.take_ruled_allow(*args) is None


def test_a_ruled_deny_pre_authorises_nothing(queue, keys):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"], "deny")
    queue.answer_late(request.id, payload, sig)
    assert queue.take_ruled_allow("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"}) is None


def test_a_ruled_allow_lapses_after_its_window(queue, keys, clock):
    request, _ = undecided(queue)
    payload, sig = ruled(queue, request.id, keys["phone"])
    queue.answer_late(request.id, payload, sig)
    args = ("claude_code", "sess-1", "Edit", {"command": "touch /tmp/x"})
    clock.now = NOW + RULED_ALLOW_LAPSE
    assert queue.take_ruled_allow(*args) is None
    clock.now = NOW + RULED_ALLOW_LAPSE - timedelta(seconds=1)
    assert queue.take_ruled_allow(*args).request_id == request.id


# --- snooze ----------------------------------------------------------------------------------------------------

def test_snooze_records_when_to_ask_again(queue, clock):
    p = ask(queue, "p", hold=timedelta(hours=2))
    u, _ = undecided(queue, "u")
    done = ask(queue, "done")
    queue.withdraw(done.id)
    assert queue.snooze(p.id) == utc_stamp(NOW + timedelta(minutes=30))
    assert queue.snooze(u.id, minutes=10) == utc_stamp(NOW + timedelta(minutes=10))
    assert queue.snooze(done.id) is None
    assert queue.due_repings() == []
    clock.now = NOW + timedelta(minutes=10)
    assert [(r.request_id, r.state) for r in queue.due_repings()] == [(u.id, "undecided")]
    due = queue.due_repings(NOW + timedelta(minutes=30))
    assert [r.request_id for r in due] == [u.id, p.id]
    assert queue.mark_repinged(due[0].seq) is True
    assert queue.mark_repinged(due[0].seq) is False
    assert queue.mark_repinged(999) is False
    assert [r.request_id for r in queue.due_repings(NOW + timedelta(minutes=30))] == [p.id]
    queue.snooze(p.id, minutes=60)                         # a later snooze replaces the earlier one
    assert queue.due_repings(NOW + timedelta(minutes=30)) == []
    _refused(queue, "DELETE FROM snoozes WHERE request_id = ?", (p.id,))
    _refused(queue, "UPDATE repings SET repinged_at = 'x' WHERE snooze_seq = ?", (due[0].seq,))


def test_a_snooze_of_a_request_that_expired_or_was_ruled_is_not_due(queue, clock, keys):
    p = ask(queue, "p", hold=timedelta(minutes=20))
    u, _ = undecided(queue, "u")
    queue.snooze(p.id)
    queue.snooze(u.id)
    payload, sig = ruled(queue, u.id, keys["phone"])
    queue.answer_late(u.id, payload, sig)
    assert queue.due_repings(NOW + timedelta(minutes=31)) == []


@pytest.mark.parametrize("minutes", [0, -5, True, float("nan"), float("inf"), "30"])
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

    new = ConsentQueue(d, authority=log.source, clock=clock)
    assert new.get(open_.id)[1] == "pending"                # the first open migrates
    after, meta_after = _dump(d / "queue.db")
    assert after == before                                  # every row, with its rowid / seq, unchanged
    assert meta_after == {**meta_before, "format": STORE_FORMAT}
    assert new.decision(allowed.id).decision == "allow"     # a /3 signed answer still delivers
    assert new.decision(denied.id).decision == "deny"
    assert new.get(expiring.id)[1] == "expired"
    assert new.dialog_outcome(late.id) == ("ran", utc_stamp(NOW))
    # the /4 machinery works on the migrated store, its triggers rebuilt
    assert new.mark_undecided(open_.id) == 1
    _refused(new, "UPDATE requests SET state = 'expired' WHERE id = ?", (open_.id,))
    _refused(new, "UPDATE requests SET state = 'denied' WHERE id = ?", (allowed.id,))
    _refused(new, "DELETE FROM receipts WHERE request_id = ?", (allowed.id,))
    payload, sig = ruled(new, open_.id, keys["phone"])
    assert new.answer_late(open_.id, payload, sig).gap == 1
    assert [r["state"] for r in new.receipts(open_.id)] == ["undecided", "allowed"]
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
        ConsentQueue(d, authority=log.source).pending()
    assert _dump(d / "queue.db") == before
    monkeypatch.undo()
    assert ConsentQueue(d, authority=log.source, clock=Clock(NOW)).decision(r.id).decision == "allow"


def test_another_format_is_still_refused(tmp_path, authority):
    q = ConsentQueue(tmp_path / "consent", authority=authority)
    q.pending()
    _raw(q, ("UPDATE meta SET value = 'levain-consent-queue/2' WHERE key = 'format'", ()))
    with pytest.raises(QueueStoreError, match="queue/2"):
        ConsentQueue(tmp_path / "consent", authority=authority).pending()


def test_the_listing_reads_a_v3_store_and_shows_an_undecided_request(tmp_path, keys):
    v3 = _v3_module()
    log = _own_log(tmp_path, keys)
    d = tmp_path / "consent"
    old = v3.ConsentQueue(d, authority=v3.authority_source(log.path, anchor=None), clock=Clock(NOW))
    r = FakeAgent(old).ask()
    assert [e.request.id for e in read_listing(d, now=NOW).pending] == [r.id]
    new = ConsentQueue(d, authority=log.source, clock=Clock(NOW))
    new.mark_undecided(r.id)
    listing = read_listing(d, now=NOW)
    assert listing.pending == () and [(e.request.id, e.state) for e in listing.recent] == [(r.id, "undecided")]
