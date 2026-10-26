"""CH-D2 S9 session 2a: the keyed path with a HELD key, over TLS, through the production spawn (`tls_rig.TlsLaunch`).

R15-17-11 / R15-19-6 (keyed half): a keyed host's request carries EXACTLY ONE credential header, the held one, whatever
credential headers the client sent. R15-17-8 / R15-21-3 (held-key half): the held value reaches the origin of its own
host and no file, log line or card of the run. R15-21-2 (keyed half): a short readable path segment on a KEYED host is
redacted in the run's artifacts.
"""

from __future__ import annotations

import pytest

from levain.chat_egress import classify

from .rig import sweep
from .tls_rig import KEYED, UNKEYED

pytestmark = pytest.mark.chat_egress_srt

KEY = "HELDKEYVALUE55xx"
CRED_HEADERS = {"authorization", "proxy-authorization", "cookie", "x-api-key", "x-auth-token", "x-access-token",
                "x-amz-security-token", "private-token", "x-gitlab-token", "x-github-token"}


def _creds(seen) -> list[tuple[str, str]]:
    return [(k, v) for k, v in seen.headers if k.lower() in CRED_HEADERS]


def _ok(resp: bytes) -> None:
    assert resp.startswith(b"HTTP/1.1 200"), resp[:300]


def test_the_held_key_replaces_the_clients_authorization_and_is_the_only_credential_header(make_tls_launch) -> None:
    L = make_tls_launch(secret=KEY)
    _ok(L.get("/a", host=KEYED, headers=[("Authorization", "Bearer from-the-sandbox")]))
    got = _creds(L.origin.reqs[0])
    print("RUN R15-19-6 keyed single:", got)
    assert got == [("authorization", f"Bearer {KEY}")]
    assert L.store.reads == 1


def test_a_multi_valued_credential_set_from_the_client_leaves_exactly_the_held_one(make_tls_launch) -> None:
    L = make_tls_launch(secret=KEY)
    sent = [("Authorization", "Bearer first-CLIENTVAL1"), ("authorization", "Bearer second-CLIENTVAL2"),
            ("AUTHORIZATION", "Basic Q0xJRU5UVkFMMw=="), ("Proxy-Authorization", "Basic CLIENTVAL4"),
            ("Cookie", "s=CLIENTVAL5"), ("Cookie", "t=CLIENTVAL6"), ("X-Api-Key", "CLIENTVAL7"),
            ("X-Auth-Token", "CLIENTVAL8"), ("Private-Token", "CLIENTVAL9"), ("X-Amz-Security-Token", "CLIENTVAL10")]
    _ok(L.get("/m", host=KEYED, headers=sent))
    seen = L.origin.reqs[0]
    got = _creds(seen)
    print("RUN R15-17-11 keyed multi-valued:", got)
    assert got == [("authorization", f"Bearer {KEY}")], got
    assert b"CLIENTVAL" not in seen.raw_head


def test_the_held_key_goes_to_its_own_host_and_to_no_other(make_tls_launch) -> None:
    L = make_tls_launch(secret=KEY)
    _ok(L.get("/a", host=KEYED))
    _ok(L.get("/b", host=UNKEYED, headers=[("Authorization", "Bearer from-the-sandbox")]))
    by = {r.target: r for r in L.origin.reqs}
    assert _creds(by["/a"]) == [("authorization", f"Bearer {KEY}")]
    assert KEY.encode() not in by["/b"].raw_head
    print("RUN unkeyed host credential headers at origin:", _creds(by["/b"]))


def _reflect(seen):
    return [b"X-Echo-Auth: " + dict((k.lower(), v) for k, v in seen.headers).get("authorization", "-").encode()]


def test_the_leak_sweep_with_a_held_key_finds_the_value_only_at_the_origin(make_tls_launch, logs) -> None:
    from .rig import autoanswer
    L = make_tls_launch(secret=KEY, reflect=_reflect)
    stop = autoanswer(L, "denied")
    try:
        for i in range(3):
            _ok(L.get(f"/k{i}?page={i}", host=KEYED, headers=[("Authorization", "Bearer from-the-sandbox")]))
        L.stop()
    finally:
        stop.set()
    cards = "\n".join(repr(c) for c in L.desk.filed).encode()
    hits = sweep(L.dir, {"held key": KEY}, {"logging": "\n".join(logs.lines).encode(), "cards": cards})
    print("RUN R15-17-8 held-key sweep hits:", hits, "reads", L.store.reads)
    assert hits == []
    assert KEY.encode() in b"".join(r.raw_head for r in L.origin.reqs)          # the control: what the origin got
    assert sweep(L.dir, {"held key": KEY}, {"origin": b"".join(r.raw_head for r in L.origin.reqs)}) == \
        ["origin: held key"]


def test_a_short_path_segment_on_a_keyed_host_is_redacted_in_every_artifact(make_tls_launch, logs) -> None:
    from .rig import autoanswer
    L = make_tls_launch(secret=KEY)
    stop = autoanswer(L, "denied")
    try:
        resp = L.get("/dl/SECRET123SHORT?SECRETNAME=v", host=KEYED)
        L.stop()
    finally:
        stop.set()
    cards = "\n".join(repr(c) for c in L.desk.filed).encode()
    secrets = {"short segment": "SECRET123SHORT", "query name": "SECRETNAME"}
    hits = sweep(L.dir, secrets, {"logging": "\n".join(logs.lines).encode()})
    print("RUN R15-21-2 keyed short segment:", resp[:20], "cards", len(L.desk.filed), "hits", hits)
    assert hits == []
    assert len(L.desk.filed) == 1 and b"SECRET123SHORT" in cards      # the card shows the path to its reader, by design


def test_open_network_mode_keeps_the_held_key_off_an_unkeyed_host_and_on_its_own(make_tls_launch) -> None:
    L = make_tls_launch(secret=KEY, mode=classify.OPEN_NETWORK)
    _ok(L.get("/a", host=KEYED))
    _ok(L.get("/b", host=UNKEYED))
    by = {r.target: r for r in L.origin.reqs}
    assert _creds(by["/a"]) == [("authorization", f"Bearer {KEY}")] and _creds(by["/b"]) == []
