"""F4 W2-c2: the queue broker's typed contract grant, built from the W2-c2 RUN's cases
(``project_memory/1020-f11/w2c2_run/RUN.txt``).

Everything is real: ssh-keygen keys, a signed authority log (the operator's display-phone key signs the contract
through ``JobStore.sign``), a signed answer-signer snapshot, a dispatch store over a real git repo that publishes
the signed contract into the tmp ``L/contracts/``, and a ``git worktree`` of that repo as the run's ``repo_root``.
The REQUEST frames carry the recorded Claude Code ``Write`` input (1019-dispatch legB) and the recorded Codex
``apply_patch`` input (1017-presence), each rewritten onto the worktree, with the ``pb1:`` digest and the ``grant``
member computed by grant.py's own functions (what the W2-c3 hook is to send).
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp, value_sha256
from levain.consent.record import ANSWER_NAMESPACE, GRANT_ANSWER_FORMAT, answer_message, answer_payload
from levain.contracts.canon import canonical_bytes, parse
from levain.dispatch import published
from levain.dispatch import sign as dsign
from levain.dispatch.contract import parse_contract
from levain.dispatch.grant import (Allow, Queue, grant_paths, input_bound_digest, path_bound_digest,
                                   paths_decision)
from levain.dispatch.jobs import JobStore
from levain.qbroker import grants
from levain.qbroker import operator as qoperator
from levain.qbroker.broker import Broker, Resolved, resolved_from
from levain.qbroker.grants import contract_authority
from levain.qbroker.receipts import Receipts, ReceiptsError
from levain.qbroker.seam import answer_refusal, grant_allow_refusal, request_of
from levain.qbroker.signers import SignersSource
from levain.qbroker.store import ANSWER_ROW_FORMAT, GRANT_ROW_FORMAT, QueueStore
from tests.test_consent_queue import Key, Log
from tests.test_dispatch_contract import NOW, POLICY, git, make_repo, registry, submission

WRITE = ["docs/x.md", "src/a.py", "src/new.py", "src/sub/deep.py"]
CC_WRITE = {"file_path": "/private/tmp/levain-1019-14-b/inst/.claude/agents/probe2.md", "content": "probe two"}
CX_ADD = "*** Begin Patch\n*** Add File: k1.txt\n+one\n*** End Patch"
CX_UPDATE = "*** Begin Patch\n*** Update File: k3.txt\n@@\n-three\n+four\n*** End Patch"
BNOW = NOW + timedelta(minutes=2)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class OneResolver:
    def __init__(self, resolved):
        self.resolved = resolved

    def queue_resolve(self, slot, cap_digest):
        return self.resolved


def _mk(p: Path, mode: int = 0o755) -> Path:
    p.mkdir(parents=True)
    os.chmod(p, mode)
    return p


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("w2c2-keys")
    return {n: Key(d, n) for n in ("laptop", "phone", "trust", "stranger")}


def signer_entry(key, spaces, cls):
    kt, kb = key.public_key.split()
    return {"principal": key.path.name, "key_type": kt, "key_b64": kb, "key_class": cls,
            "custody": "passphrase" if cls == "laptop-presence" else "unstated", "namespaces": spaces}


@pytest.fixture
def world(tmp_path, keys):
    """A signed, published job; L/ with its snapshot; the worktree root (docs/ symlinked away)."""
    tmp = Path(os.path.realpath(tmp_path))
    log = Log(_mk(tmp / "auth"), keys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("phone", "display-phone")
    L = tmp / "L"
    for d in ("queue/requests", "queue/answers", "policy", "routes", "contracts"):
        (L / d).mkdir(parents=True)
    (L / "routes" / "trust.pub").write_text(keys["trust"].public_key + " trust\n")
    snap = {"format": "levain-allowed-signers/1", "generation": 1, "issued_at": utc_stamp(NOW),
            "signers": [signer_entry(keys["phone"], ["levain-dispatch-contract"], "display-phone"),
                        signer_entry(keys["laptop"], ["levain-consent-answer"], "laptop-presence")]}
    body = canonical_bytes(snap)
    (L / "policy" / "allowed_signers.json").write_bytes(body)
    (L / "policy" / "allowed_signers.json.sig").write_text(keys["trust"].sign(body, "levain-allowed-signers"))
    repo, base = make_repo(tmp / "src")
    store = JobStore(_mk(tmp / "dispatch", 0o700), clock=Clock(NOW), published_dir=L / "contracts")
    job, cap = store.file(submission(repo, base, read=["src/a.py"], write=WRITE,
                                     grant=["Edit", "Write", "apply_patch"]),
                          resolve_template=registry(), policy=POLICY, filer_session="planner-1")
    store._clock.now = NOW + timedelta(minutes=1)
    payload = dsign.sign_payload(job, signer_fp=keys["phone"].fp, signed_at=NOW + timedelta(minutes=1))
    job = store.sign(job.id, payload, keys["phone"].sign(dsign.sign_message(payload), dsign.DISPATCH_SIGN_NAMESPACE),
                     log.source)
    root = tmp / "live" / "sess-g" / "repo"
    root.parent.mkdir(parents=True)
    git(repo, "worktree", "add", "-q", "--detach", str(root), base)
    os.rename(root / "docs", root / "docs_real")
    os.symlink("docs_real", root / "docs")
    run = {"run_id": "run-1", "job_id": job.id, "run_index": 0, "contract_digest": job.digest,
           "model_requested": "sonnet", "repo_root": str(root)}
    w = {"L": L, "store": store, "job": job, "cap": cap, "root": str(root), "run": run, "keys": keys, "log": log,
         "repo": repo, "base": base,
         "clock": Clock(BNOW), "contract": L / "contracts" / published.file_name(job.digest)}
    w["genuine"] = w["contract"].read_bytes()
    return w


def broker(w, *, kind="dispatch", owner=None, contracts=True, **kw):
    resolved = Resolved(session="sess-g", kind=kind, run=w["run"] if kind == "dispatch" else None)
    return Broker(QueueStore(w["L"] / "queue"), OneResolver(resolved),
                  SignersSource(w["L"] / "policy", w["L"] / "routes" / "trust.pub"), clock=w["clock"],
                  contracts=(w["L"] / "contracts") if contracts else None,
                  contracts_owner=os.getuid() if owner is None else owner, **kw)


_ids = iter(range(1, 10**6))


def frame(w, event, tool, tool_input, *, agent="claude_code", cwd=None, paths=None, member=None):
    real = list(grant_paths(agent, tool, tool_input, w["root"]) or [])
    fields = {"agent": agent, "agent_session_id": "s-1", "event_id": f"hook:{event}", "tool_name": tool,
              "tool_input_digest": path_bound_digest(tool, real, tool_input), "display": f"{tool} w2c2",
              "cwd": w["root"] if cwd is None else cwd, "tier": "T2", "received_at": utc_stamp(w["clock"].now),
              "expires_at": utc_stamp(w["clock"].now + timedelta(minutes=5))}
    msg = {"v": 1, "op": "REQUEST", "id": next(_ids), "cap": "cap-g", "request": fields,
           "grant": member if member is not None else {"paths": real if paths is None else paths,
                                                        "input": value_sha256(tool_input)}}
    return msg


def cc(w, rel):
    return {**CC_WRITE, "file_path": f"{w['root']}/{rel}"}


def ask(b, rid):
    return b.handle(1, {"v": 1, "op": "ANSWER?", "id": next(_ids), "cap": "cap-g", "request_id": rid})


def outcome(w, b, msg):
    got = b.handle(1, msg)
    if not got.get("ok"):
        return f"refused:{got['refused']}"
    ans = ask(b, got["request_id"])
    if ans.get("decision") == "allow":
        return "allow"
    assert ans.get("pending") is True, ans
    assert not QueueStore(w["L"] / "queue").has_grant(got["request_id"])
    return "queued"


def pend_rows(w):
    return [n for n in os.listdir(w["L"] / "queue" / "requests") if n.startswith("pend-")]


# --- the run's cases --------------------------------------------------------------------------------------------

def test_a_write_inside_the_signed_write_list_is_allowed_by_the_grant_row(world):
    b = broker(world)
    got = b.handle(1, frame(world, "a", "Write", cc(world, "src/a.py")))
    assert got["ok"] is True
    rid = got["request_id"]
    grant = QueueStore(world["L"] / "queue").read_grant(rid)
    assert grant == {"format": GRANT_ROW_FORMAT, "request_id": rid, "contract_digest": world["job"].digest,
                     "paths": [f"{world['root']}/src/a.py"], "signer_fp": world["keys"]["phone"].fp,
                     "at": utc_stamp(BNOW)}
    assert oct((world["L"] / "queue" / "requests" / f"grant-{rid}.json").stat().st_mode & 0o777) == "0o640"
    # the request row is written as today, beside it
    assert QueueStore(world["L"] / "queue").read_request(rid)["run"] == world["run"]
    got = ask(b, rid)
    assert got == {"id": got["id"], "ok": True, "decision": "allow"}


@pytest.mark.parametrize("label, tool, rel, expect", [
    ("a NEW write-list entry", "Write", "src/new.py", "allow"),
    ("outside the write list", "Write", "src/b.py", "queued"),
    ("under a symlinked directory", "Write", "docs/x.md", "queued"),
])
def test_claude_code_writes(world, label, tool, rel, expect):
    assert outcome(world, broker(world), frame(world, label, tool, cc(world, rel))) == expect


def test_an_edit_inside_the_write_list_is_allowed(world):
    edit = {"file_path": f"{world['root']}/src/a.py", "old_string": "a", "new_string": "b"}
    assert outcome(world, broker(world), frame(world, "edit", "Edit", edit)) == "allow"


@pytest.mark.parametrize("label, command, cwd, expect", [
    ("add a NEW entry at the root", CX_ADD.replace("k1.txt", "src/new.py"), "", "allow"),
    ("update an entry at the root", CX_UPDATE.replace("k3.txt", "src/a.py"), "", "allow"),
    ("update from a cwd below the root", CX_UPDATE.replace("k3.txt", "src/a.py"), "/src", "queued"),
    ("update outside the write list", CX_UPDATE.replace("k3.txt", "src/b.py"), "", "queued"),
])
def test_codex_patches_and_their_cwd(world, label, command, cwd, expect):
    msg = frame(world, label, "apply_patch", {"command": command}, agent="codex", cwd=world["root"] + cwd)
    assert outcome(world, broker(world), msg) == expect


def test_a_grant_member_whose_paths_differ_is_refused_and_nothing_is_written(world):
    b = broker(world)
    got = b.handle(1, frame(world, "lie", "Write", cc(world, "src/a.py"), paths=[f"{world['root']}/src/b.py"]))
    assert got == {"id": got["id"], "ok": False, "refused": "grant-digest-mismatch"}
    assert pend_rows(world) == []


@pytest.mark.parametrize("member", [None, {}, {"paths": [], "input": "0" * 64, "x": 1}, {"paths": "a", "input": "0" * 64},
                                    {"paths": ["/b", "/a"], "input": "0" * 64}, {"paths": ["/a"], "input": "ZZ"}])
def test_a_malformed_grant_member_is_a_digest_mismatch(world, member):
    msg = frame(world, f"m{id(member)}", "Write", cc(world, "src/a.py"))
    msg["grant"] = member
    assert broker(world).handle(1, msg)["refused"] == "grant-digest-mismatch"
    assert pend_rows(world) == []


def test_a_grant_member_on_a_non_path_bound_request_is_refused(world):
    msg = frame(world, "jcs", "Write", cc(world, "src/a.py"))
    msg["request"]["tool_input_digest"] = "jcs:" + value_sha256(cc(world, "src/a.py"))
    assert broker(world).handle(1, msg)["refused"] == "grant-digest-mismatch"


def _forge(w, data):
    w["contract"].write_bytes(data)


def _rec(w):
    return parse(w["genuine"])


def forgeries(w):
    rec, k = _rec(w), w["keys"]
    lp = dict(rec["payload"], signer_fp=k["laptop"].fp)
    sp = dict(rec["payload"], signer_fp=k["stranger"].fp)
    return {
        "bad signature": canonical_bytes(dict(rec, signature=k["laptop"].sign(
            dsign.sign_message(rec["payload"]), dsign.DISPATCH_SIGN_NAMESPACE))),
        "another digest field": canonical_bytes(dict(rec, digest="sha256:" + "0" * 64)),
        "tampered contract text": canonical_bytes(dict(rec, contract=rec["contract"].replace("Fix the lint", "Fix it"))),
        "another job_id": canonical_bytes(dict(rec, payload=dict(rec["payload"], job_id="dj-" + "1" * 32))),
        "another job record id": canonical_bytes(dict(rec, job=dict(rec["job"], job_id="dj-" + "1" * 32))),
        "an answer-only signer": canonical_bytes(dict(rec, payload=lp, signature=k["laptop"].sign(
            dsign.sign_message(lp), dsign.DISPATCH_SIGN_NAMESPACE))),
        "a signer not in the snapshot": canonical_bytes(dict(rec, payload=sp, signature=k["stranger"].sign(
            dsign.sign_message(sp), dsign.DISPATCH_SIGN_NAMESPACE))),
        "the answer namespace": canonical_bytes(dict(rec, signature=k["phone"].sign(
            dsign.sign_message(rec["payload"]), "levain-consent-answer"))),
        "a job expiry that is not the contract's": canonical_bytes(dict(rec, job=dict(
            rec["job"], expires_at=utc_stamp(NOW + timedelta(hours=9))))),
        "a signature outside the claimed lease": canonical_bytes(dict(rec, job=dict(
            rec["job"], unsigned_lease_end=utc_stamp(NOW)))),
        "not canonical": json.dumps(rec, indent=1).encode(),
        "another format": canonical_bytes(dict(rec, format="levain-dispatch-published-contract/2")),
        "an oversize file": b" " * (1024 * 1024 + 64 * 1024 + 1),
        "not JSON": b"\x00\x01",
    }


WHY = {
    "bad signature": "the contract signature does not verify",
    "another digest field": "names another digest than the run's",
    "tampered contract text": "text's digest is not the run's",
    "another job_id": "signed for another job",
    "another job record id": "signed for another job",
    "an answer-only signer": "the signer is not an enrolled key",
    "a signer not in the snapshot": "the signer is not an enrolled key",
    "the answer namespace": "the contract signature does not verify",
    "a job expiry that is not the contract's": "expiry is not the contract's",
    "a signature outside the claimed lease": "after the job's lease ended",
    "not canonical": "not canonical JSON",
    "another format": "is not 'levain-dispatch-published-contract/1'",
    "an oversize file": "is over",
    "not JSON": "does not parse",
}


@pytest.mark.parametrize("name", sorted(WHY))
def test_a_forged_published_contract_queues(world, name, caplog):
    _forge(world, forgeries(world)[name])
    with caplog.at_level("INFO", logger="levain.qbroker"):
        assert outcome(world, broker(world), frame(world, f"forge-{name}", "Write", cc(world, "src/a.py"))) \
            == "queued"
    reasons = [r.getMessage() for r in caplog.records if "goes to the queue" in r.getMessage()]
    assert len(reasons) == 1 and WHY[name] in reasons[0], reasons
    _forge(world, world["genuine"])
    assert outcome(world, broker(world), frame(world, f"back-{name}", "Write", cc(world, "src/a.py"))) == "allow"


def test_a_symlinked_published_contract_queues(world, tmp_path):
    (tmp_path / "elsewhere.json").write_bytes(world["genuine"])
    os.unlink(world["contract"])
    os.symlink(tmp_path / "elsewhere.json", world["contract"])
    assert outcome(world, broker(world), frame(world, "link", "Write", cc(world, "src/a.py"))) == "queued"


def test_a_symlinked_contracts_directory_queues(world, tmp_path):
    real = world["L"] / "contracts"
    os.rename(real, world["L"] / "contracts_real")
    os.symlink("contracts_real", real)
    assert outcome(world, broker(world), frame(world, "dlink", "Write", cc(world, "src/a.py"))) == "queued"


def test_a_published_contract_another_uid_owns_queues(world):
    b = broker(world, owner=os.getuid() + 1)
    assert outcome(world, b, frame(world, "owner", "Write", cc(world, "src/a.py"))) == "queued"


def test_an_expired_contract_queues(world):
    world["clock"].now = NOW + timedelta(hours=2, seconds=1)
    assert outcome(world, broker(world), frame(world, "exp", "Write", cc(world, "src/a.py"))) == "queued"


def test_an_interactive_session_never_takes_a_grant(world):
    assert outcome(world, broker(world, kind="interactive"), frame(world, "i", "Write", cc(world, "src/a.py"))) \
        == "queued"


def test_a_broker_with_no_contracts_directory_grants_nothing(world):
    assert outcome(world, broker(world, contracts=False), frame(world, "n", "Write", cc(world, "src/a.py"))) \
        == "queued"


def test_a_missing_signer_snapshot_queues(world):
    os.unlink(world["L"] / "policy" / "allowed_signers.json.sig")
    assert outcome(world, broker(world), frame(world, "nosnap", "Write", cc(world, "src/a.py"))) == "queued"


def test_a_cancelled_job_withdraws_its_contract_and_grants_nothing(world):
    world["store"].cancel_by_planner(world["job"].id, world["cap"])
    assert not world["contract"].exists()
    assert outcome(world, broker(world), frame(world, "cancel", "Write", cc(world, "src/a.py"))) == "queued"


# --- pending, quota, replay, restart --------------------------------------------------------------------------------

def test_a_granted_row_is_not_pending_but_is_charged_to_the_quota(world):
    b = broker(world, pending_cap=1, quota=3)
    assert b.handle(1, frame(world, "q1", "Write", cc(world, "src/b.py")))["ok"] is True        # pending
    assert b.handle(1, frame(world, "q2", "Write", cc(world, "src/a.py")))["ok"] is True        # granted
    assert b.handle(1, frame(world, "q3", "Write", cc(world, "src/b.py")))["refused"] == "queue-busy"
    assert b.handle(1, frame(world, "q4", "Write", cc(world, "src/new.py")))["ok"] is True      # granted
    assert b.handle(1, frame(world, "q5", "Write", cc(world, "src/a.py")))["refused"] == "quota-exhausted"


def test_a_replayed_granted_request_returns_its_row_and_stays_allowed(world):
    b = broker(world)
    msg = frame(world, "rep", "Write", cc(world, "src/a.py"))
    first = b.handle(1, msg)
    again = b.handle(1, dict(msg, id=next(_ids)))
    assert again["request_id"] == first["request_id"]
    assert ask(b, first["request_id"])["decision"] == "allow"


def test_a_restarted_broker_does_not_count_a_granted_row_as_pending(world):
    b = broker(world)
    rid = b.handle(1, frame(world, "rs", "Write", cc(world, "src/a.py")))["request_id"]
    b2 = broker(world, pending_cap=1)
    assert rid not in b2._pending.get("sess-g", {})
    assert b2.handle(1, frame(world, "rs2", "Write", cc(world, "src/b.py")))["ok"] is True


# --- the seam: one format, one reader ---------------------------------------------------------------------------

def test_a_grant_allow_row_forged_into_answers_is_refused(world):
    b = broker(world)
    rid = b.handle(1, frame(world, "seam", "Write", cc(world, "src/b.py")))["request_id"]
    row = {"format": GRANT_ROW_FORMAT, "request_id": rid, "contract_digest": world["job"].digest,
           "paths": [f"{world['root']}/src/b.py"], "signer_fp": world["keys"]["phone"].fp, "at": utc_stamp(BNOW)}
    (world["L"] / "queue" / "answers" / f"{rid}.json").write_bytes(canonical_bytes(row))
    assert ask(b, rid)["refused"] == "answer-refused"
    # wrapped as an answer row, the payload's grant format is refused by the seam by name
    (world["L"] / "queue" / "answers" / f"{rid}.json").write_bytes(canonical_bytes(
        {"format": ANSWER_ROW_FORMAT, "payload": row, "signature": "x"}))
    assert ask(b, rid)["refused"] == "answer-refused"
    stored = QueueStore(world["L"] / "queue").read_request(rid)
    snap = SignersSource(world["L"] / "policy", world["L"] / "routes" / "trust.pub").current()
    for fmt in (GRANT_ROW_FORMAT, GRANT_ANSWER_FORMAT):
        assert answer_refusal(stored, dict(row, format=fmt), "x", snap, BNOW) == \
            f"a {fmt} record is not a queue answer"


def test_an_answer_row_at_the_grant_name_is_refused(world):
    b = broker(world)
    rid = b.handle(1, frame(world, "g-ans", "Write", cc(world, "src/b.py")))["request_id"]
    (world["L"] / "queue" / "requests" / f"grant-{rid}.json").write_bytes(canonical_bytes(
        {"format": ANSWER_ROW_FORMAT, "payload": {}, "signature": ""}))
    with pytest.raises(ValueError, match=GRANT_ROW_FORMAT):
        QueueStore(world["L"] / "queue").read_grant(rid)
    assert ask(b, rid)["refused"] == "answer-refused"


@pytest.mark.parametrize("over, match", [
    ({"request_id": "pend-x"}, "another request"),
    ({"contract_digest": "sha256:" + "0" * 64}, "another contract"),
    ({"paths": []}, "not text"),
    ({"extra": 1}, "not a levain-queue-grant-allow/1"),
])
def test_grant_allow_refusal(world, over, match):
    b = broker(world)
    rid = b.handle(1, frame(world, f"gar{sorted(over)}", "Write", cc(world, "src/a.py")))["request_id"]
    q = QueueStore(world["L"] / "queue")
    grant, row = q.read_grant(rid), q.read_request(rid)
    assert grant_allow_refusal(row, grant, BNOW) is None
    assert match in grant_allow_refusal(row, {**grant, **over}, BNOW)
    assert "hold ended" in grant_allow_refusal(row, grant, BNOW + timedelta(minutes=6))


def test_the_contract_authority_holds_contract_signers_only(world):
    snap = SignersSource(world["L"] / "policy", world["L"] / "routes" / "trust.pub").current()
    auth = contract_authority(snap)
    assert set(auth.keys) == {world["keys"]["phone"].fp}
    assert auth.key(world["keys"]["phone"].fp).public_key == world["keys"]["phone"].public_key


# --- the run object -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("root", [None, "relative/repo", "/", "/a/../b", "/a/", "/a\0b", 7])
def test_a_dispatch_run_with_a_non_canonical_repo_root_does_not_resolve(world, root):
    run = dict(world["run"], repo_root=root)
    assert resolved_from({"session": "s", "kind": "dispatch", "run": run}) is None


def test_a_five_key_run_resolves_files_and_never_takes_a_grant(world, caplog):
    """L3 r1 F: CONTROL's run without repo_root (the shape before W2-c2) is valid; only the grant needs the root."""
    run5 = {k: v for k, v in world["run"].items() if k != "repo_root"}
    assert resolved_from({"session": "s", "kind": "dispatch", "run": run5}).run == run5
    assert resolved_from({"session": "s", "kind": "dispatch", "run": dict(run5, extra=1)}) is None
    b = Broker(QueueStore(world["L"] / "queue"), OneResolver(Resolved(session="sess-5", kind="dispatch", run=run5)),
               SignersSource(world["L"] / "policy", world["L"] / "routes" / "trust.pub"), clock=world["clock"],
               contracts=world["L"] / "contracts", contracts_owner=os.getuid())
    plain = frame(world, "five-plain", "Write", cc(world, "src/b.py"))
    plain.pop("grant")
    got = b.handle(1, plain)
    assert got["ok"] is True and ask(b, got["request_id"])["pending"] is True
    caplog.set_level("INFO", logger="levain.qbroker")
    assert outcome(world, b, frame(world, "five-grant", "Write", cc(world, "src/a.py"))) == "queued"
    assert "CONTROL named no worktree root" in caplog.text


