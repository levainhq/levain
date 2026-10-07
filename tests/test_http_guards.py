"""spore-1013: the request guards live once, in levain.http_guards, and all three servers use them.

Before the extraction, `levain serve`, `levain init --web` and `levain docs` each carried a copy of
the guard code, and a test compared two of the copies' ASTs to catch drift. The copies had already
drifted where nothing compared them: `init_server` and `docs_server` stamped the security headers in
`end_headers`, `web_server` only in `_send`, so `levain serve` answered an OPTIONS or PUT with a 501
that carried no CSP, no nosniff and no X-Frame-Options (reproduced on 437249e).

These tests pin the structure (no server defines its own copy of a guard) and the behaviour (the
headers on a framework-generated response, on every server; a change to the shared cross-site check
reaches all three).
"""
from __future__ import annotations

import http.client
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from levain.http_guards import GuardedHandler

# Derived, so a guard added to the base later is covered too (L3 2026-10-03). `_route` and `_post` are the
# methods a server defines for itself; `do_GET` / `do_HEAD` / `do_POST` are guards like the rest, so a server that
# defined its own would be routing around them.
GUARD_METHODS = tuple(
    name for name, value in vars(GuardedHandler).items()
    if callable(value) and not name.startswith("__") and name not in ("_route", "_post")
)

_TOKEN = "test-launch-token-guards"

SECURITY_HEADERS = ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options",
                    "Cache-Control", "Referrer-Policy")


# The team view's handler still routes its own do_GET (the launch token for `levain team view` lands with the team
# lane, after this one). Strict, so the day it moves onto `_route` this marker fails and has to be removed.
_NOT_YET_ON_THE_SHARED_GUARDS = {"_ViewHandler": "team view gate lands with seat/1007-5-teamview"}


def _handlers():
    """Every GuardedHandler subclass in the package, found by walking the class tree (L1 2026-10-07: a hand-written
    list of three missed the fourth server, whose own do_GET skipped the launch token)."""
    import importlib
    from pathlib import Path

    import levain

    # Import only the modules that define a handler, found by reading the source, so collecting this test does not
    # import every package in levain (codex L3 r2: walk_packages imports each package to find its children).
    root = Path(levain.__file__).parent
    for py in root.rglob("*.py"):
        if "GuardedHandler)" in py.read_text(encoding="utf-8"):
            importlib.import_module("levain." + ".".join(py.relative_to(root).with_suffix("").parts))
    found, todo = [], [GuardedHandler]
    while todo:
        for sub in todo.pop().__subclasses__():
            if sub.__module__.startswith("levain.") and sub not in found:
                found.append(sub)
                todo.append(sub)
    assert len(found) >= 3
    return [pytest.param(c, id=c.__name__, marks=pytest.mark.xfail(strict=True, reason=_NOT_YET_ON_THE_SHARED_GUARDS[c.__name__]))
            if c.__name__ in _NOT_YET_ON_THE_SHARED_GUARDS else pytest.param(c, id=c.__name__) for c in found]


@pytest.mark.parametrize("cls", _handlers())
def test_every_server_inherits_the_guards_and_copies_none(cls):
    assert issubclass(cls, GuardedHandler)
    assert {"_refuse_read", "_refuse_oversize", "_declared_length", "_refuse_untokened_read",
            "_refuse_untokened_write", "do_GET", "do_HEAD", "do_POST"} <= set(GUARD_METHODS)
    copied = [name for name in GUARD_METHODS if name in cls.__dict__]
    assert copied == [], f"{cls.__name__} defines its own {copied}; the shared copy is the only one"


@contextmanager
def _server(kind: str, tmp_path: Path):
    if kind == "serve":
        from levain.dashboard import AnnealPaths, SubstrateSource
        from levain.web_server import make_server

        httpd = make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")),
                            host="127.0.0.1", port=0, read_token=_TOKEN)
    elif kind == "init":
        from levain.init_server import make_init_server

        httpd = make_init_server(tmp_path / "install", port=0, launch_token=_TOKEN)
    else:
        from levain.docs_server import make_docs_server

        httpd = make_docs_server(tmp_path, port=0, launch_token=_TOKEN)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _request(port: int, method: str, path: str = "/", headers: dict | None = None, *,
             token: str | None = _TOKEN, body: bool = False):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    h = {"Host": "127.0.0.1", **({"X-Levain-Token": token} if token is not None else {}), **(headers or {})}
    c.request(method, path, headers=h)
    r = c.getresponse()
    data = r.read()
    c.close()
    if body:
        return r.status, dict(r.getheaders()), data
    return r.status, dict(r.getheaders())


