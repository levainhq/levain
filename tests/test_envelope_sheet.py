"""The envelope form (sign.js's drop sheet, opened from the cockpit's "Add an envelope" and on its own as the phone's sheet).

The page runs in headless Chrome over the cockpit rig. The two reads the form depends on are answered inside the page: the listing
(three known folders and one passkey that may sign) and the server's review (a canned reading), so every request the form sends to
``envelope-review`` is recorded as the page sent it. That request's ``spec`` is the contract: the keys where, lands, stop, budget,
with the shapes the signing path has always taken. ``ENVELOPE_SHOTS=<dir>`` keeps the screens."""
from __future__ import annotations

import json
import os
from pathlib import Path

from tests.test_cockpit_narrow_layout_rig import NEEDS, PHONE_TOUCH, TOK, FullRig, drive_touch, needs_browser, needs_ssh
from tests.test_msgslot_hero import READY, _tagged, _type

REPOS = ["/work/alpha", "/work/beta", "/work/gamma"]
VIEW_LINES = ["envelope ev-test", "it may work in the folder you chose"]

INIT = """(() => {
  const real = window.fetch.bind(window);
  const top = window.top;
  top.__reviews = top.__reviews || [];
  const json = (o) => Promise.resolve(new Response(JSON.stringify(o), {status: 200, headers: {'Content-Type': 'application/json'}}));
  window.fetch = (u, o) => {
    const url = String(u);
    if (url.indexOf('/cockpit/coord/drops.json') >= 0) {
      return json({coord_drops: [], coord_note: null, coord_repos: %(repos)s, coord_store_id: 'st-1', coord_store_read: true,
                   coord_signers: ['SHA256:abcdefabcdefabcdefabcdef'], coord_signer_note: null, origin: location.origin,
                   rp_id: location.hostname, credentials: {}});
    }
    if (url.indexOf('/cockpit/coord/envelope-review') >= 0) {
      const b = JSON.parse(o.body);
      top.__reviews.push(b);
      return json({review_id: 'rv-' + top.__reviews.length, body_text: 'BODY', digest: 'dg', rp_id: location.hostname,
                   view: %(view)s, store_id: 'st-1', store_created: false});
    }
    return real(u, o);
  };
})()""" % {"repos": json.dumps(REPOS), "view": json.dumps(VIEW_LINES)}

FR = "document.getElementById('levain-sign-sheet-frame')"
FD = f"({FR} && {FR}.contentDocument)"
REVIEWS = "(window.__reviews || [])"


def _shot(tmp_path: Path, name: str) -> dict:
    folder = Path(os.environ.get("ENVELOPE_SHOTS") or tmp_path)
    folder.mkdir(parents=True, exist_ok=True)
    return {"shot": str(folder / f"envelope_{name}.png"), "full": False}


def _in(doc: str, js: str) -> str:
    return f"(() => {{ const d = {doc}; if (!d) return null; {js} }})()"


def _click(doc: str, sel: str) -> dict:
    return {"eval": _in(doc, f"const e = d.querySelector({json.dumps(sel)}); if (!e) return 'missing'; e.click(); return 1;")}


def _set(doc: str, sel: str, value: str) -> dict:
    return {"eval": _in(doc, f"const e = d.querySelector({json.dumps(sel)}); e.value = {json.dumps(value)}; "
                             "e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); return 1;")}


def _reviews(n: int) -> dict:
    return {"until": f"{REVIEWS}.length >= {n}", "ms": 20000}


def _tag(t: str, doc: str, js: str) -> dict:
    return {"eval": f"JSON.stringify(Object.assign({{t: {json.dumps(t)}}}, {_in(doc, js)}))"}


def _open_from_cockpit(view: str) -> list:
    emu = {"emulate": {"w": 1280, "h": 900, "dpr": 1}} if view == "1280" else None
    pre = [emu] if emu else list(PHONE_TOUCH)
    return pre + [{"init": INIT}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
                  _type("tidy the changelog"),
                  {"eval": "document.querySelector('#ck-drop [data-coord-drop=envelope]').click(); 1"},
                  {"until": _in(FD, "return !!d.getElementById('drop-review') && /ev-test/.test(d.getElementById('drop-review').textContent);"), "ms": 30000}]


STANDARD = {"where": [{"repo": "/work/alpha", "paths": ["."]}], "lands": [], "stop": "8h",
            "budget": {"tool_calls": 800, "tokens": 8000000, "hours": 3}}


def _run(tmp_path: Path, monkeypatch, view: str, steps: list) -> dict:
    rig = FullRig(tmp_path, monkeypatch)
    with rig.serving():
        return drive_touch(tmp_path, f"{rig.origin}/#write_token={TOK}", steps)


