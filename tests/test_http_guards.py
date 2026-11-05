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
    # Since tefix2 these two are refused at the parse boundary (for every method), keeping the 411 the route helper
    # gave them; a too-long digit string is plain digits, so it still reaches the helper's own 411.
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


# --- request framing: refused at the one parse boundary (RFC 9112 6.1, 6.3) --------------------------------------------
# The L2 on the voice V1-4 branch (F3) measured `Transfer-Encoding: chunked` TOGETHER WITH `Content-Length` being
# accepted on a cockpit POST: the server read Content-Length bytes and parsed the rest of the stream as the NEXT request
# on the kept-alive socket (a request-smuggling shape behind a proxy that prefers the Transfer-Encoding). tefix put the
# refusal in `_declared_length`; its L3 (four lineages) showed the same class wherever that helper is not reached (a
# GET or HEAD) or where the header parse itself disagrees with a strict peer (a repeated Content-Length, a malformed
# header name, a folded line). tefix2 moved the check to `GuardedHandler.parse_request`, which every request passes.

import json  # noqa: E402
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


def _parsed(buf: bytes) -> list[tuple[int, bytes, bytes]]:
    """Every COMPLETE response in ``buf`` as ``(status, head, body)``, walked by each response's own Content-Length
    (every Levain response, the stdlib's ``send_error`` included, carries one). Walked, not regex-scanned: a body that
    happens to contain ``HTTP/1.1 200 `` is not a second response (deepseek L3 r1)."""
    out, i = [], 0
    while True:
        end = buf.find(b"\r\n\r\n", i)
        if end < 0:
            return out
        head = buf[i:end]
        m = re.match(rb"HTTP/1\.[01] (\d{3}) ", head)
        n = re.search(rb"\r\nContent-Length: (\d+)", head, re.IGNORECASE)
        if not m or not n:
            return out
        stop = end + 4 + int(n.group(1))
        if stop > len(buf):
            return out
        out.append((int(m.group(1)), head, buf[end + 4:stop]))
        i = stop


def _responses(buf: bytes) -> list[int]:
    return [status for status, _, _ in _parsed(buf)]


def _send_then_pipelined_get(port: int, request_line: str, header_lines: list[str], body: bytes) -> bytes:
    """Send a request's head (``header_lines`` verbatim, after Host) and read what the server says; then, in a second
    step, the body and a pipelined ``GET /``, and read what it says to that.

    Two steps, not one write: a server that refuses and closes with the body unread can reset the socket under an
    answer the client has not read yet. The two outcomes it tells apart:
    - refused and closed: step 1 reads the one answer and then EOF, and returns. The pipelined GET is never sent,
      because a closed connection cannot serve it; that is the property, not a skipped check (a refusal that did NOT
      close would reach step 1's idle timeout instead, and step 2 would then deliver the body and the GET, whose
      answers make the count wrong);
    - taken: step 1 idles out (the server is waiting for the body, or has answered a bodiless request and keeps the
      connection), step 2 sends the rest and reads until two complete responses or EOF, under a deadline. Reading to
      a COUNT, not to an idle timeout, is what keeps a slow route from turning a served GET into a false red."""
    s = socket.create_connection(("127.0.0.1", port), timeout=1.0)
    got = b""
    try:
        head = [request_line, f"Host: 127.0.0.1:{port}", *header_lines]
        s.sendall(("\r\n".join(head) + "\r\n\r\n").encode("latin-1"))
        try:
            while chunk := s.recv(65536):
                got += chunk
            return got                                        # EOF: the server answered and closed
        except (TimeoutError, ConnectionResetError):
            pass
        try:
            s.sendall(body + f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("ascii"))
        except OSError:
            return got                                        # closed under the second write: nothing more served
        s.settimeout(15)
        try:
            while len(_parsed(got)) < 2 and (chunk := s.recv(65536)):
                got += chunk
        except (TimeoutError, ConnectionResetError):
            pass
        return got
    finally:
        s.close()


def _post_then_pipelined_get(port: int, route: str, extra: dict, te_cl: list[tuple[str, str]], body: bytes) -> bytes:
    lines = ["Content-Type: application/json", *(f"{k}: {v}" for k, v in extra.items()),
             *(f"{k}: {v}" for k, v in te_cl)]
    return _send_then_pipelined_get(port, f"POST {route} HTTP/1.1", lines, body)