@pytest.mark.parametrize("kind", ["serve", "init", "docs"])
@pytest.mark.parametrize("method", ["OPTIONS", "PUT", "DELETE"])
def test_a_framework_generated_response_carries_the_security_headers(tmp_path, kind, method):
    """The reproduced drift: `levain serve` answered these with a bare 501."""
    with _server(kind, tmp_path) as port:
        status, headers = _request(port, method)
    assert status == 501
    missing = [h for h in SECURITY_HEADERS if h not in headers]
    assert missing == [], f"{kind} {method} -> 501 without {missing}"


@pytest.mark.parametrize("kind", ["serve", "init", "docs"])
def test_a_change_to_the_shared_cross_site_check_reaches_every_server(tmp_path, kind, monkeypatch):
    """One mutant in the shared code turns all three red (the spore's acceptance test)."""
    with _server(kind, tmp_path) as port:
        assert _request(port, "GET", headers={"Sec-Fetch-Site": "same-origin"})[0] == 200
        monkeypatch.setattr(GuardedHandler, "_cross_site_read", lambda self: True)
        assert _request(port, "GET", headers={"Sec-Fetch-Site": "same-origin"})[0] == 403


@pytest.mark.parametrize("kind", ["serve", "init", "docs"])
def test_each_security_header_appears_once_on_a_normal_response(tmp_path, kind):
    """`_send` stopped stamping them when `end_headers` started to; a header sent twice is the
    sign of a server that kept a copy of the old `_send`."""
    with _server(kind, tmp_path) as port:
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        c.request("GET", "/", headers={"Host": "127.0.0.1"})
        r = c.getresponse()
        r.read()
        names = [k for k, _ in r.getheaders()]
        c.close()
    for h in SECURITY_HEADERS:
        assert names.count(h) == 1, f"{kind}: {h} sent {names.count(h)} times"


WRITE_ROUTES = {"serve": "/edit", "init": "/init"}


def _raw_post(port: int, path: str, headers: dict, body: bytes = b""):
    """A POST whose headers are exactly ``headers`` plus Host (no automatic Content-Length)."""
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("POST", path, skip_host=True, skip_accept_encoding=True)
    for k, v in {"Host": "127.0.0.1", "X-Levain-Token": _TOKEN, **headers}.items():
        c.putheader(k, v)
    c.endheaders()
    if body:
        c.send(body)
    r = c.getresponse()
    data = r.read()
    out = (r.status, dict(r.getheaders()), data)
    c.close()
    return out


@pytest.mark.parametrize("kind", ["serve", "init"])
@pytest.mark.parametrize("headers,status", [
    ({"Host": "evil.example", "Content-Type": "application/json", "Content-Length": "2"}, 403),
    ({"Sec-Fetch-Site": "cross-site", "Content-Type": "application/json", "Content-Length": "2"}, 403),
    ({"Sec-Fetch-Site": "same-site", "Content-Type": "application/json", "Content-Length": "2"}, 403),
    ({"Content-Type": "text/plain", "Content-Length": "2"}, 415),
    ({"Content-Type": "application/json"}, 411),
    ({"Content-Type": "application/json", "Content-Length": "²".encode().decode("latin-1")}, 411),
    ({"Content-Type": "application/json", "Content-Length": "-1"}, 411),
    ({"Content-Type": "application/json", "Content-Length": str(10**12)}, 413),
    ({"Content-Type": "application/json", "Content-Length": "9" * 5000}, 411),
], ids=["bad-host", "cross-site", "same-site", "text-plain", "no-length", "superscript-length",
        "negative-length", "oversize", "too-many-digits"])
