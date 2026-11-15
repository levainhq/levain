"""S14 A2a: root ``start`` for kind verifier, the clone and the ticket steps (CONTRACT §5.6 steps 1-2; design A2a).

Built on session 2f3's rig: the REAL CONTROL and a fake process table (``Table``), the launch through the wall faked
(``Launches``). A step here is a script: what it writes to its two pipes, how it exits, what it leaves behind. The
real drop to ``_levain_c<n>``, the real leaf cgroup, the macOS uid sweep and no network for the child are VM rows.
"""
from __future__ import annotations

import json
import os
import signal
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Optional

import pytest

from levain.contracts import canon
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import start as SS
from levain.slot import verifier_session as VS
from levain.slot import wall
from levain.slot.runtime_grammar import VerifierStep
from levain.verifier_drivers import output_compare
from tests import test_slot_s2f2_runtime_helper as T2
from tests.test_slot_s2f3_start import Launches, Table, _world
from tests.test_slot_s2f2_runtime_helper import DISPATCH_UID

S = "s-00000000000000c1"
CHILD = 892                         # the none-class slot's child account (World's _levain_c0)
EXIT_KEYS = {"exit", "signal", "wall_s", "timed_out", "truncated_bytes", "lingering_killed"}


@dataclass
class Script:
    """One step's behaviour. ``hang``: never exits on its own. ``lingerers``: processes it leaves behind (the child uid,
    inside the step's cgroup); ``immortal``: they ignore the kill."""

    out: bytes = b""
    err: bytes = b""
    code: int = 0
    hang: bool = False
    lingerers: int = 0
    immortal: bool = False
    on_spawn: Optional[Callable[[], None]] = None


class Steps(Launches):
    """``Launches`` plus the verifier's targets: the clone writes the template's attributes line; a step runs its script."""

    def __init__(self, world, table) -> None:
        super().__init__(world, table)
        self.scripts: list[Script] = []
        self.exited: dict[int, int] = {}
        self.step_spawns: list[dict] = []
        self.child_procs_at_spawn: list[list[int]] = []

    def spawn(self, argv, spec_json, cgroup, exec_w, detach, *, stdin_null=False, stdout_w=None, stderr_w=None):
        spec = canon.parse(spec_json)
        if spec["target"] == "clone":
            attr = self.world.L / "live" / S / "repo" / ".git" / "info"
            attr.mkdir(parents=True)
            (attr / "attributes").write_bytes(SS.RD.READSET_ATTRIBUTES)
            return super().spawn(argv, spec_json, cgroup, exec_w, detach)
        assert spec["target"] == "step", spec["target"]
        k = len(self.step_spawns)
        sc = self.scripts[k]
        pid = self.next_pid
        self.next_pid += 1
        st = self.world.state(S)
        self.step_spawns.append({"pid": pid, "argv": tuple(argv), "spec": spec, "cgroup": cgroup, "state": st.state,
                                 "stdin_null": stdin_null})
        self.spawns.append({"pid": pid, "argv": tuple(argv), "spec": spec, "cgroup": cgroup, "target": "step"})
        self.child_procs_at_spawn.append(self.table.of(CHILD))
        self.table.add(pid, spec["uid"], cg=True)
        for i in range(sc.lingerers):
            self.table.add(pid + 100 + i, spec["uid"], immortal=sc.immortal, cg=True)
        if stdout_w is not None and sc.out:
            os.write(stdout_w, sc.out)
        if stderr_w is not None and sc.err:
            os.write(stderr_w, sc.err)
        if not sc.hang:
            self.exited[pid] = sc.code << 8
            self.table.procs.pop(pid, None)
        if sc.on_spawn:
            sc.on_spawn()
        return pid

    def waitpid(self, pid, flags):
        if pid in self.exited:
            return pid, self.exited[pid]
        if pid in self.table.procs:
            return 0, 0
        self.exited[pid] = signal.SIGKILL          # the domain's kill ended it
        return pid, signal.SIGKILL


