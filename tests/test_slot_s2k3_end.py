"""S1 session 2 fix round 2 (lane K3), END (CONTRACT §7.3-7.5): the round-1 L3 findings on end.py, each with a
test that failed before its fix. Same world as test_slot_s2c_end (real files, the real Control.cas, fake
freeze / kill / proof, an in-process session-uid reader)."""
from __future__ import annotations

import base64
import errno
import fcntl
import os
import stat
import struct
import threading
from dataclasses import replace
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import end as end_mod
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot import stage
from levain.slot.end import (HELD, RETAINED, REVOKED, Caps, CasNotSent, EndFacts, Reserve, end_session,
                             reconcile_session)
from levain.slot.ids import SlotId
from levain.slot.root_intent import read_root_intent
from tests.test_slot_s2c_end import (DARWIN, GID, LEASE_SEQ, NOW, SESSION, UID, Control, World, _set_xattr, mine,
                                     slot_state, thread_run_as)
from tests.test_slot_s2g_end import Recorder, _outside

OTHER = "s-fedcba9876543210"


def lock_free(path: Path) -> bool:
    """Whether a fresh descriptor can take the flock at ``path`` now (flocks are per open file description, so
    this sees a lock held by this same process)."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def meta_paths(w: World) -> set:
    return {base64.b64decode(m["path_b64"])
            for m in map(canon.parse, (w.facts.retained / "meta.jsonl").read_bytes().splitlines())}


def manifest_by_path(w: World) -> dict:
    return {base64.b64decode(m["path_b64"]): m for m in w.manifest_file()}


class _Answer:
    """A finished fake reader with an exit status."""

    def __init__(self, code=0) -> None:
        self.code = code

    def kill(self) -> None:
        pass

    def wait(self):
        return self.code


def frame_run_as(frame: bytes, code=0):
    """A session-uid reader that writes ``frame`` and exits ``code``."""
    def run_as(uid, gid, fn, wfd, keep=()):
        os.write(wfd, frame)
        os.close(wfd)
        return _Answer(code)
    return run_as


# --- codex r1 1: a RETAINED record is believed only with root's whole retained object --------------------------


def test_forged_retained_for_a_live_session_runs_the_proofs_and_never_retains_or_returns(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control(state=RETAINED)                      # CONTROL forged it; root holds no retained object
    rec = Recorder()
    with pytest.raises(CasNotSent):                    # CONTROL refuses RETAINED -> FROZEN: teardown never scrubs
        end_session(w.facts, w.seams(ctl, **rec.kw()), record_state=RETAINED)
    assert ("freeze", UID) in rec.calls                # END ran from its proofs, as for REVOKED
    assert w.S.is_dir() and not w.facts.tree.exists()
    assert ctl.state == RETAINED and ctl.history == []


def test_whole_retained_object_returns_at_once_with_no_proof(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    assert end_mod.retained_whole(w.facts, w.seams(ctl, owner_ok=mine))
    rec = Recorder()
    assert end_session(w.facts, w.seams(ctl, **rec.kw()), record_state=RETAINED) == RETAINED
    assert rec.calls == []


@pytest.mark.parametrize("damage", ["no-retained-at", "no-object", "other-session", "bad-stamp", "not-root"])
def test_a_retained_object_that_is_not_whole_is_not_believed(tmp_path, damage):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == RETAINED
    r = w.facts.retained
    owner_ok = mine
    if damage == "no-retained-at":
        (r / "retained_at").unlink()
    elif damage == "no-object":
        (r / "object.json").unlink()
    elif damage == "other-session":
        obj = canon.parse((r / "object.json").read_bytes())
        (r / "object.json").write_bytes(canon.canonical_bytes({**obj, "session": OTHER}))
    elif damage == "bad-stamp":
        (r / "retained_at").write_bytes(b"yesterday\n")
    else:
        owner_ok = lambda st: st.st_ino != os.lstat(r / "object.json").st_ino and mine(st)   # noqa: E731
    assert not end_mod.retained_whole(w.facts, w.seams(ctl, owner_ok=owner_ok))
    rec = Recorder()
    with pytest.raises(CasNotSent):
        end_session(w.facts, w.seams(ctl, owner_ok=owner_ok, **rec.kw()), record_state=RETAINED)
    assert ("freeze", UID) in rec.calls


# --- codex r1 2: the slot lock across the check, the proofs and every act ---------------------------------------


def test_the_slot_lock_is_held_from_the_slot_check_through_the_tree(tmp_path):
    w = World(tmp_path, uchg=False)
    held: list = []
    lock = w.facts.slot_lock

    def probe(tag, answer=True):
        held.append((tag, not lock_free(lock)))
        return answer

    def slot_state_read(path):
        held.append(("check", not lock_free(lock)))
        return end_mod.read_slot_state(path)

    def strip(e, note):
        if not held or held[-1][0] != "strip":
            held.append(("strip", not lock_free(lock)))
        end_mod.default_strip()(e, note)
    ctl = Control()
    seams = w.seams(ctl, slot_state=slot_state_read, freeze=lambda u, b: probe("freeze"),
                    kill=lambda u, b: probe("kill"), no_process=lambda u: probe("no_process"), strip=strip)
    assert end_session(w.facts, seams, record_state=REVOKED) == RETAINED
    assert [t for t, _ in held] == ["check", "freeze", "kill", "no_process", "strip"]
    assert all(h for _, h in held), held
    assert lock_free(lock)                               # released when END returns


def test_end_waits_for_the_slot_lock_then_refuses_slot_locked_before_its_own_lock(tmp_path, monkeypatch):
    w = World(tmp_path, uchg=False)
    w.facts.slot_lock.write_bytes(b"")
    fd = os.open(w.facts.slot_lock, os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    took: list = []
    real_end_lock = end_mod.end_lock
    monkeypatch.setattr(end_mod, "end_lock", lambda f, s: took.append(1) or real_end_lock(f, s))
    t = iter(range(0, 1000, 3))
    ctl = Control()
    rec = Recorder()
    try:
        with pytest.raises(Refused) as e:
            end_session(w.facts, w.seams(ctl, clock=lambda: float(next(t)), **rec.kw()), record_state=REVOKED)
    finally:
        os.close(fd)
    assert e.value.name == "slot-locked"
    assert took == [] and rec.calls == [] and ctl.sent == []     # slot lock first: never the END lock, no signal


def test_a_prepare_during_end_skips_the_slot_end_holds(tmp_path):
    """runtime_helper's lease_slot takes the pool lock and only TRIES each slot lock, so a prepare while END
    holds e0's lock passes e0 over (it would otherwise be the pick: reusable, lowest) and never waits on END."""
    from tests.test_slot_s2f2_runtime_helper import World as HelperWorld
    hw = HelperWorld(tmp_path, egress=2)
    store = H.Store(hw.seams)
    facts = EndFacts(SESSION, SlotId("egress", 0), UID, GID, hw.L, None, lease_seq=LEASE_SEQ, vendor="claude",
                     auth="brokered")
    picked: list = []

    def slot_state_read(path):
        leased = H.lease_slot(store, slot_class="egress", kind="interactive", vendor="claude", auth="brokered",
                              named=None, ticketed=False, session=OTHER)
        leased.release()
        picked.append(leased.slot)
        return end_mod.read_slot_state(path)
    ctl = Control()
    rec = Recorder()
    seams = end_mod.EndSeams(cas=ctl.cas, owner_ok=mine, slot_state=slot_state_read, now=lambda: NOW,
                             sleep=lambda s: None, note=lambda m: None, **rec.kw())
    assert end_session(facts, seams, record_state=REVOKED) == HELD     # e0 is reusable: not ours, nothing signalled
    assert picked == [SlotId("egress", 1)]
    assert ctl.failed_step == "slot-not-ours" and rec.calls == []


