"""CH-D2 S2: Levain's glue <-> decider lines (`levain.chat_egress.wire`), the contract S4 (the glue), S5a (C0 +
records) and S5b (the decider) build on in parallel.

No row of its own names these formats; they carry design §3.8.5(o) item 2 (the wrapper's request id, the obs and abort
reports), R-10 (`declared_length`, R15-22-4), C-2 (the glue-wall event, the flood bound and its summary: R15-25-3,
R15-27-1, R15-28-1/2, R15-29-2, R15-30-1/2/3, P-9) as fixed formats with strict readers.
"""

from __future__ import annotations

import json

import pytest

from levain.chat_egress import wire
from levain.chat_egress.wire import WireError

RID = "0b6f5f3e-3c1d-4c8e-9a3e-2d7b1f0c9a11"


# --- the observation channel -----------------------------------------------------------------------------------

def test_obs_response_round_trip():
    line = wire.obs_response_line(RID, 200, "application/x-git-upload-pack-advertisement", "123")
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    m = wire.parse_obs_line(line)
    assert m == wire.ObsResponse(RID, 200, "application/x-git-upload-pack-advertisement", "123")


def test_obs_ended_round_trip():
    assert wire.parse_obs_line(wire.obs_ended_line(RID)) == wire.ObsEnded(RID)


def test_obs_absent_headers_are_null():
    m = wire.parse_obs_line(wire.obs_response_line(RID, 204, None, None))
    assert m.content_type is None and m.content_length is None


@pytest.mark.parametrize("obj", [
    {"t": "resp", "request_id": RID, "status": 200},                                             # missing fields
    {"t": "resp", "request_id": RID, "status": 200, "content_type": None, "content_length": None, "x": 1},
    {"t": "resp", "request_id": "1", "status": 200, "content_type": None, "content_length": None},
    {"t": "resp", "request_id": RID, "status": 99, "content_type": None, "content_length": None},
    {"t": "resp", "request_id": RID, "status": True, "content_type": None, "content_length": None},
    {"t": "resp", "request_id": RID, "status": 200, "content_type": 5, "content_length": None},
    {"t": "ended", "request_id": RID, "cause": "client"},                                        # names no cause
    {"t": "req", "request_id": RID},
])
def test_obs_reader_is_strict(obj):
    with pytest.raises(WireError):
        wire.parse_obs_line(json.dumps(obj).encode() + b"\n")


@pytest.mark.parametrize("raw", [b"", b"not json\n", b"[1]\n", b'{"t":"ended","request_id":"%s"}' % RID.encode(),
                                 b'{"t":"ended","request_id":"%s","request_id":"%s"}\n' % (RID.encode(),
                                                                                           RID.encode())])
def test_obs_reader_refuses_malformed_lines(raw):
    with pytest.raises(WireError):
        wire.parse_obs_line(raw)


def test_obs_request_id_is_the_wrappers_uuid():
    assert wire.is_request_id(RID)
    assert not wire.is_request_id(RID.upper())               # crypto.randomUUID() is lower-case
    assert not wire.is_request_id("0b6f5f3e-3c1d-1c8e-9a3e-2d7b1f0c9a11")   # not version 4


@pytest.mark.parametrize("raw,n", [
    ("0", 0), ("123", 123), (str(2 ** 63 - 1), 2 ** 63 - 1),
    (str(2 ** 63), None), ("-1", None), ("+1", None), ("1.0", None), (" 1", None), ("1, 1", None), ("", None),
    (None, None), ("0x10", None), ("١٢", None),              # non-ASCII digits are not a decimal integer here
])
def test_declared_length_r15_22_4(raw, n):
    assert wire.declared_length(raw) == n


# --- the glue's event file --------------------------------------------------------------------------------------

