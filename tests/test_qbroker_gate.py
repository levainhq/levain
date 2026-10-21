"""F10 S2b-2: the GATE verb (levain.qbroker.broker, module docstring): the halt latch and the limits in a slot,
asked by the slot gate on every PreToolUse. The real-socket run is project_memory/1020-f11/s2b2_run/RUN.txt."""
from __future__ import annotations

from datetime import timedelta

import pytest

from levain.consent import broker_client
from levain.consent.broker_client import BrokerError
from levain.qbroker import broker as qbroker
from levain.qbroker.receipts import Receipts
from levain.qbroker.seam import request_of
import tests.test_qbroker as base
from tests.test_qbroker import NOW, filed, hook_fields, make_broker, row_of, write_answer, write_signers
from tests.test_consent_broker_client import Scripted

L, broker, clock, keys, resolver = base.L, base.broker, base.clock, base.keys, base.resolver

_ids = iter(range(1, 10**6))


def gate(b, slot=1, cap="cap-a", **over):
    msg = {"v": 1, "op": "GATE", "id": next(_ids), "cap": cap, "agent": "claude_code", "tool_name": "Read",
           "input_digest": "sha256:" + "0" * 64, **over}
    return b.handle(slot, {k: v for k, v in msg.items() if v is not None})


def states(L, rid):
    return [s for s, in Receipts(L / "queue")._connect().execute(
        "SELECT state FROM receipts WHERE request_id = ? ORDER BY seq", (rid,))]


def t3(b, event="t3"):
    return filed(b, fields=hook_fields(event=event, tier="T3"))


def test_a_session_with_nothing_filed_is_go(broker):
    got = gate(broker)
    assert got["ok"] is True and set(got) == {"id", "ok", "gate"} and got["gate"] == "go"


def test_an_unknown_cap_is_refused(broker):
    assert gate(broker, cap="nope")["refused"] == "cap-unknown"


@pytest.mark.parametrize("over", [{"agent": "someone"}, {"tool_name": ""}, {"input_digest": 7},
                                  {"tool_use_id": 3}, {"extra": 1}])
def test_a_frame_out_of_shape_is_refused(broker, over):
    assert gate(broker, **over)["refused"] == "frame-malformed"


def test_a_frame_missing_a_field_is_refused(broker):
    assert gate(broker, agent=None)["refused"] == "frame-malformed"


def test_an_irreversible_row_halts_its_session_once_its_hold_ends_unanswered(L, broker, clock):
    rid = t3(broker)
    assert gate(broker)["gate"] == "go"                        # still held: the session's other calls go on
    clock["now"] = NOW + timedelta(minutes=5)
    got = gate(broker)
    assert got["gate"] == "halted" and got["request_id"] == rid
    assert states(L, rid) == ["pending", "expired"]            # GATE ran ANSWER?'s step past the hold
    assert gate(broker, cap="cap-a2")["gate"] == "go"          # another session is not halted
    assert gate(broker)["gate"] == "halted"


def test_the_halt_is_derived_from_the_rows_and_survives_a_restart(L, broker, resolver, clock):
    rid = t3(broker)
    clock["now"] = NOW + timedelta(minutes=6)
    again = make_broker(L, resolver, clock)
    got = gate(again)
    assert got["gate"] == "halted" and got["request_id"] == rid


def test_a_reversible_row_never_halts(L, broker, clock):
    filed(broker, fields=hook_fields(event="t2", tier="T2"))
    clock["now"] = NOW + timedelta(minutes=6)
    assert gate(broker)["gate"] == "go"


def test_an_irreversible_row_answered_in_time_does_not_halt(L, broker, keys, clock):
    write_signers(L, keys, 2, [keys["operator"].signer(key_class="laptop-presence", custody="passphrase")])
    rid = t3(broker)
    write_answer(L, rid, keys["operator"], decision="deny", signed_at=NOW + timedelta(minutes=4))
    clock["now"] = NOW + timedelta(minutes=5, seconds=1)
    assert gate(broker)["gate"] == "go"                        # read past the hold, as ANSWER? reads it
    assert states(L, rid) == ["pending", "denied"]
    assert broker._halting["sess-a"] == {rid}                  # decided on this GATE: read again on the next
    assert gate(broker)["gate"] == "go"
    assert "sess-a" not in broker._halting                     # F4: settled, it leaves the index


