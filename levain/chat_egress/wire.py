"""The lines Levain's glue (Node, over srt 0.0.81) and Levain's decider (Python) exchange, besides srt's own decider
protocol, and the verdict frames the decider sends srt.

Design §3.8.5(o) item 2 (the wrapper's request id), R-10 (the outcome and ended-before-outcome reports, the join by
request id, `declared_length`), C-2 (the glue-wall event, the flood bound and its summary), as amended through R15-30
and the CH-D2 plan rulings (P-9). One format per line kind; each reader here is strict (an unknown field, a missing
one, a wrong type or a value past its bound is a WireError), because a reader that tolerates drift lets a glue bug
pass as data. The encoders are the reference for the glue's JS writer, and `ESCAPE_VECTORS` the vectors for its escaper.

THE OBSERVATION CHANNEL (glue -> decider, the second fd; one JSON object per line, `\n`-terminated, UTF-8):
  {"t":"resp","request_id":<id>,"status":<100..599>,"content_type":<str|null>,"content_length":<str|null>}
      written inside the allow's `onResponse(status, headers)`, before srt relays the response head (so it is in the
      channel before the client can send its next request). `content_type` / `content_length` are srt's normalised
      header values, raw, each cut by the glue to CONTENT_FIELD_LIMIT (a longer one is a WireError): the decider
      evaluates the advertisement predicate on the first and parses the second with `declared_length`, and persists
      NEITHER raw (R15-21-3).
  {"t":"ended","request_id":<id>}
      written when the request's abort signal fires (a) while the decision is pending, or (b) after the wrapper
      returned allow and before `onResponse`; never after a deny, a glue wall or an outcome. It names no cause (one
      abort signal stands for more than one, R15-29-1). The decider writes `egress_ended_before_outcome` from it.
  <id> is the wrapper's `crypto.randomUUID()`, the one `x-levain-audit` value of the decider frame (item 2).

THE GLUE'S EVENT FILE (glue only; JSON lines; NO path, NO query value, NO header value, NO raw URL: R15-17-8,
R15-21-2):
  {"t":"req","request_id","method","host","query_name_count","dropped_count","dropped_names"}
      one per request the wrapper sees; `dropped_*` are the inbound `x-levain-` names step (2) removed: the count,
      and up to DROPPED_NAMES_SHOWN of them, each cut to DROPPED_NAME_LIMIT and escaped (R15-25-2).
  {"t":"wall","request_id","method","host","reason"}
      a glue wall (C-2), detailed: at most DETAILED_WALL_LIMIT per launch IN TOTAL (R15-27-1). `request_id` and
      `method` are null for a wall at srt's connection `filter` (a CONNECT has no request yet). `host` is
      `URL.hostname` (or "<unparsed>"), cut to HOST_LIMIT and escaped (R15-25-3).
  {"t":"summary","minute","minute_count","total","by_reason","hosts","overflow","final"}
      past the detailed limit, at most one per TUMBLING wall-clock minute with at least one wall (R15-30-1);
      `minute` is that minute's start in epoch seconds; `total` the launch's running wall total (K1 reads it);
      `by_reason` RUNNING per-reason totals (R15-30-2); `hosts` the bounded host map (<= HOST_MAP_LIMIT entries, no
      eviction, "<unparsed>" takes one ordinary slot) with running per-host counts; `overflow` the walls whose host
      was not in the map (the design's `other` bucket and its overflow wall count are one number since R15-28-2).
      `final: true` is the clean-end flush, written at every clean launch end, total 0 when no wall fired (P-9).
"""

from __future__ import annotations

import json
import re
import struct
from dataclasses import dataclass, field
from typing import Iterable, Mapping

#: The wrapper's id header on the decider frame (item 2, step 3). The origin never receives it (step 5).
AUDIT_HEADER = "x-levain-audit"
#: Every inbound name whose srt fold starts with this is dropped by the wrapper before srt builds the frame (step 2).
LEVAIN_HEADER_PREFIX = "x-levain-"

