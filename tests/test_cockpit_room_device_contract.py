"""R4 of the 492-on-the-room plan: the contract a PAIRED PHONE (LevainConnect) holds with the room.

Tests only, pinning what main already does (levain project_memory 1108-23 DESIGN_492 sec 3, 1108-24 PLAN_492_room R4):
a paired device's own token reaches ``GET /cockpit/room.json`` and ``POST /cockpit/room/send`` through the proxy; the
line's owner is ``phone:<device id>`` from the credential, never the body; a resent ``outbox_id`` is answered from the
record (also after a rotation); the same id with another body is a 409 ``outbox_conflict``; a revoked device is refused
on its next request to both routes; and the owner key opens nothing off the box.

The server, the room and the pairing are the real ones (``make_server`` with a broker over a real install ledger); only
the room's child session is the test double the room tests already use. Every request a device makes carries a proxy
header, so it is an off-box request exactly as through ``tailscale serve``.
"""

from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from levain.cockpit import room as cockpit_room
from levain.web_server import make_server
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_room import World
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)

OWNER_KEY = "room-contract-owner-key"          # the `browser` surface token: the owner key, honoured on the box only
H = "X-Levain-Write-Token"
PROXY = {"X-Forwarded-For": "100.101.102.103"}  # what tailscale serve adds: the request is off the box
ROOM = cockpit_room.ROOM_PATH
SEND = "/cockpit/room/send"


@contextmanager
def _serve(tmp_path: Any, w: World) -> Iterator[int]:
    _root, src, _ids = _install(tmp_path)
    httpd = make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": OWNER_KEY}, install_class="reported",
                        chat_service=w.svc, room_host=w.host)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _call(port: int, method: str, path: str, body: Any = None, token: str | None = None,
          proxied: bool = True) -> tuple[int, dict]:
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    try:
        hdrs = dict(PROXY) if proxied else {}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        if token is not None:
            hdrs[H] = token
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hdrs)
        r = c.getresponse()
        raw = r.read()
        return r.status, json.loads(raw or b"{}")
    finally:
        c.close()


def _pair(port: int, label: str = "iphone") -> tuple[str, str]:
    """A device paired the way a phone pairs: the box mints an offer (owner key, loopback), the phone claims it."""
    st, off = _call(port, "POST", "/cockpit/pair/offer", {"label": label}, token=OWNER_KEY, proxied=False)
    assert st == 200, off
    st, cl = _call(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})
    assert st == 200, cl
    return cl["device_id"], cl["token"]


def _revoke(port: int, device: str) -> None:
    st, pv = _call(port, "POST", "/cockpit/pair/revoke-preview", {"id": device}, token=OWNER_KEY, proxied=False)
    assert st == 200, pv
    st, out = _call(port, "POST", "/cockpit/pair/revoke",
                    {"id": device, "expect_digest": pv["digests"]["family"], "mode": "family"},
                    token=OWNER_KEY, proxied=False)
    assert st == 200 and device in out["revoked"], out


@pytest.fixture
def w(qdir):  # noqa: F811
    return World(qdir)


# --- 1. a paired device's own token reaches both routes ---------------------------------------------------------------

def test_a_paired_devices_token_reaches_the_room_read_and_the_room_send(tmp_path, w):
    with _serve(tmp_path, w) as port:
        _dev, tok = _pair(port)
        st, room = _call(port, "GET", ROOM, token=tok)
        assert st == 200 and room["room_thread"] == w.thread, room
        st, sent = _call(port, "POST", SEND, w.body("from the phone"), token=tok)
        assert st == 200 and sent["receipt"]["state"] == "accepted" and sent["receipt"]["created"] is True, sent
        # no credential at all, through the same proxy, is refused on both (the contrast that makes the 200s mean something)
        assert _call(port, "GET", ROOM)[0] == 403
        assert _call(port, "POST", SEND, w.body("nobody", "ob-00000002"))[0] == 403
    assert [ln[3] for ln in w.lines()] == ["from the phone"]


# --- 2. the owner is the credential's, never the body's ---------------------------------------------------------------

