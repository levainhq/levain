"""Phill 11-03: on the iPhone the Drop box autocorrected nothing and behaved like a payment field. Both Drop textareas must say
"ordinary prose" outright; the pairing code and repo boxes keep their code/identifier attributes."""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"
PROSE = {"autocapitalize": "sentences", "autocorrect": "on", "spellcheck": "true", "inputmode": "text", "autocomplete": "off"}


def test_phone_drop_textarea_is_prose():
    html = (WEB / "phone.html").read_text()
    tag = re.search(r'<textarea[^>]*id="drop-words"[^>]*>', html, re.S).group(0)
    for k, v in PROSE.items():
        assert f'{k}="{v}"' in tag, (k, tag)
    assert "one-time-code" not in tag


def test_cockpit_drop_textarea_is_prose():
    js = (WEB / "dashboard_core.js").read_text()
    i = js.index('words.id = "ck-drop-words"')
    block = js[i:i + 1500]
    for k, v in PROSE.items():
        assert f'["{k}", "{v}"]' in block, k


def test_code_and_repo_boxes_keep_their_attributes():
    assert 'autocapitalize="off" autocorrect="off"' in (WEB / "phone.html").read_text()
    assert 'input.autocomplete = "one-time-code"' in (WEB / "dashboard_boot.js").read_text()
