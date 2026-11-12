"""levain.dream.snapshot (D1-1b): take, record-post, one-step rollback, retention, the drill and the verbs.

Every test runs the REAL ``consolidate_store`` (with the snapshot hooks) against a tiny scratch anneal store under
``tmp_path``, never ``~/.anneal-memory``. A fake in-process mind returns canned neocortex text, so nothing is
spent. Each assertion was first read off a hand run of the real functions on a scratch store: after a rollback by
hand anneal ACCEPTED the next consolidate; ``Store.get_wrap_history`` does not carry the content hash (the
``wraps.content_hash`` column does); a rolled-back dream's occurrence dir keeps ``reverted-from/``.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from anneal_memory import FLOW_SCHEMA, Store

from levain.dream import cli as dcli
from levain.dream import executor as ex
from levain.dream import snapshot as sn

_VALID = """\
## State
First consolidation of the test store.

## Active Threads
- Getting my bearings — pointer: this session.

## Patterns
Nothing graduated yet.

## Decisions
Nothing committed yet.

## Context
The first dream metabolized the opening episodes.

## Understanding
Early days.
"""


TOK = "a" * 32


def text_n(n: int) -> str:
    return _VALID.replace("Early days.", f"Day {n}.")


class Fake:
    name, source, unit, model = "fake", "fake", "tokens", "fake"

    def __init__(self, text: str) -> None:
        self.text = text

    def compose(self, req):
        return ex.ComposeResult(text=self.text, model="fake", unit="tokens", input_tokens=1, output_tokens=1)


@pytest.fixture
def scratch(tmp_path):
    d = tmp_path / "store"
    d.mkdir()
    with Store(str(d / "memory.db"), section_schema=FLOW_SCHEMA) as s:
        for i in range(3):
            s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")
    return d / "memory.db"


def add_episode(db: Path, label: str) -> None:
    with Store(str(db), section_schema=None) as s:
        s.record(content=f"Later: {label}", episode_type="observation", source="t")


def dream(db: Path, name: str, text: str):
    """One real consolidate with the snapshot hooks, occurrence dir ``<store>/dream/<name>``."""
    on_snap, on_saved = sn.hooks()
    return ex.consolidate_store(db, mind=Fake(text), occurrence_dir=db.parent / "dream" / name, max_seconds=3000,
                                on_snapshot=on_snap, on_saved=on_saved)


def occ(db: Path, name: str) -> Path:
    return db.parent / "dream" / name


def two_dreams(db: Path):
    assert dream(db, "o1", text_n(1)).outcome == "saved"
    add_episode(db, "two")
    r = dream(db, "o2", text_n(2))
    assert r.outcome == "saved", r
    return r


def store_state(db: Path):
    with Store(str(db), section_schema=None) as s:
        return s.get_wrap_started_at(), s.last_wrap_id(), len(s.episodes_since_wrap())


# ------------------------------------------------------------------------------------------ take and record-post

def test_a_dream_leaves_a_manifest_with_every_hash_and_the_post_record(scratch):
    r = dream(scratch, "o1", _VALID)
    assert r.outcome == "saved" and r.hook_errors == []
    m = sn.read_manifest(occ(scratch, "o1"))
    snap = occ(scratch, "o1") / "snap"
    assert m["occurrence"] == "o1" and m["format_version"] == 2 and m["store_id"]
    assert m["files"]["continuity"]["sha256"] is None            # a first dream replaces no text
    assert m["db_backup_sha256"] == sn.sha256_file(snap / sn.DB_BACKUP)
    post = m["post_files"]
    assert post == sn.tier_hashes(scratch) and post["continuity"] and m["wrap_id"] == r.wrap_id == 1
    assert r.manifest["occurrence"] == "o1" and r.post_files == post
    # the backup is a whole, openable db with the pre-dream episodes and no wrap
    con = sqlite3.connect(snap / sn.DB_BACKUP)
    assert con.execute("select count(*) from episodes").fetchone()[0] == 3
    assert con.execute("select count(*) from wraps").fetchone()[0] == 0
    con.close()
    assert not list(snap.glob(".*"))                             # no temp debris


def test_a_second_dream_snapshots_the_text_the_first_one_wrote(scratch):
    two_dreams(scratch)
    m = sn.read_manifest(occ(scratch, "o2"))
    first = sn.read_manifest(occ(scratch, "o1"))
    assert m["files"]["continuity"]["sha256"] == first["post_files"]["continuity"]
    assert m["files"]["meta"]["sha256"] == first["post_files"]["meta"]
    assert "Day 1." in (occ(scratch, "o2") / "snap" / "memory.continuity.md").read_text()


def test_the_content_hash_oracle_is_the_plain_sha_of_the_saved_text(scratch):
    r = dream(scratch, "o1", _VALID)
    prog = json.loads((occ(scratch, "o1") / "progress.json").read_text())
    ((wid, h, pair),) = sn.wraps_after(scratch, 0)
    assert (wid, h) == (r.wrap_id, prog["composed_sha256"])
    assert pair.startswith(prog["wrap_token"][:12] + "-")                     # the commit identity is the token's
    assert sn.wraps_after(scratch, r.wrap_id) == []


def test_take_refuses_any_format_other_than_2_and_nothing_is_prepared(scratch, monkeypatch):
    monkeypatch.setattr(sn, "format_version", lambda db: 3)
    r = dream(scratch, "o1", _VALID)
    assert (r.outcome, r.exit_code) == ("snapshot-refused", 2) and "format_version" in r.detail
    assert store_state(scratch) == (None, 0, 3)                  # no wrap opened, no episode consumed
    assert not (occ(scratch, "o1") / "snap").exists()


def test_a_refused_take_leaves_no_half_written_snapshot(scratch, monkeypatch):
    def boom(src, dest):
        raise sqlite3.OperationalError("disk full")
    monkeypatch.setattr(sn, "_backup_db", boom)
    r = dream(scratch, "o1", _VALID)
    assert r.outcome == "snapshot-refused" and not (occ(scratch, "o1") / "snap").exists()


def test_take_refuses_a_second_snapshot_for_one_occurrence_and_an_open_wrap(scratch):
    dream(scratch, "o1", _VALID)
    with Store(str(scratch), section_schema=None) as s:
        with pytest.raises(sn.SnapshotRefused, match="already holds a snapshot"):
            sn.take(s, occ(scratch, "o1"))
        add_episode(scratch, "x")
    with Store(str(scratch), section_schema=None) as s:
        from anneal_memory.continuity import prepare_wrap
        prepare_wrap(s, wrap_token=TOK)
        with pytest.raises(sn.SnapshotRefused, match="wrap is open"):
            sn.take(s, occ(scratch, "o9"))
        with pytest.raises(sn.SnapshotRefused, match="no occurrence dir"):
            sn.take(s, None)


def test_record_post_refuses_a_second_record_an_unchanged_tier_and_text_that_is_not_the_dreams(scratch):
    dream(scratch, "o1", _VALID)
    with pytest.raises(sn.SnapshotRefused, match="already has a post-save record"):
        sn.record_post_for(scratch, occ(scratch, "o1"), 1)
    # an unchanged tier: a snapshot taken now, no save since
    with Store(str(scratch), section_schema=None) as s:
        sn.take(s, occ(scratch, "o7"))
    with pytest.raises(sn.SnapshotRefused, match="unchanged since the snapshot"):
        sn.record_post_for(scratch, occ(scratch, "o7"), 2)
    # a live text that is not the composed text is never blessed as the dream's
    (scratch.parent / "memory.continuity.md").write_text("someone else's text\n")
    with pytest.raises(sn.SnapshotRefused, match="not the text this dream composed"):
        sn.record_post_for(scratch, occ(scratch, "o7"), 2, composed_sha256="0" * 64)


# ---------------------------------------------------------------------------------------------------------- rollback

def test_rollback_restores_every_file_to_the_manifests_pre_and_keeps_what_it_replaced(scratch):
    two_dreams(scratch)
    m = sn.read_manifest(occ(scratch, "o2"))
    dreams_text = (scratch.parent / "memory.continuity.md").read_bytes()
    res = sn.rollback(scratch, occ(scratch, "o2"), who="t")
    assert res.outcome == "rolled-back" and res.ok
    assert sn.tier_hashes(scratch) == {k: e["sha256"] for k, e in m["files"].items()}
    assert (occ(scratch, "o2") / sn.UNDO_DIR / "memory.continuity.md").read_bytes() == dreams_text
    rec = json.loads((occ(scratch, "o2") / sn.ROLLBACK_RECORD).read_text())
    assert rec["who"] == "t" and sorted(rec["files"]) == ["memory.continuity.md", "memory.continuity.meta.json"]
    assert sn.read_manifest(occ(scratch, "o2"))["reverted_at"]
    # the limit is stated in the output and the card carries it and the right key
    assert "NOT re-offered" in res.message and "revert_wrap" in res.message
    assert res.card["kind"] == "rolled-back" and res.card["dedupe_key"] == "dream-rollback:o2"
    assert not list(scratch.parent.glob(".*.rb-*"))              # no temp debris beside the store


def test_a_rollback_keeps_every_capture_made_before_during_and_after_the_dream(scratch):
    """The rollback never touches the db, so a seat's capture survives it. Written from a live run: 170 captures from a
    second process while a dream ran, then a rollback, 0 missing. Here the captures come from a second connection."""
    made: list[str] = []

    class Capturing(Fake):
        def compose(self, req):                                   # seats keep capturing while the mind composes
            with Store(str(scratch), section_schema=None) as s:
                made.extend(s.record(content=f"seat {i}", episode_type="observation", source="seat").id
                            for i in range(5))
            return super().compose(req)

    on_snap, on_saved = sn.hooks()
    r = ex.consolidate_store(scratch, mind=Capturing(text_n(1)), occurrence_dir=occ(scratch, "o1"), max_seconds=3000,
                             on_snapshot=on_snap, on_saved=on_saved)
    assert r.outcome == "saved"
    add_episode(scratch, "after the save")
    assert sn.rollback(scratch, occ(scratch, "o1")).ok
    con = sqlite3.connect(str(scratch))
    have = {row[0] for row in con.execute("SELECT id FROM episodes")}
    con.close()
    assert len(made) == 5 and set(made) <= have
    assert len(have) == 3 + 5 + 1


def test_the_next_dream_is_accepted_after_a_rollback_and_the_rolled_back_episodes_are_not_re_offered(scratch):
    two_dreams(scratch)
    assert sn.rollback(scratch, occ(scratch, "o2")).ok
    assert store_state(scratch) == (None, 2, 0)                  # the watermark did not move: the stated limit
    add_episode(scratch, "three")
    r = dream(scratch, "o3", text_n(3))
    assert r.outcome == "saved" and r.wrap_id == 3               # anneal accepted the next save (run by hand first)
    assert "Day 3." in (scratch.parent / "memory.continuity.md").read_text()


def test_a_later_crystal_only_write_refuses_rollback(scratch):
    two_dreams(scratch)
    (scratch.parent / "memory.crystal.json").write_text('{"crystals": ["a later crystallization"]}')
    before = sn.tier_hashes(scratch)
    res = sn.rollback(scratch, occ(scratch, "o2"))
    assert (res.outcome, res.code) == ("refused", "foreign-write") and "crystal" in res.message
    assert sn.tier_hashes(scratch) == before and not (occ(scratch, "o2") / sn.UNDO_DIR).exists()


def test_an_interrupted_rollback_finishes_on_a_rerun_and_keeps_the_dreams_text(scratch, monkeypatch):
    two_dreams(scratch)
    m = sn.read_manifest(occ(scratch, "o2"))
    dreams_text = (scratch.parent / "memory.continuity.md").read_bytes()
    real, calls = sn._atomic_copy, []

    def dies_on_second(src, dest):
        calls.append(dest.name)
        if len(calls) == 2:
            raise OSError("killed")
        real(src, dest)
    monkeypatch.setattr(sn, "_atomic_copy", dies_on_second)
    with pytest.raises(OSError):
        sn.rollback(scratch, occ(scratch, "o2"))
    mix = sn.tier_hashes(scratch)
    assert mix["continuity"] != m["post_files"]["continuity"] and mix["meta"] == m["post_files"]["meta"]
    monkeypatch.setattr(sn, "_atomic_copy", real)
    res = sn.rollback(scratch, occ(scratch, "o2"))
    assert res.outcome == "rolled-back" and sn.tier_hashes(scratch) == {k: e["sha256"] for k, e in m["files"].items()}
    # the first attempt's copy is the only one holding the dream's text: the rerun did not overwrite it
    assert (occ(scratch, "o2") / sn.UNDO_DIR / "memory.continuity.md").read_bytes() == dreams_text


def test_a_rollback_that_restored_everything_then_died_is_marked_finished_by_a_rerun(scratch, monkeypatch):
    two_dreams(scratch)
    monkeypatch.setattr(sn, "_finish", lambda *a, **k: (_ for _ in ()).throw(OSError("died before the manifest")))
    with pytest.raises(OSError):
        sn.rollback(scratch, occ(scratch, "o2"))
    m = sn.read_manifest(occ(scratch, "o2"))
    assert m["revert_started_at"] and not m["reverted_at"]
    monkeypatch.undo()
    res = sn.rollback(scratch, occ(scratch, "o2"))
    assert res.outcome == "finished" and sn.read_manifest(occ(scratch, "o2"))["reverted_at"]
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "already-reverted"


def test_rollback_refusals(scratch, monkeypatch):
    assert sn.rollback(scratch, occ(scratch, "none")).code == "no-snapshot"
    two_dreams(scratch)
    # a rollback of the older dream while the newer one stands
    assert sn.rollback(scratch, occ(scratch, "o1")).code == "later-dream"
    # a rollback of the newer, then of the older (a rollback after a rollback: one step only)
    assert sn.rollback(scratch, occ(scratch, "o2")).ok
    res = sn.rollback(scratch, occ(scratch, "o1"))
    assert res.code == "later-dream" and "one step" in res.message
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "already-reverted"


def test_rollback_refuses_with_a_wrap_open_no_post_record_a_foreign_store_a_damaged_snapshot_and_a_held_lock(scratch):
    from levain import wrap as w
    two_dreams(scratch)
    snap = occ(scratch, "o2") / "snap"
    # lock held by a consolidate
    fd = w._lock_wrap_file(scratch.parent / "wrap.lock")
    try:
        assert sn.rollback(scratch, occ(scratch, "o2")).code == "lock-held"
    finally:
        w._unlock_wrap(fd)
    # a wrap is open
    add_episode(scratch, "w")
    with Store(str(scratch), section_schema=None) as s:
        from anneal_memory.continuity import prepare_wrap
        prepare_wrap(s, wrap_token=TOK)
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "wrap-open"
    with Store(str(scratch), section_schema=None) as s:
        s.wrap_cancelled(expect_token=TOK)
    # a damaged snapshot file is caught before anything is touched
    f = snap / "memory.continuity.md"
    good = f.read_bytes()
    f.write_bytes(good + b"x")
    before = sn.tier_hashes(scratch)
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "snapshot-damaged"
    assert sn.tier_hashes(scratch) == before
    f.write_bytes(good)
    # another store's snapshot
    m = sn.read_manifest(occ(scratch, "o2"))
    m["store_id"] = "someone-elses"
    sn._write_manifest(occ(scratch, "o2"), m)
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "store-mismatch"
    # no post record
    m["store_id"] = sn._store_id(scratch)
    m["post_files"] = None
    sn._write_manifest(occ(scratch, "o2"), m)
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "no-post"


def test_rollback_refuses_a_store_that_is_no_longer_format_2(scratch, monkeypatch):
    two_dreams(scratch)
    monkeypatch.setattr(sn, "format_version", lambda db: 3)
    res = sn.rollback(scratch, occ(scratch, "o2"))
    assert res.code == "format" and sn.tier_hashes(scratch)["continuity"] == \
        sn.read_manifest(occ(scratch, "o2"))["post_files"]["continuity"]


def test_a_card_filer_failure_is_recorded_not_raised(scratch):
    two_dreams(scratch)
    seen = []

    def filer(card):
        seen.append(card["dedupe_key"])
        raise RuntimeError("review store down")
    res = sn.rollback(scratch, occ(scratch, "o2"), file_card=filer)
    assert res.ok and seen == ["dream-rollback:o2"] and "review store down" in res.card_error


def test_nothing_to_roll_back_when_the_live_text_is_already_the_snapshots(scratch):
    dream(scratch, "o1", _VALID)
    with Store(str(scratch), section_schema=None) as s:
        sn.take(s, occ(scratch, "o8"))
    assert sn.rollback(scratch, occ(scratch, "o8")).outcome == "nothing"


# ---------------------------------------------------------------------------------------------------------- retention

def test_only_the_last_two_consolidated_snapshots_are_kept(scratch):
    for i in (1, 2, 3):
        if i > 1:
            add_episode(scratch, str(i))
        assert dream(scratch, f"o{i}", text_n(i)).outcome == "saved"
    kept = [(occ(scratch, f"o{i}") / "snap").exists() for i in (1, 2, 3)]
    assert kept == [False, True, True]
    assert (occ(scratch, "o1") / "progress.json").exists()       # only the heavy snapshot goes, never the record


def test_retention_spares_an_unsettled_save_and_a_snapshot_newer_than_the_kept_ones(scratch):
    for i in (1, 2):
        if i > 1:
            add_episode(scratch, str(i))
        dream(scratch, f"o{i}", text_n(i))
    with Store(str(scratch), section_schema=None) as s:
        sn.take(s, occ(scratch, "o0"))                           # a snapshot with no post record, newest
    (occ(scratch, "o0") / "progress.json").write_text(json.dumps({"phase": "saving"}))
    assert sn.prune_snapshots(occ(scratch, "o0").parent, keep=1) == ["o1"]
    assert (occ(scratch, "o0") / "snap").exists() and (occ(scratch, "o2") / "snap").exists()


# ---------------------------------------------------------------------------------------------------------------- drill

def test_the_drill_proves_a_restore_on_a_copy_and_never_touches_the_store(scratch):
    two_dreams(scratch)
    before = (sn.tier_hashes(scratch), sn.sha256_file(scratch), sn.read_manifest(occ(scratch, "o2")))
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok" and res.snapshot == "o2"
    assert [(s["name"], s["status"]) for s in res.steps] == [("hash", "ok"), ("integrity", "ok"),
                                                             ("restore", "ok"), ("open", "ok")]
    assert (sn.tier_hashes(scratch), sn.sha256_file(scratch), sn.read_manifest(occ(scratch, "o2"))) == before
    assert sn.last_drill(scratch.parent / "dream")["outcome"] == "ok"
    assert not sn.drill_due(scratch.parent / "dream")


def test_the_drill_fails_on_a_damaged_snapshot_or_backup_and_says_which(scratch):
    two_dreams(scratch)
    snap = occ(scratch, "o2") / "snap"
    bk = snap / sn.DB_BACKUP
    good = bk.read_bytes()
    bk.write_bytes(good[:-100] + b"\0" * 100)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "failed" and res.steps[0]["name"] == "hash" and sn.DB_BACKUP in res.steps[0]["detail"]
    assert sn.last_drill(scratch.parent / "dream")["outcome"] == "failed"


def test_the_drill_catches_a_backup_that_hashes_right_but_is_corrupt(scratch):
    two_dreams(scratch)
    bk = occ(scratch, "o2") / "snap" / sn.DB_BACKUP
    bad = bk.read_bytes()[:4096] + b"\x00" * 8192 + bk.read_bytes()[12288:]
    bk.write_bytes(bad)
    m = sn.read_manifest(occ(scratch, "o2"))
    m["db_backup_sha256"] = sn.sha256_file(bk)
    sn._write_manifest(occ(scratch, "o2"), m)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "failed" and [s["name"] for s in res.steps if s["status"] == "failed"] == ["integrity"]


def test_the_drill_skips_the_restore_when_the_live_store_is_past_the_dream(scratch):
    two_dreams(scratch)
    (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')
    res = sn.drill(scratch, scratch.parent / "dream")
    # 1112-1 (F10): a drill whose restore was skipped is not a pass; it is its own outcome
    assert res.outcome == "skipped" and [s["status"] for s in res.steps] == ["ok", "ok", "skipped", "skipped"]
    assert "foreign-write" in res.steps[2]["detail"]
    assert sn.last_drill(scratch.parent / "dream")["outcome"] == "skipped"


def test_a_fresh_install_has_no_snapshot_yet_which_is_not_a_failure(scratch):
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "no-snapshot" and res.steps == []


def test_drill_due_reads_the_cadence(tmp_path):
    import datetime as dt
    root = tmp_path / "r"
    assert sn.drill_due(root)
    root.mkdir()
    now = dt.datetime.now().astimezone()
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "ok", "at": now.isoformat()})
    assert not sn.drill_due(root, now + dt.timedelta(days=6)) and sn.drill_due(root, now + dt.timedelta(days=7, seconds=1))
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "ok", "at": "garbage"})
    assert sn.drill_due(root)


def test_1112_1_a_snapshot_with_no_post_save_record_is_a_skipped_drill_not_a_pass(scratch):
    two_dreams(scratch)
    m = sn.read_manifest(occ(scratch, "o2"))
    m["post_files"], m["wrap_id"] = None, None
    sn._write_manifest(occ(scratch, "o2"), m)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "skipped" and [s["status"] for s in res.steps] == ["ok", "ok", "skipped", "skipped"]


def test_1112_1_a_skipped_drill_after_a_failed_one_carries_the_failure_forward(scratch):
    two_dreams(scratch)
    root = scratch.parent / "dream"
    bk = occ(scratch, "o2") / "snap" / sn.DB_BACKUP
    good = bk.read_bytes()
    bk.write_bytes(good[:-100] + b"\0" * 100)
    assert sn.drill(scratch, root).outcome == "failed"
    bk.write_bytes(good)
    (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')              # a seat wrote since the dream
    res = sn.drill(scratch, root)
    assert res.outcome == "skipped"
    # 1112-1 round 2: the hash step passed in THIS skipped drill, so the failure it found is not carried (it was a
    # false red: the record said "hash: ..." while the same record's own steps said hash ok)
    assert "carried_failure" not in sn.last_drill(root) and res.steps[0] == {
        "name": "hash", "status": "ok", "detail": res.steps[0]["detail"]}
    (scratch.parent / "memory.crystal.json").unlink()
    assert sn.drill(scratch, root).outcome == "ok"


class _Refused:
    """A rollback that fails on the copy for a reason that is not the guard's: the restore step itself failed."""
    outcome, code, message, restored = "failed", "x", "boom: the restore wrote the wrong files", []
    ok = False


