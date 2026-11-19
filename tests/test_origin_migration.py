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
from levain.install import ORIGIN_MIGRATION_REL, read_answers, refresh_adapter
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
    _r, out = _refresh(install, apply=False)
    assert (install / "seed" / "origin.md").read_text(encoding="utf-8") == old
    assert not install.joinpath(*ORIGIN_MIGRATION_REL).exists()
    assert "an update would bring it" in out


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