def test_no_slot_dir_is_slot_not_ours_with_nothing_signalled(tmp_path):
    w = World(tmp_path, uchg=False)
    facts = replace(w.facts, slot=SlotId("egress", 6))           # a slot with no directory
    ctl = Control()
    rec = Recorder()
    assert end_session(facts, w.seams(ctl, **rec.kw()), record_state=REVOKED) == HELD
    assert ctl.failed_step == "slot-not-ours" and rec.calls == []


# --- comp r1 3: reconciling a journal at manifest or later re-runs the proofs ------------------------------------


def _held_at_tree(w: World, ctl: Control) -> None:
    def strip(e, note):
        raise OSError(errno.EIO, "strip fails")
    assert end_session(w.facts, w.seams(ctl, strip=strip), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree" and read_root_intent(w.facts.intent, owner_ok=mine).phase == "tree"


def test_reconcile_at_tree_proves_again_and_holds_on_a_live_process(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _held_at_tree(w, ctl)
    attempts = ctl.fields["held_attempts"]
    rec = Recorder(no_process=lambda uid: False)                 # a process of the slot uid appeared
    held_lock: list = []
    kw = rec.kw()
    kw["freeze"] = lambda uid, b: held_lock.append(not lock_free(w.facts.slot_lock)) or True
    with pytest.raises(Refused) as e:
        reconcile_session(w.facts, w.seams(ctl, **kw), held_attempts=attempts)
    assert e.value.name == "kill-unproven"
    assert held_lock == [True]                                   # the proofs run under the slot lock
    assert ctl.state == HELD and ctl.failed_step == "kill" and ctl.fields["held_attempts"] == attempts + 1
    assert read_root_intent(w.facts.intent, owner_ok=mine).phase == "tree"   # nothing reconciled
    reconcile_session(w.facts, w.seams(ctl))                     # proven: it finishes
    assert ctl.state == RETAINED


def test_reconcile_at_tree_when_the_slot_is_not_ours_signals_nothing(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _held_at_tree(w, ctl)
    slot_state(w.L, w.facts.slot, session=OTHER)
    rec = Recorder()
    with pytest.raises(Refused) as e:
        reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert e.value.name == "slot-not-ours" and rec.calls == []
    assert ctl.failed_step == "slot-not-ours"


def test_a_commit_journal_finishes_without_the_slot(tmp_path):
    """The crash between the RETAINED CAS and the journal delete: SCRUBBED may have released the slot since,
    and finishing the commit (no tree access) must not need it."""
    from tests.test_slot_s2c_end import Crash
    w = World(tmp_path, uchg=False)
    ctl = Control()

    def fault(point):
        if point == "retain:after:cas":
            raise Crash(point)
    with pytest.raises(Crash):
        end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)
    assert ctl.state == RETAINED and read_root_intent(w.facts.intent, owner_ok=mine).phase == "commit"
    slot_state(w.L, w.facts.slot, state="reusable")
    rec = Recorder()
    reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert not w.facts.intent.exists() and rec.calls == [] and ctl.state == RETAINED


# --- codex r1 4: inodes no session uid owns are never read by root -----------------------------------------------


def test_files_no_session_uid_owns_are_read_by_the_session_uid_and_their_metadata_never(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    x = UID + 4242                                           # the session's uid owns nothing in this tree
    w.facts = replace(w.facts, slot_uid=x, slot_gid=x)
    pre = w.pre_manifest()
    regular = {(st.st_dev, st.st_ino) for st in (os.lstat(p) for p in w.S.rglob("*")) if stat.S_ISREG(st.st_mode)}
    opened: list = []
    real_open = os.open

    def traced_open(*a, **k):
        fd = real_open(*a, **k)
        st = os.fstat(fd)
        if threading.current_thread() is threading.main_thread() and (st.st_dev, st.st_ino) in regular:
            opened.append(a[0])
        return fd
    monkeypatch.setattr(os, "open", traced_open)
    calls: list = []
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=thread_run_as(calls)), record_state=REVOKED) == RETAINED, w.notes
    monkeypatch.setattr(os, "open", real_open)
    assert opened == []                                      # root opened none of them
    assert {c[:2] for c in calls} == {(x, x)} and len(calls) == len(regular) - 1    # the credential is parked
    assert w.manifest_file() == pre                          # what the session uid read is what is recorded
    assert meta_paths(w) == set()                            # README's xattr: never read by root
    assert not (w.facts.retained / end_mod.CLASSES).exists()   # the working record is gone at commit


def test_the_meta_step_reads_the_stored_classification_never_a_recompute(tmp_path):
    """An outside link removed between the manifest and meta steps: the inode is still the manifest step's
    OUTSIDE, so root never reads its xattrs (a recompute would have handed it to root)."""
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = _outside(w, tmp_path)
    if not _set_xattr(outside, "user.levain.outside", b"the outside owner's"):
        pytest.skip("no user xattrs here")

    def fault(point):
        if point == "retain:manifest:written" and outside.exists():
            assert (w.facts.retained / end_mod.CLASSES).is_file()
            outside.unlink()
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, fault=fault, run_as=thread_run_as()), record_state=REVOKED) == RETAINED
    assert b"repo/hl-out" not in meta_paths(w) and b"repo/private/hl-out-2" not in meta_paths(w)


