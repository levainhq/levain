"""RM-G2: the join of the room's cockpit half (``levain.cockpit.room``) and its serve half (``RoomServe``). One ChatService
for the room's reads and writes, the serve started and stopped with the server, the page able to OPEN a room (with none
open every other control is dark), a stop that is not queued behind a send, and a rotation checked against the room the
page drew inside the act. Transport: a ``ScriptedSession`` behind the serve tests' stub launcher, through a real
``make_server``. Residue (a person's, a VM's): the page in his hands, a real ``claude`` child."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request

import pytest

from levain.cockpit import room as cockpit_room
from levain.coordinator.chat import RoomRotated
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.room_launch import RoomLaunchError
from levain.coordinator.room_serve import RoomServeError
from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import TOKEN, _install, _serve
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)
from tests.test_coordinator_room_serve import scripted, wait


def _call(base, path, body=None, token=TOKEN, method="POST"):
    headers = {"Content-Type": "application/json"}
    if token is not None:
        headers["X-Levain-Write-Token"] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(base):
    return _call(base, "/cockpit/room.json", method="GET")


def _store(qdir):
    s = EnvelopeStore(qdir)
    s.coord_set_agent("none")
    return s


def _rig(tmp_path, qdir):
    """A RoomServe (scripted launcher) over the consent directory the server reads, and the install it serves."""
    store, rs, launcher = scripted(tmp_path, store=_store(qdir))
    _root, src, _ids = _install(tmp_path / "inst")
    return rs, launcher, src


def _rows(qdir, kind):
    c = sqlite3.connect(qdir / "dispatch.db")
    try:
        return c.execute("SELECT count(*) FROM room_events WHERE kind = ?", (kind,)).fetchone()[0]
    except sqlite3.OperationalError:        # no room table yet: no room was ever recorded
        return 0
    finally:
        c.close()


# --- b, c: one service, started and stopped with the server -----------------------------------------------------------

def test_the_reads_and_the_writes_go_through_one_chat_service(tmp_path, qdir):
    """rmf's L2: reads went via server.chat_service while writes went via room_host.service. Both are the serve's."""
    rs, _l, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (_base, httpd):
        assert httpd.chat_service is rs.service
        assert httpd.room_host.service is rs.service
        assert httpd.room_host.store is rs.store


def test_make_server_starts_the_serve_and_closing_it_stops_it(tmp_path, qdir):
    rs, launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (_base, httpd):
        assert httpd.room_serve is rs and rs.view()["room_thread"] is None       # started: view() would raise otherwise
        assert rs._thread.is_alive()
    with pytest.raises(RoomServeError, match="stopped"):
        rs.view()
    assert launcher.closed and not rs._thread.is_alive()


def test_a_serve_that_cannot_start_leaves_nothing_running(tmp_path, qdir, monkeypatch):
    rs, launcher, src = _rig(tmp_path, qdir)
    from levain.web_server import make_server

    def boom():
        raise RoomServeError("recover failed")
    monkeypatch.setattr(rs, "start", boom)
    with pytest.raises(RoomServeError, match="recover failed"):
        make_server(src, host="127.0.0.1", port=0, write_token=TOKEN, room_serve=rs)
    assert launcher.closed                                                       # stopped, not left holding the store


def test_make_server_refuses_a_second_service_and_a_serve_over_another_directory(tmp_path, qdir):
    from levain.web_server import make_server

    rs, _l, src = _rig(tmp_path, qdir)
    with pytest.raises(ValueError, match="own ChatService"):
        make_server(src, host="127.0.0.1", port=0, write_token=TOKEN, room_serve=rs, chat_service=rs.service)
    other = tmp_path / "elsewhere"
    other.mkdir(mode=0o700)
    s2, rs2, _l2 = scripted(tmp_path, store=_store(other))
    with pytest.raises(ValueError, match="writes .*but the cockpit reads"):
        make_server(src, host="127.0.0.1", port=0, write_token=TOKEN, room_serve=rs2)
    assert not rs2._started                                                      # the refusal came before the start


# --- d: the page can open the room -------------------------------------------------------------------------------------

def test_with_no_room_open_a_send_names_none_and_the_serve_is_never_asked_to_open_on_a_none(tmp_path, qdir, monkeypatch):
    """rmf L2 F1 HIGH: with no open room every dock control was dark and nothing could open one. A send naming no room is
    400 and RoomServe.send(room_thread=None) is never called by the route."""
    rs, launcher, src = _rig(tmp_path, qdir)
    seen = []
    real = rs.send
    monkeypatch.setattr(rs, "send", lambda **kw: (seen.append(kw), real(**kw))[1])
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        st, body = _get(base)
        assert (st, body["room_thread"], body["room_error"]) == (200, None, None)
        st, got = _call(base, "/cockpit/room/send", {"body": "hi", "outbox_id": "ob-00000001"})
        assert st == 400 and seen == [] and launcher.ensures == 0
        st, got = _call(base, "/cockpit/room/send", {"room_thread_id": "th-" + "0" * 32, "body": "hi", "outbox_id": "ob-00000001"})
        assert st == 409 and got["error"] == "room_rotated" and seen == [] and launcher.ensures == 0


def test_the_open_route_opens_one_room_and_answers_an_open_room_as_it_stands(tmp_path, qdir):
    rs, launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        st, a = _call(base, "/cockpit/room/open", {})
        assert st == 200 and a["room_thread"].startswith("th-")
        st, b = _call(base, "/cockpit/room/open", {})
        assert st == 200 and b == a and launcher.ensures == 2 and _rows(qdir, "opened") == 1   # one room, never two
        st, got = _get(base)
        assert got["room_thread"] == a["room_thread"] and got["launch_id"] is not None


def test_the_open_route_needs_a_named_credential_and_a_good_body(tmp_path, qdir):
    rs, launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        assert _call(base, "/cockpit/room/open", {}, token=None)[0] == 403
        assert _call(base, "/cockpit/room/open", {}, token="wrong")[0] == 403
        assert _call(base, "/cockpit/room/open", [1])[0] == 400
        assert launcher.ensures == 0 and _rows(qdir, "opened") == 0


def test_a_room_that_cannot_open_says_why_and_the_page_read_carries_the_pump_error(tmp_path, qdir, monkeypatch):
    rs, launcher, src = _rig(tmp_path, qdir)

    def refuse(**_kw):
        raise RoomLaunchError("the open check failed: the decoy was readable")
    monkeypatch.setattr(launcher, "ensure", refuse)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        st, got = _call(base, "/cockpit/room/open", {})
        assert st == 409 and got["error"] == "room_unavailable" and "decoy was readable" in got["message"]
        rs.last_error = "pump: OSError: the child's pipe closed"
        st, got = _get(base)
        assert got["room_error"] == "pump: OSError: the child's pipe closed"


# --- a: send through the serve, with the room the page drew -----------------------------------------------------------

def test_a_send_goes_through_the_serve_with_the_named_room_and_the_line_reaches_the_session(tmp_path, qdir, monkeypatch):
    rs, launcher, src = _rig(tmp_path, qdir)
    seen = []
    real = rs.send
    monkeypatch.setattr(rs, "send", lambda **kw: (seen.append(kw), real(**kw))[1])
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        thread = _call(base, "/cockpit/room/open", {})[1]["room_thread"]
        st, got = _call(base, "/cockpit/room/send", {"room_thread_id": thread, "body": "hello", "outbox_id": "ob-00000002"})
        assert st == 200 and got["receipt"]["state"] in ("accepted", "ambiguous")
        assert [k["room_thread"] for k in seen] == [thread]
        assert (seen[0]["author_surface"], seen[0]["client_id"]) == ("cockpit", "browser")
        st, body = _get(base)
        assert any(i["kind"] == "message" and i["body"] == "hello" for i in body["stream"])


def test_a_launch_the_serve_refuses_is_a_409_naming_the_reason_and_writes_no_line(tmp_path, qdir, monkeypatch):
    rs, launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        thread = _call(base, "/cockpit/room/open", {})[1]["room_thread"]
        rs.stop()                                                     # the serve is gone under a live page
        st, got = _call(base, "/cockpit/room/send", {"room_thread_id": thread, "body": "x", "outbox_id": "ob-00000003"})
        assert st == 409 and got["error"] == "room_unavailable" and "stopped" in got["message"]


# --- a: a stop that is not queued behind a send -----------------------------------------------------------------------

def test_a_stop_is_not_queued_behind_a_send_that_holds_the_serve(tmp_path, qdir):
    rs, launcher, _src = _rig(tmp_path, qdir)
    rs.start()
    try:
        handle = rs.ensure_room()
        held, release, done = threading.Event(), threading.Event(), threading.Event()

        def hold():
            with rs._lock:                       # what a send in flight does for as long as it runs
                held.set()
                release.wait(20)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        assert held.wait(5)
        out = {}

        def stop():
            out["got"] = rs.interrupt(handle.launch_id)
            done.set()
        threading.Thread(target=stop, daemon=True).start()
        assert done.wait(10), "the stop waited for the serve's lock"
        assert out["got"] is not None
        release.set()
        t.join(5)
    finally:
        rs.stop()


def test_the_page_read_answers_with_room_error_while_a_send_holds_the_serve(tmp_path, qdir):
    """roomfix1 R1 (reproduced): GET /cockpit/room.json called RoomServe.view(), which takes the serve's lock that a send
    holds across the launcher's open (up to OPEN_DEADLINE). Every poll stalled (and held a request slot) until the send
    ended. The read takes no serve lock: it answers at once, room_error included."""
    rs, _l, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        held, release = threading.Event(), threading.Event()

        def hold():
            with rs._lock:                       # what an open in a send does for as long as it runs
                held.set()
                release.wait(30)
        t = threading.Thread(target=hold, daemon=True)
        t.start()
        try:
            assert held.wait(5)
            rs.last_error = "pump: OSError: the child's pipe closed"
            begun = time.monotonic()
            req = urllib.request.Request(base + "/cockpit/room.json", headers={"X-Levain-Write-Token": TOKEN})
            with urllib.request.urlopen(req, timeout=5) as r:
                st, got = r.status, json.loads(r.read())
            assert time.monotonic() - begun < 3, "the page read waited for the serve's lock"
            assert st == 200 and got["room_error"] == "pump: OSError: the child's pipe closed"
        finally:
            release.set()
            t.join(5)


def test_a_stop_behind_a_send_in_flight_says_it_is_waiting(tmp_path, qdir):
    """roomfix1 R1: server_close -> RoomServe.stop() waits for a send in flight (by design); the wait is named to the
    caller (``on_wait``) instead of a silent Ctrl-C, and the pump is told to stop before the wait."""
    rs, _l, _src = _rig(tmp_path, qdir)
    rs.start()
    held, release = threading.Event(), threading.Event()

    def hold():
        with rs._lock:
            held.set()
            release.wait(30)
    t = threading.Thread(target=hold, daemon=True)
    t.start()
    assert held.wait(5)
    said = []

    def waiting():
        said.append(rs._stop_evt.is_set())
        release.set()                            # the send in flight ends
    rs.stop(on_wait=waiting)
    t.join(5)
    assert said == [True] and rs._stopped


def test_the_stop_route_acts_through_the_serve(tmp_path, qdir, monkeypatch):
    rs, launcher, src = _rig(tmp_path, qdir)
    seen = []
    real = rs.interrupt
    monkeypatch.setattr(rs, "interrupt", lambda launch: (seen.append(launch), real(launch))[1])
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        thread = _call(base, "/cockpit/room/open", {})[1]["room_thread"]
        st, got = _call(base, "/cockpit/room/stop", {"room_thread_id": thread})
        assert st in (200, 409) and "outcome" in got and seen == [launcher.handle.launch_id]


# --- e, g: a rotation checked inside the act --------------------------------------------------------------------------

def test_a_stale_rotation_is_refused_inside_the_serve_and_rotates_nothing(tmp_path, qdir):
    rs, _l, _src = _rig(tmp_path, qdir)
    rs.start()
    try:
        old = rs.ensure_room().room_thread
        new = rs.rotate(old)
        assert new != old
        with pytest.raises(RoomRotated):
            rs.rotate(old)
        assert _rows(qdir, "rotated") == 1
    finally:
        rs.stop()


def test_two_stale_taps_that_both_passed_the_route_check_rotate_once(tmp_path, qdir):
    """Reproduced: the route's room check and the rotation are two steps. Both taps pass the check (a barrier holds each
    at the host's door); without the check inside the act the second rotated again (two `rotated` rows)."""
    rs, _l, src = _rig(tmp_path, qdir)
    barrier = threading.Barrier(2, timeout=10)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, httpd):
        thread = _call(base, "/cockpit/room/open", {})[1]["room_thread"]
        inner = httpd.room_host.rotate

        def gated(expected):
            barrier.wait()
            return inner(expected)
        httpd.room_host.rotate = gated
        results = []

        def tap():
            results.append(_call(base, "/cockpit/room/rotate", {"room_thread_id": thread}))
        ts = [threading.Thread(target=tap) for _ in range(2)]
        [t.start() for t in ts]
        [t.join(30) for t in ts]
        assert sorted(s for s, _b in results) == [200, 409], results
        assert next(b for s, b in results if s == 409)["error"] == "room_rotated"
        assert _rows(qdir, "rotated") == 1
        st, got = _get(base)
        assert got["room_thread"] == next(b for s, b in results if s == 200)["room_thread"] != thread