@pytest.mark.parametrize("raw,limit,out", [
    ("example.com", 253, "example.com"),
    ("a\x00b\x1f\x7f\x9f", 253, "a\\x00b\\x1f\\x7f\\x9f"),
    ("café", 253, "caf\\xe9"),
    ("☃", 253, "\\u2603"),
    ("\U0001f600", 253, "\\ud83d\\ude00"),                  # UTF-16 code units, as the glue's JS string holds them
    ("a\\x41", 253, "a\\x5cx41"),                            # a literal backslash cannot spoof an escape
    ("x" * 300, 253, "x" * 253),                            # cut to the limit first, then escaped
    ("\x00" * 70, 64, "\\x00" * 64),
])
def test_escape_text_vectors(raw, limit, out):
    assert wire.escape_text(raw, limit) == out


def test_published_escape_vectors_hold():
    """`wire.ESCAPE_VECTORS` is what the glue's JS escaper is tested against (one format, two languages)."""
    assert len(wire.ESCAPE_VECTORS) >= 6
    for raw, limit, out in wire.ESCAPE_VECTORS:
        assert wire.escape_text(raw, limit) == out


def test_glue_req_event_round_trip():
    e = wire.GlueReq(RID, "GET", "example.com", 2, 1, ("x-levain-audit",))
    assert wire.parse_glue_event(wire.glue_event_line(e)) == e


def test_glue_req_event_bounds_dropped_names():
    with pytest.raises(WireError):
        wire.parse_glue_event(wire.glue_event_line(wire.GlueReq(RID, "GET", "h", 0, 9, tuple(f"x-levain-{i}"
                                                                                           for i in range(9)))))
    with pytest.raises(WireError):
        wire.parse_glue_event(wire.glue_event_line(wire.GlueReq(RID, "GET", "h", 0, 0, ("x",))))  # count < names


@pytest.mark.parametrize("reason", sorted(wire.GLUE_WALL_REASONS))
def test_glue_wall_event_round_trip(reason):
    e = wire.GlueWall(RID, "GET", "example.com", reason)
    assert wire.parse_glue_event(wire.glue_event_line(e)) == e


def test_glue_wall_event_without_a_request():
    """A CONNECT walled at srt's `filter` has no request yet: no id, no method."""
    e = wire.GlueWall(None, None, "example.com", "port")
    assert wire.parse_glue_event(wire.glue_event_line(e)) == e


@pytest.mark.parametrize("bad", [
    {"t": "wall", "request_id": RID, "method": "GET", "host": "h", "reason": "made-up"},
    {"t": "wall", "request_id": RID, "method": "GET", "host": "h", "reason": "port", "path": "/x"},
    {"t": "wall", "request_id": RID, "method": "GET", "host": "h" * 254, "reason": "port"},
    {"t": "wall", "request_id": RID, "method": "GET", "host": "café", "reason": "port"},   # unescaped
    {"t": "req", "request_id": RID, "method": "GET", "host": "h", "query_name_count": 0,
     "dropped_count": 0, "dropped_names": [], "url": "https://h/?token=x"},
])
def test_glue_event_reader_is_strict(bad):
    with pytest.raises(WireError):
        wire.parse_glue_event(json.dumps(bad).encode() + b"\n")


def test_glue_summary_round_trip_and_bounds():
    s = wire.GlueSummary(minute=1_792_871_520, minute_count=3, total=104, by_reason={"port": 100, "scheme": 4},
                         hosts={"a.example": 103, "<unparsed>": 1}, overflow=0, final=False)
    assert wire.parse_glue_event(wire.glue_event_line(s)) == s
    big = {f"h{i}.example": 1 for i in range(wire.HOST_MAP_LIMIT + 1)}
    with pytest.raises(WireError):
        wire.parse_glue_event(wire.glue_event_line(wire.GlueSummary(60, 65, 65, {"port": 65}, big, 0, False)))
    with pytest.raises(WireError):      # the minute is a tumbling wall-clock minute (R15-30-1)
        wire.parse_glue_event(wire.glue_event_line(wire.GlueSummary(61, 1, 1, {"port": 1}, {"h": 1}, 0, False)))
    with pytest.raises(WireError):      # per-reason counters are running totals that sum to the total (R15-30-2)
        wire.parse_glue_event(wire.glue_event_line(wire.GlueSummary(60, 1, 2, {"port": 1}, {"h": 2}, 0, False)))
    with pytest.raises(WireError):      # the host map plus the overflow account for every wall
        wire.parse_glue_event(wire.glue_event_line(wire.GlueSummary(60, 1, 2, {"port": 2}, {"h": 1}, 0, False)))


