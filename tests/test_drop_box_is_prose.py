"""Phill 11-03: on the iPhone the Drop box autocorrected nothing and behaved like a payment field. Both Drop textareas must say
"ordinary prose" outright; the pairing code and repo boxes keep their code/identifier attributes."""
from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "levain" / "templates" / "web"
PROSE = {"autocapitalize": "sentences", "autocorrect": "on", "spellcheck": "true", "inputmode": "text", "autocomplete": "off"}


def test_cockpit_drop_textarea_is_prose():
    js = (WEB / "dashboard_core.js").read_text()
    i = js.index('words.id = "ck-drop-words"')
    block = js[i:i + 1500]
    for k, v in PROSE.items():
        assert f'["{k}", "{v}"]' in block, k


def test_code_and_repo_boxes_keep_their_attributes():
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
    `dropwords`. The boxes are the cockpit hero and the envelope sheet's."""
    for fname, anchor in (("dashboard_core.js", 'words.id = "ck-drop-words"'), ("sign.js", 'words.id = "drop-words"')):
        js = (WEB / fname).read_text()
        i = js.index(anchor)
        assert '["name", "dropwords"]' in js[i:i + 1500], fname


def test_the_other_dashed_text_fields_of_the_drop_flow_have_dash_free_names():
    """The sweep behind the Drop box fix (11-04): every free-text or identifier field whose id carries a dash and that is not a
    contact field names itself without one. Left alone on purpose: the type=number inputs (drop-calls,
    drop-tokens, drop-hours; numeric, untested) and the filter and pairing inputs (no id and no name at all)."""
    ef = (WEB / "envelope_form.js").read_text()
    assert 'repo.setAttribute("name", "droprepo")' in ef[ef.index('repo.id = "drop-repo"'):][:400]
    sj = (WEB / "sign.js").read_text()
    assert 'box.setAttribute("name", "writein")' in sj[sj.index('box.id = "sheet-write-in"'):][:400]


def test_the_shared_envelope_form_names_its_name_their_repo_and_branch_boxes():
    """L3 r1 (codex + complement): the sweep covered one of each pair. The
    shared envelope form builds the same two, and the signing sheet's cancellation reason is a free-text field too."""
    ef = (WEB / "envelope_form.js").read_text()
    assert 'onto.setAttribute("name", "droponto")' in ef[ef.index('onto.id = "drop-onto"'):][:300]
    sj = (WEB / "sign.js").read_text()
    assert 'reasonBox.setAttribute("name", "sheetreason")' in sj[sj.index('reasonBox.id = "sheet-reason"'):][:300]


def test_no_drop_box_sits_in_a_form_that_a_name_could_submit():
    """L3 r1 (deepseek MED, refuted by grep): a named control inside a submitting <form> would send its words; none of the Drop
    boxes is in one (the web templates read them by id and never use FormData, getElementsByName or form.elements)."""
    for name in ("dashboard.html", "sign.html"):      # the pages that host a Drop box or the signing sheet (init.html is the setup form, no Drop box)
        assert "<form" not in (WEB / name).read_text(), name
    for fname in ("dashboard_core.js", "envelope_form.js", "sign.js"):
        src = (WEB / fname).read_text()
        assert 'el("form"' not in src and 'createElement("form")' not in src, fname
    for fname in sorted(p.name for p in WEB.glob("*.js")):
        src = (WEB / fname).read_text()
        assert "FormData" not in src and "getElementsByName" not in src and ".elements[" not in src, fname
