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


def test_one_home_for_the_sudo_path() -> None:
    from levain import slot
    from levain.consent import limits_cli
    from levain.firing import confinement
    assert slot.SUDO == "/usr/bin/sudo"
    assert supervisor.SUDO is slot.SUDO and limits_cli.SUDO is slot.SUDO and confinement.SUDO is slot.SUDO


def test_no_argv_in_levain_spells_sudo_itself() -> None:
    import re
    from pathlib import Path
    root = Path(adopt_view.__file__).resolve().parents[1]
    elem = re.compile(r"""[\[(,]\s*["'](?:/usr/bin/)?sudo["']\s*[,\])]""")
    hits = [f"{p.relative_to(root)}:{n}: {line.strip()}" for p in sorted(root.rglob("*.py"))
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if elem.search(line)]
    assert not hits, hits
