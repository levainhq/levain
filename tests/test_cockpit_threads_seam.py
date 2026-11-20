"""make_server(threads=False): an embedder turns the Build tab's Threads zone off. Both thread routes answer 404 and the manifest's
transport says ``threads: false`` (absent by default, so the default manifest is unchanged), which the page reads to draw no zone."""
from __future__ import annotations

from pathlib import Path


from levain.cockpit import threads
from levain.web_server import make_server
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

