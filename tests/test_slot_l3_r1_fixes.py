"""S1 plan layer, L3 round 1 fixes (the triage's F rows). Every row is a plan or text assertion run
against fakes; nothing here touches pf, launchd, the directory service or a namespace."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import levain
from levain.firing import hands
from levain.slot import Refused, accounts, netns, pf, root_tree, units
from levain.slot import runtime_grammar as rg
from levain.slot.accounts import SLOT_MARKER, SLOT_RETIRED_MARKER, Account
from levain.slot.grammar import parse_install_argv
from levain.slot.ids import SHARED_GROUPS
from levain.slot.install_helper import EXIT_NOT_BUILT, HostFacts, extra_tools, operator_record, plan_setup, plan_undo

HOSTS = ("linux", "darwin")
OP, OP_UID = "alice", 501


@pytest.fixture(autouse=True)
def _tools_resolve(monkeypatch):
    real = hands.shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: real(n, path=path) or f"/usr/sbin/{n}")


def _facts(host, **kw):
    base = dict(caller_uid=OP_UID, caller_name=OP, operator_uid=OP_UID, tool_present=lambda p: True,
                xcode_ready=lambda: True)
    base.update(kw)
    return HostFacts(host, **base)


def _setup_cmd():
    return parse_install_argv(["setup", "--slots", "2", "--verifier-slots", "1", "--operator", OP])


def _plan(host, **kw):
    return plan_setup(_setup_cmd(), _facts(host, **kw), read_sysctl=lambda p: "1", program_exists=lambda p: True)


def _undo(host, accts, **kw):
    return plan_undo(parse_install_argv(["undo"]), _facts(host, operator_record=operator_record(OP, OP_UID), **kw),
                     accounts=accts, shared_groups=SHARED_GROUPS)


# --- F1 / F2: pf's enable reference ---------------------------------------------------------------------


class _Pf:
    """A fake pfctl. -E hands out tokens; -X releases one it handed out. ``x_fail``: the stderr -X
    prints, with a non-zero exit, for a token it is told to fail."""

    def __init__(self, x_fail=None, fail_tokens=()):
        self.calls, self.live, self.next = [], set(), 1000
        self.x_fail, self.fail_tokens = x_fail, set(fail_tokens)

    def __call__(self, argv, **kw):
        self.calls.append(tuple(argv))
        if argv[1] == "-E":
            self.next += 1
            self.live.add(str(self.next))
            return subprocess.CompletedProcess(argv, 0, "", f"pf enabled\nToken : {self.next}\n")
        if argv[1] == "-X":
            if argv[2] in self.fail_tokens:
                return subprocess.CompletedProcess(argv, 1, "", self.x_fail)
            ok = argv[2] in self.live
            self.live.discard(argv[2])
            return subprocess.CompletedProcess(argv, 0 if ok else 1, "", "" if ok else "pfctl: pf not enabled")
        raise AssertionError(argv)


def test_pf_new_reference_taken_before_old_released(tmp_path):
    tok = tmp_path / "pf.token"
    fake = _Pf()
    assert pf.take_enable_reference(tok, fake) == (True, "")
    fake.calls.clear()
    assert pf.take_enable_reference(tok, fake) == (True, "")
    assert fake.calls == [("/sbin/pfctl", "-E"), ("/sbin/pfctl", "-X", "1001")]   # take, THEN release
    assert fake.live == {"1002"} and tok.read_text() == "1002\n"


def test_pf_old_reference_unreleasable_keeps_its_token_and_gives_the_new_one_back(tmp_path):
    tok = tmp_path / "pf.token"
    fake = _Pf(x_fail="pfctl: DIOCSTOPREF: Device busy", fail_tokens={"1001"})
    assert pf.take_enable_reference(tok, fake) == (True, "")
    ok, why = pf.take_enable_reference(tok, fake)
    assert ok is False and "token kept" in why
    assert tok.read_text() == "1001\n"                 # the reference still held is still recorded
    assert fake.live == {"1001"}                       # the new one was given back: one reference, not two


def test_pf_release_keeps_the_token_unless_pf_confirms_it_gone(tmp_path):
    tok = tmp_path / "pf.token"
    tok.write_text("4242\n")
    busy = _Pf(x_fail="pfctl: DIOCSTOPREF: Device busy", fail_tokens={"4242"})
    ok, why = pf.release_enable_reference(tok, busy)
    assert ok is False and tok.read_text() == "4242\n"
    # pfctl's text for pf holding no reference (a reboot dropped it): the reference is gone either way
    gone = _Pf(x_fail="pfctl: pf not enabled\n", fail_tokens={"4242"})
    ok, why = pf.release_enable_reference(tok, gone)
    assert ok is True and not tok.exists() and "no longer held" in why
    assert pf.PF_TOKEN_GONE == ("pf not enabled", "pf: token invalid")


def test_pf_release_treats_token_invalid_as_gone(tmp_path):
    """pf enabled by something else, the token forgotten: pfctl's measured text (1c VM row C21)."""
    tok = tmp_path / "pf.token"
    tok.write_text("4242\n")
    gone = _Pf(x_fail="pfctl: pf: token invalid\n", fail_tokens={"4242"})
    ok, why = pf.release_enable_reference(tok, gone)
    assert ok is True and not tok.exists() and "no longer held" in why


