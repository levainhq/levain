"""1027+18 lane idg1fx: the identity gate G1's fix round 1 (the L3 of d7934e4e, 1027-18/idg1/L3_r1.txt). Each test
names the finding it closes (X1-X11 in 1027-18/idg1fx/BRIEF.md).
"""
from __future__ import annotations

import http.server
import io
import json
import os
import select
import threading
import time
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import identity as ID
from levain.slot import identity_child as IC
from levain.slot import supervisor as SV
from levain.slot.runtime_grammar import OPERATOR, RuntimeRequest
from tests.test_slot_idg1_identity import E0, _baseline, _login, env  # noqa: F401 - env is a fixture
from tests.vendor_tree import vendor_installed, vendor_prefix  # noqa: F401 - autouse, the real vendor check

UUID = "4f6c1a2e-0b7d-4c9e-8a31-2d5e6f708192"
S = "s-0000000000000001"
#: a proxy the tests' plain-http fake servers never go through (ProxyHandler maps only https to it): X2 requires one.
PROXY = {"HTTPS_PROXY": "http://127.0.0.1:9"}


# --- fake servers ------------------------------------------------------------------------------------------------------

def _server(handler_cls):
    srv = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


class _Sink(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        type(self).seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, *a):
        pass


class _Redirect(http.server.BaseHTTPRequestHandler):
    location = ""
    seen: list = []

    def do_GET(self):
        type(self).seen.append(self.path)
        self.send_response(302)
        self.send_header("Location", type(self).location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *a):
        pass


class _Body(http.server.BaseHTTPRequestHandler):
    body = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", str(len(type(self).body)))
        self.end_headers()
        self.wfile.write(type(self).body)

    def log_message(self, *a):
        pass


@pytest.fixture
def sink():
    _Sink.seen = []
    srv = _server(_Sink)
    yield srv
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def redirector(sink):
    _Redirect.seen = []
    _Redirect.location = f"http://127.0.0.1:{sink.server_port}/stolen"
    srv = _server(_Redirect)
    yield srv
    srv.shutdown()
    srv.server_close()


def _creds(tmp_path, obj=None, *, proxy=True):
    cfg = tmp_path / "claude"
    cfg.mkdir(exist_ok=True)
    (cfg / ".credentials.json").write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": "tok-SECRET-123"}} if obj is None else obj))
    return {"CLAUDE_CONFIG_DIR": str(cfg), **(PROXY if proxy else {})}


def _url(srv, path="/api/oauth/profile"):
    return f"http://127.0.0.1:{srv.server_port}{path}"


def _allow(monkeypatch, srv, scheme="http"):
    """The tests' fake profile server stands in for the one origin the child sends the bearer to."""
    monkeypatch.setattr(IC, "ORIGIN", (scheme, "127.0.0.1", srv.server_port), raising=False)


def _main(env, url):
    out = io.StringIO()
    real = IC.fetch
    IC.fetch = lambda e, **kw: real(e, url=url)
    try:
        code = IC.main(env=env, out=out)
    finally:
        IC.fetch = real
    return code, out.getvalue()


# --- X1: no redirect is followed, and the bearer goes to one origin only -----------------------------------------------

def test_x1_a_redirect_is_the_final_answer_and_the_second_origin_receives_nothing(tmp_path, redirector, sink,
                                                                                  monkeypatch):
    """codex HIGH 1: a 302 to another origin is answered as the final non-200 status; the bearer is never resent."""
    _allow(monkeypatch, redirector)
    ans = IC.fetch(_creds(tmp_path), url=_url(redirector))
    assert _Redirect.seen == ["/api/oauth/profile"]
    assert _Sink.seen == []
    assert ans == {"status": 302, "body": ""}


def test_x1_a_same_origin_redirect_is_not_followed_either(tmp_path, redirector, monkeypatch):
    _Redirect.location = _url(redirector, "/again")
    _allow(monkeypatch, redirector)
    assert IC.fetch(_creds(tmp_path), url=_url(redirector)) == {"status": 302, "body": ""}
    assert _Redirect.seen == ["/api/oauth/profile"]


def test_x1_the_bearer_goes_only_to_the_profile_origin(tmp_path, sink):
    """No ORIGIN override: an http://127.0.0.1 url is not https://api.anthropic.com, so nothing is sent."""
    assert IC.fetch(_creds(tmp_path), url=_url(sink)) == {"error": "bad-origin"}
    assert _Sink.seen == []


