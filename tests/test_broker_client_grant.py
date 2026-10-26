"""F4 W2-c3: the slot hook's half of the typed contract grant (levain.consent.broker_client's ``grant_member`` and
the ``grant`` member, both adapters' slot gate), built from the cases of
``project_memory/1020-f11/w2c3_run/RUN.txt``, and the L3 r1 triage's R1-R4 from ``RUN_r3_before.txt`` /
``RUN_r3_after.txt`` (each case reproduced on the tree before the fix, then flipped).

Everything the broker side needs is real, as in the run: ssh-keygen keys, a signed authority log (the operator's
display-phone key signs the contract through ``JobStore.sign``), a signed answer-signer snapshot, a dispatch store
over a real git repo that publishes the signed contract into ``L/contracts/``, a ``git worktree`` of that repo as the
run's ``repo_root``, and a real Broker behind real SlotListeners (the real peer-uid check, this uid): slot 1 a
dispatch session, slot 2 an interactive one. The hook is the adapters' ``slot_gate``, in process. Holds are short
here; the run held the real DEFAULT_GATE_HOLD.

The head ruling 1020+49 (A'): a grantable call the gate does not gate is sent ``ungated`` with its member; the
broker answers GO outside a dispatch session (nothing written; GO is not allow), grants or queues it inside one; agent
config is never granted, and a forged ungated frame for one is filed as gated.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp, value_sha256
from levain.consent import broker_client
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx
from levain.contracts.canon import canonical_bytes
from levain.dispatch import sign as dsign
from levain.dispatch.grant import grant_paths, path_bound_digest
from levain.dispatch.jobs import JobStore
from levain.qbroker import grants
from levain.qbroker.broker import Broker, Resolved
from levain.qbroker.server import SlotListener
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import QueueStore
from tests.test_consent_queue import Key, Log
from tests.test_dispatch_contract import POLICY, git, make_repo, registry, submission, template
from tests.test_qbroker import bound_gid, write_answer
from tests._h4 import h4_env

ME = os.getuid()
CAP = "cap-w2c3"
HOLD = 1.5
WRITE = [".claude/agents/helper.md", ".claude/settings.json", "src/a.py", "src/new.py"]
# the recorded Claude Code Write input (1019-dispatch legB) and a Codex apply_patch (the run's)
CC_CONTENT = "probe two"
CX_ADD = "*** Begin Patch\n*** Add File: src/new.py\n+x = 1\n*** End Patch\n"
CX_NOT_TOTAL = "*** Begin Patch\n*** Add File: src/new.py\n+{}\n*** Frobnicate: x\n*** End Patch\n"


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class SlotResolver:
    def __init__(self, by_slot):
        self.by_slot = by_slot
        self.digest = "sha256:" + hashlib.sha256(CAP.encode()).hexdigest()

    def queue_resolve(self, slot, cap_digest):
        if cap_digest != self.digest:
            return "cap-unknown"
        return self.by_slot.get(slot.get("index"), "cap-unknown")


def _mk(p: Path, mode: int = 0o755) -> Path:
    p.mkdir(parents=True)
    os.chmod(p, mode)
    return p


def _entry(key, spaces, cls):
    kt, kb = key.public_key.split()
    return {"principal": key.path.name, "key_type": kt, "key_b64": kb, "key_class": cls,
            "custody": "passphrase" if cls == "laptop-presence" else "unstated", "namespaces": spaces}


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """A signed, published job whose write list names agent-config files; L/ with its snapshot; the worktree."""
    tmp = Path(os.path.realpath(tmp_path_factory.mktemp("w2c3")))
    kd = _mk(tmp / "keys")
    keys = {n: Key(kd, n) for n in ("laptop", "phone", "trust")}
    log = Log(_mk(tmp / "auth"), keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    L = tmp / "L"
    for d in ("policy", "routes", "contracts"):
        (L / d).mkdir(parents=True)
    (L / "routes" / "trust.pub").write_text(keys["trust"].public_key + " trust\n")
    now = datetime.now(timezone.utc)
    snap = {"format": "levain-allowed-signers/1", "generation": 1, "issued_at": utc_stamp(now),
            "signers": [_entry(keys["phone"], ["levain-dispatch-contract"], "display-phone"),
                        _entry(keys["laptop"], ["levain-consent-answer"], "laptop-presence")]}
    body = canonical_bytes(snap)
    (L / "policy" / "allowed_signers.json").write_bytes(body)
    (L / "policy" / "allowed_signers.json.sig").write_text(keys["trust"].sign(body, "levain-allowed-signers"))
    repo, base = make_repo(tmp / "src")
    clock = Clock(now - timedelta(minutes=2))
    store = JobStore(_mk(tmp / "dispatch", 0o700), clock=clock, published_dir=L / "contracts")
    # provider anthropic: the test policy lets it read ** (openrouter may not read .claude/)
    reg = registry(template(write_paths=frozenset(WRITE), provider="anthropic", route="anthropic/claude"))
    job, _ = store.file(submission(repo, base, read=["src/a.py"], write=WRITE, grant=["Edit", "Write", "apply_patch"],
                                   expires_at=utc_stamp(now + timedelta(hours=2))),
                        resolve_template=reg, policy=POLICY, filer_session="planner-1")
    clock.now = now - timedelta(minutes=1)
    payload = dsign.sign_payload(job, signer_fp=keys["phone"].fp, signed_at=clock.now)
    job = store.sign(job.id, payload, keys["phone"].sign(dsign.sign_message(payload), dsign.DISPATCH_SIGN_NAMESPACE),
                     log.source)
    root = tmp / "live" / "sess-d" / "repo"
    root.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-q", "--detach", str(root), base)
    run = {"run_id": "run-1", "job_id": job.id, "run_index": 0, "contract_digest": job.digest,
           "model_requested": "sonnet", "repo_root": str(root)}
    return {"L": L, "keys": keys, "root": str(root), "run": run}


@pytest.fixture
def slots(world, tmp_path, monkeypatch):
    """A fresh queue under the module's L/, a broker behind two live sockets: 1 dispatch, 2 interactive."""
    L = world["L"]
    q = tmp_path / "queue"
    for d in ("requests", "answers"):
        (q / d).mkdir(parents=True)
    resolver = SlotResolver({1: Resolved(session="sess-d", kind="dispatch", run=world["run"]),
                             2: Resolved(session="sess-i", kind="interactive", run=None)})
    broker = Broker(QueueStore(q), resolver, SignersSource(L / "policy", L / "routes" / "trust.pub"),
                    contracts=L / "contracts", contracts_owner=ME)
    run_dir = Path(tempfile.mkdtemp(prefix="bg", dir="/tmp"))
    lsts = {}
    for n in (1, 2):
        d = run_dir / f"s{n}"
        d.mkdir()
        lsts[n] = SlotListener(broker, n, d / "q.sock", ME, expected_gid=bound_gid(d))
        lsts[n].start()
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "home" / ".levain"))
    monkeypatch.setenv(broker_client.CAP_ENV, CAP)

    def use(n):
        monkeypatch.setenv(broker_client.SOCKET_ENV, str(lsts[n].path))

    use(1)
    try:
        yield {"q": q, "use": use, "home": tmp_path / "home", "L": tmp_path, "broker": broker}
    finally:
        for lst in lsts.values():
            lst.stop()
        shutil.rmtree(run_dir, ignore_errors=True)


