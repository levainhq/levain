"""levain.agents.session_client: the operator-side client of the dispatch run server, and the wire codec both halves
share (BUILD_PLAN P7-1 slices S9 + S10a; design ``1104-27_S10_DESIGN.md`` sections 2, 3.2-3.4, 6 F1/F12).

The shape (design section 2, option (a)): :class:`levain.agents.session_launcher.RealLauncher` runs, unchanged, inside a
``_levain_dispatch`` process (the run server, S10b); the fire (the operator uid) holds this client in its place. The
client is a :class:`levain.agents.session.SessionLauncher` AND a :class:`levain.agents.session.SessionEnder`, so
``run_session`` spawns through it and the reaper's ``RowSessionExecutor`` ends through it, with one name per side and
one act (S9, finding A: ``RealLauncher`` itself has only ``stop``, which stays server-internal).

**The wire (``levain-run/1``).** A unix stream; every frame is a 4-byte big-endian length plus canonical JSON
(:mod:`levain.contracts.canon`: ints, no floats, so every duration is whole milliseconds), at most
:data:`RUN_FRAME_MAX` bytes (a brief alone may be 64 KiB, so :data:`levain.slot.FRAME_MAX` does not fit). One request,
one answer, on the connection that asked. A request is ``{"p": "levain-run/1", "op", "id", ...}``; an answer is
``{"id", "ok": true, ...}`` or ``{"id", "ok": false, "refused": <name>[, "detail", "reason_class"]}``. Both halves
decode through the strict functions here (exact key sets, exact types), so a field neither side names is a wire
error, never silently carried.

* A **holder** connection is one session's lifeline: ``LAUNCH`` (typed launch + the signature proof), then ``RELEASE``,
  ``WAIT`` (a bounded long-poll; ``timeout_ms`` 0 is a poll), ``TERM`` and ``ABORT``. Its EOF is the server's cue to
  END the session (design 3.4); the client therefore CLOSES it on any fault.
  A launch that stages a repo (levain 34, W1) carries a ``workspace`` member ``{repo, head, withhold}`` and is preceded,
  on the SAME connection, by ``WSBEGIN`` and ``WSDATA`` frames that carry the snapshot manifest and git bundle (see
  "the workspace upload" below). The upload is built by :meth:`DispatchSessionLauncher.build_workspace`
  over ``levain.dispatch.stager`` (operator uid only) BEFORE the connection is opened, and sent by
  :meth:`DispatchSessionLauncher.stage_workspace`; the shapes are here.
* A **short** connection carries ``USAGE`` or ``END`` for a session key and nothing else.

**What never crosses.** The ticket and the run id are minted and kept on the dispatch uid (design port items (2), (3)):
no request has a field for either, no answer may carry one (an answer with any key outside its exact set is a wire
fault), and the client holds neither. The only identity the client sends is the occurrence and the session key the
supervisor recorded.

**Q1 = (b): what the client carries so the server can verify his signature at the point of use** (:class:`LinkProof`).
Derived, not invented: the fire already holds the occurrence's signed spec (``spec_hash``) and the agent link it
launched under (``link_id``), both on the occurrence row, and the link is a signed record in the cockpit authority log
(``levain.authority``). The proof is that link's born and current log rows (``seq``, ``prev_hash``, ``record``,
``signer_fp``, ``signature``: the signed message is ``prev_hash + "\\n" + record``) plus the occurrence id and spec
hash. **What his existing signature does and does not cover** (said here because the design's wording over-reads it):
the link's signature covers the spec hash, the agent, the store, the binding and the per-run budgets; it cannot cover
an occurrence id, which does not exist when he signs. The occurrence id travels beside the proof, not inside the
signature, so the server's check is: a valid signature by an enrolled key over a link that names this spec hash, and
the requested exposure within the link's budgets. S10b verifies; nothing in this module judges a signature.

**Any transport fault reads as unproven.** A holder that loses its server answers
:data:`levain.agents.controller.GATE_UNPROVEN` from ``poll`` and ``wait`` (never a termination proof, so the run is
never ``done`` on a broken wire and keeps its slot for the reaper); ``usage`` that cannot be read is the caps; an
``end_session`` that cannot be answered is ``StopResult(False, ...)``. A ``spawn`` that fails BEFORE the request is
sent raises (nothing was allocated); one that fails after it returns a down child instead of raising, because the
server may hold a session (the launcher contract: a launcher that cannot undo what it allocated must not raise).

**Known edges** (LOW, kept by intent, none built against). The first exit a holder records stands: ``terminate`` and
``abort`` never overwrite it, so an ``abort`` after a recorded exit returns that exit and sends no ABORT (the closed
holder already cued the server's END), and an exit recorded unproven on a transport fault is not upgraded by a later
proven one; the cost is a run that keeps its slot until the reaper's END proves it (``_RemoteProcess._publish``, pinned
by ``test_a_recorded_exit_is_never_downgraded_by_a_later_abort_or_terminate``).
"""
from __future__ import annotations

import base64
import dataclasses
import binascii
import logging
import re
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from urllib.parse import quote
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from levain.agents import controller as _ctl
from levain.agents.accounting import ActionExposure, StopResult, Usage
from levain.agents.session import AUTH_FOR_BILLING, S1_DISPATCH, WORKSPACE_REFUSED, SessionLaunch, Workspace
from levain.agents.session_launcher import LauncherRefused
from levain.agents.signed_role import SIGNED_TEXT_MAX, SignedLink, SignedRole
from levain.contracts import ContractError, canon

__all__ = ["PROTOCOL", "RUN_FRAME_MAX", "LAUNCHER_NAME", "WAIT_SLICE_S", "WireError", "ProofUnavailable", "LinkRow",
           "LinkProof", "SignedRole", "SignedLink", "signed_role_of", "signed_link_of", "encode_run_frame", "read_run_frame", "check_run_request", "ok_answer", "refused_answer",
           "launch_to_wire", "launch_from_wire", "usage_to_wire", "usage_from_wire", "link_proof",
           "WS_CHUNK_BYTES", "WS_PARTS", "WsBegin", "ws_begin_to_wire", "ws_begin_from_wire", "ws_chunk_frames",
           "ws_chunk_from_wire",
           "WorkspaceNotSent", "LaunchNotSent", "BrokeredServerAbsent", "BROKERED_SOCKET_NAME", "default_run_socket", "default_brokered_socket",
           "session_auth_of", "unix_connector", "DispatchSessionLauncher", "build_launcher",
           "AuthorityFeed"]

_log = logging.getLogger("levain.agents.session_client")

PROTOCOL = "levain-run/1"
LAUNCHER_NAME = "s1-dispatch"
#: [judged by this slice] a launch carries a brief of up to 64 KiB (``session.BRIEF_MAX_BYTES``); canonical JSON writes a
#: control character as six bytes, so the bound sits well above the worst case and well below anything unbounded.
RUN_FRAME_MAX = 512 * 1024
#: [judged] one WAIT is a slice of this many seconds, so a TERM or ABORT sent from another thread waits at most this long
#: for the holder connection (one request is in flight on it at a time).
WAIT_SLICE_S = 2.0
_ANSWER_MARGIN_S = 5.0          # a socket's patience beyond the slice the server was asked to hold
_CONNECT_S = 10.0
#: [judged by this slice] LAUNCH answers after the server's mint, lease, prepare, render and gated start (sudo, a pty, a
#: bundle): it may take far longer than a RELEASE. A LAUNCH that outlasts this is a down child, and the closed holder
#: makes the server END whatever it had started. Known edge: a LAUNCH that carries a workspace also waits on the
#: server's bundle verify, which is bounded by the server's own deadlines (the copy's ``STAGE_DEADLINE_S`` plus one
#: ``GIT_TIMEOUT`` per git step, a count no constant names), not by this number; a verify that outlasts it reads as a down child.
LAUNCH_ANSWER_S = 120.0
_LEN = struct.Struct(">I")
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9:_.-]{0,95}")        # a refusal name the client will repeat
_DETAIL_MAX = 400
_INT_MAX = 2 ** 53 - 1


