"""Free-text answer boxes tell the iOS keyboard they are prose, not a contact field (Phill 11-07: an email-sourced card's
reply box got the contact keyboard). String-level: the helper exists and both box builders call it."""
from __future__ import annotations

import re
from pathlib import Path

SRC = (Path(__file__).resolve().parent.parent / "levain" / "templates" / "web" / "dashboard_core.js").read_text()


def test_helper_sets_autocomplete_off():
    body = re.search(r"function noContactFill\(node\) \{(.*?)\n  \}", SRC, re.S).group(1)
    assert 'setAttribute("autocomplete", "off")' in body


def test_question_words_and_action_fields_use_it():
    assert re.search(r'el\("textarea", "question-words"\);.*?noContactFill\(words\);', SRC, re.S)
    assert re.search(r'else node\.type = "text";\s*noContactFill\(node\);', SRC)
