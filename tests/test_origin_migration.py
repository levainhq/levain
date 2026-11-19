"""levain#142: `levain update` brings an install's rendered seed/origin.md to the levain#106 wording.

Stateless: no record, no backup. The swap applies only while the file still matches the pre-#106 rendering, so it
never runs twice. The fixture is NOT a fresh install: the install's origin.md is rendered from the template as it was
before levain#106 (tests/fixtures/origin_pre_levain106.md, `git show e9b7055b3~1:levain/templates/seed/origin.md`)
with the install's own recorded answers, which is the file an existing install holds.
"""

from __future__ import annotations

from pathlib import Path

import levain
import levain.install as install_mod
import pytest

from levain import manifest, update
from levain.install import read_answers, refresh_adapter
from levain.manifest import CompatSet, InstalledSet
from levain.interview import parse_template, render_template
from tests.test_update_refresh import make_install  # noqa: F401  (fixture)

OLD_TEMPLATE = Path(__file__).parent / "fixtures" / "origin_pre_levain106.md"
OLD_PHRASES = ("your training pulls you wrong", "Resist it")
OLD_1 = "above all the specific ways your training pulls you wrong (see *How We Work*)."
NEW_1 = "above all the specific shapes your drift takes in the work (see *How We Work*)."


def _refresh(install: Path, *, apply: bool = True):
    lines: list[str] = []
    r = refresh_adapter(install, apply=apply, emit=lines.append)
    return r, "\n".join(lines)


def _origin(install: Path) -> Path:
    return install / "seed" / "origin.md"


def _as_pre_106_install(install: Path) -> str:
    """Give ``install`` the origin.md an install made before levain#106 holds; returns it."""
    spec = parse_template(OLD_TEMPLATE)
    text = render_template(spec, read_answers(install))
    assert all(p in text for p in OLD_PHRASES)
    _origin(install).write_text(text, encoding="utf-8")
    return text


def _no_state(install: Path) -> bool:
    return not (install / ".levain" / "migrations").exists() and not (install / ".levain" / "backups").exists()


def test_the_worktrees_levain_is_the_one_imported():
    assert Path(levain.__file__).resolve().parent == Path(install_mod.__file__).resolve().parent


def test_a_pre_106_install_gets_the_new_wording_and_the_second_run_is_a_no_op(make_install):
    install = make_install()
    _as_pre_106_install(install)
    _r, out = _refresh(install)
    new = _origin(install).read_text(encoding="utf-8")
    assert not any(p in new for p in OLD_PHRASES)
    assert "shapes your drift takes in the work" in new and "Say what you actually think, plainly" in new
    assert new == render_template(parse_template(
        Path(install_mod.__file__).parent / "templates" / "seed" / "origin.md"), read_answers(install))
    assert "seed/origin.md: unedited, brought to this release's wording" in out
    assert _no_state(install)  # no record, no backup
    r2, out2 = _refresh(install)
    assert "origin.md" not in out2 and r2.refreshed == [] and r2.review == []
    assert _origin(install).read_text(encoding="utf-8") == new


