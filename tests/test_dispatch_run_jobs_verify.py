"""S14 R3 (`levain.dispatch.verify_stage`, `JobRunner.verify_job`, `follow_run.control_resolve`, `run_jobs.main`): the verify
stage and the wiring, on the same c6 rig as R1/R2 (real CONTROL with a signed two-route map and its ticket store, the real
job store, the real session index; only the slot runtimes and the usage sources are fakes).

What is read BACK, never what the runner says: a verifier ticket's facts from CONTROL's own journal; the resolve's run,
entry and steps from CONTROL's `RUN-TICKET-VERIFIER`. What is NOT proven here, and why (the return names each): the
verifier SESSION's start is not served by the runtime helper (`verb-unbuilt:verifier`), so the stage's start is a fake
`VerifierRuntime`; no S1 code writes the verifier envelope, so the resolve's readers are fakes."""
from __future__ import annotations

import hashlib
import json
import os
import sys

import pytest

from levain.dispatch import follow_run as FR
from levain.dispatch import run_jobs as RJ
from levain.dispatch import verify_stage as VS
from levain.dispatch.jobs import JobStore, TakenArtifact
from levain.dispatch.run_jobs import run_key
from levain.slot import Refused
from levain.slot import control as C
from tests.test_dispatch_run_jobs import MODELS, World, cdir, keys, log, pytestmark, py, store, w  # noqa: F401
from tests.test_dispatch_run_jobs_exit import end, run
from tests import test_dispatch_control as tc
from tests.test_routemaps import DISPATCH_UID, STORE_ID

BUNDLE = ["sha256:" + f"{0xb0 + i:064x}" for i in range(2)]


class VRuntime:
    """The fake `VerifierRuntime`: records the tickets it was asked to start."""

    def __init__(self, refuse: str | None = None) -> None:
        self.started, self.refuse = [], refuse

    def start(self, ticket: str, inputs=None) -> None:
        self.inputs = getattr(self, "inputs", []) + [inputs]
        if self.refuse:
            raise Refused(self.refuse, "")
        self.started.append(ticket)


def submit_verifier(w: World) -> None:
    """CONTROL's own verifier generation 1 (CONFIG-SUBMIT, signed by the rig's store key): the same entries the operator
    accepted into dispatch's config store, so a verifier mint resolves against CONTROL's record."""
    from levain.slot import routemaps as RM
    from tests.test_routemaps import STAMP
    it = {"format": RM.CONFIG_FORMAT, "store_id": w.rig.st.head()["store_id"] if "store_id" in w.rig.st.head() else STORE_ID,
          "generation": 1, "prev": "", "issued_at": STAMP, "kind": RM.VERIFIER_KIND, "body": tc.VERIFIERS}
    it, sig = w.rig.st.signed(it)
    w.port.call("CONFIG-SUBMIT", item=it, sig=sig)


def verifying(w: World, *, bundle=True, runtime=None, **jobkw):
    """A job taken to `verifying` the way the follower and the runner do it: runs end, artifacts taken (with a bundle
    digest), `runs_exited`. Returns (job, runner with a verify stage, the VRuntime)."""
    rt = runtime if runtime is not None else VRuntime()
    submit_verifier(w)
    w.runner.verify = VS.VerifyStage(w.port, w.cfg.active, rt)
    job, _ = run(w, **jobkw)
    end(w, 0)
    end(w, 1)
    w.runner.settle_job(job.id)                                   # the poll records both runs `ended`
    for i in range(2):
        ph = w.launcher.run_phase(run_key(job.id, i))
        assert w.jobs.record_take(TakenArtifact(ph.session, ph.ticket, "sha256:" + f"{i + 1:064x}",
                                                BUNDLE[i] if bundle else None,
                                                f"takes/{i}/bundle" if bundle else None, 0, 0))
    if not bundle:
        # a nothing-new run: the runner fails it at once (run_failed/no-change), so the store's own `runs_exited` takes
        # it to `verifying` here, which is what VerifyStage's no-bundle refusal still guards
        runs = {i: (w.launcher.run_phase(run_key(job.id, i)).session, w.launcher.run_phase(run_key(job.id, i)).ticket)
                for i in range(2)}
        w.jobs.runs_exited(job.id, runs)
    else:
        assert w.runner.settle_job(job.id).action == "verifying"
    return job, rt


