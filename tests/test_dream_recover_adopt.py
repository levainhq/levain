"""Row 1696: crash recovery in the ``saving`` phase adopts a save whose text anneal changed.

anneal's save puts back a durable line the compose left out, or removes one through a drop marker, so the wrap row's
``content_hash`` is not the journal's ``composed_sha256``. Recovery then recognises the save by the token-prefixed
``pair_id`` AND ``snapshot.reinsertion_proof``, both. The real-kill tests run a REAL consolidate in a child process
that is SIGKILLed after anneal committed and before ``progress.update(phase="saved")``; recovery is then asked of the
scratch store the child left. Nothing here touches a real store.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import sqlite3
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from levain.dream import recover as rc
from levain.dream import snapshot as sn
from tests.test_dream_o1_reinsert import PRIOR_FACT, drop, live, prior_wrap, with_durable
from tests.test_dream_snapshot import add_episode, dream, occ, scratch, text_n  # noqa: F401

REPO = Path(__file__).resolve().parent.parent

KILL_BEFORE_SAVED = textwrap.dedent('''
    import os, signal, sys
    sys.path.insert(0, {repo!r})
    from levain.dream import executor as ex
    from levain.dream import snapshot as sn

    class Mind:
        name, source, unit, model = "fake", "fake", "tokens", "fake"
        def compose(self, req):
            return ex.ComposeResult(text=open({text!r}, encoding="utf-8").read(), model="fake", unit="tokens",
                                    input_tokens=1, output_tokens=1)

    real = ex.Progress.update
    def update(self, **values):
        if values.get("phase") == "saved":
            os.kill(os.getpid(), signal.SIGKILL)       # anneal has committed; the journal still reads "saving"
        return real(self, **values)
    ex.Progress.update = update

    on_snap, on_saved = sn.hooks()
    ex.consolidate_store({db!r}, mind=Mind(), occurrence_dir={occ!r}, max_seconds=3000,
                         on_snapshot=on_snap, on_saved=on_saved)
    print("NOT KILLED")
''')


def prog(db, name):
    return json.loads((occ(db, name) / "progress.json").read_text())


def kill_in_save(db: Path, name: str, composed: str, tmp_path: Path) -> subprocess.CompletedProcess:
    text = tmp_path / f"{name}.composed.md"
    text.write_text(composed, encoding="utf-8")
    script = tmp_path / f"{name}_kill.py"
    script.write_text(KILL_BEFORE_SAVED.format(repo=str(REPO), db=str(db), occ=str(occ(db, name)), text=str(text)))
    done = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, cwd=str(tmp_path),
                          timeout=120)
    assert done.returncode == -signal.SIGKILL, (done.returncode, done.stdout, done.stderr)
    p = prog(db, name)
    assert p["phase"] == "saving" and p.get("wrap_id") is None          # killed after the commit, before "saved"
    assert not (sn.read_manifest(occ(db, name)) or {}).get("post_files")  # and before the post record
    return done


def edit_last_wrap(db: Path, assignment: str, *args) -> None:
    """Rewrite the newest wraps row. The table's triggers call an anneal function this plain connection lacks, so they
    are dropped first, as the legacy-row test of recovery does; the scratch store is the test's own."""
    con = sqlite3.connect(str(db))
    for (name,) in con.execute("select name from sqlite_master where type = 'trigger' and tbl_name = 'wraps'").fetchall():
        con.execute(f"drop trigger {name}")
    con.execute(f"update wraps set {assignment} where id = (select max(id) from wraps)", args)
    con.commit()
    con.close()


def wrap_row(db: Path):
    con = sqlite3.connect(str(db))
    try:
        return con.execute("select id, content_hash, pair_id from wraps order by id desc limit 1").fetchone()
    finally:
        con.close()


# ------------------------------------------------------------------------------- the real kills, both kinds of change

def test_a_kill_in_the_save_of_a_re_inserted_text_is_adopted_with_a_post_record_and_a_rollback(scratch, tmp_path):
    prior_wrap(scratch)
    before = sn.sha256_file(scratch.parent / "memory.continuity.md")
    composed = with_durable(text_n(2), "- A new fact.")                       # PRIOR_FACT left out: anneal puts it back
    kill_in_save(scratch, "o2", composed, tmp_path)
    wid, chash, pair = wrap_row(scratch)
    assert chash != prog(scratch, "o2")["composed_sha256"] and PRIOR_FACT in live(scratch)   # the text anneal changed
    res = rc.recover(scratch, occ(scratch, "o2"))
    assert (res.ending, res.reason, res.settled) == ("done", rc.COMMITTED_NOT_RECORDED, True), res
    assert res.card["kind"] == "consolidated" and res.card["his"] == []
    assert "put back 1 line the compose left out" in res.card["reason"] and PRIOR_FACT[2:] in res.card["reason"]
    m = sn.read_manifest(occ(scratch, "o2"))
    assert m["post_files"] == sn.tier_hashes(scratch) and m["wrap_id"] == wid and m["reinserted"] == [PRIOR_FACT]
    back = sn.rollback(scratch, occ(scratch, "o2"), who="test")
    assert back.outcome == "rolled-back", back
    assert sn.sha256_file(scratch.parent / "memory.continuity.md") == before