def test_every_write_server_runs_the_shared_write_preamble(tmp_path, kind, headers, status):
    """L1 2026-10-03 MED-1 (run): with either server's Content-Length guard swapped for a bare int(),
    every existing test still passed. Each refusal is now driven through BOTH write servers, live,
    and every refusal before the body is read closes the connection."""
    import sys

    # The too-many-digits case depends on Python's int-conversion limit; pin the default so the
    # outcome does not depend on PYTHONINTMAXSTRDIGITS in the environment (L3 r2).
    old_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with _server(kind, tmp_path) as port:
            # No body: a refused request's unread bytes can make the kernel reset the socket before
            # the response is read (complement L3).
            got, resp_headers, _ = _raw_post(port, WRITE_ROUTES[kind], headers)
    finally:
        sys.set_int_max_str_digits(old_limit)
    assert got == status, f"{kind}: {headers} -> {got}"
    assert resp_headers.get("Connection") == "close"


# --- np-ebb8a399 (codex HIGH f5d5e483a0c7e3a8): the launch token ------------------------------------------------
# Reproduced 2026-10-07 on 00b0604: `levain serve` answered GET /substrate.json with 200 and the seed's contents to a
# caller holding no token, so another OS user, a container reaching host loopback or a sandboxed app with a network
# entitlement could read the operator's memory. Each test below fails on that code.

DATA_ROUTES = {"serve": "/substrate.json", "init": "/init-plan.json", "docs": "/docs.json"}


@pytest.mark.parametrize("kind", ["serve", "init", "docs"])
@pytest.mark.parametrize("token", [None, "wrong-token", ""])
def test_a_data_route_is_refused_without_the_launch_token(tmp_path, kind, token):
    import json

    with _server(kind, tmp_path) as port:
        status, headers, data = _request(port, "GET", DATA_ROUTES[kind], token=token, body=True)
        assert status == 403 and json.loads(data)["error"] == "launch_token"
        assert headers["Content-Type"].startswith("application/json")
        assert _request(port, "GET", DATA_ROUTES[kind])[0] == 200


@pytest.mark.parametrize("kind", ["serve", "init", "docs"])
def test_only_the_page_shell_is_token_free_and_an_unknown_path_is_refused_not_404(tmp_path, kind):
    """The shell (the page and its scripts) carries no operator data; everything else needs the token, including
    a path that does not exist, so a caller without it cannot map the routes."""
    with _server(kind, tmp_path) as port:
        assert _request(port, "GET", "/", token=None)[0] == 200
        assert _request(port, "GET", "/token.js", token=None)[0] == 200
        assert _request(port, "GET", "/no-such-route", token=None)[0] == 403
        assert _request(port, "GET", "/no-such-route")[0] == 404
        assert _request(port, "HEAD", DATA_ROUTES[kind], token=None)[0] == 403


@pytest.mark.parametrize("kind", ["serve", "init"])
def test_a_write_is_refused_without_the_launch_token_before_its_body_is_read(tmp_path, kind):
    with _server(kind, tmp_path) as port:
        got, headers, data = _raw_post(port, WRITE_ROUTES[kind], {
            "X-Levain-Token": "wrong", "Content-Type": "application/json", "Content-Length": "2"})
    assert got == 403 and b"launch_token" in data
    assert headers.get("Connection") == "close"


def test_the_old_chat_header_still_carries_the_launch_token(tmp_path):
    with _server("serve", tmp_path) as port:
        assert _request(port, "GET", "/substrate.json", token=None,
                        headers={"X-Levain-Chat-Token": _TOKEN})[0] == 200


def test_a_route_a_server_adds_later_is_refused_by_default(tmp_path):
    """Deny by default: a handler that serves EVERY path, with no token logic of its own, still refuses all but its
    token-free paths, because GuardedHandler runs the gate before `_route` is reached."""
    from http.server import ThreadingHTTPServer

    class _Open(GuardedHandler):
        def _route(self, *, head: bool) -> None:
            self._send(b"operator data\n", "text/plain", head=head)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Open)
    httpd.allowed_hosts = frozenset({"127.0.0.1"})
    httpd.launch_token = _TOKEN
    httpd.token_free_paths = frozenset({"/"})
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        assert _request(port, "GET", "/added-later", token=None)[0] == 403
        assert _request(port, "GET", "/added-later")[0] == 200
        assert _request(port, "GET", "/", token=None)[0] == 200
        got, _h, _d = _raw_post(port, "/added-later", {"X-Levain-Token": "", "Content-Length": "0"})
        assert got == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