def test_1112_1_r2_a_failure_on_the_restore_step_stays_carried_through_skipped_drills(scratch, monkeypatch):
    two_dreams(scratch)
    root = scratch.parent / "dream"
    with monkeypatch.context() as mp:
        mp.setattr(sn, "rollback", lambda *a, **k: _Refused())
        assert sn.drill(scratch, root).outcome == "failed"
    (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')                # a seat wrote since the dream
    res = sn.drill(scratch, root)
    assert res.outcome == "skipped" and "restore: boom" in sn.last_drill(root)["carried_failure"]
    assert sn.drill(scratch, root).as_dict()["carried_failure"] == sn.last_drill(root)["carried_failure"]
    (scratch.parent / "memory.crystal.json").unlink()                              # and a real pass drops it
    assert sn.drill(scratch, root).outcome == "ok" and "carried_failure" not in sn.last_drill(root)


def test_1112_1_r2_only_restore_and_open_failures_are_carried_by_a_skip(scratch):
    two_dreams(scratch)
    root = scratch.parent / "dream"
    steps = lambda *names: [{"name": n, "status": "failed", "detail": f"{n} broke"} for n in names]   # noqa: E731
    for name, carried in (("setup", False), ("hash", False), ("copy", False), ("integrity", False),
                          ("restore", True), ("open", True)):
        sn._write_json(root / sn.DRILL_RECORD, {"outcome": "failed", "snapshot": "o2", "steps": steps(name),
                                                "at": "2026-01-01T00:00:00+00:00"})
        (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')
        rec = sn.drill(scratch, root).as_dict()
        assert rec["outcome"] == "skipped" and ("carried_failure" in rec) is carried, (name, rec)
        if carried:
            assert rec["carried_failure"] == sn.failed_steps_text(steps(name))
    (scratch.parent / "memory.crystal.json").unlink()


def test_1112_1_r2_a_record_written_before_this_change_with_a_skipped_restore_reads_as_skipped(tmp_path):
    root = tmp_path / "r"
    root.mkdir()
    old = {"outcome": "ok", "snapshot": "o2", "at": "2026-11-01T00:00:00+00:00",
           "steps": [{"name": "hash", "status": "ok", "detail": ""}, {"name": "integrity", "status": "ok", "detail": ""},
                     {"name": "restore", "status": "skipped", "detail": "foreign-write: ..."},
                     {"name": "open", "status": "skipped", "detail": "no restored copy"}]}
    sn._write_json(root / sn.DRILL_RECORD, old)
    assert sn.last_drill(root)["outcome"] == "skipped"                            # was "ok"
    assert json.loads((root / sn.DRILL_RECORD).read_text())["outcome"] == "ok"   # the file is not rewritten: a read-time view
    real = {**old, "steps": [{**s, "status": "ok"} for s in old["steps"]]}
    sn._write_json(root / sn.DRILL_RECORD, real)
    assert sn.last_drill(root)["outcome"] == "ok"                                 # a real pass is untouched


def test_1112_1_r2_a_drill_that_finds_no_snapshot_does_not_overwrite_the_last_record(scratch):
    root = scratch.parent / "dream"
    root.mkdir(parents=True, exist_ok=True)
    sn._write_json(root / sn.DRILL_RECORD, {"outcome": "failed", "at": "2026-11-01T00:00:00+00:00",
                                            "steps": [{"name": "hash", "status": "failed", "detail": "d"}]})
    res = sn.drill(scratch, root)                                                 # no dream has run: no snapshot
    assert res.outcome == "no-snapshot"
    assert sn.last_drill(root)["outcome"] == "failed"                             # was "no-snapshot"


# ------------------------------------------------------------------------------------------------------------ the verbs

def _args(**kw):
    import argparse
    return argparse.Namespace(**kw)


def test_the_verbs_roll_back_and_drill_a_store_copy(scratch, capsys):
    two_dreams(scratch)
    capsys.readouterr()
    d = str(scratch.parent)
    assert dcli.run_dream_drill(_args(store_dir=d, root=None, json=True)) == 0
    assert json.loads(capsys.readouterr().out)["outcome"] == "ok"
    assert dcli.run_dream_rollback(_args(occurrence="o2", store_dir=d, who="cli", json=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["outcome"] == "rolled-back" and out["card"]["dedupe_key"] == "dream-rollback:o2"
    assert dcli.run_dream_rollback(_args(occurrence="o2", store_dir=d, who="cli", json=False)) == 0
    assert "already-reverted" in capsys.readouterr().out
    assert dcli.run_dream_rollback(_args(occurrence="nope", store_dir=d, who="cli", json=False)) == 2


def test_1112_1_the_drill_verb_says_skipped_and_exits_non_zero_when_the_restore_was_not_exercised(scratch, capsys):
    two_dreams(scratch)
    (scratch.parent / "memory.crystal.json").write_text('{"x": 1}')
    capsys.readouterr()
    assert dcli.run_dream_drill(_args(store_dir=str(scratch.parent), root=None, json=False)) == 3
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("levain dream drill: skipped") and "not a pass" in out.splitlines()[0]
    assert "levain dream drill: ok" not in out


def test_1112_1_r2_the_drill_verb_exits_4_and_names_the_holder_when_the_wrap_lock_is_held(scratch, capsys):
    from levain import wrap as w
    two_dreams(scratch)
    capsys.readouterr()
    fd = w._lock_wrap_file(scratch.parent / "wrap.lock")
    try:
        code = dcli.run_dream_drill(_args(store_dir=str(scratch.parent), root=None, json=False))
    finally:
        w._unlock_wrap(fd)
    out = capsys.readouterr().out
    assert code == 4, (code, out)                                                  # was 0
    first = out.splitlines()[0]
    assert first.startswith("levain dream drill: deferred") and "wrap lock" in first and "nothing was proved" in first
    assert "not a failure" not in out and "no snapshot yet" not in out
    assert sn.last_drill(scratch.parent / "dream") is None                         # nothing recorded: still due


def test_1112_1_r2_the_drill_verb_on_a_fresh_install_says_so_and_exits_0(scratch, capsys):
    assert dcli.run_dream_drill(_args(store_dir=str(scratch.parent), root=None, json=False)) == 0
    assert "no snapshot yet: not a failure" in capsys.readouterr().out


def test_the_verbs_refuse_the_live_store_and_exist_in_the_parser(monkeypatch, tmp_path, capsys):
    import argparse
    live = tmp_path / "home" / ".anneal-memory"
    live.mkdir(parents=True)
    monkeypatch.setattr("levain.firing.isolation.flow_store_dir", lambda: live)
    assert dcli.run_dream_drill(_args(store_dir=str(live), root=None, json=False)) == 2
    assert "live store" in capsys.readouterr().err
    top = argparse.ArgumentParser()
    dcli.add_parsers(top.add_subparsers())
    ns = top.parse_args(["dream", "rollback", "o1", "--store-dir", "x"])
    assert ns.func is dcli.run_dream_rollback
    assert top.parse_args(["dream", "recover", "o1", "--store-dir", "x", "--state", "stopped"]).state == "stopped"
    assert top.parse_args(["dream", "drill", "--store-dir", "x"]).func is dcli.run_dream_drill


# =========================================================================== L3 r1 fixes (fix2)

def test_h3_rollback_refuses_when_either_store_id_is_missing_or_different_before_reading_the_live_files(scratch,
                                                                                                        monkeypatch):
    two_dreams(scratch)
    m = sn.read_manifest(occ(scratch, "o2"))
    seen = []
    real = sn.tier_hashes
    monkeypatch.setattr(sn, "tier_hashes", lambda d: seen.append(d) or real(d))
    for bad in ("", None):                                             # the snapshot records no id
        m["store_id"] = bad
        sn._write_manifest(occ(scratch, "o2"), m)
        r = sn.rollback(scratch, occ(scratch, "o2"))
        assert r.code == "store-mismatch" and "no store id" in r.message
    m["store_id"] = "someone-elses"
    sn._write_manifest(occ(scratch, "o2"), m)
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "store-mismatch"
    m["store_id"] = sn._store_id(scratch)
    sn._write_manifest(occ(scratch, "o2"), m)
    monkeypatch.setattr(sn, "_store_id", lambda db: None)             # the store's own id cannot be read
    r = sn.rollback(scratch, occ(scratch, "o2"))
    assert r.code == "store-mismatch" and "cannot be read" in r.message
    assert seen == []                                                  # no live file was read before the refusals


def test_m2_a_rollback_that_dies_before_its_record_is_finished_by_a_rerun(scratch, monkeypatch):
    two_dreams(scratch)
    real = sn._write_json

    def dies(path, doc):
        if path.name == sn.ROLLBACK_RECORD:
            raise OSError(28, "No space left on device")
        return real(path, doc)
    monkeypatch.setattr(sn, "_write_json", dies)
    with pytest.raises(OSError):
        sn.rollback(scratch, occ(scratch, "o2"))
    monkeypatch.setattr(sn, "_write_json", real)
    assert not sn.read_manifest(occ(scratch, "o2")).get("reverted_at")   # the marker is last: not set
    r = sn.rollback(scratch, occ(scratch, "o2"), file_card=lambda c: None)
    assert r.outcome == "finished" and r.card["dedupe_key"] == "dream-rollback:o2"
    assert (occ(scratch, "o2") / sn.ROLLBACK_RECORD).exists()
    assert sn.read_manifest(occ(scratch, "o2"))["reverted_at"]
    assert sn.rollback(scratch, occ(scratch, "o2")).code == "already-reverted"


def test_m2_a_card_that_was_not_filed_is_refiled_by_a_rerun_with_the_same_record(scratch):
    two_dreams(scratch)
    calls = []

    def flaky(card):
        calls.append(card["at"])
        if len(calls) == 1:
            raise RuntimeError("review store down")
    first = sn.rollback(scratch, occ(scratch, "o2"), file_card=flaky)
    assert first.ok and first.card_error and not sn.read_manifest(occ(scratch, "o2")).get("reverted_at")
    at = json.loads((occ(scratch, "o2") / sn.ROLLBACK_RECORD).read_text())["at"]
    second = sn.rollback(scratch, occ(scratch, "o2"), file_card=flaky)
    assert second.outcome == "finished" and second.card_error is None
    assert calls == [at, at]                                           # the same card, the same stamp
    assert sn.read_manifest(occ(scratch, "o2"))["reverted_at"] == at


def test_m5_the_drill_after_a_real_rollback_skips_instead_of_failing(scratch):
    two_dreams(scratch)
    assert sn.rollback(scratch, occ(scratch, "o2")).ok
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "skipped" and [s["status"] for s in res.steps] == ["ok", "ok", "skipped", "skipped"]
    assert "already-reverted" in res.steps[2]["detail"]


def test_m5_the_drill_defers_while_a_wrap_is_open_and_records_nothing(scratch):
    from anneal_memory.continuity import prepare_wrap
    two_dreams(scratch)
    add_episode(scratch, "w")
    with Store(str(scratch), section_schema=None) as s:
        prepare_wrap(s, wrap_token=TOK)                                # a dream composing, no lock needed to show it
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "deferred" and "wrap-open" in res.steps[-1]["detail"]
    assert sn.last_drill(scratch.parent / "dream") is None and sn.drill_due(scratch.parent / "dream")


def test_m6_the_drill_copies_under_the_wrap_lock_and_defers_when_it_is_held(scratch, monkeypatch):
    from levain import wrap as w
    from levain.dream import executor as ex_
    two_dreams(scratch)
    monkeypatch.setattr(ex_, "_copy_store", lambda *a: pytest.fail("the live store was copied without the lock"))
    fd = w._lock_wrap_file(scratch.parent / "wrap.lock")
    try:
        res = sn.drill(scratch, scratch.parent / "dream")
    finally:
        w._unlock_wrap(fd)
    assert res.outcome == "deferred" and "wrap lock" in res.steps[-1]["detail"]
    assert sn.last_drill(scratch.parent / "dream") is None


def test_m6_the_drill_holds_the_lock_while_it_copies(scratch, monkeypatch):
    from levain import wrap as w
    from levain.dream import executor as ex_
    two_dreams(scratch)
    real = ex_._copy_store
    held = []

    def probe(src, dest):
        h = w._lock_wrap_file(scratch.parent / "wrap.lock")           # a second taker while the drill copies
        held.append(h is w._ANOTHER_WRAP_RUNNING)
        if h is not w._ANOTHER_WRAP_RUNNING:
            w._unlock_wrap(h)
        return real(src, dest)
    monkeypatch.setattr(ex_, "_copy_store", probe)
    assert sn.drill(scratch, scratch.parent / "dream").outcome == "ok"
    assert held == [True]


def test_low_snapshots_order_by_instant_not_by_the_text_of_a_local_offset_stamp(scratch):
    two_dreams(scratch)
    # across a fall-back change, "01:30-04:00" (earlier) sorts AFTER "01:10-05:00" (later) as text
    for name, stamp in (("o1", "2026-11-01T01:30:00-04:00"), ("o2", "2026-11-01T01:10:00-05:00")):
        m = sn.read_manifest(occ(scratch, name))
        m["taken_at"] = stamp
        sn._write_manifest(occ(scratch, name), m)
    assert [n for n, _ in sn.snapshots(scratch.parent / "dream")] == ["o1", "o2"]


def test_low_a_settled_failed_save_stops_pinning_its_snapshot(scratch, monkeypatch):
    two_dreams(scratch)
    root = scratch.parent / "dream"
    # an old occurrence whose save died: a snapshot with no post record, progress still `saving`
    m = sn.read_manifest(occ(scratch, "o1"))
    m["post_files"], m["wrap_id"] = None, None
    sn._write_manifest(occ(scratch, "o1"), m)
    p = json.loads((occ(scratch, "o1") / "progress.json").read_text())
    p["phase"] = "saving"
    (occ(scratch, "o1") / "progress.json").write_text(json.dumps(p))
    assert "o1" not in sn.prune_snapshots(root)                        # unsettled: recovery still needs it
    (occ(scratch, "o1") / "recovery.json").write_text(json.dumps({"ending": "failed"}))
    assert "o1" in sn.prune_snapshots(root)                            # settled: debris


def test_low_a_bare_occurrence_id_is_never_read_as_a_directory_of_the_working_dir(scratch, tmp_path, monkeypatch):
    two_dreams(scratch)
    here = tmp_path / "cwd"
    (here / "o2").mkdir(parents=True)                                  # a look-alike, with no snapshot in it
    monkeypatch.chdir(here)
    got, err = dcli._occurrence_dir(scratch, "o2")
    assert got == (scratch.parent / "dream" / "o2").resolve()
    got, err = dcli._occurrence_dir(scratch, "./o2")                   # an explicit path is still a path
    assert got == (here / "o2").resolve()


# --------------------------------------------------------------------------------------------------- fix3 (L3 round 2)

@pytest.mark.parametrize("arg", ["..", ".", "", "../x", "a/../b", "~"])
def test_f3_dot_forms_and_empty_ids_are_refused_with_the_accepted_forms(scratch, tmp_path, monkeypatch, arg):
    two_dreams(scratch)
    monkeypatch.chdir(tmp_path)
    got, err = dcli._occurrence_dir(scratch, arg)
    assert got is None and "Accepted" in err and "occurrence id" in err


def test_f3_a_dot_prefixed_bare_name_is_an_id_not_a_path(scratch, tmp_path, monkeypatch):
    two_dreams(scratch)
    (tmp_path / ".o2").mkdir()
    monkeypatch.chdir(tmp_path)
    got, err = dcli._occurrence_dir(scratch, ".o2")
    assert got is None and "no such occurrence" in err                 # looked for under <store>/dream/, not the cwd
    got, err = dcli._occurrence_dir(scratch, str(occ(scratch, "o2")))
    assert got == occ(scratch, "o2").resolve()


def test_f4_a_rerun_after_the_card_was_lost_prints_the_same_card_and_exits_zero(scratch, capsys):
    two_dreams(scratch)
    d = str(scratch.parent)
    capsys.readouterr()
    assert dcli.run_dream_rollback(_args(occurrence="o2", store_dir=d, who="cli", json=True)) == 0
    first = json.loads(capsys.readouterr().out)["card"]               # the process "crashed" before anyone filed this
    assert dcli.run_dream_rollback(_args(occurrence="o2", store_dir=d, who="other", json=True)) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["outcome"] == "already-reverted" and again["card"] == first
    assert dcli.run_dream_rollback(_args(occurrence="o2", store_dir=d, who="cli", json=False)) == 0
    out = capsys.readouterr().out
    assert "already-reverted" in out and "dream-rollback:o2" in out


def test_low_take_refuses_a_store_with_no_readable_id(scratch, tmp_path, monkeypatch):
    from anneal_memory import Store
    monkeypatch.setattr(Store, "store_id", "", raising=False)
    with Store(str(scratch), section_schema=None) as s, pytest.raises(sn.SnapshotRefused, match="store id"):
        sn.take(s, tmp_path / "occ")


# --------------------------------------------------------------------------------------------------- fix4 (L3 round 3)

def test_g3_the_drill_chooses_its_snapshot_under_the_same_wrap_lock_it_copies_under(scratch, monkeypatch):
    from levain import wrap as w
    two_dreams(scratch)
    real = sn.snapshots
    seen = []

    def probe(root):
        if Path(root) != scratch.parent / "dream":
            return real(root)
        h = w._lock_wrap_file(scratch.parent / "wrap.lock")           # a second taker while the drill selects
        seen.append(h is w._ANOTHER_WRAP_RUNNING)
        if h is not w._ANOTHER_WRAP_RUNNING:
            w._unlock_wrap(h)
        return real(root)
    monkeypatch.setattr(sn, "snapshots", probe)
    assert sn.drill(scratch, scratch.parent / "dream").outcome == "ok"
    assert seen == [True]


def test_g3_a_held_lock_defers_the_drill_before_it_looks_at_any_snapshot(scratch, monkeypatch):
    from levain import wrap as w
    two_dreams(scratch)
    monkeypatch.setattr(sn, "snapshots", lambda root: pytest.fail("a snapshot was selected without the lock"))
    fd = w._lock_wrap_file(scratch.parent / "wrap.lock")
    try:
        res = sn.drill(scratch, scratch.parent / "dream")
    finally:
        w._unlock_wrap(fd)
    assert res.outcome == "deferred" and sn.last_drill(scratch.parent / "dream") is None


# --------------------------------------------------------------------------------------------------- fix5 (L3 round 4)

def test_k4_the_lock_is_held_during_hashing_and_free_during_integrity_check(scratch, monkeypatch):
    from levain import wrap as w
    two_dreams(scratch)
    held_at_hash, held_at_integrity = [], []
    real_hash, real_connect = sn.sha256_file, sn.sqlite3.connect

    def taken():
        h = w._lock_wrap_file(scratch.parent / "wrap.lock")
        if h is not w._ANOTHER_WRAP_RUNNING:
            w._unlock_wrap(h)
            return False
        return True

    def hash_probe(path):
        if Path(path).name == sn.DB_BACKUP:
            held_at_hash.append(taken())
        return real_hash(path)

    def connect_probe(target, *a, **k):
        if sn.DB_BACKUP in str(target):
            held_at_integrity.append(taken())
        return real_connect(target, *a, **k)
    monkeypatch.setattr(sn, "sha256_file", hash_probe)
    monkeypatch.setattr(sn.sqlite3, "connect", connect_probe)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok"
    assert held_at_hash and all(held_at_hash)
    assert held_at_integrity == [False]


def _latest_snap_dir(scratch):
    name, _m = sn.snapshots(scratch.parent / "dream")[-1]
    return scratch.parent / "dream" / name / sn.SNAP_DIR


def test_fix7_a_snapshot_pruned_after_release_cannot_fail_the_drill(scratch, monkeypatch):
    import shutil as _sh
    from levain import wrap as w
    two_dreams(scratch)
    snapdir = _latest_snap_dir(scratch)
    real_unlock = w._unlock_wrap

    def unlock_then_prune(h):
        out = real_unlock(h)
        _sh.rmtree(snapdir.parent, ignore_errors=True)       # a concurrent prune right after release
        return out
    monkeypatch.setattr(w, "_unlock_wrap", unlock_then_prune)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "ok", res.steps


def test_fix7_a_short_scratch_copy_is_a_failed_copy_and_integrity_never_runs_on_it(scratch, monkeypatch):
    two_dreams(scratch)
    real = sn.shutil.copy2

    def short(src, dst, *a, **k):
        real(src, dst, *a, **k)
        data = Path(dst).read_bytes()
        Path(dst).write_bytes(data[: len(data) // 2])
        return dst
    monkeypatch.setattr(sn.shutil, "copy2", short)
    real_connect = sn.sqlite3.connect
    opened = []
    monkeypatch.setattr(sn.sqlite3, "connect",
                        lambda target, *a, **k: (opened.append(str(target)), real_connect(target, *a, **k))[1])
    res = sn.drill(scratch, scratch.parent / "dream")
    names = [(s["name"], s["status"]) for s in res.steps]
    assert res.outcome == "failed" and names[-1] == ("copy", "failed")
    assert ("hash", "failed") not in names and not any(s["name"] == "integrity" for s in res.steps)
    assert not any(sn.DB_BACKUP in o for o in opened)


def test_fix7_a_corrupted_original_is_a_corrupt_snapshot_at_the_hash_step(scratch):
    two_dreams(scratch)
    victim = _latest_snap_dir(scratch) / sn.DB_BACKUP
    data = bytearray(victim.read_bytes())
    data[len(data) // 2] ^= 0xFF
    victim.write_bytes(bytes(data))
    res = sn.drill(scratch, scratch.parent / "dream")
    names = [(s["name"], s["status"]) for s in res.steps]
    assert res.outcome == "failed" and names[-1] == ("hash", "failed")
    assert not any(s["name"] in ("copy", "integrity") for s in res.steps)


# --------------------------------------------------------------------------------------------------- fix6 (L3 round 5)

@pytest.fixture
def lock_ledger(monkeypatch):
    """Every wrap-lock handle taken and not yet released (an in-process re-acquire cannot show a leak)."""
    from levain import wrap as w
    out: list = []
    real_lock, real_unlock = w._lock_wrap_file, w._unlock_wrap

    def lock(*a, **k):
        h = real_lock(*a, **k)
        if h is not w._ANOTHER_WRAP_RUNNING:
            out.append(h)
        return h

    def unlock(h):
        if h in out:
            out.remove(h)
        return real_unlock(h)
    monkeypatch.setattr(w, "_lock_wrap_file", lock)
    monkeypatch.setattr(w, "_unlock_wrap", unlock)
    return out


@pytest.mark.parametrize("exc", [OSError("disk full"), sqlite3.OperationalError("locked"), KeyError("boom")])
def test_j1_a_copy_that_raises_never_leaves_the_wrap_lock_held(scratch, monkeypatch, lock_ledger, exc):
    from levain.dream import executor as ex_
    two_dreams(scratch)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(ex_, "_copy_store", boom)
    try:
        res = sn.drill(scratch, scratch.parent / "dream")
        assert res.outcome == "failed" and res.steps[-1]["name"] == "restore"
    except KeyError:
        assert isinstance(exc, KeyError)                 # a non-OSError may propagate, but never with the lock held
    assert lock_ledger == []


def test_j1_a_failure_in_the_snapshot_copy_loop_releases_the_lock_too(scratch, monkeypatch, lock_ledger):
    two_dreams(scratch)
    real = sn.shutil.copy2

    def boom(*a, **k):
        raise RuntimeError("not an OSError")
    monkeypatch.setattr(sn.shutil, "copy2", boom)
    with pytest.raises(RuntimeError):
        sn.drill(scratch, scratch.parent / "dream")
    monkeypatch.setattr(sn.shutil, "copy2", real)
    assert lock_ledger == []


def test_j1_the_restore_copy_failing_still_hashes_and_checks_the_snapshot(scratch, monkeypatch):
    from levain.dream import executor as ex_
    two_dreams(scratch)
    monkeypatch.setattr(ex_, "_copy_store", lambda *a, **k: (_ for _ in ()).throw(OSError("full")))
    res = sn.drill(scratch, scratch.parent / "dream")
    names = [(s["name"], s["status"]) for s in res.steps]
    assert names[:2] == [("hash", "ok"), ("integrity", "ok")] and names[-1] == ("restore", "failed")


def test_j2_a_short_copy_is_never_reported_as_a_corrupt_snapshot(scratch, monkeypatch):
    two_dreams(scratch)
    real = sn.shutil.copy2

    def short(src, dst, *a, **k):
        real(src, dst, *a, **k)
        data = Path(dst).read_bytes()
        Path(dst).write_bytes(data[: len(data) // 2])
        return dst
    monkeypatch.setattr(sn.shutil, "copy2", short)
    res = sn.drill(scratch, scratch.parent / "dream")
    hashed = [s for s in res.steps if s["name"] == "hash"]
    assert hashed and hashed[0]["status"] == "ok"        # the originals are intact, so the snapshot is not corrupt
    assert res.steps[-1]["name"] == "copy" and res.steps[-1]["status"] == "failed"


def test_j3_a_lock_or_scratch_failure_is_a_failed_drill_not_a_traceback(scratch, monkeypatch, lock_ledger):
    from levain import wrap as w
    two_dreams(scratch)
    real = w._lock_wrap_file

    def deny(*a, **k):
        raise PermissionError("no lock for you")
    monkeypatch.setattr(w, "_lock_wrap_file", deny)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "failed" and "wrap lock" in res.steps[-1]["detail"]
    monkeypatch.setattr(w, "_lock_wrap_file", real)

    def nospace(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(sn.tempfile, "mkdtemp", nospace)
    res = sn.drill(scratch, scratch.parent / "dream")
    assert res.outcome == "failed" and "scratch" in res.steps[-1]["detail"]
    assert lock_ledger == []