def test_a_kill_in_the_save_of_a_drop_marker_text_is_adopted_and_the_card_names_the_removed_line(scratch, tmp_path):
    prior_wrap(scratch)
    kill_in_save(scratch, "o2", with_durable(text_n(2), "- A new fact.", drop(PRIOR_FACT)), tmp_path)
    assert PRIOR_FACT not in live(scratch)
    res = rc.recover(scratch, occ(scratch, "o2"))
    assert (res.ending, res.reason, res.settled) == ("done", rc.COMMITTED_NOT_RECORDED, True), res
    assert "removed 1 line through drop markers" in res.card["reason"] and PRIOR_FACT[2:] in res.card["reason"]
    assert sn.read_manifest(occ(scratch, "o2"))["dropped"] == [PRIOR_FACT]


def test_a_kill_in_the_save_of_an_unchanged_text_still_adopts_by_the_hash(scratch, tmp_path):
    """The control: the pre-existing exact-hash path is untouched, and its card has no put-back sentence."""
    kill_in_save(scratch, "o1", text_n(1), tmp_path)
    res = rc.recover(scratch, occ(scratch, "o1"))
    assert (res.ending, res.reason, res.settled) == ("done", rc.COMMITTED_NOT_RECORDED, True), res
    assert "put back" not in res.card["reason"] and "removed" not in res.card["reason"]


# ----------------------------------------------------------------------------------- the two negatives (conjunction)

def killed_reinsertion(scratch, tmp_path):
    prior_wrap(scratch)
    kill_in_save(scratch, "o2", with_durable(text_n(2), "- A new fact."), tmp_path)


def assert_not_adopted(scratch, res):
    assert res.reason == rc.UNKNOWN_COMMIT, res
    assert res.card["kind"] == "failed" and "no rollback is offered" in " ".join(res.card["his"])
    assert not (sn.read_manifest(occ(scratch, "o2")) or {}).get("post_files")
    assert sn.rollback(scratch, occ(scratch, "o2")).outcome == "refused"        # no post record: nothing to roll back


def test_a_text_with_our_pair_id_and_no_proof_is_not_adopted(scratch, tmp_path):
    """Right pair_id, a live text that is not the compose plus anneal's durable step: a later hand edit of the live
    bytes AND the wrap row rewritten to hash them (so the row is no longer the composed text and the pair_id is ours),
    yet anneal's step on the dream's inputs does not reproduce them."""
    killed_reinsertion(scratch, tmp_path)
    p = scratch.parent / "memory.continuity.md"
    p.write_text(p.read_text(encoding="utf-8") + "\n- a seat's later line\n", encoding="utf-8")
    edit_last_wrap(scratch, "content_hash = ?", hashlib.sha256(p.read_bytes()).hexdigest())
    assert wrap_row(scratch)[2].startswith(prog(scratch, "o2")["wrap_token"][:12] + "-")     # the pair_id is ours
    assert_not_adopted(scratch, rc.recover(scratch, occ(scratch, "o2")))


def test_a_text_with_the_proof_and_a_foreign_pair_id_is_not_adopted(scratch, tmp_path):
    """Everything the proof reads holds (real re-insertion save, live text is the wrap's), but the wrap's pair_id is
    another writer's."""
    killed_reinsertion(scratch, tmp_path)
    edit_last_wrap(scratch, "pair_id = 'ffffffffffff-deadbeef'")
    sn.reinsertion_proof(scratch, occ(scratch, "o2"), sn.read_manifest(occ(scratch, "o2")), wrap_row(scratch)[0],
                         prog(scratch, "o2")["composed_sha256"], sn.sha256_file(scratch.parent / "memory.continuity.md"))
    assert_not_adopted(scratch, rc.recover(scratch, occ(scratch, "o2")))


def test_a_row_with_no_pair_id_and_the_proof_is_not_adopted(scratch, tmp_path):
    killed_reinsertion(scratch, tmp_path)
    edit_last_wrap(scratch, "pair_id = NULL")
    assert_not_adopted(scratch, rc.recover(scratch, occ(scratch, "o2")))


def test_a_proved_save_with_a_second_wrap_after_it_is_not_adopted(scratch, tmp_path):
    killed_reinsertion(scratch, tmp_path)
    add_episode(scratch, "three")
    assert dream(scratch, "o3", with_durable(text_n(3), PRIOR_FACT, "- A new fact.")).outcome == "saved"
    res = rc.recover(scratch, occ(scratch, "o2"))
    assert res.reason == rc.UNKNOWN_COMMIT and not (sn.read_manifest(occ(scratch, "o2")) or {}).get("post_files")


def test_the_recovery_sentence_is_worded_as_the_runs_own_card_words_it(scratch, monkeypatch):
    from levain.dream import agent as da
    from levain.dream import card as cd

    for manifest in ({"reinserted": ["- " + "x" * 300], "dropped": ["* gone fact"]},
                     {"reinserted": [f"- fact {i}" for i in range(7)]}, {"dropped": ["- a", "- b"]}, {}):
        monkeypatch.setattr(sn, "read_manifest", lambda occ, m=manifest: m)
        assert cd.durable_sentence(Path("x")) == da._durable_sentence(Path("x"))