@pytest.mark.parametrize("kind", ["init", "docs"])
def test_init_and_docs_always_run_with_a_fresh_launch_token(tmp_path, kind):
    if kind == "init":
        from levain.init_server import make_init_server as make
        arg = tmp_path / "install"
    else:
        from levain.docs_server import make_docs_server as make
        arg = tmp_path
    tokens = []
    for _ in range(2):
        httpd = make(arg, port=0)
        try:
            tokens.append(httpd.launch_token)
        finally:
            httpd.server_close()
    assert all(t and len(t) >= 32 for t in tokens) and tokens[0] != tokens[1]
    for bad in ("", " secret ", "tok/en", "tökén"):   # codex L3 r1: a token the page cannot carry is refused
        with pytest.raises(ValueError, match="launch token must be"):
            make(arg, port=0, launch_token=bad)


@pytest.mark.parametrize("bad", ["", " secret ", "tok/en"])
def test_make_server_refuses_a_token_the_page_cannot_carry(tmp_path, bad):
    from levain.dashboard import AnnealPaths, SubstrateSource
    from levain.web_server import make_server

    src = SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db"))
    with pytest.raises(ValueError, match="launch token must be"):
        make_server(src, host="127.0.0.1", port=0, read_token=bad)


def test_the_old_chat_token_attribute_can_still_be_set(tmp_path):
    from levain.dashboard import AnnealPaths, SubstrateSource
    from levain.web_server import make_server

    httpd = make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0, read_token=None)
    try:
        httpd.chat_token = "set-the-old-way"
        assert httpd.launch_token == "set-the-old-way"
    finally:
        httpd.server_close()


def test_the_old_chat_token_attribute_cannot_remove_the_token(tmp_path):
    """complement + codex L3 r2: `httpd.chat_token = None` turned the gate off on a running server."""
    from levain.dashboard import AnnealPaths, SubstrateSource
    from levain.web_server import make_server

    httpd = make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0,
                        read_token=_TOKEN)
    try:
        with pytest.raises(ValueError):
            httpd.chat_token = None
        assert httpd.launch_token == _TOKEN
    finally:
        httpd.server_close()


def test_a_server_that_never_says_launch_token_serves_nothing(tmp_path):
    """complement L3 r2: the gate read a missing attribute as "ungated". A server must set launch_token = None to be
    ungated; one that forgot fails the request rather than serving it."""
    from http.server import ThreadingHTTPServer

    class _Open(GuardedHandler):
        def _route(self, *, head: bool) -> None:
            self._send(b"operator data\n", "text/plain", head=head)

    class _Quiet(ThreadingHTTPServer):
        def handle_error(self, request, client_address):
            pass

    httpd = _Quiet(("127.0.0.1", 0), _Open)
    httpd.allowed_hosts = frozenset({"127.0.0.1"})
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        try:
            status = _request(httpd.server_address[1], "GET", "/anything", token=None)[0]
        except (http.client.HTTPException, OSError):
            status = None
        assert status != 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


# --- head rulings 2026-10-07 ----------------------------------------------------------------------------------


def test_a_handler_that_defines_its_own_do_method_is_refused_when_its_class_is_made():
    """L2: the no-do_* rule is structural, not a convention a test checks afterwards."""
    with pytest.raises(TypeError, match="do_GET"):
        class _Bypass(GuardedHandler):  # noqa: F841 — the class statement itself must raise
            def do_GET(self):  # noqa: N802
                pass
    with pytest.raises(TypeError, match="do_PUT"):
        class _Other(GuardedHandler):  # noqa: F841
            def do_PUT(self):  # noqa: N802
                pass


def test_the_do_method_exemption_is_exactly_the_team_view_and_cannot_grow():
    from levain.http_guards import _DO_METHOD_EXEMPT

    assert _DO_METHOD_EXEMPT == {("levain.team.view", "_ViewHandler")}


def test_make_server_requires_an_explicit_read_token_choice(tmp_path):
    """Head ruling: a security API whose silent default is ungated is the shape we delete. Omitting read_token is a
    TypeError; None is the visible opt-out."""
    import inspect

    from levain.dashboard import AnnealPaths, SubstrateSource
    from levain.web_server import make_server

    p = inspect.signature(make_server).parameters["read_token"]
    assert p.kind is inspect.Parameter.KEYWORD_ONLY and p.default is inspect.Parameter.empty
    with pytest.raises(TypeError, match="read_token"):
        make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0)