def test_the_rotate_route_answers_the_open_room_it_re_reads(tmp_path, qdir):
    rs, _l, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        thread = _call(base, "/cockpit/room/open", {})[1]["room_thread"]
        st, got = _call(base, "/cockpit/room/rotate", {"room_thread_id": thread})
        assert st == 200
        assert _get(base)[1]["room_thread"] == got["room_thread"] != thread


# --- f: the page mounts the room and the work-thread Stop -------------------------------------------------------------

def test_the_boot_script_mounts_the_room_view_and_passes_stop_to_the_thread_view():
    js = (cockpit_room.__file__ and __import__("pathlib").Path(cockpit_room.__file__).resolve().parents[1]
          / "templates" / "web" / "dashboard_boot.js").read_text()
    assert "LevainRoomView.mount(roomHost" in js
    assert "LevainThreadView.mount(host, { headers: () => (T ? T.headers() : {}), stop: true })" in js


def test_the_room_view_hides_the_expected_mode_report_and_shows_any_other():
    from pathlib import Path
    js = (Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web" / "room_view.js").read_text()
    assert 'it.kind === "vendor_error" && it.body === "dontAsk"' in js


@needs_browser
def test_the_real_page_opens_the_room_sends_and_shows_the_reply(tmp_path, qdir):
    """The dashboard as served: the Room zone appears on the Build tab with a token, the Open button is the only live
    control, Open opens the room, a line is sent and the session's reply is drawn."""
    rs, launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", [
            {"until": "document.querySelector('[data-mode-tab=board]')"},
            {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
            {"until": "!window.__pre && document.querySelector('.rv-zone') && document.querySelector('[data-mode-tab=build]')"},
            {"do": "document.querySelector('[data-mode-tab=build]').click(); 1",
             "until": "document.querySelector('.rv-zone .rv-open') && !document.querySelector('.rv-zone .rv-open').hidden"},
            {"eval": "JSON.stringify({send: document.querySelector('.rv-send').disabled, drop: document.querySelector('.rv-drop').disabled,"
                     " stop: document.querySelector('.rv-stop').disabled, input: document.querySelector('.rv-input').disabled,"
                     " rotate: document.querySelector('.rv-rotate').disabled})"},
            {"eval": "document.querySelector('.rv-open').click(); 1"},
            {"until": "!document.querySelector('.rv-input').disabled && document.querySelector('.rv-open').hidden"},
            {"eval": "const ta = document.querySelector('.rv-input'); ta.value = 'hello page'; ta.dispatchEvent(new Event('input')); "
                     "document.querySelector('.rv-send').click(); 1"},
            {"until": "[...document.querySelectorAll('.rv-line')].some(l => l.textContent.includes('hello page'))"},
        ])
        mine = [i for i in _get(base)[1]["stream"] if i["kind"] == "message"]
    dark = json.loads(next(v for v in out["values"] if isinstance(v, str) and v.startswith("{")))
    assert dark == {"send": True, "drop": True, "stop": True, "input": True, "rotate": True}, dark
    assert [m["body"] for m in mine] == ["hello page"]


