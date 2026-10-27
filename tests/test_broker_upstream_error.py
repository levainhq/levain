"""A brokered call the vendor refuses leaves the vendor's error in the exchange record (`upstream_error`): its type, a
redacted 200-character message and its request id; the client still receives the body byte for byte. Each case drives
the real `Keys._exchange` against the scripted upstream of test_broker_keys_upstream."""
from __future__ import annotations

import asyncio
import json
import socket

import pytest

from levain.broker import keys as K
from levain.broker import redact as RD
from tests.test_broker_keys_upstream import (BODY_Q, SECRET, _q, _Rig, _run, _scripted, _types, _Up, body_frames)

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

#: the body api.anthropic.com answered on 2026-10-27 (1027-14 b400)
MEASURED = (b'{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too low to '
            b'access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."},'
            b'"request_id":"req_011CUabcDEFghi1234567890"}')


def _http(status: str, body: bytes, ctype: bytes = b"application/json") -> bytes:
    return (b"HTTP/1.1 " + status.encode() + b"\r\ncontent-type: " + ctype + b"\r\ncontent-length: "
            + str(len(body)).encode() + b"\r\n\r\n" + body)


class _Log:
    """Stands in for gw.jsonl: the records `_log_exchange` writes."""

    def __init__(self) -> None:
        self.records: list = []

    def write(self, obj: dict) -> None:
        self.records.append(obj)


def _exchange(monkeypatch, response: bytes):
    """(frames the client got, the exchange record) for one exchange the upstream answers with `response`."""
    up = _Up()

    async def go(d: str):
        _scripted(monkeypatch, up)
        async with _Rig(d) as rig:
            rig.k.gw_log = log = _Log()
            rig.a.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)   # got() reads after the run: room for it
            rig.b.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            rig.front.feed_data(body_frames())
            up.feed(response)
            await asyncio.wait_for(rig.run(_q(BODY_Q)), 5)
            rig.k._log_exchange(rig.conn)
            (rec,) = log.records
            return await rig.got(), rec

    return _run(go)


def _relayed(frames: list) -> bytes:
    return b"".join(p for t, p in frames if t == b"b")


def test_the_measured_400_is_recorded(monkeypatch):
    frames, rec = _exchange(monkeypatch, _http("400 Bad Request", MEASURED))
    assert rec["status"] == 400
    assert rec["upstream_error"] == {
        "type": "invalid_request_error",
        "message": "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to "
                   "upgrade or purchase credits.",
        "request_id": "req_011CUabcDEFghi1234567890"}
    assert _relayed(frames) == MEASURED                      # the client's bytes are the upstream's
    assert _types(frames) == b"ARbe"


def test_a_2xx_carries_null(monkeypatch):
    _, rec = _exchange(monkeypatch, _http("200 OK", b'{"error":{"type":"x","message":"m"}}'))
    assert "upstream_error" in rec and rec["upstream_error"] is None


@pytest.mark.parametrize("response", [
    _http("500 Internal Server Error", b"<html>oops</html>", b"text/html"),
    _http("400 Bad Request", b"not json"),
    _http("400 Bad Request", b'{"nope":1}'),
    _http("400 Bad Request", b'[1,2]'),
    _http("400 Bad Request", b""),
    _http("400 Bad Request", b'{"error":"flat"}'),
], ids=["html", "not-json", "no-error", "array", "empty", "error-not-object"])
def test_a_body_that_is_not_the_vendors_json_error_is_null(monkeypatch, response):
    _, rec = _exchange(monkeypatch, response)
    assert rec["upstream_error"] is None


def test_an_oversized_body_is_null_and_relayed_whole(monkeypatch):
    body = json.dumps({"error": {"type": "e", "message": "m"}, "pad": "x" * K.UPSTREAM_ERROR_BODY_MAX}).encode()
    frames, rec = _exchange(monkeypatch, _http("400 Bad Request", body))
    assert rec["upstream_error"] is None and _relayed(frames) == body


def test_a_body_at_the_limit_is_read(monkeypatch):
    base = {"error": {"type": "e", "message": "m"}, "pad": ""}
    n = K.UPSTREAM_ERROR_BODY_MAX - len(json.dumps(base, separators=(",", ":")))
    base["pad"] = "x" * n
    body = json.dumps(base, separators=(",", ":")).encode()
    assert len(body) == K.UPSTREAM_ERROR_BODY_MAX
    _, rec = _exchange(monkeypatch, _http("400 Bad Request", body))
    assert rec["upstream_error"] == {"type": "e", "message": "m", "request_id": None}


def test_the_fields_are_shape_checked(monkeypatch):
    body = json.dumps({"error": {"type": "Bad Type!", "message": "short"}, "request_id": "req_has space"}).encode()
    _, rec = _exchange(monkeypatch, _http("429 Too Many Requests", body))
    assert rec["upstream_error"] == {"type": None, "message": "short", "request_id": None}


def test_a_json_content_type_with_parameters_counts(monkeypatch):
    _, rec = _exchange(monkeypatch, _http("400 Bad Request", MEASURED, b"Application/JSON; charset=utf-8"))
    assert rec["upstream_error"]["type"] == "invalid_request_error"


def test_the_message_is_cut_at_200_characters(monkeypatch):
    body = json.dumps({"error": {"type": "e", "message": "ab " * 200}}).encode()
    _, rec = _exchange(monkeypatch, _http("400 Bad Request", body))
    assert rec["upstream_error"]["message"] == ("ab " * 200)[:200]


def test_a_token_shaped_run_is_redacted(monkeypatch):
    msg = "key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX is bad, short-run-ok"
    body = json.dumps({"error": {"type": "authentication_error", "message": msg}}).encode()
    _, rec = _exchange(monkeypatch, _http("401 Unauthorized", body))
    assert rec["upstream_error"]["message"] == "key <redacted> is bad, short-run-ok"


def test_an_echoed_credential_is_redacted_by_the_brokers_scanner(monkeypatch):
    msg = "bad key " + SECRET + " end"
    body = json.dumps({"error": {"type": "authentication_error", "message": msg}}).encode()
    frames, rec = _exchange(monkeypatch, _http("401 Unauthorized", body))
    assert rec["upstream_error"]["message"] == RD.MARKER.decode()
    assert SECRET.encode() not in json.dumps(rec).encode() and SECRET.encode() not in _relayed(frames)
    assert rec["redactions"] == 1   # the relay's count; the message scan adds none
