"""The work thread's one write, Stop, through the real ``levain serve`` envelope (make_server) beside a ChatService over a
ScriptedSession. The gate is the sign/consent family's; the launch the body names (``expected_launch_id``) only fences the
thread's latest worker launch. A work thread has no send and no mark route in 1.0 (the operator talks in the room: see
``tests/test_cockpit_room.py``); the old routes are not routes."""
from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from levain.coordinator.chat import ChatService, ChatStore
from levain.coordinator.driver import ScriptedSession
from levain.coordinator.envelopes import EnvelopeStore
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads import clock  # noqa: F401  (fixture)
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the default consent directory the server reads)
from tests.test_coordinator_chat_send import LIVE, PHONE, _world, check, ready

OWNER = "cockpit:browser"          # the owner the write token's credential names


@pytest.fixture
def live(qdir, clock):
    """A running worker launch with an attached ScriptedSession, and the service that holds it."""
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    session = ScriptedSession()
    svc = ready(cs, world, session)
    return svc, cs, world, session


def _call(base: str, path: str, body, token: str | None = TOKEN, method: str = "POST", ctype: str = "application/json"):
    headers = {"Content-Type": ctype}
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _rows(cs: ChatStore) -> list[tuple]:
    return cs._read(lambda c: c.execute(
        "SELECT launch_id, author_kind, author_surface, client_id, body FROM chat_messages ORDER BY gseq").fetchall())


def _send_path(world) -> str:
    return f"/cockpit/thread/{world['thread']}/send"


def _stop_path(world) -> str:
    return f"/cockpit/thread/{world['thread']}/stop"


def _msg(world, body="go", outbox="ob-00000001", **kw) -> dict:
    return {"body": body, "outbox_id": outbox, "expected_launch_id": world["launch"], **kw}


def _replace(cs: ChatStore, world) -> str:
    """A second worker launch for the thread (the coordinator relaunched it): the thread's latest worker launch."""
    store = cs._opener
    ln = store.launch_request(store.coord_take(), harness="claude", role="worker", threads=[world["thread"]])
    return ln.launch_id


def test_no_credential_or_a_wrong_one_is_refused_before_anything_is_read_or_written(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        for token in (None, "wrong-token"):
            status, body = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]}, token)
            assert status == 403, (token, body)
        status, _b = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]}, TOKEN, ctype="text/plain")
        assert status in (400, 403, 415), status          # CSRF layer 2: JSON only
    assert _rows(cs) == [] and session.written == []


def test_the_routes_are_absent_without_a_chat_service(tmp_path, live):
    _svc, _cs, world, _session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]})
    assert status == 404 and body["error"] == "not_found"


def test_a_work_thread_has_no_send_and_no_mark_route(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        for action in ("send", "mark"):
            status, body = _call(base, f"/cockpit/thread/{world['thread']}/{action}", _msg(world))
            assert status == 404 and body["error"] == "not_found", (action, body)
    assert _rows(cs) == [] and session.written == []


def test_send_and_stop_against_a_replaced_launch_are_refused_and_do_nothing(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        _replace(cs, world)                         # the page still shows world["launch"]
        status, body = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]})
        assert status == 409 and body["error"] == "launch_replaced" and "changed" in body["message"]
        # the read names the launch a page must now carry
        status, got = _call(base, f"/cockpit/thread/{world['thread']}.json", None, method="GET")
        assert status == 200 and got["launch_id"] != world["launch"] and "composer" not in got
    assert session.written == []


@pytest.mark.parametrize("want", [None, "", "ln-short", 7])
def test_a_stop_without_a_well_formed_expected_launch_id_is_refused(tmp_path, live, want):
    svc, cs, world, session = live
    req = {}
    if want is not None:
        req["expected_launch_id"] = want
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        status, body = _call(base, _stop_path(world), req)
    assert status == 400 and body["error"] == "bad_request" and "expected_launch_id" in body["message"]
    assert session.written == []