# --- dispatch publishing ----------------------------------------------------------------------------------------

def test_dispatch_publishes_only_on_the_signature_and_repairs_a_lost_file(world, tmp_path, keys):
    rec = _rec(world)
    assert published.record_problem(rec) is None
    assert rec["digest"] == world["job"].digest and parse_contract(rec["contract"]).digest == rec["digest"]
    assert rec["job"]["job_id"] == world["job"].id and rec["payload"]["signer_fp"] == keys["phone"].fp
    assert oct(world["contract"].stat().st_mode & 0o777) == "0o640"
    os.unlink(world["contract"])
    world["store"].get(world["job"].id)                 # any operation re-syncs
    assert world["contract"].read_bytes() == world["genuine"]
    # a stray record for no live job is removed; other names are left alone
    stray = world["L"] / "contracts" / ("0" * 64 + ".json")
    stray.write_bytes(b"{}")
    (world["L"] / "contracts" / "README").write_text("x")
    world["store"].sweep()
    assert not stray.exists() and (world["L"] / "contracts" / "README").exists()


def test_a_filed_unsigned_job_is_not_published(world):
    signed = parse_contract(_rec(world)["contract"])
    job, _ = world["store"].file(submission(signed.repo, signed.base, read=["src/b.py"], write=["src/run.sh"]),
                                 resolve_template=registry(), policy=POLICY, filer_session="planner-2")
    assert job.state == "filed"
    assert sorted(os.listdir(world["L"] / "contracts")) == [world["contract"].name]


