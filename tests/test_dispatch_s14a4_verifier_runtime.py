"""S14 A4: ``HelperVerifierRuntime`` (the ``_levain_dispatch`` side of a verifier session's start) and the seam
(``VerifierInputs``, ``VerifyStage.run(job, i, taken)``, D7's closed-ticket fail).

The helper is a REAL subprocess, a scripted fake that records its argv, its stdin and, at ``render``, what is staged, so
the order of the verbs and what each is given are read off a log. The lease is a fake (CONTROL's LEASE-OPEN is not under
test: ``test_slot_dispatch_runtime`` runs the real one for the executor). NOT run here, named in the return: root's side
of a verifier ``start`` (A1/A2), a real ``sudo``, a real ``prepare none verifier``, the full ``test_dispatch_chain`` with
this runtime (that rig fails on the integration tip with ``edge-not-allowed``, s14rig's)."""
from __future__ import annotations

import hashlib
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.agents import session_launcher as L
from levain.contracts import canon
from levain.dispatch import verify_stage as VS
from levain.slot import Refused
from levain.slot import dispatch_runtime as DR
from levain.slot.runtime_grammar import parse_stdin
from levain.slot.runtime_helper import PREPARE_RESULT_FORMAT

SESSION = "s-00000000000000b1"
NONCE = "A" * 43      # a canonical handle (the grammar checks the spare bits)
import base64
TICKET = base64.urlsafe_b64encode(b"TICKET-SENTINEL-VERIFIER-0001!!!").rstrip(b"=").decode()   # a real handle
BUNDLE = b"a git bundle, as far as the stager can tell\n" * 5
BDIGEST = "sha256:" + hashlib.sha256(BUNDLE).hexdigest()
DRIVER_INPUT = canon.canonical_bytes({"steps": [{"exit": 0}]})
STEPS = [{"argv": ["pytest", "-q"], "wall_s": 30}, {"argv": ["true"], "wall_s": 5}]

FAKE_HELPER = '''\
import hashlib, json, os, sys
here = sys.argv[0]
cfg = json.load(open(here + ".cfg"))
verb = sys.argv[1]
stdin = sys.stdin.read() if verb in ("prepare", "render") else ""
rec = {"argv": sys.argv[1:], "stdin": stdin}
if verb == "render":
    st = cfg["staging"]
    rec["staged"] = sorted(os.listdir(st))
    rec["bundle_sha"] = hashlib.sha256(open(st + "/repo.bundle", "rb").read()).hexdigest() if os.path.exists(st + "/repo.bundle") else None
    rec["driver_input"] = open(st + "/driver_input.json").read() if os.path.exists(st + "/driver_input.json") else None
with open(here + ".log", "a") as f:
    f.write(json.dumps(rec) + "\\n")
spec = cfg.get(verb, {})
if verb == "start":
    sys.stderr.write(spec.get("err", ""))
    sys.exit(spec.get("rc", 0))
sys.stdout.write(spec["line"] + "\\n")
sys.exit(spec.get("rc", 0))
'''


def line(**obj) -> str:
    return canon.canonical_bytes(obj).decode()


class Helper:
    def __init__(self, tmp: Path, store: Path) -> None:
        self.script = tmp / "fake_helper.py"
        self.script.write_text(FAKE_HELPER)
        self.staging = store / "staging" / SESSION
        self.staging.mkdir(parents=True)
        self.cfg = {
            "staging": str(self.staging),
            "prepare": {"line": line(ok=True, format=PREPARE_RESULT_FORMAT, session=SESSION,
                                     slot={"class": "none", "index": 1}, staging=str(self.staging))},
            "render": {"line": line(ok=True)},
            "teardown": {"line": line(ok=True, state="RETAINED", released=False)},
            "start": {"rc": 0},
        }
        self.write()

    def write(self) -> None:
        Path(str(self.script) + ".cfg").write_text(json.dumps(self.cfg))

    def script_(self, verb: str, **spec) -> None:
        self.cfg[verb] = spec
        self.write()

    @property
    def prefix(self):
        return (sys.executable, str(self.script))

    def calls(self) -> list:
        p = Path(str(self.script) + ".log")
        return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []

    def verbs(self) -> list:
        return [c["argv"][0] for c in self.calls()]


class FakeLease:
    def __init__(self) -> None:
        self.calls, self.closed = [], 0

    def call(self, op, **fields):
        self.calls.append((op, fields))
        return {"lease_nonce": NONCE}

    def close(self):
        self.closed += 1


