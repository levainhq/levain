"""A Codex hooks.json rendered before the prompt hook's timeout grew still carries the short one; doctor says so
(L3 tsl2 r2: complement MED + codex OPEN 1)."""

from __future__ import annotations

import json
from pathlib import Path

from levain import doctor
from levain.band_hook import PROMPT_HOOK_TIMEOUT_S
from tests.test_repin_on_install import make_install  # noqa: F401  (fixture)


def _set_timeout(path: Path, value) -> None:
    doc = json.loads(path.read_text())
    handler = doc["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    if value is None:
        handler.pop("timeout", None)
    else:
        handler["timeout"] = value
    path.write_text(json.dumps(doc, indent=2))


def _rows(install: Path):
    return [r for r in doctor._check_codex(install) if "UserPromptSubmit timeout" in r.name]


def test_a_short_timeout_is_a_failing_row(make_install, tmp_path):
    install = make_install("a", adapter="codex")
    path = tmp_path / "codex-home" / "hooks.json"
    assert _rows(install) == []                      # the fresh render carries the long one
    _set_timeout(path, 10)
    (row,) = _rows(install)
    assert not row.ok and str(path) in row.name and "10" in row.detail and "levain update" in row.hint


def test_the_current_timeout_and_an_absent_one_are_clean(make_install, tmp_path):
    install = make_install("a", adapter="codex")
    path = tmp_path / "codex-home" / "hooks.json"
    _set_timeout(path, PROMPT_HOOK_TIMEOUT_S)
    assert _rows(install) == []
    _set_timeout(path, None)                         # the harness default is long enough
    assert _rows(install) == []


def test_no_codex_hooks_file_adds_no_row(make_install, tmp_path):
    install = make_install("a", adapter="codex")
    (tmp_path / "codex-home" / "hooks.json").unlink()
    assert _rows(install) == []
