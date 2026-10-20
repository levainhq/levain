"""S1 session 2 fix round 1 (lane G), END: the L1 + L2 findings on ``levain.slot.end``, each pinned by a test
that fails on the code before the fix. The world, the REAL CONTROL CAS and the seams are the 2c suite's
(``tests/test_slot_s2c_end.py``); nothing here is root, so the session-uid reader is stage's in-process runner
or a runner that answers as a slot that cannot read, and the drop itself is the VM's."""
from __future__ import annotations

import base64
import ctypes
import ctypes.util
import fcntl
import os
import stat
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import end as end_mod
from levain.slot.end import (CREDENTIAL_PARKED, DEAD, FROZEN, HELD, RETAINED, REVOKED, CasNotSent, EndFacts,
                             end_session, reconcile_session)
from levain.slot.ids import SlotId
from levain.slot.root_intent import read_root_intent
from tests.test_slot_s2c_end import (CRED, DARWIN, GID, LEASE_SEQ, NOW, SESSION, UF_IMMUTABLE, UID, Control, Crash,
                                     World, mine, slot_state, thread_run_as)

OTHER = "s-fedcba9876543210"


def meta_by_path(w: World) -> dict:
    return {base64.b64decode(m["path_b64"]): m
            for m in map(canon.parse, (w.facts.retained / "meta.jsonl").read_bytes().splitlines())}


def manifest_by_path(w: World) -> dict:
    return {base64.b64decode(m["path_b64"]): m for m in w.manifest_file()}


class Recorder:
    """freeze / kill / no_process seams that record every uid they are called with."""

    def __init__(self, no_process=lambda uid: True) -> None:
        self.calls: list = []
        self._np = no_process

    def kw(self) -> dict:
        return dict(freeze=lambda uid, b: self.calls.append(("freeze", uid)) or True,
                    kill=lambda uid, b: self.calls.append(("kill", uid)) or True,
                    no_process=lambda uid: self.calls.append(("no_process", uid)) or self._np(uid))


class _Done:
    def kill(self) -> None:
        pass

    def wait(self) -> None:
        pass


def cannot_read_run_as(calls: list):
    """A session-uid reader whose uid cannot open the file: it answers stage's ``staging-open-failed``."""
    def run_as(uid, gid, fn, wfd, keep=()):
        calls.append((uid, gid, tuple(keep)))
        name = b"staging-open-failed"
        os.write(wfd, b"R" + bytes([len(name)]) + name)
        os.close(wfd)
        return _Done()
    return run_as


def silent_run_as(uid, gid, fn, wfd, keep=()):
    """A reader that dies without an answer (its drop refused, say)."""
    os.close(wfd)
    return _Done()


# --- H1 / L1-F3: "cloned" is root's fact ---------------------------------------------------------------------


def test_emptied_repo_with_root_marker_is_retained(tmp_path):
    """The agent empties <S>/repo before END; root's marker says the clone ran, so the tree is retained
    (and <S> + the var-folders dir leave the slot's reach), never "never cloned"."""
    w = World(tmp_path, uchg=False)
    for p in sorted((w.S / "repo").rglob("*"), reverse=True):
        p.rmdir() if p.is_dir() and not p.is_symlink() else p.unlink()
    assert os.listdir(w.S / "repo") == []
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert w.facts.tree.is_dir() and (w.facts.tree / "config" / "claude" / "settings.local.json").exists()
    assert not w.facts.live.exists() and not w.vf.exists()
    assert ctl.history[-1][1]["manifest"]["entries"] > 0


def test_cloned_marker_is_root_written(tmp_path):
    w = World(tmp_path, uchg=False, cloned=False)
    assert end_mod.repo_cloned(w.facts, owner_ok=mine) is False
    end_mod.mark_cloned(w.L, SESSION, now=lambda: NOW)
    st = os.lstat(w.facts.cloned_marker)
    assert stat.S_IMODE(st.st_mode) == 0o600 and w.facts.cloned_marker.read_text() == NOW + "\n"
    assert end_mod.repo_cloned(w.facts, owner_ok=mine) is True
    with pytest.raises(Refused) as e:
        end_mod.repo_cloned(w.facts, owner_ok=lambda st: False)   # a marker root does not own counts for nothing
    assert e.value.name == "bad-root"


# --- H2: outside-linked inodes are read by the session uid -----------------------------------------------------


