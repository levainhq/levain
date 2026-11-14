"""Row 1694: the restore drill restores into a scratch store built from the dream's own kept saved text.

A drill that copies the live store refuses on a store somebody wrote to since the dream. These tests write to the
store after the dream in the ways that matter (a crystal write, a consolidate that is not this dream, episodes) and
require the drill to prove the restore anyway, and to say so in words when the live store is past the dream.
"""
from __future__ import annotations

import shutil

import pytest
from anneal_memory import Store

from levain.dream import cli as dcli
from levain.dream import executor as ex
from levain.dream import snapshot as sn
from tests.test_dream_snapshot import Fake, _args, add_episode, dream, occ, scratch, text_n, two_dreams  # noqa: F401


def kept(db, name):
    return occ(db, name) / sn.SNAP_DIR / sn.POST_DIR


def step_status(res):
    return [(s["name"], s["status"]) for s in res.steps]


OK_STEPS = [("hash", "ok"), ("post", "ok"), ("integrity", "ok"), ("restore", "ok"), ("open", "ok")]


# ----------------------------------------------------------------------------------- keep the saved text (T3, T4)

def test_t3_a_saved_dream_keeps_its_saved_text_beside_the_snapshot(scratch):
    r = dream(scratch, "o1", text_n(1))
    assert r.outcome == "saved"
    m = sn.read_manifest(occ(scratch, "o1"))
    tier = sn.text_tier(scratch)
    present = {k: p.name for k, p in tier.items() if p.is_file()}
    assert {"continuity", "meta"} <= set(present)
    assert sorted(p.name for p in kept(scratch, "o1").iterdir()) == sorted(present.values())
    for key, name in present.items():
        assert sn.sha256_file(kept(scratch, "o1") / name) == m["post_files"][key]
    assert not list((occ(scratch, "o1") / sn.SNAP_DIR).glob(".*"))


def test_t4_a_write_between_the_copy_and_the_re_hash_refuses_the_post_record(scratch, monkeypatch):
    on_snap, _ = sn.hooks()
    r = ex.consolidate_store(scratch, mind=Fake(text_n(1)), occurrence_dir=occ(scratch, "o1"), max_seconds=3000,
                             on_snapshot=on_snap)
    assert r.outcome == "saved"
    real = shutil.copy2

    def racing(src, dst, *a, **k):
        out = real(src, dst, *a, **k)
        if sn.POST_DIR in str(dst):
            with open(src, "ab") as f:        # a writer lands between the copy and the check
                f.write(b"\n")
        return out

    monkeypatch.setattr(sn.shutil, "copy2", racing)
    with pytest.raises(sn.SnapshotRefused, match="changed while it was copied"):
        sn.record_post_for(scratch, occ(scratch, "o1"), r.wrap_id)
    assert sn.read_manifest(occ(scratch, "o1"))["post_files"] is None


# ------------------------------------------------------------------------------ the drill restores anyway (T1 T2 T14)