def test_a_publish_that_fails_never_fails_the_operation(world):
    os.chmod(world["L"] / "contracts", 0o500)
    try:
        assert world["store"].get(world["job"].id).state == "signed"
    finally:
        os.chmod(world["L"] / "contracts", 0o755)


# --- grant.py: the two broker entry points ----------------------------------------------------------------------

def test_the_input_bound_digest_is_the_path_bound_digest():
    paths = ["/r/src/a.py"]
    assert input_bound_digest("Write", paths, value_sha256(CC_WRITE)) == path_bound_digest("Write", paths, CC_WRITE)
    for bad in ("A" * 64, "0" * 63, 7):
        with pytest.raises(ValueError):
            input_bound_digest("Write", paths, bad)


def test_paths_decision_holds_the_paths_to_grant_paths_shape(world):
    contract = parse_contract(_rec(world)["contract"])
    r = world["root"]
    assert isinstance(paths_decision(contract, "claude_code", "Write", [f"{r}/src/a.py"], root=r), Allow)
    for agent, tool, paths in [("claude_code", "Write", [f"{r}/src/new.py", f"{r}/src/a.py"]),
                               ("claude_code", "Write", [f"{r}/src/a.py", f"{r}/src/new.py"]),
                               ("claude_code", "Write", []), ("claude_code", "Write", ["src/a.py"]),
                               ("claude_code", "apply_patch", [f"{r}/src/a.py"]),
                               ("codex", "Write", [f"{r}/src/a.py"]), ("claude_code", "Bash", [f"{r}/src/a.py"]),
                               ("claude_code", "Write", "nope")]:
        assert isinstance(paths_decision(contract, agent, tool, paths, root=r), Queue), (agent, tool, paths)
    assert isinstance(paths_decision(contract, "codex", "apply_patch", [f"{r}/src/a.py", f"{r}/src/new.py"],
                                     root=r), Allow)