# the page is hidden and shown again (an app switch on the phone): dashboard_boot's sync() unmounts the room view on
# hidden and mounts a new one on visible
_HIDE_AND_SHOW = ("Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'hidden'});"
                  " document.dispatchEvent(new Event('visibilitychange'));"
                  " Object.defineProperty(document, 'visibilityState', {configurable: true, get: () => 'visible'});"
                  " document.dispatchEvent(new Event('visibilitychange')); 1")
_TO_THE_ROOM = [
    {"until": "document.querySelector('[data-mode-tab=board]')"},
    {"eval": f"localStorage.setItem('levain_write_token', {json.dumps(TOKEN)}); window.__pre = 1; location.reload(); 1"},
    {"until": "!window.__pre && document.querySelector('.rv-zone') && document.querySelector('[data-mode-tab=build]')"},
]


@needs_browser
def test_a_line_tried_before_an_app_switch_is_sent_again_under_the_same_outbox_id(tmp_path, qdir):
    """roomfix1 R2 (reproduced): the outbox id lived in the mount's closure, and the dashboard unmounts the room view when
    the page is hidden; after an unanswered send (here a 503 the page never saw land) an app switch minted a NEW id, so
    "Sending again will not duplicate it" was false across the remount. The id is kept beside the draft."""
    rs, launcher, src = _rig(tmp_path, qdir)
    first_fails = ("window.__ids = []; const real = window.fetch;"
                   " window.fetch = function (u, o) { if (String(u).endsWith('/cockpit/room/send')) {"
                   " window.__ids.push(JSON.parse(o.body).outbox_id);"
                   " if (window.__ids.length === 1) return Promise.resolve(new Response('{\"error\": \"busy\"}',"
                   " {status: 503, headers: {'Content-Type': 'application/json'}})); }"
                   " return real.apply(this, arguments); }; 1")
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        out = _drive(tmp_path, base + "/", _TO_THE_ROOM + [
            {"eval": first_fails},
            {"do": "document.querySelector('[data-mode-tab=build]').click(); 1",
             "until": "document.querySelector('.rv-zone .rv-open') && !document.querySelector('.rv-zone .rv-open').hidden"},
            {"eval": "document.querySelector('.rv-open').click(); 1"},
            {"until": "!document.querySelector('.rv-input').disabled && document.querySelector('.rv-open').hidden"},
            {"eval": "const ta = document.querySelector('.rv-input'); ta.value = 'hello twice'; ta.dispatchEvent(new Event('input')); "
                     "document.querySelector('.rv-send').click(); 1"},
            {"until": "window.__ids.length === 1 && !document.querySelector('.rv-send').disabled"},
            {"eval": _HIDE_AND_SHOW},
            {"until": "document.querySelectorAll('.rv-input').length === 1 && !document.querySelector('.rv-input').disabled"
                      " && document.querySelector('.rv-input').value === 'hello twice'"},
            {"eval": "document.querySelector('.rv-send').click(); 1"},
            {"until": "window.__ids.length === 2 && [...document.querySelectorAll('.rv-line')].some(l => l.textContent.includes('hello twice'))"},
            {"eval": "JSON.stringify(window.__ids)"},
        ])
        mine = [i["body"] for i in _get(base)[1]["stream"] if i["kind"] == "message"]
    ids = json.loads(out["values"][-1])
    assert len(ids) == 2 and ids[0] == ids[1], ids
    assert mine == ["hello twice"]