def test_a_header_with_surrounding_whitespace_still_matches(tmp_path):
    with _server("serve", tmp_path) as port:
        assert _request(port, "GET", "/substrate.json", token=f"  {_TOKEN} ")[0] == 200


def test_the_old_header_works_and_is_reported_as_deprecated_once(tmp_path, capfd):
    import levain.http_guards as hg

    hg._warned_legacy.clear()
    with _server("serve", tmp_path) as port:
        for _ in range(2):
            assert _request(port, "GET", "/substrate.json", token=None,
                            headers={"X-Levain-Chat-Token": _TOKEN})[0] == 200
    err = capfd.readouterr().err
    assert err.count("X-Levain-Chat-Token, which is deprecated") == 1


def test_publish_prints_the_token_only_to_a_terminal_and_always_leaves_a_0600_file(tmp_path, monkeypatch):
    import io
    import json
    import os
    import stat
    from types import SimpleNamespace

    from levain.http_guards import publish_launch_token, read_running

    monkeypatch.setenv("HOME", str(tmp_path))

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    tok = "abcDEF123_-xyz"
    srv = SimpleNamespace(launch_token=tok)
    pipe, tty = io.StringIO(), _Tty()
    pub = publish_launch_token(srv, "http://127.0.0.1:7499/", port=7499, kind="serve", stream=pipe)
    assert tok not in pipe.getvalue() and "--open-running --port 7499" in pipe.getvalue()
    assert pub.unlocked.startswith("http://127.0.0.1:7499/#code=") and tok not in pub.unlocked
    rt = tmp_path / ".levain-runtime"
    assert stat.S_IMODE(rt.stat().st_mode) == 0o700
    assert stat.S_IMODE((rt / "7499.json").stat().st_mode) == 0o600
    assert read_running(7499)["token"] == tok
    pub.close()
    assert not (rt / "7499.json").exists()
    publish_launch_token(srv, "http://127.0.0.1:7498/", port=7498, kind="serve", stream=tty).close()
    assert tok in tty.getvalue()
    # a record whose writer is gone is refused, not opened
    (rt / "7497.json").write_text(json.dumps({"pid": 2**22 + 12345, "token": "t", "url": "http://x/"}))
    os.chmod(rt / "7497.json", 0o600)
    with pytest.raises(ValueError, match="no longer running"):
        read_running(7497)


def test_publish_refuses_to_serve_silently_when_the_token_has_nowhere_to_go(tmp_path, monkeypatch):
    """Not a terminal and the runtime file cannot be written: raise, so the server refuses to start instead of
    running with a token nobody can get."""
    import io
    from types import SimpleNamespace

    from levain.http_guards import publish_launch_token

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".levain-runtime").write_text("not a directory")
    with pytest.raises(OSError):
        publish_launch_token(SimpleNamespace(launch_token="tok"), "http://127.0.0.1:7496/", port=7496, kind="serve",
                             stream=io.StringIO())


