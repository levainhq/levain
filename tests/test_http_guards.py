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
    _TOKEN[kind] = getattr(httpd, "write_token", None)
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
_TOKEN: dict[str, str | None] = {}


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
    import sys

    # The too-many-digits case depends on Python's int-conversion limit; pin the default so the
    # outcome does not depend on PYTHONINTMAXSTRDIGITS in the environment (L3 r2).
    old_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        with _server(kind, tmp_path) as port:
            # No body: a refused request's unread bytes can make the kernel reset the socket before
            # the response is read (complement L3).
            # init's write token is checked right after the origin checks (K2a's order); send it so the
            # later refusals are reached. The bad-host and cross-site cases refuse before it is read.
            tok = {"X-Levain-Write-Token": _TOKEN[kind]} if kind == "init" else {}
            got, resp_headers, _ = _raw_post(port, WRITE_ROUTES[kind], {**tok, **headers})
    finally:
        sys.set_int_max_str_digits(old_limit)
    assert got == status, f"{kind}: {headers} -> {got}"
    assert resp_headers.get("Connection") == "close"


def test_verbose_access_log_never_carries_a_search_query(monkeypatch, capsys) -> None:
    from levain.http_guards import GuardedHandler

    monkeypatch.setenv("LEVAIN_SERVE_VERBOSE", "1")
    h = GuardedHandler.__new__(GuardedHandler)
    h.client_address = ("127.0.0.1", 1)
    h.requestline = "GET /cockpit/panel/episodes.json?limit=5&q=secret%20words HTTP/1.1"
    h.log_request(200)
    err = capsys.readouterr().err
    assert "secret" not in err and "q=-" in err and "limit=5" in err


def test_verbose_access_log_redacts_q_by_its_decoded_name_and_on_the_error_path(monkeypatch, capsys) -> None:
    from levain.http_guards import GuardedHandler

    monkeypatch.setenv("LEVAIN_SERVE_VERBOSE", "1")
    h = GuardedHandler.__new__(GuardedHandler)
    h.client_address = ("127.0.0.1", 1)
    h.requestline = "GET /cockpit/panel/p.json?%71=secret&limit=5&q%3D=kept HTTP/1.1"
    h.log_request(200)
    h.log_error("code %d, message %s", 400, "Bad request syntax ('GET /x?a=1&q=hidden HTTP/1.1')")
    err = capsys.readouterr().err
    assert "secret" not in err and "hidden" not in err and "%71=-" in err and "limit=5" in err and "a=1" in err


# --- request framing: Transfer-Encoding is never accepted (RFC 9112 6.1) -----------------------------------------------
# The L2 on the voice V1-4 branch (F3) measured `Transfer-Encoding: chunked` TOGETHER WITH `Content-Length` being
# accepted on a cockpit POST: the server read Content-Length bytes and parsed the rest of the stream as the NEXT request
# on the kept-alive socket (a request-smuggling shape behind a proxy that prefers the Transfer-Encoding). No route
# reads a chunked body, so every Transfer-Encoding on a request is a framing the server does not speak: the one shared
# `_declared_length` refuses it and closes, before a byte of the body is read.

import re  # noqa: E402
import socket  # noqa: E402

_JSON_BODY = b'{"body":"x","priority":"low"}'


@contextmanager
def _te_target(family: str, tmp_path: Path):
    """Yield ``(port, request_line, extra_headers, body)`` for one POST route of a real server. Five families, each a
    different place the shared guard is reached from: the governed write (`/edit`), the install server (`/init`), a
    pairing route (`_pair_body`), the notify route and the unauthenticated receipt carve-out."""
    if family in ("edit", "init"):
        kind = "serve" if family == "edit" else "init"
        with _server(kind, tmp_path) as port:
            tok = {"X-Levain-Write-Token": _TOKEN[kind]} if family == "init" else {}
            yield port, WRITE_ROUTES[kind], tok, b"{}"
    elif family == "pair":
        from tests.test_cockpit_k2a import _install
        from tests.test_cockpit_pairing_http import H, T, _serve as pair_serve

        with pair_serve(_install(tmp_path)[1]) as (port, _h):
            yield port, "/cockpit/pair/offer", {H: T}, b'{"label":"x"}'
    else:
        from levain.webpush import notify_tokens as nt
        from tests.test_cockpit_k2a import _install
        from tests.test_cockpit_webpush import _signed

        q = tmp_path / "signq"
        q.mkdir(mode=0o700)
        src = _install(tmp_path)[1]
        tok = nt.create(q, "Backup")[1]
        with _signed(src, q) as (port, _h):
            if family == "notify":
                yield port, "/notify", {nt.HEADER: tok}, _JSON_BODY
            else:
                yield port, "/webpush/receipt", {}, b"{}"