def cc_write(root, rel, tool_use_id="toolu_1"):
    return {"session_id": "9032d40e-aacc-46e7-82f1-23badb9c8cbf", "cwd": root, "permission_mode": "default",
            "hook_event_name": "PreToolUse", "tool_name": "Write",
            "tool_input": {"file_path": f"{root}/{rel}", "content": CC_CONTENT}, "tool_use_id": tool_use_id}


def cx_patch(root, patch, tool_use_id="call_1"):
    return {"session_id": "01a1552b-22cb-7991-aa86-fbe69a523742", "cwd": root, "hook_event_name": "PreToolUse",
            "permission_mode": "default", "tool_name": "apply_patch", "tool_input": {"command": patch},
            "tool_use_id": tool_use_id}


def rows(q):
    return sorted(p.stem for p in (q / "requests").glob("pend-*.json"))


def only_row(q):
    (rid,) = rows(q)
    return rid, QueueStore(q).read_request(rid)


def granted(q, rid):
    return (q / "requests" / f"grant-{rid}.json").exists()


def gate(adapter, event, s, **kw):
    kw = {"home": s["home"], "environ": h4_env(), "expected_uid": ME, "hold": HOLD, **kw}
    if adapter == "codex":
        return cx.slot_gate(event, codex_dir=s["home"] / ".codex", **kw)
    return cc.slot_gate(event, **kw)


