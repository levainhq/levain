"""The phone app's envelope form is the sign sheet's form: one shared script (envelope_form.js), not a second copy.

phone.html is served from the templates by a small local server (the page's own reads, the listing and the server's review, are
answered inside the page, so every request the form sends to ``/coord/envelope-review`` is recorded as the page sent it) and driven
in headless Chrome at a phone's width. That request's ``spec`` is the contract: the keys where, lands, stop, budget, as ever."""
from __future__ import annotations

import http.server
import json
import re
import threading
from pathlib import Path

import levain
from tests.test_cockpit_narrow_layout_rig import PHONE_TOUCH, drive_touch, needs_browser
from tests.test_msgslot_hero import _tagged
from tests.test_sign_sheet import _raw, sheet  # noqa: F401  (sheet is a fixture)

WEB = Path(levain.__file__).parent / "templates" / "web"
REPOS = ["/work/alpha", "/work/beta", "/work/gamma"]
VIEW_LINES = ["envelope ev-phone", "it may work in the folder you chose"]
STANDARD = {"where": [{"repo": "/work/alpha", "paths": ["."]}], "lands": [], "stop": "8h",
            "budget": {"tool_calls": 800, "tokens": 8000000, "hours": 3}}
PAGES = {"/phone.html": "text/html", "/phone.js": "text/javascript", "/phone.css": "text/css", "/phone_app.js": "text/javascript",
         "/phone_app.css": "text/css", "/card_format.js": "text/javascript", "/envelope_form.js": "text/javascript"}

INIT = """(() => {
  window.__reviews = [];
  try { sessionStorage.setItem('levain-phone-token', 'phone-test-token'); } catch (e) {}
  const real = window.fetch.bind(window);
  const json = (o) => new Response(JSON.stringify(o), {status: 200, headers: {'Content-Type': 'application/json'}});
  window.fetch = (u, o) => {
    const url = String(u);
    if (url.indexOf('/consent.json') >= 0) {
      return Promise.resolve(json({pending: [], undecided: [], sign_pending: [], questions: [], dispatch_pending: [], notes: [], enrolled: [],
        coord_drops: [], coord_note: null, coord_repos: %(repos)s, coord_store_id: 'st-1', coord_store_read: true,
        coord_signers: ['SHA256:abcdefabcdefabcdefabcdef'], coord_signer_note: null, rp_id: location.hostname}));
    }
    if (url.indexOf('/coord/envelope-review') >= 0) {
      const b = JSON.parse(o.body);
      window.__reviews.push(b);
      return new Promise((r) => setTimeout(() => r(json({review_id: 'rv-' + window.__reviews.length, body_text: 'BODY', digest: 'dg',
        view: %(view)s, store_id: 'st-1', store_created: false})), 700));
    }
    if (/[.](js|css|html|png|webmanifest)([?]|$)/.test(url)) return real(u, o);
    return Promise.resolve(json({}));
  };
})()""" % {"repos": json.dumps(REPOS), "view": json.dumps(VIEW_LINES)}


class _Quiet(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a) -> None:
        return

    def do_GET(self) -> None:
        path = self.path.split("?")[0]
        name = "/phone.html" if path == "/" else path
        if name not in PAGES:
            self.send_response(404)
            self.end_headers()
            return
        data = (WEB / name.lstrip("/")).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", PAGES[name] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _serve():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _js(js: str) -> dict:
    return {"eval": js}


def _tag(t: str, js: str) -> dict:
    return {"eval": f"JSON.stringify(Object.assign({{t: {json.dumps(t)}}}, (() => {{ {js} }})()))"}


def _click(sel: str) -> dict:
    return _js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); if (!e) return 'missing'; e.click(); return 1; }})()")


def _set(sel: str, value: str) -> dict:
    return _js(f"(() => {{ const e = document.querySelector({json.dumps(sel)}); e.value = {json.dumps(value)}; "
               "e.dispatchEvent(new Event('input', {bubbles: true})); e.dispatchEvent(new Event('change', {bubbles: true})); return 1; })()")


