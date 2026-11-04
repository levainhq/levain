"""CH-U1b: the thread view's send and stop, through the real ``levain serve`` envelope (make_server) beside a ChatService
over a ScriptedSession. The gate is the sign/consent family's; the owner is the credential's; a body names no author or
epoch, and the launch it names (``expected_launch_id``) only fences the thread's latest worker launch."""
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
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        for path in (_send_path(world), _stop_path(world)):
            for token in (None, "wrong-token"):
                status, body = _call(base, path, _msg(world), token)
                assert status == 403, (path, token, body)
        status, _b = _call(base, _send_path(world), _msg(world), TOKEN, ctype="text/plain")
        assert status in (400, 403, 415), status          # CSRF layer 2: JSON only
    assert _rows(cs) == [] and session.written == []


def test_the_routes_are_absent_without_a_chat_service(tmp_path, live):
    _svc, _cs, world, _session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN) as (base, _h):
        status, body = _call(base, _send_path(world), _msg(world))
    assert status == 404 and body["error"] == "not_found"


def test_a_send_is_the_operators_under_the_credentials_own_owner_and_epoch(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        # the body tries to name an author and an epoch: neither is read
        status, body = _call(base, _send_path(world), _msg(
            world, "fix the build", author_kind="coordinator", author_surface="phone", client_id="someone-else",
            expected_epoch=999))
        assert status == 200, body
        assert body["receipt"]["state"] == "accepted" and body["receipt"]["created"] is True
        [row] = _rows(cs)
        assert row == (world["launch"], "operator", "cockpit", "browser", "fix the build")
        assert len(session.written) == 1
        assert cs.turn_view(world["launch"], OWNER).state == "yours"      # a free turn was claimed for it
        # the same outbox id again sends nothing again
        status, body = _call(base, _send_path(world), _msg(world, "fix the build"))
        assert status == 200 and body["receipt"]["created"] is False
        assert len(session.written) == 1 and len(_rows(cs)) == 1
        # a second line rides the same live epoch
        status, body = _call(base, _send_path(world), _msg(world, "and run the tests", "ob-00000002"))
        assert status == 200 and len(_rows(cs)) == 2


def test_a_turn_another_surface_holds_is_a_409_naming_the_holder_and_nothing_is_sent(tmp_path, live):
    svc, cs, world, session = live
    cs.turn_claim(world["launch"], PHONE)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _send_path(world), _msg(world))
    assert status == 409 and body["error"] == "turn_taken" and "phone" in body["holder"]
    assert _rows(cs) == [] and session.written == []


def test_a_launch_with_no_session_refuses_says_why_and_gives_the_turn_back(tmp_path, live):
    svc, cs, world, session = live
    svc.detach(world["launch"])
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _send_path(world), _msg(world))
    assert status == 409 and body["error"] == "refused" and "no driver session" in body["message"]
    assert body["receipt"]["state"] == "refused" and session.written == []
    # the epoch claimed for a send that moved nothing is released: no other surface waits out the lease for it
    assert cs.turn_view(world["launch"], PHONE).state == "free"


# HIGH 1: a send that did not deliver is never a success
def test_a_send_the_driver_did_not_deliver_is_a_409_carrying_its_cause(tmp_path, qdir, clock):
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    session = ScriptedSession(sends=("not_sent",))
    svc = ready(cs, world, session)
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _send_path(world), _msg(world))
        assert status == 409, body
        assert body["error"] == "not_delivered" and body["receipt"]["state"] == "not_delivered"
        assert body["message"] == body["receipt"]["cause"] and "not sent" in body["message"]
        # its resend answers the same stored refusal, never a success
        status, body = _call(base, _send_path(world), _msg(world))
        assert status == 409 and body["error"] == "not_delivered" and body["receipt"]["created"] is False
    assert cs.turn_view(world["launch"], PHONE).state == "free"


def test_a_send_cancelled_at_the_second_preflight_is_a_409_carrying_its_cause(tmp_path, qdir, clock):
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    session = ScriptedSession()
    check(cs, world["launch"], "live")
    svc = ChatService(cs, **LIVE)
    svc.attach(world["launch"], session)          # never read: no vendor position, so the attempt is cancelled
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _send_path(world), _msg(world))
    assert status == 409 and body["error"] == "cancelled" and body["receipt"]["state"] == "cancelled"
    assert body["message"] == "no vendor position yet" and session.written == []
    assert cs.turn_view(world["launch"], PHONE).state == "free"


