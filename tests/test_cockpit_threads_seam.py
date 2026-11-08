"""make_server(threads=False): an embedder turns the Build tab's Threads zone off. Both thread routes answer 404 and the manifest's
transport says ``threads: false`` (absent by default, so the default manifest is unchanged), which the page reads to draw no zone."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.cockpit import threads
from levain.web_server import make_server
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_questions import _get_status
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)
from tests.test_cockpit_threads import clock  # noqa: F401  (fixture)
from tests.test_cockpit_threads_send import _call, _rows, _stop_path, live  # noqa: F401

ONE = "/cockpit/thread/th-" + "0" * 32 + ".json"


def test_threads_off_answers_both_routes_404_even_with_the_token(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, threads=False) as (base, _h):
        for path in (threads.THREADS_LIST_PATH, ONE):
            for token in (None, TOKEN):
                status, body = _get_status(base + path, token)
                assert status == 404 and body["error"] == "not_found", (path, token, body)


def test_threads_off_is_published_on_the_manifest_transport_only_when_off(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, threads=False) as (base, _h):
        status, body = _get_status(base + "/cockpit/manifest.json", TOKEN)
        assert status == 200 and body["transport"]["threads"] is False
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _get_status(base + "/cockpit/manifest.json", TOKEN)
        assert status == 200 and "threads" not in body["transport"]


def test_the_default_still_serves_the_record_unchanged(tmp_path):
    _root, src, _ids = _install(tmp_path)
    httpd = make_server(src, host="127.0.0.1", port=0)
    try:
        assert httpd.threads is True
    finally:
        httpd.server_close()
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, _b = _get_status(base + threads.THREADS_LIST_PATH, None)
        assert status == 403                                 # the gate, not a 404: the route exists
        status, body = _get_status(base + ONE, TOKEN)
        assert status == 404 and body["message"] == "no such thread recorded"   # the reader's own 404, not the seam's


def test_the_boot_script_reads_the_flag_and_keeps_the_zone_hidden():
    js = (Path(threads.__file__).resolve().parents[1] / "templates" / "web" / "dashboard_boot.js").read_text()
    assert "manifest.transport.threads === false" in js
    assert 'document.body.dataset.threads === "off"' in js
    assert "section.hidden = off || !read || !token();" in js


@needs_browser
def test_the_real_page_draws_no_threads_zone_when_off_and_one_when_on(tmp_path):
    _root, src, _ids = _install(tmp_path)
    def probe(until):
        return [
        {"until": "document.querySelector('[data-mode-tab=board]')"},
        {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
        {"until": "!window.__pre && document.querySelector('[data-zone=threads]')"},
        {"do": "document.querySelector('[data-mode-tab=build]').click(); 1",
         "until": "document.body.dataset.mode === 'build'"},
        {"until": until},
        {"eval": "JSON.stringify({hidden: document.querySelector('[data-zone=threads]').hidden, off: document.body.dataset.threads || ''})"},
    ]
    with _serve(src, write_token=TOKEN, threads=False) as (base, _h):
        out = _drive(tmp_path, base + "/", probe("document.body.dataset.threads === 'off'"))
    assert json.loads(out["values"][-1]) == {"hidden": True, "off": "off"}
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, base + "/", probe("document.querySelector('.th-body > *')"))
    assert json.loads(out["values"][-1]) == {"hidden": False, "off": ""}


def test_threads_off_also_404s_the_thread_write_routes_and_touches_no_session(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc, threads=False) as (base, _h):
        for token in (None, TOKEN):
            status, body = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]}, token)
            assert status == 404 and body["error"] == "not_found", (token, body)
    assert session.written == [] and _rows(cs) == []
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):          # control: on, the same call is the route
        status, _b = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]}, None)
        assert status == 403


def test_substrate_json_carries_threads_false_only_when_off(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, threads=False) as (base, _h):
        status, body = _get_status(base + "/substrate.json", TOKEN)
        assert status == 200 and body["threads"] is False
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _get_status(base + "/substrate.json", TOKEN)
        assert status == 200 and "threads" not in body


def test_manifest_transport_is_a_copy_and_only_the_manifest_reads_it(tmp_path):
    _root, src, _ids = _install(tmp_path)
    httpd = make_server(src, host="127.0.0.1", port=0, threads=False)
    try:
        cls = httpd.RequestHandlerClass
        shared = {"writable": True}
        handler = cls.__new__(cls)
        handler.server = httpd
        handler._transport = lambda: shared          # a cached dict handed back to every caller
        assert handler._manifest_transport() == {"writable": True, "threads": False}
        assert shared == {"writable": True}          # never mutated
    finally:
        httpd.server_close()
    routes = (Path(threads.__file__).resolve().parent / "routes.py").read_text()
    uses = [ln for ln in routes.splitlines() if "transport=transport" in ln]
    assert len(uses) == 1 and "manifest(" in uses[0], uses         # the one consumer is the manifest response


def _count(pattern="cockpit/thread"):
    return f"performance.getEntriesByType('resource').filter(e => e.name.includes('{pattern}')).length"


def _quiet_after(until):
    return [
        {"until": "document.querySelector('[data-mode-tab=board]')"},
        {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
        {"until": "!window.__pre && document.querySelector('[data-zone=threads]')"},
        {"do": "document.querySelector('[data-mode-tab=build]').click(); 1", "until": "document.body.dataset.mode === 'build'"},
        {"until": until},
        {"eval": f"new Promise(r => setTimeout(() => r({_count()}), 300))"},
        {"eval": f"new Promise(r => setTimeout(() => r({_count()}), 3500))"},
        {"eval": "document.querySelector('[data-zone=threads]').hidden"},
    ]


@needs_browser
def test_the_legacy_substrate_page_also_draws_no_zone_and_stops_polling_when_off(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, threads=False) as (base, _h):
        out = _drive(tmp_path, base + "/?source=substrate", _quiet_after("document.body.dataset.threads === 'off'"))
    first, second, hidden = out["values"][-3:]
    assert hidden is True and second == first, out["values"]     # no thread request after the flag, over two poll periods


@needs_browser
def test_an_already_mounted_zone_is_torn_down_when_the_flag_arrives_late(tmp_path):
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, base + "/", _quiet_after("document.querySelector('.th-body > *')")[:5] + [
            {"until": f"{_count()} >= 1"},
            {"eval": "document.body.dataset.threads = 'off'; window.dispatchEvent(new Event('levain:threads-off')); 1"},
            {"eval": f"new Promise(r => setTimeout(() => r({_count()}), 300))"},
            {"eval": f"new Promise(r => setTimeout(() => r({_count()}), 3500))"},
            {"eval": "document.querySelector('[data-zone=threads]').hidden"},
        ])
    first, second, hidden = out["values"][-3:]
    assert hidden is True and second == first, out["values"]     # the mounted view stopped, not just hid


@needs_browser
def test_the_zone_is_hidden_at_first_paint_and_shown_only_once_the_manifest_says_threads_are_on(tmp_path):
    # the manifest read is held in the page (an init script wraps fetch) so the zone's first state is observable
    hold = (f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__held = 0; window.__go = null; "
            "const real = window.fetch.bind(window); "
            "window.fetch = (u, ...a) => { const s = String(u && u.url || u); "
            "if (s.includes('/cockpit/manifest.json')) { window.__held++; return new Promise(r => { const prev = window.__go; "
            "window.__go = () => { if (prev) prev(); r(real(u, ...a)); }; }); } return real(u, ...a); };")
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        out = _drive(tmp_path, base + "/", [
            {"init": hold},
            {"until": "document.querySelector('[data-zone=threads]') && window.__held >= 1"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelector('[data-zone=threads]').hidden), 400))"},
            {"eval": "window.__go(); 1"},
            {"until": "document.querySelector('[data-zone=threads]').hidden === false"},
            {"eval": "document.querySelector('[data-zone=threads]').hidden"},
        ])
    before, after = out["values"][-4], out["values"][-1]
    assert before is True and after is False, out["values"]      # hidden while the manifest is unread, shown once it says on
