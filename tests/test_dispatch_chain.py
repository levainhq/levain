"""S14 R3 remainder: the CHAINED test of the design page (section 7), filed to adopted, on ONE real CONTROL.

THE PARTIES THAT DECIDE ARE REAL: the job store and its real signature (an ssh-keygen key enrolled in a real signed
authority log, `JobStore.sign`), the c6 rig's CONTROL (real signed two-route map, its ticket store, its event journal,
served a second time on a socket as the `_levain_dispatch` peer for the follower), the runner (`JobRunner.tick`), the
launcher and its session index, the verify stage, the follower (`EventFollower`, `ControlResolve`, `SupervisorFacts`),
`record_verification`, and `land` (a real git repository, a human's real signature over the authorisation). Every
fact is read back from CONTROL's journal, the job store and the repository.

WHAT IS A FAKE, NAMED (each is something that does not exist in this tree, not a convenience):
* the slot runtime of the EXECUTOR run (`Runtime`: a gated child that exits), as in R1/R2;
* root's side of both sessions: ATTACH and the state walk to RETAINED are driven by this test through CONTROL's own
  verbs, as root's start/end would (`bind_executor`, `FakeVerifierRuntime`);
* the VERIFIER SESSION itself (`FakeVerifierRuntime`): the helper's own start answers `verb-unbuilt:verifier`, so this
  runtime is the root-side start that is unbuilt, and it writes the envelope the verifier driver would;
* the three sources `Readers` names (the envelope bytes, the artifact tree, the executor's answered model): the tree is a
  real git tree object the test wrote, the envelope is the fake runtime's, the answered model the route's own."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from datetime import timedelta
from pathlib import Path

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch import adopt
from levain.dispatch import follow as F
from levain.dispatch import follow_run as FR
from levain.dispatch import sign as dsign
from levain.dispatch import verifier_report as vr
from levain.dispatch import verify_stage as VS
from levain.dispatch.adopt import DISPATCH_AUTHORISE_NAMESPACE, authorise_message, authorise_payload
from levain.dispatch.registry import active_policy
from levain.dispatch.run_jobs import run_key
from levain.slot import control as C
from levain.slot import control_client as CC
from levain.slot import records as R
from tests import c3_rig as R3
from tests import test_dispatch_control as tc
from tests import test_dispatch_verifier_report as tvr
from tests.test_dispatch_contract import NOW, POLICY, registry, template
from tests.test_dispatch_run_jobs import MODELS, World, cdir, keys, log, pytestmark, py, store, w  # noqa: F401
from tests.test_dispatch_run_jobs_exit import end
from tests.test_dispatch_run_jobs_verify import submit_verifier
from tests.test_routemaps import DISPATCH_UID
from tests.test_slot_control_events import END_CHAIN
from tests.test_tickets import d_attach_claims

NS = dsign.DISPATCH_SIGN_NAMESPACE
BUNDLE = b"a git bundle, as far as the take can tell\n" * 8
A_FIX = {"src/a.py": b"a = 2\n"}
#: the contract's seven verifier fields, written out here so the fake's envelope does NOT come from the projection the
#: code under test uses (`verifier_report.contract_entry`): an envelope built by that function would equal the run's
#: verifier whatever the function did
SEVEN = ("verifier_id", "verifier_artifact_digest", "declared_kind", "driver_template", "driver_template_digest",
         "steps_digest", "driver_input_digest")


def git(repo: Path, *args: str, feed: bytes | None = None, **extra: str) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": os.environ.get("HOME", "/"), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "op", "GIT_AUTHOR_EMAIL": "op@x.invalid", "GIT_COMMITTER_NAME": "op",
           "GIT_COMMITTER_EMAIL": "op@x.invalid", "LC_ALL": "C", **extra}
    return subprocess.run(["git", "-C", str(repo), *args], input=feed, capture_output=True, env=env, timeout=60,
                          check=True).stdout.decode().strip()


def make_tree(repo: Path, base: str, change: dict, tmp: Path) -> str:
    """``base``'s tree with ``change`` (path -> bytes) applied through a scratch index: a real tree object."""
    index = str(tmp / f"index-{os.urandom(4).hex()}")
    git(repo, "read-tree", base, GIT_INDEX_FILE=index)
    for p, data in change.items():
        sha = git(repo, "hash-object", "-w", "--stdin", feed=data)
        git(repo, "update-index", "--add", "--cacheinfo", f"100644,{sha},{p}", GIT_INDEX_FILE=index)
    return git(repo, "write-tree", GIT_INDEX_FILE=index)


def ask(w: World, who: int, op: str, **kw) -> dict:
    ans = w.control.handle(C.Conn(who), {"v": 1, "op": op, "id": 1, **kw})
    assert ans.get("ok") is True, (op, ans)
    return ans


