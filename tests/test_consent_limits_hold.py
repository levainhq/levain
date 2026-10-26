"""Q13 session 2: the hold at a limit, Extend / End / silence, the halt and what releases it (levain.consent.limits),
against a real queue and a real signed authority log."""
from __future__ import annotations

import threading
import time

from levain.consent import limits as L
from levain.consent.queue import AnswerRefused, ConsentQueue
from levain.consent.record import RULING_NAMESPACE, ruling_message
from tests.test_consent_queue import authority, keys, signed  # noqa: F401 - the shared fixtures
from tests._h4 import h4_env, h4_home

LIM = dict(L.DEFAULTS, calls=2)


def _q(tmp_path, authority):
    return ConsentQueue(tmp_path / "home" / "consent", authority=authority, home=h4_home())


def _check(q, i, *, session="s1", hold=2.0, pings=None, limits=LIM, monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.setattr(L, "load_limits", lambda *_a, **_k: (dict(limits), "test"))
    return L.check("claude_code", {"tool_use_id": f"t{i}"}, q, session, "Read", {"file_path": f"/x/{i}"},
                   hold=hold, notify=(pings.append if pings is not None else (lambda _t: None)), cwd="/work")


def _answer_when_filed(q, key, decision, *, late=False):
    """A phone that answers the first pending limit request (or rules on its gap once undecided)."""
    def run():
        for _ in range(200):
            time.sleep(0.05)
            if late:
                gapped = q.undecided_for("claude_code")
                if gapped:
                    rid = gapped[0].request.id
                    payload = q.ruling_payload(rid, decision, key.fp)
                    q.answer_late(rid, payload, key.sign(ruling_message(payload), RULING_NAMESPACE))
                    return
                continue
            for r in q.pending():
                if r.tool_name == L.LIMIT_TOOL:
                    payload, sig = signed(q, r, key, decision)
                    try:
                        q.answer(r.id, payload, sig)
                    except AnswerRefused:
                        pass
                    return
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_extend_doubles_and_the_held_call_goes_on(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    pings: list[str] = []
    assert _check(q, 1, monkeypatch=monkeypatch).go and _check(q, 2).go
    t = _answer_when_filed(q, keys["phone"], "allow")
    out = _check(q, 3, pings=pings)
    t.join()
    assert out.go and len(pings) == 1 and "Extend to 4" in pings[0]
    assert _check(q, 4).go and not _check(q, 5, hold=0.3).go   # the doubled limit, 4, is crossed by the 5th


def test_end_halts_the_root_and_its_subagents(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    t = _answer_when_filed(q, keys["phone"], "deny")
    out = _check(q, 3)
    t.join()
    assert not out.go and out.stop and "operator chose End" in out.text
    sub = _check(q, 4, session="s1#agent:a1")
    assert not sub.go and sub.stop and "STOPPED by a budget" in sub.text and "operator chose End" in sub.text
    assert L.snapshot(q.directory)[0]["counts"]["calls"] == 3   # a halted root counts nothing


def test_silence_halts_with_a_gap_and_a_late_extend_releases_it(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    out = _check(q, 3, hold=0.5)
    assert not out.go and out.stop and "gap #1" in out.text
    assert not _check(q, 4).go
    t = _answer_when_filed(q, keys["phone"], "allow", late=True)
    t.join()
    out = _check(q, 5)
    assert out.go, out.text       # the counts resume where they stopped, under the doubled limit (4)


def test_a_late_deny_releases_nothing(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    _check(q, 3, hold=0.5)
    _answer_when_filed(q, keys["phone"], "deny", late=True).join()
    assert not _check(q, 4).go


def test_levain_limits_extend_after_end(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    t = _answer_when_filed(q, keys["phone"], "deny")
    _check(q, 3)
    t.join()
    rid, call = L.file_extend(q, "claude_code", "s1", hold=30, cwd="/work")
    assert call["limit"] == "calls" and call["extend_to"] == "4"
    request = q.get(rid)[0]
    payload, sig = signed(q, request, keys["phone"], "allow")
    q.answer(rid, payload, sig)
    assert _check(q, 4).go


def test_an_unsigned_or_revoked_allow_extends_nothing(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    t = _answer_when_filed(q, keys["revoked"], "allow")
    out = _check(q, 3, hold=0.8)
    t.join()
    assert not out.go and out.stop


def test_parallel_crossings_file_one_request(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    outs: list = []
    ths = [threading.Thread(target=lambda i=i: outs.append(_check(q, 10 + i, session=f"s1#agent:a{i}")))
           for i in range(3)]
    t = _answer_when_filed(q, keys["phone"], "allow")
    for th in ths:
        th.start()
    for th in ths:
        th.join()
    t.join()
    filed = [r for r in q.pending()]   # nothing left pending
    assert not filed
    conn = L._connect(q.directory)
    n = conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    conn.close()
    assert n == 1 and all(o.go for o in outs)


# --- L2 r1 findings ------------------------------------------------------------------------------------------------

def _dead_filer(q, kind="calls", hold=0.4):
    """A filer that claimed, filed and recorded its request, then died before waiting on it."""
    from datetime import timedelta

    L._claim(q.directory, "claude_code", "s1", kind, time.time())
    call = L.limit_call("claude_code", "s1", kind, 3, 2)
    r = q.file(agent="claude_code", agent_session_id="s1", tool_name=L.LIMIT_TOOL, tool_input=call, cwd="/work",
               hold=timedelta(seconds=hold), tier=L.LIMIT_TIER)
    L._record_filed(q.directory, "claude_code", "s1", kind, r.id, "crossed", time.time())
    return r


def test_a_dead_filers_request_is_resolved_by_the_next_call(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    r = _dead_filer(q)
    time.sleep(0.5)
    out = _check(q, 3)                         # crosses, joins the dead filer's request, resolves it: gap #1 -> halt
    assert not out.go and out.stop and "gap #1" in out.text
    assert q.get(r.id)[1] == "undecided"
    _answer_when_filed(q, keys["phone"], "allow", late=True).join()
    assert _check(q, 4).go                     # and the operator's late Extend on it lifts the halt


def test_a_dead_filers_answered_extend_is_not_lost(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    r = _dead_filer(q, hold=30)
    payload, sig = signed(q, r, keys["phone"], "allow")
    q.answer(r.id, payload, sig)
    out = _check(q, 3)
    assert out.go and out.extended == "calls"
    conn = L._connect(q.directory)
    assert conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0] == 1   # no second request, no second ping
    conn.close()


def test_a_halt_survives_the_store_failing(tmp_path, keys, authority, monkeypatch):
    import sqlite3

    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    t = _answer_when_filed(q, keys["phone"], "deny")
    _check(q, 3)
    t.join()

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(L, "_count", locked)
    out = _check(q, 4)
    assert not out.go and out.stop


def test_a_late_ruling_on_a_limit_reads_as_extend_or_end(tmp_path, keys, authority, monkeypatch):
    from levain.consent.adapters import claude_code, codex

    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    _check(q, 3, hold=0.4)
    _answer_when_filed(q, keys["phone"], "allow", late=True).join()
    text = claude_code.ruling_context(q, "s1")
    assert "EXTENDED this session's tool calls limit to 4 (gap #1)" in text and "Do «" not in text
    q2 = ConsentQueue(tmp_path / "h2" / "consent", authority=authority, home=h4_home())
    for i in (1, 2):
        L.check("codex", {"tool_use_id": f"c{i}"}, q2, "s9", "shell", {"c": i}, hold=0.4, notify=lambda _t: None,
                cwd="/w")
    L.check("codex", {"tool_use_id": "c3"}, q2, "s9", "shell", {"c": 3}, hold=0.4, notify=lambda _t: None, cwd="/w")
    rid = q2.undecided_for("codex")[0].request.id
    payload = q2.ruling_payload(rid, "deny", keys["phone"].fp)
    q2.answer_late(rid, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    assert "ENDED this session" in codex.ruling_context(q2, "s9")
    from levain.consent import delivery

    assert "stays stopped" in delivery.plan(q2, rid).reason


# --- L3 r1 findings ------------------------------------------------------------------------------------------------

def test_a_late_ruling_after_a_dead_filers_mark_is_applied(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    r = _dead_filer(q)
    time.sleep(0.5)
    q.mark_undecided(r.id)                       # the filer marked the gap, then died before halting
    payload = q.ruling_payload(r.id, "allow", keys["phone"].fp)
    q.answer_late(r.id, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    out = _check(q, 3)
    assert out.go and out.extended == "calls"


def test_a_stale_end_is_not_told_after_an_extend_and_codex_hears_a_live_end(tmp_path, keys, authority, monkeypatch):
    from levain.consent.adapters import claude_code, codex

    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    _check(q, 3, hold=0.4)                       # silence: halted on gap #1
    rid = q.undecided_for("claude_code")[0].request.id
    payload = q.ruling_payload(rid, "deny", keys["phone"].fp)
    q.answer_late(rid, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    eid, _call = L.file_extend(q, "claude_code", "s1", hold=30, cwd="/work")
    p2, s2 = signed(q, q.get(eid)[0], keys["phone"], "allow")
    q.answer(eid, p2, s2)
    assert _check(q, 4).go
    assert not (claude_code.ruling_context(q, "s1") or "")   # the End on gap #1 was superseded: not told
    q2 = ConsentQueue(tmp_path / "h2" / "consent", authority=authority, home=h4_home())
    run = lambda i: codex.gate({"hook_event_name": "PreToolUse", "session_id": "s9", "tool_use_id": f"c{i}",  # noqa: E731
                                "tool_name": "Read", "tool_input": {"file_path": f"/w/{i}"}, "cwd": "/w"}, q2,
                               codex.AdapterSettings(gate_hold_seconds=0.4), environ=h4_env(), home=h4_home())
    run(1), run(2), run(3)
    rid2 = q2.undecided_for("codex")[0].request.id
    payload = q2.ruling_payload(rid2, "deny", keys["phone"].fp)
    q2.answer_late(rid2, payload, keys["phone"].sign(ruling_message(payload), RULING_NAMESPACE))
    out = run(4)
    spec = out["hookSpecificOutput"]
    assert spec["permissionDecision"] == "deny" and "ENDED this session" in spec.get("additionalContext", "")
    assert "continue" not in out


def test_a_late_allow_that_itself_lifted_the_halt_still_plans_its_resume(tmp_path, keys, authority, monkeypatch):
    from levain.consent import delivery

    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    _check(q, 3, hold=0.4)                                   # silence: halted on r1's gap
    rid = q.undecided_for("claude_code")[0].request.id
    _answer_when_filed(q, keys["phone"], "allow", late=True).join()
    assert L.release_if_extended(q, "claude_code", "s1")     # r1's own allow lifts it (as the gate would)
    assert L.extended_by(q.directory, rid)
    assert "already extended" not in delivery.plan(q, rid).reason


# --- the parallel-crossing race (1020+52): a hold filed, answered and cleared between a crossing's count and its look --

def _crossed(q, i, session):
    v = L.count_call(q.directory, "claude_code", session, {"tool_use_id": f"t{i}"}, tool_name="Read",
                     tool_input={"file_path": f"/x/{i}"})
    assert v.state == "crossed"
    return v


def _requests(q):
    conn = L._connect(q.directory)
    try:
        return conn.execute("SELECT COUNT(*) FROM requests").fetchone()[0]
    finally:
        conn.close()


def _hold(q, v, session, sleep=time.sleep):
    return L._hold("claude_code", q, q.directory, L.session_root(session), v.kind, v, hold=2.0,
                   notify=lambda _t: None, resume=None, cwd="/work", sleep=sleep, now=time.time)


def test_a_crossing_counted_before_another_crossings_extend_joins_it(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    v = _crossed(q, 10, "s1#agent:a1")                 # counted while the limit is 2 ...
    t = _answer_when_filed(q, keys["phone"], "allow")
    assert _check(q, 11, session="s1#agent:a0").go     # ... then a0 crosses, is extended, and its hold is cleared
    t.join()
    out = _hold(q, v, "s1#agent:a1")                   # a1's claim finds no hold: it must join a0's Extend
    assert out.go and out.extended == "calls" and _requests(q) == 1


def test_a_joiner_whose_hold_is_answered_and_cleared_between_polls_goes_on(tmp_path, keys, authority, monkeypatch):
    from datetime import timedelta

    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    v = _crossed(q, 10, "s1#agent:a1")
    assert L._claim(q.directory, "claude_code", "s1", "calls", time.time()) == (True, None)   # a0 claimed, not filed

    def a0_files_and_is_extended(_s):                  # all of it inside one of a1's join polls
        if _requests(q):
            return
        call = L.limit_call("claude_code", "s1", "calls", 3, 2)
        r = q.file(agent="claude_code", agent_session_id="s1", tool_name=L.LIMIT_TOOL, tool_input=call, cwd="/work",
                   hold=timedelta(seconds=30), tier=L.LIMIT_TIER)
        L._record_filed(q.directory, "claude_code", "s1", "calls", r.id, "crossed", time.time())
        payload, sig = signed(q, r, keys["phone"], "allow")
        q.answer(r.id, payload, sig)
        assert L._resolve("claude_code", q, q.directory, "s1", "calls", r.id, sleep=time.sleep, now=time.time).go
        assert L._hold_id(q.directory, "claude_code", "s1", "calls") is None   # cleared

    out = _hold(q, v, "s1#agent:a1", sleep=a0_files_and_is_extended)
    assert out.go and out.extended == "calls" and _requests(q) == 1


def test_a_halt_landing_after_the_count_stops_the_late_crossing_too(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    v = _crossed(q, 10, "s1#agent:a1")
    t = _answer_when_filed(q, keys["phone"], "deny")
    assert not _check(q, 11, session="s1#agent:a0").go  # a0's crossing is Ended: the root halts
    t.join()
    out = _hold(q, v, "s1#agent:a1")
    assert not out.go and out.stop and "operator chose End" in out.text and _requests(q) == 1


def test_an_extend_does_not_carry_a_late_crossing_past_a_newer_end(tmp_path, keys, authority, monkeypatch):
    q = _q(tmp_path, authority)                         # L3 r1 (codex HIGH, complement MED): the root's halt wins
    _check(q, 1, monkeypatch=monkeypatch), _check(q, 2)
    v = _crossed(q, 10, "s1#agent:a1")                 # call 3, counted at the limit of 2
    t = _answer_when_filed(q, keys["phone"], "allow")
    assert _check(q, 11, session="s1#agent:a0").go     # call 4: Extend to 4
    t.join()
    t = _answer_when_filed(q, keys["phone"], "deny")
    assert not _check(q, 12, session="s1#agent:a0").go  # call 5: End, the root halts
    t.join()
    out = _hold(q, v, "s1#agent:a1")                   # a1's settled Extend is older than the End
    assert not out.go and out.stop and "STOPPED by a budget" in out.text and "operator chose End" in out.text
