"""Phill 10-20 (np-cd6de24a "(a)"): ``levain session`` is hidden from ``levain --help`` until it is runnable for a
user; it still parses (the VM runs use it)."""
from __future__ import annotations

import re

import pytest

from levain import cli


def test_session_appears_nowhere_in_levain_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])
    out = capsys.readouterr().out
    assert out.startswith("usage: levain") and "init" in out          # the help this test reads is the real one
    assert not re.search(r"^\s*session\b", out, re.M)                  # not a listed command
    assert "SUPPRESS" not in out
    usage = out.split("\n\n")[0]
    assert "session" not in usage                                      # nor a choice in the usage line


def test_levain_session_help_still_works(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["session", "--help"]) == 0
    assert capsys.readouterr().out.startswith("usage: levain session")
