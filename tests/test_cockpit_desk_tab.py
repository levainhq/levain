"""The Desk view's routes (levain#2 lane B), through the real ``levain serve`` envelope (make_server) on a loopback port.

The write gate is the point: ``POST /cockpit/desk/send`` files a line that wakes a desk, so it needs a named credential,
takes its owner from that credential and never from the body, refuses (and writes nothing) on a bad body, a conflicting
``outbox_id`` or words over the cap, and answers a retry from the port's stored claim. The port is the double
(:class:`ScriptedDeskPort`) for the gate and shape tests, and a :class:`HostCommandDeskPort` over fake argv scripts for the
end-to-end ones. No real flow script and no phone: the fake host is a model of the host, not the host."""
from __future__ import annotations

import http.client
import json
import os
import re
import sys
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager

import pytest

from levain.cockpit import desk_tab
from levain.desk import room_host, room_launch
from levain.desk.desk_port import (DESKCMD_ENV, DeskCaps, DeskCommandError, DeskCommands, HostCommandDeskPort,
                                   ScriptedDeskPort)
from levain.desk.room_host import HostCommands
from levain.web_server import _Handler, _build_desk_serve, make_server, run_web_server
from tests.test_cockpit_k2a import TOKEN, _install
from tests.test_cockpit_threads_mount import qdir  # noqa: F401  (fixture: the consent directory the server reads)

SEND = "/cockpit/desk/send"
READ = "/cockpit/desk.json"
THREAD = "th-0abcdef-a1b2c3"
H = "X-Levain-Write-Token"
IPAD = "ipad-surface-token"


def oid(n: int = 1) -> str:
    return f"desk-outbox-{n:04d}"


@contextmanager
def _serve(tmp_path, port, name="entity", **kw):
    _root, src, _ids = _install(tmp_path / name)
    kw.setdefault("write_token", TOKEN)
    httpd = make_server(src, host="127.0.0.1", port=0, desk_port=port, **kw)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _call(base, path, body=None, token=TOKEN, method="POST", ctype="application/json"):
    headers = {"Content-Type": ctype}
    if token is not None:
        headers[H] = token
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def _get(base, path, token=TOKEN):
    return _call(base, path, token=token, method="GET")


def _raw_post(base, path, raw: bytes, token=TOKEN):
    """A body that is not what ``_call`` would encode (not JSON at all)."""
    host, port = base[len("http://"):].split(":")
    c = http.client.HTTPConnection(host, int(port), timeout=15)
    try:
        c.request("POST", path, body=raw, headers={"Content-Type": "application/json", H: token})
        r = c.getresponse()
        return r.status, json.loads(r.read() or b"{}")
    finally:
        c.close()


def send(base, words="hello desk", outbox=None, token=TOKEN, **extra):
    return _call(base, SEND, {"words": words, "outbox_id": outbox or oid(), **extra}, token)


@pytest.fixture
def scripted():
    return ScriptedDeskPort()


# --- the gate ----------------------------------------------------------------------------------------------------------
def test_no_credential_or_a_wrong_one_is_refused_before_anything_is_asked_or_written(tmp_path, qdir, scripted):  # noqa: F811
    with _serve(tmp_path, scripted) as base:
        for token in (None, "wrong-token"):
            status, body = _get(base, READ, token)
            assert status == 403 and THREAD not in str(body) and "wired" not in body, (token, body)
            status, body = send(base, token=token)
            assert status == 403 and body["error"] == "credential_required", (token, body)
        status, body = _call(base, SEND, {"words": "x" * 3, "outbox_id": oid()}, ctype="text/plain")
        assert status in (400, 403, 415), status          # CSRF layer 2: JSON only
    assert scripted.posts == []


def test_a_credential_with_no_name_is_refused_with_its_own_words_and_nothing_is_written(tmp_path, qdir, scripted,  # noqa: F811
                                                                                         monkeypatch):
    """The in-route check, past do_POST's gate: whatever the gate admitted, a send with no named owner files nothing."""
    monkeypatch.setattr(_Handler, "_thread_owner", lambda self: None)
    with _serve(tmp_path, scripted) as base:
        status, body = send(base)
    assert status == 403 and body == {"error": "forbidden", "message": "a send needs a named credential"}, body
    assert scripted.posts == []


