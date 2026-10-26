"""CH-D2 S9 session 2b: the leak sweep over TLS (R15-17-8, R15-21-2, R15-21-3, R15-22-3), the full secret set of
`test_leak_sweep.SECRETS` plus a held key, through the production spawn (`tls_rig.TlsLaunch`). Every request is a
CONNECT and TLS to the Levain leaf, to the unkeyed host and to the keyed one; every file of the run (the glue's event
file and stderr, the dispatch store), the logging and the filed cards are swept.

The sweep is shown able to fail twice: the control sweeps what the ORIGIN received (an exempt signed-URL read forwards
`X-Amz-Signature`, the held key reaches its own host), and `test_the_sweep_is_red_when_the_decider_logs_the_target`
reruns the same requests with a deliberately leaky `parse.parse` and `advert.content_type_class` that log what they
are handed.
"""

from __future__ import annotations

import json
import logging

import pytest

from levain.chat_egress import advert, classify, parse

from .rig import autoanswer, sweep
from .test_leak_sweep import SECRETS
from .tls_rig import KEYED, UNKEYED

pytestmark = pytest.mark.chat_egress_srt

HELD = "HELDKEYTLS77qq"
ALL = dict(SECRETS, **{"held-key": HELD})


def _reflect(seen):
    # the query values and the credential header value, reflected into response headers (R15-21-3)
    return [b"X-Echo: " + seen.target.partition("?")[2].encode("latin-1", "replace")[:200],
            b"X-Echo-Auth: " + dict((k.lower(), v) for k, v in seen.headers).get("authorization", "-").encode()]


def _ct(seen):
    return "text/plain; charset=utf-8; reflected=" + SECRETS["reflected-ct-param"] + "; q=" + \
        seen.target.partition("?")[2][:60]


def _requests(L, host: str) -> list[bytes]:
    P = L.origin_port
    auth = [("Authorization", "Bearer " + SECRETS["authorization-value"]), ("Cookie", "s=" + SECRETS["cookie-value"])]
    out = [L.get("/plain?page=1", host=host, headers=auth),     # forwarded to both hosts: the held key flows on KEYED
           L.get(f"/dl/{SECRETS['path-segment']}?{SECRETS['query-name']}=v1", host=host, headers=auth),
           L.get(f"/x?token={SECRETS['token-value']}", host=host, headers=auth),
           L.get(f"/y?sig={SECRETS['sig-value']}", host=host, headers=auth),
           L.get(f"/z?X-Amz-Signature={SECRETS['amz-signature-value']}&X-Amz-Credential=cred%2Fscope", host=host,
                 headers=auth)]
    body = b"body=" + SECRETS["sig-value"].encode()
    h = "".join(f"{k}: {v}\r\n" for k, v in auth)
    out.append(L.raw(f"POST /p?token={SECRETS['token-value']} HTTP/1.1\r\nHost: {host}:{P}\r\n{h}Content-Length: "
                     f"{len(body)}\r\nConnection: close\r\n\r\n".encode() + body, connect_host=host, sni=host))
    # a target the decider denies on a parse failure, the secrets beside the bad escape
    out.append(L.raw(f"GET /f?token={SECRETS['token-value']}&a=%zz HTTP/1.1\r\nHost: {host}:{P}\r\n{h}"
                     "Connection: close\r\n\r\n".encode(), connect_host=host, sni=host))
    return out


def _run(L) -> list[bytes]:
    stop = autoanswer(L, "denied")
    try:
        res = _requests(L, UNKEYED) + _requests(L, KEYED)
        L.stop()
    finally:
        stop.set()
    return res


@pytest.mark.parametrize("mode", [classify.GATED, classify.OPEN_NETWORK])
def test_no_secret_byte_in_any_artifact_of_a_tls_run(make_tls_launch, logs, mode) -> None:
    L = make_tls_launch(secret=HELD, mode=mode, reflect=_reflect, content_type=_ct)
    res = _run(L)
    statuses = [r.split(b"\r\n", 1)[0] for r in res]
    print("RUN R15-17-8 TLS statuses:", mode, statuses)
    # the plain read (both hosts) and the exempt signed-URL read (UNKEYED) are forwarded silent: the controls' input
    assert [statuses[i].startswith(b"HTTP/1.1 200") for i in (0, 4, 7)] == [True] * 3, statuses
    outs = [r for r in L.e.records.rows(L.e.launch_id) if r.kind == "egress_outcome"]
    assert outs and {r.content_type_class for r in outs} == {"other"}      # stored as an enum only (R15-21-3)
    cards = json.dumps(L.desk.filed, default=repr).encode()
    hits = sweep(L.dir, ALL, extra={"logging": "\n".join(logs.lines).encode()})
    print("RUN R15-17-8 TLS sweep hits:", hits, "cards", len(L.desk.filed), "key reads", L.store.reads)
    assert hits == [], hits
    # a card carries the path and the query NAMES as sent (R15-21-2); every VALUE stays out of it (R15-22-3)
    values = {k: v for k, v in ALL.items() if k not in ("path-segment", "query-name")}
    assert sweep(L.dir, values, extra={"cards": cards}) == []
    if mode == classify.GATED:
        assert L.desk.filed and SECRETS["path-segment"].encode() in cards
    # the controls: what the ORIGIN received holds the forwarded signed-URL value and the held key, so a leak would show
    got = b"\n".join(s.raw_head + s.body for s in L.origin.reqs)
    assert sweep(L.dir, {"amz": SECRETS["amz-signature-value"], "held": HELD}, extra={"origin": got}) == \
        ["origin: amz", "origin: held"]
    assert L.store.reads >= 1


def test_the_sweep_is_red_when_the_decider_logs_the_target(make_tls_launch, logs, monkeypatch) -> None:
    """The negative control: the same run with a `parse.parse` that logs each frame and an
    `advert.content_type_class` that logs the response's Content-Type (the leaks this sweep exists to catch) is found
    by the sweep, value by value."""
    L = make_tls_launch(secret=HELD, reflect=_reflect, content_type=_ct)
    sound = parse.parse
    leak = logging.getLogger("levain.chat_egress.leaky_test_double")

    def leaky(frame, *a, **kw):
        leak.info("parsing %r", frame)
        return sound(frame, *a, **kw)
    monkeypatch.setattr(parse, "parse", leaky)
    sound_ct = advert.content_type_class                 # the response side: the reflected Content-Type value

    def leaky_ct(value, *a, **kw):
        leak.info("classifying %r", value)
        return sound_ct(value, *a, **kw)
    monkeypatch.setattr(advert, "content_type_class", leaky_ct)
    _run(L)
    hits = sweep(L.dir, ALL, extra={"logging": "\n".join(logs.lines).encode()})
    print("RUN R15-17-8 TLS sweep, leaky parse + content type:", hits)
    found = {h.split(": ", 1)[1] for h in hits if h.startswith("logging: ")}
    assert set(SECRETS) <= found, hits                   # the held key is added after the parse: not in a frame
