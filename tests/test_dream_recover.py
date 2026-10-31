"""levain.dream.recover (D1-1b): the §3.3 table, every row from a state a real crash leaves.

Every test runs against a scratch anneal store under ``tmp_path``. The crash states are not hand-built where they can
be made: the kill -9 case runs a real fire in a subprocess and kills it mid-compose; the "committed before the rename"
case makes anneal's own phase-3 rename fail (``Path.replace`` raising a ``BaseException``), which leaves the wrap
committed, ``progress.json`` at ``saving`` and the new text in anneal's ``.md.tmp``, exactly as a kill there does.
"""
from __future__ import annotations

import contextlib
import json
import os
import pathlib
import signal
import subprocess
import sys
import textwrap
import time
import uuid
from pathlib import Path

import pytest
from anneal_memory import FLOW_SCHEMA, Store
from anneal_memory.continuity import prepare_wrap

from levain.dream import executor as ex
from levain.dream import recover as rc
from levain.dream import snapshot as sn
from tests.test_dream_snapshot import _VALID, Fake, add_episode, dream, occ, store_state, text_n

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def scratch(tmp_path):
    d = tmp_path / "store"
    d.mkdir()
    with Store(str(d / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
    return d / "memory.db"


class Die(BaseException):
    """What a kill between anneal's commit and its rename leaves behind (a BaseException nothing catches)."""


def crashed_before_rename(db: Path, name: str, text: str, monkeypatch) -> None:
    real = pathlib.Path.replace

    def bad(self, target):
        if str(self).endswith(".md.tmp") and str(self).startswith(str(db.parent) + "/memory"):
            raise Die("killed before the rename")
        return real(self, target)
    monkeypatch.setattr(pathlib.Path, "replace", bad)
    on_snap, on_saved = sn.hooks()
    with pytest.raises(Die):
        ex.consolidate_store(db, mind=Fake(text), occurrence_dir=occ(db, name), max_seconds=3000,
                             on_snapshot=on_snap, on_saved=on_saved)
    monkeypatch.undo()


def prog(db: Path, name: str) -> dict:
    return json.loads((occ(db, name) / "progress.json").read_text())


def set_prog(db: Path, name: str, **kw) -> None:
    p = prog(db, name)
    p.update(kw)
    (occ(db, name) / "progress.json").write_text(json.dumps(p))


# -------------------------------------------------------------------------- committed, not recorded (the wrap-id oracle)

def test_a_commit_that_landed_but_was_never_recorded_is_proven_by_the_wrap_hash_and_recorded(scratch):
    dream(scratch, "o1", _VALID)
    m = sn.read_manifest(occ(scratch, "o1"))
    m["post_files"], m["wrap_id"] = None, None                       # the hook never ran
    sn._write_manifest(occ(scratch, "o1"), m)
    set_prog(scratch, "o1", phase="saving", wrap_id=None)            # the executor wrote `saving` before the save
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason) == ("done", rc.COMMITTED_NOT_RECORDED) and res.settled
    assert res.card["kind"] == "consolidated" and res.card["his"] == [] and "rollback o1" in res.card["remedy"]
    m = sn.read_manifest(occ(scratch, "o1"))
    assert m["wrap_id"] == 1 and m["post_files"] == sn.tier_hashes(scratch)
    assert sn.rollback(scratch, occ(scratch, "o1")).ok               # recovery's record is what rollback needs


def test_the_kill_between_anneals_commit_and_the_rename_reports_the_unrenamed_files_and_records_nothing(
        scratch, monkeypatch):
    crashed_before_rename(scratch, "o1", _VALID, monkeypatch)
    assert prog(scratch, "o1")["phase"] == "saving" and store_state(scratch)[1] == 1   # committed
    assert not (scratch.parent / "memory.continuity.md").exists()                      # the text never landed
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert res.ending == "done" and res.reason == rc.COMMITTED_NOT_RECORDED and not res.settled
    assert ".md.tmp" in " ".join(res.notes) and "mv" in " ".join(res.notes)
    assert not sn.read_manifest(occ(scratch, "o1"))["post_files"]    # no proof the live text is the dream's yet
    assert sn.rollback(scratch, occ(scratch, "o1")).outcome == "nothing"
    # the operator finishes anneal's rename by hand; the rerun records
    tmps = sorted(scratch.parent.glob("memory.continuity.*.md.tmp")), sorted(scratch.parent.glob("*.json.tmp"))
    tmps[0][0].replace(scratch.parent / "memory.continuity.md")
    tmps[1][0].replace(scratch.parent / "memory.continuity.meta.json")
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert res.ending == "done" and res.settled
    assert sn.read_manifest(occ(scratch, "o1"))["post_files"]["continuity"] == prog(scratch, "o1")["composed_sha256"]


def test_a_recorded_result_is_returned_unchanged_on_a_rerun(scratch):
    dream(scratch, "o1", _VALID)
    set_prog(scratch, "o1", phase="saving")
    first = rc.recover(scratch, occ(scratch, "o1"))
    (occ(scratch, "o1") / "progress.json").unlink()                  # whatever happens to the record now...
    again = rc.recover(scratch, occ(scratch, "o1"))
    assert (again.ending, again.reason, again.card) == (first.ending, first.reason, first.card)


# ---------------------------------------------------------------------------------------------- the open-wrap rows

def open_wrap(db: Path, token: str) -> None:
    with Store(str(db), section_schema=None) as s:
        prepare_wrap(s, wrap_token=token)


def test_a_wrap_still_open_in_the_save_is_cancelled_by_token_and_ends_failed(scratch):
    tok = uuid.uuid4().hex
    open_wrap(scratch, tok)
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps(
        {"phase": "saving", "wrap_token": tok, "last_wrap_before": 0, "composed_sha256": "ab" * 32}))
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason, res.wrap) == ("failed", rc.KILLED_IN_SAVE, "cancelled") and res.card is None
    assert store_state(scratch)[0] is None