def test_read_classes_refuses_a_missing_record(tmp_path):
    w = World(tmp_path, uchg=False)
    with pytest.raises(Refused) as e:
        end_mod.read_classes(w.facts)
    assert e.value.name == "classes-missing"


# --- comp r1 4: a shared inode of any type ---------------------------------------------------------------------


def test_a_fifo_linked_outside_the_tree_is_shared_and_refused(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    fifo = w.S / "repo" / "pipe"
    os.mkfifo(fifo)
    os.link(fifo, tmp_path / "pipe-outside")
    links = end_mod.foreign_links(w.S)
    key = (os.lstat(fifo).st_dev, os.lstat(fifo).st_ino)
    assert key in links and links[key].regular is False
    with pytest.raises(Refused) as e:
        end_mod.classify(w.S, None, w.facts.uids)
    assert e.value.name == "tree-shared-special"
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "manifest" and any("tree-shared-special" in n for n in w.notes)
    assert fifo.exists() and not w.facts.tree.exists()


# --- codex r1 5: the tree's metadata is flushed before the journal leaves `tree` ---------------------------------


def test_sync_runs_after_the_last_chown_and_before_the_commit_phase(tmp_path):
    w = World(tmp_path, uchg=False)
    events: list = []

    def chown(dirfd, name, uid, gid):
        events.append("chown")
        end_mod.os_chown(dirfd, name, uid, gid)
    ctl = Control()
    seams = w.seams(ctl, chown=chown, sync=lambda p: events.append(("sync", p)),
                    fault=lambda point: events.append(point) if point == "retain:before:commit" else None)
    assert end_session(w.facts, seams, record_state=REVOKED) == RETAINED
    i = events.index(("sync", w.facts.tree))
    assert events[i - 1] == "chown" and "chown" not in events[i:] and events[i + 1] == "retain:before:commit"


def test_a_failed_sync_holds_the_tree_phase(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control()

    def sync(p):
        raise OSError(errno.EIO, "flush failed")
    assert end_session(w.facts, w.seams(ctl, sync=sync), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree"


def test_the_real_sync_runs(tmp_path):
    end_mod.sync_tree(tmp_path)


# --- codex r1 6: a redo whose rename already happened still fsyncs both parents ----------------------------------


def test_tree_redo_after_the_rename_fsyncs_both_parents(tmp_path, monkeypatch):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    real = end_mod.fsync_dir
    synced: list = []
    failed: list = []

    def fsync_dir(p):
        if Path(p) == w.facts.live.parent and not w.facts.live.exists() and not failed:
            failed.append(p)
            raise OSError(errno.ENOSPC, "no space for the parent fsync")
        synced.append(Path(p))
        real(p)
    monkeypatch.setattr(end_mod, "fsync_dir", fsync_dir)
    assert end_session(w.facts, w.seams(ctl), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree" and w.facts.tree.is_dir() and not w.facts.live.exists()
    synced.clear()
    seams = w.seams(ctl, fault=lambda pt: synced.append(pt) if pt == "retain:tree:renamed" else None)
    assert end_session(w.facts, seams, record_state=HELD, failed_step="tree") == RETAINED
    cut = synced.index("retain:tree:renamed")
    assert w.facts.live.parent in synced[:cut] and w.facts.tree.parent in synced[:cut]


def test_tree_redo_after_the_var_folders_rename_fsyncs_both_parents(tmp_path, monkeypatch):
    w = World(tmp_path, uchg=False)
    ctl = Control()

    def fault(point):
        if point == "retain:tree:var-folders" and not getattr(fault, "fired", False):
            fault.fired = True
            raise OSError(errno.EIO, "after the var-folders rename")
    assert end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED) == HELD
    synced: list = []
    real = end_mod.fsync_dir
    monkeypatch.setattr(end_mod, "fsync_dir", lambda p: synced.append(Path(p)) or real(p))
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="tree") == RETAINED
    assert w.vf.parent in synced and w.facts.tree in synced


def test_park_redo_after_the_rename_fsyncs_both_dirs(tmp_path, monkeypatch):
    w = World(tmp_path, uchg=False)
    cred_dir = w.S / "config" / "claude"
    os.rename(cred_dir / ".credentials.json", w.facts.keep / ".credentials.json")   # renamed, never synced
    synced: list = []
    fsyncs: list = []
    real_dir, real_fsync = end_mod.fsync_dir, os.fsync
    monkeypatch.setattr(end_mod, "fsync_dir", lambda p: synced.append(Path(p)) or real_dir(p))
    monkeypatch.setattr(os, "fsync", lambda fd: fsyncs.append(os.fstat(fd).st_ino) or real_fsync(fd))
    end_mod.park_move(w.facts)
    assert w.facts.keep in synced and os.lstat(cred_dir).st_ino in fsyncs


# --- codex r1 7: link placement propagates what it must ---------------------------------------------------------


def test_a_failed_fsync_after_placing_a_copy_holds_the_tree_then_converges(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = _outside(w, tmp_path, b"z" * 32)
    real_rename, real_fsync = os.rename, os.fsync
    armed: list = []

    def rename(src, dst, *a, **k):
        real_rename(src, dst, *a, **k)
        if isinstance(src, bytes) and src.startswith(b".levain-link.") and not armed:
            armed.append(True)

    def fsync(fd):
        if armed == [True]:
            armed.append(False)
            raise OSError(errno.ENOSPC, "no space for the dir fsync")
        real_fsync(fd)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "fsync", fsync)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=thread_run_as()), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree" and any("ENOSPC" in n or "no space" in n for n in w.notes)
    assert read_root_intent(w.facts.intent, owner_ok=mine).phase == "tree"
    monkeypatch.setattr(os, "rename", real_rename)
    monkeypatch.setattr(os, "fsync", real_fsync)
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="tree") == RETAINED
    copy = w.facts.tree / "repo" / "hl-out"
    assert copy.read_bytes() == b"z" * 32 and os.lstat(copy).st_ino != os.lstat(outside).st_ino
    assert not [p for p in w.facts.tree.rglob(".levain-link.*")]