def _status_for(error: str) -> int:
    return 411 if error == "length_required" else 400


def _refused(got: bytes, error: str) -> None:
    parsed = _parsed(got)
    assert [p[0] for p in parsed] == [_status_for(error)], f"{[p[0] for p in parsed]} {got[:600]!r}"
    assert b"\r\nConnection: close" in parsed[0][1]
    assert json.loads(parsed[0][2])["error"] == error     # the boundary's word, not a route's own 400


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("te", ["chunked", "identity", "gzip, chunked"])
def test_transfer_encoding_with_content_length_is_refused_and_the_pipelined_get_is_not_served(tmp_path, family, te):
    with _te_target(family, tmp_path) as (port, route, extra, body):
        got = _post_then_pipelined_get(port, route, extra,
                                       [("Transfer-Encoding", te), ("Content-Length", str(len(body)))], body)
    _refused(got, "transfer_encoding_refused")


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("te", ["chunked", "identity"])
def test_transfer_encoding_without_content_length_is_refused_too(tmp_path, family, te):
    """tefix answered this 411 (the route helper's missing-length word). The boundary refuses every
    Transfer-Encoding with ONE answer, 400 ``transfer_encoding_refused``: a 411 told the client to add a
    Content-Length, and the request it then sent (both framings) was the one refused (gemini L3 r1)."""
    with _te_target(family, tmp_path) as (port, route, extra, body):
        got = _post_then_pipelined_get(port, route, extra, [("Transfer-Encoding", te)], b"0\r\n\r\n")
    _refused(got, "transfer_encoding_refused")


@pytest.mark.parametrize("family", FAMILIES)
def test_a_plain_content_length_post_is_unchanged_and_the_connection_stays_usable(tmp_path, family):
    """No Transfer-Encoding: the POST is answered by its route (not by the guard) and the pipelined GET is served too."""
    with _te_target(family, tmp_path) as (port, route, extra, body):
        got = _post_then_pipelined_get(port, route, extra, [("Content-Length", str(len(body)))], body)
    assert len(_responses(got)) == 2, f"{family}: {_responses(got)} {got[:400]!r}"
    words = [json.loads(body).get("error") for _, _, body in _parsed(got)[:1]]
    assert words[0] not in ("transfer_encoding_refused", "content_length_refused", "malformed_header_refused",
                            "length_required"), words


# The classes the boundary refuses, each on a bodiless route (GET / of `levain serve`) and a body route (POST /edit).
# `{n}` is the length of the body sent after the head. Each case is refused by ONE rule (a mutant that drops that
# rule turns exactly its cases red), and every case on GET was answered 200 with its body served as a second request
# before tefix2 (the RUN in project_memory/1105-17/RETURN_tefix2.md).
_GET_CASES = {
    "te-cl": (["Transfer-Encoding: chunked", "Content-Length: {n}"], "transfer_encoding_refused"),
    "te-only": (["Transfer-Encoding: chunked"], "transfer_encoding_refused"),
    "cl-twice-same": (["Content-Length: 0", "Content-Length: 0"], "content_length_refused"),
    "cl-list": (["Content-Length: 0, 0"], "content_length_refused"),
    "cl-body": (["Content-Length: {n}"], "content_length_refused"),
    "cl-not-digits": (["Content-Length: abc"], "length_required"),
    "space-before-colon": (["Content-Length: 0", "Transfer-Encoding : chunked"], "malformed_header_refused"),
    "obs-fold": (["Content-Length: 0", "X-A: a", " Transfer-Encoding: chunked"], "malformed_header_refused"),
    "name-not-a-token": (["X(A): b"], "malformed_header_refused"),
}
_POST_CASES = {
    "te-cl": (["Transfer-Encoding: chunked", "Content-Length: {n}"], "transfer_encoding_refused"),
    "cl-twice-same": (["Content-Length: {n}", "Content-Length: {n}"], "content_length_refused"),
    "cl-twice-differ": (["Content-Length: 1", "Content-Length: {n}"], "content_length_refused"),
    "cl-list": (["Content-Length: {n}, {n}"], "content_length_refused"),
    "space-before-colon": (["Content-Length: {n}", "Transfer-Encoding : chunked"], "malformed_header_refused"),
    "tab-before-colon": (["Content-Length: {n}", "Transfer-Encoding\t: chunked"], "malformed_header_refused"),
    "obs-fold": (["Content-Length: {n}", "X-A: a", " Transfer-Encoding: chunked"], "malformed_header_refused"),
}


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("case", list(_GET_CASES))
def test_a_bodiless_request_with_ambiguous_framing_is_refused_and_its_body_is_never_a_request(tmp_path, method, case):
    lines, error = _GET_CASES[case]
    with _server("serve", tmp_path) as port:
        smuggled = f"GET /x HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("ascii")
        got = _send_then_pipelined_get(port, f"{method} / HTTP/1.1",
                                       [ln.format(n=len(smuggled)) for ln in lines], smuggled)
    if method == "GET":
        _refused(got, error)
    else:
        # A HEAD answer carries the GET body's Content-Length and no body, so it is not walked by length: one head,
        # nothing after it, the connection closed.
        assert got.startswith(b"HTTP/1.1 %d " % _status_for(error)) and got.endswith(b"\r\n\r\n") and got.count(b"HTTP/1.") == 1, got[:600]
        assert b"\r\nConnection: close" in got