def test_a_link_code_unlocks_once_and_the_token_never_rides_the_link(tmp_path, monkeypatch):
    """Head ruling 2026-10-07 (L2 #2), after a measured run: Chrome's History kept every #token= link opened, fragment
    included. The link now carries a single-use code that /unlock trades for the token, once."""
    import io
    import json

    import levain.web_server as ws
    from levain.http_guards import mint_link_code, publish_launch_token

    monkeypatch.setenv("HOME", str(tmp_path))
    from levain.dashboard import AnnealPaths, SubstrateSource

    httpd = ws.make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0,
                           read_token=_TOKEN)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        code = mint_link_code(httpd)

        def unlock(c, **hdr):
            got, _h, data = _raw_post(port, "/unlock", {"X-Levain-Token": "", "X-Levain-Link-Code": c, **hdr})
            return got, json.loads(data)

        assert unlock("not-a-code") == (403, {"error": "link_code", "message": "that link was already used or has expired"})
        assert unlock(code) == (200, {"token": _TOKEN})
        assert unlock(code)[0] == 403                                   # spent
        assert unlock(mint_link_code(httpd), **{"Sec-Fetch-Site": "cross-site"})[0] == 403   # write-origin first
        # /link mints a code only for a caller that proves the token without sending it
        from levain.http_guards import request_link_code

        code2 = request_link_code(f"http://127.0.0.1:{port}/", _TOKEN)
        assert unlock(code2)[0] == 200
        assert _raw_post(port, "/link", {"Content-Length": "0"})[0] == 403          # the bearer token alone is not proof
        with pytest.raises(OSError):
            request_link_code(f"http://127.0.0.1:{port}/", "not-the-token")     # a 403, as urllib's HTTPError
        monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")                  # never used for the loopback ask
        monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
        assert unlock(request_link_code(f"http://127.0.0.1:{port}/", _TOKEN))[0] == 200
        # the startup link carries a code, and `levain serve --open-running` opens a fresh one
        pub = publish_launch_token(httpd, f"http://127.0.0.1:{port}/", port=port, kind="serve", stream=io.StringIO())
        assert "#code=" in pub.unlocked and _TOKEN not in pub.unlocked
        opened = []
        monkeypatch.setattr(ws, "_open_browser", lambda url, unlocked: opened.append(unlocked) or True)
        out = io.StringIO()
        assert ws.open_running(port, stream=out) == 0
        assert opened[0].startswith(f"http://127.0.0.1:{port}/#code=") and _TOKEN not in opened[0] + out.getvalue()
        assert unlock(opened[0].split("#code=", 1)[1])[0] == 200
        pub.close()
        assert ws.open_running(port, stream=out) == 1
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_request_link_code_refuses_a_listener_that_cannot_prove_the_token(tmp_path):
    """L2 2026-10-07: --open-running sent the bearer token to whatever answered on the port. Now the token never
    crosses the socket, and a code from a listener that cannot prove it is refused, not opened."""
    import json
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from levain.http_guards import request_link_code

    seen = {}

    class _Squatter(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            seen.update(self.headers)
            body = json.dumps({"code": "attacker-chosen-code", "proof": "0" * 64}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Squatter)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(ValueError, match="did not prove"):
            request_link_code(f"http://127.0.0.1:{httpd.server_address[1]}/", _TOKEN)
        assert _TOKEN not in json.dumps(dict(seen))
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    for bad in ("http://10.0.0.5:7420/", "https://127.0.0.1:7420/", "http://example.com:7420/", "http://127.0.0.1/"):
        with pytest.raises(ValueError, match="not an http loopback origin"):
            request_link_code(bad, _TOKEN)


def test_a_link_code_expires(tmp_path, monkeypatch):
    from types import SimpleNamespace

    import levain.http_guards as hg

    # either clock passing the deadline expires a code: the wall clock (it runs during sleep, macOS's monotonic one
    # does not) and the monotonic one (immune to a wall clock set back)
    for advance in ((0, 1), (1, 0)):
        now = [1000.0, 5000.0]
        monkeypatch.setattr(hg, "_now", lambda: (now[0], now[1]))
        srv = SimpleNamespace(launch_token=_TOKEN)
        code = hg.mint_link_code(srv)
        assert hg._spend_link_code(srv, hg.mint_link_code(srv)) is True
        now[0] += advance[0] * (hg.LINK_CODE_SECONDS + 1)
        now[1] += advance[1] * (hg.LINK_CODE_SECONDS + 1)
        assert hg._spend_link_code(srv, code) is False


def test_a_do_method_from_a_mixin_is_refused_too():
    """L1 2026-10-07: the check read only the class body, so a mixin ahead of GuardedHandler in the MRO could put its
    own do_POST in front of the guards."""
    class _Mixin:
        def do_POST(self):  # noqa: N802
            pass

    with pytest.raises(TypeError, match="do_POST"):
        class _Sneaky(_Mixin, GuardedHandler):  # noqa: F841
            pass


def test_stop_on_sigterm_puts_the_previous_handler_back():
    import signal

    from levain.http_guards import stop_on_sigterm

    before = signal.getsignal(signal.SIGTERM)
    restore = stop_on_sigterm()
    assert signal.getsignal(signal.SIGTERM) is not before
    with pytest.raises(KeyboardInterrupt):
        signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
    restore()
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_record_whose_pid_now_belongs_to_another_user_is_stale(tmp_path, monkeypatch):
    """L1 + L2 2026-10-07: a PermissionError from kill(pid, 0) was read as "alive". This user wrote the record, so
    the pid belonging to another user (pid 1 here, root's) means the writer is gone."""
    import json

    from levain.http_guards import read_running

    monkeypatch.setenv("HOME", str(tmp_path))
    rt = tmp_path / ".levain-runtime"
    rt.mkdir(mode=0o700)
    (rt / "7490.json").write_text(json.dumps({"pid": 1, "token": "t", "url": "http://127.0.0.1:7490/"}))
    with pytest.raises(ValueError, match="no longer running"):
        read_running(7490)


def test_link_round_one_review_fixes(tmp_path, monkeypatch):
    """L3 a1b7e75430a7c14d: a /link request cannot be replayed; a non-object answer is refused cleanly; a token too
    long for a header line is refused at construction; close() leaves a newer server's record alone."""
    import io
    import json
    from types import SimpleNamespace

    import levain.http_guards as hg

    monkeypatch.setenv("HOME", str(tmp_path))
    srv = SimpleNamespace(launch_token=_TOKEN)
    assert hg._first_use_of_nonce(srv, "n" * 24) is True
    assert hg._first_use_of_nonce(srv, "n" * 24) is False
    with pytest.raises(ValueError, match="launch token must be"):
        hg.check_launch_token("a" * 257)
    hg.check_launch_token("a" * 256)
    first = hg.publish_launch_token(srv, "http://127.0.0.1:7489/", port=7489, kind="serve", stream=io.StringIO())
    srv2 = SimpleNamespace(launch_token="another-token-xyz")
    second = hg.publish_launch_token(srv2, "http://127.0.0.1:7489/", port=7489, kind="serve", stream=io.StringIO())
    first.close()                                  # same pid, different token: the newer record stays
    assert json.loads((tmp_path / ".levain-runtime" / "7489.json").read_text())["token"] == "another-token-xyz"
    second.close()
    assert not (tmp_path / ".levain-runtime" / "7489.json").exists()


def test_request_link_code_refuses_a_non_object_answer(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from levain.http_guards import request_link_code

    class _List(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"[]")

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _List)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(ValueError, match="not a link"):
            request_link_code(f"http://127.0.0.1:{httpd.server_address[1]}/", _TOKEN)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_a_terminal_hears_when_the_runtime_file_could_not_be_written(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    from levain.http_guards import publish_launch_token

    class _Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".levain-runtime").write_text("not a directory")
    out = _Tty()
    publish_launch_token(SimpleNamespace(launch_token=_TOKEN), "http://127.0.0.1:7488/", port=7488, kind="serve",
                         stream=out)
    assert _TOKEN in out.getvalue() and "could not write" in out.getvalue()


def test_link_round_two_review_fixes(tmp_path, monkeypatch):
    """L3 8a52a0cce9e685ed: a /link proof binds a time (a request replayed after its nonce is forgotten is stale);
    the nonce memory exists from arming, so two first requests share it; --open-running that opened no browser
    says so and fails rather than printing "Opened"."""
    import io
    import time as _time

    import levain.http_guards as hg
    import levain.web_server as ws
    from levain.dashboard import AnnealPaths, SubstrateSource

    monkeypatch.setenv("HOME", str(tmp_path))
    httpd = ws.make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0,
                           read_token=_TOKEN)
    assert httpd.link_nonces == {}
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        old = str(int(_time.time()) - hg.LINK_SKEW_SECONDS - 5)
        nonce = "n" * 24
        got = _raw_post(port, "/link", {"X-Levain-Token": "", "X-Levain-Link-Nonce": nonce, "X-Levain-Link-Time": old,
                                        "X-Levain-Link-Proof": hg._link_proof(_TOKEN, "levain-link-request", nonce, old)})
        assert got[0] == 403
        pub = hg.publish_launch_token(httpd, f"http://127.0.0.1:{port}/", port=port, kind="serve", stream=io.StringIO())
        monkeypatch.setattr(ws, "_open_browser", lambda url, unlocked: False)
        assert ws.open_running(port, stream=io.StringIO()) == 1
        pub.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_a_stale_tmp_file_from_a_crashed_writer_is_removed(tmp_path, monkeypatch):
    import io
    from types import SimpleNamespace

    from levain.http_guards import publish_launch_token

    monkeypatch.setenv("HOME", str(tmp_path))
    rt = tmp_path / ".levain-runtime"
    rt.mkdir(mode=0o700)
    stale = rt / f".7487.{2**22 + 99}.deadbeef.tmp"   # a pid no process has
    stale.write_text("{}")
    pub = publish_launch_token(SimpleNamespace(launch_token=_TOKEN), "http://127.0.0.1:7487/", port=7487,
                               kind="serve", stream=io.StringIO())
    assert not stale.exists() and (rt / "7487.json").exists()
    pub.close()


