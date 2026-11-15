"""S14 R1 (`levain.dispatch.run_jobs`): a signed job to k running, priced runs, on the c6 rig's CONTROL.

Every party that decides is real: CONTROL (`control.serve` on a thread, a REAL signed route map with two brokered routes
and its ticket store and ceiling, as `tests/c6_rig.py` builds it), the job store (a real SQLite file, a real git repo, a
real handoff bundle from the filing door's own exporter), the config store holding the verifier item, the launcher
(`RealLauncher(auth="brokered")`) and its session index. Faked: the slot runtime (`SlotRuntime.start`), which hands back a
gated child, and the usage sources. No keys child, no upstream, no network, no real credential: R1 mints and starts, it
does not call a provider.

The facts are read BACK from CONTROL's own ticket journal (`RUN-TICKET-FACTS`), never from what the runner says it sent.
The job is walked to `signed` through the store's own edge (`signature`), as the rest of the suite does; the signing path
has its own tests (`test_dispatch_sign.py`, `test_dispatch_jobs.py`)."""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.agents import session_launcher as L
from levain.agents.session import DispatchBinding, SessionLaunch
from levain.dispatch import cli as dcli
from levain.dispatch import run_jobs as RJ
from levain.dispatch import run_workspace as RW
from levain.dispatch.jobs import JobStore, TransitionRefused
from levain.dispatch.run_jobs import JobRunner, RunnerRefused, run_key, share_micros
from levain.slot import Refused
from levain.slot import control as C
from tests import b2_keys_rig as B2
from tests import c6_rig as R6
from tests import test_dispatch_control as tc
from tests.test_agents_session_launcher import FakeChild, FakeSources
from tests.test_dispatch_contract import NOW, POLICY, make_repo, registry, submission, template
from tests.test_dispatch_envelope import Clock
from tests.test_routemaps import DISPATCH_UID, PRICE, route

keys, log, cdir, store = tc.keys, tc.log, tc.cdir, tc.store          # the control tests' fixtures: the config store

pytestmark = [pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets"),
              pytest.mark.skipif(os.geteuid() == 0, reason="keys refuses to run as root (it runs as _levain_keys)"),
              pytest.mark.skipif(not os.path.exists("/usr/bin/ssh-keygen"), reason="no /usr/bin/ssh-keygen")]

SYS_PY = "/usr/bin/python3"
PYS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])

MODELS = ("z-ai/glm-5.2", "moonshotai/kimi-k3")
CEILING = 5_000_000


def _model(requested: str, backend: str) -> list:
    return [{"requested": requested, "backend": backend, "price": PRICE}]


class Port:
    """The dispatch peer's CONTROL connection: `ControlClient.call`'s contract (a refusal raises `Refused`), over
    `Control.handle` as the `_levain_dispatch` uid. Records every op it sent, so a test says what was never asked."""

    def __init__(self, control: C.Control) -> None:
        self.control, self.ops = control, []

    def call(self, op: str, **fields) -> dict:
        self.ops.append(op)
        ans = self.control.handle(C.Conn(DISPATCH_UID), {"v": 1, "op": op, "id": 1, **fields})
        if ans.get("ok") is not True:
            raise Refused(str(ans.get("refused")), str(ans.get("detail", "")))
        return {k: v for k, v in ans.items() if k not in ("id", "ok")}


class Runtime:
    """`SlotRuntime`: starts nothing real; each start answers its own session id and a gated child. Given the
    ``workspace`` callable the production `HelperSlotRuntime` is given, it calls it with a staging dir as that runtime does
    (after the start is asked, before the child), and records the file list of a clone of the bundle it staged."""

    def __init__(self, workspace=None, staging=None) -> None:
        self.starts, self.children = [], []
        self.workspace, self.staging = workspace, staging
        self.staged: list[list[str]] = []                 # per start: the staged clone's tracked paths
        self.repo_settings = None                         # the production runtime's `repo_settings` callable, asked at start
        self.settings: list = []                          # per start: what it answered

    def start(self, req):
        self.starts.append(req)
        if self.workspace is not None:
            d = Path(tempfile.mkdtemp(dir=self.staging))
            self.workspace(req, d)
            clone = d / "clone"
            subprocess.run(["git", "clone", "-q", str(d / "repo.bundle"), str(clone)], check=True, capture_output=True)
            out = subprocess.run(["git", "-C", str(clone), "ls-tree", "-r", "--name-only", "HEAD"], check=True,
                                 capture_output=True, text=True).stdout
            self.staged.append(out.split())
            if self.repo_settings is not None:
                self.settings.append(self.repo_settings(req))
        child = FakeChild()
        self.children.append(child)
        return L.SlotHandle(f"s-{len(self.starts):016x}", child)

    def end(self, session):
        return True


