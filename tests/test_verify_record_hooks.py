"""`levain verify-hooks` runs the record-only transcript hooks (stop.py, post_tool_use.py) for real, in a throwaway
copy of the hooks directory, and never writes into the live install."""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from levain import host_guard as hg
from levain import verify
from levain.install import run_init


@pytest.mark.parametrize("adapter", ["claude-code", "codex"])
def test_record_only_hooks_write_one_line_in_a_temporary_copy_and_never_in_the_install(
        tmp_path: Path, monkeypatch, capsys, adapter):
    from tests.test_messaging_optout import _answers

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    ent = tmp_path / "e"
    assert run_init(ent, adapter, force=False, answers_file=_answers(tmp_path, capsys)) == 0
    hooks = ent / "activation" / "hooks"
    for script, payload, kind, text in verify._RECORD_ONLY:
        r = verify._invoke_record_only(hooks, script, payload, kind, text, [sys.executable])
        assert r.ok, r
    ups = verify._invoke(hooks / "user_prompt_submit.py", ent, verify.ups_payload(ent), "UserPromptSubmit",
                         [sys.executable])
    assert ups.ok, ups
    # seen red (a real verify-hooks run): the UserPromptSubmit check recorded a session in the live install
    assert not list((ent / ".levain").glob("sessions/*"))    # the live install was never written
    # the check can fail: a stop.py that records nothing is named
    broken = tmp_path / "broken"
    shutil.copytree(hooks, broken)
    (broken / "stop.py").write_text("import sys\nsys.exit(0)\n")
    r = verify._invoke_record_only(broken, *verify._RECORD_ONLY[0], [sys.executable])
    assert not r.ok and "wrote 0 transcript line" in r.detail