@pytest.mark.parametrize("code,held", [(errno.EIO, True), (errno.EPERM, False)])
def test_a_failed_rename_removes_its_temp_link(tmp_path, monkeypatch, code, held):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = _outside(w, tmp_path, b"q" * 8)
    real_rename = os.rename

    def rename(src, dst, *a, **k):
        if isinstance(src, bytes) and src.startswith(b".levain-link."):
            raise OSError(code, "rename refused")
        return real_rename(src, dst, *a, **k)
    monkeypatch.setattr(os, "rename", rename)
    ctl = Control()
    got = end_session(w.facts, w.seams(ctl, run_as=thread_run_as()), record_state=REVOKED)
    tree = w.facts.tree
    assert not [p for p in tree.rglob(".levain-link.*")]               # no temp link left in the tree
    if held:
        assert got == HELD and ctl.failed_step == "tree"
    else:                                                               # refused for good: noted, stays shared
        assert got == RETAINED and any("not replaced by its copy" in n for n in w.notes)
        assert os.lstat(tree / "repo" / "hl-out").st_ino == os.lstat(outside).st_ino


# --- codex r1 8: one link-copy budget across every reader --------------------------------------------------------


def test_the_link_copy_cap_is_one_budget_across_readers(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    outs = []
    for i in range(3):
        p = tmp_path / f"grow-{i}.txt"
        p.write_bytes(b"g")                                  # one byte each when END sums them
        os.link(p, w.S / "repo" / f"grow-{i}")
        outs.append(p)
    reads: list = []

    def run_as(uid, gid, fn, wfd, keep=()):
        outs[len(reads)].write_bytes(b"G" * 400)             # the outside holder grows it before its read
        reads.append(uid)
        return thread_run_as()(uid, gid, fn, wfd, keep)
    ctl = Control()
    caps = Caps(link_copy_bytes=1000)
    assert end_session(w.facts, w.seams(ctl, run_as=run_as), record_state=REVOKED, caps=caps) == HELD
    assert ctl.failed_step == "manifest_budget" and any("link-copy-cap" in n for n in w.notes)
    assert len(reads) == 3                                   # 400 + 400 staged, the third refused past 1000
    assert w.S.is_dir() and not w.facts.tree.exists()


# --- comp r1 1 + 2: what the reader answers ----------------------------------------------------------------------


def _one_outside(tmp_path, data=b"0123456789" * 10):
    w = World(tmp_path, var_folders=False, uchg=False)
    _outside(w, tmp_path, data)
    return w, data


@pytest.mark.parametrize("why", ["short", "no-end-mark", "exit-70", "exit-none"])
def test_a_reader_that_does_not_finish_cleanly_is_held_never_a_kept_file(tmp_path, why):
    w, data = _one_outside(tmp_path)
    head = b"S" + struct.pack(">Q", len(data))
    frame, code = {"short": (head + data[:len(data) // 2], 0), "no-end-mark": (head + data, 0),
                   "exit-70": (head + data + b"E", 70), "exit-none": (head + data + b"E", None)}[why]
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=frame_run_as(frame, code)), record_state=REVOKED) == HELD
    assert ctl.failed_step == "manifest" and any("link-reader-failed" in n for n in w.notes)
    assert w.S.is_dir() and not w.facts.tree.exists() and not (w.facts.retained / "manifest.jsonl").exists()


def test_a_whole_answer_with_its_end_mark_and_exit_0_is_kept(tmp_path):
    w, data = _one_outside(tmp_path)
    frame = b"S" + struct.pack(">Q", len(data)) + data + b"E"
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=frame_run_as(frame)), record_state=REVOKED) == RETAINED
    assert (w.facts.tree / "repo" / "hl-out").read_bytes() == data