def test_t1_a_crystal_write_after_the_dream_does_not_stop_the_drill_restoring(scratch):
    two_dreams(scratch)
    (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok" and step_status(res) == OK_STEPS, res
    assert sn.last_drill(scratch.parent / "dream")["outcome"] == "ok"
    assert res.live_past_dream == ["memory.crystal.json"]
    assert sn.last_drill(scratch.parent / "dream")["live_past_dream"] == ["memory.crystal.json"]


def test_t2_a_consolidate_that_is_not_this_dream_does_not_stop_the_drill_restoring(scratch):
    two_dreams(scratch)
    add_episode(scratch, "three")
    r = ex.consolidate_store(scratch, mind=Fake(text_n(9)), occurrence_dir=scratch.parent / "dream" / "foreign",
                             max_seconds=3000)
    assert r.outcome == "saved", r
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok" and res.snapshot == "o2" and step_status(res) == OK_STEPS, res
    assert "memory.continuity.md" in res.live_past_dream
    # the real rollback still refuses on the live store: that refusal is the rollback verb's, and is unchanged
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "foreign-write"


def test_t14_episodes_written_after_the_dream_are_not_a_foreign_write(scratch):
    two_dreams(scratch)
    for i in range(50):
        add_episode(scratch, f"later {i}")
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok" and step_status(res) == OK_STEPS and res.live_past_dream == []
    assert "live_past_dream" not in sn.last_drill(scratch.parent / "dream")


# --------------------------------------------------------------------------------------- a damaged kept copy (T5)

def test_t5_a_damaged_kept_text_fails_the_post_step_and_names_the_file(scratch):
    two_dreams(scratch)
    f = kept(scratch, "o2") / "memory.continuity.md"
    f.write_bytes(f.read_bytes() + b"damage")
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "failed"
    failed = [s for s in res.steps if s["status"] == "failed"]
    assert [s["name"] for s in failed] == ["post"] and "memory.continuity.md" in failed[0]["detail"]
    assert sn.last_drill(scratch.parent / "dream")["outcome"] == "failed"


def test_t5b_a_missing_kept_file_fails_the_post_step_too(scratch):
    two_dreams(scratch)
    (kept(scratch, "o2") / "memory.continuity.meta.json").unlink()
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "failed" and [s["name"] for s in res.steps if s["status"] == "failed"] == ["post"]


# ---------------------------------------------------------------------------- which snapshot, which markers (T6 T7)

def test_t6_the_drill_takes_the_newest_snapshot_that_holds_a_dreams_saved_text(scratch):
    two_dreams(scratch)
    m = sn.read_manifest(occ(scratch, "o2"))            # the newest night snapshotted but did not save
    m["post_files"], m["wrap_id"] = None, None
    sn._write_manifest(occ(scratch, "o2"), m)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok" and res.snapshot == "o1" and step_status(res) == OK_STEPS, res


def test_t7_a_dream_rolled_back_by_hand_still_proves_the_mechanism(scratch):
    two_dreams(scratch)
    assert sn.rollback(scratch, occ(scratch, "o2")).outcome == "rolled-back"
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok" and res.snapshot == "o2" and step_status(res) == OK_STEPS, res
    assert sn.read_manifest(occ(scratch, "o2"))["reverted_at"]            # the real manifest is untouched


# ---------------------------------------------------------------------------------------------- no-drillable (T8)

def test_t8_snapshots_without_kept_text_are_no_drillable_and_record_nothing(scratch):
    two_dreams(scratch)
    root = scratch.parent / "dream"
    assert sn.drill(scratch, root).outcome == "ok"
    for n in ("o1", "o2"):
        shutil.rmtree(kept(scratch, n))
    before = (root / sn.DRILL_RECORD).read_bytes()
    res = sn.drill(scratch, root)
    assert res.outcome == "no-drillable"
    assert (root / sn.DRILL_RECORD).read_bytes() == before
    (root / sn.DRILL_RECORD).unlink()
    assert sn.drill(scratch, root).outcome == "no-drillable" and not (root / sn.DRILL_RECORD).exists()


# ------------------------------------------------------------------------------------------ legacy record (T11)

def test_t11_a_record_the_skipped_code_wrote_is_no_verdict_and_the_drill_is_due(tmp_path):
    import datetime as dt
    root = tmp_path / "r"
    root.mkdir()
    now = dt.datetime.now().astimezone()
    for rec in ({"outcome": "skipped", "at": now.isoformat()},
                {"outcome": "ok", "at": now.isoformat(),
                 "steps": [{"name": "restore", "status": "skipped", "detail": "foreign-write"}]}):
        sn._write_json(root / sn.DRILL_RECORD, rec)
        assert sn.last_drill(root) is None and sn.drill_due(root, now)
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "ok", "at": now.isoformat(), "steps": []})
    assert sn.last_drill(root)["outcome"] == "ok" and not sn.drill_due(root, now)


# --------------------------------------------------------------------------------------------------- the verb (T13)

def test_t13_the_verb_says_ok_and_says_the_live_store_is_past_the_dream(scratch, capsys):
    two_dreams(scratch)
    (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')
    capsys.readouterr()
    assert dcli.run_dream_drill(_args(store_dir=str(scratch.parent), root=None, json=False)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("levain dream drill: ok (snapshot o2)")
    past = next(ln for ln in lines if "past this dream" in ln)
    assert "memory.crystal.json" in past and "foreign-write" in past and "proves the restore" in past


def test_t13_the_verb_on_a_store_with_nothing_to_drill_exits_4_with_the_words(scratch, capsys):
    two_dreams(scratch)
    for n in ("o1", "o2"):
        shutil.rmtree(kept(scratch, n))
    capsys.readouterr()
    code = dcli.run_dream_drill(_args(store_dir=str(scratch.parent), root=None, json=False))
    first = capsys.readouterr().out.splitlines()[0]
    assert code == 4 and first.startswith("levain dream drill: nothing to drill") and sn.NO_DRILLABLE_WHY in first
    assert "nothing was proved" in first and "stays due" in first
    assert dcli.run_dream_drill(_args(store_dir=str(scratch.parent), root=None, json=True)) == 4
