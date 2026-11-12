"""G1: `levain update` rewrites the OPEN `seed` records of the install's intention store to `someday` (ruling H8), through the
real update path, on an install whose store an a8617e55c release wrote (anneal's plain `IntentionStore`, the old way).

A refused migration (an unwritable backup) is printed and makes the update exit non-zero: it is never swallowed."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.dashboard import AnnealPaths
from levain.install import run_init
from levain.intention_migrate import BACKUP_SUBDIR
from levain.update import run_update
from tests.test_subfolder_hooks import cc_local_layout  # noqa: F401  (fixture)

pytestmark = pytest.mark.usefixtures("cc_local_layout")


@pytest.fixture
def install(tmp_path, capsys, monkeypatch) -> Path:
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    from tests.test_init_answers import _filled

    af = tmp_path / "answers.json"
    af.write_text(json.dumps(_filled(capsys)), encoding="utf-8")
    target = tmp_path / "entity"
    assert run_init(target, "claude-code", force=False, answers_file=af) == 0
    capsys.readouterr()
    return target


def _paths(install: Path) -> AnnealPaths:
    return AnnealPaths.from_db(install / ".levain" / "memory.db")


def _plant_old_store(install: Path) -> dict[str, str]:
    from anneal_memory.spores import IntentionStore

    store = IntentionStore(_paths(install).intentions_json)   # the plain store: accepts the retired spelling
    ids = {
        "open": store.add(type="thought", text="think about the bench", disposition="seed")["id"],
        "talk": store.add(type="thought", text="what should we talk about", disposition="seed", attention="talk")["id"],
        "resolved": store.add(type="thought", text="an old seed, closed", disposition="seed")["id"],
        "agenda": store.add(type="thought", text="an old dated one", disposition="agenda", target="2030-01-01")["id"],
    }
    store.descend(ids["resolved"], kind="composted")
    return ids


def _dispositions(install: Path) -> dict[str, str]:
    doc = json.loads(_paths(install).intentions_json.read_text(encoding="utf-8"))
    rows = [r for key, v in doc.items() if isinstance(v, list) for r in v if isinstance(r, dict) and "id" in r]
    return {r["id"]: r["disposition"] for r in rows}


def _backups(install: Path) -> list[Path]:
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    return sorted(d.iterdir()) if d.is_dir() else []


def test_update_rewrites_open_seeds_to_someday_with_a_backup_and_says_so(install):
    ids = _plant_old_store(install)
    before = _paths(install).intentions_json.read_bytes()
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    got = _dispositions(install)
    assert got[ids["open"]] == "someday" and got[ids["talk"]] == "someday"
    assert got[ids["resolved"]] == "seed"               # a resolved record keeps its history
    assert got[ids["agenda"]] == "agenda"
    backups = _backups(install)
    assert len(backups) == 1 and backups[0].read_bytes() == before
    text = "\n".join(lines)
    assert "2 open seed record(s) rewritten to someday" in text and str(backups[0]) in text


def test_a_second_update_is_a_no_op(install):
    _plant_old_store(install)
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    store_bytes = _paths(install).intentions_json.read_bytes()
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    assert _paths(install).intentions_json.read_bytes() == store_bytes
    assert len(_backups(install)) == 1
    assert not any("someday" in ln and "rewritten" in ln for ln in lines)


def test_an_install_with_no_open_seed_gets_no_backup_and_no_line(install):
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    assert _backups(install) == []
    assert not any("seed" in ln and "someday" in ln for ln in lines)


def test_a_refused_backup_surfaces_and_the_update_exits_non_zero(install):
    _plant_old_store(install)
    before = _paths(install).intentions_json.read_bytes()
    blocker = install / ".levain" / BACKUP_SUBDIR[0]
    blocker.write_text("a file where the backups directory must be", encoding="utf-8")
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 1
    assert _paths(install).intentions_json.read_bytes() == before     # nothing rewritten
    text = "\n".join(lines)
    assert "was not done" in text and "backup could not be written" in text


@pytest.mark.parametrize("exc", [OSError(5, "I/O error"), ValueError("a field the store rejects")])
def test_a_record_the_store_could_not_update_ends_the_update_non_zero_and_stays_a_seed(install, monkeypatch, exc):
    import levain.intentions as li
    from levain.update import _SEEDS_REFUSED_TAIL

    ids = _plant_old_store(install)
    real = li.guarded_intention_store

    def build(path):
        store = real(path)
        orig = store.update

        def update(rid, **kw):
            if rid == ids["open"]:
                raise exc
            return orig(rid, **kw)

        store.update = update
        return store

    monkeypatch.setattr(li, "guarded_intention_store", build)
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 1
    got = _dispositions(install)
    assert got[ids["open"]] == "seed" and got[ids["talk"]] == "someday"
    text = "\n".join(lines)
    assert ids["open"] in text and "left as it is" in text and _SEEDS_REFUSED_TAIL in text


def test_a_dry_run_says_what_it_would_rewrite_and_changes_nothing(install):
    _plant_old_store(install)
    before = _paths(install).intentions_json.read_bytes()
    lines: list[str] = []
    run_update(install, dry_run=True, no_pip=True, emit=lines.append)
    assert _paths(install).intentions_json.read_bytes() == before
    assert _backups(install) == []
    assert "2 open seed record(s) would be rewritten to someday" in "\n".join(lines)


@pytest.mark.parametrize("how", ["malformed_json", "not_a_json_object", "a_directory"])
def test_a_dry_run_on_a_store_it_cannot_read_says_it_cannot_tell_and_exits_non_zero(install, how):
    """`open_seed_ids` used to turn every read failure into `[]`, so the dry run printed "nothing to reconcile" and exited
    0 about a store it could not read (1111-32 L3 round 1, codex 6)."""
    target = _paths(install).intentions_json
    if how == "malformed_json":
        target.write_text("{not json", encoding="utf-8")
    elif how == "not_a_json_object":
        target.write_text("[1, 2]", encoding="utf-8")
    else:
        target.mkdir()
    lines: list[str] = []
    assert run_update(install, dry_run=True, no_pip=True, emit=lines.append) == 1
    text = "\n".join(lines)
    assert "cannot be read" in text and "cannot tell" in text and "nothing to reconcile" not in text


def test_a_dry_run_with_no_intention_store_file_still_has_nothing_to_reconcile(install):
    assert not _paths(install).intentions_json.exists()
    lines: list[str] = []
    assert run_update(install, dry_run=True, no_pip=True, emit=lines.append) == 0
    assert "nothing to reconcile" in "\n".join(lines)


def test_the_migration_also_runs_on_the_reconcile_path(install, monkeypatch):
    """A drifted pack takes the long path through `_run_update`; the migration is there too."""
    from types import SimpleNamespace

    import levain.update as upd

    ids = _plant_old_store(install)
    monkeypatch.setattr(upd.manifest, "compute_pack_drift", lambda recorded: [SimpleNamespace(drifted=True, added=(), modified=(), removed=())])
    monkeypatch.setattr(upd.manifest, "read_pack_locks", lambda inst: [object()])
    monkeypatch.setattr(upd.reconcile, "run_pack_reconcile", lambda inst, dry_run, emit: ([], False, False))
    run_update(install, no_pip=True, emit=lambda s: None)
    assert _dispositions(install)[ids["open"]] == "someday"