#: C-2's bounds (design rev 15.11).
DETAILED_WALL_LIMIT = 100
HOST_MAP_LIMIT = 64
HOST_LIMIT = 253
DROPPED_NAME_LIMIT = 64
DROPPED_NAMES_SHOWN = 8
#: S4 F5: the glue cuts a resp line's `content_type` / `content_length` to this many UTF-16 units (glue.mjs's
#: CONTENT_FIELD_LIMIT, the same number); the reader refuses a longer one, so a glue that stops cutting is a WireError.
CONTENT_FIELD_LIMIT = 256

#: The glue's wall reasons. port: CONNECT to anything but 443, plain HTTP to anything but 80 (C-2, R15-12-B1);
#: non_dns_host: a CONNECT / absolute-form authority srt cannot canonicalise (C-2); scheme: an absolute-form target
#: on the plain path whose scheme is not http (R15-17-5, R15-19-4); socks: a connection to Levain's SOCKS refuser
#: (C-1, R15-12-B9), host always `<socks>`.
GLUE_WALL_REASONS = frozenset({"port", "non_dns_host", "scheme", "socks"})

#: srt 0.0.81's decider-protocol limits a verdict must keep (decider-client.js) or srt kills the link.
MAX_REASON_BYTES = 1 << 10
MAX_BODY = 32 << 20
MAX_REMOVE_NAMES = 190
#: srt's aggregate bound on an allow's `removeHeaders`: the sum of the names' UTF-8 bytes (decider-client.js).
MAX_REMOVE_BYTES = 64 << 10
_NEVER_REMOVED = frozenset({"host", "content-length", "transfer-encoding", "connection", "upgrade", "te", "trailer",
                            "keep-alive", "proxy-connection"})
_TOKEN = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
#: A credential VALUE srt's allow frame accepts: printable ASCII, no space, 1 to 8192 bytes.
CREDENTIAL = re.compile(r"[\x21-\x7e]{1,8192}")
_REQUEST_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
_MAX_ID = 2 ** 53 - 1
_MAX_LENGTH = str(2 ** 63 - 1)
#: ASCII only: upper-case to lower-case, `_` and `.` to `-` (a Unicode `lower()` would fold U+212A to `k`).
_FOLD = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ_.", "abcdefghijklmnopqrstuvwxyz--")


class WireError(ValueError):
    """A line or frame that is not this module's format."""


def is_request_id(value: object) -> bool:
    """A wrapper request id: a lower-case RFC 4122 version-4 UUID, as `crypto.randomUUID()` writes it."""
    return isinstance(value, str) and _REQUEST_ID.fullmatch(value) is not None


def fold_name(name: str) -> str:
    """The one name fold of this package: ASCII-lower-cased (no other character changes), `_` and `.` read as `-`.
    Equal to srt 0.0.81's `foldHeaderName` on every name srt folds (header names are ASCII tokens there), so a folded
    name in an allow's `removeHeaders` matches every spelling srt would forward. Header names and query keys are
    folded by this one function (`parse.fold_name` is this object)."""
    return name.translate(_FOLD)


# --- the observation channel ------------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ObsResponse:
    """`onResponse` fired for an allowed request: its status and two raw response-header values (transient)."""
    request_id: str
    status: int
    content_type: str | None
    content_length: str | None


@dataclass(frozen=True, slots=True)
class ObsEnded:
    """The request's abort fired before any outcome (R-10's two cases). No cause."""
    request_id: str


def _line(obj: Mapping[str, object]) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=True).encode("ascii") + b"\n"


def obs_response_line(request_id: str, status: int, content_type: str | None, content_length: str | None) -> bytes:
    return _line({"t": "resp", "request_id": request_id, "status": status, "content_type": content_type,
                  "content_length": content_length})


def obs_ended_line(request_id: str) -> bytes:
    return _line({"t": "ended", "request_id": request_id})


def _no_dup_keys(pairs: list[tuple[str, object]]) -> dict:
    d: dict = {}
    for k, v in pairs:
        if k in d:
            raise WireError(f"duplicate key {k!r}")
        d[k] = v
    return d