def test_a_wrap_that_is_another_programs_is_never_cancelled(scratch):
    other = uuid.uuid4().hex
    open_wrap(scratch, other)
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps(
        {"phase": "composing", "wrap_token": uuid.uuid4().hex}))
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert res.ending == "failed" and res.wrap == "none_open"
    assert store_state(scratch)[0] is not None                       # theirs is still open


def test_died_before_the_save_with_no_child_cancels_our_wrap_and_files_no_card(scratch):
    tok = uuid.uuid4().hex
    open_wrap(scratch, tok)
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps({"phase": "composing", "wrap_token": tok}))
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason, res.card) == ("failed", rc.KILLED_BEFORE_SAVE, None)
    assert res.wrap == "cancelled" and store_state(scratch)[0] is None
    assert rc.recover(scratch, occ(scratch, "o1")).reason == rc.KILLED_BEFORE_SAVE   # idempotent


def test_saving_with_our_wrap_cleared_and_nothing_committed_ends_failed_wrap_cleared(scratch):
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps(
        {"phase": "saving", "wrap_token": uuid.uuid4().hex, "last_wrap_before": 0, "composed_sha256": "cd" * 32}))
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason) == ("failed", rc.WRAP_CLEARED) and res.card is None


def test_a_new_wrap_whose_hash_is_not_ours_is_an_integrity_item_for_him_and_nothing_is_adopted(scratch):
    dream(scratch, "o0", text_n(0))                                  # someone else's wrap...
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps(   # ...while ours believed `last_wrap_before` = 0
        {"phase": "saving", "wrap_token": uuid.uuid4().hex, "last_wrap_before": 0, "composed_sha256": "ef" * 32}))
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason) == ("failed", rc.UNKNOWN_COMMIT)
    assert res.card["his"] and "no rollback is offered" in res.card["his"][0]
    assert sn.read_manifest(occ(scratch, "o1")) is None


def test_ours_with_another_wrap_after_it_is_unknown_not_adopted(scratch):
    dream(scratch, "o1", _VALID)
    set_prog(scratch, "o1", phase="saving")
    add_episode(scratch, "later")
    dream(scratch, "o2", text_n(2))                                  # a later writer committed after ours
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert res.reason == rc.UNKNOWN_COMMIT and res.card["his"]


def test_an_unreadable_progress_file_changes_nothing(scratch):
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text("{not json")
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert res.ending == "none" and res.reason == "progress-unreadable" and not res.settled