def vfacts(w: World, job_id: str, i: int) -> dict:
    ticket = w.port.call("RUN-TICKET-STATUS", attempt_id=VS.verifier_attempt(job_id, i))["ticket"]
    return dict(w.control._ticket_store()._by_ticket[ticket].facts)


# ---- the first check: is a verifier slot started by HelperSlotRuntime? ---------------------------------------------------

def test_the_runtime_helpers_start_does_not_serve_a_verifier_slot():
    """Section 9 item 1, answered: the helper's own `start` serves a verifier session (S14 A2a: `verifier_session.run`)
    but `HelperSlotRuntime.start` is the egress/dispatch shape (it names its ticket's routes, which a verifier ticket has none
    of). Pinned from the source so a change in either flips this test, never silently."""
    from pathlib import Path
    import levain.slot.dispatch_runtime as DR
    import levain.slot.start as ST
    start_src = Path(ST.__file__).read_text()
    assert 'raise Refused("verb-unbuilt:verifier"' not in start_src
    assert "VS.run(ctx, a, _verifier_hooks(ctx, a))" in start_src
    rt_src = Path(DR.__file__).read_text()
    assert '"prepare", "egress", "dispatch", req.harness, req.auth' in rt_src
    from levain.slot.dispatch_runtime import HelperSlotRuntime
    assert not hasattr(HelperSlotRuntime, "start_verifier")


# ---- mint and start ---------------------------------------------------------------------------------------------------------

def test_each_run_gets_one_verifier_ticket_of_the_derived_fields_started_once(w):
    job, rt = verifying(w)
    s = w.runner.verify_job(job.id)
    assert s.action == "verifying" and s.detail == "0:minted,1:minted"
    reg = VS.registry.verifier_ticket(w.cfg.active(), verifier_id=job.contract.template.verifier_id)
    for i in range(2):
        f = vfacts(w, job.id, i)
        assert f["kind"] == "verifier"
        assert (f["job_id"], f["run_index"], f["contract_digest"]) == (job.id, i, job.digest)
        assert f["reserved_microdollars"] == 0 and f["egress_routes"] == [] and "route" not in f
        assert f["input_bundle_digest"] == BUNDLE[i]
        assert f["verifier_generation"] == reg["verifier_generation"] and f["verifier_steps"] == reg["verifier_steps"]
        assert f["driver_input_digest"] == reg["driver_input_digest"]
    assert len(rt.started) == 2 and len(set(rt.started)) == 2
    # a second pass mints and starts nothing more (CONTROL replays the attempt; this process remembers its starts)
    again = w.runner.verify_job(job.id)
    assert again.detail == "0:waiting,1:waiting"
    assert len(rt.started) == 2 and w.port.ops.count("RUN-TICKET-MINT") == 2 + 2


def test_a_run_whose_artifact_has_no_bundle_mints_nothing(w):
    job, rt = verifying(w, bundle=False)
    s = w.runner.verify_job(job.id)
    assert s.detail == "0:refused:no-bundle,1:refused:no-bundle"
    assert rt.started == [] and w.port.ops.count("RUN-TICKET-MINT") == 2          # only the two executor tickets


def test_the_unbuilt_default_runtime_refuses_by_name_and_the_mint_is_not_repeated(w):
    job, _ = verifying(w)
    w.runner.verify = VS.VerifyStage(w.port, w.cfg.active)                          # the production default
    first = w.runner.verify_job(job.id)
    assert first.detail == "0:refused:verifier-start-unbuilt,1:refused:verifier-start-unbuilt"
    again = w.runner.verify_job(job.id)
    assert again.detail == first.detail
    # each attempt minted one ticket (replayed by its attempt id on the second pass)
    assert len({w.port.call("RUN-TICKET-STATUS", attempt_id=VS.verifier_attempt(job.id, i))["ticket"]
                for i in range(2)}) == 2


