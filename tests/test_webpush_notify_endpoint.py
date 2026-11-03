"""The notify endpoint (N2): ``POST /notify`` on a real server over a scratch queue, its delivery through the existing
push and held paths, the token admin routes and the ``levain notify token list`` CLI. Built from the hand runs of the N2 brief:
curl-shaped posts for every status, the confinement of a notify token to its one route, the fixed tag and link on the
enqueued ping, and a held notice released by the existing flush with a summary that names its sources."""
from __future__ import annotations

import http.client
import json
import socket
import threading
import time

import pytest

from levain.cli import main
from levain.webpush import held, notify, notify_tokens as nt, presence, sender
from levain.webpush.store import PushStore
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_pairing_http import PROXY, T, _call
from tests.test_cockpit_webpush import _signed

URL = "/notify"
TOKENS = "/webpush/notify/tokens"
REVOKE = "/webpush/notify/tokens/revoke"
IDLE = {"v": 9999.0}      # the desk's input idle seconds: far past ACTIVE_WINDOW_SECS unless a test says otherwise


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    from levain.consent import band

    monkeypatch.setattr(band, "agent_process", lambda: None)
    monkeypatch.setattr("levain.consent.auto_away.input_idle_seconds", lambda: IDLE["v"])
    IDLE["v"] = 9999.0
    nt.MINUTE.clear()
    yield
    nt.MINUTE.clear()


@pytest.fixture
def rig(tmp_path):
    q = tmp_path / "signq"
    q.mkdir(mode=0o700)
    return _install(tmp_path)[1], q


def _post(port, payload=None, *, token=None, bearer=None, headers=None, raw=None, ctype="application/json"):
    """A curl-shaped POST /notify: (status, parsed body, lower-cased headers)."""
    h = dict(headers or {})
    if ctype:
        h["Content-Type"] = ctype
    if token is not None:
        h[nt.HEADER] = token
    if bearer is not None:
        h["Authorization"] = f"Bearer {bearer}"
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.request("POST", URL, body=raw if raw is not None else json.dumps(payload).encode(), headers=h)
    r = c.getresponse()
    data = r.read()
    try:
        out = json.loads(data or b"{}")
    except ValueError:
        out = {"_raw": data.decode("utf-8", "replace")}
    return r.status, out, {k.lower(): v for k, v in r.getheaders()}


def _raw(port, head: str, body: bytes = b"", wait: float = 5.0):
    """Hand-written request bytes; the answer's status, parsed body and headers. Times out (raises) if the server waits."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    try:
        s.sendall(head.encode("ascii") + body)
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
        top, _, rest = buf.partition(b"\r\n\r\n")
        lines = top.decode("latin-1").split("\r\n")
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:])}
        n = int(hdrs.get("content-length", "0"))
        while len(rest) < n:
            chunk = s.recv(65536)
            if not chunk:
                break
            rest += chunk
        return int(lines[0].split()[1]), json.loads(rest or b"{}"), hdrs
    finally:
        s.close()


def _head(port, extra: str = "", clen: str | None = "10", ctype: str = "application/json", *, close: bool = True) -> str:
    """The request head; ``close=False`` leaves the connection kept alive (HTTP/1.1's default), so a ``Connection:
    close`` in the answer is the server's own choice."""
    return (f"POST {URL} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n" + (f"Content-Type: {ctype}\r\n" if ctype else "")
            + (f"Content-Length: {clen}\r\n" if clen is not None else "") + extra
            + ("Connection: close\r\n" if close else "") + "\r\n")


def _mint(q, name="Backup", **kw):
    return nt.create(q, name, **kw)[1]


# ---- every status in the spec ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("word,priority,expect", [
    (None, "normal", "pushed"),          # nothing muted, desk idle: pushed
    (None, "low", "recorded"),
    ("quiet", "normal", "held"),         # other: held by the preset's lets_through
    ("sleep", "normal", "held"),
    ("quiet", "low", "recorded"),
    ("sleep", "low", "recorded"),
    ("quiet", "urgent", "pushed"),       # quiet lets attention through
    ("sleep", "urgent", "pushed"),       # so does sleep (Phill 10-25)
])
def test_200_for_each_decision_under_each_mute(rig, word, priority, expect, monkeypatch):
    src, q = rig
    if word:
        presence.set_word(q, word, duration="1h" if word == "quiet" else None)
    tok = _mint(q, "Backup", urgent=True)
    with _signed(src, q) as (port, _):
        monkeypatch.setattr(held, "flush_if_due", lambda *a, **k: False)
        st, got, _ = _post(port, {"title": "Disk 90%", "body": "linode-1 root volume", "priority": priority}, token=tok)
    assert st == 200 and got["ok"] is True and got["decision"] == expect, got
    notices = PushStore(q).notices()
    assert len(notices) == 1 and notices[0]["id"] == got["id"] and notices[0]["decision"] == expect
    assert notices[0]["source"] == "backup" and notices[0]["priority"] == priority
    pings = PushStore(q).record()
    if expect == "recorded":
        assert pings == [] and notices[0]["ping_id"] is None
    else:
        assert len(pings) == 1 and pings[0]["id"] == notices[0]["ping_id"]
        assert pings[0]["kind"] == ("notify_urgent" if priority == "urgent" else "notify")
        assert pings[0]["title"] == "[backup] Disk 90%" and pings[0]["ref"] == "notify:backup"
        waiting = PushStore(q).held_items()
        assert [h["ping_id"] for h in waiting] == ([pings[0]["id"]] if expect == "held" else [])
        if expect == "held":
            assert waiting[0]["reason"] == (word or "active") and waiting[0]["url"] == "/?view=notifications"


