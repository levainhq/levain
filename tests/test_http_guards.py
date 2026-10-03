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

# Derived, so a guard added to the base later is covered too (L3 2026-10-03). `_route` is the one
# method every server must define for itself.
GUARD_METHODS = tuple(
    name for name, value in vars(GuardedHandler).items()
    if callable(value) and not name.startswith("__") and name != "_route"
)

SECURITY_HEADERS = ("Content-Security-Policy", "X-Content-Type-Options", "X-Frame-Options",
                    "Cache-Control")


def _handlers():
    from levain.docs_server import _DocsHandler
    from levain.init_server import _InitHandler
    from levain.web_server import _Handler

    return [_Handler, _InitHandler, _DocsHandler]


@pytest.mark.parametrize("cls", _handlers(), ids=lambda c: c.__name__)
def test_every_server_inherits_the_guards_and_copies_none(cls):
    assert issubclass(cls, GuardedHandler)
    assert {"_refuse_read", "_refuse_oversize", "_declared_length"} <= set(GUARD_METHODS)
    copied = [name for name in GUARD_METHODS if name in cls.__dict__]
    assert copied == [], f"{cls.__name__} defines its own {copied}; the shared copy is the only one"


@contextmanager
def _server(kind: str, tmp_path: Path):
    if kind == "serve":
        from levain.dashboard import AnnealPaths, SubstrateSource
        from levain.web_server import make_server

        httpd = make_server(SubstrateSource(anneal=AnnealPaths.from_db(tmp_path / "m.db")),
                            host="127.0.0.1", port=0)
    elif kind == "init":
        from levain.init_server import make_init_server

        httpd = make_init_server(tmp_path / "install", port=0)
    else:
        from levain.docs_server import make_docs_server

        httpd = make_docs_server(tmp_path, port=0)
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=5)


def _request(port: int, method: str, path: str = "/", headers: dict | None = None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request(method, path, headers={"Host": "127.0.0.1", **(headers or {})})
    r = c.getresponse()
    r.read()
    c.close()
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
    for k, v in {"Host": "127.0.0.1", **headers}.items():
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
    with _server(kind, tmp_path) as port:
        # No body: a refused request's unread bytes can make the kernel reset the socket before the
        # response is read (complement L3).
        got, resp_headers, _ = _raw_post(port, WRITE_ROUTES[kind], headers)
    assert got == status, f"{kind}: {headers} -> {got}"
    assert resp_headers.get("Connection") == "close"
