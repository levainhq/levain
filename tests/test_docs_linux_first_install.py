"""docs/linux-first-install.md: every documented install_helper command line parses under the real grammar."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from levain.slot.grammar import Refused, parse_install_argv

ROOT = Path(__file__).resolve().parent.parent
DOC = ROOT / "docs" / "linux-first-install.md"
PLACEHOLDERS = {"<N>": "4", "<M>": "1", "<user>": "admin"}


def _documented_argvs() -> list[list[str]]:
    out = []
    for block in re.findall(r"```sh\n(.*?)```", DOC.read_text(), re.S):
        for line in block.splitlines():
            if "-m levain.slot.install_helper" not in line:
                continue
            toks = shlex.split(line)
            argv = toks[toks.index("levain.slot.install_helper") + 1:]
            out.append([PLACEHOLDERS.get(t, t) for t in argv])
    return out


def test_documented_commands_match_the_helper_grammar_and_extra():
    argvs = _documented_argvs()
    assert {a[0] for a in argvs} == {"setup", "repair", "undo"}
    for argv in argvs:
        try:
            parse_install_argv(argv)
        except Refused as e:
            pytest.fail(f"documented command {argv} refused by the grammar: {e}")
    text = DOC.read_text()
    # the remedy the doc gives is the one the helper's own refusal names, and the extra exists
    assert "levain[isolation]" in text
    assert "install levain[isolation]" in (ROOT / "levain" / "slot" / "root_tree.py").read_text()
    assert re.search(r"^isolation\s*=", (ROOT / "pyproject.toml").read_text(), re.M)
    # the README links the page
    assert "docs/linux-first-install.md" in (ROOT / "README.md").read_text()