def _typed(q, ago=5.0):
    from levain.webpush import desk

    desk._write(q, desk.DESK_MESSAGE_NAME, {"at": time.time() - ago, "source": "test", "desk": ""})


def test_a_desk_in_use_holds_a_normal_notice_as_active(rig, monkeypatch):
    src, q = rig
    IDLE["v"] = 5.0
    _typed(q)                                    # 1a (Phill 11-03): in use = a desk message just now
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        monkeypatch.setattr(held, "flush_if_due", lambda *a, **k: False)
        st, got, _ = _post(port, {"body": "x"}, token=tok)
    assert st == 200 and got["decision"] == "held"
    assert PushStore(q).held_items()[0]["reason"] == "active"


def test_a_title_may_be_left_out_and_the_label_is_the_tokens(rig):
    src, q = rig
    tok = _mint(q, "Site Monitor")
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, {"body": "down since 02:10"}, token=tok)
    assert st == 200
    n = PushStore(q).notices()[0]
    assert n["source"] == "site-monitor" and n["title"] == "Notice"
    assert PushStore(q).record()[0]["title"] == "[site-monitor] Notice"


@pytest.mark.parametrize("payload", [
    {"body": "x", "url": "https://evil"}, {"body": "x", "actions": []}, {"body": "x", "click": "https://x"},
    {"body": "x", "attach": "y"}, {"body": "x", "icon": "y"}, {"body": "x", "extras": {}}, {"body": "x", "html": "<b>"},
    {"body": "x", "markdown": True}, {"body": "x", "tags": ["a"]}, {"body": "x", "topic": "t"},
    {"body": "x", "source": "flow"}, {"body": "x", "sender": "flow"}, {"body": "x", "from": "flow"},
    {"body": "x", "kind": "consent"}, {"body": "x", "ref": "request:1"}, {"body": "x", "callback": "https://x"},
    {"body": "x", "retry": 30}, {"body": "x", "expire": 60}, {"body": "x", "ack": True}, {"body": "x", "receipt": "r"},
    {"body": "x", "user": "u"}, {"body": "x", "device": "d"}, {"body": "x", "to": "t"}, {"body": "x", "ttl": 5},
    {"body": "x", "timestamp": 1},
    {}, {"priority": "normal"}, [], "text", 7, None,
])
def test_400_for_a_bad_shape_or_any_extra_key(rig, payload):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, payload, token=tok)
    assert st == 400 and got["error"] == "bad_request", got
    assert PushStore(q).notices() == []
    assert nt.list_public(q)[0]["sent_today"] == 0          # nothing counted against the token


@pytest.mark.parametrize("raw", [b"{nope", b"", b"\xff\xfe", b'{"body": "x"} trailing'])
def test_400_for_bytes_that_are_not_json(rig, raw):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, raw=raw, token=tok)
    assert st == 400 and got["error"] == "bad_json"


@pytest.mark.parametrize("payload,error", [
    ({"body": 5}, "bad_shape"), ({"title": 5, "body": "x"}, "bad_shape"), ({"title": None, "body": "x"}, "bad_shape"),
    ({"body": "x", "priority": "high"}, "bad_priority"), ({"body": "x", "priority": 3}, "bad_priority"),
])
def test_400_for_a_field_of_the_wrong_kind(rig, payload, error):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, payload, token=tok)
    assert st == 400 and got["error"] == error


def test_401_without_a_credential_and_the_same_403_for_unknown_and_revoked(rig):
    src, q = rig
    live = _mint(q, "Live")
    gone = _mint(q, "Gone")
    nt.revoke(q, "Gone")
    with _signed(src, q) as (port, _):
        st, got, hdrs = _post(port, {"body": "x"})
        assert st == 401 and got["error"] == "unauthorized" and hdrs["www-authenticate"] == "Bearer"
        bodies = []
        for kw in ({"token": "lvn_" + "a" * 43}, {"token": gone}, {"bearer": gone}, {"bearer": "nope"},
                   {"token": "has space"}, {"token": "x" * 300}):
            st, got, _ = _post(port, {"body": "x"}, **kw)
            assert st == 403 and got["error"] == "forbidden", (kw, got)
            bodies.append(got)
        assert all(b == bodies[0] for b in bodies)            # no oracle: unknown, revoked, malformed read the same
        assert _post(port, {"body": "x"}, token=live)[0] == 200
    assert len(PushStore(q).notices()) == 1