# HIGH 2: a retried outbox id answers its stored receipt before the lease is consulted
def test_a_retry_of_an_accepted_send_answers_the_stored_receipt_though_another_surface_took_the_turn(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, first = _call(base, _send_path(world), _msg(world))
        assert status == 200 and first["receipt"]["state"] == "accepted"
        # the answer is lost; the phone takes the turn; the page retries the same outbox id
        cs.turn_take(world["launch"], PHONE)
        status, again = _call(base, _send_path(world), _msg(world))
        assert status == 200, again
        assert again["receipt"]["message_id"] == first["receipt"]["message_id"]
        assert again["receipt"]["state"] == "accepted" and again["receipt"]["created"] is False
        assert cs.turn_view(world["launch"], PHONE).state == "yours"      # the replay touched no lease
        # and so it does when the launch was replaced meanwhile: the receipt is the stored one, nothing is sent
        _replace(cs, world)
        status, again = _call(base, _send_path(world), _msg(world))
        assert status == 200 and again["receipt"]["message_id"] == first["receipt"]["message_id"]
        # the same outbox id saying something else is a conflict, and writes nothing
        status, body = _call(base, _send_path(world), _msg(world, "something else"))
        assert status == 409 and body["error"] == "outbox_conflict"
    assert len(session.written) == 1 and len(_rows(cs)) == 1


# HIGH 3: send and stop are fenced on the launch the page drew
def test_send_and_stop_against_a_replaced_launch_are_refused_and_do_nothing(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        _replace(cs, world)                         # the page still shows world["launch"]
        status, body = _call(base, _send_path(world), _msg(world))
        assert status == 409 and body["error"] == "launch_replaced" and "changed" in body["message"]
        status, body = _call(base, _stop_path(world), {"expected_launch_id": world["launch"]})
        assert status == 409 and body["error"] == "launch_replaced"
        # the read names the launch a page must now carry
        status, got = _call(base, f"/cockpit/thread/{world['thread']}.json", None, method="GET")
        assert status == 200 and got["launch_id"] != world["launch"]
    assert _rows(cs) == [] and session.written == []
    assert cs.turn_view(world["launch"], PHONE).state == "free"          # nothing claimed for the refused send


@pytest.mark.parametrize("path", ["send", "stop"])
@pytest.mark.parametrize("want", [None, "", "ln-short", 7])
def test_a_body_without_a_well_formed_expected_launch_id_is_refused(tmp_path, live, path, want):
    svc, cs, world, session = live
    req = _msg(world) if path == "send" else {}
    if want is None:
        req.pop("expected_launch_id", None)
    else:
        req["expected_launch_id"] = want
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, f"/cockpit/thread/{world['thread']}/{path}", req)
    assert status == 400 and body["error"] == "bad_request" and "expected_launch_id" in body["message"]
    assert _rows(cs) == [] and session.written == []


# MED 4: the whole local shape before any lease mutation
@pytest.mark.parametrize("req", [
    {"outbox_id": "ob-00000001"}, {"body": "   ", "outbox_id": "ob-00000001"}, {"body": "go"}, {"body": "go", "outbox_id": ""},
    {"body": "go", "outbox_id": "x"}, {"body": "go", "outbox_id": "ob-" + "a" * 200}, {"body": "go", "outbox_id": "ob 0000001"},
    {"body": "x" * 20_001, "outbox_id": "ob-00000001"}, {"body": "go\x00", "outbox_id": "ob-00000001"},
    {"body": "go", "outbox_id": "ob-00000001", "mode": "shout"}, {"body": "go", "outbox_id": "ob-00000001", "mode": "steer"},
    {"body": "go", "outbox_id": "ob-00000001", "expected_turn_id": "t-1"}, {"body": 7, "outbox_id": "ob-00000001"}, [1, 2],
])
def test_a_malformed_send_is_a_400_writes_nothing_and_claims_no_turn(tmp_path, live, req):
    svc, cs, world, session = live
    if isinstance(req, dict):
        req = {**req, "expected_launch_id": world["launch"]}
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _send_path(world), req)
    assert status == 400 and body["error"] == "bad_request", body
    assert _rows(cs) == [] and session.written == []
    assert cs.turn_view(world["launch"], PHONE).state == "free"


