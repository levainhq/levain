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
def test_a_pending_open_is_posted_with_the_pages_key_and_kept_only_while_the_network_is_down(tmp_path, monkeypatch) -> None:
    _root, src, _ids = _install(tmp_path)
    with _serve(src) as (port, _h):
        out = _drive(tmp_path, monkeypatch, f"http://127.0.0.1:{port}/", [
            {"until": "window.levainPairCard", "ms": 15000},
            # the worker's side of the contract: levain-sw-2 version 1, store "opened", key = the ping id as a string, value = the tap time
            {"eval": "(async () => { localStorage.setItem('levain_write_token', 'TKN'); window.__seen = []; window.__mode = 'offline';"
                     " const f = window.fetch.bind(window);"
                     " window.fetch = (u, o) => { if (String(u) !== '/webpush/opened') return f(u, o);"
                     "   window.__seen.push(JSON.parse(o.body).ping + ':' + o.headers['X-Levain-Write-Token']);"
                     "   return window.__mode === 'offline' ? Promise.reject(new TypeError('offline')) : Promise.resolve(new Response('{}', { status: window.__mode })); };"
                     " const db = await new Promise((ok, no) => { const q = indexedDB.open('levain-sw-2', 1);"
                     "   q.onupgradeneeded = () => q.result.createObjectStore('opened'); q.onsuccess = () => ok(q.result); q.onerror = no; });"
                     " await new Promise((ok) => { const t = db.transaction('opened', 'readwrite'); t.objectStore('opened').put(1, '7'); t.oncomplete = ok; });"
                     " db.close(); return 1; })()"},
            {"eval": "window.levainPairCard.drainOpened().then(() => new Promise((ok) => { const q = indexedDB.open('levain-sw-2', 1);"
                     " q.onsuccess = () => { const g = q.result.transaction('opened').objectStore('opened').getAllKeys(); g.onsuccess = () => { q.result.close(); ok(JSON.stringify(g.result)); }; }; }))"},
            {"eval": "(() => { window.__mode = 403; return window.levainPairCard.drainOpened().then(() => new Promise((ok) => { const q = indexedDB.open('levain-sw-2', 1);"
                     " q.onsuccess = () => { const g = q.result.transaction('opened').objectStore('opened').getAllKeys(); g.onsuccess = () => { q.result.close(); ok(JSON.stringify(g.result)); }; }; })); })()"},
            {"eval": "JSON.stringify(window.__seen)"},
        ])
    kept_offline, kept_after_refusal, seen = (json.loads(v) for v in out["values"][-3:])
    assert kept_offline == ["7"] and kept_after_refusal == [], (kept_offline, kept_after_refusal)   # a 4xx settles the key, a dead network does not
    assert seen == ["7:TKN", "7:TKN"], seen


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
