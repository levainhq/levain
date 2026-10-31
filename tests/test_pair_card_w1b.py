"""W1b slice B, the in-app pairing and pings (project_memory/1030-2-w1b/DESIGN.md sections 3.1, 4.3, 7 B, 9 row B).
The browser tests run in the headless rig (tests/_page_drive.mjs): a scratch writable server, reached under a name the server
treats as proxied (``extra_hosts`` plus Chrome's ``--host-resolver-rules``), so the page is unpaired and off the box, as on his phone."""
from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from levain.web_server import load_web_asset
from tests._page_drive_run import run as run_driver
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call, _serve

CHROME = os.environ.get("CHROME") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
NODE = shutil.which("node")
DRIVE = Path(__file__).with_name("_page_drive.mjs")
needs_browser = pytest.mark.skipif(NODE is None or not Path(CHROME).exists(), reason="node and Chrome are needed")
NAME = "pair.test"


def _drive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, url: str, steps: list[dict[str, Any]],
           flags: list[str] | None = None) -> dict[str, Any]:
    f = tmp_path / "steps.json"
    f.write_text(json.dumps(steps))
    monkeypatch.setenv("LEVAIN_DRIVE_CHROME_FLAGS", json.dumps(flags or []))
    res = run_driver([NODE, str(DRIVE), CHROME, url, str(f)], timeout=240)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def _offbox_flags(port: int) -> list[str]:
    # the name resolves to the scratch server; a worker and the push API need a secure origin, which the page's own name is not
    return [f"--host-resolver-rules=MAP {NAME} 127.0.0.1", f"--unsafely-treat-insecure-origin-as-secure=http://{NAME}:{port}"]


CARD = "document.querySelector('#ck-unpaired .pair-code')"


def _short(port: int) -> str:
    st, out, _ = _call(port, "POST", "/cockpit/pair/offer", {"short": True, "label": "Run phone"}, token=T)   # the owner key, on loopback
    assert st == 200 and re.fullmatch(r"[0-9A-Z]{4}-[0-9A-Z]{4}", out["short"]), (st, out)
    return out["short"]


def _type_and_submit(code: str) -> dict[str, Any]:
    return {"eval": f"(() => {{ const i = {CARD}; i.value = {json.dumps(code.lower())}; "
                    "document.querySelector('#ck-unpaired .pair-form').requestSubmit(); return 1; })()"}


@needs_browser
def test_an_unpaired_page_off_the_box_draws_the_pair_card_and_never_the_owner_prompt(tmp_path, monkeypatch) -> None:
    # rig run 1 (before slice B): the same page raised window.prompt("Write token ...") from the manifest's 403
    _root, src, _ids = _install(tmp_path)
    with _serve(src, extra_hosts=[NAME]) as (port, _h):
        out = _drive(tmp_path, monkeypatch, f"http://{NAME}:{port}/", [
            {"until": CARD, "ms": 15000},
            {"eval": "document.querySelector('#ck-unpaired h1').textContent"},
            {"eval": f"{CARD}.getAttribute('autocomplete')"},
        ], _offbox_flags(port))
    assert out["dialogs"] == [], out["dialogs"]
    assert out["values"][1:] == ["Pair this device", "one-time-code"]


@needs_browser
def test_a_short_claim_keeps_the_key_and_the_mark_drops_the_phone_port_keys_and_updates_the_worker(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, extra_hosts=[NAME]) as (port, _h):
        code = _short(port)
        out = _drive(tmp_path, monkeypatch, f"http://{NAME}:{port}/", [
            {"until": CARD, "ms": 15000},
            # a phone-page install: its keys, in the storage phone_app.js puts each, and its worker registration
            {"eval": "(async () => { sessionStorage.setItem('levain-phone-token', 'old-s'); localStorage.setItem('levain-device-token', 'old-l');"
                     " await navigator.serviceWorker.register('/sw.js', { scope: '/' }); await navigator.serviceWorker.ready;"
                     " window.__upd = 0; const u = ServiceWorkerRegistration.prototype.update;"
                     " ServiceWorkerRegistration.prototype.update = function () { window.__upd++; return u.call(this); }; return 1; })()"},
            _type_and_submit(code),
            {"until": "localStorage.getItem('levain_write_token')", "ms": 15000},
            {"until": "window.__upd >= 1", "ms": 10000},
            {"eval": "JSON.stringify({tok: localStorage.getItem('levain_write_token'), mark: localStorage.getItem('levain_paired_device'),"
                     " s: sessionStorage.getItem('levain-phone-token'), l: localStorage.getItem('levain-device-token')})"},
        ], _offbox_flags(port))
        state = json.loads(out["values"][-1])
        st, devs, _ = _call(port, "GET", "/cockpit/pair/devices.json", token=state["tok"], headers=PROXY)
    claims = [p for p in out["posts"] if p.startswith("/cockpit/pair/claim ")]
    assert claims == [f'/cockpit/pair/claim {{"short":"{code.replace("-", "")}"}}'], claims   # the one key, folded, never a token
    assert state["mark"] == "1" and state["s"] is None and state["l"] is None, state
    assert st == 200 and [d["label"] for d in devs["devices"]] == ["Run phone"], (st, devs)    # the stored key is a live device key


