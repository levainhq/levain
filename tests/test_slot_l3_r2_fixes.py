"""The L3 r2 triage fixes, G1-G5. Plan assertions, fakes, and pytest tmp files only: nothing here runs
launchctl, systemctl, pfctl or any other system command."""
from __future__ import annotations

import errno
import os
import stat
import subprocess
from pathlib import Path

import pytest

from levain.firing import hands
from levain.slot import Refused, intent as intent_mod, pf, root_tree, units
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import SHARED_GROUPS, SINGLETONS
from levain.slot.install_helper import HostFacts, operator_record, plan_setup, plan_undo
from levain.slot.intent import INTENT_PATH, Intent
from levain.slot.layout import operator_record_path

HOSTS = ("linux", "darwin")
OP, OP_UID = "alice", 501


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


# --- G1: a loaded job whose definition will change is unloaded BEFORE its file is written ---------------


class _Launchd:
    """A fake launchctl: ``loaded`` maps a label to the plist bytes it was bootstrapped from."""

    def __init__(self, disk, loaded=None):
        self.disk, self.loaded, self.calls = disk, dict(loaded or {}), []

    def __call__(self, argv, **kw):
        argv = tuple(argv)
        self.calls.append(argv[1:])
        verb = argv[1]
        if verb == "print":
            return subprocess.CompletedProcess(argv, 0 if argv[2].removeprefix("system/") in self.loaded else 113)
        if verb == "bootout":
            self.loaded.pop(argv[2].removeprefix("system/"), None)
            return subprocess.CompletedProcess(argv, 0)
        if verb == "bootstrap":
            path = Path(argv[3])
            self.loaded[path.name.removesuffix(".plist")] = self.disk[path]
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError(argv)


class _Systemd:
    """A fake systemctl: ``known`` is what the last daemon-reload read from disk; ``active`` maps a unit to
    the definition it was started with."""

    def __init__(self, disk, active=None, pending_restart=None):
        # ``pending_restart``: a Restart=always unit between its exit and its restart, on the definition
        # it will come back on (systemd reports it "activating"; ``is-active`` says no).
        self.disk, self.active, self.calls = disk, dict(active or {}), []
        self.pending = dict(pending_restart or {})
        self.known = dict(disk)

    def __call__(self, argv, **kw):
        argv = tuple(argv)
        self.calls.append(argv[1:])
        verb, name = argv[1], argv[-1]
        if verb == "is-active":
            return subprocess.CompletedProcess(argv, 0 if name in self.active else 3)
        if verb == "stop":
            self.active.pop(name, None)
            self.pending.pop(name, None)
            return subprocess.CompletedProcess(argv, 0)
        if verb == "show":
            state = "active" if name in self.active else "activating" if name in self.pending else "inactive"
            return subprocess.CompletedProcess(argv, 0, stdout=f"{state}\n")
        if verb == "restart":
            self.active[name] = self.known[units.systemd_path(name)]
            return subprocess.CompletedProcess(argv, 0)
        if verb == "daemon-reload":
            self.known = dict(self.disk)
            # the pending restart fires on the definition it was holding, not the reloaded file
            for n, d in self.pending.items():
                self.active.setdefault(n, d)
            self.pending.clear()
            return subprocess.CompletedProcess(argv, 0)
        if verb == "enable":
            self.active.setdefault(name, self.known[units.systemd_path(name)])
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError(argv)


def _apply(steps, fake, disk, *, crash_after_write_of=None):
    """Run ``steps`` the way the apply layer does. ``crash_after_write_of``: stop right after that
    file is written, as a run that dies there would."""
    for s in steps:
        if s.write:
            disk[s.write[0]] = s.write[1].encode()
            fake.calls.append(("WRITE", s.write[0]))
            if s.write[0] == crash_after_write_of:
                return
            continue
        if s.call:
            ok, why = s.call()
            assert ok, why
            continue
        if s.skip_if and fake(s.skip_if).returncode == 0:
            continue
        fake(s.argv)


def _old(text: str, host: str) -> str:
    return (text.replace("control</string>\n\t</array>", "old</string>\n\t</array>") if host == "darwin"
            else text.replace("RestartSec=1", "RestartSec=9"))


def _unit(host, svc):
    if host == "darwin":
        return units.launchd_label(svc), units.launchd_path(svc), units.launchd_plist_text(svc)
    name = units.systemd_name(svc)
    return name, units.systemd_path(name), units.systemd_unit_text(svc)


def _running(host, fake):
    return fake.loaded if host == "darwin" else fake.active


