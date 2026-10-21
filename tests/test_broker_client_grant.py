"""F4 W2-c3: the slot hook's half of the typed contract grant (levain.consent.broker_client's ``grant_member`` and
the ``grant`` member, both adapters' slot gate), built from the cases of
``project_memory/1020-f11/w2c3_run/RUN.txt``.

Everything the broker side needs is real, as in the run: ssh-keygen keys, a signed authority log (the operator's
display-phone key signs the contract through ``JobStore.sign``), a signed answer-signer snapshot, a dispatch store
over a real git repo that publishes the signed contract into ``L/contracts/``, a ``git worktree`` of that repo as the
run's ``repo_root``, and a real Broker behind real SlotListeners (the real peer-uid check, this uid): slot 1 a
dispatch session, slot 2 an interactive one. The hook is the adapters' ``slot_gate``, in process. Holds are short
here; the run held the real DEFAULT_GATE_HOLD.

The slot gate files only the calls ``gate_class`` gates (agent config and hooks), so the write list here names
agent-config files, as the run's does.
"""
from __future__ import annotations

import hashlib
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

ME = os.getuid()
CAP = "cap-w2c3"
HOLD = 1.5
WRITE = [".claude/agents/helper.md", ".claude/agents/old.md", ".codex/hooks.json", "src/a.py"]
# the recorded Claude Code Write input (1019-dispatch legB) and a Codex apply_patch (the run's)
CC_CONTENT = "probe two"
CX_ADD = "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** End Patch\n"
CX_NOT_TOTAL = "*** Begin Patch\n*** Add File: .codex/hooks.json\n+{}\n*** Frobnicate: x\n*** End Patch\n"


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
    repo, _ = make_repo(tmp / "src")
    (repo / ".claude" / "agents").mkdir(parents=True)
    (repo / ".claude" / "agents" / "old.md").write_text("old agent\n")
    git(repo, "add", ".claude/agents/old.md")
    git(repo, "commit", "-q", "-m", "an agent file")
    base = git(repo, "rev-parse", "HEAD")
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
        yield {"q": q, "use": use, "home": tmp_path / "home", "L": tmp_path}
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
    kw = {"home": s["home"], "expected_uid": ME, "hold": HOLD, **kw}
    if adapter == "codex":
        return cx.slot_gate(event, codex_dir=s["home"] / ".codex", **kw)
    return cc.slot_gate(event, **kw)


ALLOW = {"cc": cc._pre("allow", f"levain: allowed by {cc.SLOT_HOW}."),
         "codex": cx.decision_output("allow", cc.SLOT_HOW)}


# --- grant_member and the seal -----------------------------------------------------------------------------------

def test_the_member_is_grant_pys_paths_and_the_input_hash(world):
    root = world["root"]
    ti = {"file_path": f"{root}/.claude/agents/helper.md", "content": CC_CONTENT}
    assert broker_client.grant_member("claude_code", "Write", ti, root) == {
        "paths": [f"{root}/.claude/agents/helper.md"], "input": value_sha256(ti)}
    patch = {"command": CX_ADD}
    assert broker_client.grant_member("codex", "apply_patch", patch, root) == {
        "paths": [f"{root}/.codex/hooks.json"], "input": value_sha256(patch)}


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


def test_a_member_that_cannot_be_computed_is_no_member(monkeypatch):
    import levain.dispatch.grant as g

    def boom(*a, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(g, "grant_paths", boom)
    assert broker_client.grant_member("claude_code", "Write", {"file_path": "/r/a", "content": "x"}, "/r") is None


def test_the_sealed_digest_is_the_one_the_broker_recomputes(world):
    root = world["root"]
    ti = {"file_path": f"{root}/.claude/agents/helper.md", "content": CC_CONTENT}
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

@pytest.mark.parametrize("adapter", ["cc", "codex"])
def test_a_write_list_file_in_a_dispatch_session_is_allowed_with_no_answer(adapter, world, slots):
    root = world["root"]
    event = cc_write(root, ".claude/agents/helper.md") if adapter == "cc" else cx_patch(root, CX_ADD)
    sleeps = []
    assert gate(adapter, event, slots, sleep=sleeps.append) == ALLOW[adapter]
    rid, row = only_row(slots["q"])
    assert row["kind"] == "dispatch" and row["request"]["tool_input_digest"].startswith("pb1:")
    assert granted(slots["q"], rid) and not (slots["q"] / "answers" / f"{rid}.json").exists()
    assert sleeps == []   # the first ANSWER? answered: no poll waited


def test_an_edit_of_a_write_list_file_at_base_is_allowed(world, slots):
    root = world["root"]
    event = cc_write(root, ".claude/agents/old.md")
    event.update(tool_name="Edit", tool_input={"file_path": f"{root}/.claude/agents/old.md", "old_string": "old",
                                               "new_string": "new"})
    assert gate("cc", event, slots) == ALLOW["cc"]
    rid, _ = only_row(slots["q"])
    assert granted(slots["q"], rid)


def test_a_file_outside_the_write_list_is_pending_then_undecided_at_the_hold(world, slots):
    out = gate("cc", cc_write(world["root"], ".claude/agents/other.md"), slots)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"]
    rid, row = only_row(slots["q"])
    assert row["request"]["tool_input_digest"].startswith("pb1:") and not granted(slots["q"], rid)


def test_an_interactive_session_files_the_member_as_a_pending_request(world, slots):
    slots["use"](2)
    out = gate("cc", cc_write(world["root"], ".claude/agents/helper.md"), slots)
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"]
    rid, row = only_row(slots["q"])
    assert row["kind"] == "interactive" and row["request"]["tool_input_digest"].startswith("pb1:")
    assert not granted(slots["q"], rid)


def test_the_operators_answer_still_decides_a_pb1_request(world, slots):
    """An interactive slot's request sealed with a pb1 digest is answered as any other: the answer binds the sealed
    request, whatever its digest's scheme."""
    slots["use"](2)
    state = {"done": False}

    def sleep(s):
        if not state["done"]:
            state["done"] = True
            rid, _ = only_row(slots["q"])
            write_answer(slots["q"].parent, rid, world["keys"]["laptop"], decision="allow",
                         signed_at=datetime.now(timezone.utc))
        time.sleep(s)
    # write_answer writes under <L>/queue: this fixture's queue is <tmp>/queue
    assert gate("cc", cc_write(world["root"], ".claude/agents/helper.md"), slots, hold=10.0,
                sleep=sleep) == ALLOW["cc"]


def test_a_patch_not_read_whole_is_sealed_as_today_with_no_member(world, slots):
    out = gate("codex", cx_patch(world["root"], CX_NOT_TOTAL), slots)
    assert "UNDECIDED" in out["hookSpecificOutput"]["permissionDecisionReason"]
    rid, row = only_row(slots["q"])
    assert row["request"]["tool_input_digest"] == "jcs:" + value_sha256({"command": CX_NOT_TOTAL})
    assert not granted(slots["q"], rid)


def test_an_ordinary_write_list_file_is_ungated_in_a_slot_and_files_nothing(world, slots):
    assert gate("cc", cc_write(world["root"], "src/a.py"), slots) is None
    assert rows(slots["q"]) == []