def test_an_unknown_or_malformed_thread_is_404_and_a_malformed_route_is_not_a_route(tmp_path, live):
    svc, _cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, _b = _call(base, "/cockpit/thread/th-" + "0" * 32 + "/send", _msg(world))
        assert status == 404
        status, _b = _call(base, "/cockpit/thread/not-a-thread/send", _msg(world))
        assert status == 404
        status, _b = _call(base, f"/cockpit/thread/{world['thread']}/launch", {"body": "go"})
        assert status == 404
    assert session.written == []


def test_stop_interrupts_the_launchs_turn_and_reports_the_drivers_answer(tmp_path, live):
    svc, _cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    fence = {"expected_launch_id": world["launch"]}
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, body = _call(base, _stop_path(world), fence)
        assert status == 200 and body["outcome"] in ("confirmed", "no_such_request"), body
        status, body = _call(base, _stop_path(world), {**fence, "turn_id": 5})
        assert status == 400
        svc.detach(world["launch"])
        status, body = _call(base, _stop_path(world), fence)
        assert status == 409 and body["error"] == "no_session"
        status, _b = _call(base, "/cockpit/thread/th-" + "0" * 32 + "/stop", fence)
        assert status == 404


def test_the_reads_show_the_attached_composer_and_this_credentials_turn(tmp_path, live):
    svc, cs, world, _session = live
    _root, src, _ids = _install(tmp_path)
    one = f"/cockpit/thread/{world['thread']}.json"
    with _serve(src, write_token=TOKEN, thread_chat=svc) as (base, _h):
        status, got = _call(base, one, None, method="GET")
        assert status == 200 and got["composer"]["state"] == "writable" and got["launch_id"] == world["launch"]
        assert got["composer"]["turn"] == {"state": "free", "epoch": None}
        _call(base, _send_path(world), _msg(world))
        status, got = _call(base, one, None, method="GET")
        assert got["composer"]["turn"]["state"] == "yours" and isinstance(got["composer"]["turn"]["epoch"], int)
        cs.turn_take(world["launch"], PHONE)
        status, got = _call(base, one, None, method="GET")
        assert got["composer"]["state"] == "taken" and got["composer"]["turn"]["state"] == "taken"
    # a server given no service still reads the old way: nothing attached
    _root2, src2, _ids2 = _install(tmp_path / "b")
    with _serve(src2, write_token=TOKEN) as (base, _h):
        status, got = _call(base, one, None, method="GET")
        assert got["composer"]["state"] == "no_session" and "turn" not in got["composer"]


_PAGE = ('<!doctype html><meta charset="utf-8"><div id="host"></div><script src="/thread_view.js"></script>'
         '<script src="/tvc.js"></script>')
_BOOT = ('window.__tv = LevainThreadView.mount(document.getElementById("host"),'
         ' {pollMs: 0, compose: true, headers: () => ({"X-Levain-Write-Token": "%s"})});' % TOKEN)
# the page's fetch, with each send POST held until the test lets it go, and the reads counted (a poll every 50 ms)
_HOLD_BOOT = ('window.__reads = 0; window.__held = []; window.__posts = [];'
              'const real = window.fetch.bind(window);'
              'window.__tv = LevainThreadView.mount(document.getElementById("host"), {pollMs: 50, compose: true,'
              ' headers: () => ({"X-Levain-Write-Token": "%s"}),'
              ' fetch: (u, o) => { if (o && o.method === "POST") { window.__posts.push(o.body);'
              '   return new Promise(r => window.__held.push(() => r(real(u, o)))); }'
              '   window.__reads++; return real(u, o); }});' % TOKEN)


def _assets(boot: str) -> dict:
    return {"/tvc.html": ("text/html; charset=utf-8", _PAGE.encode()),
            "/tvc.js": ("text/javascript; charset=utf-8", boot.encode())}