def inputs(tmp: Path, *, data: bytes = BUNDLE, digest: str = BDIGEST, steps=STEPS) -> VS.VerifierInputs:
    p = tmp / "takes" / "bundle"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return VS.VerifierInputs(p, digest, DRIVER_INPUT, list(steps), "output_compare")


@pytest.fixture
def rig(tmp_path):
    store = tmp_path / "store"
    h = Helper(tmp_path, store)
    lease = FakeLease()
    slot = DR.HelperSlotRuntime(host="darwin", helper=h.prefix, store=store, client_factory=lambda _p: lease,
                                kill_grace_s=0.5)
    return h, lease, slot, DR.HelperVerifierRuntime(slot), tmp_path


# ---- the verb order, and what each verb is given ----------------------------------------------------------------------------

def test_the_order_is_lease_prepare_stage_render_start_then_the_waiters_end(rig):
    h, lease, slot, rt, tmp = rig
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters()
    assert h.verbs() == ["prepare", "render", "start", "teardown"]
    assert lease.calls == [("LEASE-OPEN", {"ticket": TICKET})]
    prep, render = h.calls()[0], h.calls()[1]
    assert prep["argv"] == ["prepare", "none", "verifier", "claude", "none"]
    assert json.loads(prep["stdin"]) == {"lease_nonce": NONCE, "ticket": TICKET}
    # what was staged when render ran: the bundle (hash equal) and the driver input, nothing else
    assert render["staged"] == ["driver_input.json", "repo.bundle"]
    assert render["bundle_sha"] == hashlib.sha256(BUNDLE).hexdigest()
    assert render["driver_input"] == DRIVER_INPUT.decode()
    assert h.calls()[2]["argv"] == ["start", SESSION] and h.calls()[3]["argv"] == ["teardown", SESSION]


def test_the_render_blob_is_the_tickets_steps_and_the_driver_and_passes_the_helpers_grammar(rig):
    h, lease, slot, rt, tmp = rig
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters()
    blob = canon.parse(h.calls()[1]["stdin"].encode())
    assert blob["vendor"] == "claude" and blob["routes"] == [] and blob["driver"] == "output_compare"
    assert blob["verifier_steps"] == STEPS and set(blob) == {"format", "vendor", "routes", "verifier_steps", "driver"}
    parsed = parse_stdin("render", h.calls()[1]["stdin"].encode())
    assert parsed.driver == "output_compare" and len(parsed.verifier_steps) == 2


def test_prepare_stdin_satisfies_the_real_dispatch_grammar_for_none_verifier(rig):
    from levain.slot.runtime_grammar import parse_runtime_argv
    h, lease, slot, rt, tmp = rig
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters()
    req = parse_runtime_argv(h.calls()[0]["argv"], configured_slots={"egress": 4, "none": 4}, caller="dispatch")
    assert (req.slot_class, req.kind, req.vendor, req.auth) == ("none", "verifier", "claude", "none")
    got = parse_stdin("prepare", h.calls()[0]["stdin"].encode())
    assert got.ticket == TICKET


# ---- the bundle: hashed as it copies; a mismatch is refused BEFORE render --------------------------------------------------

def test_a_bundle_that_is_not_the_tickets_is_refused_before_render_and_the_session_is_ended_once(rig):
    h, lease, slot, rt, tmp = rig
    with pytest.raises(Refused) as e:
        rt.start(TICKET, inputs(tmp, data=BUNDLE + b"tampered"))
    assert e.value.name == "verifier-bundle-mismatch"
    assert h.verbs() == ["prepare", "teardown"]                     # no render, no start; the known session ENDed once
    assert not (h.staging / "repo.bundle").exists()                # the copy that did not match is gone
    assert lease.closed == 1                                       # closed on the proven END


def test_a_missing_bundle_is_unreadable_and_a_symlink_is_not_followed(rig):
    h, lease, slot, rt, tmp = rig
    inp = VS.VerifierInputs(tmp / "nope", BDIGEST, DRIVER_INPUT, STEPS, "output_compare")
    with pytest.raises(Refused) as e:
        rt.start(TICKET, inp)
    assert e.value.name == "verifier-bundle-unreadable" and h.verbs() == ["prepare", "teardown"]
    (tmp / "real").write_bytes(BUNDLE)
    (tmp / "link").symlink_to(tmp / "real")
    h2 = Path(str(h.script) + ".log")
    h2.unlink()
    with pytest.raises(Refused) as e:
        rt.start(TICKET, VS.VerifierInputs(tmp / "link", BDIGEST, DRIVER_INPUT, STEPS, "output_compare"))
    assert e.value.name == "verifier-bundle-unreadable"


