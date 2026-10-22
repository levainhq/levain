"""The suite runs against a scratch HOME set before collection, never the account's real one."""

from __future__ import annotations

import os
import pwd
from pathlib import Path


def test_home_is_not_the_accounts_real_home():
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert Path.home().resolve() != real
    assert (Path.home() / ".gitconfig").is_file()
    assert "CODEX_HOME" not in os.environ


def test_ambient_git_and_xdg_config_are_unset():
    assert "GIT_CONFIG_GLOBAL" not in os.environ
    assert "XDG_CONFIG_HOME" not in os.environ


def test_import_time_home_constants_are_scratch():
    """The reproduced HIGH: a constant bound at import time must not name the operator's home."""
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    import levain.firing.anneal as anneal
    assert real not in anneal.DEFAULT_CRYSTAL_PATH.resolve().parents
    from tests import test_firing_anneal
    assert real not in test_firing_anneal.REAL_CRYSTAL.resolve().parents


def test_undo_does_not_restore_the_real_home(monkeypatch):
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    monkeypatch.setenv("HOME", "/nonexistent/x")
    monkeypatch.undo()
    assert Path.home().resolve() != real