@needs_browser
def test_the_pings_button_asks_for_permission_before_anything_else(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src, extra_hosts=[NAME]) as (port, _h):
        code = _short(port)
        out = _drive(tmp_path, monkeypatch, f"http://{NAME}:{port}/", [
            {"until": CARD, "ms": 15000},
            # the cockpit under test serves no signer, so its key is answered here; the browser calls after it are recorded
            {"eval": "(() => { window.__o = []; const f = window.fetch.bind(window);"
                     " window.fetch = (u, o) => String(u) === '/webpush/key' ? Promise.resolve(new Response(JSON.stringify({ key: 'AAAA' }), { status: 200 })) : f(u, o);"
                     " Notification.requestPermission = () => { window.__o.push('permission'); return Promise.resolve('granted'); };"
                     " navigator.serviceWorker.register = () => { window.__o.push('register'); return Promise.reject(new Error('stop here')); }; return 1; })()"},
            _type_and_submit(code),
            {"until": "[...document.querySelectorAll('#ck-unpaired button')].find((b) => b.textContent === 'Turn on pings')", "ms": 15000},
            {"eval": "[...document.querySelectorAll('#ck-unpaired button')].find((b) => b.textContent === 'Turn on pings').click()"},
            {"until": "window.__o.length >= 2", "ms": 10000},
            {"eval": "JSON.stringify(window.__o)"},
        ], _offbox_flags(port))
    assert json.loads(out["values"][-1]) == ["permission", "register"]


def test_no_postmessage_carries_a_token_and_the_worker_hand_off_is_gone() -> None:
    for name in ("pair_card.js", "devices_panel.js"):
        js = load_web_asset(name)
        assert "postMessage" not in js and "giveToken" not in js and "levain-token" not in js, name


@needs_browser
def test_a_pending_open_is_posted_with_the_pages_key_retried_online_and_kept_on_429_401_403(tmp_path, monkeypatch) -> None:
    # L3 r1: an offline tap waited for the next visibilitychange (no retry when the network came back); any 4xx dropped the key,
    # even a 403 a re-pair could still record; a key that is not a number was posted as {"ping": null}
    _root, src, _ids = _install(tmp_path)
    drain_keys = "window.levainPairCard.drainOpened().then(() => window.__keys())"
    with _serve(src) as (port, _h):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/", [
            {"until": "window.levainPairCard", "ms": 15000},
            # the worker's side of the contract: levain-sw-2 version 1, store "opened", key = the ping id as a string, value = the tap time
            {"eval": "(async () => { localStorage.setItem('levain_write_token', 'TKN'); window.__seen = []; window.__mode = 'offline';"
                     " const f = window.fetch.bind(window);"
                     " window.fetch = (u, o) => { if (String(u) !== '/webpush/opened') return f(u, o);"
                     "   window.__seen.push(JSON.parse(o.body).ping + ':' + o.headers['X-Levain-Write-Token']);"
                     "   return window.__mode === 'offline' ? Promise.reject(new TypeError('offline')) : Promise.resolve(new Response('{}', { status: window.__mode })); };"
                     " const open = () => new Promise((ok, no) => { const q = indexedDB.open('levain-sw-2', 1);"
                     "   q.onupgradeneeded = () => q.result.createObjectStore('opened'); q.onsuccess = () => ok(q.result); q.onerror = no; });"
                     " window.__put = async (k) => { const db = await open(); await new Promise((ok) => { const t = db.transaction('opened', 'readwrite');"
                     "   t.objectStore('opened').put(1, k); t.oncomplete = ok; }); db.close(); };"
                     " window.__keys = async () => { const db = await open(); const r = await new Promise((ok) => {"
                     "   const g = db.transaction('opened').objectStore('opened').getAllKeys(); g.onsuccess = () => ok(g.result); }); db.close(); return JSON.stringify(r); };"
                     " await window.__put('7'); await window.__put('x'); return 1; })()"},
            {"eval": drain_keys},                                                     # offline: both kept
            {"eval": "(() => { window.__mode = 200; window.dispatchEvent(new Event('online')); return 1; })()"},
            {"until": "window.__keys().then((k) => k === '[]')", "ms": 5000},       # the network came back: posted, no other trigger
            {"eval": "window.__put('8').then(() => { window.__mode = 403; return " + drain_keys + "; })"},
            {"eval": "(() => { window.__mode = 401; return " + drain_keys + "; })()"},
            {"eval": "(() => { window.__mode = 429; return " + drain_keys + "; })()"},
            {"eval": "(() => { window.__mode = 404; return " + drain_keys + "; })()"},
            {"eval": "JSON.stringify(window.__seen)"},
        ])
    v = out["values"]
    assert json.loads(v[2]) == ["7", "x"], v[2]
    assert [json.loads(x) for x in v[5:9]] == [["8"], ["8"], ["8"], []], v[5:9]   # 403, 401, 429 keep the key; another 4xx settles it
    assert json.loads(v[9]) == ["7:TKN", "7:TKN", "8:TKN", "8:TKN", "8:TKN", "8:TKN"], v[10]   # "x" is dropped unposted, never {"ping": null}