def _load(line: bytes) -> dict:
    if not isinstance(line, (bytes, bytearray)) or not line.endswith(b"\n") or line.count(b"\n") != 1:
        raise WireError("not one newline-terminated line")
    try:
        obj = json.loads(line.decode("utf-8"), object_pairs_hook=_no_dup_keys)
    except ValueError as e:     # UnicodeDecodeError, JSONDecodeError, and an int past Python's digit limit
        raise WireError(f"not JSON: {e}") from None
    if not isinstance(obj, dict):
        raise WireError("not an object")
    return obj


def _keys(obj: dict, want: set[str]) -> None:
    if set(obj) != want:
        raise WireError(f"fields {sorted(obj)} != {sorted(want)}")


def _int(v: object, lo: int, hi: int) -> bool:
    return type(v) is int and lo <= v <= hi


def _content_field(v: object) -> bool:
    return v is None or isinstance(v, str) and len(v) <= CONTENT_FIELD_LIMIT


def parse_obs_line(line: bytes) -> ObsResponse | ObsEnded:
    """Read one observation-channel line (the decider's drain). WireError on anything but the two formats above."""
    obj = _load(line)
    t = obj.get("t")
    if t == "resp":
        _keys(obj, {"t", "request_id", "status", "content_type", "content_length"})
        if not is_request_id(obj["request_id"]) or not _int(obj["status"], 100, 599) \
                or not _content_field(obj["content_type"]) or not _content_field(obj["content_length"]):
            raise WireError("resp field types or bounds")
        return ObsResponse(obj["request_id"], obj["status"], obj["content_type"], obj["content_length"])
    if t == "ended":
        _keys(obj, {"t", "request_id"})
        if not is_request_id(obj["request_id"]):
            raise WireError("ended request_id")
        return ObsEnded(obj["request_id"])
    raise WireError(f"unknown kind {t!r}")


def declared_length(raw: str | None) -> int | None:
    """R15-22-4: the outcome row's `declared_length`: the `Content-Length` value only when it is ONE non-negative
    ASCII decimal integer below 2^63 (no sign, no space, no list); otherwise None. The raw text is never stored."""
    if not isinstance(raw, str) or not raw or not raw.isascii() or not raw.isdigit():
        return None
    digits = raw.lstrip("0") or "0"   # compared by length before int(): no int() of an unbounded digit string
    if len(digits) > len(_MAX_LENGTH):
        return None
    n = int(digits)
    return n if n < 2 ** 63 else None


# --- the glue's event file --------------------------------------------------------------------------------------

def escape_text(text: str, limit: int) -> str:
    """The glue's escaping of an agent-chosen string for its event file (R15-25-2, R15-25-3): cut to `limit` UTF-16
    code units (a JS string's length), THEN escape each unit: printable ASCII kept, except `\\` (as `\\x5c`, so a
    literal backslash cannot spoof an escape); C0, DEL, C1 and the rest of U+0080..U+00FF as `\\xNN`; anything above
    as `\\uNNNN` (a surrogate pair is two escapes)."""
    units = struct.unpack(f"<{len(text.encode('utf-16-le', 'surrogatepass')) // 2}H",
                          text.encode("utf-16-le", "surrogatepass"))[:limit]
    out = []
    for u in units:
        if 0x20 <= u <= 0x7E and u != 0x5C:
            out.append(chr(u))
        elif u <= 0xFF:
            out.append(f"\\x{u:02x}")
        else:
            out.append(f"\\u{u:04x}")
    return "".join(out)


#: (raw, limit, escaped): the reference vectors for the glue's JS escaper.
ESCAPE_VECTORS: tuple[tuple[str, int, str], ...] = (
    ("example.com", 253, "example.com"),
    ("a\x00b\x1f\x7f\x9f", 253, "a\\x00b\\x1f\\x7f\\x9f"),
    ("café", 253, "caf\\xe9"),
    ("☃", 253, "\\u2603"),
    ("\U0001f600", 253, "\\ud83d\\ude00"),
    ("a\\x41", 253, "a\\x5cx41"),
    ("x" * 300, 253, "x" * 253),
    ("\x00" * 70, 64, "\\x00" * 64),
)


