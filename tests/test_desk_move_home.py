"""``levain desk move-home``: the rename of an old install's desk home, the hook rewrite and the doctor row.

Every test runs in tmp directories (a fake levain home and a fake settings.json); nothing here reads or writes the real
``~/.claude`` or ``~/.levain``."""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from levain import cli
from levain.desk.legacy import CLI_ALIAS_LONG, CLI_ALIAS_SHORT, DESK_HOME_NAME, LEGACY_HOME_NAME, LEGACY_SHADOW_NAME, desk_home
from levain.desk.move_home import BACKUP_NAME, SETTINGS_ENV, move_home, rewrite_settings
from levain.doctor import _check_desk_home


def _setup(tmp_path: Path, *, shadow: bool = False) -> tuple[Path, Path]:
    home = tmp_path / ".levain"
    hook = home / LEGACY_HOME_NAME / "activation" / "hooks"
    hook.mkdir(parents=True)
    (hook / "consent_claude_code.py").write_text("# hook\n")
    if shadow:
        (home / LEGACY_SHADOW_NAME).mkdir()
    settings = tmp_path / "claude" / "settings.json"
    settings.parent.mkdir()
    cmd = f"/usr/bin/python3 {home}/{LEGACY_HOME_NAME}/activation/hooks/consent_claude_code.py"
    other = f"python3 {home}/{LEGACY_SHADOW_NAME}/x.py"
    settings.write_text(json.dumps({"model": "m", "hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
        {"type": "command", "command": cmd}, {"type": "command", "command": other}]}]}}))
    return home, settings


def _run(home: Path, settings: Path, *extra: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    rc = move_home(home=home, settings=settings, dry_run="--dry-run" in extra, out=out, err=err)
    return rc, out.getvalue(), err.getvalue()


def test_dry_run_changes_nothing(tmp_path):
    home, settings = _setup(tmp_path)
    before = settings.read_text()
    rc, out, _ = _run(home, settings, "--dry-run")
    assert rc == 0 and "would rename" in out and "would rewrite" in out
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / LEGACY_HOME_NAME).is_symlink()
    assert not (home / DESK_HOME_NAME).exists()
    assert settings.read_text() == before
    assert not settings.with_name(BACKUP_NAME).exists()


def test_the_move_renames_rewrites_links_and_backs_up(tmp_path):
    home, settings = _setup(tmp_path, shadow=True)
    original = settings.read_text()
    rc, out, err = _run(home, settings)
    assert rc == 0, err
    assert (home / DESK_HOME_NAME / "activation" / "hooks" / "consent_claude_code.py").is_file()
    old = home / LEGACY_HOME_NAME
    assert old.is_symlink() and old.resolve() == (home / DESK_HOME_NAME).resolve()
    assert (home / LEGACY_SHADOW_NAME).is_dir()          # the shadow directory is left alone
    cmds = [h["command"] for m in json.loads(settings.read_text())["hooks"]["PreToolUse"] for h in m["hooks"]]
    assert cmds[0] == f"/usr/bin/python3 {home}/{DESK_HOME_NAME}/activation/hooks/consent_claude_code.py"
    assert cmds[1] == f"python3 {home}/{LEGACY_SHADOW_NAME}/x.py"        # a command naming the shadow is not rewritten
    assert Path(cmds[0].split()[1]).is_file()                            # the rewritten hook resolves to a real file
    assert settings.with_name(BACKUP_NAME).read_text() == original
    assert "doctor" in out and _check_desk_home(home) == []


def test_a_second_run_has_nothing_to_do(tmp_path):
    home, settings = _setup(tmp_path)
    assert _run(home, settings)[0] == 0
    after = settings.read_text()
    rc, out, _ = _run(home, settings)
    assert rc == 0 and "nothing to do" in out
    assert settings.read_text() == after


def test_refuses_when_the_new_dir_already_exists(tmp_path):
    home, settings = _setup(tmp_path)
    (home / DESK_HOME_NAME).mkdir()
    before = settings.read_text()
    rc, _, err = _run(home, settings)
    assert rc == 1 and "nothing was changed" in err
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / LEGACY_HOME_NAME).is_symlink()
    assert settings.read_text() == before and not settings.with_name(BACKUP_NAME).exists()


def test_refuses_invalid_json_and_changes_nothing(tmp_path):
    home, settings = _setup(tmp_path)
    settings.write_text("{not json")
    rc, _, err = _run(home, settings)
    assert rc == 1 and "valid JSON" in err
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / DESK_HOME_NAME).exists()
    assert settings.read_text() == "{not json"


def test_a_missing_settings_file_still_moves_the_directory(tmp_path):
    home, settings = _setup(tmp_path)
    settings.unlink()
    rc, out, _ = _run(home, settings)
    assert rc == 0 and (home / DESK_HOME_NAME).is_dir() and not settings.exists()