@pytest.mark.parametrize("host", HOSTS)
def test_crash_after_unit_write_reruns_to_new_definition(host):
    svc = units.by_name("control")
    name, path, text = _unit(host, svc)
    old = _old(text, host).encode()
    assert old != text.encode()
    disk = {path: old}
    fake = (_Launchd(disk, {name: old}) if host == "darwin" else _Systemd(disk, {name: old}))
    read = lambda p: disk.get(p)                                              # noqa: E731
    # the run dies right after it wrote the new file
    _apply(units.install_steps(host, [svc], run=fake, read=read), fake, disk, crash_after_write_of=path)
    assert disk[path] == text.encode()
    # the re-run plans afresh, against a disk that already holds the new file
    _apply(units.install_steps(host, [svc], run=fake, read=read), fake, disk)
    assert _running(host, fake).get(name) == text.encode()


@pytest.mark.parametrize("host", HOSTS)
@pytest.mark.parametrize("case", ["fresh", "running-unchanged", "running-changed", "stopped-changed"])
def test_unit_unloaded_before_write_only_when_running_and_changed(host, case):
    svc = units.by_name("control")
    name, path, text = _unit(host, svc)
    disk = {}
    if case == "running-unchanged":
        disk[path] = text.encode()
    elif case in ("running-changed", "stopped-changed"):
        disk[path] = _old(text, host).encode()
    running = {name: disk[path]} if case.startswith("running") else {}
    fake = _Launchd(disk, running) if host == "darwin" else _Systemd(disk, running)
    _apply(units.install_steps(host, [svc], run=fake, read=lambda p: disk.get(p)), fake, disk)
    assert disk[path] == text.encode() and _running(host, fake).get(name) == text.encode()
    unload = "bootout" if host == "darwin" else "stop"
    unloads = [i for i, c in enumerate(fake.calls) if c[0] == unload]
    # Linux stops a changed unit whether or not it reads active (r3: a pending auto-restart reads inactive).
    if case == "running-changed" or (host == "linux" and case == "stopped-changed"):
        assert len(unloads) == 1 and unloads[0] < fake.calls.index(("WRITE", path))
    else:
        assert unloads == []
    assert not any(c[0] == "restart" for c in fake.calls)


def test_linux_changed_unit_in_a_pending_restart_comes_back_on_the_new_definition():
    """r3 (codex MED): a Restart=always unit between its exit and its restart reads not-active; gating
    the stop on ``is-active`` let it restart on the old definition at the daemon-reload."""
    svc = units.by_name("control")
    name, path, text = _unit("linux", svc)
    old = _old(text, "linux").encode()
    disk = {path: old}
    fake = _Systemd(disk, pending_restart={name: old})
    _apply(units.install_steps("linux", [svc], run=fake, read=lambda p: disk.get(p)), fake, disk)
    assert fake.active.get(name) == text.encode()


@pytest.mark.parametrize("host", HOSTS)
def test_unload_step_comes_before_its_write_and_no_restart_step(host):
    steps = units.install_steps(host, units.services_for(host))
    for s in units.services_for(host):
        name, path, _ = _unit(host, s)
        w = next(i for i, x in enumerate(steps) if x.write and x.write[0] == path)
        verb = "boot out" if host == "darwin" else "stop"
        un = next(i for i, x in enumerate(steps) if x.call and x.why.startswith(f"{verb} {name} "))
        assert un < w
    assert not any(x.why.startswith("restart ") for x in steps)
    assert not any(x.argv and "restart" in x.argv for x in steps)


# --- G2: the journal and pf's token are written whole --------------------------------------------------


def _short_writes(monkeypatch, *, zero_after=None):
    """os.write that writes at most 3 bytes a call; after ``zero_after`` calls it makes no progress."""
    real, n = os.write, [0]

    def write(fd, data):
        n[0] += 1
        if zero_after is not None and n[0] > zero_after:
            return 0
        return real(fd, bytes(data[:3]))

    monkeypatch.setattr(os, "write", write)


def test_journal_write_survives_short_writes(tmp_path, monkeypatch):
    j = Intent("setup", OP, {"_levain_control": 3790}, {})
    text = intent_mod.intent_text(j)
    _short_writes(monkeypatch)
    assert intent_mod.write_intent_atomically(tmp_path / "install-intent", text) == (True, "")
    assert (tmp_path / "install-intent").read_text() == text


def test_journal_zero_progress_write_is_an_error(tmp_path, monkeypatch):
    target = tmp_path / "install-intent"
    _short_writes(monkeypatch, zero_after=1)
    ok, why = intent_mod.write_intent_atomically(target, intent_mod.intent_text(Intent("setup", OP, {}, {})))
    assert not ok and why
    assert not target.exists() and not target.with_name(target.name + ".tmp").exists()


def test_pf_token_write_survives_short_writes(tmp_path, monkeypatch):
    tok = tmp_path / "pf.token"
    _short_writes(monkeypatch)
    pf._write_token(tok, "18446744073709551615")
    assert tok.read_text() == "18446744073709551615\n"


def test_pf_token_zero_progress_write_raises_and_renames_nothing(tmp_path, monkeypatch):
    tok = tmp_path / "pf.token"
    tok.write_text("12\n")
    _short_writes(monkeypatch, zero_after=1)
    with pytest.raises(OSError):
        pf._write_token(tok, "18446744073709551615")
    assert tok.read_text() == "12\n"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pf.token"]


