"""F10 S2a: the consent hook's client for the queue broker, in a slot session (levain.consent.broker_client and both
adapters' slot path). Built from the cases of project_memory/1020-f11/f10s2a_run/RUN.txt, against a real Broker and
a real SlotListener (the real peer-uid check, this uid) on a short /tmp socket, a fake CONTROL resolver, and a real
answer-signer snapshot (real ssh-keygen keys, signed by a real trust key). Holds are short here; the run held the
real DEFAULT_GATE_HOLD.
"""
from __future__ import annotations

import io
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.consent import broker_client
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
from levain.consent.broker_client import BrokerClient, BrokerError
from levain.qbroker.broker import Broker, Resolved
from levain.qbroker.server import SlotListener
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import QueueStore
from tests.test_qbroker import FakeResolver, Key, bound_gid, write_answer, write_signers

CAP = "cap-slot-1"
SESSION = Resolved(session="slot1-session", kind="interactive", run=None)
HOLD = 1.5


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("bckeys")
    return {n: Key(d, n) for n in ("trust", "operator", "stranger")}


@pytest.fixture
def L(tmp_path, keys):
    for d in ("queue/requests", "queue/answers", "policy", "routes"):
        (tmp_path / "L" / d).mkdir(parents=True)
    L = tmp_path / "L"
    (L / "routes" / "trust.pub").write_text(f"{keys['trust'].type} {keys['trust'].b64} trust\n")
    # a gate request is T3: only a laptop-presence key covers it
    write_signers(L, keys, 1, [keys["operator"].signer(key_class="laptop-presence", custody="passphrase")])
    return L


@pytest.fixture
def ws(tmp_path):
    w = tmp_path / "ws"
    (w / ".claude" / "agents").mkdir(parents=True)
    (w / ".codex").mkdir()
    return w


@pytest.fixture
def slot(L, monkeypatch, tmp_path):
    """A live broker on a real socket; the hook's environment names it."""
    run_dir = Path(tempfile.mkdtemp(prefix="bc", dir="/tmp"))
    resolver = FakeResolver()
    resolver.add(1, CAP, SESSION)
    broker = Broker(QueueStore(L / "queue"), resolver, SignersSource(L / "policy", L / "routes" / "trust.pub"))
    lst = SlotListener(broker, 1, run_dir / "q.sock", os.getuid(), expected_gid=bound_gid(run_dir))
    lst.start()
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home" / ".levain"))
    monkeypatch.setenv(broker_client.SOCKET_ENV, str(lst.path))
    monkeypatch.setenv(broker_client.CAP_ENV, CAP)
    try:
        yield lst
    finally:
        lst.stop()
        shutil.rmtree(run_dir, ignore_errors=True)


def cc_event(ws, path=".claude/agents/probe2.md", tool_use_id="call_1", **over):
    e = {"session_id": "9032d40e-aacc-46e7-82f1-23badb9c8cbf", "cwd": str(ws), "permission_mode": "default",
         "hook_event_name": "PreToolUse", "tool_name": "Write",
         "tool_input": {"file_path": str(ws / path), "content": "probe two"}, "tool_use_id": tool_use_id}
    e.update(over)
    return e


def cx_event(ws, tool_use_id="call_9", gated=True, **over):
    e = {"session_id": "01a1552b-22cb-7991-aa86-fbe69a523742", "cwd": str(ws), "hook_event_name": "PreToolUse",
         "permission_mode": "default", "tool_use_id": tool_use_id}
    if gated:
        e.update(tool_name="apply_patch",
                 tool_input={"command": "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch\n"})
    else:
        e.update(tool_name="Bash", tool_input={"command": "git push origin HEAD:refs/heads/main"})
    e.update(over)
    return e


def rows(L):
    return sorted(p.stem for p in (L / "queue" / "requests").glob("pend-*.json"))