@needs_browser
def test_the_composer_sends_stops_and_keeps_a_draft_it_could_not_send(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc, extra_assets=_assets(_BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-input') && !document.querySelector('.tv-input').disabled"},
            {"eval": "const ta = document.querySelector('.tv-input'); ta.value = 'hello <b>there</b>'; document.querySelector('.tv-send').click(); 1"},
            {"until": "document.querySelector('.tv-sendnote').textContent.indexOf('Sent') === 0 && document.querySelector('.tv-input').value === ''"},
            {"until": "[...document.querySelectorAll('.tv-body')].some(n => n.textContent === 'hello <b>there</b>')"},
            {"eval": "document.querySelectorAll('.tv-body b').length"},
            {"eval": "document.querySelector('.tv-stop').click(); 1"},
            {"until": "/Stop confirmed|Nothing was running/.test(document.querySelector('.tv-sendnote').textContent)"},
        ])
    assert out["values"][6] == 0                      # drawn as text, never markup
    assert [r[4] for r in _rows(cs)] == ["hello <b>there</b>"]


@needs_browser
def test_a_refused_send_keeps_the_draft_and_says_why(tmp_path, live):
    svc, cs, world, session = live
    cs.turn_claim(world["launch"], PHONE)            # another surface holds the turn: the composer is read-only
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc, extra_assets=_assets(_BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-input')"},
            {"eval": "JSON.stringify({dis: document.querySelector('.tv-input').disabled, send: document.querySelector('.tv-send').disabled,"
                     " stop: document.querySelector('.tv-stop').disabled, words: document.querySelector('.tv-composer').textContent})"},
        ])
    got = json.loads(out["values"][3])
    assert got["dis"] and got["send"] and not got["stop"] and "Another surface holds the turn" in got["words"]
    assert "read-only" not in got["words"] and _rows(cs) == []


@needs_browser
def test_a_send_the_driver_did_not_deliver_keeps_the_draft_and_says_so(tmp_path, qdir, clock):
    store = EnvelopeStore(qdir, clock=clock)
    world = _world(store)
    cs = ChatStore(store)
    cs._connect().close()
    svc = ready(cs, world, ScriptedSession(sends=("not_sent",)))
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc, extra_assets=_assets(_BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-input') && !document.querySelector('.tv-input').disabled"},
            {"eval": "document.querySelector('.tv-input').value = 'a line that never lands'; document.querySelector('.tv-send').click(); 1"},
            {"until": "document.querySelector('.tv-sendnote').textContent.indexOf('Not delivered') === 0"},
            {"eval": "JSON.stringify({draft: document.querySelector('.tv-input').value, note: document.querySelector('.tv-sendnote').textContent})"},
        ])
    got = json.loads(out["values"][5])
    assert got["draft"] == "a line that never lands" and "not sent" in got["note"] and "Sent" not in got["note"]


@needs_browser
def test_a_draft_typed_while_a_send_is_in_flight_survives_and_polls_keep_send_disabled(tmp_path, live):
    svc, cs, world, session = live
    _root, src, _ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, thread_chat=svc, extra_assets=_assets(_HOLD_BOOT)) as (base, _h):
        out = _drive(tmp_path, base + "/tvc.html", [
            {"until": "document.querySelector('.tv-row')"},
            {"eval": "document.querySelector('.tv-row').click(); 1"},
            {"until": "document.querySelector('.tv-input') && !document.querySelector('.tv-input').disabled"},
            {"eval": "document.querySelector('.tv-input').value = 'first'; document.querySelector('.tv-send').click(); window.__mark = window.__reads; 1"},
            {"until": "window.__held.length === 1"},
            # several polls repaint the dock while the send is held: Send stays disabled throughout
            {"eval": "document.querySelector('.tv-input').value = 'second, typed meanwhile'; 1"},
            {"until": "window.__reads >= window.__mark + 4"},
            {"eval": "JSON.stringify({send: document.querySelector('.tv-send').disabled, ta: document.querySelector('.tv-input').disabled})"},
            {"eval": "window.__held.shift()(); 1"},
            {"until": "document.querySelector('.tv-sendnote').textContent === 'Sent.'"},
            {"eval": "JSON.stringify({draft: document.querySelector('.tv-input').value, send: document.querySelector('.tv-send').disabled,"
                     " posts: window.__posts.map(p => JSON.parse(p))})"},
        ])
    during = json.loads(out["values"][7])
    after = json.loads(out["values"][10])
    assert during == {"send": True, "ta": False}
    assert after["draft"] == "second, typed meanwhile" and after["send"] is False
    [sent] = after["posts"]
    assert sent["body"] == "first" and sent["expected_launch_id"] == world["launch"]
    assert [r[4] for r in _rows(cs)] == ["first"]