class WireError(ValueError):
    """A frame that is not ``levain-run/1``'s shape: the connection that carried it is dropped."""


class ProofUnavailable(RuntimeError):
    """The signed link the occurrence launched under cannot be turned into a proof. ``standing`` is True when a retry
    meets the same answer (the log names another spec or agent), False when the log was merely unreadable."""

    def __init__(self, detail: str, *, standing: bool) -> None:
        super().__init__(detail)
        self.standing = standing


# --- framing -------------------------------------------------------------------------------------------------------

def encode_run_frame(obj: Mapping[str, object]) -> bytes:
    try:
        data = canon.canonical_bytes(dict(obj))
        canon.parse(data)               # the codec's own refusals (a float, an oversized integer) hold on the way out too
    except (TypeError, ValueError, ContractError) as exc:
        raise WireError(f"not canonical JSON ({type(exc).__name__})") from None
    if len(data) > RUN_FRAME_MAX:
        raise WireError(f"frame of {len(data)} bytes is over {RUN_FRAME_MAX}")
    return _LEN.pack(len(data)) + data


def _recv_exact(sock: socket.socket, n: int, *, eof_ok: bool) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            if eof_ok and not buf:
                return None
            raise WireError("EOF inside a frame")
        buf += chunk
    return bytes(buf)


def read_run_frame(sock: socket.socket) -> dict | None:
    """One frame's object; None on a clean EOF between frames. Raises :class:`WireError` (and the socket's own
    ``OSError`` / ``socket.timeout``, which a caller treats alike)."""
    head = _recv_exact(sock, _LEN.size, eof_ok=True)
    if head is None:
        return None
    (n,) = _LEN.unpack(head)
    if n == 0 or n > RUN_FRAME_MAX:
        raise WireError(f"frame length {n}")
    data = _recv_exact(sock, n, eof_ok=False)
    assert data is not None
    try:
        obj = canon.parse(data)
    except ContractError as exc:
        raise WireError(str(exc)) from None
    if type(obj) is not dict or canon.canonical_bytes(obj) != data:
        raise WireError("not a canonical JSON object")
    return obj


def _exact(obj: object, keys: tuple[str, ...], where: str, *, optional: tuple[str, ...] = ()) -> dict:
    if type(obj) is not dict:
        raise WireError(f"{where} is not an object")
    extra = set(obj) - set(keys) - set(optional)
    missing = set(keys) - set(obj)
    if extra or missing:
        raise WireError(f"{where}: unexpected {sorted(extra)} missing {sorted(missing)}")
    return obj


def _int(v: object, where: str, *, lo: int = 0, none: bool = False) -> int | None:
    if v is None and none:
        return None
    if type(v) is not int or not lo <= v <= _INT_MAX:
        raise WireError(f"{where} is not an integer in range")
    return v


def _str(v: object, where: str, *, max_len: int) -> str:
    if type(v) is not str or len(v) > max_len:
        raise WireError(f"{where} is not a short string")
    return v


def _text(v: object, where: str) -> str:
    if type(v) is not str:
        raise WireError(f"{where} is not text")
    return v


def check_run_request(obj: dict) -> tuple[int, str]:
    """``{"p": PROTOCOL, "op", "id", ...}`` -> ``(id, op)``. A protocol other than this one is
    ``WireError("protocol-mismatch")`` (design F12: the server answers ``protocol-mismatch``, standing, and guesses no field)."""
    if obj.get("p") != PROTOCOL:
        raise WireError("protocol-mismatch")
    op, rid = obj.get("op"), obj.get("id")
    if type(op) is not str or not op or type(rid) is not int or not 0 <= rid <= _INT_MAX:
        raise WireError("a request carries a string op and an integer id")
    return rid, op


def ok_answer(rid: int | None, **fields: object) -> dict[str, object]:
    return {"id": rid, "ok": True, **fields}


def refused_answer(rid: int | None, name: str, detail: str = "", reason_class: str = "standing") -> dict[str, object]:
    out: dict[str, object] = {"id": rid, "ok": False, "refused": name}
    if detail:
        out["detail"] = detail[:_DETAIL_MAX]
    if reason_class != "standing":
        out["reason_class"] = reason_class
    return out


# --- the typed bodies ------------------------------------------------------------------------------------------------

@dataclass(frozen=True)
class LinkRow:
    """One authority-log row of the signed link: the signed message is ``prev_hash + "\\n" + record``."""
    seq: int
    prev_hash: str
    record: str
    signer_fp: str
    signature: str


@dataclass(frozen=True)
class LinkProof:
    """What the client carries so the server can verify his signature at the point of use (Q1 = (b)): the occurrence,
    the spec hash it launched under, the link and the store it names, and the link's ``born`` row (the create, which
    names the spec hash and the budgets) and ``current`` row (the last create or renew; the same row when never
    renewed)."""
    occurrence_id: str
    spec_hash: str
    link_id: str
    store_id: str
    born: LinkRow
    current: LinkRow
    #: levain 1107 (W2): the spec and role texts the link's ``spec_hash`` pins, for a launch that stages a workspace (the
    #: server re-derives the role's withhold from them, ``levain.agents.signed_role``). None: the proof carries none,
    #: which a launch with a workspace is refused for; it travels on the wire only when set.
    signed: SignedRole | None = None


_ROW_KEYS = ("seq", "prev_hash", "record", "signer_fp", "signature")
_EXPOSURE_KEYS = ("actions", "unit", "seconds", "tunnel_bytes")
_LAUNCH_KEYS = ("occurrence_id", "session_key", "harness", "model", "brief", "auth", "exposure")
#: OPTIONAL on the wire (an absent member is None), so a client that stages no workspace, or prices no brokered run
#: (``usd_micros``: the dollars the fire's reservation took, which the server only compares, never mints from),
#: sends the frame it always sent
_LAUNCH_OPTIONAL = ("workspace", "usd_micros")
_WORKSPACE_KEYS = ("repo", "head", "withhold")
_PROOF_KEYS = ("occurrence_id", "spec_hash", "link_id", "store_id", "born", "current")
_PROOF_OPTIONAL = ("signed",)
_SIGNED_KEYS = ("agent_id", "spec", "role")


def _row_to_wire(r: LinkRow) -> dict[str, object]:
    return {"seq": r.seq, "prev_hash": r.prev_hash, "record": r.record, "signer_fp": r.signer_fp,
            "signature": r.signature}


def _row_from_wire(o: object, where: str) -> LinkRow:
    o = _exact(o, _ROW_KEYS, where)
    return LinkRow(_int(o["seq"], f"{where}.seq", lo=1), _str(o["prev_hash"], f"{where}.prev_hash", max_len=128),
                   _str(o["record"], f"{where}.record", max_len=64 * 1024),
                   _str(o["signer_fp"], f"{where}.signer_fp", max_len=256),
                   _str(o["signature"], f"{where}.signature", max_len=16 * 1024))


def launch_to_wire(launch: SessionLaunch, proof: LinkProof) -> dict[str, object]:
    """The LAUNCH body: the typed :class:`SessionLaunch` fields and the proof. No ticket, no run id, no mint field."""
    ex = launch.exposure
    body = {"occurrence_id": launch.occurrence_id, "session_key": launch.session_key,
            "harness": launch.harness, "model": launch.model, "brief": launch.brief, "auth": launch.auth,
            "exposure": None if ex is None else {"actions": ex.actions, "unit": ex.unit,
                                                 "seconds": ex.seconds, "tunnel_bytes": ex.tunnel_bytes}}
    ws = launch.workspace
    if ws is not None:         # the manifest digest is the server's own derivation and never travels (Workspace)
        body["workspace"] = {"repo": ws.repo, "head": ws.head, "withhold": list(ws.withhold)}
    if launch.usd_micros is not None:
        body["usd_micros"] = launch.usd_micros
    wire_proof = {"occurrence_id": proof.occurrence_id, "spec_hash": proof.spec_hash, "link_id": proof.link_id,
                  "store_id": proof.store_id, "born": _row_to_wire(proof.born), "current": _row_to_wire(proof.current)}
    if proof.signed is not None:
        wire_proof["signed"] = {"agent_id": proof.signed.agent_id, "spec": proof.signed.spec, "role": proof.signed.role}
    return {"launch": body, "proof": wire_proof}