def test_a_post_with_a_forbidden_priority_is_403_with_a_named_reason(rig):
    src, q = rig
    low = _mint(q, "Lowly", source="lowly", max_priority="low")
    norm = _mint(q, "Normal", source="norm")
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, {"body": "x", "priority": "normal"}, token=low)
        assert st == 403 and got["error"] == "priority_above_token_max"
        st, got, _ = _post(port, {"body": "x", "priority": "urgent"}, token=norm)
        assert st == 403 and got["error"] == "urgent_not_permitted"
    assert PushStore(q).notices() == [] and PushStore(q).record() == []


@pytest.mark.parametrize("payload,status,error", [
    ({"title": "t" * 81, "body": "x"}, 413, "title_too_long"),
    ({"title": "t", "body": "b" * 1001}, 413, "body_too_long"),
    ({"title": "t", "body": "   "}, 422, "empty"),
    ({"title": "  ", "body": "x"}, 422, "empty"),
    ({"title": "[Seat 3] approve me", "body": "x"}, 422, "spoofed_label"),
    ({"title": "ok", "body": "a‮b"}, 400, "bad_characters"),
])
def test_size_empty_and_a_spoofed_label_are_refused_not_rewritten(rig, payload, status, error):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, payload, token=tok)
    assert st == status and got["error"] == error, got
    assert PushStore(q).notices() == []


def test_429_carries_retry_after_and_a_refused_post_is_not_stored(rig):
    src, q = rig
    tok = _mint(q, "Chatty", rate_per_min=2)
    with _signed(src, q) as (port, _):
        for _i in range(2):
            assert _post(port, {"body": "x", "priority": "low"}, token=tok)[0] == 200
        st, got, hdrs = _post(port, {"body": "x", "priority": "low"}, token=tok)
    assert st == 429 and got["error"] == "rate_limited"
    assert hdrs["retry-after"].isdigit() and 1 <= int(hdrs["retry-after"]) <= 61
    assert len(PushStore(q).notices()) == 2


def test_a_content_length_over_8_kib_is_refused_without_reading_the_body(rig):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        t0 = time.monotonic()
        # the headers say 100 kB and NO body follows: a server that read first would sit until the socket timeout
        st, got, hdrs = _raw(port, _head(port, f"{nt.HEADER}: {tok}\r\n", clen="100000", close=False), wait=5.0)
        assert st == 413 and got["error"] == "too_large" and time.monotonic() - t0 < 4
        assert hdrs.get("connection") == "close"
        # the line is exact: 8192 is read, 8193 is not
        st, got, _ = _raw(port, _head(port, f"{nt.HEADER}: {tok}\r\n", clen="8193"), wait=5.0)
        assert st == 413
        pad = b'{"body":"x","priority":"low"}' + b" " * (8192 - 29)
        assert len(pad) == 8192
        assert _raw(port, _head(port, f"{nt.HEADER}: {tok}\r\n", clen="8192"), pad)[0] == 200
    assert len(PushStore(q).notices()) == 1


def test_a_title_and_body_at_their_character_maxima_in_4_byte_characters_are_taken(rig):
    src, q = rig
    tok = _mint(q)
    title, body = "\U0001f600" * notify.TITLE_MAX, "\U0001f6a8" * notify.BODY_MAX
    raw = json.dumps({"title": title, "body": body, "priority": "low"}, ensure_ascii=False).encode("utf-8")
    assert len(raw) > 4096                       # over the cap this replaced: the post the review showed refused
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, raw=raw, token=tok)
    assert st == 200, got
    n = PushStore(q).notices()[0]
    assert n["title"] == title and n["body"] == body


@pytest.mark.parametrize("clen", [None, "abc", "-1", "1 2"])
def test_a_missing_or_malformed_content_length_is_411(rig, clen):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        head = _head(port, f"{nt.HEADER}: {tok}\r\n", clen=None)
        if clen is not None:
            head = head.replace("Connection: close", f"Content-Length: {clen}\r\nConnection: close")
        st, got, _ = _raw(port, head)
    assert st == 411 and got["error"] == "length_required"
    assert PushStore(q).notices() == []


def test_a_non_ascii_digit_content_length_is_411(rig):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall((f"POST {URL} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nContent-Type: application/json\r\n"
                       f"{nt.HEADER}: {tok}\r\nContent-Length: ²\r\nConnection: close\r\n\r\n").encode("latin-1"))
            assert b" 411 " in s.recv(4096).split(b"\r\n")[0]
        finally:
            s.close()


