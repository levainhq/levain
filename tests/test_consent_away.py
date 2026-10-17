"""`levain consent away`: away mode set through the CLI is what the adapters' own load_settings reads."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from levain.cli import main
from levain.consent import away
from levain.consent.adapters import claude_code as cc
from levain.consent.adapters import codex as cx


@pytest.fixture
def qdir(tmp_path):
    q = tmp_path / "consent"
    q.mkdir(mode=0o700)
    os.chmod(q, 0o700)
    for name in (cc.SETTINGS_NAME, cx.SETTINGS_NAME):
        (q / name).write_text(json.dumps({"cockpit_data_root": "/ck", "away_mode": False, "away_hold_seconds": 0,
                                          "other": 1}))
    return q


def test_on_then_off_is_what_both_adapters_read(qdir, capsys):
    assert main(["consent", "away", "on", "--hold", "120", "--queue-dir", str(qdir)]) == 0
    for mod in (cc, cx):
        s = mod.load_settings(qdir)
        assert s.away_mode is True and s.away_hold_seconds == 120.0 and s.cockpit_data_root == "/ck"
    assert json.loads((qdir / cc.SETTINGS_NAME).read_text())["other"] == 1  # other keys kept
    assert oct((qdir / cc.SETTINGS_NAME).stat().st_mode & 0o777) == "0o600"
    assert "claude_code: away mode ON, holds up to 120s" in capsys.readouterr().out
    assert main(["consent", "away", "off", "--agent", "codex", "--queue-dir", str(qdir)]) == 0
    assert cx.load_settings(qdir).away_mode is False and cc.load_settings(qdir).away_mode is True


def test_on_without_hold_uses_the_default_and_show_writes_nothing(qdir, capsys):
    before = (qdir / cc.SETTINGS_NAME).read_bytes()
    assert main(["consent", "away", "--queue-dir", str(qdir)]) == 0
    assert (qdir / cc.SETTINGS_NAME).read_bytes() == before
    assert "away mode off" in capsys.readouterr().out
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 0
    assert cc.load_settings(qdir).away_hold_seconds == away.DEFAULT_HOLD


@pytest.mark.parametrize("hold", ["0", "-1", "901", "nan", "inf"])
def test_a_hold_outside_the_hooks_deadline_is_refused(qdir, hold):
    before = (qdir / cc.SETTINGS_NAME).read_bytes()
    assert main(["consent", "away", "on", "--agent", "claude_code", "--hold", hold, "--queue-dir", str(qdir)]) == 1
    assert (qdir / cc.SETTINGS_NAME).read_bytes() == before


def test_a_missing_file_is_never_created(qdir):
    (qdir / cx.SETTINGS_NAME).unlink()
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
    assert not (qdir / cx.SETTINGS_NAME).exists()
    assert main(["consent", "away", "on", "--queue-dir", str(qdir)]) == 0  # all: the agent with a file
    assert not (qdir / cx.SETTINGS_NAME).exists() and cc.load_settings(qdir).away_mode is True


def test_a_symlinked_file_or_an_open_queue_dir_is_refused(qdir, tmp_path):
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (qdir / cx.SETTINGS_NAME).unlink()
    (qdir / cx.SETTINGS_NAME).symlink_to(target)
    assert main(["consent", "away", "on", "--agent", "codex", "--queue-dir", str(qdir)]) == 1
    assert target.read_text() == "{}"
    os.chmod(qdir, 0o755)
    assert main(["consent", "away", "on", "--agent", "claude_code", "--queue-dir", str(qdir)]) == 1
    assert cc.load_settings(qdir).away_mode is False


def test_no_queue_directory_is_an_error(tmp_path):
    assert main(["consent", "away", "on", "--queue-dir", str(tmp_path / "nope")]) == 1
    assert not (tmp_path / "nope").exists()