def launch_from_wire(request: Mapping[str, object]) -> tuple[SessionLaunch, LinkProof]:
    """The server's decode of a LAUNCH request: ``(SessionLaunch, LinkProof)`` or :class:`WireError`. The proof's
    occurrence must be the launch's own."""
    body = {k: v for k, v in request.items() if k not in ("p", "op", "id")}
    body = _exact(body, ("launch", "proof"), "LAUNCH")
    lo = _exact(body["launch"], _LAUNCH_KEYS, "launch", optional=_LAUNCH_OPTIONAL)
    ex = lo["exposure"]
    exposure = None
    if ex is not None:
        ex = _exact(ex, _EXPOSURE_KEYS, "launch.exposure")
        exposure = ActionExposure(_int(ex["actions"], "exposure.actions"), _str(ex["unit"], "exposure.unit", max_len=32),
                                  _int(ex["seconds"], "exposure.seconds"),
                                  _int(ex["tunnel_bytes"], "exposure.tunnel_bytes", none=True))
    workspace = None
    if "workspace" in lo:
        wo = _exact(lo["workspace"], _WORKSPACE_KEYS, "launch.workspace")
        if type(wo["withhold"]) is not list:
            raise WireError("launch.workspace.withhold is not a list")
        try:
            workspace = Workspace(_str(wo["repo"], "workspace.repo", max_len=4096),
                                  _str(wo["head"], "workspace.head", max_len=40), tuple(wo["withhold"]))
        except ValueError as exc:         # the dataclass's own shape check, as a frame the connection is dropped for
            raise WireError(str(exc)) from None
    launch = SessionLaunch(_str(lo["occurrence_id"], "occurrence_id", max_len=128),
                           _str(lo["session_key"], "session_key", max_len=128),
                           _str(lo["harness"], "harness", max_len=64), _str(lo["model"], "model", max_len=256),
                           _str(lo["brief"], "brief", max_len=RUN_FRAME_MAX), _str(lo["auth"], "auth", max_len=32),
                           exposure, workspace,
                           _int(lo["usd_micros"], "launch.usd_micros") if "usd_micros" in lo else None)
    po = _exact(body["proof"], _PROOF_KEYS, "proof", optional=_PROOF_OPTIONAL)
    signed = None
    if "signed" in po:
        so = _exact(po["signed"], _SIGNED_KEYS, "proof.signed")
        try:
            signed = SignedRole(_str(so["agent_id"], "signed.agent_id", max_len=64),
                                _str(so["spec"], "signed.spec", max_len=SIGNED_TEXT_MAX),
                                _str(so["role"], "signed.role", max_len=SIGNED_TEXT_MAX))
        except ValueError as exc:
            raise WireError(str(exc)) from None
    proof = LinkProof(_str(po["occurrence_id"], "proof.occurrence_id", max_len=128),
                      _str(po["spec_hash"], "proof.spec_hash", max_len=128),
                      _str(po["link_id"], "proof.link_id", max_len=128),
                      _str(po["store_id"], "proof.store_id", max_len=128),
                      _row_from_wire(po["born"], "proof.born"), _row_from_wire(po["current"], "proof.current"), signed)
    if proof.occurrence_id != launch.occurrence_id:
        raise WireError("the proof names another occurrence than the launch")
    return launch, proof


# --- the workspace upload (levain 34, W1) ---------------------------------------------------------------------------------
#
# The bundle cannot ride the LAUNCH frame (RUN_FRAME_MAX), and the workspace member names no bytes, so two ops before it,
# on the connection the LAUNCH will use: WSBEGIN declares the two parts (the canonical manifest and the bundle: sizes and
# sha256 digests), WSDATA carries one bounded chunk of one part. The chunking follows SYNC's: a bound well under one frame
# (base64 grows a chunk by a third), in order, each answered. The server holds the parts until the LAUNCH of the SAME
# session key consumes them or the connection ends. Only the shape is here; the bytes are the operator-uid side's to build.

#: [judged] raw bytes per WSDATA chunk: 192 KiB is 256 KiB of base64, half of RUN_FRAME_MAX, whatever the framing adds
WS_CHUNK_BYTES = 192 * 1024
WS_PARTS = ("manifest", "bundle")
_WS_BEGIN_KEYS = ("session_key", "manifest_size", "manifest_sha256", "bundle_size", "bundle_sha256")
_WS_DATA_KEYS = ("session_key", "part", "offset", "data")
_WS_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class WsBegin:
    """A decoded WSBEGIN: the session key the upload belongs to and, per part, its declared size and sha256 digest."""
    session_key: str
    sizes: Mapping[str, int]
    digests: Mapping[str, str]


def ws_begin_to_wire(session_key: str, manifest: bytes, bundle: bytes) -> dict[str, object]:
    """The WSBEGIN fields for these two parts' bytes (the op and id are the sender's)."""
    import hashlib
    return {"session_key": session_key, "manifest_size": len(manifest),
            "manifest_sha256": "sha256:" + hashlib.sha256(manifest).hexdigest(),
            "bundle_size": len(bundle), "bundle_sha256": "sha256:" + hashlib.sha256(bundle).hexdigest()}


def ws_begin_from_wire(request: Mapping[str, object]) -> WsBegin:
    body = _exact({k: v for k, v in request.items() if k not in ("p", "op", "id")}, _WS_BEGIN_KEYS, "WSBEGIN")
    sizes, digests = {}, {}
    for part in WS_PARTS:
        sizes[part] = _int(body[f"{part}_size"], f"{part}_size", lo=1)
        d = _str(body[f"{part}_sha256"], f"{part}_sha256", max_len=71)
        if _WS_DIGEST.fullmatch(d) is None:
            raise WireError(f"{part}_sha256 is not sha256: + 64 lowercase hex")
        digests[part] = d
    return WsBegin(_str(body["session_key"], "session_key", max_len=128), sizes, digests)


def ws_chunk_frames(session_key: str, part: str, data: bytes):
    """Yield the WSDATA fields that carry ``data`` (one part's bytes) in order: ``WS_CHUNK_BYTES`` at a time."""
    if part not in WS_PARTS:
        raise WireError(f"part {part!r} is not one of {WS_PARTS}")
    for off in range(0, len(data), WS_CHUNK_BYTES):
        yield {"session_key": session_key, "part": part, "offset": off,
               "data": base64.b64encode(data[off:off + WS_CHUNK_BYTES]).decode("ascii")}