@pytest.mark.parametrize("ctype", ["text/plain", "application/x-www-form-urlencoded", "application/jsonx", None])
def test_only_application_json_is_taken(rig, ctype):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, {"body": "x"}, token=tok, ctype=ctype)
        assert st == 415 and got["error"] == "unsupported_media_type"
        assert _post(port, {"body": "x", "priority": "low"}, token=tok, ctype="application/json; charset=utf-8")[0] == 200
    assert len(PushStore(q).notices()) == 1


def test_bearer_is_an_alias_of_the_header_with_the_same_outcome(rig):
    src, q = rig
    tok = _mint(q, "Hub", urgent=True)
    outcomes = []
    with _signed(src, q) as (port, _):
        for kw in ({"token": tok}, {"bearer": tok}, {"headers": {"Authorization": f"bearer {tok}"}}):
            st, got, _ = _post(port, {"title": "t", "body": "same", "priority": "low"}, **kw)
            outcomes.append((st, got["ok"], got["decision"]))
        bad = [_post(port, {"body": "x"}, **kw)[:2] for kw in ({"token": "nope"}, {"bearer": "nope"})]
        assert bad[0] == bad[1] and bad[0][0] == 403
        # a Basic scheme is not a bearer
        assert _post(port, {"body": "x"}, headers={"Authorization": f"Basic {tok}"})[0] == 401
    assert outcomes == [(200, True, "recorded")] * 3
    assert [n["source"] for n in PushStore(q).notices()] == ["hub"] * 3


def test_a_token_in_the_url_or_the_body_is_not_a_credential(rig):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        c.request("POST", f"{URL}?token={tok}", body=json.dumps({"body": "x", "token": tok}).encode(),
                  headers={"Content-Type": "application/json"})
        assert c.getresponse().status == 401
        st, got, _ = _post(port, {"body": "x", "token": tok})
        assert st == 401
    assert PushStore(q).notices() == []


def test_get_on_notify_is_not_an_endpoint(rig):
    src, q = rig
    tok = _mint(q)
    with _signed(src, q) as (port, _):
        for hdrs in ({nt.HEADER: tok}, {"Authorization": f"Bearer {tok}"}, None):
            assert _call(port, "GET", URL, headers=hdrs)[0] in (403, 404, 405)


def test_without_a_signer_there_is_no_notify_route(rig, tmp_path):
    from tests.test_cockpit_pairing_http import _serve

    src, q = rig
    with _serve(src) as (port, _):
        assert _post(port, {"body": "x"}, token="lvn_x")[0] == 404


# ---- confinement: a notify token reaches /notify and nothing else ----------------------------------------------------

def test_a_notify_token_is_refused_on_every_other_route_and_changes_nothing(rig):
    src, q = rig
    tok = _mint(q, "Hub", urgent=True)
    both = {nt.HEADER: tok, "Authorization": f"Bearer {tok}"}
    with _signed(src, q) as (port, _):
        gets = ["/webpush/presence", "/webpush/policy", "/webpush/record", "/webpush/key", TOKENS, "/substrate.json",
                "/cockpit/manifest.json", "/cockpit/pair/list", "/recall.json"]
        for path in gets:
            # a notify token adds nothing: the answer is the one a request with no credential gets, loopback or proxied
            for extra in (None, PROXY):
                bare = _call(port, "GET", path, headers=extra)[0]
                st, _g, _h = _call(port, "GET", path, headers={**both, **(extra or {})})
                assert st == bare, (path, st, bare)
                if extra:
                    assert st in (401, 403, 404), (path, st)
        posts = [("/webpush/presence/set", {"word": "away"}), ("/webpush/presence/set", {"word": "sleep"}),
                 ("/webpush/policy/set", {"mute": "sleep", "class": "other", "value": "push"}),
                 ("/webpush/presence/token/new", {"name": "x"}), (TOKENS, {"name": "mine"}), (REVOKE, {"name": "Hub"}),
                 ("/webpush/test", {}), ("/edit", {}), ("/action", {}), ("/cockpit/pair/offer", {"label": "x"})]
        for path, body in posts:
            for hdrs in (both, {**both, **PROXY}):
                st, _g, _h = _call(port, "POST", path, body, headers=hdrs)
                assert st in (401, 403, 404), (path, st)
    assert presence.read_word(q) is None
    assert [r["name"] for r in nt.list_public(q)] == ["Hub"]              # not minted over, not revoked
    assert PushStore(q).notices() == []


def test_a_presence_tap_token_and_the_full_credential_are_refused_on_notify(rig):
    from levain.webpush import presence_tokens

    src, q = rig
    _rec, tap = presence_tokens.create(q, "door")
    with _signed(src, q) as (port, _):
        for kw in ({"headers": {"X-Levain-Presence-Token": tap}}, {"headers": {"X-Levain-Write-Token": T}},
                   {"headers": {"X-Levain-Write-Token": T, **PROXY}}):
            st, got, _ = _post(port, {"body": "x"}, **kw)
            assert st == 401, kw
        # a real credential offered as a notify token is just a token that is not one
        assert _post(port, {"body": "x"}, bearer=T)[0] == 403
        assert _post(port, {"body": "x"}, token=tap)[0] == 403
        assert _post(port, {"body": "x"}, bearer=tap)[0] == 403
    assert PushStore(q).notices() == []