def _outside(w: World, tmp_path: Path, data: bytes = b"bytes of a file outside the tree\n") -> Path:
    outside = tmp_path / "outside.txt"
    outside.write_bytes(data)
    os.chmod(outside, 0o644)
    os.link(outside, w.S / "repo" / "hl-out")
    os.link(outside, w.S / "repo" / "private" / "hl-out-2")
    return outside


def test_outside_link_the_session_cannot_read_is_recorded_never_copied(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = _outside(w, tmp_path)
    ino = os.lstat(outside).st_ino
    opened: list = []
    real_open = os.open

    def traced_open(*a, **k):
        fd = real_open(*a, **k)
        if threading.current_thread() is threading.main_thread() and os.fstat(fd).st_ino == ino:
            opened.append(a[0])
        return fd
    monkeypatch.setattr(os, "open", traced_open)
    calls: list = []
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=cannot_read_run_as(calls)), record_state=REVOKED) == RETAINED, w.notes
    assert opened == []                                     # root never opened the outside inode
    assert len(calls) == 1 and calls[0][:2] == (UID, UID)    # one read per inode, as the session uid
    m = manifest_by_path(w)
    for p in (b"repo/hl-out", b"repo/private/hl-out-2"):
        assert (m[p]["type"], m[p]["size"], m[p]["digest"]) == ("file", 0, "")   # recorded, no bytes
        placeholder = w.facts.tree / os.fsdecode(p)
        assert placeholder.read_bytes() == b"" and stat.S_IMODE(os.lstat(placeholder).st_mode) == 0o600
    a, b = os.lstat(w.facts.tree / "repo" / "hl-out"), os.lstat(w.facts.tree / "repo" / "private" / "hl-out-2")
    assert a.st_ino == b.st_ino and a.st_nlink == 2           # the in-tree link set stays one inode
    after = os.lstat(outside)
    assert (after.st_ino, after.st_nlink, outside.read_bytes()) == (ino, 1, b"bytes of a file outside the tree\n")
    assert b"repo/hl-out" not in meta_by_path(w)              # its metadata is the outside owner's, never read
    assert sorted(os.listdir(w.facts.retained)) == ["manifest.jsonl", "meta.jsonl", "object.json", "retained_at",
                                                    "tree"]
    assert any("cannot read it" in n for n in w.notes)


def test_outside_link_the_session_can_read_is_copied_from_its_read(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    data = b"readable by the slot\n" * 100
    outside = _outside(w, tmp_path, data)
    calls: list = []
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=thread_run_as(calls)), record_state=REVOKED) == RETAINED
    assert [c[:2] for c in calls] == [(UID, UID)]
    m = manifest_by_path(w)
    assert m[b"repo/hl-out"]["digest"] == end_mod.digest_bytes(data) and m[b"repo/hl-out"]["size"] == len(data)
    copy = w.facts.tree / "repo" / "hl-out"
    assert copy.read_bytes() == data and os.lstat(copy).st_ino != os.lstat(outside).st_ino
    assert os.lstat(copy).st_nlink == 2 and os.lstat(outside).st_nlink == 1


def test_a_reader_that_dies_unanswered_is_held_not_unread(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    _outside(w, tmp_path)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=silent_run_as), record_state=REVOKED) == HELD
    assert ctl.failed_step == "manifest" and any("link-reader-failed" in n for n in w.notes)
    assert w.S.exists() and not w.facts.tree.exists()