@needs_browser
@needs_ssh
def test_embedded_form_is_the_modal_body_at_1280_and_390(tmp_path: Path, monkeypatch) -> None:
    for view in ("1280", "390"):
        sub = tmp_path / view
        sub.mkdir()
        steps = _open_from_cockpit(view) + [
            _tag("layout", FD, """
              const q = (s) => d.querySelector(s);
              const vis = (e) => !!e && e.getClientRects().length > 0;
              const fr = parent.document.getElementById('levain-sign-sheet-frame').getBoundingClientRect();
              const modal = parent.document.querySelector('#levain-sign-sheet .modal').getBoundingClientRect();
              const l = q('#env-words').getBoundingClientRect(), r = q('#env-size').getBoundingClientRect();
              const scrollers = [...d.querySelectorAll('body *')].filter((e) => { const o = getComputedStyle(e).overflowY;
                return (o === 'auto' || o === 'scroll') && e.scrollHeight > e.clientHeight + 1 && e.tagName !== 'TEXTAREA'; }).length;
              return {h2: parent.document.querySelector('#levain-sign-sheet .modal-head h2').textContent,
                      brand: vis(q('.pair-brand')), h1: vis(q('#sheet-title')), frameW: fr.width, modalW: modal.width,
                      over: d.documentElement.scrollWidth > d.documentElement.clientWidth, scrollers: scrollers,
                      sideBySide: r.left > l.left + 50, card: Math.round(q('#sheet').getBoundingClientRect().width), vw: d.documentElement.clientWidth};"""),
            _shot(sub, f"embedded_{view}")]
        out = _run(sub, monkeypatch, view, steps)
        t = _tagged(out)["layout"]
        assert t["h2"] == "Add an envelope" and not t["brand"] and not t["h1"], t
        assert t["frameW"] >= t["modalW"] - 2, t                      # the frame is the modal's body
        assert not t["over"] and t["scrollers"] == 0, t               # no sideways scroll, no scroll box inside the page
        assert t["card"] >= t["vw"] - 2, t                            # no 420px card cap
        assert t["sideBySide"] is (view == "1280"), t                 # two columns on a laptop, one on a phone
        if view == "1280":
            assert t["modalW"] > 900, t                               # a wide modal, not the narrow default