def test_the_presence_routes_still_take_a_tap_token_and_not_the_notify_header(rig):
    """The carve-out for /notify did not move the presence one."""
    from levain.webpush import presence_tokens

    src, q = rig
    _rec, tap = presence_tokens.create(q, "door")
    with _signed(src, q) as (port, _):
        assert _call(port, "POST", "/webpush/presence/set", {"word": "away"},
                     headers={"X-Levain-Presence-Token": tap})[0] == 200
        assert presence.read_word(q)["word"] == "away"


# ---- delivery: the tag, the link, the text, the held summary ---------------------------------------------------------

def _capture_payloads(monkeypatch):
    seen, done = [], threading.Event()
    real = sender.build_payload

    def spy(title, body, tag, url, ping_id, badge=None):
        seen.append({"title": title, "body": body, "tag": tag, "url": url, "ping": ping_id})
        done.set()
        return real(title, body, tag, url, ping_id, badge)

    monkeypatch.setattr(sender, "build_payload", spy)
    return seen, done


def test_a_pushed_notice_goes_out_under_a_fixed_tag_per_source_and_a_fixed_link(rig, monkeypatch):
    src, q = rig
    seen, done = _capture_payloads(monkeypatch)
    a, b = _mint(q, "Alpha"), _mint(q, "Beta")
    with _signed(src, q) as (port, _):
        for tok, title, body in ((a, "Disk 90%", "root volume"), (a, "Disk 95%", "root volume again"), (b, "Down", "site")):
            done.clear()
            assert _post(port, {"title": title, "body": body}, token=tok)[1]["decision"] == "pushed"
            assert done.wait(5)
            time.sleep(0.05)
    assert len(seen) == 3
    assert [s["tag"] for s in seen] == ["notify:alpha", "notify:alpha", "notify:beta"]   # one tag per source
    assert [s["url"] for s in seen] == [f"/?view=notifications&n=n{i}" for i in (1, 2, 3)]   # the item's own id
    assert [s["title"] for s in seen] == ["[alpha] Disk 90%", "[alpha] Disk 95%", "[beta] Down"]
    assert seen[0]["body"] == "root volume"
    assert sender.topic_for("notify:alpha") != sender.topic_for("notify:beta")
    assert sender.topic_for("notify:alpha") != sender.topic_for("request:abc")
    rec = PushStore(q).record()
    assert {p["ref"] for p in rec} == {"notify:alpha", "notify:beta"} and all(p["kind"] == "notify" for p in rec)


def test_markup_in_a_notice_is_carried_as_the_characters_it_is(rig, monkeypatch):
    src, q = rig
    seen, done = _capture_payloads(monkeypatch)
    tok = _mint(q)
    text = '<script>alert(1)</script> [click](https://evil.example) https://evil.example/x'
    with _signed(src, q) as (port, _):
        assert _post(port, {"title": "<b>hi</b>", "body": text}, token=tok)[0] == 200
        assert done.wait(5)
    assert seen[0]["body"] == text and seen[0]["title"] == "[backup] <b>hi</b>"
    assert seen[0]["url"] == "/?view=notifications&n=n1"      # the poster's URL never became the link


def test_a_held_notice_is_released_by_the_existing_flush_with_a_summary_naming_its_sources(rig, monkeypatch):
    src, q = rig
    presence.set_word(q, "sleep")
    a, b = _mint(q, "Nightly Backup"), _mint(q, "Site Monitor")
    with _signed(src, q) as (port, _):
        monkeypatch.setattr(held, "flush_if_due", lambda *a, **k: False)
        for tok, t in ((a, "ran"), (b, "down"), (a, "ran again")):
            assert _post(port, {"title": t, "body": "text"}, token=tok)[1]["decision"] == "held"
    # a seat's ping held beside them keeps its own summary and its own link
    PushStore(q).add_ping("question", "question:1", "levain", "approve me", hold=("/?view=needs", "sleep"))
    assert held.count(q) == 4
    sent = []
    monkeypatch.setattr("levain.webpush.live", lambda qd: True)
    monkeypatch.setattr(sender, "deliver", lambda qdir, store, pid, **kw: sent.append((pid, kw)) or [(1, 201)])
    assert held.flush(q, idle=lambda: 9999.0) == 0 and sent == []         # still asleep: nothing goes
    presence.set_word(q, "back")
    _typed(q)
    assert held.flush(q, idle=lambda: 5.0) == 0                           # back, but he just sent a desk message: follow keeps them for the desk
    _typed(q, 9999.0)
    assert held.flush(q, idle=lambda: 5.0) == 4                           # no message for the follow time (his laptop input is no matter): devices on
    summaries = [kw for _pid, kw in sent if kw["kind"] == "summary"]
    assert sorted(s["body"] for s in summaries) == ["1 ping held while you were asleep; they follow.",
                                                    "3 notices held: nightly-backup, site-monitor"]
    by_body = {s["body"]: s["url"] for s in summaries}
    assert by_body["3 notices held: nightly-backup, site-monitor"] == "/?view=notifications"
    assert by_body["1 ping held while you were asleep; they follow."] == "/?view=needs"
    notices = [kw for _pid, kw in sent if kw["kind"] == "notify"]
    assert sorted(n["title"] for n in notices) == ["[nightly-backup] ran", "[nightly-backup] ran again", "[site-monitor] down"]
    assert all(n["url"] == "/?view=notifications" and n["ref"].startswith("notify:") for n in notices)
    assert held.count(q) == 0


