"""CH-D2 S9: R15-17-11 and R15-19-6: one request carrying 16 credential values (Authorization x4 in mixed case, Cookie
x4 on separate lines, X-Api-Key / x-api-key / X_Api_Key / X.Api.Key, Api-Key, X-Auth-Token x2, Proxy-Authorization)
through the real glue and decider (plain path) reaches the origin with no credential byte. `Authorization :` is srt's
400. The keyed path (exactly one credential header, the held key) and the TLS path need a held key and a TLS origin:
not in this file.
"""

from __future__ import annotations

import pytest

from levain.chat_egress import classify
from levain.chat_egress.strip import fold

pytestmark = pytest.mark.chat_egress_srt

CRED = [
    ("Authorization", "Bearer AUTHVAL1"), ("authorization", "Basic AUTHVAL2"), ("AUTHORIZATION", "AUTHVAL3"),
    ("Authorization", "AUTHVAL4"),
    ("Cookie", "c1=COOKVAL1"), ("Cookie", "c2=COOKVAL2"), ("Cookie", "c3=COOKVAL3"), ("Cookie", "c4=COOKVAL4"),
    ("X-Api-Key", "APIKEYVAL1"), ("x-api-key", "APIKEYVAL2"), ("X_Api_Key", "APIKEYVAL3"), ("X.Api.Key", "APIKEYVAL4"),
    ("Api-Key", "APIKEYVAL5"), ("X-Auth-Token", "AUTHTOKVAL1"), ("X-Auth-Token", "AUTHTOKVAL2"),
    ("Proxy-Authorization", "Basic PROXYVAL1"),
]
VALUES = [v.rsplit(" ", 1)[-1].split("=")[-1] for _, v in CRED]


def _send(L, headers, path="/m"):
    P = L.origin_port
    head = b"GET http://pub.rb.test:%d%s HTTP/1.1\r\nHost: pub.rb.test:%d\r\n" % (P, path.encode(), P)
    head += b"".join(f"{k}: {v}\r\n".encode() for k, v in headers) + b"\r\n"
    return L.raw(head)


@pytest.mark.parametrize("mode", [classify.GATED, classify.OPEN_NETWORK])
def test_sixteen_credential_values_reach_the_origin_as_none(make_origin, make_launch_, mode) -> None:
    origin = make_origin()
    L = make_launch_(origin, mode=mode)
    resp = _send(L, CRED)
    assert resp.startswith(b"HTTP/1.1 200"), resp[:200]
    assert L.stop() == 0
    assert len(origin.reqs) == 1
    seen = origin.reqs[0]
    # control: the rig sees a header the strip leaves, so an empty credential list is not blindness
    names = {fold(k) for k, _ in seen.headers}
    assert "host" in names
    for k, v in seen.headers:
        assert fold(k) not in {"authorization", "proxy-authorization", "cookie", "x-api-key", "api-key",
                               "x-auth-token"}, (k, v)
    for val in VALUES:
        assert val.encode() not in seen.raw_head, val


def test_a_credential_header_with_a_space_before_the_colon_is_srts_400_and_reaches_nothing(make_origin,
                                                                                         make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    resp = _send(L, [("Authorization ", "AUTHSPACEVAL")])
    assert L.stop() == 0
    assert resp.startswith(b"HTTP/1.1 400"), resp[:200]
    origin.assert_untouched()


def test_the_oracle_is_red_when_the_space_before_the_colon_is_forwarded(make_origin, make_leaky_plain) -> None:
    origin = make_origin()
    L = make_leaky_plain(origin)
    _send(L, [("Authorization ", "AUTHSPACEVAL")])
    with pytest.raises(AssertionError) as red:
        origin.assert_untouched()
    print("RUN RED CONTROL space before the colon:", str(red.value)[:200])


def test_a_control_header_the_strip_does_not_list_is_forwarded(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    resp = _send(L, [("X-Deploy-Key", "DEPLOYVAL"), ("Authorization", "AUTHVAL")])
    assert resp.startswith(b"HTTP/1.1 200")
    assert L.stop() == 0
    got = {k.lower() for k, _ in origin.reqs[0].headers}
    assert "x-deploy-key" in got and "authorization" not in got      # the backstop's named edge (strip.py): not a match