class World:
    """The c6 rig (`tests/c6_rig.World`: real CONTROL, the keys child, a fake upstream) with a signed map of the two
    brokered routes, and the runner's parties around it."""

    def __init__(self, tmp: Path, cfg, log_, keys_, py: str) -> None:
        self.tmp = tmp
        self.loop = asyncio.new_event_loop()
        routes = [route("glm", host="api.z.ai", codec="anthropic-messages", models=_model(MODELS[0], "zhipu/glm-5.2")),
                  route("kimi", host="api.moonshot.ai", codec="anthropic-messages",
                        models=_model(MODELS[1], "moonshot/kimi-k3"))]
        self.rig = R6.World(py, routes=routes, hosts=["api.z.ai", "api.moonshot.ai"])
        self.loop.run_until_complete(self.rig.__aenter__())
        try:
            self.loop.run_until_complete(self.rig.start_keys())
        except BaseException:
            self.loop.run_until_complete(self.rig.__aexit__(None, None, None))
            raise
        self.control = self.rig.control
        self.port = Port(self.control)
        (tmp / "work").mkdir(mode=0o700)
        (tmp / "staging").mkdir(mode=0o700)
        self.runtime = Runtime()
        self.index = L.SessionIndex(tmp / "idx")
        self.launcher = L.RealLauncher(self.port, self.runtime, FakeSources(), self.index, auth="brokered")
        tc.accept(cfg, log_, keys_, "verifier", 1, tc.VERIFIERS)
        self.cfg = cfg
        d = tmp / "jobs"
        d.mkdir(mode=0o700)
        os.chmod(d, 0o700)
        self.jobs = JobStore(d, clock=Clock(NOW))
        self.repo, self.base = make_repo(tmp / "r")
        self.stager = RW.ReadSetStager(str(tmp / "work"), self.jobs.handoff_row)
        self.runtime.workspace, self.runtime.staging = self.stager.stage, tmp / "staging"
        self.runtime.repo_settings = self.stager.repo_settings
        self.runner = JobRunner(self.jobs, self.launcher, self.port, cfg.active, stager=self.stager)

    def job(self, *, models=MODELS, k=2, cents=150, state="signed", handoff=True, write=None, **tpl):
        reg = registry(template(executor="claude", models=models, k=k, **tpl))
        more = {} if write is None else {"write": write}          # another write file: a second LIVE job beside the first
        job, _cap = self.jobs.file(submission(self.repo, self.base, budget={"tool_calls": 40, "tokens": 200000,
                                                                            "dollars_cents": cents}, **more),
                                   resolve_template=reg, policy=POLICY, filer_session="coord-1",
                                   export=dcli.readset_export if handoff else None)
        self.filed_cap = _cap                                   # the filing planner's capability, the last job's
        return self.jobs._transition(job.id, "signed", "signature") if state == "signed" else job

    def facts(self, job_id: str, i: int) -> dict:
        """The facts CONTROL journaled at the mint of run ``i``'s ticket, found by its attempt id (the run key). Read
        from CONTROL's own ticket store: `RUN-TICKET-FACTS` answers only a ticket a session was BOUND to, and R1's
        runs are minted and started, not yet attached through the lease (the VM half-day's step)."""
        ticket = self.port.call("RUN-TICKET-STATUS", attempt_id=run_key(job_id, i))["ticket"]
        return dict(self.control._ticket_store()._by_ticket[ticket].facts)

    def minted(self) -> int:
        return self.port.ops.count("RUN-TICKET-MINT")

    def close(self) -> None:
        self.loop.run_until_complete(self.rig.__aexit__(None, None, None))
        self.loop.close()


