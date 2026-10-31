"""The Needs-you strip is a column of fixed-height summary rows (Phill 10-26, option A): one row height for every item however long
its text, no answer control inside the strip, and the whole item with every option and the rec in its focus view."""
from __future__ import annotations

import json
from pathlib import Path

from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

LAST = "THE LAST SENTENCE OF THE LONG ITEM ENDS HERE."
LONG_WHAT = "Which way should the long decision go? " + " ".join(f"Clause {i} carries more of the analysis the item leads with." for i in range(14)) + " " + LAST
LONG_BLOCKS = "the whole chain of seats stays parked until this is ruled, and " + "every later step waits on it " * 6
LONG_OPTS = [("a", "Take the first route all the way through, rebuild the whole fixture, and rerun every pass before anything merges to main"),
             ("b", "Take the second route: keep today's shape, ship the small fix, and leave the larger change for the evening session at a calmer pace"),
             ("c", "Not now, keep this open")]
SIZES = (("desktop", {"w": 1440, "h": 900, "dpr": 1}), ("phone", {"w": 390, "h": 844, "dpr": 2, "mobile": True}))
ROWS = "[...document.querySelectorAll('#ck-needs .needs-row, #ck-needs .needs-card')]"
READY = f"{ROWS}.length === 3 && /Medium one/.test(document.getElementById('ck-needs').innerText) && !/reading the/.test(document.getElementById('ck-needs').innerText)"


def _file(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    store.file(agent="claude_code", agent_session_id="s1", what="Short one?", options=[("a", "Yes"), ("b", "No")], rec="a",
               blocks="a short block", cwd="/w")
    store.file(agent="codex", agent_session_id="s2", what=LONG_WHAT, options=LONG_OPTS, rec="b", blocks=LONG_BLOCKS[:299], cwd="/w")
    store.file(agent="claude_code", agent_session_id="s3", what="Medium one: keep the plan or cut it back to the first slice?",
               options=[("a", "Keep the plan"), ("b", "Cut it back")], rec="a", blocks="the second slice waits on it", cwd="/w")


def _shoot_steps(out: Path | None, prefix: str) -> list:
    steps: list = []
    for name, emu in SIZES:
        steps += [{"emulate": emu}, {"until": READY, "ms": 20000},
                  {"eval": "new Promise(r => setTimeout(() => r(1), 600))"},
                  {"eval": f"JSON.stringify({{w: {emu['w']}, heights: {ROWS}.map(r => r.getBoundingClientRect().height),"
                           f" controls: document.querySelectorAll('#ck-needs .needs-row button, #ck-needs .needs-row textarea, #ck-needs .needs-row input,"
                           f" #ck-needs .needs-row .question-option').length,"
                           " ctl2: document.querySelectorAll('#ck-needs .question-option, #ck-needs .needs-btn, #ck-needs textarea').length})"}]
        if out:
            steps.append({"shot": str(out / f"{prefix}_{name}.png"), "full": True})
    return steps


@needs_browser
def test_every_row_is_one_height_and_holds_no_answer_control(tmp_path: Path, monkeypatch) -> None:
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", _shoot_steps(None, "x"))["values"]
    for got in (json.loads(v) for v in vals if isinstance(v, str) and v.startswith("{")):
        hs = got["heights"]
        assert len(hs) == 3, got
        assert max(hs) - min(hs) <= 2, got                  # the long item's row is no taller than the short ones
        assert max(hs) <= 100, got                          # a grid row stretches cards to equal height, so equal is not enough: bound it
        assert got["controls"] == 0 and got["ctl2"] == 0, got   # nothing in the strip answers an item


@needs_browser
def test_the_focus_view_draws_the_long_item_whole_and_decides_there(tmp_path: Path, monkeypatch) -> None:
    _file(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    open_long = (f"(() => {{ const r = {ROWS}.find(x => /rec b/.test(x.textContent)); r.click(); return 1; }})()")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": SIZES[0][1]}, {"until": READY, "ms": 20000},
            {"eval": open_long},
            {"until": "document.querySelectorAll('#levain-modal .needs-question .question-option').length === 3"},
            {"eval": "JSON.stringify({text: document.querySelector('#levain-modal .needs-q-title').textContent,"
                     " opts: [...document.querySelectorAll('#levain-modal .question-option')].map(b => b.textContent),"
                     " rec: /REC\\s+b/.test(document.querySelector('#levain-modal .needs-card').innerText),"
                     " words: !!document.querySelector('#levain-modal .question-words')})"}])["values"]
    got = json.loads(vals[-1])
    assert got["text"].endswith(LAST), got
    for (key, label), btn in zip(LONG_OPTS, got["opts"]):
        assert btn == f"{key} · {label}", (btn, label)       # every option in full
    assert got["rec"] and got["words"], got