@pytest.fixture
def rig(tmp_path, request, monkeypatch):
    w, _, table = _world(tmp_path)
    la = Steps(w, table)
    s = w.seams
    s.spawn, s.wait, s.wait_within, s.kill, s.capture = la.spawn, la.wait, lambda pid, b: 0, la.kill, la.capture
    s.monotonic, s.sleep = table.clock, table.clock.sleep
    monkeypatch.setattr(VS, "WAITPID", la.waitpid)
    monkeypatch.setattr(VS, "finish_after_steps", lambda ctx, a, hooks, res: {"exit": 0})   # A2b's tests own the driver
    T2.test_none_class_no_front_no_handle(w, request)        # renders S: a real verifier ticket, alloc, record
    w.session = S
    return w, la, table


def _steps(w, *pairs) -> None:
    a = w.alloc(S)
    steps = tuple(VerifierStep(tuple(argv), wall_s) for argv, wall_s in pairs)
    (w.L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(replace(a, policy=replace(a.policy, verifier_steps=steps))))


def _run(w, k: int) -> Path:
    return w.L / "live" / S / "run"


def _exit_rec(w, k: int) -> dict:
    return json.loads((_run(w, k) / f"step{k}.exit").read_bytes())


def _start(w):
    return w.main(["start", S], uid=DISPATCH_UID)


def test_verifier_step_capture(rig):
    """Each step's stdout, stderr and exit status are root's: three root-written files per step, in the contract's keys,
    the first step before the second starts, the record ACTIVE while a step runs."""
    w, la, table = rig
    _steps(w, (["pytest", "-q"], 60), (["echo", "two"], 30))
    la.scripts = [Script(out=b"1 passed\n", err=b"warn\n", code=0), Script(out=b"two\n", code=3)]
    code, line = _start(w)
    assert line == {"ok": True, "exit": 0}, w.last_err
    assert w.state(S).state == R.ACTIVE
    assert [r["state"] for r in la.step_spawns] == [R.ACTIVE, R.ACTIVE]
    assert [r["argv"] is not None for r in la.step_spawns] == [True, True]
    assert (_run(w, 0) / "step0.out").read_bytes() == b"1 passed\n"
    assert (_run(w, 0) / "step0.err").read_bytes() == b"warn\n"
    assert (_run(w, 1) / "step1.out").read_bytes() == b"two\n"
    r0, r1 = _exit_rec(w, 0), _exit_rec(w, 1)
    assert set(r0) == EXIT_KEYS and set(r1) == EXIT_KEYS
    assert (r0["exit"], r0["signal"], r0["timed_out"], r0["truncated_bytes"], r0["lingering_killed"]) == (
        0, None, False, 0, 0)
    assert (r1["exit"], r1["signal"]) == (3, None)
    assert type(r0["wall_s"]) is int
    # the step process: the child's uid, stdin /dev/null, the step's own leaf cgroup, repo as cwd
    s0, s1 = la.step_spawns
    assert s0["spec"]["uid"] == CHILD and s0["spec"]["target"] == "step" and s0["stdin_null"]
    assert s0["spec"]["cwd"].endswith(f"/live/{S}/repo") and s0["spec"]["argv"] == ["pytest", "-q"]
    assert s0["cgroup"].endswith(f"/{S}/step0") and s1["cgroup"].endswith(f"/{S}/step1")
    # the files' owner and mode: root : the verifier's group, 0640; the dir 0750
    chowned = {rel: (u, g) for rel, u, g in w.chowns}
    for name in ("step0.out", "step0.err", "step0.exit", "step1.exit"):
        assert chowned[f"live/{S}/run/{name}"] == (0, 893)
        assert w.mode(f"live/{S}/run/{name}") == 0o640
    assert chowned[f"live/{S}/run"] == (0, 893) and w.mode(f"live/{S}/run") == 0o750
    # no driver here (A2b), and the report fields stay unset
    assert [r["spec"]["target"] for r in la.step_spawns] == ["step", "step"]
    assert "verifier_report_digest" not in w.state(S).fields