ALLOW = {"cc": cc._pre("allow", f"levain: allowed by {cc.SLOT_HOW}."),
         "codex": cx.decision_output("allow", cc.SLOT_HOW)}


# --- grant_member and the seal -----------------------------------------------------------------------------------

def test_the_member_is_grant_pys_paths_and_the_input_hash(world):
    root = world["root"]
    ti = {"file_path": f"{root}/src/a.py", "content": CC_CONTENT}
    assert broker_client.grant_member("claude_code", "Write", ti, root) == {
        "paths": [f"{root}/src/a.py"], "input": value_sha256(ti)}
    patch = {"command": CX_ADD}
    assert broker_client.grant_member("codex", "apply_patch", patch, root) == {
        "paths": [f"{root}/src/new.py"], "input": value_sha256(patch)}


@pytest.mark.parametrize("agent,tool,tool_input", [
    ("claude_code", "MultiEdit", {"file_path": "/r/a", "edits": []}),        # not grantable
    ("claude_code", "Bash", {"command": "echo"}),                            # never grantable
    ("claude_code", "Write", {"file_path": "/r/a", "content": "x", "mode": 1}),   # a key Write does not have
    ("claude_code", "Write", {"file_path": "rel/a", "content": "x"}),        # not absolute
    ("claude_code", "Write", {"file_path": "/r/a", "content": 1.5}),         # outside JCS
    ("codex", "apply_patch", {"command": CX_NOT_TOTAL}),                     # a line not read whole
    ("codex", "Write", {"file_path": "/r/a", "content": "x"}),               # not Codex's file tool
    ("claude_code", "Write", "not a mapping"),
])
def test_a_call_not_grantable_or_not_read_whole_has_no_member(agent, tool, tool_input):
    assert broker_client.grant_member(agent, tool, tool_input, "/r") is None


def _break_grant_paths(monkeypatch):
    import levain.dispatch.grant as g

    def boom(*a, **k):
        raise RuntimeError("grant_paths regression")
    monkeypatch.setattr(g, "grant_paths", boom)


def test_a_member_that_cannot_be_computed_raises(monkeypatch):
    """R1: only a parse that completed says "no member"; a failure is never read as "not grantable"."""
    _break_grant_paths(monkeypatch)
    with pytest.raises(RuntimeError):
        broker_client.grant_member("claude_code", "Write", {"file_path": "/r/a", "content": "x"}, "/r")


def test_the_sealed_digest_is_the_one_the_broker_recomputes(world):
    root = world["root"]
    ti = {"file_path": f"{root}/src/a.py", "content": CC_CONTENT}
    member = broker_client.grant_member("claude_code", "Write", ti, root)
    req = broker_client.gate_request(agent="claude_code", session="s", tool_name="Write", tool_input=ti, cwd=root,
                                     event_id="e", tier="T3", hold=5, grant=member)
    assert req.tool_input_digest == path_bound_digest("Write", grant_paths("claude_code", "Write", ti, root), ti)
    assert req.tool_input_digest.startswith("pb1:")
    assert grants.grant_member_refusal(req, member) is None
    plain = broker_client.gate_request(agent="claude_code", session="s", tool_name="Write", tool_input=ti, cwd=root,
                                       event_id="e", tier="T3", hold=5)
    assert plain.tool_input_digest.startswith("jcs:")
    assert grants.grant_member_refusal(plain, member) is not None