@pytest.mark.parametrize("answer,unread", [(b"staging-open-failed", False), (b"link-unreadable", True),
                                           (b"staging-missing", True), (b"staging-not-regular", True)])
def test_only_cannot_read_answers_are_unread(tmp_path, answer, unread):
    w, _ = _one_outside(tmp_path)
    ctl = Control()
    got = end_session(w.facts, w.seams(ctl, run_as=frame_run_as(b"R" + bytes([len(answer)]) + answer)),
                      record_state=REVOKED)
    if unread:
        assert got == RETAINED and manifest_by_path(w)[b"repo/hl-out"]["digest"] == ""
    else:
        assert got == HELD and ctl.failed_step == "manifest"


_REAL_OPEN = os.open


def _reader_answer(dirfd: int, name: bytes, monkeypatch=None, open_errno=None) -> bytes:
    """What :func:`end.session_reader` writes for ``name``; ``open_errno``: every open of it fails so."""
    if open_errno is not None:
        def failing_open(path, *a, **k):
            if path == name:
                raise OSError(open_errno, os.strerror(open_errno))
            return _REAL_OPEN(path, *a, **k)
        monkeypatch.setattr(os, "open", failing_open)
    r, wfd = os.pipe()
    try:
        end_mod.session_reader(dirfd, name, 1 << 20, wfd)
    finally:
        os.close(wfd)
        if monkeypatch is not None:
            monkeypatch.undo()
    with os.fdopen(r, "rb") as fh:
        return fh.read()