def test_a_verifier_that_does_not_resolve_mints_nothing(w):
    job, rt = verifying(w)
    w.runner.verify = VS.VerifyStage(w.port, lambda: {}, rt)                        # no verifier item active
    s = w.runner.verify_job(job.id)
    assert s.detail.startswith("0:refused:verifier-unresolved") and rt.started == []
    assert w.port.ops.count("RUN-TICKET-MINT") == 2


def test_a_stage_that_is_not_wired_and_a_job_that_is_not_verifying_touch_nothing(w):
    job = w.job()
    assert w.runner.verify_job(job.id).action == "skipped"
    w.runner.verify = VS.VerifyStage(w.port, w.cfg.active, VRuntime())
    assert w.runner.verify_job(job.id).action == "skipped"                          # signed, not verifying


class FlakyRuntime(VRuntime):
    """Refuses its first start (transient, so the ticket stays minted and unbound), then starts."""

    def start(self, ticket: str, inputs=None) -> None:
        self.refuse = "helper-timeout" if not hasattr(self, "inputs") else None
        super().start(ticket, inputs)


def _moved(active, **entry_over):
    """The active items with the verifier item moved on one generation, every entry's fields replaced by ``entry_over``."""
    from types import SimpleNamespace
    items = dict(active)
    item = items["verifier"]
    body = json.loads(json.dumps(item.body))
    body["verifiers"] = [{**e, **entry_over} for e in body["verifiers"]]
    items["verifier"] = SimpleNamespace(generation=item.generation + 1, body=body)
    return items


def test_a_config_change_between_mint_and_start_still_starts_the_tickets_steps_and_template(w):
    """S14 fix 2 (L1 F2): the start's steps and driver template are CONTROL's record of the MINTED ticket, never the
    active item read again at start. Asserts: run 0's ticket is minted under generation G1, its first start refused
    (transient); the active item moves to G2 with other steps; the retried start hands the runtime G1's steps (the
    ticket's facts) and the ticket's template. RED on the old code: `_inputs` re-derived from `active()`, so the second
    start carried G2's steps, which root then refused against the ticket."""
    rt = FlakyRuntime()
    job, _ = verifying(w, runtime=rt)
    first = w.runner.verify_job(job.id)
    assert first.detail.startswith("0:refused:helper-timeout")
    g1 = vfacts(w, job.id, 0)
    other = [{"argv": ["sh", "-c", "exit 0"], "wall_s": 7}]
    assert other != g1["verifier_steps"]
    w.runner.verify.active = lambda: _moved(w.cfg.active(), steps=other)
    again = w.runner.verify_job(job.id)
    # run 1 started in the first pass (only the first start is refused), so it is waiting now
    assert again.detail == "0:started,1:waiting"
    started = rt.inputs[2]                                   # [run 0 refused, run 1 started, run 0 started]
    assert list(started.steps) == g1["verifier_steps"]
    held = w.port.call("RUN-TICKET-VERIFIER", ticket=ticket_of(w, job, 0))
    assert started.driver_template == held["verifier"]["driver_template"]
    assert "sha256:" + hashlib.sha256(started.driver_input).hexdigest() == g1["driver_input_digest"]


def test_a_driver_input_moved_since_the_mint_is_refused_by_name_not_started(w):
    """S14 fix 2: CONTROL keeps only the driver input's digest; bytes at hand that are not the ticket's are refused
    `verifier-driver-input-moved` and the runtime is not asked. RED on the old code: the start went ahead with G2's
    bytes, and root refused `driver-input-mismatch` inside a session the start had already opened."""
    rt = FlakyRuntime()
    job, _ = verifying(w, runtime=rt)
    w.runner.verify_job(job.id)
    asked = len(rt.inputs)
    w.runner.verify.active = lambda: _moved(w.cfg.active(), driver_input={"steps": [{"exit": 1}]})
    again = w.runner.verify_job(job.id)
    assert again.detail == "0:refused:verifier-driver-input-moved,1:waiting"     # run 1 started in the first pass
    assert len(rt.inputs) == asked and rt.started == [ticket_of(w, job, 1)]


def test_tick_drives_the_stage_for_a_verifying_job(w):
    job, rt = verifying(w)
    out = w.runner.tick()
    assert any(s.action == "verifying" and s.detail == "0:minted,1:minted" for s in out)
    assert len(rt.started) == 2


