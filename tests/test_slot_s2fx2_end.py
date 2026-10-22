"""S1 session 2 fix round 3 (lane FX2), END (CONTRACT §7.3-7.5): the round-2 L3 findings on end.py (g3-O3, g3-L2,
g3-N1, g3-N2, g3-L3, g3-L4), each with a test built from the finding's scenario. Same world as test_slot_s2c_end
(real files, the real Control.cas, fake freeze / kill / proof, an in-process session-uid reader)."""
from __future__ import annotations

import errno
import os
from dataclasses import replace

import pytest

from levain.slot import Refused
from levain.slot import end as end_mod
from levain.slot.end import (CREDENTIAL_PARKED, DEAD, HELD, PROVEN_PHASES, RETAINED, REVOKED, end_lock, end_session,
                             reconcile_session, slot_lock)
from levain.slot.root_intent import END_INTENTS, INTENT_PHASES, read_root_intent
from tests.test_slot_s2c_end import UID, Control, Crash, World, mine, slot_state, thread_run_as
from tests.test_slot_s2g_end import Recorder, _outside
from tests.test_slot_s2k3_end import OTHER, frame_run_as, lock_free

CRED = ("config", "claude", ".credentials.json")


def _crash_at(w: World, ctl: Control, point: str) -> None:
    def fault(p):
        if p == point:
            raise Crash(p)
    with pytest.raises(Crash):
        end_session(w.facts, w.seams(ctl, fault=fault), record_state=REVOKED)


# --- g3-O3 / g3-L2: every journal that will touch live/ is finished only after fresh proofs --------------------


def test_the_gate_names_every_phase_but_commit():
    assert PROVEN_PHASES == {"retain": ("init", "manifest", "meta", "object", "tree"), "park": ("move",)}
    assert set(PROVEN_PHASES) == set(END_INTENTS)
    for intent, phases in PROVEN_PHASES.items():
        assert phases + ("commit",) == INTENT_PHASES[intent]


def test_reconcile_of_an_init_journal_proves_again_and_holds_on_a_live_process(tmp_path):
    """codex r2 O3: a crash leaves ``phase: init``; the reconciler would run manifest and tree from it."""
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _crash_at(w, ctl, "retain:after:init")
    assert ctl.state == CREDENTIAL_PARKED and read_root_intent(w.facts.intent, owner_ok=mine).phase == "init"
    rec = Recorder(no_process=lambda uid: False)                 # a process of the slot uid appeared
    with pytest.raises(Refused) as e:
        reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert e.value.name == "kill-unproven"
    assert ctl.state == HELD and ctl.failed_step == "kill"
    assert read_root_intent(w.facts.intent, owner_ok=mine).phase == "init"   # nothing reconciled
    assert not (w.facts.retained / "manifest.jsonl").exists() and not w.facts.tree.exists() and w.S.is_dir()
    reconcile_session(w.facts, w.seams(ctl))                     # proven: it finishes
    assert ctl.state == RETAINED and not w.facts.intent.exists() and w.facts.tree.is_dir()


def test_reconcile_of_an_init_journal_when_the_slot_is_not_ours_signals_nothing(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _crash_at(w, ctl, "retain:after:init")
    slot_state(w.L, w.facts.slot, session=OTHER)
    rec = Recorder()
    with pytest.raises(Refused) as e:
        reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert e.value.name == "slot-not-ours" and rec.calls == [] and ctl.failed_step == "slot-not-ours"
    assert not w.facts.tree.exists() and w.S.is_dir()


def test_reconcile_of_a_park_move_journal_proves_again_and_holds_on_a_live_process(tmp_path):
    """comp r2 L2: park's ``move`` renames the credential out of the session-owned live/."""
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _crash_at(w, ctl, "park:after:move")
    assert ctl.state == DEAD and read_root_intent(w.facts.intent, owner_ok=mine).intent == "park"
    cred = w.S.joinpath(*CRED)
    assert cred.exists()
    rec = Recorder(no_process=lambda uid: False)
    with pytest.raises(Refused) as e:
        reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert e.value.name == "kill-unproven" and ctl.state == HELD and ctl.failed_step == "kill"
    assert cred.exists() and read_root_intent(w.facts.intent, owner_ok=mine).phase == "move"   # never moved
    reconcile_session(w.facts, w.seams(ctl))
    assert ctl.state == CREDENTIAL_PARKED and not cred.exists() and not w.facts.intent.exists()


def test_reconcile_of_a_park_move_journal_when_the_slot_is_not_ours_signals_nothing(tmp_path):
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _crash_at(w, ctl, "park:after:move")
    slot_state(w.L, w.facts.slot, session=OTHER)
    rec = Recorder()
    with pytest.raises(Refused) as e:
        reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert e.value.name == "slot-not-ours" and rec.calls == [] and w.S.joinpath(*CRED).exists()


def test_a_park_commit_journal_finishes_without_the_slot(tmp_path):
    """The commit carve-out holds for park too: its step syncs keep/ and CASes, live/ is done."""
    w = World(tmp_path, uchg=False)
    ctl = Control()
    _crash_at(w, ctl, "park:after:cas")
    assert ctl.state == CREDENTIAL_PARKED and read_root_intent(w.facts.intent, owner_ok=mine).phase == "commit"
    slot_state(w.L, w.facts.slot, session=OTHER)
    rec = Recorder()
    reconcile_session(w.facts, w.seams(ctl, **rec.kw()))
    assert not w.facts.intent.exists() and rec.calls == []


# --- g3-N1: the already-placed retry fsyncs the entry's dir before the staging copies go -----------------------


def test_a_placed_entry_whose_dir_fsync_failed_is_fsynced_before_the_staging_unlink(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    _outside(w, tmp_path, b"z" * 32)
    real_rename, real_fsync, real_unlink = os.rename, os.fsync, os.unlink
    armed: list = []

    def rename(src, dst, *a, **k):
        real_rename(src, dst, *a, **k)
        if isinstance(src, bytes) and src.startswith(b".levain-link.") and not armed:
            armed.append(True)

    def failing_fsync(fd):
        if armed == [True]:
            armed.append(False)
            raise OSError(errno.EIO, "the dir fsync after the rename fails")
        real_fsync(fd)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "fsync", failing_fsync)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=thread_run_as()), record_state=REVOKED) == HELD
    assert ctl.failed_step == "tree"
    placed_dir = w.facts.tree / "repo"
    linkcopy = w.facts.retained / end_mod.LINKCOPY
    assert os.lstat(placed_dir / "hl-out").st_ino in {os.lstat(p).st_ino for p in linkcopy.iterdir()}
    dir_ino = {os.lstat(placed_dir).st_ino: "placed", os.lstat(linkcopy).st_ino: "linkcopy"}
    events: list = []

    def fsync(fd):
        events.append(("fsync", dir_ino.get(os.fstat(fd).st_ino)))
        real_fsync(fd)

    def unlink(path, *a, dir_fd=None, **k):
        if dir_fd is not None:
            events.append(("unlink", dir_ino.get(os.fstat(dir_fd).st_ino)))
        return real_unlink(path, *a, dir_fd=dir_fd, **k)
    monkeypatch.setattr(os, "rename", real_rename)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "unlink", unlink)
    assert end_session(w.facts, w.seams(ctl), record_state=HELD, failed_step="tree") == RETAINED
    first_staging_unlink = events.index(("unlink", "linkcopy"))
    assert ("fsync", "placed") in events[:first_staging_unlink]