def test_the_summary_is_bounded_in_the_sources_it_names():
    items = [{"ref": f"notify:s{i}", "kind": "notify"} for i in range(7)] * 2
    line = held.notice_summary(items)
    assert line == "14 notices held: s0, s1, s2, s3 and 3 more"
    assert held.notice_summary([{"ref": "notify:one", "kind": "notify"}]) == "1 notice held: one"


def test_a_post_starts_the_existing_flush_for_what_a_mute_that_ended_left_held(rig, monkeypatch):
    src, q = rig
    tok = _mint(q)
    called = []
    monkeypatch.setattr(held, "flush_if_due", lambda qd, **kw: called.append(str(qd)) or True)
    with _signed(src, q) as (port, _):
        assert _post(port, {"body": "x"}, token=tok)[0] == 200
        assert _post(port, {"body": "x", "priority": "low"}, token=tok)[0] == 200
    assert called == [str(q)]                       # a low notice, recorded only, has nothing to release


# ---- the admin routes -----------------------------------------------------------------------------------------------

def test_mint_list_and_revoke_ride_the_full_credential_only(rig):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", TOKENS, {"name": "Hub", "urgent": True, "rate_per_min": 3}, token=T)
        assert st == 200 and got["token"].startswith("lvn_") and got["record"]["urgent"] is True
        tok = got["token"]
        assert got["record"]["rate_per_min"] == 3 and "sha256" not in got["record"]
        assert [r["name"] for r in got["tokens"]] == ["Hub"]
        st, listing, _ = _call(port, "GET", TOKENS, token=T)
        assert st == 200 and tok not in json.dumps(listing) and "sha256" not in json.dumps(listing)
        assert [r["name"] for r in listing["tokens"]] == ["Hub"]
        # the minted token posts, urgent included
        assert _post(port, {"body": "x", "priority": "urgent"}, token=tok)[0] == 200
        # no credential, a wrong one, a notify token and a tap token do not list, mint or revoke
        from levain.webpush import presence_tokens

        tap = presence_tokens.create(q, "door")[1]
        for hdrs in (None, PROXY, {nt.HEADER: tok}, {"Authorization": f"Bearer {tok}"}, {"X-Levain-Presence-Token": tap}):
            assert _call(port, "GET", TOKENS, headers=hdrs)[0] == 403
            assert _call(port, "POST", TOKENS, {"name": "evil"}, headers=hdrs)[0] == 403
            assert _call(port, "POST", REVOKE, {"name": "Hub"}, headers=hdrs)[0] == 403
        assert _call(port, "POST", TOKENS, {"name": "evil"}, token="wrong")[0] == 403
        assert [r["name"] for r in nt.list_public(q)] == ["Hub"]
        st, got, _ = _call(port, "POST", REVOKE, {"name": "Hub"}, token=T)
        assert st == 200 and got["tokens"] == []
        assert _post(port, {"body": "x"}, token=tok)[0] == 403
        assert _call(port, "POST", REVOKE, {"name": "Hub"}, token=T)[0] == 404


def test_a_mint_whose_listing_fails_still_returns_the_token_it_made(rig, monkeypatch):
    # the secret is shown once: a failed supplementary listing after the commit must not withhold it
    src, q = rig
    def boom(_q):
        raise nt.NotifyTokenError("listing failed")
    monkeypatch.setattr(nt, "list_public", boom)
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", TOKENS, {"name": "Hub"}, token=T)
    assert st == 200 and got["token"].startswith("lvn_") and got["tokens"] == []
    monkeypatch.undo()
    assert [r["name"] for r in nt.list_public(q)] == ["Hub"]


