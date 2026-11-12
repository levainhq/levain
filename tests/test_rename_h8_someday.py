"""H8: the stored disposition ``seed`` becomes ``someday``. No write sets ``seed`` any more; it stays a READ spelling
(a resolved record keeps it for good, and ``is_loop`` fails open on an unknown value); ``levain update`` rewrites the OPEN
records through ``levain.intention_migrate.migrate_seed_to_someday``.

Every store below is built the OLD way: through anneal's plain ``IntentionStore``, which accepts the retired spelling the
way an a8617e55c release wrote it."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from levain.dashboard import AnnealPaths
from levain.intentions import (
    BUCKET_TRAY,
    NON_COGNITION_DISPOSITIONS,
    RETIRED_DISPOSITIONS,
    RetiredDisposition,
    TRAY_WRITE_DISPOSITIONS,
    WRITE_DISPOSITIONS,
    bucket_of,
    guarded_intention_store,
    is_loop,
    is_tray,
    retired_message,
)
from levain.writes import EditError, WriteScope, apply_edit

NOW = datetime(2026, 11, 11, 12, 0, 0, tzinfo=timezone.utc)


def _migrate(paths, **kw):
    from levain.intention_migrate import migrate_seed_to_someday   # imported here so a missing module fails each test, not the file

    return migrate_seed_to_someday(paths, now=NOW, **kw)


def _old_store(tmp_path: Path) -> tuple[AnnealPaths, dict[str, str]]:
    """What an old release left: an open seed, an open TALK seed, a resolved seed, an open handoff, an open loop."""
    from anneal_memory.spores import IntentionStore

    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    store = IntentionStore(paths.intentions_json)               # the plain store: no write guard, the old id prefix
    ids = {
        "open": store.add(type="thought", text="think about the bench", disposition="seed")["id"],
        "talk": store.add(type="thought", text="what should we talk about", disposition="seed", attention="talk")["id"],
        "resolved": store.add(type="thought", text="an old seed, closed", disposition="seed")["id"],
        "handoff": store.add(type="thought", text="a legacy handoff", disposition="handoff")["id"],
        "loop": store.add(type="task", text="a real loop")["id"],
    }
    store.descend(ids["resolved"], kind="composted")
    return paths, ids


def _doc(paths: AnnealPaths) -> dict:
    return json.loads(paths.intentions_json.read_text(encoding="utf-8"))


def _open(paths: AnnealPaths, rid: str) -> dict:
    return next(r for r in _doc(paths)["intentions"] if r["id"] == rid)


# --- the sets ------------------------------------------------------------------------------------------------------

def test_someday_is_written_and_seed_is_only_read() -> None:
    assert "someday" in WRITE_DISPOSITIONS and "seed" not in WRITE_DISPOSITIONS
    assert TRAY_WRITE_DISPOSITIONS == ("someday", "agenda")
    assert "seed" in RETIRED_DISPOSITIONS and "seed" in NON_COGNITION_DISPOSITIONS


def test_a_resolved_seed_never_reads_as_a_loop() -> None:
    """The read set keeps ``seed``: is_loop fails open on an unknown value, so dropping it would put every resolved
    (and every not-yet-migrated) seed into the entity's cognition."""
    for row in ({"disposition": "seed"}, {"disposition": "seed", "status": "resolved"}):
        assert not is_loop(row) and is_tray(row) and bucket_of(row) == BUCKET_TRAY
    assert is_loop({"disposition": "sed"})      # the fail-open class this guards against stays what it is


def test_the_standalone_hooks_still_filter_seed() -> None:
    from levain import templates
    import importlib.util

    root = Path(templates.__file__).parent if getattr(templates, "__file__", None) else Path(list(templates.__path__)[0])
    for rel in ("activation/hooks/_levain_hook.py", "adapters/codex/activation/hooks/_levain_hook.py"):
        spec = importlib.util.spec_from_file_location("_hook_" + rel.split("/")[0], root / rel)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "seed" in mod._NON_COGNITION_DISPOSITIONS and "someday" in mod._NON_COGNITION_DISPOSITIONS


# --- a NEW write of seed refuses, pointing at someday ---------------------------------------------------------------

def test_the_message_points_at_someday() -> None:
    for spelling in ("seed", "Seed", "SEED "):
        assert "someday" in (retired_message(spelling) or ""), spelling