def test_a_staging_name_that_already_exists_is_a_refusal_not_an_overwrite(rig):
    h, lease, slot, rt, tmp = rig
    (h.staging / "repo.bundle").write_bytes(b"planted")
    with pytest.raises(Refused) as e:
        rt.start(TICKET, inputs(tmp))
    assert e.value.name == "staging-dest-exists" and (h.staging / "repo.bundle").read_bytes() == b"planted"
    assert h.verbs() == ["prepare", "teardown"]


def test_the_prepare_format_literal_is_the_helpers():
    assert DR._PREPARE_RESULT_FORMAT == PREPARE_RESULT_FORMAT


def test_both_bundle_refusals_are_permanent_for_the_stage():
    assert VS.PERMANENT_START_REFUSALS == {"verifier-bundle-mismatch", "verifier-bundle-unreadable"}


# ---- D3: the waiter ENDs, and the lease closes only on the proof --------------------------------------------------------------

def test_the_lease_is_closed_only_after_the_proven_end(rig):
    h, lease, slot, rt, tmp = rig
    seen = []
    real = slot._teardown
    slot._teardown = lambda s: (seen.append(lease.closed), real(s))[1]
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters()
    assert seen == [0] and lease.closed == 1                       # open while END ran, closed after it proved
    assert slot.final_tunnel_bytes(SESSION) is None and SESSION in slot._ended


def test_an_unproven_end_leaves_the_lease_open_and_close_at_shutdown_closes_it(rig):
    h, lease, slot, rt, tmp = rig
    h.script_("teardown", line=line(ok=True, state="HELD", released=False))
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters()
    assert lease.closed == 0 and SESSION not in slot._ended
    rt.close()                                                     # the shared lease table: CONTROL revokes on EOF
    assert lease.closed == 1


def test_a_start_that_exits_nonzero_is_still_ended_once(rig):
    h, lease, slot, rt, tmp = rig
    h.script_("start", rc=3, err="boom")
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters()
    assert h.verbs().count("teardown") == 1 and lease.closed == 1


def test_a_start_past_its_bound_is_ended_by_the_waiter(rig, monkeypatch):
    h, lease, slot, rt, tmp = rig
    h.script_("start", rc=0)
    monkeypatch.setattr(DR, "verifier_wait_bound", lambda steps: 0.0)   # the bound is read at start
    rt.start(TICKET, inputs(tmp))
    assert rt.join_waiters() and h.verbs().count("teardown") == 1


def test_the_wait_bound_is_every_step_the_driver_the_clone_roots_lock_waits_and_kill_proofs_and_a_slack():
    """S14 fix 5 (L2 M2): the bound covers root's real worst case. Asserts the per-spawn constants equal root's, and
    the bound for k=1 and k=16 (the contract's most steps, each at a long wall) is walls + driver + clone + (k+1) x
    (2 lock waits + kill proof + reap) + 2 lock waits + slack. RED on the old code: the bound was walls + driver + clone
    + slack, short by (k+1) x 75 s + 60 s, so a 16-step ticket was ended ~1335 s early as no-report:ACTIVE (and the new
    constants did not exist)."""
    from levain.slot import proc as PR
    from levain.slot import verifier_session as vs
    assert DR.DRIVER_WALL_S == vs.DRIVER_WALL_S                      # pinned to root's V26
    assert DR.ROOT_LOCK_WAIT_S == vs.LOCK_WAIT_S
    assert DR.ROOT_KILL_PROOF_S >= max(PR.V12_SECONDS, PR.V12K_SECONDS)
    assert DR.ROOT_REAP_GRACE_S == vs._REAP_GRACE_S
    per = 2 * vs.LOCK_WAIT_S + max(PR.V12_SECONDS, PR.V12K_SECONDS) + vs._REAP_GRACE_S
    one = [{"argv": ["true"], "wall_s": 5}]
    assert DR.verifier_wait_bound(one) == 5 + vs.DRIVER_WALL_S + DR.CLONE_WALL_S + 2 * per + 2 * vs.LOCK_WAIT_S \
        + DR.WAIT_SLACK_S
    long = [{"argv": ["pytest"], "wall_s": 3600}] * 16
    assert DR.verifier_wait_bound(long) == 16 * 3600 + vs.DRIVER_WALL_S + DR.CLONE_WALL_S + 17 * per \
        + 2 * vs.LOCK_WAIT_S + DR.WAIT_SLACK_S
    # STEPS (two steps, walls 30 + 5)
    assert DR.verifier_wait_bound(STEPS) == 35 + vs.DRIVER_WALL_S + DR.CLONE_WALL_S + 3 * per + 2 * vs.LOCK_WAIT_S \
        + DR.WAIT_SLACK_S