def test_a_half_done_move_is_finished_by_the_second_run(tmp_path):
    home, settings = _setup(tmp_path)
    (home / LEGACY_HOME_NAME).rename(home / DESK_HOME_NAME)        # the directory moved; the hook was never rewritten
    (home / LEGACY_HOME_NAME).symlink_to(DESK_HOME_NAME)
    rc, out, _ = _run(home, settings)
    assert rc == 0 and "rewrote 1 hook" in out
    assert _run(home, settings)[1].startswith("nothing to do")


def test_the_settings_write_failing_puts_the_directory_back(tmp_path, monkeypatch):
    home, settings = _setup(tmp_path)
    from levain.desk import move_home as mh

    def boom(path, data, expected=None):
        raise OSError("disk full")

    monkeypatch.setattr(mh, "_write_atomic", boom)
    rc, _, err = _run(home, settings)
    assert rc == 1 and "failed" in err
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / LEGACY_HOME_NAME).is_symlink()
    assert not (home / DESK_HOME_NAME).exists()


def test_rewrite_settings_touches_only_command_strings(tmp_path):
    base = tmp_path / ".levain"
    old = f"{base}/{LEGACY_HOME_NAME}/a.py"
    data = {"note": old, "hooks": [{"command": old}, {"command": f"{base}/{LEGACY_SHADOW_NAME}/a.py"}]}
    new, changed = rewrite_settings(data, base)
    assert new["note"] == old                       # not a command key: kept
    assert new["hooks"][0]["command"] == f"{base}/{DESK_HOME_NAME}/a.py"
    assert new["hooks"][1] == data["hooks"][1] and len(changed) == 1


def test_the_doctor_row_is_present_for_an_old_home_and_absent_otherwise(tmp_path):
    home = tmp_path / ".levain"
    assert _check_desk_home(home) == []                                   # no home at all: a new install, no row
    (home / LEGACY_HOME_NAME).mkdir(parents=True)
    rows = _check_desk_home(home)
    assert len(rows) == 1 and rows[0].warn and rows[0].ok
    assert "old name" in rows[0].detail and "move-home" in (rows[0].hint or "")
    assert desk_home(home) == home / LEGACY_HOME_NAME
    (home / DESK_HOME_NAME).mkdir()
    assert _check_desk_home(home) == []


@pytest.mark.parametrize("alias", ["desk", CLI_ALIAS_SHORT, CLI_ALIAS_LONG])
def test_the_verb_runs_through_the_cli_and_both_aliases(tmp_path, monkeypatch, capsys, alias):
    home, settings = _setup(tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    monkeypatch.setenv(SETTINGS_ENV, str(settings))
    rc = cli.main([alias, "move-home", "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0 and "dry run" in out and (home / LEGACY_HOME_NAME).is_dir()
    assert not (home / DESK_HOME_NAME).exists()


# --- L3 round 1 -------------------------------------------------------------------------------------------------------
def _desk_settings(home: Path, name: str = "settings.json") -> Path:
    p = home / LEGACY_HOME_NAME / ".claude" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    cmd = f"python3 -m levain.hookrun {home}/{LEGACY_HOME_NAME}/activation/hooks/consent_claude_code.py"
    p.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": cmd}]}]}}))
    return p


def _cmds(path: Path) -> list[str]:
    d = json.loads(path.read_text())
    return [h["command"] for m in d["hooks"]["PreToolUse"] for h in m["hooks"]]


def test_the_desk_homes_own_hook_settings_are_rewritten_too(tmp_path):
    home, settings = _setup(tmp_path)
    ds, dl = _desk_settings(home), _desk_settings(home, "settings.local.json")
    rc, out, err = _run(home, settings)
    assert rc == 0, err
    for name in ("settings.json", "settings.local.json"):
        p = home / DESK_HOME_NAME / ".claude" / name
        assert _cmds(p) == [f"python3 -m levain.hookrun {home}/{DESK_HOME_NAME}/activation/hooks/consent_claude_code.py"]
        assert p.with_name(name + ".pre-desk-move").is_file()
    # nothing in the moved directory names the old path any more
    assert f"/{LEGACY_HOME_NAME}/" not in (home / DESK_HOME_NAME / ".claude" / "settings.json").read_text()
    assert ds.exists() and dl.exists()       # still reachable through the link


def test_a_command_naming_the_old_home_without_a_trailing_slash_is_rewritten(tmp_path):
    home, settings = _setup(tmp_path)
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"hooks": [
        {"type": "command", "command": f"cd {home}/{LEGACY_HOME_NAME} && run"},
        {"type": "command", "command": f"run --cwd={home}/{LEGACY_HOME_NAME}"},
        {"type": "command", "command": f"run {home}/{LEGACY_HOME_NAME}2/x {home}/{LEGACY_SHADOW_NAME}"}]}]}}))
    rc, out, _ = _run(home, settings, "--dry-run")
    assert rc == 0 and "names the old path" not in out and out.count("would rewrite a hook command") == 2
    assert _run(home, settings)[0] == 0
    assert _cmds(settings) == [f"cd {home}/{DESK_HOME_NAME} && run", f"run --cwd={home}/{DESK_HOME_NAME}",
                               f"run {home}/{LEGACY_HOME_NAME}2/x {home}/{LEGACY_SHADOW_NAME}"]