# --- g3-N2: a hash-only UNOWNED file the session uid cannot read refuses, never a size-0 record --------------------


def test_an_unowned_file_the_session_uid_cannot_read_holds_the_manifest(tmp_path):
    w = World(tmp_path, var_folders=False, uchg=False)
    x = UID + 4242                                           # the session's uid owns nothing: every file UNOWNED
    w.facts = replace(w.facts, slot_uid=x, slot_gid=x)
    answer = end_mod.LINK_UNREADABLE.encode("ascii")
    ctl = Control()
    got = end_session(w.facts, w.seams(ctl, run_as=frame_run_as(b"R" + bytes([len(answer)]) + answer)),
                      record_state=REVOKED)
    assert got == HELD and ctl.failed_step == "manifest" and any("unowned-unreadable" in n for n in w.notes)
    assert not (w.facts.retained / "manifest.jsonl").exists() and not w.facts.tree.exists() and w.S.is_dir()


# --- g3-L3: the failed rename's temp cleanup never replaces the rename's errno ------------------------------------


def test_a_refused_rename_whose_temp_is_already_gone_is_still_noted(tmp_path, monkeypatch):
    w = World(tmp_path, var_folders=False, uchg=False)
    outside = _outside(w, tmp_path, b"q" * 8)
    real_rename = os.rename

    def rename(src, dst, *a, src_dir_fd=None, dst_dir_fd=None, **k):
        if isinstance(src, bytes) and src.startswith(b".levain-link."):
            os.unlink(src, dir_fd=src_dir_fd)                # the temp is gone: END's cleanup unlink sees ENOENT
            raise OSError(errno.EPERM, "rename refused")
        return real_rename(src, dst, *a, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd, **k)
    monkeypatch.setattr(os, "rename", rename)
    ctl = Control()
    assert end_session(w.facts, w.seams(ctl, run_as=thread_run_as()), record_state=REVOKED) == RETAINED, w.notes
    assert any("not replaced by its copy" in n for n in w.notes)
    assert os.lstat(w.facts.tree / "repo" / "hl-out").st_ino == os.lstat(outside).st_ino


# --- g3-L4: no forked child keeps END's locks ---------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(os, "fork"), reason="fork")
def test_a_forked_child_holds_neither_the_slot_lock_nor_the_end_lock(tmp_path):
    w = World(tmp_path, uchg=False)
    seams = w.seams(Control())
    with slot_lock(w.facts, seams) as locked, end_lock(w.facts, seams):
        assert locked
        fds = sorted(end_mod._LOCK_FDS)
        assert len(fds) == 2
        pid = os.fork()
        if pid == 0:   # the child: 0 when every lock fd is closed, else 1
            code = 0
            for fd in fds:
                try:
                    os.fstat(fd)
                    code = 1
                except OSError:
                    pass
            os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        assert not lock_free(w.facts.slot_lock) and not lock_free(w.facts.lock)   # the parent's locks hold
    assert end_mod._LOCK_FDS == set()
    assert lock_free(w.facts.slot_lock) and lock_free(w.facts.lock)