def test_the_owner_of_a_phone_line_is_phone_and_the_device_id_whatever_the_body_names(tmp_path, w):
    with _serve(tmp_path, w) as port:
        dev, tok = _pair(port)
        _other, _t2 = _pair(port, "ipad")
        st, _b = _call(port, "POST", SEND, w.body("hello", author_kind="coordinator", author_surface="cockpit",
                                                  client_id="phone:someone-else", owner="phone:someone-else"), token=tok)
        assert st == 200      # main IGNORES an author field in the body (it is not read), it does not refuse the line
    # (author_kind, author_surface, client_id): surface `phone`, client the paired device, kind operator
    assert w.lines() == [("operator", "phone", dev, "hello", "queue", None)]
    assert w.svc.message_thread("phone:" + dev, "ob-00000001").thread_id == w.thread    # the record answers under phone:<id> alone
    assert w.svc.message_thread("phone:someone-else", "ob-00000001") is None


# --- 3. a resent outbox_id is answered from the record ----------------------------------------------------------------

def test_a_resent_outbox_id_is_answered_from_the_record_with_no_second_line_also_after_a_rotation(tmp_path, w):
    with _serve(tmp_path, w) as port:
        _dev, tok = _pair(port)
        st, first = _call(port, "POST", SEND, w.body("once"), token=tok)
        assert st == 200 and first["receipt"]["created"] is True
        st, again = _call(port, "POST", SEND, w.body("once"), token=tok)           # the app was killed before the answer
        assert st == 200 and again["receipt"]["created"] is False
        assert again["receipt"]["message_id"] == first["receipt"]["message_id"]
        assert len(w.lines()) == 1 and len(w.session.written) == 1
        w._rotate()                                                                # the room rotates under the phone
        st, after = _call(port, "POST", SEND, w.body("once"), token=tok)           # same id, the old room thread
        assert st == 200 and after["receipt"]["created"] is False
        assert after["receipt"]["message_id"] == first["receipt"]["message_id"]
        st, fresh = _call(port, "POST", SEND, w.body("new", "ob-00000002"), token=tok)
        assert st == 409 and fresh["error"] == "room_rotated"                      # the contrast: a NEW line is refused
    assert len(w.lines()) == 1 and len(w.session.written) == 1


# --- 4. the same outbox_id with another body is a conflict ------------------------------------------------------------

def test_the_same_outbox_id_with_a_different_body_is_a_409_outbox_conflict_and_writes_nothing(tmp_path, w):
    with _serve(tmp_path, w) as port:
        _dev, tok = _pair(port)
        assert _call(port, "POST", SEND, w.body("first"), token=tok)[0] == 200
        st, body = _call(port, "POST", SEND, w.body("something else"), token=tok)
        assert st == 409 and body["error"] == "outbox_conflict", body
    assert [ln[3] for ln in w.lines()] == ["first"] and len(w.session.written) == 1


# --- 5. a revoked device is refused on its next request to both routes ------------------------------------------------

def test_a_revoked_device_is_refused_on_its_next_request_to_both_routes_and_writes_nothing(tmp_path, w):
    with _serve(tmp_path, w) as port:
        dev, tok = _pair(port)
        _keep, other = _pair(port, "ipad")
        assert _call(port, "GET", ROOM, token=tok)[0] == 200
        assert _call(port, "POST", SEND, w.body("before"), token=tok)[0] == 200
        _revoke(port, dev)
        st, body = _call(port, "GET", ROOM, token=tok)
        assert st == 403 and w.thread not in json.dumps(body), body
        st, body = _call(port, "POST", SEND, w.body("after", "ob-00000002"), token=tok)
        assert st == 403, body
        assert _call(port, "GET", ROOM, token=other)[0] == 200          # the contrast: a sibling device is untouched
    assert [ln[3] for ln in w.lines()] == ["before"] and len(w.session.written) == 1


# --- 6. the owner key opens nothing off the box -----------------------------------------------------------------------

def test_the_owner_key_is_refused_on_both_routes_when_the_request_is_off_the_box(tmp_path, w):
    with _serve(tmp_path, w) as port:
        # on the box (loopback, no proxy header) the key works: the contrast
        assert _call(port, "GET", ROOM, token=OWNER_KEY, proxied=False)[0] == 200
        # through the proxy it is the no-credential answer, never a phone's
        st, body = _call(port, "GET", ROOM, token=OWNER_KEY)
        assert st == 403 and w.thread not in json.dumps(body), body
        st, body = _call(port, "POST", SEND, w.body("with the key"), token=OWNER_KEY)
        assert st == 403, body
    assert w.lines() == [] and w.session.written == []