def test_glue_final_flush_with_zero_walls_p9():
    """P-9: a clean launch end always writes the final flush, total 0 when no wall fired."""
    s = wire.GlueSummary(minute=1_792_871_520, minute_count=0, total=0, by_reason={}, hosts={}, overflow=0, final=True)
    assert wire.parse_glue_event(wire.glue_event_line(s)) == s
    with pytest.raises(WireError):      # R15-30-1: a non-final zero-wall minute emits no summary
        wire.parse_glue_event(wire.glue_event_line(wire.GlueSummary(60, 0, 0, {}, {}, 0, False)))


def test_glue_limits_are_the_designs():
    assert (wire.DETAILED_WALL_LIMIT, wire.HOST_MAP_LIMIT, wire.HOST_LIMIT, wire.DROPPED_NAME_LIMIT,
            wire.DROPPED_NAMES_SHOWN) == (100, 64, 253, 64, 8)


# --- verdict frames to srt ----------------------------------------------------------------------------------------

def test_allow_frame_shape():
    f = wire.allow_frame(5, remove_headers=["x-original-url", "x-levain-audit", "x-original-url"])
    assert f == {"t": "verdict", "id": 5, "action": "allow", "removeHeaders": ["x-original-url", "x-levain-audit"]}
    assert wire.allow_frame(5) == {"t": "verdict", "id": 5, "action": "allow"}     # empty optionals left out
    k = wire.allow_frame(5, cred="levain", credential="tok_abc")
    assert k["cred"] == "levain" and k["credential"] == "tok_abc"


@pytest.mark.parametrize("kw", [
    {"credential": "x"},                                 # cred and credential together or not at all
    {"cred": "levain", "credential": "has space"},
    {"remove_headers": ["bad name"]},
    {"remove_headers": ["content-length"]},              # srt refuses an allow that removes framing
])
def test_allow_frame_refuses_what_srt_would(kw):
    with pytest.raises(WireError):
        wire.allow_frame(1, **kw)


def test_remove_headers_dedupe_by_fold():
    f = wire.allow_frame(1, remove_headers=["x_original_url", "x-original-url", "x.original.url"])
    assert f["removeHeaders"] == ["x-original-url"]


def test_deny_and_need_body_frames():
    assert wire.deny_frame(2, 403, "card: POST") == {"t": "verdict", "id": 2, "action": "deny", "status": 403,
                                                     "reason": "card: POST"}
    assert wire.need_body_frame(3, 2 << 20) == {"t": "verdict", "id": 3, "action": "need_body", "max": 2 << 20}
    for bad in [lambda: wire.deny_frame(2, 200, "x"), lambda: wire.deny_frame(2, 403, "a\nb"),
                lambda: wire.deny_frame(2, 403, "x" * 1025), lambda: wire.need_body_frame(3, 0),
                lambda: wire.need_body_frame(3, (32 << 20) + 1), lambda: wire.deny_frame(0, 403, "x")]:
        with pytest.raises(WireError):
            bad()


def test_encode_frame_is_length_prefixed_json():
    raw = wire.encode_frame({"t": "hello", "proto": 1, "allowedDomains": [], "deniedDomains": []})
    n = int.from_bytes(raw[:4], "big")
    assert n == len(raw) - 4 and json.loads(raw[4:]) == {"t": "hello", "proto": 1, "allowedDomains": [],
                                                         "deniedDomains": []}


