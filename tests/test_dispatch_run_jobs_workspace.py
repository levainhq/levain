"""S14 design B piece B2: the dispatch runner's workspace wiring (`levain.dispatch.run_jobs` + `run_workspace`), run for real.

The world is `tests.test_dispatch_run_jobs.World` (real CONTROL, real job store, a real handoff from the filing door's own
exporter, a real `ReadSetStager`); its fake slot runtime calls the `workspace` callable it is given and clones the bundle
that callable staged, as `HelperSlotRuntime` does at start. Properties P1-P6 of the design page, through the runner.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from levain.agents import session_launcher as L
from levain.dispatch import readset as RS
from levain.dispatch import run_jobs as RJ
from levain.dispatch import run_workspace as RW
from levain.dispatch.policy import ProviderPolicy
from levain.dispatch.run_jobs import RunnerRefused, run_key
from tests.test_dispatch_contract import git, make_repo, registry, submission, template
from tests.test_dispatch_run_jobs import MODELS, World, cdir, keys, log, pytestmark, py, store, w  # noqa: F401

READ = ["src/a.py", "src/b.py"]          # tests.test_dispatch_contract.submission's read list, sorted as the tree lists it


def zero_handoff(w: World, job) -> None:
    _bid, path, size = w.jobs.handoff_row(job.id)
    os.chmod(path, 0o600)
    Path(path).write_bytes(b"\0" * size)


def corrupt_tail(w: World, job) -> None:
    """Flip the last byte (the pack's own checksum): past the prefix the cheap check reads, so only the full verify sees it."""
    _bid, path, size = w.jobs.handoff_row(job.id)
    data = bytearray(Path(path).read_bytes())
    data[-1] ^= 0xFF
    os.chmod(path, 0o600)
    Path(path).write_bytes(bytes(data))


def work(w: World) -> list[str]:
    return os.listdir(w.tmp / "work")


# ---- P1: the executor's tree is exactly the signed read list, through the runner ---------------------------------------

def test_p1_each_started_run_stages_exactly_the_signed_read_list(w):
    job = w.job()
    started = w.runner.run_job(job.id)
    assert started.refused is None and started.released
    assert w.runtime.staged == [READ, READ]                      # one clone per start, each exactly the read list
    # mutation: stage make_bundle(repo, "main") instead and the clone holds the whole repo (src/run.sh, docs, README ...)


# ---- P2: no unverified byte reaches a slot, and a bad handoff mints nothing --------------------------------------------

def test_p2_a_zeroed_handoff_fails_the_job_untaken_and_mints_nothing(w):
    job = w.job()
    zero_handoff(w, job)
    out = w.runner.tick()
    assert [(s.action, s.cause, s.detail) for s in out] == [("stopped", "run_failed", "handoff-mismatch")]
    assert w.minted() == 0 and w.runtime.starts == [] and work(w) == []
    assert w.index.get(run_key(job.id, 0)) is None
    assert w.jobs.get(job.id).state == "failed"


def test_p2_the_preflight_names_a_zeroed_handoff_standing(w):
    job = w.job()
    zero_handoff(w, job)
    with pytest.raises(RunnerRefused) as e:
        w.runner.preflight(job)
    assert e.value.name == "handoff-mismatch" and e.value.reason_class == "standing"


def test_p2_a_flipped_byte_past_the_prefix_is_refused_by_the_full_verify_before_the_take(w):
    job = w.job()
    corrupt_tail(w, job)
    w.runner.preflight(job)                                       # the cheap prefix check passes: that is the point
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name.startswith("readset-") and e.value.reason_class == "standing"
    assert w.jobs.get(job.id).state == "signed" and w.minted() == 0 and work(w) == []
    out = w.runner.tick()
    assert [(s.action, s.cause) for s in out] == [("stopped", "run_failed")] and w.minted() == 0


@pytest.mark.parametrize("name", ["readset-git-unavailable", "readset-workdir", "readset-handoff-raced"])
def test_a_host_fault_at_verify_is_transient_and_the_job_waits_untaken(w, monkeypatch, name):
    # S14 fix 3 (L1 F3): `readset-handoff-raced` (the handoff copy past its deadline or size on a slow disk) is the
    # host's fault. RED for that case on the old code: run_jobs classed it standing, so the tick failed the signed job
    # `run_failed` for good while the same name from the stager's `stage` was transient
    job = w.job()

    def verify(j):
        raise RS.ReadSetRefused(name, "the host")

    monkeypatch.setattr(w.stager, "verify", verify)
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == name and e.value.reason_class == "transient"
    out = w.runner.tick()
    assert [(s.action, s.detail) for s in out] == [("waiting", name)]
    assert w.jobs.get(job.id).state == "queued" and w.minted() == 0


def test_the_runners_transient_readset_names_are_the_slot_workspaces():
    """S14 fix 3: the runner and the slot workspace each keep a table of the transient read-set refusals (one name
    met at two call sites must get one class); pinned equal so the copies cannot drift again. RED on the old code: the
    runner's lacked `readset-handoff-raced`."""
    from levain.dispatch import run_jobs as RJ
    from levain.slot import workspace as SW
    assert RJ._TRANSIENT_READSET == SW._TRANSIENT_READSET


def test_a_standing_refusal_at_verify_fails_the_job_by_its_own_name(w, monkeypatch):
    job = w.job()

    def verify(j):
        raise RS.ReadSetRefused("readset-surplus", "an extra object")

    monkeypatch.setattr(w.stager, "verify", verify)
    out = w.runner.tick()
    assert [(s.action, s.cause, s.detail) for s in out] == [("stopped", "run_failed", "readset-surplus")]
    assert w.minted() == 0


# ---- P3 -----------------------------------------------------------------------------------------------------------------

def test_p3_a_start_that_holds_no_verified_set_stages_nothing(w, tmp_path):
    from types import SimpleNamespace
    with pytest.raises(L.LauncherRefused) as e:
        w.stager.stage(SimpleNamespace(session_key=run_key("dj-x", 0)), tmp_path)
    assert e.value.name == "workspace-unregistered"


# ---- P4: .mcp.json parity with W1, through the runner ---------------------------------------------------------------------

def test_p4_a_contract_reading_mcp_json_is_refused_before_the_take(w, tmp_path):
    repo, base = make_repo(tmp_path / "r2")
    (repo / ".mcp.json").write_text('{"mcpServers": {}}\n')
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "mcp")
    base = git(repo, "rev-parse", "HEAD").strip()
    policy = ProviderPolicy.from_mapping({"never": ["work/**"], "providers": {
        "openrouter": {"read": ["src/**", ".mcp.json"]}, "anthropic": {"read": ["**"]}}})
    reg = registry(template(executor="claude", models=MODELS, k=2))
    from levain.dispatch import cli as dcli
    from tests.test_dispatch_contract import POLICY  # noqa: F401
    job, _cap = w.jobs.file(submission(repo, base, read=[".mcp.json", "src/a.py"],
                                       budget={"tool_calls": 40, "tokens": 200000, "dollars_cents": 150}),
                            resolve_template=reg, policy=policy, filer_session="coord-1", export=dcli.readset_export)
    job = w.jobs._transition(job.id, "signed", "signature")
    with pytest.raises(RunnerRefused) as e:
        w.runner.run_job(job.id)
    assert e.value.name == "readset-refused-name" and e.value.reason_class == "standing"
    assert w.jobs.get(job.id).state == "signed" and w.minted() == 0 and work(w) == []