@pytest.mark.parametrize("step", ["render", "start"])
def test_a_failure_after_prepare_ends_once_and_closes_the_lease_on_proof(rig, step, monkeypatch):
    h, lease, slot, rt, tmp = rig
    if step == "render":
        h.script_("render", line=line(ok=False, refused="bad"), rc=1)
    else:
        class Boom(Exception):
            pass

        def gated(*a, **k):
            raise Boom()
        monkeypatch.setattr(DR._ctl, "GatedChild", gated)
    with pytest.raises(Exception):
        rt.start(TICKET, inputs(tmp))
    assert h.verbs().count("teardown") == 1 and lease.closed == 1 and rt.join_waiters()


def test_a_prepare_refusal_closes_the_lease_with_no_teardown(rig):
    h, lease, slot, rt, tmp = rig
    h.script_("prepare", line=line(ok=True, format="x"))
    with pytest.raises(Refused) as e:
        rt.start(TICKET, inputs(tmp))
    assert e.value.name == "prepare-answer-malformed" and h.verbs() == ["prepare"] and lease.closed == 1


def test_a_none_class_answer_with_a_gwport_or_an_egress_slot_is_malformed(rig):
    h, lease, slot, rt, tmp = rig
    for bad in (dict(slot={"class": "egress", "index": 1}), dict(gwport=47100)):
        base = dict(format=PREPARE_RESULT_FORMAT, session=SESSION, slot={"class": "none", "index": 1},
                    staging=str(h.staging))
        base.update(bad)
        h.script_("prepare", line=line(ok=True, **base))
        with pytest.raises(Refused) as e:
            rt.start(TICKET, inputs(tmp))
        assert e.value.name == "prepare-answer-malformed"


def test_a_closed_runtime_refuses_a_start_and_ends_it(rig):
    h, lease, slot, rt, tmp = rig
    rt.close()
    with pytest.raises(L.LauncherRefused) as e:
        rt.start(TICKET, inputs(tmp))
    assert e.value.name == "runtime-closed" and h.verbs().count("teardown") == 1 and "render" not in h.verbs()


def test_the_blob_is_checked_by_the_grammar_before_anything_is_allocated(rig):
    h, lease, slot, rt, tmp = rig
    with pytest.raises(Refused):
        rt.start(TICKET, inputs(tmp, steps=[{"argv": [], "wall_s": 1}]))
    assert h.verbs() == [] and lease.calls == []


# ---- P10: the ticket is written nowhere but the lease frame and prepare's stdin ---------------------------------------------

def test_the_ticket_is_in_the_lease_frame_and_prepare_stdin_only(rig, caplog):
    h, lease, slot, rt, tmp = rig
    h.script_("start", rc=1, err="stderr noise with no ticket")
    h.script_("teardown", line=line(ok=True, state="HELD", released=False))     # exercise the warning paths too
    with caplog.at_level(logging.DEBUG):
        rt.start(TICKET, inputs(tmp))
        assert rt.join_waiters()
        (h.staging / "repo.bundle").unlink()
        (h.staging / "driver_input.json").unlink()
        with pytest.raises(Refused):
            rt.start(TICKET, inputs(tmp, data=b"other"))
    for c in h.calls():
        if c["argv"][0] != "prepare":
            assert TICKET not in json.dumps(c)
        assert TICKET not in " ".join(c["argv"])
    assert TICKET in h.calls()[0]["stdin"]
    assert TICKET not in caplog.text
    for f in h.staging.iterdir():
        assert TICKET.encode() not in f.read_bytes()


# ---- the stage: inputs, closed tickets, the permanent refusals ---------------------------------------------------------------