def _frame(name: str) -> bytes:
    return b"R" + bytes([len(name)]) + name.encode()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode 000 file")
def test_session_reader_tells_eacces_from_other_errnos(tmp_path, monkeypatch):
    (tmp_path / "locked").write_bytes(b"no")
    os.chmod(tmp_path / "locked", 0)
    (tmp_path / "ok").write_bytes(b"yes")
    dfd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        assert _reader_answer(dfd, b"locked") == _frame(end_mod.LINK_UNREADABLE)
        assert _reader_answer(dfd, b"absent") == _frame("staging-missing")
        assert _reader_answer(dfd, b"ok") == b"S" + struct.pack(">Q", 3) + b"yes" + b"E"
        assert _reader_answer(dfd, b"ok", monkeypatch, errno.EMFILE) == _frame("staging-open-failed")
        assert _reader_answer(dfd, b"ok", monkeypatch, errno.EPERM) == _frame(end_mod.LINK_UNREADABLE)
    finally:
        os.close(dfd)
        os.chmod(tmp_path / "locked", 0o600)


# --- glm r1 LOWs -------------------------------------------------------------------------------------------------


def test_stream_jsonl_closes_its_fd_when_fdopen_fails(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise MemoryError("fdopen")
    before = len(os.listdir("/dev/fd"))
    monkeypatch.setattr(os, "fdopen", broken)
    with pytest.raises(MemoryError):
        end_mod._stream_jsonl(tmp_path / "x.jsonl", iter([{"a": 1}]), 1 << 20, "meta-byte-cap")
    monkeypatch.undo()
    assert len(os.listdir("/dev/fd")) == before and os.listdir(tmp_path) == []


def test_refill_fsyncs_the_directory_when_it_creates_the_reserve(tmp_path, monkeypatch):
    synced: list = []
    real = end_mod.fsync_dir
    monkeypatch.setattr(end_mod, "fsync_dir", lambda p: synced.append(Path(p)) or real(p))
    r = Reserve(tmp_path / "reserve", size=4096)
    assert r.refill(lambda: 1 << 40, floor=0) is True
    assert synced == [tmp_path]
    os.truncate(r.path, 100)
    synced.clear()
    assert r.refill(lambda: 1 << 40, floor=0) is True and synced == []     # it existed: nothing new to name