# ---- P5: no workdir outlives its launch --------------------------------------------------------------------------------

def test_p5_the_workdir_is_registered_during_the_launch_and_gone_after_it(w):
    job = w.job()
    seen = []
    real = w.launcher.spawn

    def spawn(launch):
        seen.append((launch.session_key in w.stager._held, len(work(w))))
        return real(launch)

    w.launcher.spawn = spawn
    w.runner.run_job(job.id)
    assert seen == [(True, 1), (True, 1)]                         # held and on disk for each spawn
    assert w.stager._held == {} and w.stager._contracts == {} and work(w) == []


def test_p5_a_refused_start_leaves_no_workdir_and_no_registration(w):
    job = w.job()
    real = w.launcher.spawn

    def spawn(launch):
        if launch.binding.run_index == 1:
            raise L.LauncherRefused("mint-refused:ticket-ceiling", "x", reason_class="standing")
        return real(launch)

    w.launcher.spawn = spawn
    started = w.runner.run_job(job.id)
    assert started.refused and w.stager._held == {} and work(w) == []


def test_p5_a_spawn_that_raises_leaves_no_workdir_and_no_registration(w):
    job = w.job()

    def spawn(launch):
        raise RuntimeError("boom")

    w.launcher.spawn = spawn
    with pytest.raises(RuntimeError):
        w.runner.run_job(job.id)
    assert w.stager._held == {} and work(w) == []


def test_p5_a_take_the_store_refuses_leaves_no_workdir(w, monkeypatch):
    job = w.job()

    def refuse(job_id, *, envelope=None):
        from levain.dispatch.jobs import TransitionRefused
        raise TransitionRefused("another writer took it")

    monkeypatch.setattr(w.jobs, "take_slot", refuse)
    from levain.dispatch.jobs import TransitionRefused
    with pytest.raises(TransitionRefused):
        w.runner.run_job(job.id)
    assert work(w) == [] and w.stager._contracts == {}