def answering(L, key, decision="allow"):
    """A ``sleep`` for the hold that writes the answer on its first call (the hook is then polling)."""
    state = {"done": False}

    def sleep(s):
        if not state["done"]:
            state["done"] = True
            (rid,) = rows(L)
            write_answer(L, rid, key, decision=decision, signed_at=datetime.now(timezone.utc))
        time.sleep(s)
    return sleep


def gate_of(adapter):
    return cc.slot_gate if adapter == "cc" else cx.slot_gate


def event_of(adapter, ws, **kw):
    return cc_event(ws, **kw) if adapter == "cc" else cx_event(ws, **kw)


def kw_of(adapter, tmp_path):
    return {"home": tmp_path / "home"} if adapter == "cc" else {"home": tmp_path / "home",
                                                                 "codex_dir": tmp_path / "home" / ".codex"}


# --- the run's cases -------------------------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_gated_call_files_a_row_and_an_enrolled_allow_allows(adapter, L, slot, ws, keys, tmp_path):
    out = gate_of(adapter)(event_of(adapter, ws), hold=HOLD, sleep=answering(L, keys["operator"]),
                           **kw_of(adapter, tmp_path))
    (rid,) = rows(L)
    row = QueueStore(L / "queue").read_request(rid)
    assert (row["session"], row["slot"], row["request"]["tier"]) == ("slot1-session", {"class": "egress", "index": 1},
                                                                      "T3")
    assert row["request"]["event_id"] == ("hook:call_1" if adapter == "cc" else "hook:call_9")
    if adapter == "cc":
        assert out == cc._pre("allow", f"levain: allowed by {cc.SLOT_HOW}.")
    else:
        assert out == cx.decision_output("allow", cc.SLOT_HOW)
        assert "permissionDecision" not in out["hookSpecificOutput"]   # Codex refuses an explicit allow


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_deny_answer_denies(adapter, L, slot, ws, keys, tmp_path):
    out = gate_of(adapter)(event_of(adapter, ws), hold=HOLD, sleep=answering(L, keys["operator"], "deny"),
                           **kw_of(adapter, tmp_path))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert out["hookSpecificOutput"]["permissionDecisionReason"] == (
        f"levain: denied by {cc.SLOT_HOW}. Do not retry it and do not achieve it another way.")
    assert "continue" not in out


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_an_answer_by_a_key_not_enrolled_holds_to_the_end_and_is_undecided(adapter, L, slot, ws, keys, tmp_path):
    started = time.monotonic()
    out = gate_of(adapter)(event_of(adapter, ws), hold=HOLD, sleep=answering(L, keys["stranger"]),
                           **kw_of(adapter, tmp_path))
    assert time.monotonic() - started >= HOLD - 0.1          # a refused answer never ends the hold early
    (rid,) = rows(L)
    what = (cc._what("Write", {"file_path": str(ws / ".claude/agents/probe2.md"), "content": "probe two"})
            if adapter == "cc" else "apply_patch of .codex/hooks.json")
    want = cc.undecided_output("irreversible", what, rid) if adapter == "cc" else \
        cx.undecided_output("irreversible", what, rid)
    assert out == want
    assert (out.get("continue") is False) == (adapter == "cc")   # Codex: no hard stop (its latch is a local queue's)


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_no_answer_at_all_is_undecided_at_the_hold(adapter, L, slot, ws, tmp_path):
    out = gate_of(adapter)(event_of(adapter, ws), hold=0.6, **kw_of(adapter, tmp_path))
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_the_broker_stopped_denies_a_gated_call_and_passes_an_ungated_one(adapter, L, slot, ws, tmp_path):
    slot.stop()
    out = gate_of(adapter)(event_of(adapter, ws), hold=HOLD, **kw_of(adapter, tmp_path))
    if adapter == "cc":
        what = cc._what("Write", {"file_path": str(ws / ".claude/agents/probe2.md"), "content": "probe two"})
        assert out == cc.could_not_run("irreversible", what) and out["continue"] is False
    else:
        assert out == cx.could_not_run("apply_patch of .codex/hooks.json")
    ungated = cc_event(ws, path="notes.txt") if adapter == "cc" else cx_event(ws, gated=False)
    assert gate_of(adapter)(ungated, hold=HOLD, **kw_of(adapter, tmp_path)) is None


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_an_ungated_call_gets_no_output_and_files_nothing(adapter, L, slot, ws, tmp_path):
    ungated = cc_event(ws, path="notes.txt") if adapter == "cc" else cx_event(ws, gated=False)
    assert gate_of(adapter)(ungated, hold=HOLD, **kw_of(adapter, tmp_path)) is None
    assert rows(L) == []