# --- L3 r1 fixes (W2C2_L3_r1_TRIAGE.md; the RUN_r2.txt cases) -------------------------------------------------------

def _file_and_sign(w, write, goal):
    store, keys = w["store"], w["keys"]
    job, cap = store.file(submission(w["repo"], w["base"], read=["src/a.py"], write=write,
                                     grant=["Edit", "Write", "apply_patch"], goal=goal),
                          resolve_template=registry(), policy=POLICY, filer_session="planner-r2")
    p = dsign.sign_payload(job, signer_fp=keys["phone"].fp, signed_at=store._clock.now)
    return store.sign(job.id, p, keys["phone"].sign(dsign.sign_message(p), dsign.DISPATCH_SIGN_NAMESPACE),
                      w["log"].source), cap


def _operator_answer(w, rid, decision, *, sign=True):
    row = QueueStore(w["L"] / "queue").read_request(rid)
    ap = answer_payload(queue_id=row["queue_id"], request=request_of(row["request"]), decision=decision,
                        signer_fp=w["keys"]["laptop"].fp, signed_at=utc_stamp(w["clock"].now),
                        receipts_head=Receipts(w["L"] / "queue", create=False).head())
    sig = w["keys"]["laptop"].sign(answer_message(ap), ANSWER_NAMESPACE) if sign else "x"
    (w["L"] / "queue" / "answers" / f"{rid}.json").write_bytes(canonical_bytes(
        {"format": ANSWER_ROW_FORMAT, "payload": ap, "signature": sig}))