def test_verifier_step_wall_kills(rig):
    """A step still running at its wall: the whole domain is killed and proven empty, the record says so, and the next
    step starts only after."""
    w, la, table = rig
    _steps(w, (["sleep", "999"], 3), (["true"], 30))
    la.scripts = [Script(out=b"partial", hang=True, lingerers=1), Script()]
    code, line = _start(w)
    assert line["ok"], w.last_err
    r0 = _exit_rec(w, 0)
    assert r0["timed_out"] is True and r0["exit"] is None and r0["signal"] == signal.SIGKILL
    assert r0["wall_s"] >= 3 and r0["lingering_killed"] == 0       # a timed-out domain's count is not taken
    assert (_run(w, 0) / "step0.out").read_bytes() == b"partial"
    assert la.child_procs_at_spawn[1] == []                         # nothing of step 0 alive when step 1 spawned
    assert table.of(CHILD) == []
    assert _exit_rec(w, 1)["timed_out"] is False


def test_daemonized_step_killed_before_next(rig):
    """A step that exits 0 but leaves a process behind: the process is counted, killed with its domain, and is gone
    before step 1 spawns (it cannot touch step 1's input or evidence)."""
    w, la, table = rig
    _steps(w, (["sh", "run.sh"], 60), (["cat", "result"], 60))
    la.scripts = [Script(out=b"done\n", lingerers=2), Script(out=b"ok\n")]
    code, line = _start(w)
    assert line["ok"], w.last_err
    r0 = _exit_rec(w, 0)
    assert r0["exit"] == 0 and r0["lingering_killed"] == 2 and r0["timed_out"] is False
    assert la.child_procs_at_spawn == [[], []]
    assert _exit_rec(w, 1)["lingering_killed"] == 0


def test_revoke_between_steps_starts_no_step_k_plus_1(rig):
    """A REVOKE after step 0 spawned: step 0's evidence is still written (its domain is root's to end), step 1 never
    spawns, and the start refuses ``not-active``."""
    w, la, table = rig
    _steps(w, (["a"], 60), (["b"], 60))

    def revoke() -> None:
        st = w.state(S)
        w.control.cas(S, st.generation, R.ACTIVE, R.REVOKED, {})

    la.scripts = [Script(out=b"x", on_spawn=revoke), Script(out=b"never")]
    code, line = _start(w)
    assert line == {"ok": False, "refused": "not-active", **{k: v for k, v in line.items() if k == "detail"}}
    assert len(la.step_spawns) == 1
    assert (_run(w, 0) / "step0.exit").exists() and not (_run(w, 1) / "step1.exit").exists()
    assert not (_run(w, 1) / "step1.out").exists()
    assert w.state(S).state == R.REVOKED


def test_unkillable_step_is_refused_and_recorded(rig):
    """A process that ignores the kill: no ``.exit`` is written, the start refuses ``step-unkillable``, the field is
    recorded on the ACTIVE record, and no later step runs."""
    w, la, table = rig
    _steps(w, (["a"], 60), (["b"], 60))
    la.scripts = [Script(lingerers=1, immortal=True), Script()]
    code, line = _start(w)
    assert line["refused"] == "step-unkillable"
    assert not (_run(w, 0) / "step0.exit").exists()
    assert len(la.step_spawns) == 1
    assert w.state(S).fields["verifier_report_refused"] == "step-unkillable"