def _escaped(s: object, limit: int) -> bool:
    """Whether `s` is already the output of escape_text (printable ASCII, no raw backslash but in an escape)."""
    if not isinstance(s, str) or not s.isascii():
        return False
    body = re.sub(r"\\x[0-9a-f]{2}|\\u[0-9a-f]{4}", "", s)
    if "\\" in body or not all(0x20 <= ord(c) <= 0x7E for c in body):
        return False
    units = len(body) + len(re.findall(r"\\x[0-9a-f]{2}|\\u[0-9a-f]{4}", s))
    return units <= limit


@dataclass(frozen=True, slots=True)
class GlueReq:
    request_id: str
    method: str
    host: str
    query_name_count: int
    dropped_count: int
    dropped_names: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GlueWall:
    request_id: str | None
    method: str | None
    host: str
    reason: str


@dataclass(frozen=True, slots=True)
class GlueSummary:
    minute: int
    minute_count: int
    total: int
    by_reason: Mapping[str, int] = field(default_factory=dict)
    hosts: Mapping[str, int] = field(default_factory=dict)
    overflow: int = 0
    final: bool = False


GlueEvent = GlueReq | GlueWall | GlueSummary


def glue_event_line(e: GlueEvent) -> bytes:
    """The reference encoder (the glue writes the same objects)."""
    if isinstance(e, GlueReq):
        return _line({"t": "req", "request_id": e.request_id, "method": e.method, "host": e.host,
                      "query_name_count": e.query_name_count, "dropped_count": e.dropped_count,
                      "dropped_names": list(e.dropped_names)})
    if isinstance(e, GlueWall):
        return _line({"t": "wall", "request_id": e.request_id, "method": e.method, "host": e.host,
                      "reason": e.reason})
    return _line({"t": "summary", "minute": e.minute, "minute_count": e.minute_count, "total": e.total,
                  "by_reason": dict(e.by_reason), "hosts": dict(e.hosts), "overflow": e.overflow, "final": e.final})


def _counts(m: object, ok_key) -> dict[str, int] | None:
    if not isinstance(m, dict) or not all(ok_key(k) and _int(v, 1, 2 ** 53) for k, v in m.items()):
        return None
    return m


def parse_glue_event(line: bytes) -> GlueEvent:
    """Read one glue-event line (K1's reader). WireError on anything but the three formats above, a bound broken,
    an unescaped string, or a summary whose counters do not account for its total."""
    obj = _load(line)
    t = obj.get("t")
    if t == "req":
        _keys(obj, {"t", "request_id", "method", "host", "query_name_count", "dropped_count", "dropped_names"})
        names = obj["dropped_names"]
        if not is_request_id(obj["request_id"]) or not (isinstance(obj["method"], str) and _TOKEN.fullmatch(
                obj["method"])) or not _escaped(obj["host"], HOST_LIMIT) \
                or not _int(obj["query_name_count"], 0, 2 ** 53) or not _int(obj["dropped_count"], 0, 2 ** 53) \
                or not isinstance(names, list) or len(names) > DROPPED_NAMES_SHOWN \
                or len(names) > obj["dropped_count"] or not all(_escaped(n, DROPPED_NAME_LIMIT) for n in names):
            raise WireError("req fields")
        return GlueReq(obj["request_id"], obj["method"], obj["host"], obj["query_name_count"], obj["dropped_count"],
                       tuple(names))
    if t == "wall":
        _keys(obj, {"t", "request_id", "method", "host", "reason"})
        rid, method = obj["request_id"], obj["method"]
        if (rid is None) != (method is None) or (rid is not None and not is_request_id(rid)) \
                or (method is not None and not (isinstance(method, str) and _TOKEN.fullmatch(method))) \
                or not _escaped(obj["host"], HOST_LIMIT) or obj["reason"] not in GLUE_WALL_REASONS:
            raise WireError("wall fields")
        return GlueWall(rid, method, obj["host"], obj["reason"])
    if t == "summary":
        _keys(obj, {"t", "minute", "minute_count", "total", "by_reason", "hosts", "overflow", "final"})
        by_reason = _counts(obj["by_reason"], lambda k: k in GLUE_WALL_REASONS)
        hosts = _counts(obj["hosts"], lambda k: _escaped(k, HOST_LIMIT))
        if not _int(obj["minute"], 0, 2 ** 53) or obj["minute"] % 60 or not _int(obj["minute_count"], 0, 2 ** 53) \
                or not _int(obj["total"], 0, 2 ** 53) or not _int(obj["overflow"], 0, 2 ** 53) \
                or type(obj["final"]) is not bool or by_reason is None or hosts is None:
            raise WireError("summary fields")
        total = obj["total"]
        if len(hosts) > HOST_MAP_LIMIT or sum(by_reason.values()) != total \
                or sum(hosts.values()) + obj["overflow"] != total or obj["minute_count"] > total \
                or (obj["minute_count"] == 0 and not obj["final"]):
            raise WireError("summary counters")
        return GlueSummary(obj["minute"], obj["minute_count"], total, by_reason, hosts, obj["overflow"],
                           obj["final"])
    raise WireError(f"unknown kind {t!r}")