def _receipts(w, rid):
    conn = sqlite3.connect(f"file:{w['L'] / 'queue' / 'requests' / 'receipts.db'}?mode=ro", uri=True)
    try:
        return [(s, json.loads(f)) for s, f in conn.execute(
            "SELECT state, face FROM receipts WHERE request_id = ? ORDER BY seq", (rid,))]
    finally:
        conn.close()


def test_a_granted_request_has_its_own_receipt_and_reads_settled(world):
    """Head ruling 1020+49 (the S2b-1 merge): pending then allowed, the grant row the authority, no signed head;
    the operator lists it settled and is refused an answer payload."""
    b = broker(world)
    rid = b.handle(1, frame(world, "deny", "Write", cc(world, "src/a.py")))["request_id"]
    assert QueueStore(world["L"] / "queue").has_grant(rid) and ask(b, rid)["decision"] == "allow"
    recs = _receipts(world, rid)
    assert [s for s, _ in recs] == ["pending", "allowed"]
    face = recs[1][1]
    assert face["gate"] == {"verdict": "approved", "by": "binding", "binding_id": f"grant-{rid}"}
    assert face["authority_scope"]["grantor"] == "binding"
    assert face["authority_scope"]["binding_id"] == f"grant-{rid}"
    assert face["authority_scope"]["grant"] == (f"contract-grant:{world['run']['contract_digest']} "
                                                f"signer={world['keys']['phone'].fp}")
    assert "receipts_head" not in json.dumps(face) and "signed-answer" not in json.dumps(face)
    listed = {r.request_id: r for r in qoperator.rows(world["L"] / "queue", now=world["clock"].now)}
    assert listed[rid].state == "allowed" and listed[rid].settled
    with pytest.raises(qoperator.OperatorError, match="already allowed"):
        qoperator.answer_payload(world["L"] / "queue", rid, "deny", world["keys"]["laptop"].fp,
                                 now=world["clock"].now)


