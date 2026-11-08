"""R2 of the 492-on-the-room plan: ``GET /cockpit/room.json?after=<gseq>&wait=<s>`` long-polls, and the manifest's
``transport.room`` says the room's cursor and wait bound exist."""
from __future__ import annotations

import http.client
import json
import threading
import time
from contextlib import contextmanager

import pytest

from levain.cockpit import room as cockpit_room
from levain.web_server import make_server
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_room import TOKEN, World, _call, _serve_room
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture)

GET = cockpit_room.ROOM_PATH
OWNER_KEY = "room-wait-owner-key"                  # the `browser` surface token: the owner key, honoured on the box only
H = "X-Levain-Write-Token"
PROXY = {"X-Forwarded-For": "100.101.102.103"}     # what tailscale serve adds: the request is off the box
WAIT_MAX = getattr(cockpit_room, "WAIT_MAX_S", 25)
FAR = 10 ** 9           # a cursor past everything the room holds


@contextmanager
def _dserve(tmp_path, w):
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


def _dcall(port, method, path, body=None, token=None, proxied=True):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        hdrs = dict(PROXY) if proxied else {}
        if body is not None:
            hdrs["Content-Type"] = "application/json"
        if token is not None:
            hdrs[H] = token
        c.request(method, path, body=json.dumps(body).encode() if body is not None else None, headers=hdrs)
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


def _pair(port, label="iphone"):
    st, off = _dcall(port, "POST", "/cockpit/pair/offer", {"label": label}, token=OWNER_KEY, proxied=False)
    assert st == 200, off
    st, cl = _dcall(port, "POST", "/cockpit/pair/claim", {"code": off["code"]})
    assert st == 200, cl
    return cl["device_id"], cl["token"]


def _revoke(port, device):
    st, pv = _dcall(port, "POST", "/cockpit/pair/revoke-preview", {"id": device}, token=OWNER_KEY, proxied=False)
    assert st == 200, pv
    st, out = _dcall(port, "POST", "/cockpit/pair/revoke",
                     {"id": device, "expect_digest": pv["digests"]["family"], "mode": "family"},
                     token=OWNER_KEY, proxied=False)
    assert st == 200 and device in out["revoked"], out


@pytest.fixture
def w(qdir):  # noqa: F811
    return World(qdir)


@pytest.fixture(autouse=True)
def quick(monkeypatch):
    monkeypatch.setattr(cockpit_room, "WAIT_TICK_S", 0.05, raising=False)


def _send(base, w, n=1, first=1):
    for i in range(first, first + n):
        assert _call(base, "/cockpit/room/send", w.body(f"line {i}", f"ob-{i:08d}"))[0] == 200


def _in_thread(fn, *a):
    out = []
    t = threading.Thread(target=lambda: out.append(fn(*a)), daemon=True)
    t.start()
    return t, out


def test_a_wait_returns_early_on_a_new_item(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 1)
        cursor = _call(base, GET, method="GET")[1]["next"]
        t, out = _in_thread(_call, base, f"{GET}?after={cursor}&wait=10", None, TOKEN, "GET")
        time.sleep(0.3)
        assert t.is_alive()
        t0 = time.monotonic()
        _send(base, w, 1, first=2)
        t.join(5)
        status, body = out[0]
        assert status == 200 and len(body["stream"]) >= 1 and body["next"] > cursor
        assert time.monotonic() - t0 < 3


def test_a_wait_with_nothing_new_ends_at_the_deadline_empty_with_next_unchanged(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 1)
        cursor = _call(base, GET, method="GET")[1]["next"]
        t0 = time.monotonic()
        status, body = _call(base, f"{GET}?after={cursor}&wait=1", method="GET")
        assert status == 200 and body["stream"] == [] and body["next"] == cursor
        assert 0.8 <= time.monotonic() - t0 < 4


def test_a_rotation_ends_the_wait_with_reset(tmp_path, w):
    with _serve_room(tmp_path, w) as (base, _h):
        _send(base, w, 1)
        cursor = _call(base, GET, method="GET")[1]["next"]
        t, out = _in_thread(_call, base, f"{GET}?after={cursor}&thread={w.thread}&wait=10", None, TOKEN, "GET")
        time.sleep(0.3)
        w._rotate()
        t.join(5)
        assert out and out[0][0] == 200 and out[0][1]["reset"] is True


@pytest.mark.parametrize("q", ["wait=1", f"after=1&wait=0", f"after=1&wait={WAIT_MAX + 1}", "after=1&wait=x",
                               "after=1&wait=-1", "after=1&wait="])
def test_a_wait_without_after_or_out_of_range_is_a_400(tmp_path, w, q):
    with _serve_room(tmp_path, w) as (base, _h):
        status, body = _call(base, f"{GET}?{q}", method="GET")
        assert status == 400 and body["error"] == "bad_request"


def test_waiters_past_the_cap_get_a_503_and_a_wait_holds_no_request_slot(tmp_path, w, monkeypatch):
    monkeypatch.setattr(cockpit_room, "WAITERS_MAX", 1, raising=False)
    with _serve_room(tmp_path, w) as (base, h):
        _send(base, w, 1)
        cursor = _call(base, GET, method="GET")[1]["next"]
        t, out = _in_thread(_call, base, f"{GET}?after={cursor}&wait=10", None, TOKEN, "GET")
        time.sleep(0.3)
        status, _ = _call(base, f"{GET}?after={cursor}&wait=10", method="GET")
        assert status == 503
        # the parked waiter holds no request_gate slot: every slot is still free for another read
        held = []
        try:
            while h.request_gate.acquire(blocking=False):
                held.append(1)
        finally:
            for _ in held:
                h.request_gate.release()
        assert len(held) >= 1
        assert _call(base, GET, method="GET")[0] == 200
        _send(base, w, 1, first=2)          # releases the waiter
        t.join(5)
        assert out[0][0] == 200


def test_a_revoked_device_ends_the_wait_with_403_within_a_tick(tmp_path, w):
    with _dserve(tmp_path, w) as port:
        dev, tok = _pair(port)
        assert _dcall(port, "GET", f"{GET}?after={FAR}", token=tok)[0] == 200
        t, out = _in_thread(_dcall, port, "GET", f"{GET}?after={FAR}&wait=20", None, tok)
        time.sleep(0.4)
        assert t.is_alive()
        t0 = time.monotonic()
        _revoke(port, dev)
        t.join(8)
        assert out and out[0][0] == 403 and time.monotonic() - t0 < 4


def test_the_manifest_names_the_room_only_with_a_room_host_and_its_etag_moves(tmp_path, w):
    def manifest(**kw):
        _root, src, _ids = _install(tmp_path / ("m" + str(len(kw))))
        httpd = make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": OWNER_KEY}, **kw)
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            c = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=20)
            c.request("GET", "/cockpit/manifest.json", headers={"X-Levain-Write-Token": OWNER_KEY})
            r = c.getresponse()
            return json.loads(r.read()), r.getheader("ETag")
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(5)

    with_room, etag_a = manifest(chat_service=w.svc, room_host=w.host)
    without, etag_b = manifest()
    assert with_room["transport"]["room"] == {"read": GET, "cursor": "gseq", "after": True, "thread": True,
                                              "wait_max_s": WAIT_MAX, "live": True}
    assert "room" not in without["transport"]
    assert etag_a and etag_b and etag_a != etag_b