def test_p5_the_transient_retry_path_verifies_again_before_it_launches(w):
    job = w.job()
    real = w.launcher.spawn
    calls = []

    def spawn(launch):
        calls.append(launch.binding.run_index)
        if len(calls) == 1:
            raise L.LauncherRefused("mint-refused:no-free-slot", "none", reason_class="transient")
        return real(launch)

    w.launcher.spawn = spawn
    w.launcher.refuse_key(run_key(job.id, 0), "mint:no-free-slot", occurrence_id=job.id, reason_class="transient")
    w.runner.run_job(job.id)
    assert w.jobs.get(job.id).state == "running" and work(w) == []
    verified = []
    real_verify = w.stager.verify
    w.stager.verify = lambda j: (verified.append(j.id), real_verify(j))[1]
    out = w.runner.tick()
    assert [s.action for s in out] == ["waiting"] and verified == [job.id]
    assert w.runtime.staged == [READ, READ] and work(w) == [] and w.stager._held == {}


def test_p5_a_standing_refusal_on_the_retry_path_fails_the_running_job(w):
    job = w.job()
    w.launcher.refuse_key(run_key(job.id, 0), "mint:no-free-slot", occurrence_id=job.id, reason_class="transient")
    w.jobs.take_slot(job.id)
    zero_handoff(w, job)
    out = w.runner.tick()
    assert [(s.action, s.cause) for s in out] == [("stopped", "run_failed")] and w.minted() == 0


def test_p5_main_sweeps_a_crash_leftover_under_its_lock(w):
    planted = w.tmp / "work" / RS.workdir_name("dv-0123456789abcdef")
    planted.mkdir(mode=0o700)
    (planted / "handoff.bundle").write_bytes(b"x")
    rc = RJ.main(["--store-dir", str(w.tmp / "jobs"), "--socket", "/nonexistent/control.sock"],
                 build=lambda store, sock, host: w.runner, wait=lambda s: True, max_ticks=1)
    assert rc == 0 and work(w) == []


# ---- P6 / the seam is never None in production --------------------------------------------------------------------------

def _settings_job(w: World, tmp_path, body: str):
    repo, _b = make_repo(tmp_path / "r3")
    (repo / ".claude").mkdir()
    (repo / ".claude/settings.json").write_text(body)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "s")
    base = git(repo, "rev-parse", "HEAD").strip()
    policy = ProviderPolicy.from_mapping({"never": ["work/**"], "providers": {
        "openrouter": {"read": ["src/**", ".claude/**"]}, "anthropic": {"read": ["**"]}}})
    reg = registry(template(executor="claude", models=MODELS, k=2))
    from levain.dispatch import cli as dcli
    job, _cap = w.jobs.file(submission(repo, base, read=[".claude/settings.json", "src/a.py"],
                                       budget={"tool_calls": 40, "tokens": 200000, "dollars_cents": 150}),
                            resolve_template=reg, policy=policy, filer_session="coord-1", export=dcli.readset_export)
    return w.jobs._transition(job.id, "signed", "signature")


def test_p6_a_benign_settings_file_rides_the_runtime_as_its_repo_settings(w, tmp_path):
    job = _settings_job(w, tmp_path, '{"permissions": {"deny": ["Bash(rm:*)"]}}')
    started = w.runner.run_job(job.id)
    assert started.refused is None and w.runtime.staged == [[".claude/settings.json", "src/a.py"]] * 2
    assert len(w.runtime.settings) == 2 and all(s and "permissions" in s for s in w.runtime.settings), w.runtime.settings


def test_p6_a_loosening_settings_file_never_reaches_the_runner(w, tmp_path):
    # the filing door refuses it by the loosening's own name, so there is nothing to sign; the run side's own refusal of a
    # handoff that got past the door is pinned in test_dispatch_run_workspace (P6, raw handoff)
    with pytest.raises(RS.ReadSetRefused) as e:
        _settings_job(w, tmp_path, '{"disableAllHooks": true}')
    assert e.value.name.startswith("loosening:")


def test_the_production_runner_wires_the_stager_into_the_runtime(tmp_path, monkeypatch):
    from levain.dispatch.jobs import JobStore
    from levain.slot import layout
    host = "darwin" if os.uname().sysname == "Darwin" else "linux"
    root = Path(os.path.realpath(tmp_path)) / "store"
    root.mkdir(mode=0o700)
    monkeypatch.setitem(layout.STORE, host, root)
    jobs = tmp_path / "jobs"
    jobs.mkdir(mode=0o700)
    runner = RJ.build_runner(JobStore(jobs), str(tmp_path / "control.sock"), host)
    assert isinstance(runner.stager, RW.ReadSetStager)
    rt = runner.launcher.runtime
    assert rt._workspace == runner.stager.stage and rt._repo_settings == runner.stager.repo_settings
    assert rt._workspace is not None
    assert runner.stager.parent == str(root / "dispatch" / "readset-work")
    assert os.stat(runner.stager.parent).st_mode & 0o777 == 0o700