def test_a_signed_deny_for_a_granted_request_is_refused_and_the_grant_stands(world):
    """Head ruling 1020+49 (supersedes W2-c3 L3 r1 D, 'his deny wins'): a granted request is SETTLED; an answer
    written past the operator library, signed by an enrolled key over the real head, is refused, never recorded."""
    b = broker(world)
    rid = b.handle(1, frame(world, "deny", "Write", cc(world, "src/a.py")))["request_id"]
    before = _receipts(world, rid)
    _operator_answer(world, rid, "deny")
    assert ask(b, rid)["decision"] == "allow"
    assert broker(world).handle(1, {"v": 1, "op": "ANSWER?", "id": next(_ids), "cap": "cap-g",
                                    "request_id": rid})["decision"] == "allow"
    assert _receipts(world, rid) == before


def test_an_answer_that_is_not_authority_does_not_unseat_the_grant_and_refuses_alone(world):
    b = broker(world)
    granted = b.handle(1, frame(world, "bad-g", "Write", cc(world, "src/a.py")))["request_id"]
    _operator_answer(world, granted, "deny", sign=False)
    assert ask(b, granted)["decision"] == "allow"
    queued = b.handle(1, frame(world, "bad-q", "Write", cc(world, "src/b.py")))["request_id"]
    _operator_answer(world, queued, "deny", sign=False)
    assert ask(b, queued)["refused"] == "answer-refused"


def test_a_snapshot_read_that_raises_queues_the_call(world, caplog):
    """L3 r1 I."""
    b = broker(world)

    def boom():
        raise OSError("half-written snapshot")
    b._signers.current = boom
    caplog.set_level("INFO", logger="levain.qbroker")
    got = b.handle(1, frame(world, "snap-raise", "Write", cc(world, "src/a.py")))
    assert got["ok"] is True and not QueueStore(world["L"] / "queue").has_grant(got["request_id"])
    assert "the signer snapshot could not be read (OSError)" in caplog.text


def test_one_write_that_fails_still_removes_a_cancelled_jobs_record(world, caplog):
    """L3 r1 A: removals run first and each write in its own guard; the failure is logged."""
    job_a, cap_a = _file_and_sign(world, ["src/b.py"], "Job A: fix b.py.")
    path_a = world["L"] / "contracts" / published.file_name(job_a.digest)
    assert path_a.exists()
    job_b, _ = world["store"].file(submission(world["repo"], world["base"], read=["src/a.py"], write=["src/run.sh"],
                                              grant=["Write"], goal="Job B: fix run.sh."),
                                   resolve_template=registry(), policy=POLICY, filer_session="planner-r2")
    blocker = world["L"] / "contracts" / published.file_name(job_b.digest)
    blocker.mkdir()
    (blocker / "x").write_text("x")
    p = dsign.sign_payload(job_b, signer_fp=world["keys"]["phone"].fp, signed_at=world["store"]._clock.now)
    world["store"].sign(job_b.id, p, world["keys"]["phone"].sign(dsign.sign_message(p),
                                                                  dsign.DISPATCH_SIGN_NAMESPACE), world["log"].source)
    caplog.set_level("ERROR", logger="levain.dispatch")
    assert world["store"].cancel_by_planner(job_a.id, cap_a).state == "cancelled"
    assert not path_a.exists() and world["contract"].exists()
    assert f"{blocker.name} could not be written" in caplog.text