def ws_chunk_from_wire(request: Mapping[str, object]) -> tuple[str, str, int, bytes]:
    """``(session_key, part, offset, raw bytes)`` of a WSDATA, or :class:`WireError`: the part is one of
    :data:`WS_PARTS`, the data is canonical base64 of 1..``WS_CHUNK_BYTES`` bytes."""
    body = _exact({k: v for k, v in request.items() if k not in ("p", "op", "id")}, _WS_DATA_KEYS, "WSDATA")
    part = body["part"]
    if part not in WS_PARTS:
        raise WireError(f"part is not one of {WS_PARTS}")
    text = _str(body["data"], "data", max_len=4 * ((WS_CHUNK_BYTES + 2) // 3) + 4)
    try:
        raw = base64.b64decode(text.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError, ValueError):
        raise WireError("data is not base64") from None
    if not 0 < len(raw) <= WS_CHUNK_BYTES or base64.b64encode(raw).decode("ascii") != text:
        raise WireError("data is not canonical base64 of 1..WS_CHUNK_BYTES bytes")
    return (_str(body["session_key"], "session_key", max_len=128), part,
            _int(body["offset"], "offset"), raw)  # type: ignore[return-value]


_USAGE_KEYS = ("usd_micros", "input_tokens", "output_tokens", "source", "actions", "tunnel_bytes", "seconds")


def usage_to_wire(u: Usage) -> dict[str, object]:
    return {"usd_micros": u.usd_micros, "input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
            "source": u.source, "actions": u.actions, "tunnel_bytes": u.tunnel_bytes, "seconds": u.seconds}


def usage_from_wire(o: object) -> Usage:
    o = _exact(o, _USAGE_KEYS, "usage")
    return Usage(_int(o["usd_micros"], "usd_micros", none=True), _int(o["input_tokens"], "input_tokens", none=True),
                 _int(o["output_tokens"], "output_tokens", none=True), _text(o["source"], "source")[:200],
                 actions=_int(o["actions"], "actions", none=True),
                 tunnel_bytes=_int(o["tunnel_bytes"], "tunnel_bytes", none=True),
                 seconds=_int(o["seconds"], "seconds", none=True))


# --- the proof, derived from what the fire already holds ---------------------------------------------------------------

def link_proof(store: Any, cockpit_root: Path | str, occurrence_id: str, *, now: Any = None) -> LinkProof:
    """The :class:`LinkProof` of ``occurrence_id``: its row's ``spec_hash`` and ``link_id``, and that link's born and
    current rows read from the cockpit authority log at ``cockpit_root`` after a verified replay
    (:class:`levain.agent_authority.AgentAuthority`); ``now`` (an aware datetime, default the real clock) is a test seam. Raises :class:`ProofUnavailable` for a row with no link, a link
    that is not an agent link of this occurrence's agent and spec hash, or a log that does not read."""
    from levain.cockpit.firejournal import DB_NAME
    spec_hash, link_id, store_id, born, current, _grant = _standing_link(store, cockpit_root, occurrence_id, now)
    rows: dict[int, LinkRow] = {}
    try:
        db = sqlite3.connect("file:" + quote(str(Path(cockpit_root) / DB_NAME), safe="/") + "?mode=ro", uri=True,
                             timeout=5.0)
        try:
            for seq in {born, current}:
                got = db.execute("SELECT seq, prev_hash, record, signer_fp, signature FROM authority_log"
                                 " WHERE seq = ?", (seq,)).fetchone()
                if got is None or not all(type(c) is str for c in got[1:]):
                    raise ProofUnavailable(f"log row {seq} is absent or not text", standing=False)
                rows[seq] = LinkRow(*got)
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise ProofUnavailable(f"the authority log rows do not read ({type(exc).__name__})", standing=False) from None
    return LinkProof(occurrence_id, spec_hash, link_id, store_id, rows[born], rows[current])


def signed_link_of(store: Any, cockpit_root: Path | str, occurrence_id: str, *, now: Any = None) -> SignedLink:
    """The :class:`SignedLink` (``agent_id``, ``spec_hash``, ``role_hash``) of the standing link ``occurrence_id``
    launched under, from the same verified replay :func:`link_proof` reads: what the operator-uid half derives the
    signed role's withhold against (L3 W2 r1 HIGH). Raises :class:`ProofUnavailable` as :func:`link_proof` does."""
    _spec_hash, _link_id, _sid, _born, _current, grant = _standing_link(store, cockpit_root, occurrence_id, now)
    try:
        return SignedLink(grant["agent_id"], grant["spec_hash"], grant["role_hash"])
    except (KeyError, TypeError, ValueError):
        raise ProofUnavailable("the link names no role", standing=True) from None


def _standing_link(store: Any, cockpit_root: Path | str, occurrence_id: str, now: Any) -> tuple:
    """``(spec_hash, link_id, store_id, born_seq, current_seq, grant)`` of the standing agent link ``occurrence_id``
    launched under, after a verified replay of the log at ``cockpit_root`` (:func:`link_proof`'s checks)."""
    from levain import authority as _auth
    from levain.agent_authority import AgentAuthority
    try:
        with store.read() as conn:
            row = conn.execute("SELECT agent_id, spec_hash, link_id FROM agent_occurrences WHERE occurrence_id = ?",
                               (occurrence_id,)).fetchone()
            sid = conn.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()
    except Exception as exc:  # noqa: BLE001
        raise ProofUnavailable(f"the agent store does not read ({type(exc).__name__})", standing=False) from None
    if sid is None:
        raise ProofUnavailable("the agent store holds no store id", standing=True)
    if row is None or type(row[2]) is not str or not row[2]:
        raise ProofUnavailable("the occurrence row names no signed link", standing=True)
    agent_id, spec_hash, link_id = row
    try:
        state = AgentAuthority(cockpit_root).replay()
    except Exception as exc:  # noqa: BLE001 - a replay that fails proves nothing
        raise ProofUnavailable(f"the authority log does not replay ({type(exc).__name__})", standing=False) from None
    grant = state.policy.grants.get(link_id)
    born, current = state.grant_born.get(link_id), state.grant_seqs.get(link_id)
    if not _auth.agent_link(grant) or born is None or current is None or grant["spec_hash"] != spec_hash \
            or grant["agent_id"] != agent_id or grant["store_id"] != sid[0]:
        raise ProofUnavailable("the link the occurrence launched under is not a standing link of its agent and spec",
                               standing=True)
    # best effort on the client (the server re-judges standing, freshness and the signers against ITS OWN replay: two
    # historical rows are not proof of current standing): refuse a link already lapsed or born before a revoke
    from datetime import datetime, timezone
    if not _auth.agent_link_arms(state, link_id, now=now or datetime.now(timezone.utc)):
        raise ProofUnavailable("the link is expired or was revoked", standing=True)
    return spec_hash, link_id, sid[0], born, current, grant


def signed_role_of(store: Any, occurrence_id: str) -> SignedRole:
    """The spec and role texts an occurrence launched under, each through its full row check (the spec re-hashes to the
    occurrence's ``spec_hash``; the role to the hash the spec pins), as the :class:`SignedRole` the run server re-derives
    the role's withhold from. Raises :class:`ProofUnavailable` (standing: a stored text that fails its check does not
    heal by waiting) when either is unreadable."""
    from levain.agents import role as _role, spec as _spec
    from levain.authority import _canonical
    try:
        signed = _spec.occurrence_spec(store, occurrence_id)
        with store.read() as conn:
            row = conn.execute("SELECT agent_id FROM agent_occurrences WHERE occurrence_id = ?",
                               (occurrence_id,)).fetchone()
        role = _role.role_by_hash(store, signed["role"]["role_hash"])
        if row is None or role is None:
            raise ProofUnavailable("the occurrence's role is not in the agent store", standing=True)
        return SignedRole(row[0], _canonical(signed), _role.canonical_role(role))
    except ProofUnavailable:
        raise
    except (_spec.SpecError, _role.RoleError, ValueError) as exc:
        raise ProofUnavailable(f"the signed spec or role does not read ({type(exc).__name__})", standing=True) from None
    except Exception as exc:  # noqa: BLE001 - a store that does not read proves nothing, and may read next time
        raise ProofUnavailable(f"the agent store does not read ({type(exc).__name__})", standing=False) from None


# --- the authority feed: what the fire SYNCs to the server's replica -----------------------------------------------------

_SYNC_CHUNK_ROWS = 256
_SYNC_CHUNK_BYTES = RUN_FRAME_MAX // 4          # well under one frame, whatever the row sizes (a row is capped at 64 KiB)


@dataclass(frozen=True)
class AuthorityRead:
    """The operator side as the fire verified it: the ledger ``head`` ``(seq, hash)`` after a verified replay, and the
    agent's ``bar`` ``(open bar?, highest closed bar's revoke_seq or 0)`` from the agent store."""
    head: tuple[int, str]
    bar: tuple[bool, int]


class AuthorityFeed:
    """Reads what the operator-side fire sends the dispatch server (S10b B-pub, design 2.3): its verified ledger head,
    the agent's bar, and the ``authority_log`` rows the server's replica lacks. The server re-verifies every row
    (signatures, chain, anchor) and trusts none of this beyond that."""

    def __init__(self, store: Any, cockpit_root: Path | str) -> None:
        self._store, self._root = store, Path(cockpit_root)

    def read(self, occurrence_id: str) -> AuthorityRead:
        from levain.agent_authority import AgentAuthority
        try:
            with self._store.read() as conn:
                row = conn.execute("SELECT agent_id FROM agent_occurrences WHERE occurrence_id = ?",
                                   (occurrence_id,)).fetchone()
                if row is None or type(row[0]) is not str:
                    raise ProofUnavailable("the occurrence row names no agent", standing=True)
                opened = conn.execute("SELECT 1 FROM agent_bars WHERE agent_id = ? AND closed_at IS NULL",
                                      (row[0],)).fetchone() is not None
                closed = conn.execute("SELECT MAX(revoke_seq) FROM agent_bars WHERE agent_id = ? AND closed_at IS NOT NULL",
                                      (row[0],)).fetchone()[0]
                if closed is not None and type(closed) is not int:   # text or a blob in an INTEGER column: corrupt
                    raise ProofUnavailable("the agent's closed bar names no integer revoke seq", standing=True)
        except ProofUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProofUnavailable(f"the agent store does not read ({type(exc).__name__})", standing=False) from None
        try:
            state = AgentAuthority(self._root).replay()
        except Exception as exc:  # noqa: BLE001 - a replay that fails proves nothing
            raise ProofUnavailable(f"the authority log does not replay ({type(exc).__name__})", standing=False) from None
        return AuthorityRead((state.seq, state.head_hash), (opened, 0 if closed is None else closed))

    def rows(self, after: int, upto: int) -> list[dict[str, object]]:
        """The next chunk of rows with ``after < seq <= upto``, in order, bounded by count and bytes (at least one)."""
        from levain.cockpit.firejournal import DB_NAME
        out: list[dict[str, object]] = []
        size = 0
        try:
            db = sqlite3.connect("file:" + quote(str(self._root / DB_NAME), safe="/") + "?mode=ro", uri=True, timeout=5.0)
            try:
                cur = db.execute("SELECT seq, prev_hash, record, signer_fp, signature, hash FROM authority_log"
                                 " WHERE seq > ? AND seq <= ? ORDER BY seq", (after, upto))
                for got in cur:
                    if type(got[0]) is not int or not all(type(c) is str for c in got[1:]):
                        raise ProofUnavailable(f"log row {got[0]} is not text", standing=False)
                    row = dict(zip(("seq", "prev_hash", "record", "signer_fp", "signature", "hash"), got))
                    size += sum(len(str(v)) for v in got) + 80
                    if out and (len(out) >= _SYNC_CHUNK_ROWS or size > _SYNC_CHUNK_BYTES):
                        break
                    out.append(row)
            finally:
                db.close()
        except sqlite3.Error as exc:
            raise ProofUnavailable(f"the authority log rows do not read ({type(exc).__name__})", standing=False) from None
        return out


# --- the connection ---------------------------------------------------------------------------------------------------

class _Down(Exception):
    """The wire failed (connect, send, receive, off-shape answer, EOF): the connection is closed, nothing is known."""


class _Refused(Exception):
    def __init__(self, name: str, detail: str, reason_class: str) -> None:
        super().__init__(name)
        self.name, self.detail, self.reason_class = name, detail, reason_class


class _Conn:
    """One socket, one request in flight at a time. A fault of any kind closes it (on a holder connection that close is
    the server's cue to END the session, design 3.4) and raises :class:`_Down`."""

    def __init__(self, sock: socket.socket) -> None:
        self._sock: socket.socket | None = sock
        self._mu = threading.Lock()
        self._next = 0

    @property
    def alive(self) -> bool:
        return self._sock is not None

    def close(self) -> None:
        sock, self._sock = self._sock, None
        if sock is None:
            return
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    def request(self, op: str, expect: tuple[str, ...], *, timeout: float = _CONNECT_S, **fields: object) -> dict:
        """The answer's fields (exactly ``expect``), or :class:`_Refused` for a named refusal, or :class:`_Down`."""
        with self._mu:
            sock = self._sock
            if sock is None:
                raise _Down("closed")
            self._next += 1
            rid = self._next
            try:
                frame = encode_run_frame({**fields, "p": PROTOCOL, "op": op, "id": rid})
                sock.settimeout(timeout)
                sock.sendall(frame)
                ans = read_run_frame(sock)
            except (OSError, WireError) as exc:
                self.close()
                raise _Down(f"{op}: {type(exc).__name__}") from None
            if ans is None:
                self.close()
                raise _Down(f"{op}: EOF")
            try:
                return self._shape(ans, rid, expect)
            except WireError as exc:
                self.close()
                raise _Down(f"{op}: {exc}") from None

    @staticmethod
    def _shape(ans: dict, rid: int, expect: tuple[str, ...]) -> dict:
        if type(ans.get("id")) is not int or ans["id"] != rid:
            raise WireError("an answer for another request")
        if ans.get("ok") is True:
            _exact({k: v for k, v in ans.items() if k not in ("id", "ok")}, expect, "answer")
            return {k: v for k, v in ans.items() if k not in ("id", "ok")}
        if ans.get("ok") is False:
            _exact(ans, ("id", "ok", "refused"), "refusal", optional=("detail", "reason_class"))
            name = ans["refused"]
            if type(name) is not str or _NAME_RE.fullmatch(name) is None:
                raise WireError("a refusal name off the grammar")
            detail = ans.get("detail", "")
            if type(detail) is not str:
                raise WireError("a refusal detail that is not text")
            cls = ans.get("reason_class", "standing")
            if cls not in ("transient", "standing"):
                raise WireError("a reason class off the grammar")
            raise _Refused(name, detail[:_DETAIL_MAX], cls)
        raise WireError("an answer that is neither ok nor refused")


def _exit_code(v: object, where: str) -> int:
    if type(v) is not int or not -1024 <= v <= 1024:
        raise WireError(f"{where} is not an exit status")
    return v


# --- the gated child, remote ------------------------------------------------------------------------------------------

class _RemoteProcess:
    """The ``process`` of a :class:`_RemoteChild`: ``poll``, ``wait`` and ``terminate`` as a Popen's. The exit it answers
    is the server's (which holds END behind it) or :data:`levain.agents.controller.GATE_UNPROVEN` once the wire has
    failed."""

    def __init__(self, conn: _Conn | None) -> None:
        self._conn = conn
        self._rc: int | None = None
        self._lost = conn is None
        self._rc_mu = threading.Lock()

    def _publish(self, rc: int) -> int:
        """Record an exit once: the first one stands (a server exit is never downgraded to unproven, nor the reverse
        overwritten by a late racer), and the holder closes after it."""
        with self._rc_mu:
            if self._rc is None:
                self._rc = rc
            out = self._rc
        if self._conn is not None:
            self._conn.close()
        return out

    @property
    def returncode(self) -> int | None:
        return self._rc

    def _lose(self) -> int:
        self._lost = True
        return self._publish(_ctl.GATE_UNPROVEN)       # monotonic: a recorded server exit is never downgraded

    def _slice(self, seconds: float) -> int | None:
        """One WAIT of at most ``seconds``: the exit once the server says so, else None. A fault is unproven."""
        if self._rc is not None:
            return self._rc
        if self._lost or self._conn is None:
            return self._lose()
        try:
            ans = self._conn.request("WAIT", ("exited", "returncode"), timeout=seconds + _ANSWER_MARGIN_S,
                                     timeout_ms=int(max(0.0, seconds) * 1000))
            if type(ans["exited"]) is not bool:
                raise WireError("exited is not a boolean")
            if not ans["exited"]:
                if ans["returncode"] is not None:
                    raise WireError("a returncode before the exit")
                return None
            return self._publish(_exit_code(ans["returncode"], "returncode"))
        except (_Down, _Refused, WireError):
            return self._lose()

    def poll(self) -> int | None:
        return self._slice(0.0)

    def wait(self, timeout: float | None = None) -> int:
        end = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            left = WAIT_SLICE_S if end is None else min(WAIT_SLICE_S, end - time.monotonic())
            got = self._slice(max(0.0, left))
            if got is not None:
                return got
            if end is not None and time.monotonic() >= end:
                raise subprocess.TimeoutExpired("dispatch session", float(timeout or 0.0))

    def terminate(self) -> None:
        """TERM: the server reaches the gate, and END does the real work (design finding G). A fault is silent: the
        closed holder is the server's cue to END anyway."""
        if self._conn is None or self._lost or self._rc is not None:
            return
        try:
            self._conn.request("TERM", ())
        except (_Down, _Refused):
            self._lose()


class _RemoteChild:
    """The gated child the client answers: ``release``, ``abort`` and a ``process`` (design 3.2). ``session`` is the S1
    session id the server answered, None for a child whose LAUNCH answer was lost."""

    def __init__(self, conn: _Conn | None, session: str | None,
                 release_fields: Callable[[_Conn], dict[str, object]] | None = None) -> None:
        self._conn = conn
        self._release_fields = release_fields
        self.session = session
        self.process = _RemoteProcess(conn)
        self.released = False

    def release(self) -> None:
        """RELEASE. A failure raises: the caller (``start_run``, inside the store's lock) aborts the child, and the
        server treats ABORT the same whether or not the release landed (design T3)."""
        if self._conn is None:
            raise RuntimeError("the dispatch run server did not answer LAUNCH: nothing to release")
        try:
            # the fire's fresh verified head and the agent's fresh bar, SYNCed first: the server re-judges the link and
            # the bar against its replica at RELEASE (L3 r1: a bar opened after LAUNCH was not seen)
            fields = self._release_fields(self._conn) if self._release_fields is not None else {}
            self._conn.request("RELEASE", (), **fields)
        except LauncherRefused as exc:
            raise RuntimeError(f"the authority could not be synced for RELEASE: {exc.name}") from None
        except (_Down, WireError, KeyError) as exc:
            # an off-shape SYNC answer (WireError, KeyError) leaves the connection open: closed here, as spawn does
            self._conn.close()
            self.process._lose()
            raise RuntimeError(f"the dispatch run server is unreachable at RELEASE ({type(exc).__name__}: {exc})") \
                from None
        except _Refused as exc:
            raise RuntimeError(f"the dispatch run server refused RELEASE: {exc.name}") from None
        self.released = True

    def abort(self, timeout: float = 5.0) -> int:
        """ABORT: nothing runs, or the session is ENDed. The exit is the server's, GATE_UNPROVEN when it cannot say."""
        if self.process._rc is not None:
            return self.process._rc
        if self._conn is None or self.process._lost:
            return self.process._lose()
        try:
            ans = self._conn.request("ABORT", ("returncode",), timeout=max(0.0, float(timeout)) + _ANSWER_MARGIN_S,
                                     timeout_ms=int(max(0.0, float(timeout)) * 1000))
            rc = _exit_code(ans["returncode"], "returncode")
        except Exception:  # noqa: BLE001 - abort never raises (a nan or inf timeout included): unproven
            return self.process._lose()
        return self.process._publish(rc)


# --- the launcher --------------------------------------------------------------------------------------------------------

class WorkspaceNotSent(LauncherRefused):
    """The workspace could not be staged, and no LAUNCH was sent: nothing is live on the server, nothing was spent.
    ``nothing_sent`` is what :func:`levain.agents.session.run_session` reads to settle the run at zero (a spawn that
    raised some other error is read as one that may have started a session)."""
    nothing_sent = True


class LaunchNotSent(LauncherRefused):
    """A launch refused on the operator uid before its LAUNCH frame was sent (no proof, a frame too big, no socket, a
    SYNC that was refused or lost its wire): nothing reached the server that can start a process, so
    :func:`levain.agents.session.run_session` settles the run at zero (``nothing_sent``). A refusal the SERVER answers
    to LAUNCH is not this: the server records its own refusals before a spawn as zero-usage, and its usage answer is
    what settles them."""
    nothing_sent = True


def default_run_socket() -> Path:
    """[judged by this slice, S10b confirms] where the run server listens: ``dispatch/run.sock`` under the host's run
    directory (:data:`levain.slot.layout.RUN`). S10b owns the path; a launcher built with another ``connect`` ignores it."""
    from levain.slot import layout
    return layout.RUN["darwin" if sys.platform == "darwin" else "linux"] / "dispatch" / "run.sock"


#: the brokered run server's socket, in the same directory as ``run.sock``: the billing decision is which socket a
#: launch reaches (the server module serves it under this name)
BROKERED_SOCKET_NAME = "run-brokered.sock"


def default_brokered_socket() -> Path:
    """Where the brokered run server listens: :data:`BROKERED_SOCKET_NAME` beside :func:`default_run_socket`."""
    return default_run_socket().parent / BROKERED_SOCKET_NAME


class BrokeredServerAbsent(LauncherRefused):
    """A brokered launch found no brokered run server: it is turned off (root's ``L/policy/dispatch_auth`` holds
    ``passthrough``) or its service is down. Nothing was sent and the launch does not go to the other socket."""
    nothing_sent = True


def session_auth_of(store: Any) -> Callable[[str], str | None]:
    """``auth_of(session_key)`` over the agent store: the auth the session's occurrence launched under, which is its
    reservation's billing (a reservation with no billing is an api_key one, as :func:`levain.agents.session.reserved_terms`
    reads it). None when the store holds no such session or a billing this client does not know: the caller reads
    that as passthrough's."""
    def auth_of(session_key: str) -> str | None:
        with store.read() as conn:
            row = conn.execute("SELECT r.billing FROM agent_occurrences o JOIN agent_reservations r"
                               " ON r.occurrence_id = o.occurrence_id WHERE o.session_key = ?", (session_key,)).fetchone()
        return None if row is None else AUTH_FOR_BILLING.get(row[0] or "api_key")
    return auth_of


def unix_connector(path: Path | str, *, timeout: float = _CONNECT_S) -> Callable[[], socket.socket]:
    def connect(within: float | None = None) -> socket.socket:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(timeout if within is None else max(0.001, min(timeout, within)))
            sock.connect(str(path))
        except OSError:
            sock.close()
            raise
        return sock
    connect.accepts_within = True  # type: ignore[attr-defined]  # end_session caps the connect to its remaining deadline
    return connect


def _head_of(ans: dict) -> tuple[int, str]:
    h = _exact(ans["head"], ("seq", "hash"), "head")
    return _int(h["seq"], "head.seq"), _str(h["hash"], "head.hash", max_len=128)


def _sync_to(conn: _Conn, feed: AuthorityFeed, head: tuple[int, str]) -> None:
    """Bring the server's replica to ``head``: read its head (an empty SYNC), send the rows it lacks in chunks, and
    require it to land on ``head`` or past it (another fire's SYNC; the server then judges that its replica HOLDS ``head``).
    ``ledger-stale`` (another fire SYNCed first) is already-synced after ONE more head read; the same seq with another
    hash is ``ledger-fork``; ``ledger-unverifiable`` / ``ledger-behind`` and the rest surface as :class:`_Refused`."""
    for attempt in (0, 1):
        try:
            try:
                have = _head_of(conn.request("SYNC", ("head",), rows=[]))
            except _Refused as exc:
                if exc.name != "ledger-empty":      # an empty replica refuses the head read: it holds nothing yet
                    raise
                have = (0, "")
            while have[0] < head[0]:
                chunk = feed.rows(have[0], head[0])
                if not chunk:
                    raise _Refused("ledger-behind", "the operator's ledger lacks rows its own head names", "transient")
                have = _head_of(conn.request("SYNC", ("head",), rows=chunk))
            break
        except _Refused as exc:
            if exc.name == "ledger-stale" and attempt == 0:
                continue
            raise
    if have == head:
        return
    if have[0] > head[0]:
        # another fire SYNCed past this fire's read: the frame names this head and the SERVER judges whether its
        # replica holds it (held: go on, against the whole replica; another chain: a standing ledger-fork). b2rel.
        return
    raise _Refused("ledger-fork", "the server's ledger and the operator's differ at the same position", "standing")


class DispatchSessionLauncher:
    """A :class:`levain.agents.session.SessionLauncher` and :class:`levain.agents.session.SessionEnder` over the run
    server (module docstring). ``connect()`` returns a connected socket (raising ``OSError``); ``proof_of(occurrence_id)``
    returns the occurrence's :class:`LinkProof` (raising :class:`ProofUnavailable`)."""

    route = S1_DISPATCH
    auth = "passthrough"

    def __init__(self, connect: Callable[[], socket.socket], proof_of: Callable[[str], LinkProof],
                 feed: AuthorityFeed | None = None,
                 signed_of: Callable[[str], SignedRole] | None = None,
                 link_of: Callable[[str], SignedLink] | None = None,
                 connect_brokered: Callable[[], socket.socket] | None = None,
                 auth_of: Callable[[str], str | None] | None = None) -> None:
        self._connect = connect     # the passthrough socket (``run.sock``)
        # the brokered socket, and what says which socket a recorded session belongs to (its auth): both or neither.
        # Without them this client serves passthrough alone, as every launcher class serves its one auth.
        if (connect_brokered is None) != (auth_of is None):
            raise ValueError("connect_brokered and auth_of come together")
        self._connect_brokered = connect_brokered
        self._auth_of = auth_of
        self.auths = frozenset({"passthrough", "brokered"}) if connect_brokered is not None else frozenset({"passthrough"})
        self._proof_of = proof_of
        self._signed_of = signed_of   # None: this client can prove no signed role, so it stages no workspace (W2)
        self._link_of = link_of       # None: no verified link names the role, so it stages no workspace (W2 r1 HIGH)
        self._feed = feed           # None: a server without a replica (its frames carry no ledger_head or bar)

    def _claims(self, conn: _Conn, occurrence_id: str, *, bar: bool) -> dict[str, object]:
        """SYNC the server's replica up to the fire's verified ledger head, then the frame fields naming it (and, with
        ``bar``, the agent's bar as read now: LAUNCH and RELEASE both send it). Raises :class:`LauncherRefused`
        (named; ``ledger-fork`` and the like are standing) or :class:`_Down`."""
        assert self._feed is not None
        try:
            read = self._feed.read(occurrence_id)
            _sync_to(conn, self._feed, read.head)
        except ProofUnavailable as exc:
            raise LauncherRefused("link-proof-unavailable", str(exc),
                                  reason_class="standing" if exc.standing else "transient") from None
        except _Refused as exc:
            raise LauncherRefused(exc.name, exc.detail, reason_class=exc.reason_class) from None
        fields: dict[str, object] = {"ledger_head": {"seq": read.head[0], "hash": read.head[1]}}
        if bar:
            fields["bar"] = {"open": read.bar[0], "closed_revoke_seq": read.bar[1]}
        return fields

    # -- the workspace -------------------------------------------------------------------------------------------------

    def build_workspace(self, launch: SessionLaunch) -> Any:
        """The launch's upload (``levain.dispatch.stager.Upload``), built on the operator uid with NO connection open,
        or None for a launch with no workspace. :meth:`spawn` calls it before it connects: a snapshot of a big repo is
        minutes of local git work, and a connection held for that long pins one of the server's connection permits
        (the server keeps capacity for END and USAGE; a holder that is only waiting on the client defeats it). Raises
        :class:`WorkspaceNotSent`: nothing is open, nothing was sent. A host fault (git could not run) keeps its own
        name and is ``transient``; every other refusal is :data:`WORKSPACE_REFUSED`. The default is
        ``levain.dispatch.stager`` (operator-uid code, imported here and nowhere else: this module is in the dispatch
        run server's closure)."""
        ws = launch.workspace
        if ws is None:
            return None
        from levain.agents.signed_role import SignedRoleRefused, signed_withhold
        from levain.dispatch import stager
        try:
            signed = self._signed(launch)
            if self._link_of is None:
                raise ProofUnavailable("this client holds no signed-link source", standing=True)
            link = self._link_of(launch.occurrence_id)
            # the operator-uid half of "refuse on loosening" (np-c9e8a60b (a)): the withhold this snapshot is cut under is
            # the one the SIGNED role declares, derived from the role's own hash chain against the role the verified link
            # names (its agent_id and role_hash, not only its spec_hash: L3 W2 r1 HIGH), here and again by the server
            want = signed_withhold(signed, link)
        except (ProofUnavailable, SignedRoleRefused) as exc:
            name = exc.name if isinstance(exc, SignedRoleRefused) else "signed-role-unavailable"
            raise WorkspaceNotSent(WORKSPACE_REFUSED, f"{name}: {exc}"[:_DETAIL_MAX]) from None
        try:
            return stager.build_upload(ws, signed_withhold=want)
        except stager.StageRefused as exc:
            detail = f"{exc.name}: {exc.detail}"[:_DETAIL_MAX]
            if exc.transient:
                raise WorkspaceNotSent(exc.name, detail, reason_class="transient") from None
            raise WorkspaceNotSent(WORKSPACE_REFUSED, detail) from None
        except Exception as exc:  # noqa: BLE001 - a sender that broke authorises nothing; the cause is the type only
            _log.warning("the workspace for %s could not be built: %s", launch.session_key, type(exc).__name__)
            raise WorkspaceNotSent(WORKSPACE_REFUSED, f"stage-failed: {type(exc).__name__}") from None

    def _signed(self, launch: SessionLaunch) -> SignedRole:
        if self._signed_of is None:
            raise ProofUnavailable("this client holds no signed-role source", standing=True)
        return self._signed_of(launch.occurrence_id)

    def stage_workspace(self, conn: _Conn, launch: SessionLaunch, upload: Any = None) -> None:
        """Upload the launch's workspace snapshot on ``conn``, ahead of LAUNCH on the same connection (levain 34, W1).
        ``upload`` is what :meth:`build_workspace` built before the connection was opened (None: built here, for a
        caller that has no earlier step). Returns when every byte is acknowledged, else raises :class:`WorkspaceNotSent`
        (nothing sent, the run server holds nothing it will launch): a refusal of the sender itself or of the server's
        upload ops, or a lost wire. The seam a different sender overrides, with :meth:`build_workspace`."""
        ws = launch.workspace
        if ws is None:
            return
        up = upload if upload is not None else self.build_workspace(launch)
        key = launch.session_key
        try:
            conn.request("WSBEGIN", (), **ws_begin_to_wire(key, up.manifest, up.bundle))
            for part, data in (("manifest", up.manifest), ("bundle", up.bundle)):
                for frame in ws_chunk_frames(key, part, data):
                    ans = conn.request("WSDATA", ("received",), **frame)
                    sent = frame["offset"] + len(base64.b64decode(frame["data"]))
                    if ans["received"] != sent:
                        conn.close()
                        raise WorkspaceNotSent(WORKSPACE_REFUSED, f"upload-ack: the server holds {ans['received']} "
                                               f"bytes of the {part}, {sent} were sent")
        except _Refused as exc:
            raise WorkspaceNotSent(exc.name, exc.detail, reason_class=exc.reason_class) from None
        except (_Down, WireError) as exc:
            conn.close()
            raise WorkspaceNotSent("dispatch-unreachable", f"WSDATA: {type(exc).__name__}",
                                   reason_class="transient") from None

    # -- spawn ---------------------------------------------------------------------------------------------------------

    def spawn(self, launch: SessionLaunch) -> Any:
        # the snapshot first: minutes of local git work on a big repo, done before the proof is read (so the proof the
        # server judges is current) and before any connection is open (so none is pinned while this builds)
        upload = self.build_workspace(launch)
        try:
            proof = self._proof_of(launch.occurrence_id)
        except ProofUnavailable as exc:
            raise LaunchNotSent("link-proof-unavailable", str(exc),
                                reason_class="standing" if exc.standing else "transient") from None
        if launch.workspace is not None:     # the signed role rides only a launch that stages a workspace
            try:
                proof = dataclasses.replace(proof, signed=self._signed(launch))
            except ProofUnavailable as exc:
                raise LaunchNotSent("link-proof-unavailable", str(exc),
                                    reason_class="standing" if exc.standing else "transient") from None
        body = launch_to_wire(launch, proof)
        try:
            # worst-case claims (the largest seq and hash the wire admits), so a body that passes here cannot overflow
            # once the real ones are added (L3 r1)
            extra = ({"ledger_head": {"seq": _INT_MAX, "hash": "0" * 128},
                      "bar": {"open": False, "closed_revoke_seq": _INT_MAX}} if self._feed is not None else {})
            encode_run_frame({**body, **extra, "p": PROTOCOL, "op": "LAUNCH", "id": 1})      # too big: refused before any connect
        except WireError as exc:
            raise LaunchNotSent("launch-frame-refused", str(exc)) from None
        try:
            sock = self._connector(launch.auth)()
        except BrokeredServerAbsent:
            raise
        except OSError as exc:
            if launch.auth == "brokered":
                raise BrokeredServerAbsent(
                    "brokered-server-absent", "no brokered run server answers: it is turned off (L/policy/dispatch_auth "
                    "holds 'passthrough') or its service is down", reason_class="transient") from None
            raise LaunchNotSent("dispatch-unreachable", f"{type(exc).__name__}", reason_class="transient") from None
        conn = _Conn(sock)
        if self._feed is not None:
            try:
                body = {**body, **self._claims(conn, launch.occurrence_id, bar=True)}
            except LauncherRefused as exc:
                conn.close()
                raise LaunchNotSent(exc.name, exc.detail, reason_class=exc.reason_class) from None
            except (_Down, WireError, KeyError) as exc:     # nothing was launched yet: a plain transient refusal
                conn.close()
                raise LaunchNotSent("dispatch-unreachable", f"SYNC: {type(exc).__name__}", reason_class="transient") from None
        if launch.workspace is not None:
            try:
                self.stage_workspace(conn, launch, upload)  # before LAUNCH, on this connection; raises or has staged
            except BaseException:
                conn.close()
                raise
        try:
            ans = conn.request("LAUNCH", ("session",), timeout=LAUNCH_ANSWER_S, **body)
            session = _str(ans["session"], "session", max_len=128)
        except _Refused as exc:
            conn.close()
            raise LauncherRefused(exc.name, exc.detail, reason_class=exc.reason_class) from None
        except (_Down, WireError, KeyError) as exc:
            # a classified refusal (_Refused) was surfaced above; only these are a lost answer. The request may have
            # been delivered: the server may hold a session, so this must not raise (a raising spawn promises nothing
            # is live). The closed holder is the server's cue to END; this child proves nothing.
            _log.warning("LAUNCH for %s lost its answer (%s): returning a down child", launch.session_key, exc)
            conn.close()
            return _RemoteChild(None, None)
        occ = launch.occurrence_id
        return _RemoteChild(conn, session,
                            (lambda c: self._claims(c, occ, bar=True)) if self._feed is not None else None)

    # -- usage and end, keyed by the recorded session key -----------------------------------------------------------------

    def _connector(self, auth: str) -> Callable[..., socket.socket]:
        """The connector of ``auth``'s socket. Nothing here falls back from one socket to the other: an auth this
        client does not serve (a brokered launch with no brokered socket configured) is a refusal."""
        if auth == "passthrough":
            return self._connect
        if auth == "brokered" and self._connect_brokered is not None:
            return self._connect_brokered
        raise BrokeredServerAbsent("brokered-server-absent", "this client holds no brokered run server socket",
                                   reason_class="standing")

    def _connector_for_session(self, session_key: str) -> Callable[..., socket.socket]:
        """The connector of the socket a recorded session was launched through: the auth the store recorded for the
        key; a session the store records no auth for is passthrough's (as the server reads an index record with no
        auth). One socket is chosen and never a second tried. With no ``auth_of`` this client serves passthrough alone."""
        if self._auth_of is None:
            return self._connect
        return self._connector(self._auth_of(session_key) or "passthrough")

    def _short(self, op: str, expect: tuple[str, ...], timeout: float, **fields: object) -> dict:
        sock = self._connector_for_session(fields["session_key"])()
        conn = _Conn(sock)
        try:
            return conn.request(op, expect, timeout=timeout, **fields)
        finally:
            conn.close()

    def usage(self, session_key: str) -> Usage:
        """The server's usage for the key; never raises. Anything unreadable is the caps."""
        try:
            if type(session_key) is not str or _ctl.SESSION_KEY_RE.fullmatch(session_key) is None:
                return Usage(None, None, None, "session:dispatch-bad-key")
            ans = self._short("USAGE", ("usage",), _CONNECT_S, session_key=session_key)
            return usage_from_wire(ans["usage"])
        except WireError:
            return Usage(None, None, None, "session:dispatch-protocol")
        except Exception:  # noqa: BLE001 - usage() never raises; unreadable debits the whole reservation
            return Usage(None, None, None, "session:dispatch-unreachable")

    def end_session(self, session_key: str, deadline_s: float) -> StopResult:
        """The S1 END for the key (``SessionEnder``): ``StopResult(True, ...)`` only when the server answers
        ``terminated: true`` exactly. Never raises."""
        try:
            if type(session_key) is not str or _ctl.SESSION_KEY_RE.fullmatch(session_key) is None:
                return StopResult(False, "the session key is off the grammar; nothing asked")
            deadline = max(0.0, float(deadline_s))
            if not deadline < float("inf"):
                raise ValueError("deadline is not finite")
            # ONE absolute deadline, set before the connect: the connect, the read and the server's hold all draw on
            # it, and no answer margin extends it (the server is asked for a little less than is left)
            end = time.monotonic() + deadline
            connect = self._connector_for_session(session_key)
            sock = connect(max(0.001, deadline)) if getattr(connect, "accepts_within", False) else connect()
            conn = _Conn(sock)
            try:
                left = end - time.monotonic()
                if left <= 0:
                    return StopResult(False, "the deadline was spent connecting; nothing assumed ended")
                ans = conn.request("END", ("terminated", "detail"), timeout=left, session_key=session_key,
                                   deadline_ms=int(max(0.0, left - min(1.0, left * 0.2)) * 1000))
            finally:
                conn.close()
            if type(ans["detail"]) is not str:
                raise WireError("detail is not text")
            detail = ans["detail"][:_DETAIL_MAX]            # a long detail never undoes a proven stop
            if ans["terminated"] is True:
                return StopResult(True, detail)
            if ans["terminated"] is False:
                return StopResult(False, detail)
            raise WireError("terminated is not a boolean")
        except _Refused as exc:
            return StopResult(False, f"the dispatch run server refused END: {exc.name}")
        except (_Down, WireError, OSError) as exc:
            return StopResult(False, f"the dispatch run server did not answer END ({type(exc).__name__}); "
                                     "nothing assumed ended")
        except Exception as exc:  # noqa: BLE001 - end_session never raises
            return StopResult(False, f"END could not complete ({type(exc).__name__}); nothing assumed ended")


def build_launcher(store: Any) -> DispatchSessionLauncher:
    """The builder ``session_wire`` registers under :data:`LAUNCHER_NAME`: the production client for ``store`` (its
    proofs read the install's cockpit authority log; a passthrough run goes to :func:`default_run_socket`, a brokered
    one to :func:`default_brokered_socket`, and a recorded session's usage and end to the one its store auth names)."""
    from levain.agents.session_wire import read_install
    install = read_install(store.directory)
    return DispatchSessionLauncher(unix_connector(default_run_socket()),
                                   lambda occ: link_proof(store, install.cockpit_root, occ),
                                   AuthorityFeed(store, install.cockpit_root),
                                   lambda occ: signed_role_of(store, occ),
                                   lambda occ: signed_link_of(store, install.cockpit_root, occ),
                                   unix_connector(default_brokered_socket()), session_auth_of(store))