# ---- the production resolve, from CONTROL's own record -----------------------------------------------------------------------

def readers(*, envelope=b"{}", tree="a" * 40, answered="glm-5"):
    return VS.Readers(tree=lambda _j, _r: tree, envelope=lambda _s, _t: envelope, answered=lambda _j, _r: answered)


def event(ticket, session="s-" + "9" * 16, **kw):
    return {"kind": "state", "session": session, "ticket": ticket, "state": "retained", **kw}


def ticket_of(w, job, i):
    return w.port.call("RUN-TICKET-STATUS", attempt_id=VS.verifier_attempt(job.id, i))["ticket"]


def test_the_resolve_reads_the_run_the_entry_and_the_steps_from_controls_own_record(w):
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    res = VS.ControlResolve(w.port, w.jobs, readers())
    call = res(event(ticket_of(w, job, 1), verifier_report_digest="sha256:" + "d" * 64))
    f = vfacts(w, job.id, 1)
    assert call is not None and (call.run.job_id, call.run.run) == (job.id, 1)
    assert call.run.ticket == ticket_of(w, job, 1) and call.run.session == "s-" + "9" * 16
    assert call.run.steps == f["verifier_steps"] and call.run.driver_input_digest == f["driver_input_digest"]
    assert call.run.input_bundle_digest == BUNDLE[1]
    assert call.run.verifier["verifier_id"] == job.contract.template.verifier_id
    assert (call.repo, call.tree, call.model, call.envelope) == (job.repo, "a" * 40, {"answered": "glm-5"}, b"{}")
    # the entry is CONTROL's resolved one (its mint line), not what dispatch holds, PROJECTED to the contract's seven
    # (L1 F7): CONTROL's answer holds the eight of RESOLVED_KEYS (the generation too), and an entry passed whole is
    # refused `verifier-entry` by every report's envelope check
    from levain.dispatch.verifier_report import VERIFIER_KEYS, run_problem
    from levain.slot.tickets import RESOLVED_KEYS
    held = w.control._ticket_store()._by_ticket[ticket_of(w, job, 1)].resolved
    assert set(held) == RESOLVED_KEYS and set(held) - VERIFIER_KEYS == {"verifier_generation"}
    assert set(call.run.verifier) == VERIFIER_KEYS
    assert dict(call.run.verifier) == {k: held[k] for k in VERIFIER_KEYS}
    assert run_problem(call.run) is None


def test_the_resolve_answers_nothing_for_a_ticket_dispatch_did_not_mint_as_this_runs_verifier(w):
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    res = VS.ControlResolve(w.port, w.jobs, readers())
    assert res(event("nonesuch")) is None                                                  # CONTROL holds no such ticket
    ph = w.launcher.run_phase(run_key(job.id, 0))
    assert res(event(ph.ticket)) is None                                                   # an EXECUTOR ticket
    # the same facts minted under another attempt id: a verifier ticket, but not the one dispatch's runner minted
    f = vfacts(w, job.id, 0)
    args = {k: f[k] for k in ("job_id", "run_index", "contract_digest", "egress_routes", "reserved_microdollars",
                              "verifier_generation", "verifier_id", "verifier_steps", "driver_input_digest",
                              "input_bundle_digest")}
    w.port.call("RUN-TICKET-CANCEL", ticket=ticket_of(w, job, 1))             # room under the open-ticket ceiling
    other = w.port.call("RUN-TICKET-MINT", attempt_id="dv-" + "0" * 32, kind="verifier", **args)["ticket"]
    assert other != ticket_of(w, job, 0)
    assert res(event(other)) is None
    # a ticket without a session in the event, and a malformed event
    assert res({"ticket": ticket_of(w, job, 0)}) is None and res({}) is None