def test_x1_the_profile_url_is_the_origin():
    assert IC.ORIGIN == ("https", "api.anthropic.com", 443)
    assert IC._origin(IC.PROFILE_URL) == IC.ORIGIN


# --- X2: the tunnel or nothing -----------------------------------------------------------------------------------------

@pytest.mark.parametrize("proxy", [None, "", "   "])
def test_x2_no_proxy_no_request(tmp_path, sink, monkeypatch, proxy):
    """codex MED 3 / complement LOW 2: no HTTPS_PROXY (or an empty one) is ``no-proxy``, and no request is made."""
    _allow(monkeypatch, sink)
    env = _creds(tmp_path, proxy=False)
    if proxy is not None:
        env["HTTPS_PROXY"] = proxy
    assert IC.fetch(env, url=_url(sink)) == {"error": "no-proxy"}
    assert _Sink.seen == []


def test_x2_a_malformed_proxy_is_request_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(IC, "ORIGIN", ("https", "127.0.0.1", 9), raising=False)
    env = {**_creds(tmp_path, proxy=False), "HTTPS_PROXY": "http://[::1"}
    assert IC.fetch(env, url="https://127.0.0.1:9/api/oauth/profile") == {"error": "request-failed"}


def test_x2_an_opener_that_cannot_be_built_is_request_failed(tmp_path, sink, monkeypatch):
    """deepseek LOW 4 related: the opener is built inside fetch's try."""
    _allow(monkeypatch, sink)

    def broken(*a, **kw):
        raise ValueError("bad proxy")
    monkeypatch.setattr(IC.urllib.request, "ProxyHandler", broken)
    assert IC.fetch(_creds(tmp_path), url=_url(sink)) == {"error": "request-failed"}


# --- X3: a credential of the wrong shape is no-token -------------------------------------------------------------------

@pytest.mark.parametrize("obj", [{"claudeAiOauth": None}, {"claudeAiOauth": []}, {"claudeAiOauth": "x"},
                                 {"claudeAiOauth": 7}, {"claudeAiOauth": {"accessToken": 7}},
                                 {"claudeAiOauth": {"accessToken": ""}}, {"claudeAiOauth": {}}, [], "x"])
def test_x3_a_credential_of_the_wrong_shape_is_no_token(tmp_path, sink, monkeypatch, obj):
    """codex MED 5 / deepseek MED 1: never a traceback; the fixed ``no-token`` line, exit 1, no request."""
    _allow(monkeypatch, sink)
    code, out = _main(_creds(tmp_path, obj), _url(sink))
    assert (code, json.loads(out)) == (1, {"error": "no-token"})
    assert _Sink.seen == []


# --- X4: root's parser is total ----------------------------------------------------------------------------------------

DEEP = "[" * 200_000


@pytest.mark.parametrize("out", [DEEP.encode()[:60_000],
                                 json.dumps({"status": 200, "body": DEEP[:60_000]}).encode()], ids=["line", "body"])
def test_x4_deep_nesting_is_unparsable_not_a_recursion_error(out):
    """complement MED 1: RecursionError on either json.loads is ``identity-unparsable``."""
    assert len(out) <= ID.PROFILE_ANSWER_MAX
    with pytest.raises(Refused) as e:
        ID.parse_profile_answer(0, out)
    assert e.value.name == "identity-unparsable"


# --- X5: the child's line is ASCII ---------------------------------------------------------------------------------------

def test_x5_the_child_line_is_ascii(tmp_path, monkeypatch):
    """complement LOW 4: a non-ASCII display name never meets a non-UTF-8 stdout."""
    _Body.body = json.dumps({"account": {"uuid": UUID, "display_name": "Zoë ☃"}}, ensure_ascii=False).encode()
    srv = _server(_Body)
    try:
        _allow(monkeypatch, srv)
        code, out = _main(_creds(tmp_path), _url(srv))
    finally:
        srv.shutdown()
        srv.server_close()
    out.encode("ascii")
    assert ID.parse_profile_answer(code, out.encode("ascii")) == UUID


# --- X9: no assert on the answer -----------------------------------------------------------------------------------------