def test_full_branch_round_review_fixes(tmp_path, monkeypatch):
    """L3 17da00ebad17a811: a timestamp header too long for int() is a 403, not a traceback; a non-object runtime
    record is refused by read_running and does not stop close(); a successor that reuses the token is not removed by
    its predecessor's close(); a squatter cannot make --open-running read without end."""
    import io
    import json
    from types import SimpleNamespace

    import levain.http_guards as hg
    import levain.web_server as ws
    from levain.dashboard import AnnealPaths, SubstrateSource

    monkeypatch.setenv("HOME", str(tmp_path))
    httpd = ws.make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0,
                           read_token=_TOKEN)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        port = httpd.server_address[1]
        got = _raw_post(port, "/link", {"X-Levain-Token": "", "X-Levain-Link-Nonce": "n" * 24,
                                        "X-Levain-Link-Time": "9" * 5000, "X-Levain-Link-Proof": "x"})
        assert got[0] == 403
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)
    srv = SimpleNamespace(launch_token=_TOKEN)
    old = hg.publish_launch_token(srv, "http://127.0.0.1:7486/", port=7486, kind="serve", stream=io.StringIO())
    new = hg.publish_launch_token(srv, "http://127.0.0.1:7486/", port=7486, kind="serve", stream=io.StringIO())
    old.close()
    assert (tmp_path / ".levain-runtime" / "7486.json").exists()
    (tmp_path / ".levain-runtime" / "7486.json").write_text("[]")
    with pytest.raises(ValueError, match="not a Levain runtime record"):
        hg.read_running(7486)
    new.close()   # does not raise


