"""The needs-phill card's display formatter (card_format.js drawBody / drawDetail / looseCard): headed sections, lists and
option rows from six REAL rows (tests/card_prose_cases.json, each with the message flow's build_message made for it).
The logic runs in node (tests/card_prose_check.js, a fake DOM); the browser half is test_cockpit_card_format.py. Display only: the strict grammar stays the authority for a signed message."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
WEB = HERE.parent / "levain" / "templates" / "web"


def test_the_formatter_draws_headings_lists_and_option_rows_on_every_real_row() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("needs node")
    out = subprocess.run([node, str(HERE / "card_prose_check.js")], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "FAIL" not in out.stdout


def test_no_message_text_goes_through_innerhtml() -> None:
    """R5: text only. The card module and the new question-body helper never assign markup."""
    for name in ("card_format.js",):
        src = (WEB / name).read_text()
        assert not re.search(r"innerHTML|outerHTML|insertAdjacentHTML|document\.write", src), name
    core = (WEB / "dashboard_core.js").read_text()
    helper = core[core.index("function questionText("):core.index("function drawQuestionBody(")]
    assert "innerHTML" not in helper
