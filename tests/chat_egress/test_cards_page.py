"""CH-D2 S7, the page half: a real `levain_egress` card, filed in a real consent queue, served by the real web server
and drawn by the cockpit page in headless Chrome at phone width, with hostile field values. The DOM is read back.

Rows: R15-10-A16 (the fields render as structured rows; markup is text; no raw bidi or control character reaches the
page; the removed names and the key line are drawn before the long path), C9 / R15-12-B7 (a card whose client left
reads ENDED). What the card looks like to him on his phone, signed with Face ID, is S10's (residue, not here).

`LEVAIN_S7_SHOTS=<dir>` keeps the phone-width screenshots there (the default is the test's tmp dir).
"""

from __future__ import annotations

import dataclasses
import json
import os
from datetime import timedelta
from pathlib import Path

from levain.chat_egress import classify, parse, strip
from levain.chat_egress.cards import card_input, file_card
from levain.consent.queue import ConsentQueue, authority_source
from tests.chat_egress.test_classify import frame, route_for
from tests.test_cockpit_consent_rows import OPEN_DECISIONS
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install, _serve

NARROW = {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}}
LONG_PATH = "/repos/o/r/git/refs/" + "a" * 400 + "/tail-end"
CARD = ("[...document.querySelectorAll('#ck-needs .needs-consent')]"
        ".find(c => (c.querySelector('.consent-tool') || {}).textContent === 'levain_egress' && c.querySelector('.egress-kv'))")
HIDDEN = r"/[\u0000-\u001f\u007f-\u009f؜​-‏‪-‮⁠-⁩﻿]/"


def _hostile(audit: str) -> dict:
    fr = frame(method="POST", host="api.github.com", path=LONG_PATH, query="q=SECRETQUERYVALUE",
               headers={"authorization": "Bearer TOPSECRETTOKEN", "cookie": "s=1", "x-levain-audit": audit})
    p = parse.parse(fr)
    v = classify.decide(p, route_for(p), classify.GATED)
    p = dataclasses.replace(p, params=(dataclasses.replace(p.params[0], raw_key="⁦q<i>x</i>"),))
    v = dataclasses.replace(v, method="<b>P</b>‮", card_header_names=v.card_header_names + ("x-‮evil\x07",))
    return card_input(p, v, strip.strip(p).stripped, body_len=3, body_sha256="ab" * 32, launch_id="launch-1")


