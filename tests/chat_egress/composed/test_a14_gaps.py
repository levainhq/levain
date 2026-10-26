"""CH-D2 S9 session 2b: the R15-10-A14 items no composed test held when the umbrella map (`test_a14_umbrella.py`) was
first drawn, each now run on the real glue and decider:

- *"R-1's smuggling suite, run through the actual srt parser."*: the row's `TE: chunked, identity` form
  (`Transfer-Encoding: chunked, identity`, plain and TLS) reaches the origin as nothing.
- *"... `X-Original-URL`, `X-Rewrite-URL` and `X-Forwarded-Host` handling."* (expected verdict STRIPPED, row
  R15-12-B3): a forwarded read carries none of the three to the origin.
- *"A decider crash fails closed with a visible state."*: the decider's link cut under a running glue: the glue exits
  non-zero with no final flush, nothing after it reaches the origin, and K1's report says crashed.
- *"Open network + keyed write (finding 4)."*: composed over TLS with a held key, as R15-10-A11 rules it (a card).

The first three have a negative control (the same bytes through an open relay, `LeakyPlain`, reach the origin, or a
clean stop reads exact); the last its control (an anonymous POST under open network is forwarded).
"""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from levain.chat_egress import report

from .rig import autoanswer
from .tls_rig import UNKEYED

pytestmark = pytest.mark.chat_egress_srt

SMUGGLE = b"0\r\n\r\nGET /SMUGGLED HTTP/1.1\r\nHost: s\r\n\r\n"
ROUTING = [("X-Original-URL", "/admin/delete"), ("X-Rewrite-URL", "/admin/rewrite"), ("X-Forwarded-Host", "evil.test")]


def _te_identity(hostport: bytes, target: bytes) -> bytes:
    return (b"POST " + target + b" HTTP/1.1\r\nHost: " + hostport + b"\r\nTransfer-Encoding: chunked, identity\r\n"
            b"Connection: close\r\n\r\n5\r\nhello\r\n" + SMUGGLE)