def test_recovery_refuses_while_a_run_holds_the_store_lock(scratch):
    from levain import wrap as w
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps({"phase": "composing", "wrap_token": "x"}))
    fd = w._lock_wrap_file(scratch.parent / "wrap.lock")
    try:
        res = rc.recover(scratch, occ(scratch, "o1"))
    finally:
        w._unlock_wrap(fd)
    assert (res.ending, res.reason) == ("none", "lock-held") and not (occ(scratch, "o1") / rc.RECOVERY_RECORD).exists()


# --------------------------------------------------------------------------------------------------- phase `saved`

def test_died_between_the_effect_and_the_card_files_the_card_once(scratch):
    r = dream(scratch, "o1", _VALID)
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason) == ("done", rc.DIED_BEFORE_CARD) and res.card["dedupe_key"] == "dream:o1"
    assert r.wrap_id == 1
    occ2 = occ(scratch, "o2")
    occ2.mkdir()
    (occ2 / "progress.json").write_text((occ(scratch, "o1") / "progress.json").read_text())
    assert rc.recover(scratch, occ2, card_filed=True).card is None


def test_a_saved_dream_whose_post_record_was_lost_gets_it_back(scratch):
    dream(scratch, "o1", _VALID)
    m = sn.read_manifest(occ(scratch, "o1"))
    m["post_files"], m["wrap_id"] = None, None
    sn._write_manifest(occ(scratch, "o1"), m)
    rc.recover(scratch, occ(scratch, "o1"))
    assert sn.read_manifest(occ(scratch, "o1"))["post_files"]


# ------------------------------------------------------------------------------------------------- the `stopped` rows

def test_a_stop_in_composing_leaves_our_wrap_cancelled_and_no_card_and_the_row_stays_stopped(scratch):
    tok = uuid.uuid4().hex
    open_wrap(scratch, tok)
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps({"phase": "composing", "wrap_token": tok}))
    res = rc.recover(scratch, occ(scratch, "o1"), state="stopped")
    assert (res.ending, res.card, res.wrap) == ("stopped", None, "cancelled") and store_state(scratch)[0] is None


def test_a_stop_observed_after_a_committed_save_files_one_committed_after_stop_card_and_stays_stopped(scratch):
    dream(scratch, "o1", _VALID)
    set_prog(scratch, "o1", phase="saving")
    res = rc.recover(scratch, occ(scratch, "o1"), state="stopped")
    assert (res.ending, res.reason) == ("stopped", rc.COMMITTED_AFTER_STOP)
    assert res.card["reason"] == rc.COMMITTED_AFTER_STOP and res.card["his"] == []
    assert rc.recover(scratch, occ(scratch, "o1"), state="stopped").card == res.card   # one card: the record repeats it
    set_prog(scratch, "o1", phase="saved")
    p = occ(scratch, "o3")
    p.mkdir()
    (p / "progress.json").write_text((occ(scratch, "o1") / "progress.json").read_text())
    assert rc.recover(scratch, p, state="stopped").reason == rc.COMMITTED_AFTER_STOP


def test_a_bad_state_is_refused(scratch):
    with pytest.raises(ValueError):
        rc.recover(scratch, occ(scratch, "o1"), state="done")


# ---------------------------------------------------------------------------------------- the orphaned compose child

SLEEP_FIRE = textwrap.dedent('''\
    import subprocess, sys, time
    sys.path.insert(0, {repo!r})
    from levain.dream import executor as ex
    from levain.consent.delivery import process_identity

    class SleepMind:
        name, source, unit, model, has_child = "sleep", "x", "tokens", "x", False
        def compose(self, req):
            argv = ["sh", "-c", "sleep 300; true", "sh", "--add-dir", str(req.occ_dir)]
            p = subprocess.Popen(argv, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(0.4)
            ident = process_identity(p.pid)
            req.progress.update(child_pid=p.pid, child_started_at=ident[0], child_argv=argv)
            print("READY", p.pid, flush=True)
            time.sleep(300)

    ex.consolidate_store({db!r}, mind=SleepMind(), occurrence_dir={occ!r}, max_seconds=3000)
    ''')