@needs_browser
def test_a_hostile_egress_card_draws_as_text_rows_with_the_removed_names_before_the_path(tmp_path, monkeypatch):
    home = tmp_path / "levain_home"
    monkeypatch.setenv("LEVAIN_HOME", str(home))
    (home / "consent").mkdir(parents=True, mode=0o700)
    q = ConsentQueue(home / "consent", authority=authority_source(home / "consent", anchor=None))
    file_card(q, _hostile("0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"), agent_session_id="s", cwd="/w")
    gone = file_card(q, _hostile("1c7a6f4e-4d2e-4f9f-8b4f-3e8c2a1d0b22"), agent_session_id="s", cwd="/w")
    q.file(agent="claude_code", agent_session_id="s", tool_name="Bash", tool_input={"command": "ls"}, cwd="/w",
           hold=timedelta(minutes=5), event_id="keep")    # a plain card beside it, drawn as before
    assert q.withdraw(gone.id, note="egress: the client closed")   # C0's CARD_ENDED, as S5b's composer does it
    # a body that carries `egress_state` (card_state over the egress rows) is drawn as that: no server sends one yet, so
    # the page's fetch is rewritten to add it for this card only (AMBIGUOUS, R15-10-A8)
    amb = file_card(q, _hostile("2d8b7a5f-5e3f-4a0a-9c5a-4f9d3b2e1c33"), agent_session_id="s", cwd="/w")
    assert q.withdraw(amb.id, note="egress: the client closed")
    stub = ("(() => { const real = window.fetch.bind(window); const id = " + json.dumps(amb.id) + ";"
            " window.fetch = async (u, o) => { const r = await real(u, o);"
            " if (!String(u).includes('/cockpit/consent/' + encodeURIComponent(id))) return r;"
            " const b = await r.clone().json(); b.egress_state = 'ambiguous';"
            " return new Response(JSON.stringify(b), {status: r.status, headers: r.headers}); }; return 1; })()")
    shots = Path(os.environ.get("LEVAIN_S7_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    read = ("(() => { const c = " + CARD + "; const dl = c.querySelector('.egress-kv');"
            " const dts = [...dl.querySelectorAll('dt')].map(e => e.textContent);"
            " const dd = (label) => dl.querySelectorAll('dd')[dts.indexOf(label)];"
            " const top = (label) => dd(label).getBoundingClientRect().top;"
            " return JSON.stringify({ dts, method: dd('method').textContent, removed: dd('removed').textContent,"
            "  key: dd('Levain adds your key for').textContent, names: dd('header names').textContent,"
            "  query: dd('query names').textContent, path: dd('path').textContent,"
            "  markup: c.querySelectorAll('dd *').length, raw_hidden: " + HIDDEN + ".test(c.textContent),"
            "  pwned: !!window.__pwned, state: (c.querySelector('.egress-state') || {}).textContent,"
            "  removed_above_path: top('removed') < top('path'), key_above_path: top('Levain adds your key for') < top('path'),"
            "  removed_in_view: top('removed') < window.innerHeight,"
            "  plain_pre: [...document.querySelectorAll('#ck-needs .needs-consent')].filter(x => x.querySelector('.consent-display')).length,"
            "  card_text: c.textContent }); })()")

    def row_of(rid: str) -> str:
        return f"document.querySelector('#levain-modal .consent-row[data-card=\"consent:{rid}\"]')"

    def open_row(rid: str) -> str:
        return f"(() => {{ const r = {row_of(rid)}; r.querySelector('.consent-details').click(); return 1; }})()"

    def drawn(rid: str) -> str:
        return (f"[({row_of(rid)}.querySelector('.consent-audit') || {{}}).textContent,"
                f" ({row_of(rid)}.querySelector('.egress-state') || {{}}).textContent]")
    tagged = row_of(gone.id)
    with _serve(_install(tmp_path)[1], surface_tokens={"browser": "T"}, install_class="reported") as (url, _httpd):
        out = _drive(tmp_path, f"{url}/#write_token=T", [
            NARROW,
            {"until": CARD},
            # the card at the top of the phone's screen: "in view" then means on the card's first screen
            {"eval": f"(() => {{ {CARD}.scrollIntoView({{block: 'start'}}); return 1; }})()"},
            {"eval": read},
            {"shot": str(shots / "s7_card_phone.png")},
            {"eval": stub},
            {"eval": OPEN_DECISIONS},
            {"until": f"{row_of(gone.id)} && {row_of(amb.id)} && {row_of(amb.id)}.querySelector('.consent-details')"},
            {"eval": open_row(gone.id)},
            {"eval": open_row(amb.id)},
            {"until": f"{row_of(amb.id)}.querySelector('.egress-state') && {tagged}.querySelector('.egress-state')"},
            {"eval": drawn(amb.id)},
            {"eval": drawn(gone.id)},
            {"eval": f"new Promise((res) => {{ {tagged}.scrollIntoView({{block: 'start'}}); setTimeout(() => res(1), 600); }})"},
            {"shot": str(shots / "s7_ended_phone.png")},
        ])["values"]
    assert out[-4] == ["AMBIGUOUS", "AMBIGUOUSyou allowed it, then Levain stopped before the result: it may or may not"
                       " have been sent"], out[-4]
    got = json.loads(out[2])
    assert got["dts"] == ["method", "scheme", "host", "port", "Levain adds your key for", "removed", "why it asks",
                          "held", "body bytes", "body", "header names", "query names", "path", "whole path length",
                          "whole path", "launch", "request"], got["dts"]
    assert got["method"] == "<b>P</b>⟨U+202E⟩" and got["markup"] == 0 and not got["pwned"], got
    assert not got["raw_hidden"], got                                   # no raw bidi or control character on the page
    assert "x-⟨U+202E⟩evil⟨U+0007⟩" in got["names"] and got["query"] == "⟨U+2066⟩q<i>x</i>", got
    assert got["removed"] == "authorization, cookie" and got["key"] == "api.github.com", got
    assert got["path"].startswith("/repos/o/r/git/refs/aaa") and got["path"].endswith("/tail-end") and "…(+" in got["path"]
    assert got["removed_above_path"] and got["key_above_path"] and got["removed_in_view"], got
    assert "SECRETQUERYVALUE" not in got["card_text"] and "TOPSECRETTOKEN" not in got["card_text"], got
    assert got["state"].startswith("PENDING"), got
    assert got["plain_pre"] == 1, got                                   # the Bash card still draws as it did
    assert out[-3] == ["ENDED", "ENDEDthe request's client left; nothing was sent after it left"], out[-3]
