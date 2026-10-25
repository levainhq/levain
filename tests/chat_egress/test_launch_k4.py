"""CH-D2 S8 (second slice): row K4's request half, composed through `start_egress` and the real glue: under
`open network` an anonymous POST is forwarded silently and loses `Authorization`; every wall still fires (a port the
glue does not admit, a GET carrying `?token=`: R15-17-10). The gated launch is the control: the same POST is not
forwarded silently.
"""

from __future__ import annotations

import http.client
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from levain.chat_egress import launch as lm

from .test_launch import _start


class RecOrigin:
    def __init__(self) -> None:
        self.seen: list[tuple[str, str, str | None, bytes]] = []
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _rec(self):
                n = int(self.headers.get("content-length") or 0)
                outer.seen.append((self.command, self.path, self.headers.get("Authorization"), self.rfile.read(n)))
                self.send_response(200)
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = _rec

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


def _req(e, method: str, port: int, path: str, body: bytes | None = None) -> int:
    c = http.client.HTTPConnection("127.0.0.1", e.http_port, timeout=10)
    try:
        c.request(method, f"http://pub.s8.test:{port}{path}", body=body,
                  headers={"Host": f"pub.s8.test:{port}", "Authorization": "Bearer sandbox-secret",
                           "content-type": "application/json"})
        r = c.getresponse()
        r.read()
        return r.status
    except (OSError, http.client.HTTPException):
        return -1
    finally:
        c.close()


@pytest.fixture
def origin():
    o = RecOrigin()
    yield o
    o.close()


@pytest.mark.chat_egress_srt
def test_open_network_forwards_an_anonymous_post_without_authorization_and_walls_still_fire(tmp_path, origin,
                                                                                         monkeypatch) -> None:
    e = _start(tmp_path, origin, monkeypatch, open_network=True)
    try:
        post = _req(e, "POST", origin.port, "/w", b'{"a":1}')
        tok = _req(e, "GET", origin.port, "/r?token=abc")
        port = _req(e, "GET", origin.port + 1 if origin.port < 65535 else origin.port - 1, "/p")
    finally:
        out = lm.stop_egress(e)
    events = [json.loads(x) for x in (tmp_path / "events.jsonl").read_text().splitlines()]
    print(f"RUN S8B K4 open: POST {post}, ?token= GET {tok}, other port {port}; origin saw {origin.seen}; "
          f"final flush {out.final_flush}")
    assert post == 200 and origin.seen[0] == ("POST", "/w", None, b'{"a":1}')
    assert tok != 200 and port != 200 and len(origin.seen) == 1
    assert any(ev.get("t") == "wall" and ev.get("reason") == "port" for ev in events)
    assert out.glue_exit == 0 and out.final_flush["total"] >= 1


@pytest.mark.chat_egress_srt
def test_control_gated_the_same_post_is_not_forwarded_silently(tmp_path, origin, monkeypatch) -> None:
    e = _start(tmp_path, origin, monkeypatch, open_network=False)
    try:
        post = _req(e, "POST", origin.port, "/w", b'{"a":1}')
    finally:
        lm.stop_egress(e)
    print(f"RUN S8B K4 gated: POST {post}; origin saw {origin.seen}")
    assert post != 200 and origin.seen == []