def test_a_removal_that_fails_stops_no_other_removal_or_write(world, tmp_path):
    d = world["L"] / "contracts"
    stuck = d / ("a" * 64 + ".json")
    stuck.mkdir()
    (stuck / "x").write_text("x")
    stray = d / ("b" * 64 + ".json")
    stray.write_bytes(b"{}")
    os.unlink(world["contract"])
    failures = published.sync(d, [_rec(world)])
    assert [f for f in failures if stuck.name in f and "could not be removed" in f] and len(failures) == 1
    assert not stray.exists() and world["contract"].read_bytes() == world["genuine"]


def test_sync_fsyncs_the_directory_after_removals(world, monkeypatch):
    """L3 r1 C."""
    d = world["L"] / "contracts"
    (d / ("c" * 64 + ".json")).write_bytes(b"{}")
    synced = []
    real = os.fsync

    def spy(fd):
        synced.append(os.fstat(fd).st_ino)
        return real(fd)
    monkeypatch.setattr(published.os, "fsync", spy)
    assert published.sync(d, [_rec(world)]) == []
    assert os.stat(d).st_ino in synced


def test_sync_removes_its_own_stale_temp_names_only(world):
    """L3 r1 J."""
    d = world["L"] / "contracts"
    old, young, other = d / ".tmp-123-0123456789abcdef", d / ".tmp-124-fedcba9876543210", d / ".tmp-notours"
    for p in (old, young, other):
        p.write_bytes(b"x")
    past = old.stat().st_mtime - published.TMP_STALE - 1
    os.utime(old, (past, past))
    assert published.sync(d, [_rec(world)]) == []
    assert not old.exists() and young.exists() and other.exists()


def test_a_record_over_the_brokers_bound_is_not_published(world, tmp_path):
    """L3 r1 H: a contract text under the door's limit, all quotes, doubles when embedded."""
    from levain.dispatch.cli import _CONTRACT_MAX
    d = tmp_path / "big"
    d.mkdir()
    fat = dict(_rec(world), digest="sha256:" + "1" * 64, contract='"' * (_CONTRACT_MAX - 4096))
    assert len(canonical_bytes(fat)) > grants.PUBLISHED_MAX
    failures = published.sync(d, [fat])
    assert os.listdir(d) == [] and len(failures) == 1 and "over the broker's" in failures[0]


def test_a_store_logs_a_record_it_will_not_publish_and_withdraws_it(world, monkeypatch, caplog):
    monkeypatch.setattr(grants, "PUBLISHED_MAX", len(world["genuine"]) - 1)
    caplog.set_level("ERROR", logger="levain.dispatch")
    world["store"].get(world["job"].id)
    assert not world["contract"].exists()
    assert "not published, so its calls queue" in caplog.text


# --- S2b-1 merge L3 r1 (S2B1_MERGE_L3_r1_TRIAGE.md S1-S3; the s2b1_fix1_run cases) -----------------------------------

def _rids(w):
    return sorted(r["request"]["id"] for r in QueueStore(w["L"] / "queue").rows())


def _pause_in(obj, name, thread_name):
    """``obj.name`` waits, in the thread called ``thread_name`` only, until released; (paused, release)."""
    paused, release = threading.Event(), threading.Event()
    real = getattr(obj, name)

    def hooked(*a, **k):
        if threading.current_thread().name == thread_name:
            paused.set()
            release.wait(20)
        return real(*a, **k)
    setattr(obj, name, hooked)
    return paused, release


def _filed_without_receipts(w, b, label):
    """A granted request whose receipt write failed at its filing: its row and its grant row, no receipt."""
    def boom(*a, **k):
        raise ReceiptsError("injected")
    b._receipts.granted = b._receipts.filed = boom
    assert b.handle(1, frame(w, label, "Write", cc(w, "src/a.py")))["refused"] == "broker-error"
    del b._receipts.granted, b._receipts.filed
    (rid,) = _rids(w)
    assert QueueStore(w["L"] / "queue").has_grant(rid) and _receipts(w, rid) == []
    return rid


def test_s1_a_replay_waits_out_the_filing_in_flight_and_never_files_a_granted_request_pending(world):
    """S1: thread A has linked the row and not yet written its grant row; the replay (B) waits on A's flight, so
    nothing is pending in between, an operator deny is never recorded, and the grant settles it."""
    b = broker(world)
    msg = frame(world, "s1", "Write", cc(world, "src/a.py"))
    paused, release = _pause_in(b, "_write_grant", "A")
    out = {}
    a = threading.Thread(name="A", target=lambda: out.__setitem__("A", b.handle(1, msg)))
    a.start()
    assert paused.wait(20)
    (rid,) = _rids(world)
    replay = threading.Thread(name="B", target=lambda: out.__setitem__("B", b.handle(1, dict(msg, id=next(_ids)))))
    replay.start()
    replay.join(1.0)
    try:
        assert replay.is_alive() and _receipts(world, rid) == []
        listed = {r.request_id: r for r in qoperator.rows(world["L"] / "queue", now=world["clock"].now)}
        assert listed[rid].state == "filing" and not listed[rid].settled
        _operator_answer(world, rid, "deny")
        assert "decision" not in ask(b, rid)
    finally:
        release.set()
        a.join(20)
        replay.join(20)
    assert out["A"]["request_id"] == out["B"]["request_id"] == rid
    assert [s for s, _ in _receipts(world, rid)] == ["pending", "allowed"]
    assert ask(b, rid)["decision"] == "allow"


def test_s1_a_replay_after_a_failed_receipt_write_repairs_it_from_the_grant_row(world):
    b = broker(world)
    rid = _filed_without_receipts(world, b, "s1-rep")
    msg = frame(world, "s1-rep", "Write", cc(world, "src/a.py"))
    assert b.handle(1, msg)["request_id"] == rid
    assert [s for s, _ in _receipts(world, rid)] == ["pending", "allowed"]


