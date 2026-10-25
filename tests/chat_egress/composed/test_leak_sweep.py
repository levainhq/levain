"""CH-D2 S9: the leak sweep (R15-17-8, R15-21-2, R15-21-3, R15-22-3). Every artifact of a run of the real glue and
decider is grepped for the secret values: the glue's event file, its stderr, the dispatch store (every sqlite file),
the logging the decider and the service emitted, and the cards the desk was handed. Zero hits is the pass.

The control is the same sweep over what the ORIGIN received: it must find the value a silent read forwards, so the
sweep can fail.
"""

from __future__ import annotations

import json

import pytest

from levain.chat_egress import classify

from .rig import RecordingOrigin, autoanswer, sweep

pytestmark = pytest.mark.chat_egress_srt

SECRETS = {
    # an UNKEYED host's path keeps short readable segments literal (B15) and redacts an identifier-like one (>= 32
    # characters is always one); the keyed-host shape of `SECRET123SHORT` (R15-21-2) needs a held key: not here
    "path-segment": "SECRETSEGMENTxxxxxxxxxxxxxxxxxxxxyyyyyyyy",
    "query-name": "SECRETNAME",          # a NAME is logged as a token unless it is in a named list
    "token-value": "TOKVAL7q3xz",
    "sig-value": "AZSIGVAL9k2m",
    "amz-signature-value": "AMZSIGVALp8w1",
    "authorization-value": "HELDKEYVALUE55xx",
    "cookie-value": "COOKIEVAL31ab",
    "reflected-ct-param": "REFLECTCT4411",
}


def _reflect(seen):
    # an origin that reflects the request's query values and the credential header values into response headers
    return [b"X-Echo: " + seen.target.partition("?")[2].encode("latin-1", "replace")[:200],
            b"X-Echo-Auth: " + dict((k.lower(), v) for k, v in seen.headers).get("authorization", "-").encode()]


def _ct(seen):
    return "text/plain; charset=utf-8; reflected=" + SECRETS["reflected-ct-param"] + "; q=" + \
        seen.target.partition("?")[2][:60]


def _requests(L):
    P = L.origin_port
    auth = {"Authorization": "Bearer " + SECRETS["authorization-value"], "Cookie": "s=" + SECRETS["cookie-value"]}
    out = []
    out.append(L.request("GET", f"/dl/{SECRETS['path-segment']}?{SECRETS['query-name']}=v1", headers=auth))
    out.append(L.request("GET", f"/x?token={SECRETS['token-value']}", headers=auth))
    out.append(L.request("GET", f"/y?sig={SECRETS['sig-value']}", headers=auth))
    out.append(L.request("GET", f"/z?X-Amz-Signature={SECRETS['amz-signature-value']}&X-Amz-Credential=cred%2Fscope",
                         headers=auth))
    out.append(L.request("POST", f"/p?token={SECRETS['token-value']}", body=b"body=" + SECRETS["sig-value"].encode(),
                         headers=auth))
    # a request the decider denies on a parse failure, secrets beside the bad escape
    head = L.raw(b"GET http://pub.rb.test:%d/f?token=%s&a=%%zz HTTP/1.1\r\nHost: pub.rb.test:%d\r\nAuthorization: "
                 b"Bearer %s\r\n\r\n" % (P, SECRETS["token-value"].encode(), P, SECRETS["authorization-value"].encode()))
    out.append(("raw", head[:12], 0.0))
    return out


@pytest.mark.parametrize("mode", [classify.GATED, classify.OPEN_NETWORK])
def test_no_secret_byte_in_any_artifact_of_a_run(make_origin, make_launch_, logs, mode) -> None:
    origin = make_origin(reflect=_reflect, content_type=_ct)
    L = make_launch_(origin, mode=mode)
    stop = autoanswer(L)
    try:
        res = _requests(L)
        code = L.stop()
    finally:
        stop.set()
    assert code == 0
    statuses = [r[0] for r in res[:5]]
    # the exempt signed-URL read is forwarded (silent) in both modes: that is the control's input
    assert statuses[3] == 200, statuses
    assert any(SECRETS["amz-signature-value"].encode() in s.raw_head for s in origin.reqs)
    cards = json.dumps(L.desk.filed, default=repr).encode()
    # the reflecting response was stored as an enum only (R15-21-3), and an outcome row exists, so the sweep is not vacuous
    outs = [r for r in L.records.rows(L.launch_id) if r.kind == "egress_outcome"]
    assert outs and {r.content_type_class for r in outs} == {"other"}
    # the sweep proper: every file of the run and the logging hold none of the eight; a card carries the path and the
    # query NAMES as sent (R15-21-2), so its sweep leaves those two out and holds every VALUE to zero (R15-22-3)
    hits = sweep(L.dir, SECRETS, extra={"logging": "\n".join(logs.lines).encode()})
    assert hits == [], hits
    values = {k: v for k, v in SECRETS.items() if k not in ("path-segment", "query-name")}
    assert sweep(L.dir, values, extra={"cards": cards}) == []
    if mode == classify.GATED:
        assert L.desk.filed and SECRETS["path-segment"].encode() in cards     # the card does show what it should
    # the control: the same sweep finds the value in what the ORIGIN received, so a leak would show
    seen_by_origin = b"\n".join(s.raw_head + s.body for s in origin.reqs)
    assert sweep(L.dir, {"amz": SECRETS["amz-signature-value"]}, extra={"origin": seen_by_origin}) == \
        ["origin: amz"]


def test_the_rows_hold_names_and_shapes_never_values(make_origin, make_launch_) -> None:
    origin = make_origin(reflect=_reflect, content_type=_ct)
    L = make_launch_(origin)
    stop = autoanswer(L)
    try:
        _requests(L)
        L.stop()
    finally:
        stop.set()
    rows = [r for r in L.records.rows(L.launch_id) if r.kind == "egress_attempt"]
    assert rows
    for r in rows:
        assert "?" not in (r.path or "")
        assert SECRETS["path-segment"] not in (r.path or "")
        names = json.loads(r.names or "[]")
        # query NAMES are literal only for the named lists; the backstop-only name is a token
        assert SECRETS["query-name"].lower() not in names
    assert any("token" in json.loads(r.names or "[]") for r in rows)


def test_the_glue_event_file_carries_no_path_and_no_query(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    stop = autoanswer(L)
    try:
        _requests(L)
        L.stop()
    finally:
        stop.set()
    for ln in L.events.read_text().splitlines():
        ev = json.loads(ln)
        assert not ({"path", "url", "query", "target"} & set(ev)), ev
    text = L.events.read_text()
    assert "/dl/" not in text and "/x" not in text and "?" not in text