@needs_browser
def test_an_edit_to_a_card_survives_an_app_switch(tmp_path, qdir):
    """roomfix1 R2: a card's edited words lived in the mount's closure and were lost when the page was hidden."""
    from levain.coordinator import room as coordroom
    rs, launcher, src = _rig(tmp_path, qdir)
    with _serve(src, write_token=TOKEN, room_serve=rs) as (base, _h):
        thread = _call(base, "/cockpit/room/open", {})[1]["room_thread"]
        assert _call(base, "/cockpit/room/send", {"room_thread_id": thread, "body": "quote me", "outbox_id": "ob-00000009"})[0] == 200
        launch = _get(base)[1]["launch_id"]
        c = sqlite3.connect(qdir / "dispatch.db")
        try:
            mid = c.execute("SELECT message_id FROM chat_messages WHERE body = 'quote me'").fetchone()[0]
        finally:
            c.close()
        with rs.store._write() as (conn, now):
            coordroom.propose(conn, launch, [mid], "the room's summary", now=now)
        out = _drive(tmp_path, base + "/", _TO_THE_ROOM + [
            {"do": "document.querySelector('[data-mode-tab=build]').click(); 1", "until": "document.querySelector('.rv-cardwords')"},
            {"eval": "const t = document.querySelector('.rv-cardwords'); t.value = 'my edited words'; t.dispatchEvent(new Event('input')); 1"},
            {"eval": _HIDE_AND_SHOW},
            {"until": "document.querySelectorAll('.rv-cardwords').length === 1"},
            {"eval": "document.querySelector('.rv-cardwords').value"},
        ])
    assert out["values"][-1] == "my edited words"