def test_x9_an_answer_outside_yes_no_is_refused_by_name(env):
    """deepseek LOW 3: an explicit refusal, never an assert (stripped under -O, the yes path would run)."""
    w, la, _ = env
    s = _login(w, la, "codex")
    ctx = None                                 # refused before any read: no context is needed
    req = RuntimeRequest("confirm-identity", OPERATOR, session=s, answer="maybe")
    with pytest.raises(Refused) as e:
        ID.confirm_identity(ctx, req, None)
    assert e.value.name == "bad-answer"
    assert not _baseline(w).exists()
    with pytest.raises(Refused) as e:
        ID.confirm_identity(ctx, RuntimeRequest("confirm-identity", OPERATOR, session=None, answer="yes"), None)
    assert e.value.name == "no-session"


# --- X11: a torn slot-state file ------------------------------------------------------------------------------------------

def test_x11_a_torn_slot_state_is_slot_state_malformed(env):
    """deepseek LOW 5: the field-only CAS precheck refuses with the one slot-state name, slot-state-malformed; nothing is written."""
    w, la, _ = env
    s = _login(w, la, "codex")
    p = E0.slot_dir(w.L) / "state"
    p.write_bytes(p.read_bytes()[:20])
    assert w.main(["confirm-identity", s, "yes"])[1]["refused"] == "slot-state-malformed"
    assert not _baseline(w).exists()


# --- X6: the root-sourced identity read ------------------------------------------------------------------------------------

def test_x6_status_answers_the_records_identity(env):
    """``status <session>`` carries CONTROL's ``identity`` field, which the slot cannot write."""
    w, la, _ = env
    s = _login(w, la, "codex")
    line = w.main(["status", s])[1]
    assert line["identity"] == {"observed_id": "not-a-check"}
    assert line["records"][0]["session"] == s


def test_x6_status_answers_a_claude_logins_result(env):
    w, la, _ = env
    s = _login(w, la, "claude")
    assert w.main(["status", s])[1]["identity"] == {"result": "identity-call-unbuilt"}


def test_x6_status_with_no_identity_has_no_key(env):
    w, _, _ = env
    s = w.prepare()["session"]
    line = w.main(["status", s])[1]
    assert "identity" not in line
    assert "identity" not in w.main(["status"])[1]


class _Sup(SV.Supervisor):
    """The call site with the helper faked per verb (``replies[verb]``: a dict answer or a Refused)."""

    def __init__(self, vendor: str, answer, identity=None):
        super().__init__(SV.Request(vendor=vendor, repo=Path("/nonexistent"), auth="passthrough", kind="login"),
                         SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                  base_map=Path("/b"), client_factory=lambda p: None,
                                  confirm=lambda prompt: (self.prompts.append(prompt), answer)[1]))
        self.prompts: list = []
        self.calls: list = []
        self.said: list = []
        self.replies: dict = {"status": {"records": [], **({} if identity is None else {"identity": identity})},
                              "confirm-identity": {"baseline": "written"}}

    def helper(self, *args, stdin=b"", stoppable=True):
        self.calls.append(args)
        r = self.replies[args[0]]
        if isinstance(r, Refused):
            raise r
        return r

    def say(self, msg):
        self.said.append(msg)


def test_x6_a_forged_tail_exit_does_not_skip_the_prompt():
    """codex MED 2: the pty tail can say exit 1 after a real exit 0; root's record says the login observed an id."""
    sup = _Sup("codex", True, {"observed_id": "not-a-check"})
    sup._vendor_exit = 1                       # what a forged tail line would have set
    sup._confirm_identity(S)
    assert sup.calls == [("status", S), ("confirm-identity", S, "yes")]
    assert "not a check" in sup.prompts[0]


def test_x6_a_forged_tail_exit_zero_does_not_make_a_prompt():
    sup = _Sup("codex", True, {"result": "login-exit-nonzero"})
    sup._vendor_exit = 0
    sup._confirm_identity(S)
    assert sup.prompts == [] and ("confirm-identity", S, "yes") not in sup.calls
    assert any("login-exit-nonzero" in m for m in sup.said)


def test_x6_the_prompt_shows_the_observed_id_from_root():
    """RULINGS G1b's root-sourced id display: a Claude login with an observed id is asked with that id shown."""
    sup = _Sup("claude", True, {"observed_id": UUID})
    sup._confirm_identity(S)
    assert UUID in sup.prompts[0]
    assert sup.calls[-1] == ("confirm-identity", S, "yes")