def test_the_owner_comes_from_the_credential_never_from_the_body(tmp_path, qdir, scripted):  # noqa: F811
    with _serve(tmp_path, scripted, surface_tokens={"ipad": IPAD}) as base:
        lies = {"owner": "cockpit:evil", "origin": "phone:evil", "author_surface": "cli", "client_id": "evil"}
        assert send(base, "from the browser", oid(1), **lies)[0] == 200
        assert send(base, "from the ipad", oid(2), token=IPAD, **lies)[0] == 200
    assert [(p.words, p.origin) for p in scripted.posts] == [("from the browser", "cockpit:browser"),
                                                              ("from the ipad", "cockpit:ipad")]


def test_a_paired_devices_token_files_as_that_device(tmp_path, qdir, scripted):  # noqa: F811
    from tests.test_cockpit_room_device_contract import OWNER_KEY, _call as _dcall, _pair

    _root, src, _ids = _install(tmp_path / "entity")
    httpd = make_server(src, host="127.0.0.1", port=0, surface_tokens={"browser": OWNER_KEY}, install_class="reported",
                        desk_port=scripted)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        device, tok = _pair(port)
        st, body = _dcall(port, "POST", SEND, {"words": "from the phone", "outbox_id": oid()}, token=tok)
        assert st == 200 and body["outcome"] == "accepted", body
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    assert [(p.words, p.origin) for p in scripted.posts] == [("from the phone", "phone:" + device)]


# --- not wired ---------------------------------------------------------------------------------------------------------
def test_a_server_with_no_port_says_wired_false_and_its_send_is_a_404(tmp_path, qdir):  # noqa: F811
    with _serve(tmp_path, None) as base:
        assert _get(base, READ) == (200, {"wired": False})
        status, body = send(base)
        assert status == 404, body
        assert _get(base, READ, token=None)[0] == 403        # whether this cockpit has a desk is not public


def test_make_server_refuses_a_port_with_no_surface_token_and_a_non_port(tmp_path, scripted):
    _root, src, _ids = _install(tmp_path / "ro")
    import dataclasses
    read_only = dataclasses.replace(src, write_scope=None)
    with pytest.raises(ValueError, match="refusing desk_port.*surface token"):
        make_server(read_only, host="127.0.0.1", port=0, desk_port=scripted)
    _root, src2, _ids = _install(tmp_path / "rw")
    with pytest.raises(ValueError, match="not a DeskPort"):
        make_server(src2, host="127.0.0.1", port=0, write_token=TOKEN, desk_port=object())


# --- the read ----------------------------------------------------------------------------------------------------------
def test_the_read_carries_the_ports_caps_lease_and_the_threads_receipts(tmp_path, qdir, scripted):  # noqa: F811
    scripted.script_lease("held", "desk 25", 180)
    scripted.script_event(THREAD, "script", "took", "took it", "10:42:01")
    scripted.script_event(THREAD, "desk", "reply", "on it", "10:43:00")
    with _serve(tmp_path, scripted) as base:
        status, cold = _get(base, READ)
        assert status == 200 and cold["wired"] is True, cold
        assert cold["caps"] == {"adapter": "scripted", "wake": "event", "receipts": True, "lease": True, "replies": True,
                                "words_max": 2000}
        assert cold["lease"] == {"state": "held", "holder": "desk 25", "age_s": 180}
        assert (cold["thread_id"], cold["events"], cold["cursor"], cold["readable"]) == (None, [], None, True)
        status, got = _get(base, f"{READ}?thread={THREAD}")
        assert [(e["by"], e["kind"], e["text"], e["at"]) for e in got["events"]] == [
            ("script", "took", "took it", "10:42:01"), ("desk", "reply", "on it", "10:43:00")]
        assert got["thread_id"] == THREAD and got["cursor"] == "2" and got["readable"] is True
        scripted.script_event(THREAD, "script", "held", "no lease", "10:44:00")
        status, more = _get(base, f"{READ}?thread={THREAD}&after={got['cursor']}")
        assert [e["kind"] for e in more["events"]] == ["held"] and more["cursor"] == "3"
        scripted.script_lease("none")
        assert _get(base, READ)[1]["lease"] == {"state": "none", "holder": None, "age_s": None}


