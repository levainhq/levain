"""levain#142: `levain update` brings an install's rendered seed/origin.md to the levain#106 wording, once.

The fixture is NOT a fresh install: the install's origin.md is rendered from the template as it was before
levain#106 (tests/fixtures/origin_pre_levain106.md, `git show e9b7055b3~1:levain/templates/seed/origin.md`) with
the install's own recorded answers, which is the file an existing install holds.
"""

from __future__ import annotations

import json
from pathlib import Path

import levain
import levain.install as install_mod
import pytest

from levain import manifest, update
from levain.install import ORIGIN_MIGRATION_REL, read_answers, refresh_adapter
from levain.manifest import CompatSet, InstalledSet
from levain.interview import parse_template, render_template
from tests.test_update_refresh import make_install  # noqa: F401  (fixture)

OLD_TEMPLATE = Path(__file__).parent / "fixtures" / "origin_pre_levain106.md"
OLD_PHRASES = ("your training pulls you wrong", "Resist it")


def _refresh(install: Path, *, apply: bool = True):
    lines: list[str] = []
    r = refresh_adapter(install, apply=apply, emit=lines.append)
    return r, "\n".join(lines)


def _as_pre_106_install(install: Path) -> str:
    """Give ``install`` the origin.md an install made before levain#106 holds; returns it."""
    spec = parse_template(OLD_TEMPLATE)
    text = render_template(spec, read_answers(install))
    assert all(p in text for p in OLD_PHRASES)
    (install / "seed" / "origin.md").write_text(text, encoding="utf-8")
    install.joinpath(*ORIGIN_MIGRATION_REL).unlink(missing_ok=True)
    return text


def _record(install: Path) -> dict:
    return json.loads(install.joinpath(*ORIGIN_MIGRATION_REL).read_text())


def test_the_worktrees_levain_is_the_one_imported():
    assert Path(levain.__file__).resolve().parent == Path(install_mod.__file__).resolve().parent