def test_the_member_rides_beside_request_and_only_when_there_is_one(monkeypatch):
    sent = []

    def call(self, op, *, timeout=None, **fields):
        sent.append(fields)
        return {"ok": True, "id": 1, "request_id": req.id, "queue_id": "q"}
    monkeypatch.setattr(broker_client.BrokerClient, "call", call)
    client = broker_client.BrokerClient("/x", "cap", expected_uid=ME)
    member = {"paths": ["/r/a"], "input": "0" * 64}
    req = broker_client.gate_request(agent="claude_code", session="s", tool_name="Write", tool_input={}, cwd="/r",
                                     event_id="e", tier="T3", hold=5, grant=member)
    client.request(req, grant=member)
    client.request(req)
    assert sent[0]["grant"] == member and set(sent[0]) == {"request", "grant"}
    assert set(sent[1]) == {"request"}


# --- the run's cases, through the slot gate ----------------------------------------------------------------------

def no_rows(q):
    return rows(q) == [] and not list((q / "requests").glob("grant-*.json"))


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_an_ungated_write_in_an_interactive_session_gets_go_and_nothing_is_written(adapter, world, slots):
    slots["use"](2)
    root = world["root"]
    event = cc_write(root, "src/a.py") if adapter == "cc" else cx_patch(root, CX_ADD)
    assert gate(adapter, event, slots) is None
    assert no_rows(slots["q"])


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_write_list_file_in_a_dispatch_session_is_allowed_with_no_answer(adapter, world, slots):
    root = world["root"]
    event = cc_write(root, "src/a.py") if adapter == "cc" else cx_patch(root, CX_ADD)
    sleeps = []
    assert gate(adapter, event, slots, sleep=sleeps.append) == ALLOW[adapter]
    rid, row = only_row(slots["q"])
    assert row["kind"] == "dispatch" and row["request"]["tool_input_digest"].startswith("pb1:")
    assert row["request"]["tier"] == broker_client.UNGATED_TIER
    assert granted(slots["q"], rid) and not (slots["q"] / "answers" / f"{rid}.json").exists()
    assert sleeps == []   # the first ANSWER? answered: no poll waited


def test_an_edit_of_a_write_list_file_is_allowed(world, slots):
    root = world["root"]
    event = cc_write(root, "src/a.py")
    event.update(tool_name="Edit", tool_input={"file_path": f"{root}/src/a.py", "old_string": "a = 1",
                                               "new_string": "a = 2"})
    assert gate("cc", event, slots) == ALLOW["cc"]
    rid, _ = only_row(slots["q"])
    assert granted(slots["q"], rid)


def test_a_file_outside_the_write_list_is_pending_then_undecided_reversible(world, slots):
    out = gate("cc", cc_write(world["root"], "src/b.py"), slots)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "continue" not in out    # the reversible tier: no hard stop
    rid, row = only_row(slots["q"])
    assert row["request"]["tool_input_digest"].startswith("pb1:") and not granted(slots["q"], rid)


def test_agent_config_in_the_write_list_is_gated_and_never_granted(world, slots):
    out = gate("cc", cc_write(world["root"], ".claude/settings.json"), slots)
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"] and out.get("continue") is False
    rid, row = only_row(slots["q"])
    # the gated path: today's digest at the agent-config tier, no member
    assert row["request"]["tool_input_digest"].startswith("jcs:") and row["request"]["tier"] == "T3"
    assert not granted(slots["q"], rid)


def test_the_operators_answer_decides_a_queued_ungated_call(world, slots):
    state = {"done": False}

    def sleep(s):
        if not state["done"]:
            state["done"] = True
            rid, _ = only_row(slots["q"])
            # write_answer writes under <its L>/queue: this fixture's queue is <tmp>/queue
            write_answer(slots["q"].parent, rid, world["keys"]["laptop"], decision="deny",
                         signed_at=datetime.now(timezone.utc))
        time.sleep(s)
    out = gate("cc", cc_write(world["root"], "src/b.py"), slots, hold=10.0, sleep=sleep)
    assert out == cc.decision_output("deny", cc.SLOT_HOW)