def test_an_envelope_reader_that_answers_nothing_is_passed_on_as_none(w):
    """S14 L3 F1: an envelope reader's None (absent, retention-expired, a link, over the cap) resolves to a call whose
    envelope is None, which the store's check_envelope fails `report-missing` and the follower acks
    (test_dispatch_follow pins the follower side). Asserts the pass-through, and that a wrong-typed or raising reader is
    still unresolved. RED on the old code: the resolve answered None for a None envelope, so the follower stopped
    `ticket-unresolved` at the event unacked and every restart replayed it, blocking every event behind it."""
    # `answered` is optional (S14 D5), see test_dispatch_s14a5_readers; `tree` is passed on as None (S14 fix 1, below)
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    rd = readers(envelope=None)
    ev = event(ticket_of(w, job, 0), verifier_report_digest="sha256:" + "d" * 64)
    call = VS.ControlResolve(w.port, w.jobs, rd)(ev)
    assert call is not None and call.envelope is None and (call.run.job_id, call.run.run) == (job.id, 0)
    assert VS.ControlResolve(w.port, w.jobs, readers(envelope="{}"))(ev) is None           # not bytes: a broken reader
    boom = VS.Readers(tree=lambda _j, _r: 1 / 0, envelope=readers().envelope, answered=rd.answered)
    assert VS.ControlResolve(w.port, w.jobs, boom)(ev) is None                              # a raising reader: unresolved


def test_a_tree_reader_that_answers_nothing_is_passed_on_as_none(w):
    """S14 fix 1 (HEAD DEFAULT): a None tree resolves to a call whose tree is None, which the store fails
    `verifier-tree-unavailable` (test_dispatch_follow pins the follower side). RED on the old code: the resolve answered
    None, and the follower stopped `ticket-unresolved` at the event for ever."""
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    ev = event(ticket_of(w, job, 0), verifier_report_digest="sha256:" + "d" * 64)
    call = VS.ControlResolve(w.port, w.jobs, readers(tree=None))(ev)
    assert call is not None and call.tree is None and call.envelope == b"{}"
    # a tree that is neither a string nor None is a broken reader: unresolved, never passed on
    assert VS.ControlResolve(w.port, w.jobs, readers(tree=7))(ev) is None


def test_a_bytes_like_envelope_is_normalised_and_a_wrong_typed_answered_resolves_nothing(w):
    """S14 L3 round 2 (three lineages): the docstring says any reader answering a value of the wrong type resolves
    nothing, and a bytes-like envelope is the same bytes. Asserts a bytearray and a memoryview envelope resolve to a call
    whose envelope is exactly ``bytes`` (so the store's check reads it), and an ``answered`` that is neither None nor str
    (bytes, int, dict) resolves None, while an ``answered`` str and None still resolve. RED on the old code: the
    bytearray/memoryview envelope resolved None (the follower stopped `ticket-unresolved`, unacked: the wedge F1 removed
    for None), and a non-str ``answered`` was passed on into the run record as ``{"answered": b"glm-5"}``."""
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    ev = event(ticket_of(w, job, 0), verifier_report_digest="sha256:" + "d" * 64)
    for env in (bytearray(b"{}"), memoryview(b"{}")):
        call = VS.ControlResolve(w.port, w.jobs, readers(envelope=env))(ev)
        assert call is not None and type(call.envelope) is bytes and call.envelope == b"{}", type(env)
    for bad in (b"glm-5", 5, {"model": "glm-5"}):
        assert VS.ControlResolve(w.port, w.jobs, readers(answered=bad))(ev) is None, bad
    assert VS.ControlResolve(w.port, w.jobs, readers(answered="glm-5"))(ev).model == {"answered": "glm-5"}
    assert VS.ControlResolve(w.port, w.jobs, readers(answered=None))(ev).model == {}


def test_a_refused_event_needs_no_reader_and_an_unreachable_control_resolves_nothing(w):
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    ev = event(ticket_of(w, job, 0), verifier_report_refused="verifier-failed:exit")
    call = VS.ControlResolve(w.port, w.jobs, FR.unsourced_readers())(ev)
    assert call is not None and call.envelope is None and call.run.job_id == job.id

    class Down:
        def call(self, op, **kw):
            raise Refused("control-down", "")
    assert VS.ControlResolve(Down(), w.jobs, readers())(ev) is None