def test_a_dry_run_changes_nothing(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    r, out = _refresh(install, apply=False)
    assert _origin(install).read_text(encoding="utf-8") == old
    assert "an update would bring it" in out
    assert r.refreshed == ["seed/origin.md"] and _no_state(install)


def test_a_hand_edited_origin_is_kept_and_said_so_on_each_run(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    edited = old.replace("Welcome.", "Welcome, my own way.")
    assert edited != old
    _origin(install).write_text(edited, encoding="utf-8")
    for _ in range(2):
        r, out = _refresh(install)
        assert _origin(install).read_text(encoding="utf-8") == edited
        assert "seed/origin.md: yours" in out and "left as it is" in out and "older wording" in out
        assert r.refreshed == []
    assert _no_state(install)


def test_an_edit_to_one_of_the_two_old_sentences_is_kept_whole(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    edited = old.replace("Resist it, and", "Resist it hard, and")
    assert edited != old
    _origin(install).write_text(edited, encoding="utf-8")
    _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == edited  # never a half-migrated file


def test_a_hybrid_one_sentence_hand_replaced_is_kept(make_install):
    install = make_install()
    old = _as_pre_106_install(install)
    hybrid = old.replace(OLD_1, NEW_1)
    assert hybrid != old and "Resist it" in hybrid
    _origin(install).write_text(hybrid, encoding="utf-8")
    r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == hybrid
    assert r.refreshed == [] and "older wording" in out


def test_an_origin_already_on_the_new_wording_is_untouched_and_silent(make_install):
    install = make_install()
    before = _origin(install).read_text(encoding="utf-8")
    r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == before
    assert "origin.md" not in out and r.refreshed == [] and r.review == []
    assert _no_state(install)


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


def test_a_migration_only_dry_run_is_a_plan_not_nothing_to_do(make_install, declared, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    monkeypatch.setattr(update, "_run_anneal", lambda *a, **k: (True, "ok"))
    lines: list[str] = []
    rc = update.run_update(install, dry_run=True, emit=lines.append, confirm=lambda _p: True)
    out = "\n".join(lines)
    assert "nothing to reconcile mechanically" not in out
    assert "WOULD do" in out and rc == 1
    assert _origin(install).read_text(encoding="utf-8") == old


def test_a_write_failure_reaches_review_and_leaves_the_file_whole(make_install, monkeypatch):
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
    monkeypatch.undo()
    _r2, _ = _refresh(install)  # the failure left nothing behind: the next update migrates
    assert not any(p in _origin(install).read_text(encoding="utf-8") for p in OLD_PHRASES)


def test_an_unreadable_file_is_in_review(make_install):
    install = make_install()
    _as_pre_106_install(install)
    _origin(install).write_bytes(b"\xff\xfe not utf8")
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "could not be read" in out


def test_a_transient_check_failure_is_in_review_and_retries(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    import levain.interview as interview

    real = interview.parse_template

    def boom(path):
        raise OSError("template unreadable")

    monkeypatch.setattr(interview, "parse_template", boom)
    r, out = _refresh(install)
    assert r.review == ["seed/origin.md"] and "could not be checked" in out
    assert _origin(install).read_text(encoding="utf-8") == old
    monkeypatch.setattr(interview, "parse_template", real)
    _refresh(install)
    assert not any(p in _origin(install).read_text(encoding="utf-8") for p in OLD_PHRASES)


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


def test_a_save_between_the_read_and_the_replace_is_not_overwritten(make_install, monkeypatch):
    install = make_install()
    old = _as_pre_106_install(install)
    saved = old + "\nan operator line\n"
    real = install_mod.read_answers

    def answers_then_operator_saves(inst):
        got = real(inst)
        _origin(install).write_text(saved, encoding="utf-8")
        return got

    monkeypatch.setattr(install_mod, "read_answers", answers_then_operator_saves)
    r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == saved
    assert "edited since it was read" in out and r.refreshed == [] and r.review == ["seed/origin.md"]


def test_a_symlinked_seed_dir_is_refused_into_review(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    real_dir = tmp_path / "shared_seed"
    (install / "seed").rename(real_dir)
    (install / "seed").symlink_to(real_dir)
    r, out = _refresh(install)
    assert (real_dir / "origin.md").read_text(encoding="utf-8") == old
    assert "is a link" in out and r.review == ["seed/origin.md"]


def test_a_symlinked_origin_is_refused_and_its_target_untouched(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    elsewhere = tmp_path / "dotfile_origin.md"
    elsewhere.write_text(old, encoding="utf-8")
    _origin(install).unlink()
    _origin(install).symlink_to(elsewhere)
    r, out = _refresh(install)
    assert elsewhere.read_text(encoding="utf-8") == old
    assert _origin(install).is_symlink() and "is a link" in out and r.review == ["seed/origin.md"]
    assert _refresh(install, apply=False)[0].review == ["seed/origin.md"]


def test_a_symlinked_levain_dir_is_refused(make_install, tmp_path):
    install = make_install()
    old = _as_pre_106_install(install)
    real_dir = tmp_path / "shared_levain"
    (install / ".levain").rename(real_dir)
    (install / ".levain").symlink_to(real_dir)
    r, out = _refresh(install)
    assert _origin(install).read_text(encoding="utf-8") == old
    assert "is a link" in out and "seed/origin.md" in r.review


def test_a_missing_origin_is_a_quiet_no_op(make_install):
    install = make_install()
    _origin(install).unlink()
    r, out = _refresh(install)
    assert "seed/origin.md:" not in out and "seed/origin.md" not in r.review


def test_a_non_regular_origin_is_in_review(make_install):
    install = make_install()
    _origin(install).unlink()
    _origin(install).mkdir()
    r, out = _refresh(install)
    assert "seed/origin.md" in r.review and "not a regular file" in out