def test_a_symlink_failure_rolls_back_before_settings_change(tmp_path, monkeypatch):
    home, settings = _setup(tmp_path)
    ds = _desk_settings(home)
    before, dbefore = settings.read_text(), ds.read_text()
    import os as _os

    def nolink(src, dst, *a, **k):
        raise PermissionError("EPERM probe")

    monkeypatch.setattr(_os, "symlink", nolink)
    rc, _, err = _run(home, settings)
    assert rc == 1 and "failed" in err
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / DESK_HOME_NAME).exists()
    assert settings.read_text() == before and ds.read_text() == dbefore


def test_a_later_failure_restores_every_settings_file(tmp_path, monkeypatch):
    home, settings = _setup(tmp_path)
    ds = _desk_settings(home)
    before, dbefore = settings.read_text(), ds.read_text()
    from levain.desk import move_home as mh
    real, calls = mh._write_atomic, []

    def second_fails(path, data, expected=None):
        calls.append(path)
        if len(calls) == 2:
            raise OSError("disk full")
        real(path, data, expected)

    monkeypatch.setattr(mh, "_write_atomic", second_fails)
    rc, _, err = _run(home, settings)
    assert rc == 1 and len(calls) == 2
    assert settings.read_text() == before and ds.read_text() == dbefore
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / LEGACY_HOME_NAME).is_symlink()
    assert not (home / DESK_HOME_NAME).exists()


def test_an_interrupted_move_gets_its_link_back(tmp_path):
    home, settings = _setup(tmp_path)
    (home / LEGACY_HOME_NAME).rename(home / DESK_HOME_NAME)        # moved, no link, hook never rewritten
    rc, out, err = _run(home, settings)
    assert rc == 0, err
    assert (home / LEGACY_HOME_NAME).is_symlink() and "link" in out


def test_neither_home_existing_refuses_when_settings_name_the_old_path(tmp_path):
    home, settings = _setup(tmp_path)
    import shutil

    shutil.rmtree(home / LEGACY_HOME_NAME)
    before = settings.read_text()
    rc, out, err = _run(home, settings)
    assert rc == 1 and "no desk home to move" in err and settings.read_text() == before
    settings.write_text("{}")
    rc, out, _ = _run(home, settings)
    assert rc == 0 and "no desk home to move" in out and "nothing to do" not in out


def test_a_settings_symlink_keeps_its_link_and_the_target_is_written(tmp_path):
    home, settings = _setup(tmp_path)
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    settings.rename(real)
    settings.symlink_to(real)
    rc, _, err = _run(home, settings)
    assert rc == 0, err
    assert settings.is_symlink() and f"/{DESK_HOME_NAME}/" in real.read_text()


def test_the_dollar_home_spellings_are_rewritten_only_for_the_users_own_levain(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    base = tmp_path / ".levain"
    base.mkdir()
    data = {"command": "a $HOME/.levain/coordinator/x ${HOME}/.levain/coordinator ~/.levain/coordinator/y"}
    new, _ = rewrite_settings(data, base)
    assert new["command"] == "a $HOME/.levain/desk/x ${HOME}/.levain/desk ~/.levain/desk/y"
    other = tmp_path / "x" / ".levain"
    other.mkdir(parents=True)
    kept, ch = rewrite_settings(data, other)
    assert kept == data and ch == []             # a levain home elsewhere does not own the user's ~/.levain


def test_settings_changed_before_the_replace_refuses_and_changes_nothing(tmp_path, monkeypatch):
    home, settings = _setup(tmp_path)
    from levain.desk import move_home as mh
    real = mh._replace_bytes

    def racing(path, raw, expected=None):
        if expected is not None:
            Path(path).write_text('{"hooks": {}, "edited": true}')     # another writer got in first
        real(path, raw, expected)

    monkeypatch.setattr(mh, "_replace_bytes", racing)
    rc, _, err = _run(home, settings)
    assert rc == 1 and "changed while" in err
    assert json.loads(settings.read_text()).get("edited") is True        # the other writer's file is untouched
    assert (home / LEGACY_HOME_NAME).is_dir() and not (home / LEGACY_HOME_NAME).is_symlink()


def _store_with_cwd(tmp_path: Path, cwd: str):
    from levain.desk.store import CoordStore

    st = CoordStore(tmp_path / "queue")
    st.coord_set_agent("claude", cwd=cwd)
    return st


def test_the_stored_desk_cwd_moves_with_the_home(tmp_path):
    home, settings = _setup(tmp_path)
    st = _store_with_cwd(tmp_path, f"{home}/{LEGACY_HOME_NAME}")
    out, err = io.StringIO(), io.StringIO()
    rc = move_home(home=home, settings=settings, store_dir=tmp_path / "queue", out=out, err=err)
    assert rc == 0, err.getvalue()
    assert st.coord_setting().cwd == f"{home}/{DESK_HOME_NAME}"