class _Control:
    """A CONTROL whose attempt is known `unbound` (or in `state`), recording cancels; its record of the ticket
    (`RUN-TICKET-VERIFIER`, S14 fix 2) names STEPS, the `output_compare` template and the digest of `driver_input`."""

    def __init__(self, state="unbound", driver_input=None):
        self.state, self.ops = state, []
        self.driver_input = driver_input if driver_input is not None else canon.canonical_bytes({"steps": [{"exit": 0}]})

    def call(self, op, **kw):
        self.ops.append(op)
        if op == "RUN-TICKET-STATUS":
            return {"ticket": "tk-1", "state": self.state}
        if op == "RUN-TICKET-CANCEL":
            self.state = "cancelled"
            return {"ok": True}
        if op == "RUN-TICKET-VERIFIER":
            assert kw == {"ticket": "tk-1"}
            return {"facts": {"kind": "verifier", "verifier_steps": STEPS,
                              "driver_input_digest": "sha256:" + hashlib.sha256(self.driver_input).hexdigest()},
                    "verifier": {"driver_template": "output_compare"}, "state": self.state}
        raise AssertionError(op)


def _active(driver_input=None):
    entry = {"verifier_id": "v1", "driver_template": "output_compare", "steps": STEPS,
             "driver_input": driver_input or {"steps": [{"exit": 0}]}}
    return entry


def test_the_stage_hands_the_runtime_the_taken_bundle_the_entrys_driver_input_and_steps(monkeypatch, tmp_path):
    entry = _active()
    monkeypatch.setattr(VS.registry, "verifier_ticket",
                        lambda items, verifier_id: {"verifier_steps": entry["steps"], "entry": entry})
    got = []
    rt = SimpleNamespace(start=lambda t, i: got.append((t, i)))
    control = _Control()
    stage = VS.VerifyStage(control, lambda: {}, rt, store_dir=tmp_path)
    job = SimpleNamespace(id="dj-" + "a" * 32, contract=SimpleNamespace(template=SimpleNamespace(verifier_id="v1")))
    taken = SimpleNamespace(bundle_digest=BDIGEST, path="takes/0/bundle")
    assert stage.run(job, 0, taken).action == "started"
    (ticket, inp), = got
    assert ticket == "tk-1" and inp.bundle_path == tmp_path / "takes/0/bundle" and inp.bundle_digest == BDIGEST
    assert inp.driver_input == canon.canonical_bytes(entry["driver_input"])
    assert list(inp.steps) == STEPS and inp.driver_template == "output_compare"
    # an explicit entry_driver_input (the caller's derivation) is the one handed on, when it is the ticket's
    got.clear()
    stage._started.clear()
    control.driver_input = b"{}"
    stage.run(job, 0, taken, b"{}")
    assert got[0][1].driver_input == b"{}"
    # and refused by name, the runtime never asked, when it is not (S14 fix 2)
    got.clear()
    stage._started.clear()
    act = stage.run(job, 0, taken, b'{"other":1}')
    assert (act.action, act.name) == ("refused", "verifier-driver-input-moved") and got == []


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "takes/../../x", "", None])
def test_a_taken_path_that_leaves_the_store_is_refused_not_started(monkeypatch, tmp_path, path):
    entry = _active()
    monkeypatch.setattr(VS.registry, "verifier_ticket",
                        lambda items, verifier_id: {"verifier_steps": entry["steps"], "entry": entry})
    started = []
    stage = VS.VerifyStage(_Control(), lambda: {}, SimpleNamespace(start=lambda t, i: started.append(t)), store_dir=tmp_path)
    job = SimpleNamespace(id="dj-" + "a" * 32, contract=SimpleNamespace(template=SimpleNamespace(verifier_id="v1")))
    act = stage.run(job, 0, SimpleNamespace(bundle_digest=BDIGEST, path=path))
    assert act.action == "refused" and started == []


def test_a_permanent_start_refusal_cancels_the_ticket_and_the_next_pass_reads_it_closed(monkeypatch, tmp_path):
    entry = _active()
    monkeypatch.setattr(VS.registry, "verifier_ticket",
                        lambda items, verifier_id: {"verifier_steps": entry["steps"], "entry": entry})

    def refuse(t, i):
        raise Refused("verifier-bundle-mismatch", "")
    control = _Control()
    stage = VS.VerifyStage(control, lambda: {}, SimpleNamespace(start=refuse), store_dir=tmp_path)
    job = SimpleNamespace(id="dj-" + "a" * 32, contract=SimpleNamespace(template=SimpleNamespace(verifier_id="v1")))
    taken = SimpleNamespace(bundle_digest=BDIGEST, path="takes/0/bundle")
    assert stage.run(job, 0, taken).name == "verifier-bundle-mismatch" and "RUN-TICKET-CANCEL" in control.ops
    nxt = stage.run(job, 0, taken)
    assert (nxt.action, nxt.name) == ("closed", "cancelled")