# --- G3: undo clears a setup that died before its operator record ---------------------------------------


def _facts(host, **kw):
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                xcode_ready=lambda: True)
    base.update(kw)
    return HostFacts(host, **base)


def _abandoned(host):
    first = plan_setup(parse_install_argv(["setup", "--slots", "2", "--verifier-slots", "1", "--operator", OP]),
                       _facts(host), read_sysctl=lambda p: "1", program_exists=lambda p: True)
    j = first.intent
    made = {**{g: j.groups[g] for g in SHARED_GROUPS}, **{n: j.accounts[n] for n in SINGLETONS},
            "_levain_s0": j.accounts["_levain_s0"]}
    return j, made


@pytest.mark.parametrize("host", HOSTS)
def test_undo_clears_abandoned_setup_intent(host):
    j, made = _abandoned(host)
    present = {**made, "_levain_gw0": j.accounts["_levain_gw0"] + 1}       # gw0's name is held at another id
    undo_cmd = parse_install_argv(["undo"])
    # the caller here is not the operator the journal names
    stranger = _facts(host, caller_uid=OP_UID + 1, caller_name="mallory", intent=j, host_ids=present)
    assert pytest.raises(Refused, plan_undo, undo_cmd, stranger, accounts={}, shared_groups=()).value.name \
        == "operator-mismatch"
    # with no journal and no record: nothing to undo
    assert pytest.raises(Refused, plan_undo, undo_cmd, _facts(host), accounts={}, shared_groups=()).value.name \
        == "not-set-up"
    # a replenish journal with no record is not a setup that died: still not set up
    rj = Intent("replenish", OP, {"_levain_s0": j.accounts["_levain_s0"]})
    assert pytest.raises(Refused, plan_undo, undo_cmd, _facts(host, intent=rj, host_ids=present),
                         accounts={}, shared_groups=()).value.name == "not-set-up"
    undo = plan_undo(undo_cmd, _facts(host, intent=j, host_ids=present), accounts={}, shared_groups=())
    tombed = {s.why.split(":")[0].removeprefix("tombstone ") for s in undo.steps if s.why.startswith("tombstone ")}
    assert tombed == {n for n in made if n not in SHARED_GROUPS}
    assert j.accounts["_levain_gw0"] not in undo.retired_after
    assert {j.accounts[n] for n in tombed} <= undo.retired_after
    deleted = [s.why.removeprefix("delete the shared group ") for s in undo.steps if "delete the shared group" in s.why]
    assert sorted(deleted) == sorted(SHARED_GROUPS)
    # the journal goes last; there is no record to remove
    assert undo.steps[-1].argv[1:] == ("-f", str(INTENT_PATH[host]))
    assert not any(str(operator_record_path(host)) in s.argv for s in undo.steps)
    # with the record present, undo is the ordinary one
    normal = plan_undo(undo_cmd, _facts(host, operator_record=operator_record(OP, OP_UID), intent=j,
                                        host_ids=present), accounts={}, shared_groups=())
    assert normal.steps[-1].argv[1:] == ("-f", str(operator_record_path(host)))


# --- G4 / G5: an OSError is a refusal, not a crash -----------------------------------------------------


def _st(mode, uid=0):
    return os.stat_result((mode, 0, 0, 1, uid, 0, 0, 0, 0, 0))


def test_root_owned_ancestors_dangling_link_ancestor_refuses():
    # /opt/lv is a link to a target that is gone; realpath leads to /gone, whose lstat raises
    def lstat(p):
        if p in ("/", "/opt"):
            return _st(stat.S_IFDIR | 0o755)
        if p == "/opt/lv":
            return _st(stat.S_IFLNK | 0o777)
        raise FileNotFoundError(errno.ENOENT, "No such file", p)

    ok, why = root_tree._root_owned_ancestors(Path("/opt/lv/tree"), lstat=lstat, realpath=lambda p: "/gone/tree")
    assert not ok and why


def test_root_owned_ancestors_other_oserror_refuses():
    def lstat(p):
        raise PermissionError(errno.EACCES, "Permission denied", p)

    ok, why = root_tree._root_owned_ancestors(Path("/opt/lv/tree"), lstat=lstat, realpath=lambda p: p)
    assert not ok and why


def test_pf_token_path_that_is_a_directory_refuses(tmp_path):
    tok = tmp_path / "pf.token"
    tok.mkdir()
    calls = []
    run = lambda argv, **kw: calls.append(argv) or subprocess.CompletedProcess(argv, 0, "Token : 7\n", "")  # noqa: E731
    ok, why = pf.release_enable_reference(tok, run)
    assert not ok and why and tok.is_dir()
    ok, why = pf.take_enable_reference(tok, run)
    assert not ok and why and tok.is_dir()
    assert calls == []