def test_pf_token_written_atomically(tmp_path, monkeypatch):
    tok = tmp_path / "pf.token"
    synced = []
    real = os.fsync
    monkeypatch.setattr(pf.os, "fsync", lambda fd: synced.append(fd) or real(fd))
    assert pf.take_enable_reference(tok, _Pf()) == (True, "")
    assert len(synced) == 2                            # the file's bytes, then the directory entry
    assert tok.read_text() == "1001\n" and stat.S_IMODE(tok.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pf.token"]   # no temp file left


def test_pf_malformed_token_does_not_wedge_undo(tmp_path):
    tok = tmp_path / "pf.token"
    for junk in (b"1; rm -rf /\n", b"", b"\xff\xfe\n"):
        tok.write_bytes(junk)
        fake = _Pf()
        ok, why = pf.release_enable_reference(tok, fake)
        assert ok is True and not tok.exists() and fake.calls == []
    # setup over a malformed file takes a fresh reference and replaces it
    tok.write_text("garbage\n")
    fake = _Pf()
    ok, _ = pf.take_enable_reference(tok, fake)
    assert ok and fake.calls == [("/sbin/pfctl", "-E")] and tok.read_text() == "1001\n"


# --- F3: the label is the last act -----------------------------------------------------------------------


def test_linux_revive_lifts_the_expiry_before_the_live_label():
    steps = accounts.revive_steps("linux", [Account("_levain_control", 3790, ())])
    labelled = [i for i, s in enumerate(steps) if SLOT_MARKER in s.argv]
    expiry = [i for i, s in enumerate(steps) if s.argv[0].endswith("chage")]
    assert labelled == [len(steps) - 1] and expiry and max(expiry) < labelled[0]


@pytest.mark.parametrize("host", HOSTS)
def test_tombstone_label_is_last(host):
    steps = accounts.tombstone_steps(host, "_levain_s0")
    retired = [i for i, s in enumerate(steps) if SLOT_RETIRED_MARKER[host] in s.argv]
    assert retired == [len(steps) - 1]
    if host == "darwin":
        leave = next(i for i, s in enumerate(steps) if s.call)
        assert leave < retired[0]
    else:
        assert any(s.argv[0].endswith("chage") for s in steps[:-1])


# --- F5 / F6 / F7: launchd -------------------------------------------------------------------------------


def test_pf_one_shot_retries_until_it_succeeds():
    import plistlib

    job = plistlib.loads(units.launchd_plist_text(units.by_name("pf")).encode())
    assert job["KeepAlive"] == {"SuccessfulExit": False}


class _Launchd:
    def __init__(self, loaded=()):
        self.loaded, self.calls = set(loaded), []

    def __call__(self, argv, **kw):
        argv = tuple(argv)
        self.calls.append(argv)
        verb = argv[1]
        if verb == "print":
            return subprocess.CompletedProcess(argv, 0 if argv[2].removeprefix("system/") in self.loaded else 113)
        if verb == "bootout":
            self.loaded.discard(argv[2].removeprefix("system/"))
            return subprocess.CompletedProcess(argv, 0)
        if verb == "bootstrap":
            self.loaded.add(Path(argv[3]).name.removesuffix(".plist"))
            return subprocess.CompletedProcess(argv, 0)
        raise AssertionError(argv)


def _apply(steps, launchd, disk):
    """Run ``steps`` the way the apply layer does, against a fake launchctl and a dict as the disk."""
    for s in steps:
        if s.write:
            disk[s.write[0]] = s.write[1].encode()
            continue
        if s.call:
            ok, why = s.call()
            assert ok, why
            continue
        if s.skip_if and launchd(s.skip_if).returncode == 0:
            continue
        launchd(s.argv)


@pytest.mark.parametrize("case", ["fresh", "loaded-unchanged", "loaded-changed"])
def test_launchd_reload_only_a_loaded_changed_job(case):
    svc = units.by_name("control")
    label, path, text = units.launchd_label(svc), units.launchd_path(svc), units.launchd_plist_text(svc)
    disk = {}
    if case == "loaded-unchanged":
        disk[path] = text.encode()
    elif case == "loaded-changed":
        disk[path] = text.replace("control</string>\n\t</array>", "old</string>\n\t</array>").encode()
        assert disk[path] != text.encode()
    launchd = _Launchd(() if case == "fresh" else (label,))
    steps = units.install_steps("darwin", [svc], run=launchd, read=lambda p: disk.get(p))
    _apply(steps, launchd, disk)
    verbs = [c[1] for c in launchd.calls if c[1] != "print"]
    assert disk[path] == text.encode() and label in launchd.loaded
    assert verbs == {"fresh": ["bootstrap"], "loaded-unchanged": [], "loaded-changed": ["bootout", "bootstrap"]}[case]


@pytest.mark.parametrize("host", HOSTS)
def test_install_steps_take_any_iterable(host):
    want = units.install_steps(host, tuple(units.services_for(host)))
    got = units.install_steps(host, (s for s in units.services_for(host)))
    assert [(s.why, s.argv, s.write) for s in got] == [(s.why, s.argv, s.write) for s in want]
    assert len([s for s in got if s.write]) == len(units.services_for(host))


# --- F8: the install-helper entry refuses until the apply layer exists ---------------------------------


def test_install_helper_entry_exits_non_zero(tmp_path):
    tree = tmp_path / "tree"
    shutil.copytree(Path(levain.__file__).parent, tree / "levain", ignore=shutil.ignore_patterns("__pycache__"))
    entry = tmp_path / "install-helper"
    entry.write_text(root_tree.entry_text("darwin" if sys.platform == "darwin" else "linux", "install-helper",
                                          tree=tree))
    r = subprocess.run([sys.executable, "-IS", str(entry), "setup"], capture_output=True, text=True, cwd=tmp_path,
                       env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == EXIT_NOT_BUILT == 2, r.stderr
    assert "levain install helper: apply layer not built (S1 session 2)" in r.stderr


# --- F9: a failing `ip netns list` is neither present nor absent -----------------------------------------


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="no /bin/sh")
def test_netns_probe_fails_loud_when_ip_fails(tmp_path, monkeypatch):
    ip = tmp_path / "ip"
    ip.write_text("#!/bin/sh\necho 'netns: permission denied' >&2\nexit 1\n")
    ip.chmod(0o755)
    real = shutil.which
    monkeypatch.setattr(hands.shutil, "which", lambda n, path=None: str(ip) if n == "ip" else real(n) or real(n, path=path))
    for probe in (netns.netns_present, netns.netns_absent):
        assert subprocess.run(probe("levain-e0"), capture_output=True).returncode == 2, probe.__name__


# --- F10: the stdin cap counts bytes ---------------------------------------------------------------------


def test_stdin_cap_counts_bytes_of_a_wide_memoryview():
    import array

    wide = memoryview(array.array("I", [0x20202020] * (rg.STDIN_MAX // 4 + 1)))
    assert len(wide) < rg.STDIN_MAX < wide.nbytes
    with pytest.raises(Refused) as ei:
        rg.parse_stdin("start", wide)
    assert ei.value.name == "stdin-too-large"


# --- F13: tools that appear only as arguments or inside callbacks ----------------------------------------


@pytest.mark.parametrize("host", HOSTS)
def test_preflight_checks_extra_tools(host):
    extras = extra_tools(host, "setup")
    want = {"darwin": ("/usr/sbin/dseditgroup", "/usr/bin/pkill", "/usr/bin/pgrep"),
            "linux": tuple(hands._abs(n) for n in ("bwrap", "pkill", "pgrep"))}[host]
    assert extras == want and all(t.startswith("/") for t in extras)
    assert extra_tools(host, "replenish") == want
    # TRIAGE r1 (A1 report): undo never reads the system bwrap, so a host without one can still undo
    undo_want = tuple(t for t in want if host != "linux" or not t.endswith("/bwrap"))
    assert extra_tools(host, "undo") == undo_want
    for t in extras:
        facts_kw = dict(tool_present=lambda p, gone=t: p != gone)
        with pytest.raises(Refused) as ei:
            _plan(host, **facts_kw)
        assert ei.value.name == "tool-missing" and t in ei.value.detail
        if t not in undo_want:
            _undo(host, {"_levain_s0": 3999 if host == "darwin" else 899}, **facts_kw)
            continue
        with pytest.raises(Refused) as ei:
            _undo(host, {"_levain_s0": 3999 if host == "darwin" else 899}, **facts_kw)
        assert ei.value.name == "tool-missing" and t in ei.value.detail
