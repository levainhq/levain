"""TEST ONLY: the environment and home a test hands a consent reader directly (H4: every reader of the environment on
the consent core path takes ``environ`` / ``home`` as a REQUIRED parameter; the hook entry points bind ``os.environ``
and ``Path.home()`` once, at their top). A test that calls a reader directly passes these: the test's own scratch
environment and home as conftest set them (a per-test ``$HOME`` and ``$LEVAIN_HOME``), read when the call is made, so
a test that sets a variable before its call still sees it, and the reader is handed a copy, never ``os.environ``."""

from __future__ import annotations

import os
from pathlib import Path


def h4_env() -> dict[str, str]:
    """A copy of this test's environment, taken now."""
    return dict(os.environ)


def h4_home() -> Path:
    """This test's home directory, read now."""
    return Path.home()


def h4_codex_dir() -> Path:
    """This test's CODEX_HOME as Codex reads it (``$CODEX_HOME``, else ``~/.codex``), read now. Spelled here, not
    imported: this helper imports nothing of ``levain``, so a test file that uses it keeps its CI root selection
    (``tests/ci/root_closure.py`` counts a helper's imports as the file's)."""
    return Path(os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"))
