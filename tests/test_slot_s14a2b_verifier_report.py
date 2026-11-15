"""S14 A2b: root ``start`` for kind verifier, the driver, the envelope and the report CAS (CONTRACT §5.6 step 3; design A2b).

Built on A2a's rig (the real CONTROL, a fake process table, the launch through the wall faked past ``spawn``). The
driver here is a script: what it prints, how it ends. The driver under the real ``ROOT_PYTHON -I -S``, the real
``_levain_v<n>`` drop and the export dir's ownership are VM rows.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
from dataclasses import dataclass, replace

import pytest

from levain.contracts import canon
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import verifier_session as VS
from levain.slot.root_tree import ROOT_PYTHON
from tests import test_slot_s2f2_runtime_helper as T2
from tests.test_slot_s14a2a_verifier_steps import CHILD, S, Script, Steps, _start, _steps
from tests.test_slot_s2f3_start import _world

SLOT_UID = 893
EXPORT_GID = 795
BODY = {"format": "levain-dispatch-verifier-report/1", "result": "pass", "steps": [{"step": 0, "verdict": "pass", "note": ""}]}


@dataclass
class Driver:
    """The driver's behaviour: stdout and stderr bytes, how it ends (``code`` or ``sig``), whether it hangs, the processes
    it leaves behind."""

    out: bytes = b""
    err: bytes = b""
    code: int = 0
    sig: int = 0
    hang: bool = False
    lingerers: int = 0
    immortal: bool = False
    on_spawn: object = None


class Drivers(Steps):
    def __init__(self, world, table) -> None:
        super().__init__(world, table)
        self.driver = Driver(out=canon.canonical_bytes(BODY))
        self.driver_spawns: list[dict] = []
        self.procs_at_driver: list[list[int]] = []

    def spawn(self, argv, spec_json, cgroup, exec_w, detach, *, stdin_null=False, stdout_w=None, stderr_w=None):
        spec = canon.parse(spec_json)
        if spec["target"] != "driver":
            return super().spawn(argv, spec_json, cgroup, exec_w, detach, stdin_null=stdin_null,
                                 stdout_w=stdout_w, stderr_w=stderr_w)
        d = self.driver
        pid = self.next_pid
        self.next_pid += 1
        st = self.world.state(S)
        self.driver_spawns.append({"pid": pid, "argv": tuple(argv), "spec": spec, "cgroup": cgroup, "state": st.state,
                                   "stdin_null": stdin_null, "steps_done": len(self.step_spawns)})
        self.procs_at_driver.append(self.table.of(CHILD))
        self.table.add(pid, spec["uid"], cg=True)
        for i in range(d.lingerers):
            self.table.add(pid + 100 + i, spec["uid"], immortal=d.immortal, cg=True)
        if d.out:
            os.write(stdout_w, d.out)
        if d.err:
            os.write(stderr_w, d.err)
        if not d.hang:
            self.exited[pid] = d.sig if d.sig else d.code << 8
            self.table.procs.pop(pid, None)
        if d.on_spawn:
            d.on_spawn()
        return pid


@pytest.fixture
def rig(tmp_path, request, monkeypatch):
    w, _, table = _world(tmp_path)
    la = Drivers(w, table)
    s = w.seams
    s.spawn, s.wait, s.wait_within, s.kill, s.capture = la.spawn, la.wait, lambda pid, b: 0, la.kill, la.capture
    s.monotonic, s.sleep = table.clock, table.clock.sleep
    monkeypatch.setattr(VS, "WAITPID", la.waitpid)
    monkeypatch.setitem(T2.GROUPS, "levain-dispatch-export", EXPORT_GID)
    T2.test_none_class_no_front_no_handle(w, request)
    w.session = S
    for d, mode in (("export", 0o755), ("export/dispatch", 0o750)):
        (w.L / d).mkdir()
        os.chmod(w.L / d, mode)
    _steps(w, (["pytest", "-q"], 60))
    la.scripts = [Script(out=b"1 passed\n")]
    return w, la, table


def _report(w) -> bytes:
    return (w.L / "export" / "dispatch" / S / "verifier-report.json").read_bytes()


def test_verifier_report_carried_opaque(rig):
    """The driver's body is carried byte-for-byte inside root's envelope, unread (a member root does not know, a
    result root cannot judge); the file is root's, 0640, in a 0750 dir of the export group; its digest is on the record,
    set by a field-only CAS in ACTIVE; and the driver ran in its own leaf after the last step's evidence."""
    w, la, table = rig
    body = {"format": "anything", "result": "maybe", "steps": [1, {"x": [None, True, "y"]}], "zzz": {"deep": {"er": 1}}}
    la.driver.out, la.driver.err = canon.canonical_bytes(body), b"note\n"
    code, line = _start(w)
    assert line == {"ok": True, "exit": 0}, w.last_err
    raw = _report(w)
    env = canon.parse(raw)
    assert canon.canonical_bytes(env) == raw
    assert env["body"] == body and canon.canonical_bytes(env["body"]) == la.driver.out
    assert set(env) == {"session", "ticket", "verifier", "driver_input_digest", "input_bundle_digest", "steps", "body"}
    assert env["session"] == S and env["ticket"] == w.alloc(S).ticket
    assert [set(e) for e in env["steps"]] == [VS_EXIT_KEYS]
    rec = w.state(S)
    assert rec.state == R.ACTIVE and rec.fields["verifier_report_digest"] == "sha256:" + hashlib.sha256(raw).hexdigest()
    assert "verifier_report_refused" not in rec.fields
    rel = f"export/dispatch/{S}"
    assert w.chown_of(f"{rel}/verifier-report.json") == (0, EXPORT_GID) and w.chown_of(rel) == (0, EXPORT_GID)
    assert w.mode(f"{rel}/verifier-report.json") == 0o640 and w.mode(rel) == 0o750
    assert os.listdir(w.L / "export" / "dispatch" / S) == ["verifier-report.json"]      # the temp is gone
    # the driver: its own leaf, the slot's account, after every step, ACTIVE, the contract's argv
    (d,) = la.driver_spawns
    assert d["state"] == R.ACTIVE and d["steps_done"] == 1 and d["stdin_null"]
    assert d["spec"]["uid"] == SLOT_UID and d["spec"]["target"] == "driver" and d["cgroup"].endswith(f"/{S}/driver")
    assert d["spec"]["argv"][:3] == [ROOT_PYTHON["linux"], "-I", "-S"]
    assert d["spec"]["argv"][3].endswith("/levain/verifier_drivers/output_compare.py")
    assert d["spec"]["argv"][4:] == [f"{w.L}/live/{S}/driver/input.json", f"{w.L}/live/{S}/run"]
    assert la.procs_at_driver == [[]]
    assert (w.L / "live" / S / "run" / "driver.err").read_bytes() == b"note\n"
    assert w.mode(f"live/{S}/run/driver.err") == 0o640