def test_s2_two_polls_recovering_one_grant_both_answer_allow(world):
    """S2: poll A stops at its receipts write; poll B records the grant whole; A's write then finds the request
    allowed by this same grant and succeeds (it was answer-refused)."""
    b = broker(world)
    rid = _filed_without_receipts(world, b, "s2")
    paused, release = _pause_in(b._receipts, "granted", "A")
    out = {}
    a = threading.Thread(name="A", target=lambda: out.__setitem__("A", ask(b, rid)))
    a.start()
    try:
        assert paused.wait(20)
        assert ask(b, rid)["decision"] == "allow"
    finally:
        release.set()
        a.join(20)
    assert out["A"]["decision"] == "allow"
    assert [s for s, _ in _receipts(world, rid)] == ["pending", "allowed"]


def test_s2_a_grant_row_does_not_answer_for_a_request_allowed_by_anything_else(world):
    b = broker(world)
    rid = _filed_without_receipts(world, b, "s2o")
    q = QueueStore(world["L"] / "queue")
    request = request_of(q.read_request(rid)["request"])
    stamp = utc_stamp(world["clock"].now)
    with b._receipts._write() as conn:
        Receipts._append(conn, request, "pending", stamp)
        Receipts._append(conn, request, "allowed", stamp, world["keys"]["laptop"].fp)
    assert ask(b, rid)["refused"] == "answer-refused"
    assert b._receipts.granted(request, q.read_grant(rid), stamp) is False
    # the same grant again is success, and writes nothing; another grant's binding is not
    rid2 = b.handle(1, frame(world, "s2-same", "Write", cc(world, "src/new.py")))["request_id"]
    request2, grant2 = request_of(q.read_request(rid2)["request"]), q.read_grant(rid2)
    before = _receipts(world, rid2)
    assert b._receipts.granted(request2, grant2, stamp) is True and _receipts(world, rid2) == before
    assert b._receipts.granted(request2, dict(grant2, signer_fp="SHA256:other"), stamp) is False
    assert _receipts(world, rid2) == before


def _forge_grant(w, rid, change):
    path = w["L"] / "queue" / "requests" / f"grant-{rid}.json"
    row = json.loads(path.read_bytes())
    change(row)
    path.unlink()
    path.write_bytes(canonical_bytes(row))


_OTHER = "pend-2026-10-19T20:02:00.000000Z-0000000000000000"
_FORGERIES = [
    pytest.param(lambda r: r.__setitem__("request_id", _OTHER), "names another request", id="another-request"),
    pytest.param(lambda r: r.pop("signer_fp"), "not a levain-queue-grant-allow/1", id="no-signer-fp"),
    pytest.param(lambda r: r.__setitem__("contract_digest", "sha256:" + "0" * 64), "another contract",
                 id="another-contract"),
]


@pytest.mark.parametrize("change, why", _FORGERIES)
def test_s3_a_refused_grant_row_at_filing_files_plain_pending_and_is_logged(world, caplog, change, why):
    b = broker(world)
    real = b._write_grant

    def forging(who, request, granted, now):
        real(who, request, granted, now)
        _forge_grant(world, request.id, change)
    b._write_grant = forging
    caplog.set_level("WARNING", logger="levain.qbroker")
    got = b.handle(1, frame(world, f"s3f-{why}", "Write", cc(world, "src/a.py")))
    assert got["ok"] is True
    rid = got["request_id"]
    assert [s for s, _ in _receipts(world, rid)] == ["pending"]
    listed = {r.request_id: r for r in qoperator.rows(world["L"] / "queue", now=world["clock"].now)}
    assert listed[rid].state == "pending" and not listed[rid].settled
    assert f"grant row for {rid} refused, filed as no grant: " in caplog.text and why in caplog.text


@pytest.mark.parametrize("change, why", _FORGERIES)
def test_s3_a_refused_grant_row_at_start_reconciles_plain_pending_and_the_broker_starts(world, caplog, change, why):
    b = broker(world)
    rid = _filed_without_receipts(world, b, f"s3s-{why}")
    _forge_grant(world, rid, change)
    caplog.set_level("WARNING", logger="levain.qbroker")
    b2 = broker(world)
    assert [s for s, _ in _receipts(world, rid)] == ["pending"]
    assert f"grant row for {rid} refused, filed as no grant: " in caplog.text and why in caplog.text
    assert ask(b2, rid)["refused"] == "answer-refused"      # the client reads it as pending: the operator decides


def test_s3_the_start_reconciles_a_valid_grant_whose_hold_ended(world):
    b = broker(world)
    rid = _filed_without_receipts(world, b, "s3-late")
    world["clock"].now = BNOW + timedelta(minutes=10)
    broker(world)
    assert [s for s, _ in _receipts(world, rid)] == ["pending", "allowed"]
    q = QueueStore(world["L"] / "queue")
    row, grant = q.read_request(rid), q.read_grant(rid)
    assert "hold ended" in grant_allow_refusal(row, grant, world["clock"].now)
    assert grant_allow_refusal(row, grant, world["clock"].now, check_hold=False) is None


def test_s3_a_grant_whose_receipts_raise_in_reconcile_is_that_rows_plain_pending(world, caplog):
    b = broker(world)
    rid = _filed_without_receipts(world, b, "s3-raise")
    q = QueueStore(world["L"] / "queue")
    request = request_of(q.read_request(rid)["request"])
    caplog.set_level("ERROR", logger="levain.qbroker")
    bad = {k: v for k, v in q.read_grant(rid).items() if k != "contract_digest"}
    assert b._receipts.reconcile([request], utc_stamp(world["clock"].now), grants={rid: bad}) == 1
    assert [s for s, _ in _receipts(world, rid)] == ["pending"]
    assert f"the grant receipts of {rid} could not be built" in caplog.text