def _reviews(n: int) -> dict:
    return {"until": f"window.__reviews.length >= {n}", "ms": 20000}


def _last(tag: str) -> dict:
    return _js(f"JSON.stringify({{t: {json.dumps(tag)}, n: window.__reviews.length, spec: (window.__reviews[window.__reviews.length - 1] || {{}}).spec}})")


def _open_drop(words: str = "tidy the changelog") -> list:
    return list(PHONE_TOUCH) + [
        {"init": INIT},
        {"until": "document.getElementById('tab-drop')", "ms": 30000},
        _click("#tab-drop"),
        {"until": "document.querySelectorAll('#drop-repo-pick input[type=radio]').length === 4", "ms": 30000},
        _set("#drop-words", words)]


def _run(tmp_path: Path, steps: list) -> dict:
    httpd = _serve()
    try:
        return drive_touch(tmp_path, f"http://127.0.0.1:{httpd.server_address[1]}/phone.html", steps)
    finally:
        httpd.shutdown()


@needs_browser
def test_the_phone_drop_tab_shows_the_plain_form_in_one_column_and_the_old_number_fields_are_gone(tmp_path: Path) -> None:
    steps = _open_drop() + [
        _reviews(1),
        {"until": "!document.getElementById('drop-sign').disabled", "ms": 10000},
        _tag("form", """
          const q = (s) => document.querySelector(s);
          const vis = (e) => !!e && e.getClientRects().length > 0;
          const left = (s) => q(s).getBoundingClientRect().left, top = (s) => q(s).getBoundingClientRect().top;
          return {details: !!q('details#drop-env'), datalist: !!q('#drop-repos'),
                  numbersShown: [...document.querySelectorAll('#drop input[type=number]')].filter(vis).length,
                  oldLabels: /signed with your passkey|a repo on the laptop|land onto \(a branch|stop after/.test(q('#drop').textContent),
                  pick: [...document.querySelectorAll('#drop-repo-pick .env-opt-name')].map((e) => e.textContent),
                  checkedRepo: [...document.querySelectorAll('#drop-repo-pick input')].filter((e) => e.checked).map((e) => e.value),
                  land: [...document.querySelectorAll('input[name=drop-land]')].filter((e) => e.checked).map((e) => e.value),
                  landN: document.querySelectorAll('input[name=drop-land]').length,
                  seg: [...document.querySelectorAll('#drop-size .env-seg-btn')].map((e) => e.textContent + ':' + e.getAttribute('aria-checked')),
                  customShown: vis(q('#drop-custom')), summary: q('#drop-summary').textContent, signText: q('#drop-sign').textContent,
                  oneColumn: left('#env-repo') === left('#env-land') && left('#env-land') === left('#env-size') && top('#env-size') > top('#env-land'),
                  over: document.documentElement.scrollWidth > document.documentElement.clientWidth,
                  vw: document.documentElement.clientWidth};""")]
    t = _tagged(_run(tmp_path, steps))["form"]
    assert not t["details"] and not t["datalist"] and t["numbersShown"] == 0 and not t["oldLabels"], t
    assert t["pick"] == ["alpha", "beta", "gamma", "Another folder…"] and t["checkedRepo"] == ["/work/alpha"], t
    assert t["land"] == ["none"] and t["landN"] == 3, t                                     # "Don't land" by default
    assert t["seg"] == ["Quick:false", "Standard:true", "Long:false", "Custom:false"] and not t["customShown"], t
    assert t["summary"] == "\n".join(VIEW_LINES) and t["signText"] == "Sign with your passkey", t   # the server's own reading
    assert t["oneColumn"] and not t["over"] and t["vw"] == 390, t