# --- verdict frames to srt (decider-client.js's schemas, kept here so srt never kills the link) -----------------

def _id(srt_id: int) -> int:
    if not _int(srt_id, 1, _MAX_ID):
        raise WireError("srt id")
    return srt_id


def allow_frame(srt_id: int, remove_headers: Iterable[str] = (), *, cred: str | None = None,
                credential: str | None = None) -> dict:
    """An allow verdict. `remove_headers` is folded and deduped (srt matches by fold); an empty optional is left out
    (srt refuses an empty one). `cred` / `credential` together or not at all; the credential 1..8192 printable ASCII
    with no space. The wrapper adds its own removals after srt validates (item 2 step 5); this is the decider's
    list. No `setHeaders`: the decider never sets a header (R-6 attaches through `cred`)."""
    names: list[str] = []
    for n in remove_headers:
        f = fold_name(n)
        if not _TOKEN.fullmatch(f) or f in _NEVER_REMOVED:
            raise WireError(f"removeHeaders name {n!r}")
        if f not in names:
            names.append(f)
    if len(names) > MAX_REMOVE_NAMES:
        raise WireError("removeHeaders over srt's limit")
    if sum(len(n.encode("utf-8")) for n in names) > MAX_REMOVE_BYTES:
        raise WireError("removeHeaders over srt's byte limit")
    if (cred is None) != (credential is None):
        raise WireError("cred and credential go together")
    f: dict = {"t": "verdict", "id": _id(srt_id), "action": "allow"}
    if names:
        f["removeHeaders"] = names
    if cred is not None:
        if not isinstance(cred, str) or not cred or not CREDENTIAL.fullmatch(credential or ""):
            raise WireError("credential shape")
        f["cred"], f["credential"] = cred, credential
    return f


def deny_frame(srt_id: int, status: int, reason: str) -> dict:
    """A deny verdict: status 400..599, a reason of 1..1024 UTF-8 bytes with no control character but tab."""
    if not _int(status, 400, 599):
        raise WireError("deny status")
    try:
        size = len(reason.encode("utf-8")) if isinstance(reason, str) else 0
    except UnicodeEncodeError:   # a lone surrogate
        raise WireError("deny reason") from None
    if not isinstance(reason, str) or not reason or size > MAX_REASON_BYTES \
            or any((ord(c) < 0x20 and c != "\t") or 0x7F <= ord(c) <= 0x9F for c in reason):
        raise WireError("deny reason")
    return {"t": "verdict", "id": _id(srt_id), "action": "deny", "status": status, "reason": reason}


def need_body_frame(srt_id: int, max_bytes: int) -> dict:
    """Ask srt for the request body, at most `max_bytes` (1 .. 32 MiB) decoded entity bytes."""
    if not _int(max_bytes, 1, MAX_BODY):
        raise WireError("need_body max")
    return {"t": "verdict", "id": _id(srt_id), "action": "need_body", "max": max_bytes}


def encode_frame(msg: Mapping[str, object]) -> bytes:
    """srt's framing: a 4-byte big-endian length, then that many bytes of one UTF-8 JSON object."""
    try:
        raw = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "strict")
    except UnicodeEncodeError:   # a lone surrogate somewhere in the message
        raise WireError("frame is not UTF-8 encodable") from None
    return struct.pack(">I", len(raw)) + raw