@pytest.mark.parametrize("kind", ["serve", "init"])
@pytest.mark.parametrize("case", list(_POST_CASES))
def test_a_write_with_ambiguous_framing_is_refused_at_the_boundary(tmp_path, kind, case):
    lines, error = _POST_CASES[case]
    with _server(kind, tmp_path) as port:
        tok = [f"X-Levain-Write-Token: {_TOKEN[kind]}"] if kind == "init" else []
        got = _send_then_pipelined_get(port, f"POST {WRITE_ROUTES[kind]} HTTP/1.1",
                                       ["Content-Type: application/json", *tok,
                                        *(ln.format(n=2) for ln in lines)], b"{}")
    _refused(got, error)


def test_a_leading_from_line_the_parser_swallows_is_refused(tmp_path):
    """The stdlib's header parser takes a first header line starting ``From `` as a mailbox envelope line: it vanishes
    from the parsed headers with no defect recorded, so a peer that reads it as a header sees a field Levain never did.
    (Not through the helper: it always sends Host first.)"""
    with _server("serve", tmp_path) as port:
        s = socket.create_connection(("127.0.0.1", port), timeout=5)
        try:
            s.sendall(f"GET / HTTP/1.1\r\nFrom x\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode("ascii"))
            got = b""
            while chunk := s.recv(65536):
                got += chunk
        finally:
            s.close()
    _refused(got, "malformed_header_refused")


def test_a_plain_get_and_a_zero_length_get_are_unchanged(tmp_path):
    with _server("serve", tmp_path) as port:
        plain = _send_then_pipelined_get(port, "GET / HTTP/1.1", [], b"")
        zero = _send_then_pipelined_get(port, "GET / HTTP/1.1", ["Content-Length: 0"], b"")
    assert _responses(plain) == [200, 200] and _responses(zero) == [200, 200]


@pytest.mark.parametrize("kind", ["serve", "init"])
def test_a_refused_write_that_expects_100_continue_is_never_told_to_continue(tmp_path, kind):
    """The stdlib answers ``Expect: 100-continue`` inside its own ``parse_request``, before the framing check after it
    could run; without the ``handle_expect_100`` override the client was told to send a body the server then refused
    (codex L3 r1). The refusal comes first, alone."""
    with _server(kind, tmp_path) as port:
        tok = [f"X-Levain-Write-Token: {_TOKEN[kind]}"] if kind == "init" else []
        got = _send_then_pipelined_get(port, f"POST {WRITE_ROUTES[kind]} HTTP/1.1",
                                       ["Content-Type: application/json", *tok, "Expect: 100-continue",
                                        "Transfer-Encoding: chunked", "Content-Length: 2"], b"{}")
    assert b" 100 " not in got.split(b"\r\n", 1)[0], got[:300]
    _refused(got, "transfer_encoding_refused")