def test_the_guarded_store_refuses_to_add_or_update_to_seed(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    store = guarded_intention_store(tmp_path / ".levain" / "memory.spores.json")
    with pytest.raises(RetiredDisposition, match="someday"):
        store.add(type="thought", text="x", disposition="seed")
    made = store.add(type="thought", text="x", disposition="someday")
    with pytest.raises(RetiredDisposition, match="someday"):
        store.update(made["id"], disposition="seed")
    assert store.get(made["id"])["disposition"] == "someday"


def test_the_capture_and_set_disposition_seams_refuse_seed(tmp_path: Path) -> None:
    from tests.test_writes import _add_intention, _make_install

    install = _make_install(tmp_path)
    scope = WriteScope.from_install_root(install)
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "intention_seed", "text": "x", "disposition": "seed"})
    assert ei.value.http_status == 422 and "someday" in str(ei.value)
    rid = _add_intention(install, stype="thought", text="y", disposition="someday")
    with pytest.raises(EditError) as ei:
        apply_edit(scope, {"kind": "intention_set_disposition", "intention_id": rid, "disposition": "seed"})
    assert ei.value.http_status == 422 and "someday" in str(ei.value)


# --- the migration -------------------------------------------------------------------------------------------------

def test_open_seeds_become_someday_and_nothing_else_moves(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    before = paths.intentions_json.read_bytes()
    doc = _doc(paths)
    resolved_before = doc["resolved"]
    handoff_before = _open(paths, ids["handoff"])
    loop_before = _open(paths, ids["loop"])

    report = _migrate(paths)

    assert sorted(report.migrated) == sorted([ids["open"], ids["talk"]]) and report.count == 2
    assert report.skipped == [] and report.refused is None
    assert _open(paths, ids["open"])["disposition"] == "someday"
    talk = _open(paths, ids["talk"])
    assert talk["disposition"] == "someday" and talk["attention"] == "talk"      # the talk flag is kept
    assert _doc(paths)["resolved"] == resolved_before                           # a resolved seed keeps its spelling
    assert [r for r in _doc(paths)["resolved"] if r["id"] == ids["resolved"]][0]["disposition"] == "seed"
    assert _open(paths, ids["handoff"]) == handoff_before                       # a handoff is another migration's
    assert _open(paths, ids["loop"]) == loop_before
    assert _open(paths, ids["open"])["text"] == "think about the bench"
    # the backup is the pre-migration file, byte for byte, under <store dir>/backups/
    assert report.backup is not None and report.backup.parent == tmp_path / ".levain" / "backups" / "intentions"
    assert report.backup.name.startswith("memory.spores.json.20261111T120000Z")
    assert report.backup.read_bytes() == before


def test_a_second_run_is_a_no_op(tmp_path: Path) -> None:
    paths, _ids = _old_store(tmp_path)
    assert _migrate(paths).count == 2
    after = paths.intentions_json.read_bytes()
    backups = sorted((tmp_path / ".levain" / "backups" / "intentions").iterdir())

    second = _migrate(paths)

    assert second.count == 0 and second.backup is None and second.refused is None and second.lines() == []
    assert paths.intentions_json.read_bytes() == after
    assert sorted((tmp_path / ".levain" / "backups" / "intentions").iterdir()) == backups      # no second copy


def test_a_store_with_no_open_seed_is_left_untouched_and_unbacked(tmp_path: Path) -> None:
    from anneal_memory.spores import IntentionStore

    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    IntentionStore(paths.intentions_json, id_prefix="intention").add(type="task", text="a loop")
    before = paths.intentions_json.read_bytes()
    report = _migrate(paths)
    assert report.count == 0 and report.backup is None
    assert paths.intentions_json.read_bytes() == before and not (tmp_path / ".levain" / "backups").exists()


def test_no_store_at_all_is_not_an_error(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    report = _migrate(AnnealPaths.from_db(tmp_path / ".levain" / "memory.db"))
    assert report.count == 0 and report.refused is None and report.backup is None


def test_an_unwritable_backup_refuses_and_the_store_is_untouched(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    (tmp_path / ".levain" / "backups").write_text("a file where the backup directory must go")
    before = paths.intentions_json.read_bytes()

    report = _migrate(paths)

    assert report.count == 0 and report.backup is None
    assert report.refused and "backup" in report.refused and "nothing was rewritten" in report.refused
    assert paths.intentions_json.read_bytes() == before
    assert _open(paths, ids["open"])["disposition"] == "seed"


def test_a_corrupt_store_refuses_and_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    paths.intentions_json.write_text("{not json")
    report = _migrate(paths)
    assert report.refused and report.count == 0
    assert paths.intentions_json.read_text() == "{not json" and not (tmp_path / ".levain" / "backups").exists()


def test_a_record_that_moved_since_the_read_is_skipped_not_overwritten(tmp_path: Path, monkeypatch) -> None:
    """The compare-and-set: the ids are read first, then each is updated under the store lock expecting ``seed``."""
    from anneal_memory.spores import IntentionStore
    import levain.intention_migrate as mig

    paths, ids = _old_store(tmp_path)
    plain = IntentionStore(paths.intentions_json)
    real = mig._open_seed_ids

    def stale_read(path):
        found = real(path)
        plain.update(ids["open"], disposition="agenda")           # a concurrent writer re-routes it
        plain.descend(ids["talk"], kind="composted")              # another resolves it
        return found

    monkeypatch.setattr(mig, "_open_seed_ids", stale_read)
    report = _migrate(paths)

    assert report.migrated == [] and sorted(r for r, _why in report.skipped) == sorted([ids["open"], ids["talk"]])
    assert _open(paths, ids["open"])["disposition"] == "agenda"                  # its writer's value stands
    assert any(ln.startswith("  ") and "left as it is" in ln for ln in report.lines())
    assert not report.incomplete          # a record that moved is its writer's: nothing is left behind to migrate


def _fail_updates_of(monkeypatch, fail_ids, exc: BaseException) -> None:
    """Make the migration's guarded store raise ``exc`` from ``update`` for the ids in ``fail_ids`` (the rest go through)."""
    import levain.intentions as li

    real = li.guarded_intention_store

    def build(path):
        store = real(path)
        orig = store.update

        def update(rid, **kw):
            if rid in fail_ids:
                raise exc
            return orig(rid, **kw)

        store.update = update
        return store

    monkeypatch.setattr(li, "guarded_intention_store", build)


@pytest.mark.parametrize("exc", [OSError(5, "I/O error"), ValueError("a field the store rejects")])
def test_a_record_the_store_could_not_update_is_reported_and_leaves_the_migration_incomplete(tmp_path: Path, monkeypatch,
                                                                                            exc) -> None:
    """A store FAULT on one record (an OSError, or any other exception) never escapes mid-update with the backup already
    written, and it is not a plain compare-and-set loss: the row is still an open ``seed``, so the run is incomplete
    (1111-32 L3 round 1, complement 1 / codex 3)."""
    paths, ids = _old_store(tmp_path)
    _fail_updates_of(monkeypatch, {ids["open"]}, exc)

    report = _migrate(paths)

    assert report.migrated == [ids["talk"]] and [r for r, _why in report.skipped] == [ids["open"]]
    assert report.incomplete and report.refused is None
    assert _open(paths, ids["open"])["disposition"] == "seed"           # still there, for the next `levain update`
    assert _open(paths, ids["talk"])["disposition"] == "someday"
    assert any(ids["open"] in ln and "left as it is" in ln for ln in report.lines())


@pytest.mark.parametrize("how", ["read_only_file", "a_directory"])
def test_a_gitignore_that_cannot_be_written_does_not_stop_the_migration(tmp_path: Path, how: str) -> None:
    """The git-ignore line for the backup directory is best-effort: a `.gitignore` the store dir cannot take (read-only, or
    something that is not a file) is no refusal and no traceback, and the migration still runs (1111-32 L3 round 1,
    deepseek 5 claimed it escapes: it does not, `_ensure_gitignored` catches the OSError itself)."""
    import os

    paths, ids = _old_store(tmp_path)
    gi = tmp_path / ".levain" / ".gitignore"
    if how == "read_only_file":
        if os.geteuid() == 0:
            pytest.skip("root writes through a read-only mode")
        gi.write_text("sessions/\n")
        gi.chmod(0o444)
    else:
        gi.mkdir()

    report = _migrate(paths)

    assert report.refused is None and sorted(report.migrated) == sorted([ids["open"], ids["talk"]])
    assert report.backup is not None and report.backup.is_file()


def test_the_report_says_the_provenance_label_degrades(tmp_path: Path) -> None:
    """A label is the sha256 of a signed field value: the rewritten disposition matches no signed fire."""
    from levain.cockpit.consent import value_sha256

    paths, _ids = _old_store(tmp_path)
    text = "\n".join(_migrate(paths).lines())
    assert "unverified" in text and "not a signed edit" in text
    assert value_sha256("seed") != value_sha256("someday")


def test_a_row_stored_under_the_new_prefix_migrates_too(tmp_path: Path) -> None:
    """``intention-N`` ids (anneal 1.0.7's mint) and ``spore-N`` ids live in one store; the discovery is by disposition."""
    from anneal_memory.spores import IntentionStore

    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    old = IntentionStore(paths.intentions_json).add(type="thought", text="old id", disposition="seed")["id"]
    new = IntentionStore(paths.intentions_json, id_prefix="intention").add(type="thought", text="new id", disposition="seed")["id"]
    assert old.startswith("spore-") and new.startswith("intention-")
    assert sorted(_migrate(paths).migrated) == sorted([old, new])
