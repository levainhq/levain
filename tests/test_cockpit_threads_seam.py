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
    assert "section.hidden = off || !token();" in js


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
