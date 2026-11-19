"""levain#127 (ruling np-389e829d (a)): a non-default dream ``claude_bin`` is shown by ``agents show dream`` (text and
--json) and by the doctor row ``dream program``; a default one prints nothing."""
from __future__ import annotations

import json

from levain import doctor
from levain.dream import agent as da
from tests.test_dream_live_binding import _enabled, _show_json, env  # noqa: F401 - env is a fixture


def _show_text(env, capsys) -> str:
    from levain.cli import main

    capsys.readouterr()
    assert main(["agents", "--store-dir", str(env["ag"]), "show", "dream"]) == 0
    return capsys.readouterr().out


def _stub(env) -> str:
    p = env["tmp"] / "stubclaude"
    p.write_text("#!/bin/sh\n")
    p.chmod(0o700)
    return str(p)


def test_a_default_program_is_not_shown(env, capsys):
    da.write_install(env["ag"], **env["kw"])
    _enabled(env, capsys)
    assert da.non_default_claude_bin(env["ag"]) is None
    assert "the program a fire runs" not in _show_text(env, capsys)
    assert _show_json(env, capsys)["dream_claude_bin"] is None
    assert doctor._check_dream_claude_bin(env["ag"]) == []


def test_a_non_default_program_is_shown_by_show_and_the_doctor(env, capsys):
    bin_ = _stub(env)
    da.write_install(env["ag"], claude_bin=bin_, **env["kw"])
    _enabled(env, capsys)
    assert da.non_default_claude_bin(env["ag"]) == bin_
    assert f"the program a fire runs is {bin_!r}" in _show_text(env, capsys)
    assert _show_json(env, capsys)["dream_claude_bin"] == bin_
    (row,) = doctor._check_dream_claude_bin(env["ag"])
    assert row.ok and row.warn and bin_ in row.detail


def test_no_install_prints_nothing(tmp_path):
    assert da.non_default_claude_bin(tmp_path) is None
    assert doctor._check_dream_claude_bin(tmp_path) == []