def test_a_pre_106_install_gets_the_new_wording_once_and_the_old_file_is_kept(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    r, out = _refresh(install)
    new = (install / "seed" / "origin.md").read_text(encoding="utf-8")
    assert not any(p in new for p in OLD_PHRASES)
    assert "shapes your drift takes in the work" in new and "Say what you actually think, plainly" in new
    # it is exactly what the current template renders for the install's own answers
    assert new == render_template(parse_template(
        Path(install_mod.__file__).parent / "templates" / "seed" / "origin.md"), read_answers(install))
    backups = list((install / ".levain" / "backups" / "seed").glob("origin.md.*"))
    assert [b.read_text(encoding="utf-8") for b in backups] == [old]
    assert "seed/origin.md: unedited, brought to this release's wording" in out
    assert _record(install)["outcome"] == "migrated"
    # the second update: nothing to do, nothing said, nothing backed up again
    r2, out2 = _refresh(install)
    assert "origin.md" not in out2
    assert len(list((install / ".levain" / "backups" / "seed").glob("origin.md.*"))) == 1


def test_a_dry_run_changes_nothing_and_records_nothing(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    r, out = _refresh(install, apply=False)
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == old
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()
    assert "an update would bring it" in out
    assert r.refreshed == ["seed/origin.md"] and not (install / ".levain" / "backups").exists()


def test_a_hand_edited_origin_is_left_alone_and_said_so_once(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    edited = old.replace("Welcome.", "Welcome, my own way.")
    assert edited != old
    (install / "seed" / "origin.md").write_text(edited, encoding="utf-8")
    _r, out = _refresh(install)
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == edited
    assert "seed/origin.md: yours" in out and "left as it is" in out
    assert not (install / ".levain" / "backups" / "seed").exists()
    assert _record(install)["outcome"] == "kept-edited"
    _r2, out2 = _refresh(install)
    assert "origin.md" not in out2
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == edited


def test_an_edit_to_one_of_the_two_old_sentences_is_kept_whole(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    edited = old.replace("Resist it, and", "Resist it hard, and")
    assert edited != old
    (install / "seed" / "origin.md").write_text(edited, encoding="utf-8")
    _refresh(install)
    # the first sentence is not touched either: all-or-nothing, never a half-migrated file
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == edited


def test_an_origin_already_on_the_new_wording_is_recorded_and_untouched(make_install):
    install = make_install()
    before = (install / "seed" / "origin.md").read_text(encoding="utf-8")
    _r, out = _refresh(install)
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == before
    assert "origin.md" not in out
    assert _record(install)["outcome"] == "current"
    assert not (install / ".levain" / "backups" / "seed").exists()


def test_a_recorded_install_is_never_migrated_again(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    _refresh(install)
    (install / "seed" / "origin.md").write_text(old, encoding="utf-8")  # the operator puts the old text back
    _r, out = _refresh(install)
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == old
    assert "origin.md" not in out


def test_an_unreadable_record_write_is_tried_again_next_update(make_install, monkeypatch):
    install = make_install()
    _as_pre_106_install(install)
    real = install_mod._atomic_write_text

    def deny_record(target, payload):
        if Path(target).name == ORIGIN_MIGRATION_REL[-1]:
            raise OSError("disk full")
        return real(target, payload)

    monkeypatch.setattr(install_mod, "_atomic_write_text", deny_record)
    _r, out = _refresh(install)
    assert "could not record the origin.md migration" in out
    monkeypatch.undo()
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


# ---- the fix lane (levain#142 L3 round 1) ----

OLD_1 = "above all the specific ways your training pulls you wrong (see *How We Work*)."
NEW_1 = "above all the specific shapes your drift takes in the work (see *How We Work*)."


def _origin(install: Path) -> Path:
    return install / "seed" / "origin.md"


@pytest.fixture
def declared(monkeypatch):
    d = CompatSet(levain="0.3.4", anneal="0.9.5", schema="partnership")
    monkeypatch.setattr(manifest, "declared_set", lambda: d)
    monkeypatch.setattr(manifest, "discover_installed_set", lambda *a, **k: InstalledSet(
        levain="0.3.4", anneal="0.9.5", schema="partnership", migrate_acked="0.9.4", pending_count=4))
    return d


def test_an_ack_with_pending_proposals_is_skipped_when_the_migration_changed_origin(
        make_install, declared, monkeypatch):
    install = make_install()
    _as_pre_106_install(install)
    calls = []
    monkeypatch.setattr(update, "_run_anneal", lambda store, ap, args, **k: calls.append(args) or (True, "ok"))
    lines: list[str] = []
    update.run_update(install, ack=True, emit=lines.append, confirm=lambda _p: True)
    assert not any(a[:2] == ["migrate", "ack"] for a in calls)
    assert any("--ack SKIPPED" in ln and "refreshed" in ln for ln in lines)
    assert _record(install)["outcome"] == "migrated"


def test_a_migration_only_dry_run_is_a_plan_not_nothing_to_do(make_install, declared, monkeypatch):
    install = make_install()
    _as_pre_106_install(install)
    monkeypatch.setattr(update, "_run_anneal", lambda *a, **k: (True, "ok"))
    lines: list[str] = []
    rc = update.run_update(install, dry_run=True, emit=lines.append, confirm=lambda _p: True)
    out = "\n".join(lines)
    assert "nothing to reconcile mechanically" not in out
    assert "WOULD do" in out and rc == 1


def test_a_failed_replace_is_in_review_and_records_nothing(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    real = install_mod._atomic_write_text

    def deny(target, payload):
        if Path(target).name == "origin.md":
            raise OSError("read-only")
        return real(target, payload)

    monkeypatch.setattr(install_mod, "_atomic_write_text", deny)
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "seed/origin.md" not in r.refreshed
    assert "could not be migrated" in out
    assert _origin(install).read_text(encoding="utf-8") == old
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


def test_an_unreadable_file_is_in_review(make_install):
    install = make_install()
    _as_pre_106_install(install)
    _origin(install).write_bytes(b"\xff\xfe not utf8")
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "could not be read" in out
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


def test_a_hybrid_one_sentence_hand_replaced_is_kept(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    hybrid = old.replace(OLD_1, NEW_1)
    assert hybrid != old and "Resist it" in hybrid
    _origin(install).write_text(hybrid, encoding="utf-8")
    r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == hybrid
    assert _record(install)["outcome"] == "kept-edited" and r.refreshed == []
    assert "older wording" in out


def test_a_kept_edited_file_without_the_old_wording_does_not_claim_it(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    edited = old.replace(OLD_1, "my own line.").replace("Resist it", "Do not")
    _origin(install).write_text(edited, encoding="utf-8")
    _r, out = _refresh(install)
    assert "yours" in out and "older wording" not in out


def test_a_transient_check_failure_records_nothing_and_retries(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    import levain.interview as interview

    real = interview.parse_template

    def boom(path):
        raise OSError("template unreadable")

    monkeypatch.setattr(interview, "parse_template", boom)
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "could not be checked" in out
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()
    assert _origin(install).read_text(encoding="utf-8") == old
    monkeypatch.setattr(interview, "parse_template", real)
    _r2, _out2 = _refresh(install)
    assert _record(install)["outcome"] == "migrated"


def test_a_save_between_the_read_and_the_replace_is_not_overwritten(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    saved = old + "\nan operator line\n"
    real = install_mod._origin_backup

    def backup_then_operator_saves(inst, raw):
        b = real(inst, raw)
        _origin(install).write_text(saved, encoding="utf-8")
        return b

    monkeypatch.setattr(install_mod, "_origin_backup", backup_then_operator_saves)
    r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == saved
    assert "edited while the update ran" in out and r.refreshed == []
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


def test_a_symlinked_seed_dir_is_left_whole(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    real_dir = tmp_path / "shared_seed"
    (install / "seed").rename(real_dir)
    (install / "seed").symlink_to(real_dir)
    r, out = _refresh(install)
    assert (real_dir / "origin.md").read_text(encoding="utf-8") == old
    assert "is a link" in out and not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


def test_a_symlinked_backups_dir_is_left_whole(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (install / ".levain" / "backups").mkdir(parents=True, exist_ok=True)
    (install / ".levain" / "backups").rmdir()
    (install / ".levain" / "backups").symlink_to(elsewhere)
    _r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == old
    assert "is a link" in out and not list(elsewhere.iterdir())


def test_a_missing_origin_is_a_quiet_no_op(make_install):
    install = make_install()
    _origin(install).unlink()
    r, out = _refresh(install)
    assert "seed/origin.md:" not in out and "seed/origin.md" not in r.review
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


def test_a_failed_backup_leaves_the_file_whole_and_in_review(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)

    def no_backup(inst, raw):
        raise OSError("no space")

    monkeypatch.setattr(install_mod, "_origin_backup", no_backup)
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "could not be migrated" in out
    assert _origin(install).read_text(encoding="utf-8") == old


def test_a_retry_reuses_the_backup_whose_bytes_equal_the_file(make_install, monkeypatch):
    install = make_install()
    _as_pre_106_install(install)
    real = install_mod._atomic_write_text

    def deny(target, payload):
        if Path(target).name == "origin.md":
            raise OSError("read-only")
        return real(target, payload)

    monkeypatch.setattr(install_mod, "_atomic_write_text", deny)
    _refresh(install)
    _refresh(install)
    assert len(list((install / ".levain" / "backups" / "seed").glob("origin.md.*"))) == 1


def test_a_record_that_is_not_a_json_outcome_counts_as_no_record_and_is_said(make_install):
    install = make_install()
    _as_pre_106_install(install)
    rec = install.joinpath(*ORIGIN_MIGRATION_REL)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text("", encoding="utf-8")
    _r, out = _refresh(install)
    assert out.count("is not usable") == 1
    assert _record(install)["outcome"] == "migrated"


def test_a_new_backup_is_private_and_its_folder_is_0700(make_install):
    import stat

    install = make_install()
    _as_pre_106_install(install)
    folder = install / ".levain" / "backups" / "seed"
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    _origin(install).chmod(0o600)
    _refresh(install)
    (backup,) = folder.glob("origin.md.*")
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_a_symlinked_gitignore_is_never_written_through(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    elsewhere = tmp_path / "dotfile_gitignore"
    elsewhere.write_text("mine\n", encoding="utf-8")
    gi = install / ".levain" / ".gitignore"
    gi.unlink(missing_ok=True)
    gi.symlink_to(elsewhere)
    r, out = _refresh(install)
    assert elsewhere.read_text(encoding="utf-8") == "mine\n"
    assert _origin(install).read_text(encoding="utf-8") == old
    assert ".gitignore is a link" in out and "seed/origin.md" in r.review
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()


def test_a_record_behind_a_symlinked_migrations_dir_is_not_trusted(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    shared = tmp_path / "shared_migrations"
    shared.mkdir()
    (shared / ORIGIN_MIGRATION_REL[-1]).write_text(json.dumps({"schema": 1, "outcome": "current"}), encoding="utf-8")
    mig = install / ".levain" / "migrations"
    if mig.exists():
        mig.rmdir()
    mig.symlink_to(shared)
    r, out = _refresh(install)
    assert "is a link" in out and "seed/origin.md" in r.review
    assert _origin(install).read_text(encoding="utf-8") == old


def test_a_record_with_another_schema_is_not_a_record(make_install):
    install = make_install()
    _as_pre_106_install(install)
    rec = install.joinpath(*ORIGIN_MIGRATION_REL)
    rec.parent.mkdir(parents=True, exist_ok=True)
    rec.write_text(json.dumps({"schema": 2, "outcome": "current"}), encoding="utf-8")
    _r, out = _refresh(install)
    assert out.count("is not usable") == 1
    assert _record(install)["outcome"] == "migrated"


def test_a_non_regular_origin_is_in_review(make_install):
    install = make_install()
    _origin(install).unlink()
    _origin(install).mkdir()
    r, out = _refresh(install)
    assert "seed/origin.md" in r.review and "not a regular file" in out


def test_a_save_during_the_run_is_in_review_and_the_orphan_backup_is_removed(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    real = install_mod._origin_backup

    def backup_then_operator_saves(inst, raw):
        b = real(inst, raw)
        _origin(install).write_text(old + "\nedit\n", encoding="utf-8")
        return b

    monkeypatch.setattr(install_mod, "_origin_backup", backup_then_operator_saves)
    r, out = _refresh(install)
    assert "seed/origin.md" in r.review and r.refreshed == []
    assert list((install / ".levain" / "backups" / "seed").glob("origin.md.*")) == []


def test_an_odd_origin_that_raises_outside_the_usual_errors_fails_this_file_only(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    import levain.answers as answers

    def boom(*a, **k):
        raise KeyError("odd")

    monkeypatch.setattr(answers, "extract_answers", boom)
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "could not be checked" in out
    assert _origin(install).read_text(encoding="utf-8") == old
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()