def test_the_unsourced_readers_pass_a_report_event_on_with_no_envelope(w):
    """S14 L3 F1: with no envelope source the report event resolves with envelope None (the store fails the job
    `report-missing`). RED on the old code: it resolved nothing (the follower's `ticket-unresolved` wedge)."""
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    ev = event(ticket_of(w, job, 0), verifier_report_digest="sha256:" + "d" * 64)
    call = VS.ControlResolve(w.port, w.jobs, FR.unsourced_readers())(ev)
    assert call is not None and call.envelope is None and call.tree is None


# ---- the verb: read-only, dispatch's, verifier tickets only -------------------------------------------------------------------

def test_run_ticket_verifier_is_dispatchs_and_answers_a_verifier_ticket_only(w):
    job, _ = verifying(w)
    w.runner.verify_job(job.id)
    ans = w.port.call("RUN-TICKET-VERIFIER", ticket=ticket_of(w, job, 0))
    assert ans["facts"]["kind"] == "verifier" and ans["state"] == "unbound" and "session" not in ans
    assert set(ans["verifier"]) == {"verifier_generation", "verifier_id", "verifier_artifact_digest", "declared_kind",
                                    "driver_template", "driver_template_digest", "steps_digest", "driver_input_digest"}
    ph = w.launcher.run_phase(run_key(job.id, 0))
    with pytest.raises(Refused) as e:
        w.port.call("RUN-TICKET-VERIFIER", ticket=ph.ticket)
    assert e.value.name == "ticket-unknown"
    # not the operator's, nor a foreign peer's
    for uid in (os.getuid(), 4242):
        got = w.control.handle(C.Conn(uid), {"v": 1, "op": "RUN-TICKET-VERIFIER", "id": 1,
                                             "ticket": ticket_of(w, job, 0)})
        assert got.get("ok") is not True
    assert "RUN-TICKET-VERIFIER" in C.PEER_VERBS["dispatch"]
    assert all("RUN-TICKET-VERIFIER" not in v for k, v in C.PEER_VERBS.items() if k != "dispatch")
    got = w.control.handle(C.Conn(DISPATCH_UID), {"v": 1, "op": "RUN-TICKET-VERIFIER", "id": 1})
    assert got.get("ok") is False                                                          # the ticket is required


# ---- no_minted_runs is no longer the production resolve ---------------------------------------------------------------------

def test_the_followers_default_resolve_is_the_control_resolve_not_no_minted_runs(tmp_path, monkeypatch):
    seen = {}

    class Runner:
        def __init__(self, follower):
            seen["resolve"] = follower.resolve

        def run(self):
            return None

        def stop(self):
            return None

    monkeypatch.setattr(FR, "FollowRunner", Runner)
    monkeypatch.setattr(FR, "_run_into", lambda runner, failed: None)
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    rc = FR.main(["--store-dir", str(d), "--socket", str(tmp_path / "s.sock"), "--export-dir", str(tmp_path / "e")])
    assert rc == 0
    assert isinstance(seen["resolve"], VS.ControlResolve) and seen["resolve"] is not FR.no_minted_runs
    # an explicit resolve still replaces it
    seen.clear()
    FR.main(["--store-dir", str(d), "--socket", str(tmp_path / "s.sock"), "--export-dir", str(tmp_path / "e")],
            resolve=FR.no_minted_runs)
    assert seen["resolve"] is FR.no_minted_runs


# ---- the process: lock, ticks, the root refusal ----------------------------------------------------------------------------

class FakeRunner:
    def __init__(self):
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        return []


def test_main_ticks_on_a_fixed_interval_holds_its_lock_and_refuses_a_second_runner(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)
    r = FakeRunner()
    waits = []
    rc = RJ.main(["--store-dir", str(d), "--socket", str(tmp_path / "s"), "--interval", "2"],
                 build=lambda store, sock, host: r, wait=lambda s: waits.append(s) or False, max_ticks=3)
    assert rc == 0 and r.ticks == 3 and waits == [2.0, 2.0]
    assert (d / RJ.LOCK_NAME).exists()
    fd = RJ.take_instance_lock(d)
    assert fd is not None
    try:
        rc = RJ.main(["--store-dir", str(d), "--socket", str(tmp_path / "s")], build=lambda *a: r, max_ticks=1)
        assert rc == RJ.EX_INSTANCE_HELD and r.ticks == 3                                  # nothing ticked
    finally:
        os.close(fd)