def test_te_chunked_identity_on_the_plain_path_reaches_the_origin_as_nothing(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    P = L.origin_port
    resp = L.raw(_te_identity(f"pub.rb.test:{P}".encode(), f"http://pub.rb.test:{P}/x".encode()))
    print("RUN R15-10-A14 TE chunked, identity plain:", resp[:60])
    assert not resp.startswith(b"HTTP/1.1 200"), resp[:200]
    assert L.stop() == 0
    origin.assert_untouched()


def test_te_chunked_identity_inside_tls_reaches_the_origin_as_nothing(make_tls_launch) -> None:
    L = make_tls_launch()
    resp = L.raw(_te_identity(f"{UNKEYED}:{L.origin_port}".encode(), b"/x"))
    print("RUN R15-10-A14 TE chunked, identity TLS:", resp[:60])
    assert not resp.startswith(b"HTTP/1.1 200"), resp[:200]
    L.origin.assert_untouched()


def test_the_oracle_is_red_when_te_chunked_identity_is_relayed(make_origin, make_leaky_plain) -> None:
    origin = make_origin()
    L = make_leaky_plain(origin)
    L.raw(_te_identity(f"pub.rb.test:{L.origin_port}".encode(), b"/x"))
    time.sleep(0.3)
    assert origin.touched()["bytes"] > 0


def _routing_get(L) -> bytes:
    P = L.origin_port
    h = "".join(f"{k}: {v}\r\n" for k, v in ROUTING)
    return L.raw(f"GET http://pub.rb.test:{P}/r HTTP/1.1\r\nHost: pub.rb.test:{P}\r\n{h}Connection: close\r\n\r\n"
                 .encode())


def test_routing_headers_are_stripped_from_a_forwarded_read(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    resp = _routing_get(L)
    assert L.stop() == 0
    names = [k.lower() for r in origin.reqs for k, _ in r.headers]
    print("RUN R15-12-B3 routing headers at the origin:", resp[:20], names)
    assert resp.startswith(b"HTTP/1.1 200") and [r.target for r in origin.reqs] == ["/r"], (resp[:200], origin.reqs)
    assert not {k.lower() for k, _ in ROUTING} & set(names), names
    assert not any(v.encode() in r.raw_head for r in origin.reqs for _, v in ROUTING)


def test_the_oracle_is_red_when_the_proxy_relays_routing_headers(make_origin, make_leaky_plain) -> None:
    origin = make_origin()
    L = make_leaky_plain(origin)
    P = L.origin_port
    h = "".join(f"{k}: {v}\r\n" for k, v in ROUTING)
    L.raw(f"GET /r HTTP/1.1\r\nHost: pub.rb.test:{P}\r\n{h}Connection: close\r\n\r\n".encode())
    assert {"x-original-url", "x-rewrite-url", "x-forwarded-host"} <= {k.lower() for r in origin.reqs for k, _ in r.headers}


def _get_after(L, timeout: float = 5.0) -> bytes:
    """A GET on a fresh connection; whatever the listener answers (b"" for a refused or reset connection)."""
    try:
        return L.raw(f"GET http://pub.rb.test:{L.origin_port}/after HTTP/1.1\r\nHost: pub.rb.test:{L.origin_port}\r\n"
                     "Connection: close\r\n\r\n".encode(), timeout=timeout)
    except OSError:
        return b""


def test_a_decider_crash_fails_closed_with_a_visible_state(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    stop = autoanswer(L, "denied")
    try:
        ok = L.request("GET", "/before")
        L.d.link._sock.shutdown(socket.SHUT_RDWR)        # the decider's end of the link, cut as a crash cuts it
        try:
            code = L.proc.wait(timeout=10)
        except Exception:                                # noqa: BLE001 - reported by the assertion below
            code = None
        after = _get_after(L)
    finally:
        stop.set()
    err = L.stderr.read_text(errors="replace")
    lines = L.events.read_bytes().splitlines(keepends=True) if L.events.exists() else []
    finals = [json.loads(ln) for ln in lines if b'"summary"' in ln and json.loads(ln).get("final")]
    rep = report.build(L.records.rows(L.launch_id), lines)
    print("RUN R15-10-A14 decider crash: before", ok[0], "glue exit", code, "after", after[:30],
          "stderr tail", err.strip().splitlines()[-1:], "walls:", rep.walls.text)
    assert ok[0] == 200
    assert code not in (None, 0), code                   # the glue exited on its own, non-zero
    assert "decider dead" in err
    assert not after.startswith(b"HTTP/1.1 2"), after[:200]
    assert [r.target for r in origin.reqs] == ["/before"]      # nothing after the crash reached the origin
    assert finals == [] and rep.walls.crashed and not rep.walls.exact, rep.walls


def test_control_a_clean_stop_writes_the_final_flush_and_reads_exact(make_origin, make_launch_) -> None:
    """The control for the report's half: the same launch stopped cleanly reads exact, not crashed."""
    origin = make_origin()
    L = make_launch_(origin)
    assert L.request("GET", "/before")[0] == 200
    assert L.stop() == 0
    rep = report.build(L.records.rows(L.launch_id), L.events.read_bytes().splitlines(keepends=True))
    assert rep.walls.exact and not rep.walls.crashed, rep.walls


def test_open_network_a_keyed_write_is_a_card_and_an_anonymous_one_is_silent(make_tls_launch) -> None:
    """*"Open network + keyed write (finding 4)."* as R15-10-A11 rules it (*"`open network` + POST to a setup host = a
    card."*; an anonymous POST under `open network` stays silent, K4): the keyed POST files a card, nothing reaches the
    origin and the held key is not read before his answer, and his deny is a 403; the anonymous POST is forwarded."""
    from levain.chat_egress import classify

    from .tls_rig import KEYED
    L = make_tls_launch(secret="HELDKEYOPEN9", mode=classify.OPEN_NETWORK)
    body = b"x=1"

    def post(host: str) -> bytes:
        return L.raw(f"POST /w HTTP/1.1\r\nHost: {host}:{L.origin_port}\r\nContent-Type: text/plain\r\nContent-Length: "
                     f"{len(body)}\r\nConnection: close\r\n\r\n".encode() + body, connect_host=host, sni=host)
    seen: dict = {}

    def answer() -> None:
        end = time.monotonic() + 10
        while not L.desk.filed and time.monotonic() < end:
            time.sleep(0.05)
        seen["before"] = (L.origin.touched(), L.store.reads)
        L.desk.answer["card-1"] = "denied"
    t = threading.Thread(target=answer, daemon=True)
    t.start()
    resp = post(KEYED)
    t.join(10)
    print("RUN R15-10-A11 open network keyed write:", resp[:40], "cards", len(L.desk.filed), "before answer", seen)
    assert len(L.desk.filed) == 1 and resp.startswith(b"HTTP/1.1 403"), resp[:200]
    before, reads = seen["before"]
    assert before["conns"] == 0 and before["bytes"] == 0 and reads == 0, seen
    L.origin.assert_untouched()
    anon = post(UNKEYED)                                 # the control: K4, silent under open network
    assert anon.startswith(b"HTTP/1.1 200") and len(L.desk.filed) == 1, anon[:200]
    assert [(r.method, r.target, r.body) for r in L.origin.reqs] == [("POST", "/w", body)]