def fire_killed_mid_compose(db: Path, name: str, tmp_path: Path):
    """Run a real fire in a subprocess, wait until its compose child is recorded, then kill -9 the fire."""
    script = tmp_path / "fire.py"
    script.write_text(SLEEP_FIRE.format(repo=str(REPO), db=str(db), occ=str(occ(db, name))))
    fire = subprocess.Popen([sys.executable, "-I", str(script)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=str(tmp_path))
    line = ""
    while not line.startswith("READY"):
        line = fire.stdout.readline()
        assert line, fire.stderr.read()
    child = int(line.split()[1])
    os.kill(fire.pid, signal.SIGKILL)
    fire.wait(timeout=10)
    return child


def alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def test_kill_9_in_compose_recovery_cancels_by_token_and_ends_the_orphaned_child_group(scratch, tmp_path):
    child = fire_killed_mid_compose(scratch, "o1", tmp_path)
    try:
        assert alive(child) and store_state(scratch)[0] is not None            # the orphan lives; the wrap is open
        assert prog(scratch, "o1")["phase"] == "composing" and prog(scratch, "o1")["child_pid"] == child
        res = rc.recover(scratch, occ(scratch, "o1"), term_wait=3, kill_wait=3)
        assert (res.ending, res.reason) == ("failed", rc.KILLED_BEFORE_SAVE)
        assert res.child == "ended" and res.wrap == "cancelled"
        deadline = time.time() + 5
        while alive(child) and time.time() < deadline:
            time.sleep(0.05)
        assert not alive(child) and store_state(scratch)[0] is None
    finally:
        try:
            os.killpg(child, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def test_a_stranger_at_the_recorded_pids_place_is_never_signalled(scratch, tmp_path):
    child = fire_killed_mid_compose(scratch, "o1", tmp_path)
    try:
        set_prog(scratch, "o1", child_started_at="Mon Jan  1 00:00:00 2001")     # a different start time: another process
        res = rc.recover(scratch, occ(scratch, "o1"), term_wait=0.3, kill_wait=0.3)
        assert res.child == "not-confirmed" and alive(child)                      # no signal was sent
        assert res.ending == "failed" and res.wrap == "cancelled"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child, signal.SIGKILL)


def test_an_unreadable_process_table_sends_no_signal(scratch, tmp_path, monkeypatch):
    child = fire_killed_mid_compose(scratch, "o1", tmp_path)
    try:
        monkeypatch.setattr("levain.consent.delivery.process_identity", lambda pid: None)
        res = rc.recover(scratch, occ(scratch, "o1"), term_wait=0.3, kill_wait=0.3)
        assert res.child == "not-confirmed" and alive(child) and "could not be read" in " ".join(res.notes)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child, signal.SIGKILL)


def test_a_command_line_that_is_another_occurrences_is_a_mismatch(scratch, tmp_path):
    child = fire_killed_mid_compose(scratch, "o1", tmp_path)
    try:
        elsewhere = tmp_path / "elsewhere" / "o9"
        elsewhere.mkdir(parents=True)
        (elsewhere / "progress.json").write_text(json.dumps(prog(scratch, "o1")))
        res = rc.recover(scratch, elsewhere, term_wait=0.3, kill_wait=0.3)        # recorded argv names o1, not o9
        assert res.child == "not-confirmed" and alive(child)
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child, signal.SIGKILL)


def test_a_child_that_is_already_gone_is_reported_ended_without_a_signal(scratch):
    occ(scratch, "o1").mkdir(parents=True)
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps(
        {"phase": "composing", "wrap_token": uuid.uuid4().hex, "child_pid": 2 ** 22 + 12345,
         "child_started_at": "Mon Jan  1 00:00:00 2001", "child_argv": ["x"]}))
    assert rc.recover(scratch, occ(scratch, "o1")).child == "ended"


def test_the_recover_verb_prints_the_ending_and_returns_nonzero_only_when_nothing_was_settled(scratch, capsys):
    import argparse
    from levain.dream import cli as dcli
    dream(scratch, "o1", _VALID)
    capsys.readouterr()
    ns = argparse.Namespace(occurrence="o1", store_dir=str(scratch.parent), state="launched", card_filed=False,
                            json=True)
    assert dcli.run_dream_recover(ns) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == rc.DIED_BEFORE_CARD
    occ(scratch, "o2").mkdir()
    (occ(scratch, "o2") / "progress.json").write_text("{bad")
    ns.occurrence = "o2"
    assert dcli.run_dream_recover(ns) == 1