def test_an_unreadable_thread_says_so_and_is_never_no_events_yet(tmp_path, qdir, scripted):  # noqa: F811
    scripted.script_unreadable()
    with _serve(tmp_path, scripted) as base:
        status, got = _get(base, f"{READ}?thread={THREAD}")
    assert status == 200 and got["readable"] is False and got["events"] == [] and got["cursor"] is None, got


def test_a_port_that_cannot_answer_a_poll_is_unknown_and_unreadable_never_a_500(tmp_path, qdir):  # noqa: F811
    class Broken(ScriptedDeskPort):
        def lease(self):
            raise RuntimeError("the host command exploded")

        def events(self, thread_id, after=None):
            raise RuntimeError("the host command exploded")

    with _serve(tmp_path, Broken()) as base:
        status, got = _get(base, f"{READ}?thread={THREAD}")
    assert status == 200 and got["lease"]["state"] == "unknown" and got["readable"] is False, got


@pytest.mark.parametrize("query", ["after=3", "thread=a&thread=b", "thread=a&after=1&after=2", "thread=a&since=1",
                                   "thread=" + "x" * 201])
def test_a_bad_read_query_is_a_400(tmp_path, qdir, scripted, query):  # noqa: F811
    with _serve(tmp_path, scripted) as base:
        status, body = _get(base, f"{READ}?{query}")
    assert status == 400 and body["error"] == "bad_request", body


def test_a_caps_less_port_draws_no_slots_it_does_not_have(tmp_path, qdir):  # noqa: F811
    port = ScriptedDeskPort(caps=DeskCaps("bare", "poll", False, False, False, 500))
    with _serve(tmp_path, port) as base:
        status, got = _get(base, f"{READ}?thread={THREAD}")
    assert got["caps"] == {"adapter": "bare", "wake": "poll", "receipts": False, "lease": False, "replies": False,
                           "words_max": 500}
    assert got["lease"]["state"] == "unknown" and got["readable"] is False, got


# --- the send ----------------------------------------------------------------------------------------------------------
def test_a_line_is_filed_and_the_answer_is_the_ports(tmp_path, qdir, scripted):  # noqa: F811
    scripted.script_post("accepted", ref="Filed in the inbox.", thread_id=THREAD)
    scripted.script_post("not_delivered", reason="The desk would not take that message.")
    scripted.script_post("ambiguous", reason="It may not have arrived.")
    with _serve(tmp_path, scripted) as base:
        assert send(base, "one", oid(1)) == (200, {"outcome": "accepted", "ref": "Filed in the inbox.", "thread_id": THREAD,
                                                   "reason": None, "replayed": False})
        assert send(base, "two", oid(2)) == (200, {"outcome": "not_delivered", "ref": None, "thread_id": None,
                                                   "reason": "The desk would not take that message.", "replayed": False})
        assert send(base, "three", oid(3)) == (200, {"outcome": "ambiguous", "ref": None, "thread_id": None,
                                                     "reason": "It may not have arrived.", "replayed": False})
    assert [p.words for p in scripted.posts] == ["one", "two", "three"]


def test_a_retry_of_the_same_outbox_id_is_answered_from_the_claim_and_filed_once(tmp_path, qdir, scripted):  # noqa: F811
    with _serve(tmp_path, scripted) as base:
        first = send(base, "once only", oid())
        again = send(base, "once only", oid())
    assert first[0] == again[0] == 200
    assert first[1]["replayed"] is False and again[1]["replayed"] is True
    assert {k: v for k, v in again[1].items() if k != "replayed"} == {k: v for k, v in first[1].items() if k != "replayed"}
    assert len(scripted.posts) == 1


def test_the_same_outbox_id_with_other_words_is_a_409_and_files_nothing(tmp_path, qdir, scripted):  # noqa: F811
    with _serve(tmp_path, scripted) as base:
        assert send(base, "the first words", oid())[0] == 200
        status, body = send(base, "different words", oid())
    assert status == 409 and body["error"] == "outbox_conflict", body
    assert [p.words for p in scripted.posts] == ["the first words"]