VS_EXIT_KEYS = {"exit", "signal", "wall_s", "timed_out", "truncated_bytes", "lingering_killed"}


def _no_report(w) -> None:
    assert not (w.L / "export" / "dispatch" / S / "verifier-report.json").exists()
    assert "verifier_report_digest" not in w.state(S).fields


def test_driver_wall_kills(rig):
    """A driver still running at V26: its whole domain is killed and proven empty, the start refuses ``driver-timeout``,
    the field is on the ACTIVE record, and no envelope is written."""
    w, la, table = rig
    la.driver = Driver(out=b"{", hang=True, lingerers=1)
    t0 = table.clock()
    code, line = _start(w)
    assert line["refused"] == "driver-timeout"
    assert table.clock() - t0 >= VS.DRIVER_WALL_S == 600
    assert table.of(SLOT_UID) == []
    rec = w.state(S)
    assert rec.state == R.ACTIVE and rec.fields["verifier_report_refused"] == "driver-timeout"
    _no_report(w)


@pytest.mark.parametrize("driver, name", [
    (Driver(out=b"x", hang=True), "driver-timeout"),
    (Driver(sig=signal.SIGSEGV), f"driver-signal:{int(signal.SIGSEGV)}"),
    (Driver(code=2), "driver-exit:2"),
    (Driver(out=b"x" * 50), "report-too-large"),
    (Driver(out=b'{"b": 1}'), "report-not-canonical"),
    (Driver(out=b""), "report-not-canonical"),
    (Driver(out=b'[1,2]'), "report-format"),
])
def test_the_six_refusals(rig, monkeypatch, driver, name):
    """Each end of the driver that is not one canonical JSON object within the cap is a named refusal, recorded as
    ``verifier_report_refused`` on the ACTIVE record, with no envelope. An exit status wins over its stdout."""
    w, la, table = rig
    monkeypatch.setattr(VS, "REPORT_CAP", 40)
    la.driver = driver
    if name.startswith("driver-exit") or name.startswith("driver-signal"):
        la.driver = replace(driver, out=canon.canonical_bytes(BODY)[:30])
    code, line = _start(w)
    assert line["refused"] == name, w.last_err
    assert w.state(S).fields["verifier_report_refused"] == name and w.state(S).state == R.ACTIVE
    _no_report(w)