def _no_member_writes(root):
    """RUN_r3 R1b: file writes the gate does not gate whose parse gives no member."""
    multi = {"file_path": f"{root}/src/a.py", "edits": [{"old_string": "a = 1", "new_string": "a = 2"}]}
    return {"MultiEdit (in the write list, not grantable)": ("MultiEdit", multi),
            "NotebookEdit": ("NotebookEdit", {"notebook_path": f"{root}/src/n.ipynb", "new_source": "x = 1"}),
            "Write with a relative path": ("Write", {"file_path": "src/a.py", "content": "rel"}),
            "Write with a key Write does not have": ("Write", {"file_path": f"{root}/src/a.py", "content": "x",
                                                               "mode": 1})}


def _event(root, tool, tool_input):
    event = cc_write(root, "src/a.py")
    event.update(tool_name=tool, tool_input=tool_input)
    return event


@pytest.mark.parametrize("case", list(_no_member_writes("/r")))
def test_a_file_write_with_no_member_in_dispatch_is_queued_never_left_to_the_mode(case, world, slots):
    """R1: filed for the operator (today's digest, no grant), held, then UNDECIDED at the reversible tier."""
    tool, tool_input = _no_member_writes(world["root"])[case]
    out = gate("cc", _event(world["root"], tool, tool_input), slots)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"] and "continue" not in out
    rid, row = only_row(slots["q"])
    assert row["kind"] == "dispatch" and row["request"]["tool_input_digest"].startswith("jcs:")
    assert row["request"]["tier"] == broker_client.UNGATED_TIER and not granted(slots["q"], rid)


def test_a_file_write_with_no_member_in_an_interactive_session_gets_go(world, slots):
    slots["use"](2)
    tool, tool_input = _no_member_writes(world["root"])["MultiEdit (in the write list, not grantable)"]
    assert gate("cc", _event(world["root"], tool, tool_input), slots) is None
    assert no_rows(slots["q"])


def test_a_file_write_with_no_member_and_the_broker_down_is_denied(world, slots, monkeypatch):
    monkeypatch.setenv(broker_client.SOCKET_ENV, "/tmp/levain-no-such-broker.sock")
    tool, tool_input = _no_member_writes(world["root"])["MultiEdit (in the write list, not grantable)"]
    out = gate("cc", _event(world["root"], tool, tool_input), slots)
    # F10 S2b-2: the GATE asked first, before the call is classified, is what fails
    assert out == cc.could_not_run("irreversible", "this call")


def test_a_call_that_is_not_a_file_write_asks_only_gate(world, slots, monkeypatch):
    slots["use"](2)
    assert gate("cc", _event(world["root"], "Bash", {"command": "ls"}), slots) is None
    assert no_rows(slots["q"])
    # and with the broker down, the GATE fails closed (every call asks it)
    monkeypatch.setenv(broker_client.SOCKET_ENV, "/tmp/levain-no-such-broker.sock")
    assert gate("cc", _event(world["root"], "Bash", {"command": "ls"}), slots) == \
        cc.could_not_run("irreversible", "this call")


@pytest.mark.parametrize("adapter,slot", [("cc", 1), ("cc", 2), ("codex", 1), ("codex", 2)])
def test_a_member_that_cannot_be_read_denies_the_call(adapter, slot, world, slots, monkeypatch):
    """R1 (RUN_r3 R1a): grant_paths raising inside the hook is could_not_run's deny, nothing filed."""
    slots["use"](slot)
    _break_grant_paths(monkeypatch)
    root = world["root"]
    event = cc_write(root, "src/a.py") if adapter == "cc" else cx_patch(root, CX_ADD)
    out = gate(adapter, event, slots)
    if adapter == "cc":
        assert out == cc.could_not_run("reversible", cc._what("Write", event["tool_input"]))
    else:
        assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "could not run" in json.dumps(out)
    assert no_rows(slots["q"])


def test_claude_codes_write_to_a_codex_dir_file_is_not_agent_config_to_the_broker(world, slots):
    """R3 (RUN_r3): Claude Code's classifier does not gate .codex/notes.md, and neither does the broker for it."""
    slots["use"](2)
    assert gate("cc", cc_write(world["root"], ".codex/notes.md"), slots) is None
    assert no_rows(slots["q"])


