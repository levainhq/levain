"""The suite runs against a scratch HOME, never the account's real one."""

from __future__ import annotations

import os
import pwd
from pathlib import Path


def test_home_is_not_the_accounts_real_home():
    real = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
    assert Path.home().resolve() != real
    assert (Path.home() / ".gitconfig").is_file()
    assert "CODEX_HOME" not in os.environ