def test_the_order_of_the_names():
    """Timeout beats a signal, a signal beats an exit code, an exit code beats a bad body, size beats syntax."""
    good = canon.canonical_bytes(BODY)
    assert VS.classify(True, None, 9, b"")[0] == "driver-timeout"
    assert VS.classify(False, None, 9, good)[0] == "driver-signal:9"
    assert VS.classify(False, 3, None, b"[")[0] == "driver-exit:3"
    assert VS.classify(False, 0, None, b"x" * (VS.REPORT_CAP + 1))[0] == "report-too-large"
    assert VS.classify(False, 0, None, good) == (None, BODY)
    assert VS.classify(False, 0, None, b'{"a":' * 40 + b"1" + b"}" * 40)[0] == "report-not-canonical"   # canon's depth cap


def test_unkillable_driver_is_refused_and_recorded(rig):
    w, la, table = rig
    la.driver = Driver(lingerers=1, immortal=True)
    code, line = _start(w)
    assert line["refused"] == "driver-unkillable"
    assert w.state(S).fields["verifier_report_refused"] == "driver-unkillable"
    _no_report(w)


def test_a_revoke_during_the_driver_writes_no_envelope(rig):
    """The record left ACTIVE while the driver ran: root writes no envelope and no digest."""
    w, la, table = rig

    def revoke() -> None:
        st = w.state(S)
        w.control.cas(S, st.generation, R.ACTIVE, R.REVOKED, {})

    la.driver = Driver(out=canon.canonical_bytes(BODY), on_spawn=revoke)
    code, line = _start(w)
    assert line["refused"] == "not-active"
    assert w.state(S).state == R.REVOKED and "verifier_report_digest" not in w.state(S).fields
    assert not (w.L / "export" / "dispatch" / S / "verifier-report.json").exists()


def test_no_driver_when_a_step_is_unkillable(rig):
    w, la, table = rig
    la.scripts = [Script(lingerers=1, immortal=True)]
    code, line = _start(w)
    assert line["refused"] == "step-unkillable" and la.driver_spawns == []


def test_an_existing_report_is_never_replaced(rig):
    w, la, table = rig
    d = w.L / "export" / "dispatch" / S
    d.mkdir()
    os.chmod(d, 0o750)
    (d / "verifier-report.json").write_bytes(b"old")
    code, line = _start(w)
    assert line["refused"] == "report-exists" and (d / "verifier-report.json").read_bytes() == b"old"
    assert "verifier_report_digest" not in w.state(S).fields


def test_driver_wall_is_the_contracts():
    assert VS.DRIVER_WALL_S == 600 and VS.REPORT_CAP == 1 << 20


def test_a_driver_reaped_elsewhere_is_killed_proven_and_refused_by_name(rig, monkeypatch):
    """S14 fix 6: the driver's `waitpid` answering ChildProcessError: its domain is still killed and proven, the field is
    recorded, the start refuses `driver-unreaped`, and no envelope is written. RED on the old code: the exception
    escaped raw past the kill-and-prove (the lingerer stayed, no name was recorded)."""
    w, la, table = rig
    plain = la.waitpid

    def reaped_elsewhere(pid, flags):
        if any(d["pid"] == pid for d in la.driver_spawns):
            raise ChildProcessError(10, "No child processes")
        return plain(pid, flags)

    monkeypatch.setattr(VS, "WAITPID", reaped_elsewhere)
    la.driver = Driver(out=canon.canonical_bytes(BODY), lingerers=1)
    code, line = _start(w)
    assert line["refused"] == "driver-unreaped", w.last_err
    assert table.of(SLOT_UID) == []
    assert w.state(S).fields["verifier_report_refused"] == "driver-unreaped"
    _no_report(w)


def test_a_driver_template_changed_since_render_is_refused_before_the_driver_spawns(rig):
    """S14 fix 6 (L2 L2): the template's digest is checked again immediately before the driver spawn, not only at
    render. Asserts: an import tree whose template no longer hashes to the entry's digest (an upgrade during the steps)
    spawns no driver, records `driver-template-digest-mismatch` and writes no envelope. RED on the old code: the driver
    spawned and its report was enveloped under the old template digest."""
    w, la, table = rig
    w.seams.driver_template = lambda name: b"# an upgraded driver\n"
    code, line = _start(w)
    assert line["refused"] == "driver-template-digest-mismatch", w.last_err
    assert la.driver_spawns == [] and len(la.step_spawns) == 1
    assert w.state(S).fields["verifier_report_refused"] == "driver-template-digest-mismatch"
    _no_report(w)