def test_the_halting_index_keeps_pending_and_expired_and_drops_settled(L, broker, keys, clock):
    write_signers(L, keys, 2, [keys["operator"].signer(key_class="laptop-presence", custody="passphrase")])
    settled, held = t3(broker, "s1"), t3(broker, "s2")
    write_answer(L, settled, keys["operator"], decision="allow", signed_at=NOW + timedelta(minutes=1))
    base.ask(broker, settled)                                  # decided in its hold: the receipt says allowed
    assert gate(broker)["gate"] == "go"
    assert broker._halting["sess-a"] == {held}                 # the pending one stays
    clock["now"] = NOW + timedelta(minutes=6)
    assert gate(broker)["request_id"] == held
    assert broker._halting["sess-a"] == {held}                 # expired stays: it halts


def test_a_late_answer_is_refused_and_the_halt_stands(L, broker, keys, clock):
    """KNOWN EDGE (module docstring): the broker has no late-ruling path yet."""
    write_signers(L, keys, 2, [keys["operator"].signer(key_class="laptop-presence", custody="passphrase")])
    rid = t3(broker)
    clock["now"] = NOW + timedelta(minutes=6)
    assert gate(broker)["gate"] == "halted"
    write_answer(L, rid, keys["operator"], signed_at=NOW + timedelta(minutes=6))
    assert gate(broker)["gate"] == "halted"


def test_a_decided_receipt_after_the_expiry_releases_the_halt(L, broker, keys, clock):
    """The release rule: a ruling on the halting request, read as the LAST receipt state (what a late-ruling path
    will append); nothing in GATE changes when it lands."""
    rid = t3(broker)
    clock["now"] = NOW + timedelta(minutes=6)
    assert gate(broker)["gate"] == "halted"
    request = request_of(row_of(L, rid)["request"])
    with broker._receipts._write() as conn:
        Receipts._append(conn, request, "denied", "2026-01-01T00:00:00Z", keys["operator"].fp)
    assert gate(broker)["gate"] == "go"


def limited(L, resolver, clock, value, seen=None):
    def limits(agent, session):
        if seen is not None:
            seen.append((agent, session))
        return value
    return make_broker(L, resolver, clock, limits=limits)


def test_no_limits_configured_is_go_and_counts_nothing(L, broker):
    for _ in range(5):
        assert gate(broker)["gate"] == "go"
    assert not (L / "queue" / "requests" / qbroker.LIMITS_DIR).exists()


def test_a_callable_answering_none_is_go(L, resolver, clock):
    b = limited(L, resolver, clock, None)
    assert [gate(b)["gate"] for _ in range(3)] == ["go"] * 3


def test_a_session_over_its_limit_is_over_limit(L, resolver, clock):
    seen = []
    b = limited(L, resolver, clock, {"calls": 2}, seen)
    assert [gate(b)["gate"] for _ in range(2)] == ["go", "go"]
    got = gate(b)
    assert got["gate"] == "over-limit" and got["kind"] == "calls"
    assert set(seen) == {("claude_code", "sess-a")}            # the resolved session, never the frame's
    assert gate(b, cap="cap-a2")["gate"] == "go"               # another session has its own count
    d = L / "queue" / "requests" / qbroker.LIMITS_DIR
    assert (d.stat().st_mode & 0o777) == 0o700 and (d / "limits.db").is_file()


def test_calls_denied_over_limit_are_not_counted_so_a_raised_limit_is_not_spent(L, resolver, clock):
    # F6 (s2b2_fix1_run/RUN_before.txt: calls=2, 3 denied, raised to 3, the next call still denied)
    lim = {"calls": 2}
    b = make_broker(L, resolver, clock, limits=lambda a, s: dict(lim))
    assert [gate(b)["gate"] for _ in range(5)] == ["go", "go"] + ["over-limit"] * 3
    lim["calls"] = 3
    assert [gate(b)["gate"] for _ in range(2)] == ["go", "over-limit"]   # the third fits, the fourth crosses