def test_a_transient_start_refusal_cancels_nothing(monkeypatch, tmp_path):
    entry = _active()
    monkeypatch.setattr(VS.registry, "verifier_ticket",
                        lambda items, verifier_id: {"verifier_steps": entry["steps"], "entry": entry})

    def refuse(t, i):
        raise Refused("helper-timeout", "")
    control = _Control()
    stage = VS.VerifyStage(control, lambda: {}, SimpleNamespace(start=refuse), store_dir=tmp_path)
    job = SimpleNamespace(id="dj-" + "a" * 32, contract=SimpleNamespace(template=SimpleNamespace(verifier_id="v1")))
    assert stage.run(job, 0, SimpleNamespace(bundle_digest=BDIGEST, path="t/b")).name == "helper-timeout"
    assert "RUN-TICKET-CANCEL" not in control.ops


def test_the_unbuilt_runtime_still_refuses_by_name():
    with pytest.raises(Refused) as e:
        VS.UnbuiltVerifierRuntime().start("t", None)
    assert e.value.name == "verifier-start-unbuilt"


# ---- D7 through the runner (the real job store and CONTROL rig of the R3 tests) ------------------------------------------------

from tests.test_dispatch_run_jobs import World, cdir, keys, log, pytestmark as _rig_marks, py, store, w  # noqa: E402,F401
from tests.test_dispatch_run_jobs_verify import BUNDLE as TAKE_BUNDLES, VRuntime, ticket_of, verifying  # noqa: E402

pytestmark = _rig_marks


def test_a_cancelled_verifier_ticket_fails_the_job_verifier_refused_with_the_state(w):
    job, rt = verifying(w)
    assert w.runner.verify_job(job.id).action == "verifying"
    w.port.call("RUN-TICKET-CANCEL", ticket=ticket_of(w, job, 0))
    s = w.runner.verify_job(job.id)
    assert s.action == "failed" and s.job.state == "failed"
    last = w.jobs.get(job.id).history[-1]
    assert (last.cause, last.evidence) == ("verifier_refused", "ticket-closed:cancelled")


def test_verify_job_passes_the_taken_artifact_whole_and_the_stores_directory(w):
    job, rt = verifying(w)
    seen = []
    w.runner.verify = VS.VerifyStage(w.port, w.cfg.active, rt, store_dir=w.jobs.directory)
    w.runner.verify_job(job.id)
    assert [i.bundle_digest for i in rt.inputs] == list(TAKE_BUNDLES)
    assert [i.bundle_path for i in rt.inputs] == [w.jobs.directory / f"takes/{n}/bundle" for n in range(2)]
    assert all(i.driver_template and i.steps and i.driver_input for i in rt.inputs)


def test_the_fail_door_refuses_a_job_that_is_not_verifying(w):
    from levain.dispatch.jobs import TransitionRefused
    job = w.job()
    with pytest.raises(TransitionRefused):
        w.jobs.fail_verifier_closed(job.id, 0, "cancelled")


# ---- the wiring --------------------------------------------------------------------------------------------------------------

def test_build_runner_defaults_to_the_helper_runtime_sharing_the_executors(tmp_path, monkeypatch):
    from levain.dispatch import run_jobs as RJ
    from levain.dispatch.jobs import JobStore
    import levain.dispatch.cli as dcli
    from levain.slot import layout
    monkeypatch.setattr(dcli, "_control_read", lambda *a, **k: None)
    store = tmp_path / "slotstore"                         # build_runner makes dirs under STORE[host]: /private/var is not writable on Linux
    store.mkdir(mode=0o700)
    monkeypatch.setitem(layout.STORE, "darwin", store)
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    runner = RJ.build_runner(JobStore(d), str(tmp_path / "s.sock"), "darwin")
    rt = runner.verify.runtime
    assert isinstance(rt, DR.HelperVerifierRuntime) and rt._slot is runner.launcher.runtime
    assert runner.verify.store_dir == d
    custom = SimpleNamespace(start=lambda t, i: None)
    assert RJ.build_runner(JobStore(d), str(tmp_path / "s.sock"), "darwin", verify_runtime=custom).verify.runtime is custom