def cas(w: World, session: str, frm: str, to: str, **fields) -> None:
    gen = w.control.store.get(session).generation
    ask(w, 0, "CAS", session=session, generation=gen, fields=fields, **{"from": frm, "to": to})


def retire(w: World, session: str, **last_fields) -> None:
    """Root's end of a session: ALLOCATED -> RENDERING, REVOKE, then the END chain to RETAINED, ``last_fields`` set on
    the verifier's report digest riding the move into RETAINED (A0) and the executor's artifact digest a field-only CAS
    on RETAINED (D0), the only edges CONTROL accepts them on."""
    cas(w, session, R.ALLOCATED, R.RENDERING)
    ask(w, 0, "REVOKE", session=session)
    for frm, to in END_CHAIN[:-1]:
        cas(w, session, frm, to)
    report = {k: v for k, v in last_fields.items() if k.startswith("verifier_report")}
    artifact = {k: v for k, v in last_fields.items() if k not in report}
    cas(w, session, R.CREDENTIAL_PARKED, R.RETAINED, **report)
    if artifact:
        cas(w, session, R.RETAINED, R.RETAINED, **artifact)


def attach(w: World, ticket: str, session: str, slot: int, **claims) -> None:
    """The dispatch lease on ``ticket`` and root's ATTACH of ``session`` to it (the c6 rig's `Run` does the same)."""
    nonce = ask(w, DISPATCH_UID, "LEASE-OPEN", ticket=ticket)["lease_nonce"]
    cls = claims.get("class", "egress")
    ask(w, 0, "ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=session,
        claims=d_attach_claims(ticket, operator_uid=R3.OP_UID, slot={"class": cls, "index": slot}, **claims))


class FakeVerifierRuntime:
    """The root-side START of a verifier session, which does not exist (`verb-unbuilt:verifier`), done through CONTROL's
    own verbs: bind the minted ticket (class ``none``), write the envelope the verifier driver would from the facts
    CONTROL holds for the ticket, then walk the session to RETAINED carrying the envelope's digest."""

    def __init__(self, w: World, bodies: dict | None = None) -> None:
        self.w, self.envelopes, self.sessions, self.bodies = w, {}, {}, bodies or {}
        self.problems: list = []

    def start(self, ticket: str, inputs=None) -> None:
        w = self.w
        n = len(self.sessions)
        session = f"s-{0xfe00 + n:016x}"
        ans = w.port.call("RUN-TICKET-VERIFIER", ticket=ticket)
        facts, entry = ans["facts"], ans["verifier"]
        run = vr.VerifierRun(facts["job_id"], facts["run_index"], session, ticket, {k: entry[k] for k in SEVEN},
                             list(facts["verifier_steps"]), facts["driver_input_digest"], facts["input_bundle_digest"])
        self.problems.append((vr.run_problem(run), sorted(entry), dict(entry)))
        body = self.bodies.get(facts["run_index"], tvr.body(("pass",) * len(run.steps)))
        raw, digest = tvr.envelope(run, body)
        self.envelopes[(session, ticket)] = raw
        self.sessions[ticket] = session
        attach(w, ticket, session, n, **{"class": "none"}, kind="verifier", auth="none", slot_rev=1, lease_seq=1)
        retire(w, session, verifier_report_digest=digest)

    def envelope(self, session: str, ticket: str):
        return self.envelopes.get((session, ticket))


def bind_executor(w: World, job, i: int, export: Path) -> tuple[str, str]:
    """Run ``i``'s executor session as root's side makes it visible: its (index-record) session bound to its ticket,
    walked to RETAINED with an artifact digest, and the export directory root would publish. Returns (session, ticket)."""
    ph = w.launcher.run_phase(run_key(job.id, i))
    session, ticket = ph.session, ph.ticket
    attach(w, ticket, session, i + 1, **{"class": "egress"})
    obj = {"session": session, "ticket": ticket, "manifest_digest": "sha256:" + "c" * 64, "bundle": "bundle",
           "bundle_digest": "sha256:" + hashlib.sha256(BUNDLE).hexdigest(), "files": 1, "bytes": len(BUNDLE)}
    text = json.dumps(obj).encode()
    art = "sha256:" + hashlib.sha256(text).hexdigest()   # the event names the sha256 of the exported file's own bytes
    retire(w, session, artifact_digest=art)
    (export / session).mkdir(parents=True)
    (export / session / "dispatch-artifact.json").write_bytes(text)
    (export / session / "bundle").write_bytes(BUNDLE)
    return session, ticket