@needs_browser
def test_the_dashboard_never_asks_the_browser_whether_dictation_is_available(tmp_path, qdir):
    """Reproduced 2026-11-04: `SpeechRecognition.available({processLocally: true})` froze the renderer of the installed
    headless Chrome (a bare page, no dashboard), and the first run of the real page hung on the Build tab. The boot script
    mounts with probe:false and the mount then never calls it; with the probe on (the default for a bare mount) it does."""
    from tests.test_room_phone import _FAKE, _assets
    js = __import__("pathlib").Path(cockpit_room.__file__).resolve().parents[1] / "templates" / "web" / "dashboard_boot.js"
    assert "probe: false" in js.read_text()
    boot = (_FAKE + ' window.__asked = 0; Installed.available = () => { window.__asked++; return Promise.resolve("available"); };'
            ' window.__rv = LevainRoomView.mount(document.getElementById("host"), {pollMs: 0, storage: null, speech: Installed,'
            ' probe: PROBE, headers: () => ({"X-Levain-Write-Token": "%s"})});' % TOKEN)
    asked = {}
    for probe in ("false", "true"):
        d = tmp_path / probe
        d.mkdir()
        rs, _l, src = _rig(d, qdir)
        with _serve(src, write_token=TOKEN, room_serve=rs, extra_assets=_assets(boot.replace("PROBE", probe))) as (base, _h):
            out = _drive(d, base + "/rvc.html", [{"until": "document.querySelector('.rv-input')"},
                                                 {"eval": "new Promise(r => setTimeout(() => r(window.__asked), 300))"}])
            asked[probe] = out["values"][1]
    assert asked == {"false": 0, "true": 1}, asked