def test_agent_config_path_reads_each_agents_own_rule():
    root = "/w/repo"
    notes, hooks = [f"{root}/.codex/notes.md"], [f"{root}/.codex/hooks.json"]
    assert grants.agent_config_path(notes, root, "claude_code") is None
    assert grants.agent_config_path(notes, root, "codex") == notes[0]
    assert grants.agent_config_path(notes, root, "another-agent") == notes[0]   # both rules: gated wins
    assert grants.agent_config_path(hooks, root, "claude_code") == hooks[0]     # Claude Code's own rule
    assert grants.agent_config_path([f"{root}/.claude/settings.json"], root, "codex") is not None


def test_a_patch_not_read_whole_stays_gated_as_today_with_no_member(world, slots):
    """Codex's gate tags a patch it cannot read whole (fail closed), so it is filed at the agent-config tier with
    today's digest: never a member, never granted."""
    out = gate("codex", cx_patch(world["root"], CX_NOT_TOTAL), slots)
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"]
    rid, row = only_row(slots["q"])
    assert row["request"]["tool_input_digest"] == "jcs:" + value_sha256({"command": CX_NOT_TOTAL})
    assert row["request"]["tier"] == "T3" and not granted(slots["q"], rid)


@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_an_ungated_grantable_call_with_the_broker_down_is_denied(adapter, world, slots, monkeypatch):
    """Fail closed (head ruling 1020+49): only an explicit GO lets the mode decide."""
    monkeypatch.setenv(broker_client.SOCKET_ENV, "/tmp/levain-no-such-broker.sock")
    root = world["root"]
    event = cc_write(root, "src/a.py") if adapter == "cc" else cx_patch(root, CX_ADD)
    out = gate(adapter, event, slots)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "could not run" in json.dumps(out)


def test_a_forged_t2_frame_for_agent_config_by_path_gets_no_grant(world, slots):
    """B' by PATH: sealed T2 for a .claude/agents/ file the signed write list names, the broker still refuses the grant."""
    got = forge(world, slots, 1, ".claude/agents/helper.md")
    rid, row = only_row(slots["q"])
    assert got["request_id"] == rid and row["request"]["tier"] == "T2" and not granted(slots["q"], rid)


def test_a_frame_sealed_at_the_agent_config_tier_gets_no_grant(world, slots):
    got = forge(world, slots, 1, "src/a.py", tier="T3")
    rid, _ = only_row(slots["q"])
    assert got["request_id"] == rid and not granted(slots["q"], rid)


# --- forged frames: what Bash holding the cap can get ------------------------------------------------------------

_ABSENT = object()


def forge(world, slots, slot, rel, *, member=True, ungated=True, tier="T2"):
    slots["use"](slot)
    root = world["root"]
    ti = {"file_path": f"{root}/{rel}", "content": "forged"}
    m = broker_client.grant_member("claude_code", "Write", ti, root)
    req = broker_client.gate_request(agent="claude_code", session="forger", tool_name="Write", tool_input=ti,
                                     cwd=root, event_id=None, tier=tier, hold=60, grant=m if member else None)
    fields = req.fields()
    fields.pop("id")
    extra = {"grant": m} if member else {}
    if ungated is not _ABSENT:
        extra["ungated"] = ungated
    return broker_client.BrokerClient.from_env(expected_uid=ME).call("REQUEST", request=fields, **extra)


def test_a_forged_ungated_frame_gets_go_at_worst_and_writes_nothing(world, slots):
    got = forge(world, slots, 2, "src/b.py")
    assert set(got) == {"id", "ok", "go"} and got["go"] is True
    assert no_rows(slots["q"])


def test_a_forged_ungated_frame_for_agent_config_is_filed_as_gated(world, slots):
    got = forge(world, slots, 2, ".claude/settings.json")
    assert "request_id" in got and "go" not in got
    rid, _ = only_row(slots["q"])
    assert not granted(slots["q"], rid)


def test_a_forged_ungated_frame_for_listed_agent_config_in_dispatch_is_never_granted(world, slots):
    got = forge(world, slots, 1, ".claude/settings.json")
    rid, row = only_row(slots["q"])
    assert got["request_id"] == rid and row["kind"] == "dispatch" and not granted(slots["q"], rid)


