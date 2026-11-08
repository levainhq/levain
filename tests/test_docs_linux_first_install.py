"""docs/linux-first-install.md: documented install_helper commands parse under the real grammar, and the page's
numbers and extra match the code."""
from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

# tomllib is 3.11+; the root-run job collects this file on the 3.9 floor, and pyproject carries no tomli fallback
tomllib = pytest.importorskip("tomllib")

from levain.slot import grammar
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
    text = DOC.read_text()
    argvs = _documented_argvs()
    assert {a[0] for a in argvs} == {"setup", "repair", "undo"}
    for argv in argvs:
        try:
            parse_install_argv(argv)
        except Refused as e:
            pytest.fail(f"documented command {argv} refused by the grammar: {e}")

    # the numbers the page states are the grammar's
    m = re.search(r"`--slots` takes (\d+) to (\d+) and `--verifier-slots` (\d+) to (\d+)", text)
    assert m, "the slot-range sentence moved"
    assert tuple(map(int, m.groups())) == (grammar.MIN_SETUP_SLOTS, grammar.MAX_SLOTS, 1, grammar.MAX_VERIFIER_SLOTS)
    assert f"up to {grammar.MAX_ROOTS} times" in text
    with pytest.raises(Refused):
        parse_install_argv(["setup", "--slots", str(grammar.MAX_SLOTS + 1), "--verifier-slots", "1", "--operator", "a"])
    with pytest.raises(Refused):
        parse_install_argv(["setup", "--slots", "4", "--verifier-slots", "0", "--operator", "a"])

    # the central claim: the isolation extra exists, carries h11, and the base install does not
    proj = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert any(re.match(r"h11\b", r) for r in proj["optional-dependencies"]["isolation"])
    assert not any(re.match(r"h11\b", r) for r in proj["dependencies"])
    assert "install levain[isolation]" in (ROOT / "levain" / "slot" / "root_tree.py").read_text()
    assert "install levain[isolation]" in text

    # the installed-helper forms the prose names
    from levain.slot.layout import repair_line
    assert repair_line("linux") in text
    assert repair_line("linux").replace(" repair", " undo") in text

    assert "docs/linux-first-install.md" in (ROOT / "README.md").read_text()