@needs_browser
def test_turn_on_pings_can_be_tapped_again_after_a_refusal_or_a_failed_subscribe(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    btn = "[...document.querySelectorAll('#ck-unpaired button')].find((b) => b.textContent === 'Turn on pings')"
    msg = "(document.querySelector('#ck-unpaired .pair-msg') || {}).textContent || ''"
    with _serve(src, extra_hosts=[NAME]) as (port, _h):
        code = _short(port)
        out = _drive(tmp_path, monkeypatch, f"http://{NAME}:{port}/", [
            {"until": CARD, "ms": 15000},
            {"eval": "(() => { const f = window.fetch.bind(window); window.__perm = 'denied';"
                     " window.fetch = (u, o) => String(u) === '/webpush/key' ? Promise.resolve(new Response(JSON.stringify({ key: 'AAAA' }), { status: 200 }))"
                     "   : String(u) === '/webpush/subscribe' ? Promise.resolve(new Response('{\"message\": \"no\"}', { status: 500 })) : f(u, o);"
                     " Notification.requestPermission = () => Promise.resolve(window.__perm);"
                     " const sub = { toJSON: () => ({ endpoint: 'https://push.invalid/x', keys: {} }), unsubscribe: () => Promise.resolve(true) };"
                     " const reg = { pushManager: { getSubscription: () => Promise.resolve(null), subscribe: () => Promise.resolve(sub) } };"
                     " navigator.serviceWorker.register = () => Promise.resolve(reg);"
                     " Object.defineProperty(navigator.serviceWorker, 'ready', { configurable: true, get: () => Promise.resolve(reg) }); return 1; })()"},
            _type_and_submit(code),
            {"until": btn, "ms": 15000},
            {"eval": f"{btn}.click()"},
            {"until": f"({msg}).startsWith('Pings stay off')", "ms": 10000},
            {"eval": f"{btn}.disabled"},
            {"eval": f"(() => {{ window.__perm = 'granted'; {btn}.click(); return 1; }})()"},
            {"until": f"({msg}).startsWith('Could not turn pings on (')", "ms": 10000},
            {"eval": f"{btn}.disabled"},
        ], _offbox_flags(port))
    assert out["values"][6] is False and out["values"][9] is False, out["values"]


@needs_browser
def test_the_pair_card_keeps_typed_text_folds_invisible_characters_and_says_a_lost_answer_may_have_paired(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    msg = "(document.querySelector('#ck-unpaired .pair-msg') || {}).textContent || ''"
    with _serve(src, extra_hosts=[NAME]) as (port, _h):
        code = _short(port)
        out = _drive(tmp_path, monkeypatch, f"http://{NAME}:{port}/", [
            {"until": CARD, "ms": 15000},
            # a passive re-read (the page coming back into view) must not rebuild the card under the operator's typing
            {"eval": f"(() => {{ window.__inp = {CARD}; window.__inp.value = 'ab'; document.dispatchEvent(new Event('visibilitychange')); return 1; }})()"},
            {"eval": "new Promise((ok) => setTimeout(() => ok(1), 2000))"},
            {"eval": f"JSON.stringify({{same: {CARD} === window.__inp, value: {CARD}.value}})"},
            # the server drops Cf / Cc characters (a zero-width space, a soft hyphen) before it validates; the page does the same
            {"eval": "window.levainPairCard.fold('ab\\u200bcd-\\u00adef\\u0007gh')"},
            # the claim reaches the cockpit and its answer is lost: the retry's refusal must say the first try may have paired
            {"eval": "(() => { const f = window.fetch.bind(window); window.__lose = true;"
                     " window.fetch = (u, o) => String(u) === '/cockpit/pair/claim' && window.__lose"
                     "   ? f(u, o).then(() => { throw new TypeError('lost'); }) : f(u, o); return 1; })()"},
            _type_and_submit(code),
            {"until": f"({msg}).startsWith('Could not reach')", "ms": 10000},
            {"eval": "(() => { window.__lose = false; document.querySelector('#ck-unpaired .pair-form').requestSubmit(); return 1; })()"},
            {"until": f"!({msg}).startsWith('Could not reach') && ({msg})", "ms": 10000},
        ], _offbox_flags(port))
    v = out["values"]
    assert json.loads(v[3]) == {"same": True, "value": "ab"}, v[3]
    assert v[4] == "ABCDEFGH", v[4]
    assert "may have worked" in v[-1] and "revoke the newest device" in v[-1], v[-1]


@needs_browser
def test_make_a_link_and_show_a_code_are_both_disabled_while_either_offer_is_in_flight(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    both = "JSON.stringify([...document.querySelectorAll('.dev-offer .dev-btn')].map((b) => b.disabled))"
    with _serve(src) as (port, _h):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.dev-zone') && !document.querySelector('.dev-zone').hidden && document.querySelector('.dev-offer .dev-btn')", "ms": 20000},
            {"eval": "(() => { const f = window.fetch.bind(window); window.__rel = null;"
                     " window.fetch = (u, o) => String(u) === '/cockpit/pair/offer' ? new Promise((ok) => { window.__rel = () =>"
                     "   ok(new Response(JSON.stringify({ short: 'ABCD-EFGH', short_ttl_seconds: 600 }), { status: 200 })); }) : f(u, o);"
                     " [...document.querySelectorAll('.dev-offer .dev-btn')].find((b) => b.textContent === 'Show a code').click(); return 1; })()"},
            {"until": "!!window.__rel", "ms": 5000},
            {"eval": both},
            {"eval": "(() => { window.__rel(); return 1; })()"},
            {"until": "[...document.querySelectorAll('.dev-offer .dev-btn')].every((b) => !b.disabled)", "ms": 5000},
        ])
    assert json.loads(out["values"][3]) == [True, True], out["values"][3]


@needs_browser
def test_the_devices_panel_offers_a_link_and_a_code_and_the_link_sends_no_short_key(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _h):
        st, off, _ = _call(port, "POST", "/cockpit/pair/offer", {"label": "Run iPad"}, token=T)
        assert st == 200 and _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})[0] == 200
        click = ("[...document.querySelectorAll('.dev-offer .dev-btn')].find((b) => b.textContent === '%s').click()")
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/#write_token={T}", [
            {"until": "document.querySelector('.dev-zone') && !document.querySelector('.dev-zone').hidden && document.querySelector('.dev-row')", "ms": 20000},
            {"eval": "document.querySelector('.dev-row .dev-meta').textContent"},
            {"eval": click % "Make a link"},
            {"posts": 1, "ms": 10000},
            {"until": "document.querySelector('.dev-url')", "ms": 10000},
            {"eval": click % "Show a code"},
            {"until": "/^[0-9A-Z]{4}-[0-9A-Z]{4}$/.test((document.querySelector('.dev-code') || {}).textContent || '')", "ms": 10000},
        ])
    offers = [json.loads(p.split(" ", 1)[1]) for p in out["posts"] if p.startswith("/cockpit/pair/offer ")]
    assert len(offers) == 2 and "short" not in offers[0] and offers[1].get("short") is True, offers
    assert "pings off" in out["values"][1], out["values"][1]