@needs_browser
@needs_ssh
def test_repo_is_picked_and_another_folder_reveals_the_path(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("1280") + [
        _tag("pick", FD, """
          const radios = [...d.querySelectorAll('#drop-repo-pick input[type=radio]')];
          return {names: [...d.querySelectorAll('#drop-repo-pick .env-opt-name')].map((e) => e.textContent),
                  subs: [...d.querySelectorAll('#drop-repo-pick .env-opt-sub')].map((e) => e.textContent),
                  checked: radios.filter((r) => r.checked).map((r) => r.value),
                  pathShown: d.getElementById('drop-repo').getClientRects().length > 0};"""),
        _click(FD, "#drop-repo-pick label:last-child input"),
        _tag("other", FD, """
          return {pathShown: d.getElementById('drop-repo').getClientRects().length > 0,
                  hint: d.querySelector('#env-repo .env-other .env-hint').textContent,
                  btnDisabled: d.getElementById('drop-sign').disabled,
                  review: d.getElementById('drop-review').textContent};"""),
        _set(FD, "#drop-repo", "/elsewhere/project"), _reviews(2),
        {"eval": f"JSON.stringify({{t: 'spec', last: {REVIEWS}[{REVIEWS}.length - 1].spec}})"}]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["pick"]["names"] == ["alpha", "beta", "gamma", "Another folder…"], t["pick"]
    assert t["pick"]["subs"] == REPOS and t["pick"]["checked"] == ["/work/alpha"] and not t["pick"]["pathShown"], t["pick"]
    assert t["other"]["pathShown"] and t["other"]["hint"].startswith("the full path of a folder on this machine"), t["other"]
    assert t["other"]["btnDisabled"] is True, t["other"]       # nothing named yet: nothing to sign
    assert t["spec"]["last"]["where"] == [{"repo": "/elsewhere/project", "paths": ["."]}], t["spec"]


@needs_browser
@needs_ssh
def test_each_size_and_landing_sends_exactly_its_spec_and_custom_starts_from_the_preset(tmp_path: Path, monkeypatch) -> None:
    def last(tag: str) -> dict:
        return {"eval": f"JSON.stringify({{t: {json.dumps(tag)}, n: {REVIEWS}.length, spec: ({REVIEWS}[{REVIEWS}.length - 1] || {{}}).spec}})"}

    steps = _open_from_cockpit("1280") + [
        last("standard"),
        _tag("btn0", FD, "return {disabled: d.getElementById('drop-sign').disabled, text: d.getElementById('drop-sign').textContent, "
                         "review: d.getElementById('drop-review').textContent, custom: d.getElementById('drop-custom').hidden};"),
        _click(FD, "#drop-size-quick"), _reviews(2), last("quick"),
        _click(FD, "#drop-size-long"), _reviews(3), last("long"),
        _click(FD, "#drop-land-main"), _reviews(4), last("main"),
        _click(FD, "#drop-land-other"), _tag("noBranch", FD, "return {review: d.getElementById('drop-review').textContent, btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-onto", "release"), _reviews(5), last("other"),
        _click(FD, "#drop-land-none"), _reviews(6),
        _click(FD, "#drop-size-quick"), _reviews(7),
        _click(FD, "#drop-size-custom"),
        _tag("custom", FD, "return {shown: !d.getElementById('drop-custom').hidden, stop: d.getElementById('drop-stop').value, hours: d.getElementById('drop-hours').value, "
                           "calls: d.getElementById('drop-calls').value, tokens: d.getElementById('drop-tokens').value};"),
        _reviews(8), last("customStart"),
        _set(FD, "#drop-tokens", ""),
        _tag("blank", FD, "return {review: d.getElementById('drop-review').textContent, btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-tokens", "0"), _set(FD, "#drop-hours", "5"), _set(FD, "#drop-stop", "12h"), _reviews(9), last("customTyped"),
        _shot(tmp_path, "custom_1280")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["standard"]["spec"] == STANDARD and t["standard"]["n"] == 1, t["standard"]
    assert t["btn0"]["disabled"] is False and t["btn0"]["text"] == "Sign with your passkey" and t["btn0"]["custom"] is True, t["btn0"]
    assert t["btn0"]["review"] == "\n".join(VIEW_LINES), t["btn0"]                    # the server's reading, as sent
    assert t["quick"]["spec"] == {**STANDARD, "stop": "2h", "budget": {"tool_calls": 200, "tokens": 2000000, "hours": 1}}, t["quick"]
    assert t["long"]["spec"] == {**STANDARD, "stop": "24h", "budget": {"tool_calls": 2500, "tokens": 25000000, "hours": 8}}, t["long"]
    assert t["main"]["spec"]["lands"] == [{"repo": "/work/alpha", "onto": "main"}], t["main"]
    assert t["noBranch"]["btn"] is True and "branch" in t["noBranch"]["review"], t["noBranch"]
    assert t["other"]["spec"]["lands"] == [{"repo": "/work/alpha", "onto": "release"}], t["other"]
    assert t["custom"] == {"t": "custom", "shown": True, "stop": "2h", "hours": "1", "calls": "200", "tokens": "2000000"}, t["custom"]   # from Quick, the size last chosen
    assert t["customStart"]["spec"] == {**STANDARD, "stop": "2h", "budget": {"tool_calls": 200, "tokens": 2000000, "hours": 1}}, t["customStart"]
    assert t["blank"]["btn"] is True and "Fill in every limit" in t["blank"]["review"], t["blank"]        # a blank box is never sent as 0
    assert t["customTyped"]["n"] == 9, t["customTyped"]
    assert t["customTyped"]["spec"]["budget"] == {"tool_calls": 200, "tokens": 0, "hours": 5} and t["customTyped"]["spec"]["stop"] == "12h", t["customTyped"]


@needs_browser
@needs_ssh
def test_the_sign_button_waits_for_a_current_review(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("1280") + [
        _tag("ready", FD, "return {btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-words", "tidy the changelog, and the readme"),
        _tag("typed", FD, "return {btn: d.getElementById('drop-sign').disabled, review: d.getElementById('drop-review').textContent};"),
        _reviews(2),
        {"until": _in(FD, "return !d.getElementById('drop-sign').disabled;"), "ms": 10000},
        _set(FD, "#drop-words", ""),
        _tag("empty", FD, "return {btn: d.getElementById('drop-sign').disabled, review: d.getElementById('drop-review').textContent};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["ready"]["btn"] is False                                  # opened with words, a folder and Standard: one tap away
    assert t["typed"]["btn"] is True and "Reading" in t["typed"]["review"], t["typed"]   # a change retires the review at once
    assert t["empty"]["btn"] is True and "words" in t["empty"]["review"], t["empty"]


@needs_browser
@needs_ssh
def test_the_phone_sheet_keeps_its_card_with_the_same_plain_fields_in_one_column(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = list(PHONE_TOUCH) + [
        {"eval": "window.localStorage.setItem('levain_write_token', %s); 1" % json.dumps(TOK)},
        {"init": INIT},
        {"eval": f"(() => {{ location.href = '{rig.origin}/sign/drop'; return 1; }})()"},
        {"until": "document.getElementById('drop-words')", "ms": 30000},
        {"eval": "(() => { const w = document.getElementById('drop-words'); w.value = 'phone words'; w.dispatchEvent(new Event('input', {bubbles: true})); return 1; })()"},
        {"until": "(window.__reviews || []).length >= 1", "ms": 20000},
        {"eval": "JSON.stringify({t: 'p', embedded: document.body.classList.contains('sheet-embedded'), brand: document.querySelector('.pair-brand').getClientRects().length > 0, "
                 "pick: document.querySelectorAll('#drop-repo-pick input').length, seg: document.querySelectorAll('#drop-size .env-seg-btn').length, "
                 "land: document.querySelectorAll('input[name=drop-land]').length, over: document.documentElement.scrollWidth > window.innerWidth, "
                 "oneColumn: document.getElementById('env-size').getBoundingClientRect().left === document.getElementById('env-words').getBoundingClientRect().left, "
                 "spec: window.__reviews[0].spec})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/nope", steps)
    p = _tagged(out)["p"]
    assert not p["embedded"] and p["brand"] and p["pick"] == 4 and p["seg"] == 4 and p["land"] == 3, p
    assert not p["over"] and p["oneColumn"] and p["spec"] == STANDARD, p
