"""CH-D2 S9 session 2a: R15-12-B9's smuggling forms (CL+TE, duplicate Content-Length, `Transfer-Encoding: xchunked`,
obs-fold), composed, on the plain path (the real glue and decider) and inside TLS (the production spawn). Each:
the origin receives no request at all, and the reply is a refusal or an empty reply, never a 200.

The bodies are round 3's (`probes/test_r3_b9_delegated.py::_smuggle_req`); the origin check is the strong one, since a
smuggled `GET /SMUGGLED` would show as a second request at the origin.
"""

from __future__ import annotations

import time

import pytest

from ..probes.test_r3_b9_delegated import _smuggle_req
from .tls_rig import UNKEYED

pytestmark = pytest.mark.chat_egress_srt

KINDS = ["clte", "dupcl", "xchunked", "obsfold"]


def _nothing_reached(origin) -> None:
    time.sleep(0.4)
    assert origin.reqs == [], [(r.method, r.target) for r in origin.reqs]


@pytest.mark.parametrize("kind", KINDS)
def test_plain_path_smuggling_form_reaches_the_origin_as_nothing(make_origin, make_launch_, kind) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    P = L.origin_port
    hostport = f"pub.rb.test:{P}".encode()
    resp = L.raw(_smuggle_req(kind, hostport, f"http://pub.rb.test:{P}/x".encode()))
    print(f"RUN R15-12-B9 plain {kind}:", resp[:40])
    assert not resp.startswith(b"HTTP/1.1 200"), resp[:200]
    assert L.stop() == 0
    _nothing_reached(origin)


@pytest.mark.parametrize("kind", KINDS)
def test_tls_smuggling_form_reaches_the_origin_as_nothing(make_tls_launch, kind) -> None:
    L = make_tls_launch()
    hostport = f"{UNKEYED}:{L.origin_port}".encode()
    resp = L.raw(_smuggle_req(kind, hostport, b"/x"))
    print(f"RUN R15-12-B9 TLS {kind}:", resp[:40])
    assert not resp.startswith(b"HTTP/1.1 200"), resp[:200]
    _nothing_reached(L.origin)


def test_control_the_origin_rig_records_a_request_that_reaches_it(make_origin) -> None:
    """So `_nothing_reached` can fail: a clean request sent straight at the recording origin is recorded."""
    import socket
    origin = make_origin()
    s = socket.create_connection(("127.0.0.1", origin.port), timeout=5)
    s.sendall(b"GET /clean HTTP/1.1\r\nHost: o\r\n\r\n")
    s.recv(4096)
    s.close()
    deadline = time.monotonic() + 5
    while not origin.reqs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert [r.target for r in origin.reqs] == ["/clean"]