def test_ungated_without_a_member_gets_go_interactive_and_is_filed_in_dispatch(world, slots):
    """R1, the protocol half: ``ungated`` with no ``grant`` is accepted; GO outside dispatch, a pending row in it."""
    got = forge(world, slots, 2, "src/b.py", member=False)
    assert set(got) == {"id", "ok", "go"} and got["go"] is True and no_rows(slots["q"])
    got = forge(world, slots, 1, "src/b.py", member=False)
    rid, row = only_row(slots["q"])
    assert got["request_id"] == rid and row["kind"] == "dispatch" and not granted(slots["q"], rid)


def test_a_forged_frame_for_a_codex_dir_file_under_claude_codes_own_rule_is_filed(world, slots):
    got = forge(world, slots, 2, ".codex/hooks.json")
    assert "request_id" in got and "go" not in got


def _sealed(world, rel):
    root = world["root"]
    ti = {"file_path": f"{root}/{rel}", "content": "r2"}
    member = broker_client.grant_member("claude_code", "Write", ti, root)
    req = broker_client.gate_request(agent="claude_code", session="forger", tool_name="Write", tool_input=ti,
                                     cwd=root, event_id=None, tier="T2", hold=60, grant=member)
    fields = req.fields()
    fields.pop("id")
    return req.id, fields, member


def test_a_resent_ungated_frame_is_answered_by_its_filed_row_and_signed_deny(world, slots):
    """R2 (RUN_r3): file without the flag, the operator signs a deny, resend the identical request flagged: the row
    answers, never GO, and ANSWER? still reads the deny."""
    slots["use"](2)
    client = broker_client.BrokerClient.from_env(expected_uid=ME)
    rid, fields, member = _sealed(world, "src/c.py")
    assert client.call("REQUEST", request=fields, grant=member)["request_id"] == rid
    write_answer(slots["q"].parent, rid, world["keys"]["laptop"], decision="deny",
                 signed_at=datetime.now(timezone.utc))
    got = client.call("REQUEST", request=fields, grant=member, ungated=True)
    assert got["request_id"] == rid and "go" not in got
    assert client.answer(rid) == "deny"
    rid2, fields2, member2 = _sealed(world, "src/d.py")     # pending, no answer yet
    client.call("REQUEST", request=fields2, grant=member2)
    got = client.call("REQUEST", request=fields2, grant=member2, ungated=True)
    assert got["request_id"] == rid2 and "go" not in got


def test_an_ungated_go_waits_for_the_request_ids_filing_flight(world, slots):
    """R2: GO is decided inside the same single flight as filing; while a filing of that id runs, GO waits."""
    import threading

    slots["use"](2)
    broker = slots["broker"]
    rid, fields, member = _sealed(world, "src/f.py")
    flight = broker._enter_flight(rid)
    box = {}
    worker = threading.Thread(target=lambda: box.update(got=broker_client.BrokerClient.from_env(
        expected_uid=ME).call("REQUEST", request=fields, grant=member, ungated=True)))
    worker.start()
    worker.join(0.5)
    assert worker.is_alive() and "got" not in box
    broker._leave_flight(rid, flight)
    worker.join(10)
    assert box["got"]["go"] is True


def test_a_raise_reading_agent_config_files_the_call_as_gated(world, slots, monkeypatch):
    """R4 (RUN_r3): the broker reads a failure of agent_config_path as agent config: filed, never GO."""
    def boom(*a, **k):
        raise RuntimeError("agent_config_path failure")
    monkeypatch.setattr(grants, "agent_config_path", boom)
    got = forge(world, slots, 2, "src/e.py")
    rid, row = only_row(slots["q"])
    assert got["request_id"] == rid and "go" not in got and not granted(slots["q"], rid)


@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_an_ungated_flag_that_is_not_true_is_malformed(world, slots, value):
    with pytest.raises(broker_client.BrokerError) as e:
        forge(world, slots, 2, "src/b.py", ungated=value)
    assert e.value.name == "request-malformed"
    assert no_rows(slots["q"])
