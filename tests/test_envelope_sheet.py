"""The envelope form (sign.js's drop sheet, opened from the cockpit's "Add an envelope" and on its own at /sign/drop, which a phone sees).

The page runs in headless Chrome over the cockpit rig. The two reads the form depends on are answered inside the page: the listing
(three known folders and one passkey that may sign) and the server's review (a canned reading), so every request the form sends to
``envelope-review`` is recorded as the page sent it. That request's ``spec`` is the contract: the keys where, lands, stop, budget,
with the shapes the signing path has always taken. ``ENVELOPE_SHOTS=<dir>`` keeps the screens."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import levain
from tests.test_cockpit_narrow_layout_rig import NEEDS, PHONE_TOUCH, TOK, FullRig, drive_touch, needs_browser, needs_ssh
from tests.test_msgslot_hero import READY, _tagged, _type

WEB = Path(levain.__file__).parent / "templates" / "web"


def test_the_size_table_lives_only_in_the_envelope_form() -> None:
    table = re.compile(r"\bquick\s*:\s*\{[^}]*tool_calls")
    form = (WEB / "envelope_form.js").read_text()
    assert table.search(form) and "judged by levain 7" in form
    for name in ("sign.js", "card_format.js"):
        text = (WEB / name).read_text()
        assert not table.search(text) and "SIZES" not in text and "tool_calls: 800" not in text and "tokens: 8000000" not in text, name
    assert "LevainEnvelopeForm" in (WEB / "sign.js").read_text()


REPOS = ["/work/alpha", "/work/beta", "/work/gamma"]
VIEW_LINES = ["envelope ev-test", "it may work in the folder you chose", "digest sha256:" + "0123456789abcdef" * 12]   # one long unbroken line

# The page's two reads are answered inside it. CFG sets how the fake server behaves: store (the listing's store id, None for a fresh
# install), hold (how many first reviews are held until the test releases them), strict (a review that names no store while another
# such review is still unanswered is refused 409 other_store, as the real store refuses it), refuse (a review is refused 422, with
# the given extra keys in the body), challenge ("ok" or "gone").
INIT_JS = """(() => {
  const CFG = __CFG__;
  const real = window.fetch.bind(window);
  const top = window.top;
  top.__reviews = top.__reviews || [];
  top.__challenges = top.__challenges || [];
  top.__held = top.__held || [];
  top.__conflicts = top.__conflicts || 0;
  top.__holdLeft = top.__holdLeft === undefined ? CFG.hold : top.__holdLeft;
  top.__nullOut = top.__nullOut || 0;
  top.__forced = top.__forced || 0;
  top.__frameLists = top.__frameLists || 0;
  top.__heldList = top.__heldList || [];
  top.__skew = top.__skew || 0;
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + (top.__skew || 0);
  const reply = (status, o) => new Response(JSON.stringify(o), {status: status, headers: {'Content-Type': 'application/json'}});
  const json = (o) => Promise.resolve(reply(200, o));
  // a drop an earlier visit kept unanswered, bound to store st-a: the sheet sends it again when it opens
  if (CFG.pending && window !== top && !top.__seeded) {
    top.__seeded = 1;
    try { localStorage.setItem('levain-drop-pending:' + location.hostname + ':ob-old',
      JSON.stringify({words: 'an earlier drop', outbox_id: 'ob-old', sent_at: '2026-10-31T10:00:00.000000Z', store_id: 'st-a'})); } catch (e) { /* none kept */ }
  }
  try { navigator.credentials.get = () => new Promise(() => undefined); } catch (e) { /* the page's own */ }
  window.fetch = (u, o) => {
    const url = String(u);
    if (url.indexOf('/cockpit/coord/drops.json') >= 0) {
      const sheet = window !== top;
      if (sheet) top.__frameLists += 1;
      const later = sheet && top.__frameLists > 1;
      const body = {coord_drops: [], coord_note: null, coord_repos: __REPOS__, coord_store_id: later && 'storeAfter' in CFG ? CFG.storeAfter : CFG.store,
                    coord_store_read: true, coord_signers: ['SHA256:abcdefabcdefabcdefabcdef'], coord_signer_note: null, origin: location.origin,
                    rp_id: location.hostname, credentials: {}};
      if (later && CFG.holdList) return new Promise((r) => top.__heldList.push(() => r(reply(200, body))));
      return json(body);
    }
    if (url.indexOf('/cockpit/coord/drop') >= 0 && 'dropStore' in CFG) {
      const b = JSON.parse(o.body);
      top.__drops = top.__drops || [];
      top.__drops.push(b);
      return json({created: false, drop: {drop_id: 'dr-old', words: b.words, sent_at: b.sent_at, receipts: []}, envelope: null,
                   woken: null, store_id: CFG.dropStore});
    }
    if (url.indexOf('/cockpit/coord/envelope-challenge') >= 0) {
      top.__challenges.push(JSON.parse(o.body));
      if (CFG.challenge === 'gone') return Promise.resolve(reply(409, {error: 'review_gone', message: 'this envelope review is no longer held here'}));
      return json({challenge: 'AAAA', payload: {}, rp_id: location.hostname, line: '', store_id: 'st-1', store_created: false});
    }
    if (url.indexOf('/cockpit/coord/envelope-review') >= 0) {
      const b = JSON.parse(o.body);
      top.__reviews.push(b);
      const n = top.__reviews.length;
      const nullStore = b.store_id === null;
      if (CFG.strict && nullStore && top.__nullOut > 0) {
        top.__conflicts += 1;
        return Promise.resolve(reply(409, {error: 'other_store', message: 'this page read another Levain store'}));
      }
      if (top.__forced < (CFG.conflictFirst || 0) || (CFG.serverStore && typeof b.store_id === 'string' && b.store_id !== CFG.serverStore)) {
        top.__forced += 1;
        top.__conflicts += 1;
        return Promise.resolve(reply(409, {error: 'other_store', message: 'this page read another Levain store'}));
      }
      if (CFG.refuse) return Promise.resolve(reply(CFG.refuse.status || 422, Object.assign({error: 'refused', message: CFG.refuse.message}, CFG.refuse.extra || {})));
      const bad = n <= (CFG.badFirst || 0);
      if (bad && 'rawBody' in CFG) return Promise.resolve(new Response(CFG.rawBody, {status: 200, headers: {'Content-Type': 'application/json'}}));
      // the hard stop as the server computes it: from the review, in its fixed-width UTC form
      const hours = parseInt((b.spec && b.spec.stop) || '2', 10);
      const stamp = (ms) => new Date(ms).toISOString().slice(0, 19) + '.000000Z';
      const readMs = Date.now() + (CFG.serverOffset || 0);       // the server's own clock, which may differ from the page's
      const stopAt = stamp(readMs + hours * 3600000);
      top.__lastStopAt = stopAt;
      const ok = () => {
        const r = {review_id: 'rv-' + n, body_text: 'BODY' + n, digest: 'dg', rp_id: location.hostname, stop_at: stopAt,
                   view: __VIEW__.concat(['review ' + n]), store_id: nullStore ? 'st-new' : b.store_id,
                   store_created: nullStore};
        // the server's own reading of its clock, in case a form ever ages a review from it: none may
        if (CFG.serverOffset) r.reviewed_at = stamp(readMs);
        if (bad && 'view' in CFG) { if (CFG.view === 'missing') delete r.view; else r.view = CFG.view; }
        if (bad && 'stopAt' in CFG) { if (CFG.stopAt === 'missing') delete r.stop_at; else r.stop_at = CFG.stopAt; }
        return r;
      };
      if (nullStore) top.__nullOut += 1;
      if (top.__holdLeft > 0) {
        top.__holdLeft -= 1;
        return new Promise((r) => top.__held.push(() => { if (nullStore) top.__nullOut -= 1; r(reply(200, ok())); }));
      }
      if (nullStore) top.__nullOut -= 1;
      return json(ok());
    }
    return real(u, o);
  };
})()"""


def make_init(store: "str | None" = "st-1", hold: int = 0, strict: bool = False, refuse: "dict | None" = None, challenge: str = "ok", **more) -> str:
    # more: conflictFirst (the first n reviews are refused other_store), serverStore (a review naming another store is refused
    # other_store), storeAfter (the sheet's later listing reads name this store), holdList (those reads are held until released),
    # badFirst with view (the first n reviews carry this view; "missing" leaves it out) or rawBody (they answer 200 with this text)
    # or stopAt (their stop_at is this text; "missing" leaves it out), serverOffset (ms: the server's clock reads that far from the page's; a negative one is a page running ahead),
    # pending (a drop bound to st-a is kept unanswered) with dropStore (the store a drop's answer names)
    cfg = {"store": store, "hold": hold, "strict": strict, "refuse": refuse, "challenge": challenge, **more}
    return (INIT_JS.replace("__CFG__", json.dumps(cfg)).replace("__REPOS__", json.dumps(REPOS)).replace("__VIEW__", json.dumps(VIEW_LINES)))


INIT = make_init()

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


def _open_from_cockpit(view: str, init: str = INIT, wait: bool = True) -> list:
    emu = {"emulate": {"w": 1280, "h": 900, "dpr": 1}} if view == "1280" else None
    pre = [emu] if emu else list(PHONE_TOUCH)
    return pre + [{"init": init}, {"until": NEEDS, "ms": 30000}, {"until": READY, "ms": 30000},
                  _type("tidy the changelog"),
                  {"eval": "document.querySelector('#ck-drop [data-coord-drop=envelope]').click(); 1"},
                  {"until": _in(FD, "return !!d.getElementById('drop-review') && /ev-test/.test(d.getElementById('drop-review').textContent);") if wait
                   else _in(FD, "return !!d.getElementById('drop-review') && " + REVIEWS + ".length >= 1;"), "ms": 30000}]


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
    assert t["btn0"]["review"].startswith("\n".join(VIEW_LINES + ["review 1"]) + "\n\nHard stop at "), t["btn0"]   # the server's reading, as sent
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
def test_the_launch_toggle_is_off_by_default_clamped_to_the_servers_bounds_and_absent_on_the_phone_page(tmp_path: Path, monkeypatch) -> None:
    def last(tag: str) -> dict:
        return {"eval": f"JSON.stringify({{t: {json.dumps(tag)}, n: {REVIEWS}.length, spec: ({REVIEWS}[{REVIEWS}.length - 1] || {{}}).spec}})"}

    steps = _open_from_cockpit("1280") + [
        last("off"),
        _tag("ui", FD, "return {on: d.getElementById('drop-launch').checked, countShown: d.getElementById('drop-launch-count').getClientRects().length > 0, "
                       "dflt: d.getElementById('drop-launch-count').value};"),
        _click(FD, "#drop-launch"), _reviews(2), last("on"),
        _tag("shown", FD, "return {countShown: d.getElementById('drop-launch-count').getClientRects().length > 0};"),
        _set(FD, "#drop-launch-count", "5"),
        _tag("five", FD, "return {review: d.getElementById('drop-review').textContent, btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-launch-count", "0"),
        _tag("zero", FD, "return {btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-launch-count", "3"), _reviews(3), last("three"),
        _click(FD, "#drop-launch"), _reviews(4), last("offAgain")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["off"]["spec"] == STANDARD and "launches" not in t["off"]["spec"], t["off"]
    assert t["ui"] == {"t": "ui", "on": False, "countShown": False, "dflt": "2"}, t["ui"]
    assert t["on"]["spec"] == {**STANDARD, "launches": [{"repo": "/work/alpha", "count": 2}]}, t["on"]
    assert t["shown"]["countShown"] is True, t["shown"]
    assert t["five"]["btn"] is True and "launches[0].count is an integer from 1 to 4" in t["five"]["review"], t["five"]
    assert t["zero"]["btn"] is True, t["zero"]
    assert t["three"]["spec"]["launches"] == [{"repo": "/work/alpha", "count": 3}], t["three"]
    assert "launches" not in t["offAgain"]["spec"], t["offAgain"]       # turned off again: the key leaves the spec


@needs_browser
@needs_ssh
def test_the_phone_page_has_no_launch_toggle(tmp_path: Path, monkeypatch) -> None:
    rig = FullRig(tmp_path, monkeypatch)
    steps = list(PHONE_TOUCH) + [
        {"eval": "window.localStorage.setItem('levain_write_token', %s); 1" % json.dumps(TOK)},
        {"init": INIT},
        {"eval": f"(() => {{ location.href = '{rig.origin}/sign/drop'; return 1; }})()"},
        {"until": "document.getElementById('drop-words')", "ms": 30000},
        {"eval": "JSON.stringify({t: 'p', toggle: !!document.getElementById('drop-launch'), group: !!document.getElementById('env-launch')})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/nope", steps)
    assert _tagged(out)["p"] == {"t": "p", "toggle": False, "group": False}


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
                 "pick: document.querySelectorAll('#drop-repo-pick input').length, seg: document.querySelectorAll('#drop-size input[type=radio]').length, "
                 "land: document.querySelectorAll('input[name=drop-land]').length, over: document.documentElement.scrollWidth > window.innerWidth, "
                 "oneColumn: document.getElementById('env-size').getBoundingClientRect().left === document.getElementById('env-words').getBoundingClientRect().left, "
                 "spec: window.__reviews[0].spec})"}]
    with rig.serving():
        out = drive_touch(tmp_path, f"{rig.origin}/nope", steps)
    p = _tagged(out)["p"]
    assert not p["embedded"] and p["brand"] and p["pick"] == 4 and p["seg"] == 4 and p["land"] == 3, p
    assert not p["over"] and p["oneColumn"] and p["spec"] == STANDARD, p


# ---- the review's life: stale replies, the store's first identity, a gone review, and what the person is told ----

def _sleep(ms: int) -> dict:
    return {"eval": f"new Promise((r) => setTimeout(() => r(1), {ms}))"}


def _release() -> dict:
    return {"eval": "(() => { const f = window.__held.shift(); if (!f) return 'none'; f(); return 1; })()"}


def _top(t: str, js: str) -> dict:
    return {"eval": "JSON.stringify(Object.assign({t: " + json.dumps(t) + "}, (() => { " + js + " })()))"}


SIGN_ON = {"until": _in(FD, "return !d.getElementById('drop-sign').disabled;"), "ms": 10000}
SUMMARY = "d.getElementById('drop-review').textContent"


@needs_browser
@needs_ssh
def test_a_fresh_install_asks_one_review_at_a_time_until_the_store_has_an_identity(tmp_path: Path, monkeypatch) -> None:
    # the first review makes the store: a second one that still names no store would be refused other_store and leave the form stuck
    steps = _open_from_cockpit("1280", make_init(store=None, hold=1, strict=True), wait=False) + [
        _set(FD, "#drop-words", "tidy the changelog, and the readme"),
        _sleep(1500),
        _top("mid", "return {n: window.__reviews.length, conflicts: window.__conflicts};"),
        _release(), _reviews(2), SIGN_ON,
        _top("end", "return {n: window.__reviews.length, conflicts: window.__conflicts, stores: window.__reviews.map((r) => r.store_id)};"),
        _tag("shown", FD, "return {review: " + SUMMARY + ", errs: [...d.querySelectorAll('.env-err')].filter((e) => !e.hidden).length};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["mid"] == {"t": "mid", "n": 1, "conflicts": 0}, t["mid"]                       # the second waits for the first
    assert t["end"]["conflicts"] == 0 and t["end"]["stores"] == [None, "st-new"], t["end"]  # and then names the store the first made
    assert "review 2" in t["shown"]["review"] and t["shown"]["errs"] == 0, t["shown"]


@needs_browser
@needs_ssh
def test_a_late_review_never_enables_or_shows_for_a_form_that_has_moved_on(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("1280", make_init(hold=2), wait=False) + [
        _set(FD, "#drop-words", "tidy the changelog, and the readme"), _reviews(2),
        _release(),                                                                         # review 1 answers late, after the edit
        _sleep(300),
        _tag("late", FD, "return {btn: d.getElementById('drop-sign').disabled, review: " + SUMMARY + ", live: d.getElementById('env-live').textContent};"),
        _click(FD, "#drop-sign"), _sleep(300),
        _top("noTap", "return {challenges: window.__challenges.length};"),                  # a disabled button posts nothing
        _release(), SIGN_ON,
        _tag("current", FD, "return {review: " + SUMMARY + ", live: d.getElementById('env-live').textContent};"),
        {"eval": _in(FD, "const b = d.getElementById('drop-sign'); b.click(); b.click(); return 1;")},
        _sleep(800),
        _top("tapped", "return {challenges: window.__challenges};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["late"]["btn"] is True and "Reading" in t["late"]["review"] and "review 1" not in t["late"]["review"], t["late"]
    assert t["late"]["live"] == "", t["late"]                                              # the late reply is not announced either
    assert t["noTap"]["challenges"] == 0, t["noTap"]
    assert "review 2" in t["current"]["review"] and t["current"]["live"] == t["current"]["review"], t["current"]
    assert t["tapped"]["challenges"] == [{"review_id": "rv-2"}], t["tapped"]               # the review in hand, once, for a double tap


@needs_browser
@needs_ssh
def test_a_review_the_server_no_longer_holds_is_asked_for_again(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("1280", make_init(challenge="gone")) + [
        _click(FD, "#drop-sign"), _reviews(2), SIGN_ON,
        _top("end", "return {n: window.__reviews.length, challenges: window.__challenges};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["end"]["n"] == 2 and t["end"]["challenges"] == [{"review_id": "rv-1"}], t["end"]


@needs_browser
@needs_ssh
def test_the_selected_size_does_nothing_when_pressed_and_custom_numbers_are_whole_numbers_within_the_servers_bounds(tmp_path: Path, monkeypatch) -> None:
    fields = "const f = (k) => d.getElementById('drop-' + k);"
    steps = _open_from_cockpit("1280") + [
        _click(FD, "#drop-size-standard"), _sleep(1300),
        _top("same", "return {n: window.__reviews.length};"),
        _tag("stops", FD, "const F = d.defaultView.LevainEnvelopeForm; const opts = [...d.querySelectorAll('#drop-stop option')].map((o) => o.value);"
                          "return {ok: Object.values(F.SIZES).every((s) => opts.indexOf(s.stop) >= 0), opts: opts};"),
        _click(FD, "#drop-size-custom"), _reviews(2),
        _tag("attrs", FD, fields + "return {step: f('calls').step, max: [f('hours').max, f('calls').max, f('tokens').max], min: f('calls').min};"),
        _set(FD, "#drop-calls", "2.5"),
        _tag("frac", FD, "return {review: " + SUMMARY + ", btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-calls", "100001"),
        _tag("big", FD, "return {review: " + SUMMARY + ", btn: d.getElementById('drop-sign').disabled};"),
        _set(FD, "#drop-hours", "25"),
        _tag("hours", FD, "return {review: " + SUMMARY + "};"),
        _set(FD, "#drop-hours", "1"), _set(FD, "#drop-calls", "0"), _reviews(3),
        {"until": _in(FD, "return /nothing can start/.test(" + SUMMARY + ");"), "ms": 10000},
        _tag("zero", FD, fields + "const w = f('calls-warn');"
                         "return {warn: w ? w.textContent : null, shown: !!w && w.getClientRects().length > 0, review: " + SUMMARY + ", btn: d.getElementById('drop-sign').disabled};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["same"]["n"] == 1, t["same"]                                                  # pressing the chosen size retires no review
    assert t["stops"]["ok"] is True, t["stops"]                                            # every preset's stop is a Custom choice
    assert t["attrs"] == {"t": "attrs", "step": "1", "max": ["24", "100000", "1000000000"], "min": "0"}, t["attrs"]
    assert t["frac"]["btn"] is True and "budget.tool_calls is an integer from 0 to 100000" in t["frac"]["review"], t["frac"]
    assert t["big"]["btn"] is True and "budget.tool_calls is an integer from 0 to 100000" in t["big"]["review"], t["big"]
    assert "budget.hours is an integer from 0 to 24" in t["hours"]["review"], t["hours"]
    assert t["zero"]["warn"] == "0 tool calls means nothing can start" and t["zero"]["shown"], t["zero"]
    assert "0 tool calls means nothing can start" in t["zero"]["review"] and t["zero"]["btn"] is False, t["zero"]   # a warning, not a block


def test_the_forms_client_side_bounds_are_the_servers() -> None:
    from levain.coordinator.envelope import BUDGET_MAX

    src = (WEB / "envelope_form.js").read_text()
    m = re.search(r"const BUDGET_MAX = \{([^}]*)\}", src)
    assert m, "the form keeps the server's bounds in one BUDGET_MAX table"
    mine = {k: int(v) for k, v in re.findall(r"(\w+)\s*:\s*(\d+)", m.group(1))}
    assert mine == {k: BUDGET_MAX[k] for k in ("tool_calls", "tokens", "hours")}, mine


@needs_browser
@needs_ssh
def test_the_form_is_native_radios_with_hints_tied_to_their_inputs_and_only_a_settled_review_is_announced(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("1280") + [
        _tag("a11y", FD, """
          const ids = (e) => (e.getAttribute('aria-describedby') || '').split(' ').filter(Boolean);
          const tied = (e) => { const t = ids(e).map((i) => d.getElementById(i)); return t.length > 0 && t.every((n) => !!n && n.textContent.length > 0); };
          const fields = [...d.querySelectorAll('.env-field')].map((f) => f.querySelector('input, select'));
          const groups = [...d.querySelectorAll('fieldset.env-group')].filter((g) => g.querySelector(':scope > .env-hint'));
          const live = d.getElementById('env-live');
          return {radios: [...d.querySelectorAll('#drop-size input[type=radio]')].map((r) => r.value), roleRadio: d.querySelectorAll('[role=radio]').length,
                  labelled: d.querySelectorAll('fieldset [role=radiogroup][aria-label]').length,
                  fieldsTied: fields.every(tied), fieldN: fields.length, groupsTied: groups.every(tied), groupN: groups.length,
                  repoPath: tied(d.getElementById('drop-repo')), summaryLive: d.getElementById('drop-review').hasAttribute('aria-live') || d.getElementById('drop-review').closest('[aria-live]').getAttribute('aria-live') !== 'off',
                  liveAttr: live.getAttribute('aria-live'), liveText: live.textContent, review: """ + SUMMARY + """};"""),
        _set(FD, "#drop-words", "tidy the changelog, and the readme"),
        _tag("reading", FD, "return {live: d.getElementById('env-live').textContent, review: " + SUMMARY + "};"),
        _reviews(2), SIGN_ON,
        _tag("settled", FD, "return {live: d.getElementById('env-live').textContent, review: " + SUMMARY + "};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    a = t["a11y"]
    assert a["radios"] == ["quick", "standard", "long", "custom"] and a["roleRadio"] == 0 and a["labelled"] == 0, a
    assert a["fieldN"] == 5 and a["fieldsTied"] and a["groupN"] == 4 and a["groupsTied"] and a["repoPath"], a
    assert a["summaryLive"] is False and a["liveAttr"] == "polite" and a["liveText"] == a["review"], a
    assert t["reading"]["live"] == "" and "Reading" in t["reading"]["review"], t["reading"]   # the wait is never announced
    assert t["settled"]["live"] == t["settled"]["review"] and "review 2" in t["settled"]["live"], t["settled"]


@needs_browser
@needs_ssh
def test_the_form_says_what_each_limit_counts_and_what_happens_next(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("1280") + [_tag("text", FD, "return {text: d.body.textContent.replace(/\\s+/g, ' ')};")]
    text = _tagged(_run(tmp_path, monkeypatch, "1280", steps))["text"]["text"]
    for want in ("An envelope is a permission slip you sign: which folder it may work in, whether it may land, for how long, and how much it may do.",
                 "The limits count across the whole thread, all its workers together; extending sets a new total.",
                 "Hard stop after", "counted from when the summary is read, not from when you sign",
                 "fresh tokens: input, output and new cache writes (cache reads don't count)",
                 "These numbers are starting points",
                 "Reaching a limit: no new workers start until you extend. Nothing running is killed.",
                 "Hard stop: it ends at this time regardless.",
                 "sends your words with no limits attached",
                 "then the coordinator may start work inside these limits"):
        assert want.lower() in text.lower(), want
    assert "Ends by" not in text and "pauses new work" not in text


@needs_browser
@needs_ssh
def test_a_refusal_goes_where_the_server_named_it_else_under_the_summary_and_the_button_says_why(tmp_path: Path, monkeypatch) -> None:
    probe = ("return {where: [...d.querySelectorAll('.env-err')].filter((e) => !e.hidden).map((e) => e.parentElement.id + ': ' + e.textContent),"
             "btn: d.getElementById('drop-sign').disabled, why: d.getElementById('drop-sign-why').textContent,"
             "by: d.getElementById('drop-sign').getAttribute('aria-describedby')};")
    msg = "budget.tokens is an integer from 0 to 1000000000"
    # the server's words end in its own ": nothing was signed", which the form does not repeat; a 5xx is the cockpit's fault, so
    # the person is told how to ask again
    for name, refuse, parent in (("plain", {"message": msg + ": nothing was signed."}, "env-sum"),
                                 ("named", {"message": msg, "extra": {"field": "budget"}}, "env-size"),
                                 ("down", {"message": msg + ": the drop was not delivered", "status": 503}, "env-sum")):
        sub = tmp_path / name
        sub.mkdir()
        steps = _open_from_cockpit("1280", make_init(refuse=refuse), wait=False) + [
            {"until": _in(FD, "return /cannot be signed/.test(" + SUMMARY + ");"), "ms": 10000}, _tag("refused", FD, probe)]
        r = _tagged(_run(sub, monkeypatch, "1280", steps))["refused"]
        assert len(r["where"]) == 1 and r["where"][0].startswith(parent + ": ") and msg in r["where"][0], r
        assert "nothing was signed" not in r["where"][0] and "was not delivered" not in r["where"][0], r
        assert ("change anything to ask again" in r["where"][0]) is (name == "down"), r
        assert r["btn"] is True and r["why"] and r["by"] == "drop-sign-why", r
    steps = _open_from_cockpit("1280") + [_set(FD, "#drop-words", ""), _tag("empty", FD, probe)]
    sub = tmp_path / "empty"
    sub.mkdir()
    e = _tagged(_run(sub, monkeypatch, "1280", steps))["empty"]
    assert e["btn"] is True and "words" in e["why"], e


@needs_browser
@needs_ssh
def test_at_phone_width_the_sign_button_is_reachable_without_scrolling(tmp_path: Path, monkeypatch) -> None:
    steps = _open_from_cockpit("390") + [
        _tag("sticky", FD, "d.defaultView.scrollTo(0, 0); const b = d.getElementById('drop-sign').getBoundingClientRect();"
                           "return {bottom: b.bottom, top: b.top, h: d.defaultView.innerHeight, tall: d.documentElement.scrollHeight};"),
        _shot(tmp_path, "sticky_390")]
    s = _tagged(_run(tmp_path, monkeypatch, "390", steps))["sticky"]
    assert s["tall"] > s["h"] and 0 <= s["top"] and s["bottom"] <= s["h"], s


@needs_browser
@needs_ssh
def test_the_repo_default_is_said_aloud_until_the_person_chooses_or_types(tmp_path: Path, monkeypatch) -> None:
    probe = ("const h = d.getElementById('env-repo-default');"
             "return {text: h ? h.textContent : null, shown: !!h && h.getClientRects().length > 0, checked: [...d.querySelectorAll('#drop-repo-pick input')].filter((r) => r.checked).map((r) => r.value)};")
    for name, step in (("typed", _set(FD, "#drop-repo", "/elsewhere")), ("picked", _click(FD, "#drop-repo-pick label:nth-child(2) input"))):
        sub = tmp_path / name
        sub.mkdir()
        t = _tagged(_run(sub, monkeypatch, "1280", _open_from_cockpit("1280") + [_tag("before", FD, probe), step, _tag("after", FD, probe)]))
        # the listing's first folder is the most recently active thread's; the hint names it
        assert t["before"]["text"] == "Pre-picked: alpha (/work/alpha), the folder of your most recently active thread. Change it if that's wrong." and t["before"]["shown"], t["before"]
        assert t["after"]["shown"] is False, t["after"]
        if name == "typed":
            assert t["after"]["checked"] == ["/work/alpha"], t["after"]      # typing marks the choice as the person's, it does not move the pick


# ---- the store a sheet is bound to, a reply that cannot be signed, a stuck "Reading…", and the hard stop's time ----

def _top_eval(js: str) -> dict:
    return {"eval": "(() => { " + js + " })()"}


STORES = "window.__reviews.map((r) => r.store_id)"
SIGN_OFF = "d.getElementById('drop-sign').disabled"


@needs_browser
@needs_ssh
def test_a_fresh_install_whose_first_review_meets_a_store_reads_it_and_asks_once_more(tmp_path: Path, monkeypatch) -> None:
    # sent with no store id: the store was made meanwhile, so the page reads its identity and asks again, once
    steps = _open_from_cockpit("1280", make_init(store=None, conflictFirst=1, storeAfter="st-b"), wait=False) + [
        SIGN_ON,
        _top("end", f"return {{stores: {STORES}, conflicts: window.__conflicts, lists: window.__frameLists}};"),
        _tag("shown", FD, "return {review: " + SUMMARY + "};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["end"]["stores"] == [None, "st-b"] and t["end"]["conflicts"] == 1 and t["end"]["lists"] == 2, t["end"]
    assert "review 2" in t["shown"]["review"], t["shown"]


@needs_browser
@needs_ssh
def test_a_sheet_bound_to_one_store_never_rebinds_to_another_and_never_signs_into_it(tmp_path: Path, monkeypatch) -> None:
    # opened for store A; store B answers now. B refuses A's review, and the sheet says so and stays off: it never reads B's
    # identity and asks again (that would sign into a store the person never opened the sheet for)
    moved = "different Levain store"
    probe = ("return {review: " + SUMMARY + ", btn: " + SIGN_OFF + ", why: d.getElementById('drop-sign-why').textContent};")
    steps = _open_from_cockpit("1280", make_init(store="st-a", serverStore="st-b", storeAfter="st-b"), wait=False) + [
        {"until": _in(FD, "return /" + moved + "/.test(" + SUMMARY + ");"), "ms": 10000},
        _sleep(800),
        _top("after", f"return {{stores: {STORES}, lists: window.__frameLists}};"),
        _tag("refused", FD, probe),
        _set(FD, "#drop-words", "tidy the changelog, and the readme"), _sleep(1500),
        _top("edited", f"return {{stores: {STORES}, lists: window.__frameLists}};"),
        _tag("still", FD, probe)]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["after"] == {"t": "after", "stores": ["st-a"], "lists": 1}, t["after"]          # no retry, no re-read of the store
    r = t["refused"]
    assert r["review"].startswith("This sheet was opened for a different Levain store than the one answering now. Reload the page "
                                  "to see the current store; nothing was signed."), r
    assert r["btn"] is True and moved in r["why"], r
    assert t["edited"] == {"t": "edited", "stores": ["st-a"], "lists": 1}, t["edited"]     # an edit does not ask the other store either
    assert t["still"]["btn"] is True and moved in t["still"]["review"], t["still"]


@needs_browser
@needs_ssh
def test_an_edit_while_the_store_is_read_again_is_asked_for_once_the_read_returns(tmp_path: Path, monkeypatch) -> None:
    # the 409 retry's read of the store is slow; an edit made meanwhile waits its turn, and when the read returns the retry is
    # stale: the edit is the one asked for, never left waiting on "Reading…"
    steps = _open_from_cockpit("1280", make_init(store=None, conflictFirst=1, storeAfter="st-b", holdList=True), wait=False) + [
        {"until": "(window.__heldList || []).length >= 1", "ms": 10000},
        _set(FD, "#drop-words", "tidy the changelog, and the readme"), _sleep(1200),
        _top("mid", f"return {{stores: {STORES}}};"),
        _top_eval("const f = window.__heldList.shift(); f(); return 1;"),
        SIGN_ON,
        _top("end", f"return {{stores: {STORES}, words: window.__reviews.map((r) => r.words)}};"),
        _tag("shown", FD, "return {review: " + SUMMARY + "};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["mid"]["stores"] == [None], t["mid"]                                               # the edit waits for the store
    assert t["end"]["stores"] == [None, "st-b"], t["end"]
    assert t["end"]["words"] == ["tidy the changelog", "tidy the changelog, and the readme"], t["end"]
    assert "review 2" in t["shown"]["review"], t["shown"]


@needs_browser
@needs_ssh
def test_a_reply_with_no_summary_never_enables_sign_and_never_stalls_the_form(tmp_path: Path, monkeypatch) -> None:
    # a 200 whose view is empty, null or missing, or whose body is not an object, shows nothing to sign; on a fresh install (one
    # review at a time) the next edit is still asked for
    empty = "The server sent an empty summary; nothing can be signed. Change anything to ask again."
    for name, more in (("empty", {"view": []}), ("null", {"view": None}), ("missing", {"view": "missing"}), ("text", {"rawBody": '"hello"'})):
        sub = tmp_path / name
        sub.mkdir()
        steps = _open_from_cockpit("1280", make_init(store=None, badFirst=1, **more), wait=False) + [
            {"until": _in(FD, "return /empty summary/.test(" + SUMMARY + ");"), "ms": 10000},
            _sleep(300),
            _tag("bad", FD, "return {review: " + SUMMARY + ", btn: " + SIGN_OFF + "};"),
            _set(FD, "#drop-words", "tidy the changelog, and the readme"), _reviews(2), SIGN_ON,
            _tag("next", FD, "return {review: " + SUMMARY + "};")]
        t = _tagged(_run(sub, monkeypatch, "1280", steps))
        assert t["bad"]["review"] == empty and t["bad"]["btn"] is True, (name, t["bad"])
        assert "review 2" in t["next"]["review"], (name, t["next"])


@needs_browser
@needs_ssh
def test_the_summary_says_when_the_hard_stop_is_and_a_summary_over_five_minutes_old_is_read_again(tmp_path: Path, monkeypatch) -> None:
    # the stop is computed when the summary is read, so the summary says the time it lands on, and an old one is read again
    steps = _open_from_cockpit("1280") + [
        _tag("stop", FD, "const at = new Date(Date.parse(window.top.__lastStopAt));"
                         "return {review: " + SUMMARY + ", hm: at.toLocaleTimeString([], {hour: '2-digit', minute: '2-digit'})};"),
        _top_eval("window.__skew = 6 * 60000; return 1;"),
        {"until": f"{REVIEWS}.length >= 2", "ms": 25000}, SIGN_ON,
        _top("again", "return {n: window.__reviews.length};"),
        _tag("fresh", FD, "return {review: " + SUMMARY + "};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    s = t["stop"]
    assert ("Hard stop at " + s["hm"]) in s["review"] and "set when this summary was read" in s["review"], s
    assert t["again"]["n"] == 2 and "review 2" in t["fresh"]["review"], t


@needs_browser
@needs_ssh
def test_a_kept_drop_answered_by_another_store_never_rebinds_the_sheet(tmp_path: Path, monkeypatch) -> None:
    # opened for store A, the sheet sends a drop an earlier visit kept; store B answers it as delivered. The answer's store id
    # is never taken over the one held: the sheet says so and nothing more is asked of, or signed into, either store
    moved = "different Levain store"
    probe = ("return {review: " + SUMMARY + ", btn: " + SIGN_OFF + ", note: d.getElementById('sheet-note').textContent};")
    opened = _open_from_cockpit("1280", make_init(store="st-a", pending=True, dropStore="st-b"), wait=False)[:-1]
    steps = opened + [
        {"until": "(window.__drops || []).length >= 1", "ms": 20000},
        _sleep(1500),
        _top("after", f"return {{drops: window.__drops.map((d) => d.store_id), stores: {STORES}}};"),
        _tag("shown", FD, probe),
        _set(FD, "#drop-words", "tidy the changelog, and the readme"), _sleep(1500),
        _top("edited", f"return {{drops: window.__drops.length, stores: {STORES}}};"),
        _tag("still", FD, probe)]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["after"]["drops"] == ["st-a"] and "st-b" not in t["after"]["stores"], t["after"]
    assert t["edited"]["drops"] == 1 and "st-b" not in t["edited"]["stores"], t["edited"]  # never re-bound to the store that answered
    s = t["shown"]
    assert "delivered as dr-old" in s["note"] and moved in s["note"], s                    # what happened, and what it means
    assert s["btn"] is True and moved in s["review"], s
    assert t["still"]["btn"] is True and moved in t["still"]["review"], t["still"]


@needs_browser
@needs_ssh
def test_a_store_still_unnamed_after_the_read_stops_asking_even_when_edited_meanwhile(tmp_path: Path, monkeypatch) -> None:
    # sent with no store id and refused other_store; the person edits while the store is read again; the read names no
    # store. The sheet stops: the edit waiting its turn is not sent, and a later edit sends nothing either
    no_store = "Levain has not said which store answers here yet"
    probe = "return {review: " + SUMMARY + ", btn: " + SIGN_OFF + "};"
    steps = _open_from_cockpit("1280", make_init(store=None, conflictFirst=9, holdList=True), wait=False) + [
        {"until": "(window.__heldList || []).length >= 1", "ms": 10000},
        _set(FD, "#drop-words", "tidy the changelog, and the readme"), _sleep(1200),
        _top_eval("const f = window.__heldList.shift(); f(); return 1;"),
        _sleep(1500),
        _top("read", "return {n: window.__reviews.length, conflicts: window.__conflicts};"),
        _tag("stuck", FD, probe),
        _set(FD, "#drop-words", "tidy the changelog, the readme and the notes"), _sleep(1500),
        _top("edited", "return {n: window.__reviews.length, conflicts: window.__conflicts};"),
        _tag("still", FD, probe)]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["read"] == {"t": "read", "n": 1, "conflicts": 1}, t["read"]
    assert no_store in t["stuck"]["review"] and t["stuck"]["btn"] is True, t["stuck"]
    assert t["edited"] == {"t": "edited", "n": 1, "conflicts": 1}, t["edited"]
    assert no_store in t["still"]["review"] and t["still"]["btn"] is True, t["still"]


@needs_browser
@needs_ssh
def test_a_device_clock_far_ahead_of_the_server_still_signs(tmp_path: Path, monkeypatch) -> None:
    # a review is aged from its arrival, so a server whose clock reads three hours behind the page's changes nothing: one
    # review, signable, no second ask
    steps = _open_from_cockpit("1280", make_init(serverOffset=-3 * 3600000), wait=False) + [
        _sleep(2500), SIGN_ON,
        _top("asked", "return {n: window.__reviews.length};"),
        _tag("shown", FD, "return {review: " + SUMMARY + "};")]
    t = _tagged(_run(tmp_path, monkeypatch, "1280", steps))
    assert t["asked"]["n"] == 1 and "review 1" in t["shown"]["review"], t


@needs_browser
@needs_ssh
def test_a_reply_with_no_usable_stop_never_enables_sign(tmp_path: Path, monkeypatch) -> None:
    words = "The server did not say when this envelope would stop; nothing can be signed. Change anything to ask again."
    for name, stop in (("missing", "missing"), ("garbage", "tomorrow-ish")):
        sub = tmp_path / name
        sub.mkdir()
        steps = _open_from_cockpit("1280", make_init(badFirst=1, stopAt=stop), wait=False) + [
            {"until": _in(FD, "return /did not say when/.test(" + SUMMARY + ");"), "ms": 10000},
            _sleep(300),
            _tag("bad", FD, "return {review: " + SUMMARY + ", btn: " + SIGN_OFF + "};"),
            _set(FD, "#drop-words", "tidy the changelog, and the readme"), _reviews(2), SIGN_ON,
            _tag("next", FD, "return {review: " + SUMMARY + "};")]
        t = _tagged(_run(sub, monkeypatch, "1280", steps))
        assert t["bad"]["review"] == words and t["bad"]["btn"] is True, (name, t["bad"])
        assert "review 2" in t["next"]["review"], (name, t["next"])