def test_a_job_goes_from_filed_to_adopted_through_one_real_control(w, keys, log, tmp_path):  # noqa: F811
    # ---- file and SIGN (the real signature path) ----------------------------------------------------------------
    job = w.job(state="filed", verifier_hash=tc.BB)       # the template pins the active verifier entry
    assert job.state == "filed"
    payload = dsign.sign_payload(job, signer_fp=keys["laptop"].fp, signed_at=NOW + timedelta(minutes=1))
    signed = w.jobs.sign(job.id, payload, keys["laptop"].sign(dsign.sign_message(payload), NS), log.source)
    assert signed.state == "signed"
    git(w.repo, "checkout", "-q", "-b", "work")                          # main must not be checked out: a land adopts

    # ---- the runner takes it: k = 2 priced executor tickets ------------------------------------------------------
    submit_verifier(w)
    rt = FakeVerifierRuntime(w)
    w.runner.verify = VS.VerifyStage(w.port, w.cfg.active, rt)
    out = w.runner.tick()
    assert [s.action for s in out] == ["waiting"], out
    assert w.jobs.get(job.id).state == "running"
    for i in range(2):
        f = w.facts(job.id, i)
        assert (f["kind"], f["job_id"], f["run_index"], f["contract_digest"]) == ("executor", job.id, i, job.digest)
    # the executor's runtime was handed the stager's `workspace` and cut each run's staging from the signed read list: the
    # clone of what it staged holds exactly the contract's read list, nothing else of the repo
    read = sorted(e.path for e in w.jobs.get(job.id).contract.read)
    assert w.runtime.staged == [read, read], w.runtime.staged
    assert os.listdir(w.tmp / "work") == []                              # no workdir outlives the launch

    # ---- both runs end; root's side retains each session with an artifact; the follower takes both -----------------
    end(w, 0), end(w, 1)
    export = tmp_path / "export"
    export.mkdir()
    for i in range(2):
        bind_executor(w, job, i, export)
    sockdir = tempfile.mkdtemp(prefix="lvchain-")       # a unix socket path must stay short (tmp_path is too long)
    path = Path(sockdir) / "c.sock"
    server = C.ControlServer(w.control, path, peer_uid=lambda _s: DISPATCH_UID)
    server.start()
    try:
        tree = make_tree(w.repo, w.base, A_FIX, tmp_path)
        answered = {i: MODELS[i] for i in range(2)}
        readers = VS.Readers(tree=lambda _j, _r: tree, envelope=rt.envelope, answered=lambda _j, r: answered[r])
        resolve = VS.ControlResolve(w.port, w.jobs, readers)
        sup = FR.SupervisorFacts(w.port)
        follower = F.EventFollower.at(w.jobs, path, resolve, read_deadline=0.5, export_dir=export,
                                      routemap=sup.routemap, ticket_for=sup.ticket_for)
        assert follower.step().outcome == CC.DEADLINE
        assert w.runner.tick() and w.jobs.get(job.id).state == "verifying", w.jobs.get(job.id).state

        # ---- verify: one verifier ticket per run, started once; the follower records them ---------------------------
        assert len(rt.sessions) == 2, rt.problems
        assert len(set(rt.sessions.values())) == 2
        assert set(SEVEN) == vr.VERIFIER_KEYS                                 # the literal above is the contract's
        assert [p[0] for p in rt.problems] == [None, None], rt.problems       # each minted run is a contract entry
        assert follower.step().outcome == CC.DEADLINE
    finally:
        server.stop()
        shutil.rmtree(sockdir, ignore_errors=True)
    done = w.jobs.get(job.id)
    assert done.state == "verified", (done.state, [(e.cause, e.evidence) for e in done.history][-1], rt.problems[:1])
    recs = w.jobs.run_records(job.id)
    assert [r.run for r in recs] == [0, 1] and {r.tree for r in recs} == {tree}
    assert [r.model["identity_source"] for r in recs] == ["routemap", "routemap"]
    assert len({r.model["backend_identity"] for r in recs}) == 2
    assert all(r.verifier["result"] == "pass" for r in recs)

    # ---- recommend, authorise (his real signature) and land -------------------------------------------------------
    rec = recs[0]
    onto = "main"
    pl = authorise_payload(done, rec, onto=onto, signer_fp=keys["laptop"].fp, signed_at=NOW + timedelta(minutes=6))
    sig = keys["laptop"].sign(authorise_message(pl), DISPATCH_AUTHORISE_NAMESPACE)
    w.jobs._clock.now = NOW + timedelta(minutes=10)
    reg = registry(template(executor="claude", models=MODELS, k=2, verifier_hash=tc.BB))
    landing = adopt.land(w.jobs, job.id, landing_tier=lambda repo, paths: "T2", resolve_template=reg,
                         policy=POLICY, onto=onto, human=(pl, sig, log.source))
    assert landing.outcome == "adopted", (landing.outcome, landing.message)
    final = w.jobs.get(job.id)
    assert final.state == "adopted"
    assert git(w.repo, "rev-parse", "main^{tree}") == tree                    # main holds the verified tree
    assert git(w.repo, "show", "main:src/a.py") == "a = 2"