def test_x6_no_identity_in_the_record_is_not_asked():
    for ident in (None, {"result": "identity-call-unbuilt"}):
        sup = _Sup("claude", True, ident)
        sup._confirm_identity(S)
        assert sup.prompts == [] and [c[0] for c in sup.calls] == ["status"]


def test_x6_a_refused_or_malformed_status_read_is_not_asked():
    for reply in (Refused("no-session", "x"), {"records": [], "identity": {"observed_id": "a b"}},
                  {"records": [], "identity": "not-a-check"}):
        sup = _Sup("codex", True)
        sup.replies["status"] = reply
        sup._confirm_identity(S)
        assert sup.prompts == [] and [c[0] for c in sup.calls] == ["status"]
        assert any("no baseline written" in m for m in sup.said)


# --- X8: "baseline written" only on root's answer -------------------------------------------------------------------------

def test_x8_baseline_written_only_on_roots_written_answer():
    """deepseek MED 2: an answer that does not say root wrote it is never reported as written."""
    sup = _Sup("codex", True, {"observed_id": "not-a-check"})
    sup.replies["confirm-identity"] = {}
    sup._confirm_identity(S)
    assert not any(m.startswith("baseline written") for m in sup.said)
    sup = _Sup("codex", True, {"observed_id": "not-a-check"})
    sup.replies["confirm-identity"] = Refused("lease-dead", "x")
    sup._confirm_identity(S)
    assert "no baseline written: refused lease-dead" in sup.said


def test_x8_confirm_yes_answers_written(env):
    w, la, _ = env
    s = _login(w, la, "codex")
    assert w.main(["confirm-identity", s, "yes"])[1] == {"ok": True, "baseline": "written"}
    assert canon.parse(_baseline(w).read_bytes())["session"] == s


# --- X7: the prompt write cannot hold the lease ---------------------------------------------------------------------------

def test_x7_a_stalled_stderr_does_not_hold_the_prompt(tmp_path, monkeypatch):
    """codex MED 4: stderr's pipe is full and nobody reads it; the prompt returns no answer within the deadline (which
    starts before the write) instead of blocking teardown forever."""
    monkeypatch.setattr(SV, "CONFIRM_S", 0.5)
    r, wfd = os.pipe()
    os.set_blocking(wfd, False)
    try:
        while True:
            os.write(wfd, b"x" * 4096)
    except BlockingIOError:
        pass
    os.set_blocking(wfd, True)
    err = os.fdopen(wfd, "w")
    master, slave = os.openpty()
    got: list = []
    try:
        sup = SV.Supervisor(SV.Request(vendor="codex", repo=tmp_path, auth="passthrough", kind="login"),
                            SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                     base_map=Path("/b"), client_factory=lambda p: None, stdin=slave, err=err))
        t = threading.Thread(target=lambda: got.append(sup._ask("q? ")), daemon=True)
        t0 = time.monotonic()
        t.start()
        t.join(5)
        stuck = t.is_alive()
        if stuck:                              # free the blocked write so the thread ends
            os.set_blocking(r, False)
            try:
                while True:
                    os.read(r, 1 << 16)
            except BlockingIOError:
                pass
            t.join(5)
        assert not stuck, "the prompt write blocked past its deadline"
        assert got == [None] and time.monotonic() - t0 < 3
    finally:
        os.close(master)
        os.close(slave)
        os.close(r)
        try:
            err.close()
        except OSError:
            pass


# --- X10: a long line leaves nothing behind ---------------------------------------------------------------------------------

def test_x10_a_long_line_is_no_and_its_rest_is_flushed(tmp_path):
    """complement LOW 6: past the line cap the answer is no and the rest of the line does not reach his shell."""
    master, slave = os.openpty()
    try:
        sup = SV.Supervisor(SV.Request(vendor="codex", repo=tmp_path, auth="passthrough", kind="login"),
                            SV.Seams(host="linux", helper=("helper",), store=Path("/L"), control_socket=Path("/c"),
                                     base_map=Path("/b"), client_factory=lambda p: None, stdin=slave,
                                     err=io.StringIO()))
        os.write(master, b"y" * 200 + b"\n")
        assert sup._ask("q? ") is False
        assert select.select([slave], [], [], 0.3)[0] == []
    finally:
        os.close(master)
        os.close(slave)
