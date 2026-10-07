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
_NOT_YET_ON_THE_SHARED_GUARDS = {"_ViewHandler": "levain team view: launch token pending (np-ebb8a399, team half)"}


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
                            host="127.0.0.1", port=0, launch_token=_TOKEN)
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
        make_server(src, host="127.0.0.1", port=0, launch_token=bad)


def test_the_old_chat_token_attribute_can_still_be_set(tmp_path):
    from levain.dashboard import AnnealPaths, SubstrateSource
    from levain.web_server import make_server

    httpd = make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")), host="127.0.0.1", port=0)
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
                        launch_token=_TOKEN)
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