def test_request_link_code_bounds_what_it_reads(tmp_path):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from levain.http_guards import request_link_code

    class _Flood(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = b"{" + b" " * 100_000 + b"}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Flood)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        with pytest.raises(ValueError, match="too long"):
            request_link_code(f"http://127.0.0.1:{httpd.server_address[1]}/", _TOKEN)
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def test_request_link_code_gives_up_on_a_dripping_listener(tmp_path):
    import time as _time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from levain.http_guards import request_link_code

    class _Drip(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            for _ in range(1000):
                self.wfile.write(b" ")
                self.wfile.flush()
                _time.sleep(0.2)

        def log_message(self, *a):
            pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Drip)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        started = _time.monotonic()
        with pytest.raises(ValueError, match="took too long"):
            request_link_code(f"http://127.0.0.1:{httpd.server_address[1]}/", _TOKEN, timeout=1.0)
        assert _time.monotonic() - started < 10
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_request_link_code_gives_up_on_a_listener_dripping_its_headers(tmp_path):
    """L1: a socket timeout is per receive, so a deadline only on the body let a listener drip the status line and
    headers for as long as it liked."""
    import socket
    import time as _time

    from levain.http_guards import request_link_code

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    stop = threading.Event()

    def drip():
        conn, _ = srv.accept()
        conn.recv(65536)
        conn.sendall(b"HTTP/1.1 200 OK\r\n")
        while not stop.is_set():
            try:
                conn.sendall(b"X-Drip: a\r\n")
            except OSError:
                break
            _time.sleep(0.2)
        conn.close()

    t = threading.Thread(target=drip, daemon=True)
    t.start()
    try:
        started = _time.monotonic()
        with pytest.raises(ValueError, match="took too long"):
            request_link_code(f"http://127.0.0.1:{srv.getsockname()[1]}/", _TOKEN, timeout=0.5)
        assert _time.monotonic() - started < 3
    finally:
        stop.set()
        srv.close()