FAMILIES = ["edit", "init", "pair", "notify", "receipt"]


def _responses(buf: bytes) -> list[int]:
    """The status of every HTTP response in ``buf`` (responses are not CRLF-separated from the previous body, so a regex, not a line split)."""
    return [int(m) for m in re.findall(rb"HTTP/1\.[01] (\d{3}) ", buf)]


def _post_then_pipelined_get(port: int, route: str, extra: dict, te_cl: list[tuple[str, str]], body: bytes) -> bytes:
    """Send the POST head ``te_cl`` (Transfer-Encoding / Content-Length lines) and read what the server says; then, in a
    second step, the body and a pipelined ``GET /``, and read what it says to that. Two steps, not one write: a server
    that refuses and closes with the body unread can reset the socket under an answer the client has not read yet. A
    server that refused answers once and closes (the second step may find the socket gone); a server that took the
    request answers the POST and then the GET."""
    s = socket.create_connection(("127.0.0.1", port), timeout=1.5)
    got = b""
    try:
        lines = [f"POST {route} HTTP/1.1", f"Host: 127.0.0.1:{port}", "Content-Type: application/json"]
        lines += [f"{k}: {v}" for k, v in extra.items()] + [f"{k}: {v}" for k, v in te_cl]
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode("ascii"))
        for step in (b"", body + f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("ascii")):
            if step:
                try:
                    s.sendall(step)
                except OSError:
                    pass                                  # the server already closed: the point
            try:
                while True:
                    chunk = s.recv(65536)
                    if not chunk:
                        return got
                    got += chunk
            except (TimeoutError, ConnectionResetError):
                pass
        return got
    finally:
        s.close()


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("te", ["chunked", "identity", "gzip, chunked"])
def test_transfer_encoding_with_content_length_is_refused_and_the_pipelined_get_is_not_served(tmp_path, family, te):
    with _te_target(family, tmp_path) as (port, route, extra, body):
        got = _post_then_pipelined_get(port, route, extra,
                                       [("Transfer-Encoding", te), ("Content-Length", str(len(body)))], body)
    statuses = _responses(got)
    assert statuses == [400], f"{family} TE={te!r}: {statuses}\n{got[:600]!r}"
    assert b'"error": "transfer_encoding_refused"' in got        # the guard's word, not a route's own 400
    assert b"Connection: close" in got.split(b"\r\n\r\n")[0]


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("te", ["chunked", "identity"])
def test_transfer_encoding_without_content_length_is_still_411(tmp_path, family, te):
    with _te_target(family, tmp_path) as (port, route, extra, body):
        got = _post_then_pipelined_get(port, route, extra, [("Transfer-Encoding", te)], body)
    assert _responses(got) == [411], f"{family} TE={te!r}: {got[:600]!r}"


@pytest.mark.parametrize("family", FAMILIES)
def test_a_plain_content_length_post_is_unchanged_and_the_connection_stays_usable(tmp_path, family):
    """No Transfer-Encoding: the POST is answered by its route (not by the guard) and the pipelined GET is served too."""
    with _te_target(family, tmp_path) as (port, route, extra, body):
        got = _post_then_pipelined_get(port, route, extra, [("Content-Length", str(len(body)))], body)
    assert len(_responses(got)) == 2, f"{family}: {_responses(got)} {got[:400]!r}"
    assert b"transfer_encoding_refused" not in got and b"length_required" not in got