def test_words_over_the_cap_are_refused_whole_with_a_422_never_cut(tmp_path, qdir):  # noqa: F811
    port = ScriptedDeskPort(caps=DeskCaps("scripted", "event", True, True, True, 20))
    with _serve(tmp_path, port) as base:
        status, body = send(base, "x" * 21, oid(1))
        assert status == 422 and body["error"] == "words_over_cap" and body["words_max"] == 20, body
        assert "not sent or cut" in body["message"]
        assert port.posts == []
        assert send(base, "y" * 20, oid(2))[0] == 200          # exactly the cap goes whole
    assert [p.words for p in port.posts] == ["y" * 20]


@pytest.mark.parametrize("body", [
    [], "words", 7, False,
    {}, {"outbox_id": "desk-outbox-0001"}, {"words": "hi"},
    {"words": 5, "outbox_id": "desk-outbox-0001"}, {"words": "", "outbox_id": "desk-outbox-0001"},
    {"words": "   \n ", "outbox_id": "desk-outbox-0001"}, {"words": ["a"], "outbox_id": "desk-outbox-0001"},
    {"words": "hi", "outbox_id": 12345678}, {"words": "hi", "outbox_id": "short"}, {"words": "hi", "outbox_id": "-leading-dash1"},
    {"words": "hi", "outbox_id": "has space in it"}, {"words": "hi", "outbox_id": "a" * 129},
    {"words": "has a NUL \x00 in it", "outbox_id": "desk-outbox-0001"},
    {"words": "a lone surrogate \ud800", "outbox_id": "desk-outbox-0001"},
])
def test_a_bad_body_is_a_400_and_nothing_is_written(tmp_path, qdir, scripted, body):  # noqa: F811
    with _serve(tmp_path, scripted) as base:
        status, got = _call(base, SEND, body)
        assert status == 400 and got["error"] == "bad_request", (body, got)
    assert scripted.posts == []


def test_a_body_that_is_not_json_is_a_400_and_nothing_is_written(tmp_path, qdir, scripted):  # noqa: F811
    with _serve(tmp_path, scripted) as base:
        status, got = _raw_post(base, SEND, b"{not json")
        assert status == 400 and got["error"] == "bad_json", got
        status, got = _raw_post(base, SEND, b"null")          # JSON, but not an object
        assert status == 400 and got["error"] == "bad_request", got
    assert scripted.posts == []


def test_a_port_that_fails_before_it_answers_is_a_500_that_says_a_retry_files_once(tmp_path, qdir):  # noqa: F811
    class Failing(ScriptedDeskPort):
        def post(self, words, *, outbox_id, origin):
            raise OSError("claims file gone")

    with _serve(tmp_path, Failing()) as base:
        status, got = send(base)
    assert status == 500 and got["error"] == "send_failed" and "never files it twice" in got["message"], got


def test_no_response_says_fenced_read_or_acted_on(tmp_path, qdir, scripted):  # noqa: F811
    """The server cannot observe any of the three, so it must not say them (the design's section 7 'mock proves')."""
    scripted.script_lease("held", "desk 25", 5)
    scripted.script_post("accepted", ref="Filed in the inbox.", thread_id=THREAD)
    scripted.script_event(THREAD, "script", "took", "took it", "10:42:01")
    seen: list[str] = []
    with _serve(tmp_path, scripted, surface_tokens={"ipad": IPAD}) as base:
        for call in (lambda: _get(base, READ), lambda: _get(base, f"{READ}?thread={THREAD}"), lambda: _get(base, READ, None),
                     lambda: _get(base, READ + "?after=1"), lambda: send(base, "hi", oid(1)), lambda: send(base, "hi", oid(1)),
                     lambda: send(base, "other", oid(1)), lambda: send(base, "", oid(2)), lambda: send(base, "x" * 2001, oid(3)),
                     lambda: send(base, "hi", "short"), lambda: send(base, "hi", oid(4), token=None)):
            seen.append(json.dumps(call()[1]))
    text = " ".join(seen)
    assert re.search(r"\b(fenced|read|acted on)\b", text, re.I) is None, text
    assert "readable" in text            # the key says "readable", and the whole-word test above does not take it for "read"