def test_crash_between_link_and_rename_converges(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = _outside(w, tmp_path, b"x" * 10)
    ctl = Control()
    real_rename = os.rename
    fired = []

    def crash_once(src, dst, *a, **k):
        if isinstance(src, bytes) and src.startswith(b".levain-link.") and not fired:
            fired.append(src)
            raise Crash("between link and rename")
        return real_rename(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", crash_once)
    with pytest.raises(Crash):
        end_session(w.facts, w.seams(ctl, run_as=thread_run_as()), record_state=REVOKED)
    monkeypatch.setattr(os, "rename", real_rename)
    reconcile_session(w.facts, w.seams(ctl, run_as=thread_run_as()))
    assert ctl.state == RETAINED
    names = [p.name for p in w.facts.tree.rglob("*")]
    assert not [n for n in names if n.startswith(".levain-link.")]
    assert (w.facts.tree / "repo" / "hl-out").read_bytes() == b"x" * 10 and os.lstat(outside).st_nlink == 1
    assert not (w.facts.retained / "linkcopy").exists()


# --- H3 / L1-F2: every uid the session holds -----------------------------------------------------------------


def _verifier(w: World, child: int) -> EndFacts:
    (w.S / "config" / "claude" / ".credentials.json").unlink()
    facts = EndFacts(SESSION, SlotId("none", 2), UID, GID, w.L, lease_seq=LEASE_SEQ, child_uid=child,
                     vendor="claude", auth="none")
    slot_state(w.L, facts.slot)
    return facts


def test_verifier_end_proves_slot_and_child_uids(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    child = UID + 7
    facts = _verifier(w, child)
    rec = Recorder()
    ctl = Control()
    assert end_session(facts, w.seams(ctl, **rec.kw()), record_state=REVOKED) == RETAINED
    for what in ("freeze", "kill", "no_process"):
        assert [u for c, u in rec.calls if c == what] == [UID, child], what
    # freezes all come before the first kill, and every proof before the tree is touched
    assert [c for c, _ in rec.calls] == ["freeze", "freeze", "kill", "kill", "no_process", "no_process"]


def test_a_live_child_process_is_held_kill(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    child = UID + 7
    facts = _verifier(w, child)
    rec = Recorder(no_process=lambda uid: uid != child)
    ctl = Control()
    pre = sorted(p.name for p in w.S.rglob("*"))
    assert end_session(facts, w.seams(ctl, **rec.kw()), record_state=REVOKED) == HELD
    assert ctl.failed_step == "kill" and sorted(p.name for p in w.S.rglob("*")) == pre
    assert not facts.retained.exists()


# --- M1: setuid / setgid never survive into a root-owned tree --------------------------------------------------


def test_setuid_setgid_cleared_before_root_chown(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    f = w.S / "repo" / "suid"
    f.write_text("#!/bin/sh\n")
    os.chmod(f, 0o6755)
    d = w.S / "repo" / "sgid-dir"
    d.mkdir()
    os.chmod(d, 0o2755)
    set_bits = stat.S_IMODE(os.lstat(f).st_mode) & 0o6000
    assert set_bits & stat.S_ISUID                          # the test's own file holds setuid as the slot would
    ctl = Control()
    # root's chown (a no-op here, as root's keeps the bits on macOS): the strip alone must clear them
    seams = w.seams(ctl, chown=lambda dirfd, name, uid, gid: None)
    assert end_session(w.facts, seams, record_state=REVOKED) == RETAINED
    assert stat.S_IMODE(os.lstat(w.facts.tree / "repo" / "suid").st_mode) == 0o755
    assert stat.S_IMODE(os.lstat(w.facts.tree / "repo" / "sgid-dir").st_mode) & 0o6000 == 0
    assert manifest_by_path(w)[b"repo/suid"]["mode"] == 0o755 | set_bits   # the original mode is in the manifest


# --- M2: no full-path metadata call ----------------------------------------------------------------------------


@pytest.mark.skipif(not DARWIN, reason="macOS PATH_MAX and BSD flags")
def test_deep_immutable_file_is_retained_with_its_flags(tmp_path):
    """A file past PATH_MAX (1,024 on macOS) with ``uchg``: its flags are recorded, cleared, and END ends
    RETAINED, never HELD "tree"."""
    w = World(tmp_path, var_folders=False, uchg=False)
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    seg = b"d" * 200
    fds = [os.open(w.S / "repo", os.O_RDONLY | os.O_DIRECTORY)]
    try:
        for _ in range(6):
            os.mkdir(seg, dir_fd=fds[-1])
            fds.append(os.open(seg, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fds[-1]))
        f = os.open(b"deep", os.O_WRONLY | os.O_CREAT, 0o644, dir_fd=fds[-1])
        os.write(f, b"deep work\n")
        assert libc.fchflags(f, UF_IMMUTABLE) == 0
        os.close(f)
    finally:
        for fd in fds:
            os.close(fd)
    rel = b"repo/" + b"/".join([seg] * 6) + b"/deep"
    assert len(os.fsencode(w.S) + b"/" + rel) > 1024
    ctl = Control()
    try:
        assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
        assert meta_by_path(w)[rel]["flags"] & UF_IMMUTABLE
        assert not [n for n in w.notes if "deep" in n]
        fd = os.open(w.facts.tree / "repo", os.O_RDONLY | os.O_DIRECTORY)
        for _ in range(6):
            nfd = os.open(seg, os.O_RDONLY | os.O_DIRECTORY, dir_fd=fd)
            os.close(fd)
            fd = nfd
        try:
            assert os.stat(b"deep", dir_fd=fd, follow_symlinks=False).st_flags == 0
        finally:
            os.close(fd)
    finally:
        for root in (w.S, w.facts.tree):          # leave nothing immutable for the tmp cleanup
            if root.exists():
                for e in end_mod.walk(root):
                    if getattr(e.st, "st_flags", 0):
                        g = os.open(e.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=e.dirfd)
                        libc.fchflags(g, 0)
                        os.close(g)


# --- M4: root's slot state file before any signal --------------------------------------------------------------


@pytest.mark.parametrize("state_kw", [
    {"session": OTHER},                     # re-leased to another session
    {"lease_seq": LEASE_SEQ + 1},          # a later lease of this slot
    {"state": "scrubbed"},                 # scrubbed, not leased
    None,                                  # no slot state file at all
])
def test_slot_not_ours_signals_nothing(tmp_path, state_kw):
    w = World(tmp_path, var_folders=False, uchg=False)
    if state_kw is None:
        w.facts.slot_state.unlink()
    else:
        slot_state(w.L, w.facts.slot, **state_kw)
    rec = Recorder()
    ctl = Control()
    pre = sorted(p.name for p in w.S.rglob("*"))
    assert end_session(w.facts, w.seams(ctl, **rec.kw()), record_state=REVOKED) == HELD
    assert rec.calls == []                                   # nothing frozen, killed or swept
    assert ctl.failed_step == "slot-not-ours" and [h[0] for h in ctl.history] == [HELD]
    assert sorted(p.name for p in w.S.rglob("*")) == pre and not w.facts.retained.exists()


def test_forged_revoked_for_a_retained_session_reaches_no_new_tenant(tmp_path):
    """L2 M4's scenario: S1 RETAINED, its slot re-leased to S2, a forged REVOKED for S1 fed to the reaper.
    No signal; CONTROL refuses the HELD edge from RETAINED, and that refusal is no step failure."""
    w = World(tmp_path, var_folders=False, uchg=False)
    assert end_session(w.facts, w.seams(Control()), record_state=REVOKED) == RETAINED
    slot_state(w.L, w.facts.slot, session=OTHER, lease_seq=LEASE_SEQ + 1)
    rec = Recorder()
    ctl = Control(RETAINED)
    with pytest.raises(CasNotSent) as e:
        end_session(w.facts, w.seams(ctl, **rec.kw()), record_state=REVOKED)
    assert rec.calls == [] and isinstance(e.value.cause, Refused) and e.value.cause.name == "edge-not-allowed"
    assert ctl.state == RETAINED and ctl.history == []


# --- M5: a CAS CONTROL did not take is never a step failure ----------------------------------------------------


class Clock:
    """A clock that only the backoff's sleep moves."""

    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


def test_locked_cas_is_retried_never_held(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    ctl = Control()
    ctl.refuse = ["locked", "locked", "stale-generation"]
    clock = Clock()
    assert end_session(w.facts, w.seams(ctl, clock=clock, sleep=clock.sleep), record_state=REVOKED) == RETAINED
    assert HELD not in [h[0] for h in ctl.history]
    assert [h[0] for h in ctl.history] == [FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED]
    assert clock.sleeps == [0.01, 0.02]                      # two backoffs; stale-generation retried at once


@pytest.mark.parametrize("refusal", ["locked", "stale-generation", "edge-not-allowed", OSError])
def test_a_cas_never_taken_raises_and_records_nothing(tmp_path, refusal):
    w = World(tmp_path, var_folders=False, uchg=False)
    ctl = Control()
    clock = Clock()

    def cas(target, fields):
        ctl.sent.append(target)
        if refusal is OSError:
            raise ConnectionRefusedError(61, "CONTROL is down")
        raise Refused(refusal)
    seams = w.seams(ctl, cas=cas, clock=clock, sleep=clock.sleep)
    with pytest.raises(CasNotSent) as e:
        end_session(w.facts, seams, record_state=REVOKED)
    assert e.value.target == FROZEN and HELD not in ctl.sent          # never misnamed as a "freeze" failure
    assert ctl.state == REVOKED and ctl.history == []
    assert clock.t <= end_mod.CAS_RETRY_SECONDS
    if refusal == "stale-generation":
        assert ctl.sent == [FROZEN, FROZEN]                          # retried once
    if refusal == "edge-not-allowed":
        assert ctl.sent == [FROZEN]


def test_a_refused_finishing_cas_leaves_the_journal_for_the_next_pass(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    ctl = Control()
    real = ctl.cas

    def cas(target, fields):
        if target == RETAINED and not getattr(cas, "done", False):
            cas.done = True
            raise Refused("lease-dead")
        return real(target, fields)
    with pytest.raises(CasNotSent):
        end_session(w.facts, w.seams(ctl, cas=cas), record_state=REVOKED)
    assert ctl.state == CREDENTIAL_PARKED and read_root_intent(w.facts.intent, owner_ok=mine).phase == "commit"
    reconcile_session(w.facts, w.seams(ctl))
    assert ctl.state == RETAINED and not w.facts.intent.exists()


# --- M7: one END per session ---------------------------------------------------------------------------------


def test_a_second_end_is_refused_end_busy(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    fd = os.open(w.facts.lock, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rec = Recorder()
        ctl = Control()
        for call in (lambda s: end_session(w.facts, s, record_state=REVOKED),
                     lambda s: reconcile_session(w.facts, s)):
            with pytest.raises(Refused) as e:
                call(w.seams(ctl, **rec.kw()))
            assert e.value.name == "end-busy"
        assert rec.calls == [] and ctl.history == []
    finally:
        os.close(fd)
    assert end_session(w.facts, w.seams(Control()), record_state=REVOKED) == RETAINED


# --- L1-F1: park only a passthrough session's own credential, never over keep/ ---------------------------------


def test_brokered_session_retains_a_credential_named_file_as_work(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    facts = replace(w.facts, auth="brokered")
    (w.facts.keep / ".credentials.json").write_bytes(b"THE PARKED LOGIN")
    ctl = Control()
    assert end_session(facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert (w.facts.keep / ".credentials.json").read_bytes() == b"THE PARKED LOGIN"
    assert (facts.tree / "config" / "claude" / ".credentials.json").read_bytes() == CRED   # retained work
    assert b"config/claude/.credentials.json" in manifest_by_path(w)


def test_passthrough_parks_only_its_own_vendor(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    facts = replace(w.facts, vendor="codex")
    (w.S / "config" / "codex" / "auth.json").write_bytes(b"CODEX LOGIN")
    (w.facts.keep / ".credentials.json").write_bytes(b"THE PARKED CLAUDE LOGIN")
    ctl = Control()
    assert end_session(facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert (w.facts.keep / "auth.json").read_bytes() == b"CODEX LOGIN"
    assert (w.facts.keep / ".credentials.json").read_bytes() == b"THE PARKED CLAUDE LOGIN"
    assert (facts.tree / "config" / "claude" / ".credentials.json").read_bytes() == CRED


def test_park_never_replaces_a_keep_entry(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    (w.facts.keep / ".credentials.json").write_bytes(b"THE PARKED LOGIN")
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "park" and any("keep-occupied" in n for n in w.notes)
    assert (w.facts.keep / ".credentials.json").read_bytes() == b"THE PARKED LOGIN"
    assert (w.S / "config" / "claude" / ".credentials.json").read_bytes() == CRED


# --- L1-F4: leaving HELD clears failed_step and held_attempts --------------------------------------------------


def test_recovery_from_held_clears_failed_step_and_attempts(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    ctl = Control()
    real = end_mod._tree_step
    n = {"calls": 0}

    def fail_twice(*a, **k):
        n["calls"] += 1
        if n["calls"] <= 2:
            raise OSError(5, "tree fails")
        return real(*a, **k)
    monkeypatch.setattr(end_mod, "_tree_step", fail_twice)
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert (ctl.failed_step, ctl.fields["held_attempts"]) == ("tree", 1)
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="tree",
                       held_attempts=ctl.fields["held_attempts"]) == HELD
    assert (ctl.failed_step, ctl.fields["held_attempts"]) == ("tree", 2)
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="tree",
                       held_attempts=ctl.fields["held_attempts"]) == RETAINED
    assert ctl.state == RETAINED
    assert "failed_step" not in ctl.fields and "held_attempts" not in ctl.fields
    assert set(ctl.fields) == {"manifest"}


# --- LOWs fixed as one-line bounds -----------------------------------------------------------------------------


def test_a_mount_inside_the_tree_refuses_tree_mount(tmp_path, monkeypatch):
    """L2 L12: an entry on another filesystem than its directory is refused before root walks into it."""
    (tmp_path / "t" / "mnt").mkdir(parents=True)
    (tmp_path / "t" / "mnt" / "x").write_text("x")
    real = os.stat

    def stat_(p, *a, **k):
        st = real(p, *a, **k)
        if p == b"mnt":
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev + 1, *tuple(st)[3:10]))
        return st
    monkeypatch.setattr(os, "stat", stat_)
    with pytest.raises(Refused) as e:
        list(end_mod.walk(tmp_path / "t"))
    assert e.value.name == "tree-mount"