@pytest.mark.parametrize("body,status,error", [
    ({}, 400, "bad_request"), ({"name": 3}, 400, "bad_request"), ({"name": "a", "words": ["x"]}, 400, "bad_request"),
    ({"name": "a", "caps": {}}, 400, "bad_request"), ([1], 400, "bad_request"),
    ({"name": "bad/name"}, 422, "notify_token_refused"), ({"name": "a", "urgent": "yes"}, 422, "notify_token_refused"),
    ({"name": "a", "max_priority": "urgent"}, 422, "notify_token_refused"),
    ({"name": "a", "max_priority": "high"}, 422, "notify_token_refused"),
    ({"name": "a", "rate_per_min": 0}, 422, "notify_token_refused"),
    ({"name": "a", "source": "Has Caps"}, 422, "notify_token_refused"),
])
def test_a_bad_mint_is_refused_and_mints_nothing(rig, body, status, error):
    src, q = rig
    with _signed(src, q) as (port, _):
        st, got, _ = _call(port, "POST", TOKENS, body, token=T)
    assert st == status and got["error"] == error, got
    assert nt.list_public(q) == []


def test_a_duplicate_name_or_source_is_refused(rig):
    src, q = rig
    _mint(q, "Hub")
    with _signed(src, q) as (port, _):
        assert _call(port, "POST", TOKENS, {"name": "Hub"}, token=T)[0] == 422
        assert _call(port, "POST", TOKENS, {"name": "Other", "source": "hub"}, token=T)[0] == 422


def test_a_held_notify_lock_answers_503_and_changes_nothing(rig, monkeypatch):
    import fcntl

    src, q = rig
    tok = _mint(q)
    monkeypatch.setattr(nt, "_locked", _short_lock(nt._locked))
    d = q / "webpush"
    holder = open(d / nt.LOCK_NAME, "w")
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        with _signed(src, q) as (port, _):
            assert _post(port, {"body": "x"}, token=tok)[:2] == (503, {"error": "notify_busy", "message": "busy; try again"})
            st, got, _ = _call(port, "POST", TOKENS, {"name": "new"}, token=T)
            assert st == 503 and got["error"] == "notify_busy"
    finally:
        holder.close()
    assert PushStore(q).notices() == [] and [r["name"] for r in nt.list_public(q)] == ["Backup"]


def _short_lock(real):
    def locked(queue_dir, wait=3.0):
        return real(queue_dir, 0.2)

    return locked


def test_no_response_or_log_line_carries_a_token(rig, caplog, capfd):
    import logging

    src, q = rig
    tok = _mint(q)
    caplog.set_level(logging.DEBUG)
    with _signed(src, q) as (port, _):
        responses = [_post(port, {"body": "x"}, token=tok), _post(port, {"body": "x"}, bearer=tok),
                     _post(port, {"body": "x", "bad": 1}, token=tok), _post(port, {"body": "x"}, token="nope")]
    for _st, got, hdrs in responses:
        assert tok not in json.dumps(got) and tok not in json.dumps(hdrs)
    out, err = capfd.readouterr()
    assert tok not in caplog.text and tok not in out and tok not in err
    assert tok not in json.dumps(PushStore(q).notices()) and tok not in json.dumps(PushStore(q).record())


# ---- the list CLI ---------------------------------------------------------------------------------------------------

def test_the_list_cli_shows_the_live_tokens_and_never_a_secret(rig, capsys):
    _src, q = rig
    _rec, tok = nt.create(q, "Flow Hub", urgent=True)
    nt.create(q, "Plain")
    assert main(["notify", "token", "list", "--queue-dir", str(q)]) == 0
    listing = capsys.readouterr().out
    assert "Flow Hub" in listing and "Plain" in listing and tok not in listing
    assert main(["notify", "token", "list", "--queue-dir", str(q), "--json"]) == 0
    assert [r["name"] for r in json.loads(capsys.readouterr().out)] == ["Flow Hub", "Plain"]


@pytest.mark.parametrize("verb", [["mint", "--name", "x"], ["revoke", "x"]])
def test_the_terminal_mint_and_revoke_verbs_are_gone(rig, verb):
    _src, q = rig
    with pytest.raises(SystemExit):
        main(["notify", "token", *verb, "--queue-dir", str(q)])
    assert nt.list_public(q) == []


# ---- L3 r1: a token that is not live never holds a request slot ------------------------------------------------------

UNKNOWN = "lvn_" + "a" * 43          # the minted shape, but no token


def test_a_token_that_is_not_live_is_refused_from_the_header_and_never_holds_a_slot(rig):
    src, q = rig
    live = _mint(q, "Live")
    gone = _mint(q, "Gone")
    nt.revoke(q, "Gone")
    with _signed(src, q) as (port, _):
        t0 = time.monotonic()
        # more stalled posts than the server has request slots: each declares a body and never sends it. A server that
        # held a slot to read the body would sit on it until the socket timeout and this answer would never come
        for i in range(12):
            bad = (UNKNOWN, gone, "nope")[i % 3]
            st, got, hdrs = _raw(port, _head(port, f"{nt.HEADER}: {bad}\r\n", clen="100", close=False), wait=3.0)
            assert (st, got) == (403, {"error": "forbidden", "message": "unknown or revoked token"}), (bad, got)
            assert hdrs.get("connection") == "close"
        assert time.monotonic() - t0 < 4
        # the slots are free: a live post and another gated route are answered at once
        assert _post(port, {"body": "x", "priority": "low"}, token=live)[0] == 200
        assert _call(port, "GET", "/webpush/presence", token=T)[0] == 200
    assert len(PushStore(q).notices()) == 1