def test_an_unknown_or_malformed_thread_is_404_and_a_malformed_route_is_not_a_route(tmp_path, live):
    svc, _cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    fence = {"expected_launch_id": world["launch"]}
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        status, _b = _call(base, "/cockpit/thread/th-" + "0" * 32 + "/stop", fence)
        assert status == 404
        status, _b = _call(base, "/cockpit/thread/not-a-thread/stop", fence)
        assert status == 404
        status, _b = _call(base, f"/cockpit/thread/{world['thread']}/launch", {"body": "go"})
        assert status == 404
    assert session.written == []


@pytest.mark.parametrize("turn", [5, "", "two\nlines", ["t"], "x" * 5000], ids=["int", "empty", "two-lines", "list", "long"])
def test_a_malformed_turn_id_is_a_400_never_a_409_no_session(tmp_path, live, turn):
    # L3_chatstack_r1 codex-8: the service refuses a turn id that is not one line of plain text; the route says so first
    svc, _cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        status, body = _call(base, _stop_path(world), {"expected_launch_id": world["launch"], "turn_id": turn})
    assert status == 400 and body["error"] == "bad_request", body
    assert session.written == []


def test_stop_interrupts_the_launchs_turn_and_reports_the_drivers_answer(tmp_path, live):
    svc, _cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    fence = {"expected_launch_id": world["launch"]}
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        status, body = _call(base, _stop_path(world), fence)
        assert status == 200 and body["outcome"] in ("confirmed", "no_such_request"), body
        svc.detach(world["launch"])
        status, body = _call(base, _stop_path(world), fence)
        assert status == 409 and body["error"] == "no_session"


_PAGE = ('<!doctype html><meta charset="utf-8"><div id="host"></div><script src="/thread_view.js"></script>'
         '<script src="/tvc.js"></script>')
_BOOT = ('window.__tv = LevainThreadView.mount(document.getElementById("host"),'
         ' {pollMs: 0, stop: true, headers: () => ({"X-Levain-Write-Token": "%s"})});' % TOKEN)


def _assets(boot: str) -> dict:
    return {"/tvc.html": ("text/html; charset=utf-8", _PAGE.encode()),
            "/tvc.js": ("text/javascript; charset=utf-8", boot.encode())}


@needs_browser
def test_a_work_thread_draws_a_stop_and_no_composer(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc, extra_assets=_assets(_BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-stop')"},
            {"eval": "JSON.stringify({input: document.querySelectorAll('.tv-input, .tv-send, .tv-composer').length})"},
            {"eval": "document.querySelector('.tv-stop').click(); 1"},
            {"until": "/Stop confirmed|Nothing was running/.test(document.querySelector('.tv-sendnote').textContent)"},
        ])
    assert json.loads(out["values"][3]) == {"input": 0}
    assert _rows(cs) == []


# --- roomfix1 R3: a Stop only where the server can reach a session -----------------------------------------------------

def test_the_thread_body_says_whether_a_stop_can_reach_a_session(tmp_path, live):
    """The page drew a Stop on every thread naming a launch, even where it could never succeed: with no chat service the
    route is a 404, and under `levain serve --room` the room's service holds no work launch (409 no_session). The thread
    body now carries `stoppable`: true only while the server's service holds a usable session for that launch."""
    from levain.cockpit import threads as cockpit_threads
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    path = f"/cockpit/thread/{world['thread']}.json"
    with _serve(src, write_token=TOKEN, chat_service=svc) as (base, _h):
        assert _call(base, path, None, method="GET")[1]["stoppable"] is True
        svc.detach(world["launch"])
        assert _call(base, path, None, method="GET")[1]["stoppable"] is False
    directory = cs._opener.path.parent
    assert cockpit_threads.thread_stream(directory, world["thread"], None)["stoppable"] is False      # no service
    other = ChatService(cs)                      # a service that holds no session for this launch (the room's case)
    try:
        assert cockpit_threads.thread_stream(directory, world["thread"], other)["stoppable"] is False
    finally:
        other.close()


@needs_browser
def test_a_work_thread_whose_launch_no_session_holds_draws_no_stop(tmp_path, live):
    svc, cs, world, session = live
    svc.detach(world["launch"])
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, chat_service=svc, extra_assets=_assets(_BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-title')"},
            {"eval": "new Promise(r => setTimeout(() => r(document.querySelectorAll('.tv-stop').length), 300))"},
        ])
    assert out["values"][-1] == 0