# --- L3 r1 fixes (chd2_s2/L3_r1_findings.md) ------------------------------------------------------------------------

def test_m2_remove_headers_aggregate_bytes_limit():
    assert wire.MAX_REMOVE_BYTES == 64 << 10
    assert wire.allow_frame(1, ["x" * (64 << 10)])["removeHeaders"] == ["x" * (64 << 10)]
    with pytest.raises(WireError):
        wire.allow_frame(1, ["x" * ((64 << 10) + 1)])
    with pytest.raises(WireError):
        wire.allow_frame(1, ["a" * (32 << 10), "b" * ((32 << 10) + 1)])
    # counted after fold + dedupe: two spellings of one name are one name
    half = (40 << 10)
    assert wire.allow_frame(1, ["a" * half, "A" * half])["removeHeaders"] == ["a" * half]


@pytest.mark.parametrize("raw,n", [
    ("0" * 5000, 0), ("0" * 5000 + "5", 5), ("9" * 5000, None), ("0" * 4000 + str(2 ** 63 - 1), 2 ** 63 - 1),
    ("0" * 4000 + str(2 ** 63), None), ("1" + "0" * 19, None),
])
def test_m3_declared_length_never_raises_on_long_digits(raw, n):
    assert wire.declared_length(raw) == n


def test_l1_wire_fold_is_ascii_only():
    assert wire.fold_name("Key") == "Key"


@pytest.mark.parametrize("bad", ["a\ud800b", "\udfff"])
def test_l2_lone_surrogate_is_a_wire_error(bad):
    with pytest.raises(WireError):
        wire.deny_frame(2, 403, bad)
    with pytest.raises(WireError):
        wire.encode_frame({"t": "verdict", "id": 2, "action": "deny", "status": 403, "reason": bad})


def test_codex_r1_low7_an_integer_past_the_digit_limit_is_a_wire_error():
    with pytest.raises(WireError):
        wire.parse_obs_line(b'{"t": "x", "n": ' + b"9" * 5000 + b"}\n")


# --- S4 F5, the wire half: the resp fields' bound is the glue's -------------------------------------------------

def test_content_field_limit_is_the_glues():
    """The glue cuts `content_type` / `content_length` to its CONTENT_FIELD_LIMIT; the reader's bound is that same
    number, read from glue.mjs's source so the two cannot drift apart."""
    import re
    from pathlib import Path
    glue = (Path(wire.__file__).parent / "node" / "glue.mjs").read_text(encoding="utf-8")
    found = re.findall(r"^export const CONTENT_FIELD_LIMIT = (\d+);", glue, flags=re.M)
    assert found == [str(wire.CONTENT_FIELD_LIMIT)]


@pytest.mark.parametrize("field", ["content_type", "content_length"])
def test_obs_resp_fields_are_bounded_by_the_glues_limit(field):
    at = {"t": "resp", "request_id": RID, "status": 200, "content_type": None, "content_length": None}
    at[field] = "x" * wire.CONTENT_FIELD_LIMIT
    assert getattr(wire.parse_obs_line(json.dumps(at).encode() + b"\n"), field) == "x" * wire.CONTENT_FIELD_LIMIT
    # an astral character is one code point here and two UTF-16 units in the glue's slice: the glue's cut
    # output always fits the reader's bound
    at[field] = "\U0001F600" * wire.CONTENT_FIELD_LIMIT
    assert getattr(wire.parse_obs_line(json.dumps(at).encode() + b"\n"), field) == at[field]
    for over in ("x" * (wire.CONTENT_FIELD_LIMIT + 1), "x" * 100_000):
        at[field] = over
        with pytest.raises(WireError):
            wire.parse_obs_line(json.dumps(at).encode() + b"\n")


def test_a_socks_wall_is_a_glue_wall_reason() -> None:
    line = wire.glue_event_line(wire.GlueWall(None, None, "<socks>", "socks"))
    assert wire.parse_glue_event(line) == wire.GlueWall(None, None, "<socks>", "socks")
