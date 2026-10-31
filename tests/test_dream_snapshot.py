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
    assert res.outcome == "ok" and [s["status"] for s in res.steps] == ["ok", "ok", "skipped", "skipped"]
    assert "foreign-write" in res.steps[2]["detail"]


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
    assert dcli.run_dream_rollback(_args(occurrence="o2", store_dir=d, who="cli", json=False)) == 1
    assert "already-reverted" in capsys.readouterr().out
    assert dcli.run_dream_rollback(_args(occurrence="nope", store_dir=d, who="cli", json=False)) == 2


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