def test_main_goes_on_after_a_tick_that_raises_and_stops_when_asked(tmp_path):
    d = tmp_path / "d"
    d.mkdir(mode=0o700)

    class Boom(FakeRunner):
        def tick(self):
            self.ticks += 1
            if self.ticks == 1:
                raise RuntimeError("one bad tick")
            return []

    r = Boom()
    stops = iter([False, True])
    rc = RJ.main(["--store-dir", str(d), "--socket", str(tmp_path / "s")], build=lambda *a: r,
                 wait=lambda s: next(stops))
    assert rc == 0 and r.ticks == 2                                                        # the raise did not end the loop


def test_main_refuses_a_relative_path_and_a_bad_interval(tmp_path):
    with pytest.raises(SystemExit):
        RJ.main(["--store-dir", "rel", "--socket", str(tmp_path / "s")], build=lambda *a: FakeRunner(), max_ticks=1)
    with pytest.raises(SystemExit):
        RJ.main(["--store-dir", str(tmp_path), "--socket", str(tmp_path / "s"), "--interval", "0"],
                build=lambda *a: FakeRunner(), max_ticks=1)


def test_main_refuses_to_run_as_root(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    assert RJ.main(["--store-dir", str(tmp_path), "--socket", str(tmp_path / "s")]) == RJ.EX_USAGE


# ---- the sign view's dollars line ------------------------------------------------------------------------------------------

def test_the_envelope_sign_view_says_the_thread_gauge_counts_no_dollars_not_levain():
    from pathlib import Path
    import levain.desk.cli as DC
    src = Path(DC.__file__).read_text()
    assert "Levain counts no dollars" not in src
    assert "the thread's dollars are not metered and never stop a step; " in src
    assert "each dispatch job's dollars are capped per run at the gateway" in src


class _StatusControl:
    """A CONTROL whose RUN-TICKET-STATUS answers the ticket ``tk-1`` in ``state`` (or mints ``minted``)."""

    def __init__(self, state="unbound", minted=None):
        self.state, self.minted = state, minted

    def call(self, op, **fields):
        if op == "RUN-TICKET-STATUS":
            if self.state is None:
                raise Refused("attempt-unknown")
            return {"ticket": "tk-1", "state": self.state}
        assert op == "RUN-TICKET-MINT"
        return self.minted


def test_the_started_set_holds_only_tickets_still_unbound():
    # L3 deepseek M5 / codex L231: _started kept every started ticket for the process's life; a ticket leaves it once
    # CONTROL answers it bound, released, cancelled or expired
    from types import SimpleNamespace
    control = _StatusControl()
    stage = VS.VerifyStage(control, lambda: {}, SimpleNamespace(start=lambda t, i: None))
    stage._inputs = lambda *a: None                                         # the inputs are not under test here
    job = SimpleNamespace(id="dj-" + "a" * 32)
    taken = SimpleNamespace(bundle_digest="sha256:" + "0" * 64, path="takes/0/bundle")
    assert stage.run(job, 0, taken).action == "started" and stage._started == {"tk-1"}
    assert stage.run(job, 0, taken).action == "waiting"                     # never started twice while unbound
    for state, act in (("bound", "waiting"), ("expired", "closed")):
        stage._started.add("tk-1")
        control.state = state
        assert stage.run(job, 0, taken).action == act and stage._started == set()


def test_a_mint_that_answers_no_ticket_is_named_not_closed(monkeypatch):
    # L3 deepseek L4: a mint answer with no ticket read as "closed" (the ticket is over); it is a refusal by name
    from types import SimpleNamespace
    monkeypatch.setattr(VS, "mint_args", lambda *a, **k: {})
    stage = VS.VerifyStage(_StatusControl(state=None, minted={}), lambda: {}, SimpleNamespace(start=lambda t, i: None))
    act = stage.run(SimpleNamespace(id="dj-" + "a" * 32), 0, SimpleNamespace(bundle_digest="sha256:" + "0" * 64))
    assert (act.action, act.name) == ("refused", "mint-no-ticket")
