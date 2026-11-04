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


def test_envelope_sheet_drop_textarea_is_prose():
    js = (WEB / "sign.js").read_text()
    i = js.index('words.id = "drop-words"')
    block = js[i:i + 900]
    for k, v in PROSE.items():
        assert f'["{k}", "{v}"]' in block, k


def test_every_drop_textarea_has_a_dash_free_name():
    """Phill 11-04, on his iPhone: Safari's AutoFill showed "AutoFill Contact" and his phone numbers on the Drop box (not on a
    page whose box carried a dash-free id and name: variant K of ~/Desktop/ios_autofill_variants). The WebKit classifier is closed
    code and its one reported trigger is a dash in a field's name (Apple Developer Forums 764041), so each Drop box names itself
    `dropwords`. The three boxes are the phone page's, the cockpit hero and the envelope sheet's."""
    phone = (WEB / "phone.html").read_text()
    assert 'name="dropwords"' in re.search(r'<textarea[^>]*id="drop-words"[^>]*>', phone, re.S).group(0)
    for fname, anchor in (("dashboard_core.js", 'words.id = "ck-drop-words"'), ("sign.js", 'words.id = "drop-words"')):
        js = (WEB / fname).read_text()
        i = js.index(anchor)
        assert '["name", "dropwords"]' in js[i:i + 1500], fname
