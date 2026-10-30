"""The /2-only rule as a CI test (BUILD_PLAN r7 R-16; A0 r6 §3.5): agents run on /2, so no module under ``levain/`` opens
a /3 store except ``levain/autonomic/schema3.py`` itself. The first PR that adds a /3 opener fails here."""
from __future__ import annotations

from pathlib import Path

import pytest

from levain import cli

ROOT = Path(__file__).resolve().parents[1] / "levain"
OWNER = ROOT / "autonomic" / "schema3.py"


def test_no_module_outside_schema3_opens_a_3_store():
    hits = [f"{p.relative_to(ROOT.parent)}:{n}" for p in sorted(ROOT.rglob("*.py")) if p != OWNER
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1) if "AutonomicStore3(" in line]
    assert hits == [], f"a /3 opener outside schema3.py ({hits}): agents run on /2 (A0 §3.5)"


@pytest.mark.parametrize("command", ["agents", "dream"])
def test_the_seams_exist(command, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main([command, "--help"])
    assert e.value.code == 0
    assert command in capsys.readouterr().out


def test_dream_does_nothing_and_says_so(capsys):
    assert cli.main(["dream"]) == 1
    assert "not built" in capsys.readouterr().err