@needs_browser
def test_blocks_that_begin_with_the_title_show_only_the_remainder(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    store.file(agent="codex", agent_session_id="s1", what="Ship the fix now?", options=[("a", "Yes"), ("b", "No")], rec="a",
               blocks="Ship the fix now? The release waits on this.", cwd="/w")
    store.file(agent="codex", agent_session_id="s2", what="flow: Cut the second slice?", options=[("a", "Yes"), ("b", "No")], rec="a",
               blocks="Cut the second slice?", cwd="/w")   # the provider's repo prefix is on the title only
    _root, src, _ids = _install(tmp_path)
    rows = "[...document.querySelectorAll('#ck-needs .needs-row')]"
    ready = f"{rows}.length === 2 && {rows}.every(r => /Ship|Cut/.test(r.querySelector('.needs-row-title').textContent))"
    subs = f"JSON.stringify({rows}.map(r => [r.querySelector('.needs-row-title').textContent, r.querySelector('.needs-row-sub').textContent]))"
    open_ship = f"{rows}.find(r => /Ship/.test(r.textContent)).click(); 1"
    focus = ("JSON.stringify({text: document.querySelector('#levain-modal .needs-card').innerText,"
             " blocks: [...document.querySelectorAll('#levain-modal .needs-q-blocks')].map(x => x.textContent)})")
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": SIZES[0][1]}, {"until": ready, "ms": 20000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 600))"}, {"eval": subs}, {"eval": open_ship},
            {"until": "document.querySelector('#levain-modal .needs-q-title')"}, {"eval": focus}])["values"]
    got = {t: s for t, s in json.loads(vals[-4])}
    assert got["Ship the fix now?"] == "blocks: The release waits on this.", got
    assert got["flow: Cut the second slice?"] == "", got            # nothing remains: the row has no second line
    f = json.loads(vals[-1])
    assert f["blocks"] == [], f                                # the card draws the remainder as prose (card_format.js); .needs-q-blocks is only the no-module fallback
    assert "The release waits on this." in f["text"] and "blocks:" not in f["text"], f
    assert f["text"].count("Ship the fix now?") == 1, f       # the title is drawn once


@needs_browser
def test_blocks_strip_the_title_only_at_a_word_boundary(tmp_path: Path, monkeypatch) -> None:
    """L3 r1 (3 lineages): `startsWith` with no boundary turned 'Ship?' + 'Shipment ...' into 'ment ...' and dropped a letter of 'Use All'."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    store = QuestionStore(home / "consent")
    for what, blocks in (("Ship?", "Shipment is waiting on legal"), ("Use A", "Use All of it"), ("Cut it?", "Cut it: the rest waits")):
        store.file(agent="codex", agent_session_id="s-" + what, what=what, options=[("a", "Yes"), ("b", "No")], rec="a", blocks=blocks, cwd="/w")
    _root, src, _ids = _install(tmp_path)
    rows = "[...document.querySelectorAll('#ck-needs .needs-row')]"
    ready = f"{rows}.length === 3 && {rows}.every(r => /Ship|Use|Cut/.test(r.querySelector('.needs-row-title').textContent))"
    subs = f"JSON.stringify({rows}.map(r => [r.querySelector('.needs-row-title').textContent, r.querySelector('.needs-row-sub').textContent]))"
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        vals = _drive(tmp_path, f"{url}/#write_token=T", [{"emulate": SIZES[0][1]}, {"until": ready, "ms": 20000},
            {"eval": "new Promise(r => setTimeout(() => r(1), 600))"}, {"eval": subs}])["values"]
    got = {t: s for t, s in json.loads(vals[-1])}
    assert got["Ship?"] == "blocks: Shipment is waiting on legal", got
    assert got["Use A"] == "blocks: Use All of it", got
    assert got["Cut it?"] == "blocks: the rest waits", got