# --- durable claims and the whole path over a host command --------------------------------------------------------------
FAKE = """\
import json, sys
mode, rec = sys.argv[1], sys.argv[2]
if mode == "post":
    text = sys.stdin.read()
    open(rec, "a").write(json.dumps({"argv": sys.argv[1:], "stdin": text}) + "\\n")
    if "FAIL" in text:
        sys.exit(1)
    if "REFUSE" in text:
        print("The desk takes 2000 characters at most.")
        sys.exit(2)
    print("Filed in the inbox. (thread th-0abcdef-a1b2c3)")
else:
    sys.stdout.write(open(sys.argv[3]).read())
"""


@pytest.fixture
def host(tmp_path):
    script = tmp_path / "fake_host.py"
    script.write_text(FAKE)
    rec = tmp_path / "rec.jsonl"
    lease = tmp_path / "lease.json"
    events = tmp_path / "events.json"
    lease.write_text(json.dumps({"state": "held", "holder": "desk 25", "age_s": 180}))
    events.write_text(json.dumps({"events": [{"by": "script", "kind": "took", "text": "took it", "at": "10:42:01"}],
                                  "cursor": "c1"}))
    py = sys.executable
    cmds = DeskCommands(post=(py, str(script), "post", str(rec)), lease=(py, str(script), "lease", str(rec), str(lease)),
                        events=(py, str(script), "events", str(rec), str(events)))

    class Host:
        pass
    h = Host()
    h.cmds, h.claims, h.lease, h.events = cmds, tmp_path / "state" / "claims.db", lease, events
    h.runs = lambda: [json.loads(x) for x in rec.read_text().splitlines()] if rec.exists() else []
    h.claims.parent.mkdir()
    return h


def test_end_to_end_over_a_host_command_the_whole_path(tmp_path, qdir, host):  # noqa: F811
    port = HostCommandDeskPort(host.cmds, host.claims)
    with _serve(tmp_path, port) as base:
        status, got = _get(base, READ)
        assert status == 200 and got["lease"] == {"state": "held", "holder": "desk 25", "age_s": 180}, got
        assert got["caps"]["adapter"] == "host-command" and got["caps"]["receipts"] is True
        status, sent = send(base, "a line for the desk", oid(1))
        assert status == 200 and sent["outcome"] == "accepted" and sent["thread_id"] == THREAD, sent
        assert sent["ref"] == "Filed in the inbox. (thread th-0abcdef-a1b2c3)" and sent["replayed"] is False
        status, ev = _get(base, f"{READ}?thread={sent['thread_id']}")
        assert [(e["kind"], e["text"]) for e in ev["events"]] == [("took", "took it")] and ev["cursor"] == "c1", ev
        status, refused = send(base, "REFUSE this", oid(2))
        assert status == 200 and refused["outcome"] == "not_delivered" and "2000" in refused["reason"], refused
        status, failed = send(base, "FAIL this", oid(3))
        assert status == 200 and failed["outcome"] == "ambiguous" and "not sent again" in failed["reason"], failed
        again = send(base, "FAIL this", oid(3))[1]
        assert again["outcome"] == "ambiguous" and again["replayed"] is True
    posts = [r for r in host.runs() if r["argv"][0] == "post"]
    assert [r["stdin"] for r in posts] == ["a line for the desk", "REFUSE this", "FAIL this"]       # words on stdin, run once each
    assert all(len(r["argv"]) == 2 for r in posts)       # nothing appended to the host's argv: not the words, not the owner


def test_a_restart_keeps_the_claim_so_a_retry_is_never_a_second_filing(tmp_path, qdir, host):  # noqa: F811
    with _serve(tmp_path, HostCommandDeskPort(host.cmds, host.claims), name="first") as base:
        assert send(base, "before the restart", oid())[1]["replayed"] is False
    with _serve(tmp_path, HostCommandDeskPort(host.cmds, host.claims), name="second") as base:       # a new port, a new server
        status, body = send(base, "before the restart", oid())
        assert status == 200 and body["outcome"] == "accepted" and body["replayed"] is True, body
        assert send(base, "other words", oid())[0] == 409
    assert len([r for r in host.runs() if r["argv"][0] == "post"]) == 1