def test_the_local_counter_still_counts_the_crossing_call(tmp_path):
    # count_call's default is unchanged: the local gate's crossing call is counted (it halts there, then holds)
    from levain.consent import limits as consent_limits
    q = tmp_path / "q"
    q.mkdir(mode=0o700)
    full = {k: float("inf") for k in consent_limits.KINDS} | {"calls": 1.0}
    kw = {"tool_name": "Read", "tool_input": {}, "limits": full}
    assert consent_limits.count_call(q, "codex", "s", {}, **kw).state == "ok"
    got = consent_limits.count_call(q, "codex", "s", {}, **kw)
    assert got.state == "crossed" and got.count == 2


def test_a_repeated_tool_use_id_counts_once(L, resolver, clock):
    b = limited(L, resolver, clock, {"calls": 1})
    assert [gate(b, tool_use_id="call-1")["gate"] for _ in range(3)] == ["go"] * 3
    assert gate(b, tool_use_id="call-2")["gate"] == "over-limit"


def test_the_halt_is_read_before_the_count(L, resolver, clock):
    b = limited(L, resolver, clock, {"calls": 1})
    rid = t3(b)
    clock["now"] = NOW + timedelta(minutes=6)
    assert [gate(b)["request_id"] for _ in range(3)] == [rid] * 3   # halted, nothing counted
    assert not (L / "queue" / "requests" / qbroker.LIMITS_DIR / "limits.db").exists()


@pytest.mark.parametrize("spoil", ["file", "symlink", "wide"])
def test_an_unusable_limits_directory_fails_closed_and_logs_once(L, resolver, clock, caplog, tmp_path, spoil):
    # F2 (s2b2_fix1_run/RUN_before.txt: a file at requests/limits answered go on every call, counting nothing)
    d = L / "queue" / "requests" / qbroker.LIMITS_DIR
    if spoil == "file":
        d.write_text("not a dir\n")
    elif spoil == "symlink":
        (tmp_path / "elsewhere").mkdir(mode=0o700)
        d.symlink_to(tmp_path / "elsewhere")
    else:
        d.mkdir(mode=0o700)
        d.chmod(0o755)
    b = limited(L, resolver, clock, {"calls": 2})
    got = [gate(b) for _ in range(4)]
    assert [g["gate"] for g in got] == ["limits-failed"] * 4
    assert set(got[0]) == {"id", "ok", "gate", "reason"} and got[0]["reason"] == qbroker.LIMITS_DIR_FAILED
    assert sum("limits directory" in r.getMessage() for r in caplog.records) == 1
    assert [gate(make_broker(L, resolver, clock))["gate"]] == ["go"]   # no limits configured: never asked


def test_a_counter_past_its_failure_bound_is_limits_failed_not_over_limit(L, resolver, clock):
    # F3: the counter's fail_deny (kind None) was sent over-limit and rendered as a budget "reached its a limit"
    from levain.consent import limits as consent_limits
    d = L / "queue" / "requests" / qbroker.LIMITS_DIR
    d.mkdir(mode=0o700)
    (d / consent_limits.DB_NAME).mkdir()                 # a store that can never open
    b = limited(L, resolver, clock, {"calls": 100})
    got = [gate(b) for _ in range(consent_limits.FAIL_DENY_AFTER)]
    assert [g["gate"] for g in got[:-1]] == ["go"] * (consent_limits.FAIL_DENY_AFTER - 1)   # bounded fail-open
    assert got[-1]["gate"] == "limits-failed" and "limits store" in got[-1]["reason"]
    text = broker_client.gate_check(Scripted([{"gate": "limits-failed", "reason": got[-1]["reason"]}]),
                                    agent="claude_code", tool_name="Read", tool_input={}, tool_use_id=None)
    assert "not a budget" in text and "limits.db" in text and "reached its" not in text