def test_clone_failure_is_refused_no_step_runs(rig, capsys):
    """A clone that exits non-zero: ``clone-failed``, the record stays RENDERED with ``verifier_report_refused:
    clone-failed`` set (A0 lets that one field ride a field-only CAS on RENDERED), no step spawns, no ``run/`` dir."""
    w, la, table = rig
    la.codes["clone"] = 1
    s = w.seams
    s.wait_within = lambda pid, bound: 1
    code, line = _start(w)
    assert line["refused"] == "clone-failed"
    assert w.state(S).state == R.RENDERED and la.step_spawns == []
    assert not (w.L / "live" / S / "run").exists()
    assert w.state(S).fields["verifier_report_refused"] == "clone-failed"
    assert "not recorded" not in capsys.readouterr().err


def test_a_second_start_and_a_set_report_field_refuse(rig):
    w, la, table = rig
    _steps(w, (["a"], 60))
    la.scripts = [Script()]
    assert _start(w)[1]["ok"]
    assert _start(w)[1]["refused"] == "already-started"
    assert len(la.step_spawns) == 1


def test_stream_cap_counts_the_rest(rig, monkeypatch):
    """A stream past the cap is cut at the cap and the excess is ``truncated_bytes`` (a verdict for the driver, never an
    infrastructure error)."""
    w, la, table = rig
    monkeypatch.setattr(VS, "STREAM_CAP", 10)
    _steps(w, (["a"], 60))
    la.scripts = [Script(out=b"x" * 25, err=b"y" * 4)]
    assert _start(w)[1]["ok"]
    assert (_run(w, 0) / "step0.out").read_bytes() == b"x" * 10
    assert (_run(w, 0) / "step0.err").read_bytes() == b"y" * 4
    assert _exit_rec(w, 0)["truncated_bytes"] == 15


def test_root_cap_is_the_drivers():
    """D4: root never writes a capture the shipped driver refuses."""
    assert VS.STREAM_CAP == output_compare.MAX_CAPTURE_BYTES


def test_the_seam_a2b_fills_gets_every_exit(rig, monkeypatch):
    """:func:`finish_after_steps` is called once, after the last ``.exit`` is durable, with the steps' records."""
    w, la, table = rig
    _steps(w, (["a"], 60), (["b"], 60))
    la.scripts = [Script(), Script(code=1)]
    seen = []

    def finish(ctx, a, hooks, res):
        seen.append((tuple(e["exit"] for e in res.exits), (_run(w, 1) / "step1.exit").exists()))
        return {"exit": 7}

    monkeypatch.setattr(VS, "finish_after_steps", finish)
    code, line = _start(w)
    assert line == {"ok": True, "exit": 7} and seen == [((0, 1), True)]


def test_a_step_reaped_elsewhere_is_killed_proven_and_refused_by_name(rig, monkeypatch):
    """S14 fix 6 (L1 F6 / L2 L1): `waitpid` answering ChildProcessError for the step's main process (reaped elsewhere,
    or SIGCHLD ignored) is treated as already reaped: the whole domain is still killed and proven (the step's lingerer is
    gone), no `.exit` is written, the field is recorded and the start refuses `step-unreaped`, with no later step. RED
    on the old code: the ChildProcessError escaped `run_step` raw past the kill-and-prove, so the lingerer stayed in the
    table and no refusal name was recorded."""
    w, la, table = rig
    _steps(w, (["a"], 60), (["b"], 60))
    la.scripts = [Script(lingerers=1), Script()]
    plain = la.waitpid

    def reaped_elsewhere(pid, flags):
        if any(s["pid"] == pid for s in la.step_spawns):
            raise ChildProcessError(10, "No child processes")
        return plain(pid, flags)

    monkeypatch.setattr(VS, "WAITPID", reaped_elsewhere)
    code, line = _start(w)
    assert line["refused"] == "step-unreaped", w.last_err
    assert table.of(CHILD) == []                                   # the domain was killed and proven
    assert not (_run(w, 0) / "step0.exit").exists() and len(la.step_spawns) == 1
    assert w.state(S).fields["verifier_report_refused"] == "step-unreaped"