def test_a_port_with_no_post_command_files_nothing_and_says_so(tmp_path, qdir, host):  # noqa: F811
    port = HostCommandDeskPort(DeskCommands(lease=host.cmds.lease), host.claims)
    with _serve(tmp_path, port) as base:
        status, body = send(base)
    assert status == 200 and body["outcome"] == "not_delivered" and "no desk wired" in body["reason"], body
    assert host.runs() == [] or all(r["argv"][0] != "post" for r in host.runs())


# --- where the port comes from -------------------------------------------------------------------------------------------
def test_the_port_is_built_from_the_env_with_a_durable_claims_file_under_the_queue(tmp_path, qdir, host, monkeypatch):  # noqa: F811
    monkeypatch.delenv(DESKCMD_ENV, raising=False)
    assert _build_desk_serve() is None
    monkeypatch.setenv(DESKCMD_ENV, "")
    assert _build_desk_serve() is None
    monkeypatch.setenv(DESKCMD_ENV, host.cmds.to_json())
    port = _build_desk_serve()
    claims = qdir / desk_tab.CLAIMS_DIR / desk_tab.CLAIMS_NAME
    assert claims.is_file() and (claims.stat().st_mode & 0o777) == 0o600, claims
    assert port.capabilities().adapter == "host-command"
    with _serve(tmp_path, port) as base:
        assert send(base, "kept", oid())[1]["replayed"] is False
    again = _build_desk_serve()        # a "restart": a new port over the same environment finds the same claim
    assert again.post("kept", outbox_id=oid(), origin="cockpit:browser").replayed is True
    assert len([r for r in host.runs() if r["argv"][0] == "post"]) == 1


@pytest.mark.parametrize("value", ["not json", "[]", json.dumps({"relay": ["/bin/true"]}),
                                   json.dumps({"post": ["relative"]}), json.dumps({"post": ["/bin/true"], "words_max": 0})])
def test_a_malformed_env_value_refuses_and_a_room_key_is_never_accepted(qdir, value):  # noqa: F811
    with pytest.raises(DeskCommandError):
        _build_desk_serve({**os.environ, DESKCMD_ENV: value})


def test_a_malformed_env_refuses_the_serve_before_it_binds(tmp_path, qdir, monkeypatch, capsys):  # noqa: F811
    root, _src, _ids = _install(tmp_path / "entity")
    monkeypatch.setenv(DESKCMD_ENV, "not json")
    assert run_web_server(root, write=True, open_browser=False, port=0) == 1
    assert f"refusing {DESKCMD_ENV}" in capsys.readouterr().err


# --- the model-free line ------------------------------------------------------------------------------------------------
def test_the_desk_argvs_are_not_in_the_rooms_model_environment(qdir, host, monkeypatch):  # noqa: F811
    """The serve's own environment carries the desk commands (that is how it learns them); the room's child gets an
    allowlisted one, and the room door's config names the Room's host commands only."""
    desk_json = host.cmds.to_json()
    monkeypatch.setenv(DESKCMD_ENV, desk_json)
    child = room_launch.room_env(os.environ)
    assert DESKCMD_ENV not in child and desk_json not in "".join(child.values())
    assert DESKCMD_ENV not in room_launch.ENV_KEEP and not DESKCMD_ENV.startswith(room_launch.ENV_KEEP_PREFIXES)
    door = room_launch.mcp_config_json("ln-" + "0" * 32, sys.executable, HostCommands(relay=("/bin/true",)))
    distinctive = [w for w in host.cmds.post + host.cmds.lease + host.cmds.events
                   if os.path.isabs(w) and w != sys.executable]
    assert distinctive and all(w not in door for w in distinctive), door
    # and neither side's from_env takes the other's keys
    with pytest.raises(room_host.HostCommandError):
        room_host.from_env(desk_json)
    with pytest.raises(DeskCommandError):
        DeskCommands.from_env(json.dumps({"relay": ["/bin/true"]}))