@pytest.fixture(params=PYS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def py(request):
    B2.require_h11(request.param)
    return request.param


@pytest.fixture
def w(tmp_path, store, log, keys, py):
    world = World(tmp_path, store, log, keys, py)
    yield world
    world.close()


def assert_bound(w: World, job, started) -> None:
    """The R1 DONE-when, read from CONTROL's journal: two priced executor tickets carrying the JOB's id, run index 0/1,
    the job's digest and the share. Raises AssertionError (or a CONTROL refusal) for anything else."""
    share = 150 * 10_000 // 2
    assert [r.model for r in started.runs] == list(MODELS)
    for i, r in enumerate(started.runs):
        f = w.facts(job.id, i)
        assert f["kind"] == "executor" and "max_calls" not in f, sorted(f)      # priced, no passthrough caps
        assert (f["job_id"], f["run_index"], f["contract_digest"]) == (job.id, i, job.digest)
        assert f["reserved_microdollars"] == share == 750_000
        assert f["model_requested"] == MODELS[i] and f["route"] == ("glm", "kimi")[i]
        assert r.key == run_key(job.id, i)


# ---- the DONE-when ---------------------------------------------------------------------------------------------------

def test_a_signed_k2_job_goes_running_with_two_priced_executor_tickets(w):
    job = w.job()
    assert job.state == "signed"
    started = w.runner.run_job(job.id)
    assert started.refused is None and started.released and started.unproven == ()
    assert started.job.state == "running" and started.job.history[-1].cause == "slot_taken"
    assert w.jobs.get(job.id).state == "running"
    assert_bound(w, job, started)
    assert w.minted() == 2
    # each run: a gated child this call spawned, released once, under its own key, a started index record
    assert [c.released for c in w.runtime.children] == [True, True]
    for i, r in enumerate(started.runs):
        rec = w.index.get(r.key)
        assert rec["state"] == "started" and rec["auth"] == "brokered" and rec["occurrence_id"] == job.id
        assert w.runtime.starts[i].model == MODELS[i] and w.runtime.starts[i].auth == "brokered"
        assert w.runtime.starts[i].brief_digest == w.facts(job.id, i)["brief_digest"]
    assert started.runs[0].key != started.runs[1].key


def test_the_run_key_is_deterministic_and_a_second_pass_mints_nothing_more(w):
    job = w.job()
    first = w.runner.run_job(job.id)
    assert [r.key for r in first.runs] == [run_key(job.id, 0), run_key(job.id, 1)]
    assert run_key(job.id, 0) == "dj-" + hashlib.sha256(f"{job.id}:0".encode()).hexdigest()[:32]
    # the job is running: the runner takes it no more, and CONTROL holds exactly the two it minted
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "job-not-takable" and w.minted() == 2
    # the same runs asked for again (a restart that finds the job signed): each key holds its record, so the launcher
    # refuses `session-key-already-used` BEFORE any mint, and the runner reports a run it did not mint and will not release
    plan = w.runner.preflight(job)                      # `job` is the signed job as it was read
    again = [w.runner._start(plan, i, MODELS[i]) for i in range(2)]
    assert all(r.child is None and r.refused is None for r in again) and w.minted() == 2


def test_a_share_over_the_ceiling_is_refused_by_name_and_nothing_is_minted(w):
    job = w.job(cents=2000)                                  # 2000 cents over k=2: 10_000_000 micro-dollars a run
    assert 2000 * 10_000 // 2 > CEILING
    started = w.runner.run_job(job.id)
    assert started.refused.startswith("mint-refused:ticket-ceiling"), started.refused
    assert started.released is False and started.runs[0].reason_class == "standing" and len(started.runs) == 1
    with pytest.raises(Refused) as e:
        w.port.call("RUN-TICKET-STATUS", attempt_id=run_key(job.id, 0))
    assert e.value.name == "attempt-unknown"                 # CONTROL's journal holds no ticket for it
    assert w.runtime.starts == [] and w.runtime.children == []
    rec = w.index.get(run_key(job.id, 0))
    assert rec["state"] == "refused" and rec["reason_class"] == "standing"


def test_a_missing_route_refuses_before_the_take_and_nothing_is_minted(w):
    job = w.job(models=(MODELS[0], "nobody/none"))
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "route-resolve:no-brokered-route" and e.value.reason_class == "standing"
    assert w.jobs.get(job.id).state == "signed"              # not taken
    assert w.minted() == 0 and w.runtime.starts == []
    assert w.index.get(run_key(job.id, 0)) is None and w.index.get(run_key(job.id, 1)) is None


def test_share_zero_refuses_by_name_and_nothing_is_taken_or_minted(w, monkeypatch):
    assert share_micros(1, 2) == 5_000 and share_micros(0, 2) == 0 and share_micros(150, 2) == 750_000
    assert share_micros(1, 3) == 3_333                       # floor division: the remainder is reserved by no run
    job = w.job()
    real = RJ.parse_contract(job.contract_text)
    # the parser refuses to FILE a 0-dollar contract, so the runner's own check is what holds if a stored one read 0
    zero = SimpleNamespace(digest=real.digest, template=real.template, budget={**real.budget, "dollars_cents": 0})
    monkeypatch.setattr(RJ, "parse_contract", lambda text: zero)
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "share-zero"
    assert w.jobs.get(job.id).state == "signed" and w.minted() == 0 and w.port.ops == []


# ---- the rest of the preflight, each by name, each before the take -----------------------------------------------------

def test_a_job_with_no_handoff_bundle_is_refused_before_the_take(w):
    job = w.job(handoff=False)
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "no-handoff" and w.jobs.get(job.id).state == "signed" and w.minted() == 0


def test_a_template_whose_verifier_is_not_active_is_refused_before_the_take(w):
    job = w.job(verifier_id="pytest-none")
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "verifier-unresolved" and w.jobs.get(job.id).state == "signed" and w.minted() == 0


def test_a_filed_job_and_an_unknown_job_are_refused_by_name(w):
    filed = w.job(state="filed")
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(filed.id)
    assert e.value.name == "job-not-takable"
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job("dj-" + "0" * 32)
    assert e.value.name == "job-unknown"
    assert w.minted() == 0


def test_a_stored_contract_that_is_not_the_jobs_digest_is_refused(w, monkeypatch):
    job = w.job()
    other = RJ.parse_contract(job.contract_text)
    monkeypatch.setattr(RJ, "parse_contract", lambda text: SimpleNamespace(digest="sha256:" + "0" * 64,
                                                                         template=other.template, budget=other.budget))
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "contract-digest-drift" and w.jobs.get(job.id).state == "signed" and w.minted() == 0


def test_a_take_the_store_refuses_leaves_the_job_and_mints_nothing(w, monkeypatch):
    job = w.job()

    def refuse(job_id, *, envelope=None):
        raise TransitionRefused("another writer took it")

    monkeypatch.setattr(w.jobs, "take_slot", refuse)
    with pytest.raises(TransitionRefused):
        w.runner.run_job(job.id)
    assert w.jobs.get(job.id).state == "signed" and w.minted() == 0


def test_a_run_refused_after_the_first_aborts_the_gated_runs_already_started(w):
    job = w.job()
    real = w.launcher.spawn

    def spawn(launch):
        if launch.binding.run_index == 1:
            raise L.LauncherRefused("mint-refused:no-free-slot", "none", reason_class="transient")
        return real(launch)

    w.launcher.spawn = spawn
    started = w.runner.run_job(job.id)
    assert started.refused == "mint-refused:no-free-slot" and started.released is False
    assert [r.reason_class for r in started.runs] == [None, "transient"]
    assert w.runtime.children[0].aborted == 1 and w.runtime.children[0].released is False   # it ran nothing
    assert started.unproven == ()


# ---- the binding, at the launcher -------------------------------------------------------------------------------------

def test_a_binding_replaces_the_three_occurrence_derived_facts(w):
    bind = DispatchBinding("dj-" + "a" * 32, 1, "sha256:" + "b" * 64)
    launch = SessionLaunch("occ-other", run_key("x", 0), "claude", MODELS[0], "brief", "brokered", None,
                           usd_micros=1000, binding=bind)
    f = w.launcher._facts(launch)
    assert (f["job_id"], f["run_index"], f["contract_digest"]) == (bind.job_id, 1, bind.contract_digest)
    plain = w.launcher._facts(dataclasses.replace(launch, binding=None))
    assert plain["job_id"] == "occ-other" and plain["run_index"] == 0 and plain["contract_digest"] != bind.contract_digest


def test_a_malformed_binding_never_exists_and_a_passthrough_launch_refuses_one():
    for args in (("", 0, "sha256:" + "a" * 64), ("dj-x", -1, "sha256:" + "a" * 64), ("dj-x", True, "sha256:" + "a" * 64),
                 ("dj-x", 0, "sha256:short")):
        with pytest.raises(ValueError):
            DispatchBinding(*args)


def test_a_passthrough_launcher_refuses_a_binding(tmp_path):
    from tests.test_agents_session_launcher import FakeControl, FakeRuntime
    ln = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "i"))
    launch = SessionLaunch("occ", "dj-" + "c" * 32, "claude", MODELS[0], "brief", "passthrough", None,
                           binding=DispatchBinding("dj-" + "a" * 32, 0, "sha256:" + "b" * 64))
    with pytest.raises(L.LauncherRefused) as e:
        ln.spawn(launch)
    assert e.value.name == "binding-on-a-passthrough-launch"
    assert ln.control.ops() == []


def test_the_runner_wants_a_brokered_launcher(w, tmp_path):
    from tests.test_agents_session_launcher import FakeControl, FakeRuntime
    plain = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "j"))
    with pytest.raises(ValueError):
        JobRunner(w.jobs, plain, w.port, w.cfg.active)


# ---- the mutation: drop the binding, use the occurrence ---------------------------------------------------------------

def test_mutation_dropping_the_binding_goes_red(w):
    """The same DONE-when check, with the launcher handed launches whose binding is gone (the facts fall back to the
    occurrence's id, run index 0 and a brief-derived digest): it must fail, or the assertions above prove nothing."""
    job = w.job()
    real = w.launcher.spawn
    w.launcher.spawn = lambda launch: real(dataclasses.replace(launch, binding=None))
    started = w.runner.run_job(job.id)
    with pytest.raises((AssertionError, Refused)):
        assert_bound(w, job, started)