@needs_browser
def test_the_expected_mode_report_is_not_drawn_as_an_error_and_any_other_mode_is(tmp_path, qdir):
    """The session reports its permission mode on every turn as an error-kind row (claude_session ``mode:``); drawn as
    "Error / dontAsk" at the top of every turn it would train the eye past a real error."""
    from tests.test_room_phone import _STYLED
    boot = 'window.__m = [LevainRoomView.lineModel({kind: "vendor_error", body: "dontAsk", gseq: 1}).hidden, ' \
           'LevainRoomView.lineModel({kind: "vendor_error", body: "bypassPermissions", gseq: 2}).hidden, ' \
           'LevainRoomView.lineModel({kind: "vendor_error", body: "the child exited", gseq: 3}).hidden];'
    rs, _l, src = _rig(tmp_path, qdir)
    assets = {"/rvc.html": ("text/html; charset=utf-8", _STYLED.encode()), "/rvc.js": ("text/javascript; charset=utf-8", boot.encode())}
    with _serve(src, write_token=TOKEN, room_serve=rs, extra_assets=assets) as (base, _h):
        out = _drive(tmp_path, base + "/rvc.html", [{"until": "window.__m"}, {"eval": "JSON.stringify(window.__m)"}])
    assert json.loads(out["values"][1]) == [True, False, False]