# --- main(): the slot path vs the local one ----------------------------------------------------------------------

def run_main(module, event):
    out = io.StringIO()
    assert module.main([], stdin=io.StringIO(json.dumps(event)), stdout=out) == 0
    return out.getvalue()


@pytest.mark.parametrize("module", [cc, cx])
def test_main_in_a_slot_opens_no_local_queue(module, L, slot, ws, tmp_path, monkeypatch):
    monkeypatch.setattr(module, "_queue_for", lambda *a: pytest.fail("a local ConsentQueue in a slot"))
    ev =cc_event(ws, path="notes.txt") if module is cc else cx_event(ws, gated=False)
    assert run_main(module, ev) == ""
    assert not (tmp_path / "home" / ".levain" / "consent").exists()


@pytest.mark.parametrize("module", [cc, cx])
def test_a_permission_request_in_a_slot_gets_no_decision(module, L, slot, ws, monkeypatch):
    monkeypatch.setattr(module, "_queue_for", lambda *a: pytest.fail("a local ConsentQueue in a slot"))
    ev = cc_event(ws) if module is cc else cx_event(ws)
    ev["hook_event_name"] = "PermissionRequest"
    assert run_main(module, ev) == ""
    assert rows(L) == []


@pytest.mark.parametrize("module", [cc, cx])
def test_main_in_a_slot_denies_a_gated_call_when_the_broker_is_down(module, L, slot, ws):
    slot.stop()
    got = json.loads(run_main(module, cc_event(ws) if module is cc else cx_event(ws)))
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "the gate could not run" in got["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize("module", [cc, cx])
def test_main_without_the_socket_variable_takes_the_local_path(module, ws, tmp_path, monkeypatch):
    monkeypatch.delenv(broker_client.SOCKET_ENV, raising=False)
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(module, "slot_gate", lambda *a, **k: pytest.fail("the slot path without the variable"))
    ev = (cc_event(ws) if module is cc else cx_event(ws))
    ev["permission_mode"] = "bypassPermissions"
    got = json.loads(run_main(module, ev))
    # the local floor (no enrolled key), as before S2a
    assert "no enrolled key can approve this change" in got["hookSpecificOutput"]["permissionDecisionReason"]
    assert (tmp_path / "lh" / "consent").is_dir()


# --- fail closed --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_settings_file_naming_the_socket_variable_denies_a_gated_call(adapter, L, slot, ws, tmp_path):
    (ws / ".claude" / "settings.json").write_text(json.dumps({"env": {"LEVAIN_QUEUE_SOCKET": "/tmp/evil.sock"}}))
    out = gate_of(adapter)(event_of(adapter, ws), hold=HOLD, **kw_of(adapter, tmp_path))
    assert "the gate could not run" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert rows(L) == []   # nothing was sent to the socket


@pytest.mark.parametrize("env,name", [({}, "no-socket"), ({broker_client.SOCKET_ENV: "rel/q.sock"}, "no-socket"),
                                      ({broker_client.SOCKET_ENV: "/tmp/q.sock"}, "no-cap"),
                                      ({broker_client.SOCKET_ENV: "/tmp/q.sock", broker_client.CAP_ENV: ""},
                                       "no-cap")])
def test_from_env_refuses_a_missing_or_relative_handle(env, name):
    with pytest.raises(BrokerError) as exc:
        BrokerClient.from_env(env)
    assert exc.value.name == name


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_slot_with_no_cap_denies(adapter, L, slot, ws, tmp_path, monkeypatch):
    monkeypatch.delenv(broker_client.CAP_ENV)
    out = gate_of(adapter)(event_of(adapter, ws), hold=HOLD, **kw_of(adapter, tmp_path))
    assert "the gate could not run" in out["hookSpecificOutput"]["permissionDecisionReason"]


def test_a_cap_the_broker_does_not_know_is_refused_and_denies(L, slot, ws, tmp_path):
    out = cc.slot_gate(cc_event(ws), hold=HOLD, home=tmp_path / "home",
                       client=BrokerClient(str(slot.path), "cap-unknown-to-control"))
    assert out["continue"] is False and "the gate could not run" in out["hookSpecificOutput"][
        "permissionDecisionReason"]


class Scripted(BrokerClient):
    def __init__(self, answers):
        super().__init__("/nonexistent", "cap")
        self.answers = list(answers)

    def call(self, op, **fields):
        got = self.answers.pop(0)
        if isinstance(got, str):
            raise BrokerError(got)
        return {"id": 1, "ok": True, **got}


@pytest.mark.parametrize("answer", [{}, {"decision": "maybe"}, {"pending": False}, {"decision": "allow", "x": 1},
                                    {"pending": True, "decision": "allow"}, "no-such-request", "broker-error"])
def test_an_answer_out_of_shape_or_refused_raises(answer):
    with pytest.raises(BrokerError):
        Scripted([answer]).answer("pend-x")


def test_pending_and_the_pending_refusals_keep_the_hold_polling():
    client = Scripted([{"pending": True}, "rate-limited", "answer-refused", {"decision": "deny"}])
    assert broker_client.hold_for_decision(client, "pend-x", 10.0, poll=0.0, sleep=lambda s: None) == "deny"


def test_the_brokers_expired_ends_the_hold_undecided():
    client = Scripted([{"pending": True}, "request-expired"])
    assert broker_client.hold_for_decision(client, "pend-x", 10.0, poll=0.0, sleep=lambda s: None) is None


def test_a_request_answered_for_another_id_is_malformed(ws):
    request = broker_client.gate_request(agent="claude_code", session="s", tool_name="Write", tool_input={},
                                         cwd=str(ws), event_id="e", tier="T3", hold=5)
    with pytest.raises(BrokerError) as exc:
        Scripted([{"request_id": "pend-other", "queue_id": "q"}]).request(request)
    assert exc.value.name == "malformed"


def test_a_frame_answering_another_id_is_malformed(L, slot):
    import socket
    import threading

    from levain.qbroker import wire

    path = Path(tempfile.mkdtemp(prefix="bcs", dir="/tmp")) / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def serve():
        conn, _ = srv.accept()
        with conn:
            wire.read_frame(conn, timeout=5)
            wire.write_frame(conn, {"id": 99, "ok": True, "pending": True})
    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        with pytest.raises(BrokerError) as exc:
            BrokerClient(str(path), "cap").answer("pend-x")
        assert exc.value.name == "malformed"
    finally:
        t.join(5)
        srv.close()
        shutil.rmtree(path.parent, ignore_errors=True)


def test_a_silent_broker_times_out_and_is_unreachable():
    import socket

    path = Path(tempfile.mkdtemp(prefix="bct", dir="/tmp")) / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    try:
        started = time.monotonic()
        with pytest.raises(BrokerError) as exc:
            BrokerClient(str(path), "cap", timeout=0.3).answer("pend-x")
        assert exc.value.name == "unreachable" and time.monotonic() - started < 3
    finally:
        srv.close()
        shutil.rmtree(path.parent, ignore_errors=True)


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_malformed_event_in_a_slot_is_denied_as_the_local_gate_denies_it(adapter, ws, tmp_path):
    ev = event_of(adapter, ws)
    ev["cwd"] = "relative"
    out = gate_of(adapter)(ev, hold=HOLD, **kw_of(adapter, tmp_path))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "has no valid cwd" in out["hookSpecificOutput"]["permissionDecisionReason"]