@needs_browser
def test_a_default_phone_envelope_sends_standards_exact_spec_and_never_zeros(tmp_path: Path) -> None:
    steps = _open_drop() + [_reviews(1), _last("standard")]
    t = _tagged(_run(tmp_path, steps))["standard"]
    assert t["n"] == 1 and t["spec"] == STANDARD, t
    assert t["spec"]["lands"] == [] and 0 not in t["spec"]["budget"].values(), t           # a default envelope is not "no new steps allowed"


@needs_browser
def test_phone_custom_starts_from_the_preset_and_a_blank_is_never_sent_as_zero(tmp_path: Path) -> None:
    steps = _open_drop() + [
        _reviews(1),
        _click("#drop-size-quick"), _reviews(2),
        _click("#drop-size-custom"),
        _tag("start", "return {tokens: document.getElementById('drop-tokens').value, calls: document.getElementById('drop-calls').value, "
                      "hours: document.getElementById('drop-hours').value, stop: document.getElementById('drop-stop').value};"),
        _set("#drop-tokens", ""),
        _tag("blank", "return {n: window.__reviews.length, btn: document.getElementById('drop-sign').disabled, "
                      "text: document.getElementById('drop-summary').textContent};"),
        _js("new Promise((r) => setTimeout(() => r(1), 1500))"),
        _tag("later", "return {n: window.__reviews.length};"),
        _set("#drop-tokens", "0"), _reviews(3), _last("typed")]
    t = _tagged(_run(tmp_path, steps))
    assert t["start"] == {"t": "start", "tokens": "2000000", "calls": "200", "hours": "1", "stop": "2h"}, t["start"]   # from Quick
    assert t["blank"]["btn"] is True and "Fill in every limit" in t["blank"]["text"], t["blank"]
    assert t["later"]["n"] == 2, t["later"]                                                  # the blank box asked for no review at all
    assert t["typed"]["spec"]["budget"] == {"tool_calls": 200, "tokens": 0, "hours": 1}, t["typed"]   # a 0 only when typed


@needs_browser
def test_the_phone_sign_button_waits_for_a_current_review(tmp_path: Path) -> None:
    steps = _open_drop() + [
        _tag("asked", "return {btn: document.getElementById('drop-sign').disabled, text: document.getElementById('drop-summary').textContent};"),
        {"until": "!document.getElementById('drop-sign').disabled", "ms": 10000},
        _click("#drop-land-main"),
        _tag("changed", "return {btn: document.getElementById('drop-sign').disabled};"),
        _reviews(2),
        {"until": "!document.getElementById('drop-sign').disabled", "ms": 10000},
        _set("#drop-words", ""),
        _tag("empty", "return {btn: document.getElementById('drop-sign').disabled, text: document.getElementById('drop-summary').textContent};")]
    t = _tagged(_run(tmp_path, steps))
    assert t["asked"]["btn"] is True and "Reading" in t["asked"]["text"], t["asked"]        # the review is still out: nothing to sign
    assert t["changed"]["btn"] is True, t["changed"]                                         # a change retires the review at once
    assert t["empty"]["btn"] is True and "words" in t["empty"]["text"], t["empty"]


def test_the_size_table_lives_only_in_the_shared_form_and_the_pages_load_it(sheet) -> None:  # noqa: F811
    served = {n: _raw(sheet["url"], n)[2].decode() for n in ("/sign.js", "/envelope_form.js", "/card_format.js")}
    table = re.compile(r"\bquick\s*:\s*\{[^}]*tool_calls")
    assert table.search(served["/envelope_form.js"]) and "judged by levain 7" in served["/envelope_form.js"]
    phone = (WEB / "phone.js").read_text()
    for name, text in (("sign.js", served["/sign.js"]), ("phone.js", phone), ("card_format.js", served["/card_format.js"])):
        assert not table.search(text) and "SIZES" not in text and "tool_calls: 800" not in text and "tokens: 8000000" not in text, name
    assert "LevainEnvelopeForm" in served["/sign.js"] and "LevainEnvelopeForm" in phone
    html = (WEB / "phone.html").read_text()
    assert html.index('src="/envelope_form.js"') < html.index('src="/phone.js"')
