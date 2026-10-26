"""The root-helper call names sudo by absolute path: a bare ``sudo`` lets a PATH-dir plant answer for the helper."""
from __future__ import annotations

import os

from levain.slot import adopt_view, supervisor


def test_supervisor_helper_argv0_is_absolute() -> None:
    s = supervisor.Seams()
    assert os.path.isabs(s.helper[0]) and s.helper[0] == supervisor.SUDO


def test_adopt_view_helper_argv0_is_absolute() -> None:
    s = adopt_view.Seams()
    assert os.path.isabs(s.helper[0]) and s.helper[0] == supervisor.SUDO


_SPELLINGS = {"sudo", "/usr/bin/sudo"}


def sudo_spellings(source: str) -> list[int]:
    """Line numbers where the source spells the sudo path as a list/tuple element, a call argument or an assigned value."""
    import ast
    tree = ast.parse(source)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.List, ast.Tuple)):
            kids = node.elts
        elif isinstance(node, ast.Call):
            kids = node.args
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            kids = [node.value]
        else:
            continue
        hits += [k.lineno for k in kids if isinstance(k, ast.Constant) and k.value in _SPELLINGS]
    return hits


def test_the_guard_sees_a_multiline_argv_and_a_second_home() -> None:
    assert sudo_spellings('argv = [\n    "sudo",\n    "-n",\n]\n') == [2]
    assert sudo_spellings('SUDO = "/usr/bin/sudo"\n') == [1]
    assert sudo_spellings('run(("/usr/bin/sudo", "-n"))\n') == [1]
    assert sudo_spellings('"""sudo"""\nx = frozenset({"sudo", "doas"})\n') == []


def test_one_home_for_the_sudo_path() -> None:
    import importlib
    from levain import slot
    assert slot.SUDO == "/usr/bin/sudo"
    for mod in ("levain.slot.supervisor", "levain.slot.adopt_view", "levain.consent.limits_cli",
                "levain.firing.confinement", "levain.firing.hands", "levain.firing.ws_git", "levain.doctor"):
        assert importlib.import_module(mod).SUDO is slot.SUDO, mod


def test_no_file_in_levain_spells_sudo_itself_outside_the_home() -> None:
    from pathlib import Path
    root = Path(adopt_view.__file__).resolve().parents[1]
    home = root / "hands_live.py"
    hits = [f"{p.relative_to(root)}:{n}" for p in sorted(root.rglob("*.py")) if p != home
            for n in sudo_spellings(p.read_text(encoding="utf-8"))]
    assert not hits, hits
