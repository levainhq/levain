"""The loop fold's ledger: ONE reader for the key-absent window and the revert (L3 r2, board item levain#4)."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.dashboard import AnnealPaths
from levain.intention_migrate import BACKUP_SUBDIR, LEDGER_PREFIX
from tests.test_loopfold_migrate import (  # noqa: F401  (install and cc_local_layout are fixtures)
    NOW, _backups, _ipaths, _ledger_doc, _ledgers, _migrate, _old_store, _open, _rewrite_ledger, _snapshot_dir, _store,
    cc_local_layout, install,
)


def _complete_first_run(tmp_path: Path):
    paths, ids = _old_store(tmp_path)
    report = _migrate(paths)
    assert report.ledger is not None and not report.incomplete
    return paths, ids, report


def _plant_absent(paths: AnnealPaths) -> str:
    rid = _store(paths).add(type="task", text="a key-absent row")["id"]
    assert "disposition" not in _open(paths, rid)
    return rid


# --- a ledger that is not a valid one for THIS store never closes the window ---

def test_a_ledger_of_another_store_in_a_shared_backup_dir_never_closes_the_window(tmp_path: Path) -> None:
    shared = tmp_path / "shared-backups"
    a_root, b_root = tmp_path / "a", tmp_path / "b"
    a_root.mkdir()
    b_root.mkdir()
    a_paths, _a_ids = _old_store(a_root)
    assert _migrate(a_paths, backup_dir=shared).ledger is not None          # A's completed ledger now sits in the shared dir
    b_paths, b_ids = _old_store(b_root)
    again = _migrate(b_paths, backup_dir=shared)
    assert b_ids["plain"] in again.migrated and _open(b_paths, b_ids["plain"])["disposition"] == "someday"


def test_a_stray_unmigrated_zero_file_does_not_close_the_window(tmp_path: Path) -> None:
    paths, ids = _old_store(tmp_path)
    d = tmp_path / ".levain" / Path(*BACKUP_SUBDIR)
    d.mkdir(parents=True)
    (d / f"{LEDGER_PREFIX}stray.json").write_text(json.dumps({"unmigrated": 0}), encoding="utf-8")
    assert ids["plain"] in _migrate(paths).migrated


@pytest.mark.parametrize("flag", [False, True, "0", 0.5, None])
def test_an_unmigrated_that_is_not_an_int_zero_does_not_close_the_window(tmp_path: Path, flag) -> None:
    paths, ids, report = _complete_first_run(tmp_path)
    _rewrite_ledger(report.ledger, unmigrated=flag)
    late = _plant_absent(paths)
    assert late in _migrate(paths).migrated


def test_a_ledger_without_the_new_schema_fields_does_not_close_the_window(tmp_path: Path) -> None:
    paths, ids, report = _complete_first_run(tmp_path)
    doc = _ledger_doc(report.ledger)
    del doc["store"]
    report.ledger.write_text(json.dumps(doc), encoding="utf-8")
    late = _plant_absent(paths)
    assert late in _migrate(paths).migrated


def test_the_ledger_records_the_store_as_a_realpath(tmp_path: Path) -> None:
    paths, ids, report = _complete_first_run(tmp_path)
    assert _ledger_doc(report.ledger)["store"] == os.path.realpath(paths.intentions_json)


@pytest.mark.parametrize("prior", [{"intention-001": "agenda"}, [1], {"intention-001": 7}, {"intention-001": ["loop"]}])
def test_the_revert_refuses_a_ledger_whose_prior_values_are_not_what_a_loop_held(tmp_path: Path, prior) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids, report = _complete_first_run(tmp_path)
    _rewrite_ledger(report.ledger, migrated=["intention-001"], prior=prior)
    before = paths.intentions_json.read_bytes()
    back = revert_loop_migration(paths, report.ledger)
    assert back.refused and back.restored == [] and paths.intentions_json.read_bytes() == before


# --- a run that finds nothing still closes the window, once ---

def test_a_first_run_that_finds_nothing_writes_a_completed_ledger_so_the_entitys_first_add_is_left_alone(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    _store(paths).add(type="thought", text="a someday", disposition="someday")
    first = _migrate(paths)
    assert first.migrated == [] and first.backup is None and first.ledger is not None and _backups(tmp_path) == []
    doc = _ledger_doc(first.ledger)
    assert doc["migrated"] == [] and doc["prior"] == {} and doc["unmigrated"] == 0 and doc["backup"] is None
    late = _plant_absent(paths)
    names, before = _snapshot_dir(tmp_path), paths.intentions_json.read_bytes()
    again = _migrate(paths)
    assert again.migrated == [] and again.backup is None and again.ledger is None
    assert _snapshot_dir(tmp_path) == names and paths.intentions_json.read_bytes() == before
    assert "disposition" not in _open(paths, late)


def test_an_unreadable_store_writes_no_ledger_and_a_later_no_op_run_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / ".levain").mkdir()
    paths = AnnealPaths.from_db(tmp_path / ".levain" / "memory.db")
    paths.intentions_json.write_text("{not json", encoding="utf-8")
    bad = _migrate(paths)
    assert bad.refused and bad.ledger is None and _ledgers(tmp_path) == []
    paths.intentions_json.unlink()
    _store(paths).add(type="thought", text="a someday", disposition="someday")
    assert _migrate(paths).ledger is not None
    names = _snapshot_dir(tmp_path)
    assert _migrate(paths).ledger is None and _snapshot_dir(tmp_path) == names


@pytest.mark.usefixtures("cc_local_layout")
def test_update_on_a_fresh_store_closes_the_window_before_the_entitys_first_add(install) -> None:
    from levain.update import run_update

    assert run_update(install, no_pip=True, emit=lambda s: None) == 0       # no loops to rewrite at all
    _store(_ipaths(install)).add(type="task", text="the entity's first add")
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    names, before = sorted(p.name for p in d.iterdir()), _ipaths(install).intentions_json.read_bytes()
    lines: list[str] = []
    assert run_update(install, dry_run=True, no_pip=True, emit=lines.append) == 0
    assert "nothing to reconcile" in "\n".join(lines)
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    assert sorted(p.name for p in d.iterdir()) == names and _ipaths(install).intentions_json.read_bytes() == before
    assert not [p for p in d.iterdir() if not p.name.startswith(LEDGER_PREFIX)]            # no store copy was ever taken


# --- a revert marks its ledger ---

def test_a_reverted_ledger_stops_closing_the_window_and_cannot_be_reverted_twice(tmp_path: Path) -> None:
    from levain.intention_migrate import revert_loop_migration

    paths, ids, report = _complete_first_run(tmp_path)
    back = revert_loop_migration(paths, report.ledger)
    assert back.refused is None and back.restored and back.ledger_error is None
    assert "reverted" in _ledger_doc(report.ledger)
    assert all("disposition" not in _open(paths, ids[k]) for k in ("plain", "parked", "past"))
    again = revert_loop_migration(paths, report.ledger)
    assert again.refused and "already reverted" in again.refused and again.restored == []
    second = _migrate(paths)                          # the restored key-absent rows are loops again to this release
    assert ids["plain"] in second.migrated and ids["parked"] in second.migrated


def test_the_plan_skips_a_row_the_policy_cannot_read_instead_of_raising(tmp_path: Path, monkeypatch) -> None:
    import levain.intention_migrate as m

    paths, ids = _old_store(tmp_path)
    real = m.q2_past_next_policy

    def flaky(row, now):
        if row.get("id") == ids["plain"]:
            raise TypeError("a bad row")
        return real(row, now)

    monkeypatch.setattr(m, "q2_past_next_policy", flaky)
    plan = m.plan_loop_migration(paths, now=NOW)
    assert ids["plain"] in plan.ids and ids["past"] in [r for r, _n, _w in plan.past_dated]