def test_parallel_gates_draw_their_own_bucket_never_answer_s(L, resolver, clock):
    # F5 (s2b2_fix1_run/RUN_before.txt: a parallel batch past ANSWER_BURST was refused rate-limited and denied)
    import threading
    b = make_broker(L, resolver, clock, monotonic=lambda: 0.0)        # no refill: the burst is all there is
    n = qbroker.ANSWER_BURST * 2
    assert n <= qbroker.GATE_BURST
    outs, barrier = [None] * n, threading.Barrier(n)

    def one(i):
        barrier.wait()
        outs[i] = gate(b)
    ts = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert [o.get("gate") for o in outs] == ["go"] * n
    assert base.ask(b, "pend-none")["refused"] != "rate-limited"     # ANSWER?'s bucket was not drawn
    rest = [gate(b) for _ in range(qbroker.GATE_BURST - n + 1)]
    assert rest[-1]["refused"] == "rate-limited" and all(r["gate"] == "go" for r in rest[:-1])


def test_a_receipts_read_that_fails_is_a_broker_error(L, broker, clock):
    t3(broker)
    clock["now"] = NOW + timedelta(minutes=6)

    def boom(*a):
        raise RuntimeError("receipts down")
    broker._receipts.state = boom
    assert gate(broker)["refused"] == "broker-error"


# --- the client --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("answer,want", [({"gate": "go"}, ("go", None)),
                                         ({"gate": "halted", "request_id": "pend-x"}, ("halted", "pend-x")),
                                         ({"gate": "over-limit", "kind": "calls"}, ("over-limit", "calls")),
                                         ({"gate": "limits-failed", "reason": "r"}, ("limits-failed", "r"))])
def test_the_client_reads_the_four_answers(answer, want):
    assert Scripted([answer]).gate(agent="claude_code", tool_name="Read", tool_input={}, tool_use_id=None) == want


@pytest.mark.parametrize("answer", [{}, {"gate": "allow"}, {"gate": "go", "x": 1}, {"gate": "halted"},
                                    {"gate": "over-limit"}, {"gate": "halted", "request_id": 1}, "broker-error",
                                    {"gate": "limits-failed"}, {"gate": "limits-failed", "reason": 3}])
def test_any_other_gate_answer_raises(answer):
    with pytest.raises(BrokerError):
        Scripted([answer]).gate(agent="claude_code", tool_name="Read", tool_input={}, tool_use_id=None)


def test_gate_check_texts_and_one_retry():
    kw = {"agent": "codex", "tool_name": "Bash", "tool_input": {}, "tool_use_id": None}
    assert broker_client.gate_check(Scripted([{"gate": "go"}]), **kw) is None
    assert "halted on request pend-x" in broker_client.gate_check(
        Scripted([{"gate": "halted", "request_id": "pend-x"}]), **kw)
    assert "tool calls limit" in broker_client.gate_check(Scripted([{"gate": "over-limit", "kind": "calls"}]), **kw)
    failed = broker_client.gate_check(Scripted([{"gate": "limits-failed", "reason": "disk full"}]), **kw)
    assert "disk full" in failed and "limits.db" in failed and "not a budget" in failed
    sleeps = []
    assert broker_client.gate_check(Scripted(["rate-limited", "rate-limited", {"gate": "go"}]),
                                    sleep=sleeps.append, **kw) is None
    assert sleeps == list(broker_client.GATE_BACKOFF[:2])
    sleeps.clear()
    n = len(broker_client.GATE_BACKOFF)
    with pytest.raises(BrokerError):     # bounded: one more refusal than there are pauses denies
        broker_client.gate_check(Scripted(["rate-limited"] * (n + 1)), sleep=sleeps.append, **kw)
    assert sleeps == list(broker_client.GATE_BACKOFF) and sum(sleeps) < 2.0
    with pytest.raises(BrokerError):     # only rate-limited is retried
        broker_client.gate_check(Scripted(["broker-error", {"gate": "go"}]), sleep=sleeps.append, **kw)
