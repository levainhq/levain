"""1111-32 fix lane: the seed -> someday migration that `levain update` runs (levain.intention_migrate), three review findings.

G2 (MED): when `levain update` HOLDS levain's files for review (`AdapterRefresh.review` non-empty), the installed hooks may
still be the old ones, whose `_NON_COGNITION_DISPOSITIONS` names `seed` and not `someday` and which fail open on an unknown
disposition. The migration is left for the next update: the store is untouched, the update says so. Deferral is safe
because every read tuple keeps `seed` and the migration is idempotent."""
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


def _plant_open_seed(install: Path) -> str:
    from anneal_memory.spores import IntentionStore

    store = IntentionStore(_paths(install).intentions_json)   # the plain store: accepts the retired spelling, as an old release
    return store.add(type="thought", text="think about the bench", disposition="seed")["id"]


def _backups(install: Path) -> list[Path]:
    d = install / ".levain" / Path(*BACKUP_SUBDIR)
    return sorted(d.iterdir()) if d.is_dir() else []


def _hold_a_hook(monkeypatch) -> None:
    """The real adapter refresh, then one hook file held for review (what H9's coupled hold or an edited hook leaves)."""
    import levain.update as upd

    real = upd.refresh_adapter

    def held(install, **kw):
        out = real(install, **kw)
        out.review.append("activation/hooks/_levain_hook.py")
        return out

    monkeypatch.setattr(upd, "refresh_adapter", held)


def test_g2_held_hooks_leave_the_migration_for_the_next_update(install, monkeypatch):
    sid = _plant_open_seed(install)
    before = _paths(install).intentions_json.read_bytes()
    _hold_a_hook(monkeypatch)
    lines: list[str] = []
    rc = run_update(install, no_pip=True, emit=lines.append)
    assert rc == 1                                                   # the held file already makes the update non-zero
    assert _paths(install).intentions_json.read_bytes() == before    # store untouched
    assert _backups(install) == []                                   # and not even copied
    text = "\n".join(lines)
    assert "left for the next `levain update`" in text and "held" in text, text
    assert "record(s) rewritten to someday" not in text and "was not done" not in text
    assert json.loads(before)["spores"][0]["id"] == sid


def test_g2_once_nothing_is_held_the_next_update_migrates(install, monkeypatch):
    sid = _plant_open_seed(install)
    with monkeypatch.context() as m:
        _hold_a_hook(m)
        run_update(install, no_pip=True, emit=lambda s: None)
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 0
    doc = json.loads(_paths(install).intentions_json.read_text(encoding="utf-8"))
    assert {r["id"]: r["disposition"] for r in doc["spores"]}[sid] == "someday"
    assert "1 open seed record(s) rewritten to someday" in "\n".join(lines)


def test_g2_a_held_dry_run_says_the_migration_would_wait(install, monkeypatch):
    _plant_open_seed(install)
    _hold_a_hook(monkeypatch)
    lines: list[str] = []
    run_update(install, dry_run=True, no_pip=True, emit=lines.append)
    text = "\n".join(lines)
    assert "would be left for the next `levain update`" in text and "would be rewritten" not in text, text


def _anneal_before_1_0_7(monkeypatch) -> None:
    """anneal-memory 1.0.4's `anneal_memory.spores` has `SporeError` / `SporeStore` and no `Intention*` alias: simulated by
    making the `Intention*` names unimportable (no real downgrade)."""
    import anneal_memory.spores as am_spores

    import levain.intentions as lint

    for name in ("IntentionError", "IntentionStore", "IntentionApply"):
        monkeypatch.delattr(am_spores, name, raising=False)
    monkeypatch.setattr(lint, "_GUARDED", None)                     # the guarded class is built lazily from IntentionStore


def test_med1_an_anneal_below_the_floor_refuses_before_any_backup(install, monkeypatch):
    """L1 MED-1: the imports ran AFTER the backup, so an anneal below 1.0.7 crashed `levain update` with a raw ImportError,
    lock unrecorded, one more backup per retry. Now: a refusal naming the floor, rc 1, no backup, store untouched."""
    _plant_open_seed(install)
    before = _paths(install).intentions_json.read_bytes()
    _anneal_before_1_0_7(monkeypatch)
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 1
    assert _paths(install).intentions_json.read_bytes() == before
    assert _backups(install) == []
    text = "\n".join(lines)
    assert "was not done" in text and "anneal-memory>=1.0.7" in text, text
    assert (install / ".levain" / "manifest.json").is_file()        # the lock is still recorded (the refusal path ran)


def test_med1_the_migration_itself_refuses_without_a_backup(tmp_path, monkeypatch):
    from levain.intention_migrate import migrate_seed_to_someday

    from anneal_memory.spores import IntentionStore

    paths = AnnealPaths.from_db(tmp_path / "memory.db")
    IntentionStore(paths.intentions_json).add(type="thought", text="x", disposition="seed")
    before = paths.intentions_json.read_bytes()
    _anneal_before_1_0_7(monkeypatch)
    report = migrate_seed_to_someday(paths)
    assert report.refused and "anneal-memory>=1.0.7" in report.refused and report.backup is None
    assert not (tmp_path / "backups").exists() and paths.intentions_json.read_bytes() == before


def test_e2_the_migration_backup_is_git_ignored(install):
    """1111-32 E2: the copy of the private store under .levain/backups/intentions/ is covered by .levain/.gitignore."""
    import subprocess

    _plant_open_seed(install)
    gi = install / ".levain" / ".gitignore"
    gi.write_text("\n".join(ln for ln in (gi.read_text(encoding="utf-8").splitlines() if gi.is_file() else [])
                            if ln.strip() != "backups/") + "\n", encoding="utf-8")   # an install that never backed up a seed
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    (backup,) = _backups(install)
    assert "backups/" in gi.read_text(encoding="utf-8").splitlines()
    subprocess.run(["git", "init", "-q", str(install)], check=True)
    r = subprocess.run(["git", "-C", str(install), "check-ignore", "-q", str(backup)])
    assert r.returncode == 0, "the store copy would be committable"


def test_e2_an_install_with_nothing_to_migrate_gets_no_line(install):
    gi = install / ".levain" / ".gitignore"
    before = gi.read_text(encoding="utf-8") if gi.is_file() else None
    assert run_update(install, no_pip=True, emit=lambda s: None) == 0
    assert (gi.read_text(encoding="utf-8") if gi.is_file() else None) == before


def test_g2_the_reconcile_path_also_defers(install, monkeypatch):
    """A drifted pack takes the long path through `_run_update`; the deferral is there too."""
    from types import SimpleNamespace

    import levain.update as upd

    _plant_open_seed(install)
    before = _paths(install).intentions_json.read_bytes()
    monkeypatch.setattr(upd.manifest, "compute_pack_drift", lambda recorded: [SimpleNamespace(drifted=True, added=(), modified=(), removed=())])
    monkeypatch.setattr(upd.manifest, "read_pack_locks", lambda inst: [object()])
    monkeypatch.setattr(upd.reconcile, "run_pack_reconcile", lambda inst, dry_run, emit: ([], False, False))
    _hold_a_hook(monkeypatch)
    lines: list[str] = []
    assert run_update(install, no_pip=True, emit=lines.append) == 1
    assert _paths(install).intentions_json.read_bytes() == before
    assert "left for the next `levain update`" in "\n".join(lines)