def _answers(port, data: bytes, wait: float = 3.0) -> tuple[int, bool]:
    """Send ``data`` on one kept-alive socket; how many responses came back, and whether the server closed it."""
    s = socket.create_connection(("127.0.0.1", port), timeout=wait)
    buf, closed = b"", False
    try:
        s.sendall(data)
        while True:
            try:
                chunk = s.recv(65536)
            except (socket.timeout, ConnectionResetError):
                break
            if not chunk:
                closed = True
                break
            buf += chunk
    finally:
        s.close()
    return buf.count(b"HTTP/1.1 "), closed


@pytest.mark.parametrize("case", ["404", "401", "403", "411", "413", "415", "503"])
def test_every_early_refusal_closes_so_an_unread_body_is_never_the_next_request(rig, case):
    from tests.test_cockpit_pairing_http import _serve

    src, q = rig
    tok = _mint(q)

    def run(port, httpd):
        smuggled = f"GET /webpush/key HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        auth = "" if case == "401" else f"{nt.HEADER}: {UNKNOWN if case == '403' else tok}\r\n"
        clen = {"411": None, "413": "9000"}.get(case, str(len(smuggled)))
        head = _head(port, auth, clen=clen, ctype="text/plain" if case == "415" else "application/json", close=False)
        held_slots = 0
        if case == "503":
            while httpd.request_gate.acquire(blocking=False):
                held_slots += 1
        try:
            n, closed = _answers(port, head.encode() + smuggled)
        finally:
            for _i in range(held_slots):
                httpd.request_gate.release()
        assert (n, closed) == (1, True), (case, n, closed)

    if case == "404":
        with _serve(src) as (port, httpd):
            run(port, httpd)
    else:
        with _signed(src, q) as (port, httpd):
            run(port, httpd)
    assert PushStore(q).notices() == []


# ---- L3 r1: a token store that fails answers 503 with a fixed message ------------------------------------------------

def test_a_token_store_write_failure_is_a_fixed_503_and_the_detail_goes_to_the_log(rig, monkeypatch, caplog):
    import logging

    src, q = rig
    tok = _mint(q)

    def full(*_a, **_k):
        raise nt.NotifyTokenError(f"notify tokens not written: [Errno 28] No space left on device: '{q}/webpush/x'")

    caplog.set_level(logging.WARNING)
    with _signed(src, q) as (port, _):
        monkeypatch.setattr(nt, "_write", full)
        st, got, _ = _post(port, {"body": "x"}, token=tok)
    assert st == 503 and got == {"error": "notify_unavailable",
                                 "message": "notices cannot be taken right now; nothing was stored, try again later"}
    assert str(q) not in json.dumps(got) and "Errno" not in json.dumps(got)
    assert "No space left on device" in caplog.text
    assert PushStore(q).notices() == [] and PushStore(q).record() == []


def test_a_malformed_record_beside_a_live_token_is_a_503_and_the_file_is_left(rig):
    src, q = rig
    tok = _mint(q)
    f = q / "webpush" / nt.FILE_NAME
    doc = json.loads(f.read_text())
    doc["tokens"].append({"name": "Broken", "source": "broken", "sha256": "0" * 64, "rate_per_min": "six"})
    f.write_text(json.dumps(doc))
    before = f.read_bytes()
    with _signed(src, q) as (port, _):
        st, got, _ = _post(port, {"body": "x"}, token=tok)
        assert st == 503 and got["error"] == "notify_unavailable"
        st, got, _h = _call(port, "GET", TOKENS, token=T)
        assert st == 503 and got["error"] == "notify_tokens_unreadable"      # never an empty or partial listing
    assert f.read_bytes() == before and PushStore(q).notices() == []


def test_a_store_failure_stores_nothing_and_answers_not_stored(rig, monkeypatch):
    import sqlite3

    src, q = rig
    tok = _mint(q)
    presence.set_word(q, "sleep")                       # a normal notice is held: a ping AND a held row would be written

    def boom(*_a, **_k):
        raise sqlite3.OperationalError("database or disk is full")

    with _signed(src, q) as (port, _):
        monkeypatch.setattr(PushStore, "_insert_notice", staticmethod(boom))
        st, got, _ = _post(port, {"body": "x"}, token=tok)
    assert st == 503 and got == {"error": "not_stored", "message": "the notice was not stored; try again"}
    s = PushStore(q)
    assert s.notices(include_dismissed=True) == [] and s.record() == [] and s.held_items() == []
    assert nt.list_public(q)[0]["sent_today"] == 1      # the known edge: the quota a store failure spent is not refunded
