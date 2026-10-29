"""The 7a identity gate's first slice (CONTRACT §7.6, §5.1 ``confirm-identity``; 1027-18 RULINGS "Identity gate"
IG-2, IG-3, IG-4 G1): the per-vendor identity call, the observed id root records for a ``login`` session, and the
baseline ``confirm-identity yes`` writes.

* The identity call (:func:`identity_call`): one function per vendor. Codex has no server-validated call, so its
  observed id is the literal :data:`NOT_A_CHECK` (IG-2, published per §7.6 "not a check"). Claude's (IG-3, G1b):
  :mod:`levain.slot.identity_child` run AS THE SLOT through wall's ``identity`` target (start's
  :func:`levain.slot.start.identity_capture`), its line judged by root (:func:`parse_profile_answer`): the profile's
  ``account.uuid``.
* A ``login`` session's ``start`` runs it as its last step, after the vendor exits (:func:`record_login_identity`),
  and CASes the result into the state record's ``identity``: ``{observed_id}`` when the call answered, else
  ``{result: <refusal name>}``. A vendor that exited non-zero is not asked: ``{result: login-exit-nonzero}``.
* ``confirm-identity <session> yes|no`` (:func:`confirm_identity`): the operator only, alloc kind ``login`` only,
  under the session lock and the session's lease (a field-only CAS on a live state, which CONTROL refuses
  ``lease-dead``). ``yes`` writes ``L/slots/e<n>/baseline`` (:data:`BASELINE_FORMAT`) from the record's
  ``identity.observed_id`` and the alloc's vendor and session, never from anything the slot wrote; ``no`` writes
  nothing. A record with no observed id refuses ``no-observed-id[:<result>]``.

* An interactive passthrough session's ``start`` runs the same call before the vendor (:func:`gate_check`, with the
  baseline from :func:`read_slot_baseline`): the answer is recorded in the state record's ``identity`` and compared
  with the baseline's ``observed_id``; a difference refuses ``identity-changed``.

* Claude's child refreshes the stored login once before it asks the profile (PIN_REFRESH_DESIGN), so the account it
  names is the one the file's own refresh token belongs to. A refresh the server refuses as a dead login is
  ``identity-expired``; one that did not reach an answer is ``identity-unreachable`` (:func:`parse_profile_answer`).

The baseline file is root's, 0600, replaced atomically (temp, fsync, rename, directory fsync). :func:`baseline_vendor`
reads it, and also the earlier vendor-only shape with no ``format`` key.
"""
from __future__ import annotations

import os
import re
import stat
from typing import Callable, Dict, Mapping, Optional

from levain.contracts import ContractError, canon
from levain.slot import Refused
from levain.slot import records as R
from levain.slot import runtime_helper as H
from levain.slot.grammar import SESSION_RE
from levain.slot.runtime_grammar import _NAME_RE as _WIRE_NAME_RE
from levain.slot.runtime_grammar import OPERATOR, RuntimeRequest

BASELINE_FORMAT = "levain-baseline/1"
BASELINE_KEYS = frozenset({"format", "vendor", "observed_id", "confirmed_at", "session"})
BASELINE_NAME = "baseline"
BASELINE_MODE = 0o600
#: IG-2: the observed id of a vendor with no server-validated identity call.
NOT_A_CHECK = "not-a-check"
CONFIRMED = "confirmed"
LOGIN_FAILED = "login-exit-nonzero"
#: ``confirm-identity yes``'s answer once the file is written: the supervisor says "written" on this only.
BASELINE_WRITTEN = "written"
#: the states a session's lease is live in past render (§7.3): the only ones confirm runs in.
_RUNNING = (R.ACTIVE, R.BRAKED)
_VENDORS = ("claude", "codex")


# --- the identity call (§7.6) ------------------------------------------------------------------------------------------

def _codex_call(ctx: Optional[H.Ctx], a: Optional[R.Alloc], *, held: bool = False) -> str:
    return NOT_A_CHECK


def _claude_call(ctx: Optional[H.Ctx], a: Optional[R.Alloc], *, held: bool = False) -> str:
    """IG-3: the child spawned as the slot (start's :func:`~levain.slot.start.identity_capture`), its line judged
    here. Only for a session: with no ``ctx`` or alloc there is nothing to run as. ``held``: the caller (start's
    pre-launch gate) already holds the session END lock and the record is RENDERED, so the lock-free
    :func:`~levain.slot.start.identity_capture_held` runs, admitting RENDERED only."""
    if ctx is None or a is None:
        raise Refused("internal", "the Claude identity call runs for a session")
    from levain.slot import start as SS

    if held:
        return parse_profile_answer(*SS.identity_capture_held(ctx, a, states=(R.RENDERED,)))
    return parse_profile_answer(*SS.identity_capture(ctx, a))


IDENTITY_CALLS: Mapping[str, Callable[..., str]] = {"codex": _codex_call, "claude": _claude_call}


def identity_call(ctx: Optional[H.Ctx], vendor: str, a: Optional[R.Alloc] = None, *, held: bool = False) -> str:
    """The stable account id the vendor's identity call observed, checked against §7.6's pattern (else
    ``identity-unparsable``), or :class:`Refused` by the call's own name. ``held`` reaches the vendor's callable
    (Codex ignores it)."""
    call = IDENTITY_CALLS.get(vendor)
    if call is None:
        raise Refused("identity-call-unbuilt", vendor[:32])
    oid = call(ctx, a, held=held)
    if type(oid) is not str or not R.OBSERVED_ID_RE.fullmatch(oid):
        raise Refused("identity-unparsable", vendor)
    return oid


_OAUTH_WORD_RE = re.compile(r"[a-z_]{1,40}", re.ASCII)
#: the child's fixed names for a refresh with no usable answer (``identity_child``): words, never a status or a token.
_FIXED_NAME_RE = re.compile(r"[a-z\-]{1,32}", re.ASCII)

#: §7.6: root reads at most this much of the identity call's stdout.
PROFILE_ANSWER_MAX = 64 * 1024


def parse_profile_answer(exit_code: int, out: bytes) -> str:
    """IG-3 / PIN_REFRESH_DESIGN §4: root's judgement of :mod:`levain.slot.identity_child`'s line. The observed id is the
    profile's ``account.uuid`` when the child exited 0, its line is within :data:`PROFILE_ANSWER_MAX`, the refresh (when
    the line carries one) answered 200, the profile answered 200 and the body is JSON holding a uuid that fits
    ``OBSERVED_ID_RE``. A refresh outcome is judged first, whatever the exit code:

    * ``identity-expired``: the refresh answered 400 with the OAuth word ``invalid_grant``, or 401/403 with an OAuth
      word at all; or the refresh was 200 and the profile 401/403.
    * ``identity-unreachable``: every other refresh that did not answer 200 (a fixed name instead of a status, 429,
      5xx, a 403 with no OAuth word, any other 4xx, a status other than 200 that is not an error). Both fail closed.

    A line without a ``refresh`` key is a profile-only answer (judged as before); anything else is
    ``identity-unparsable``."""
    import json

    def bad(why: str) -> Refused:
        return Refused("identity-unparsable", why)

    if len(out) > PROFILE_ANSWER_MAX:
        raise bad("answer over 64 KiB")
    try:
        ans = json.loads(out.decode("utf-8"))
    except (ValueError, RecursionError):      # UnicodeDecodeError is a ValueError
        if exit_code != 0:
            raise bad(f"identity call exit {exit_code}") from None
        raise bad("answer is not JSON") from None
    if type(ans) is dict and "refresh" in ans:
        ref = ans["refresh"]
        word = ans.get("oauth_error")
        word = word if type(word) is str and _OAUTH_WORD_RE.fullmatch(word) else None
        if type(ref) is str:
            if not _FIXED_NAME_RE.fullmatch(ref):
                raise bad("refresh name")
            raise Refused(UNREACHABLE, f"the refresh did not reach an answer ({ref})")
        if type(ref) is not int:
            raise bad("refresh outcome")
        if ref != 200:
            if (ref == 400 and word == "invalid_grant") or (ref in (401, 403) and word is not None):
                raise Refused(EXPIRED, f"the refresh answered HTTP {ref} {word} (the stored login looks expired or "
                                       "revoked: sign in to this slot again)")
            raise Refused(UNREACHABLE, f"the refresh answered HTTP {ref}" + (f" {word}" if word else ""))
    if exit_code != 0:
        raise bad(f"identity call exit {exit_code}")
    if type(ans) is not dict or type(ans.get("status")) is not int:
        raise bad("no status")
    if ans["status"] in (401, 403):
        # a distinct name: the supervisor routes on the name alone and must tell the operator to sign in again
        raise Refused(EXPIRED, f"HTTP {ans['status']} (the stored login looks expired or revoked: sign in to this slot "
                               "again)")
    if ans["status"] != 200:
        raise bad(f"HTTP {ans['status']}")
    try:
        prof = json.loads(ans["body"]) if type(ans.get("body")) is str else None
    except (ValueError, RecursionError):
        prof = None
    acct = prof.get("account") if type(prof) is dict else None
    uuid = acct.get("uuid") if type(acct) is dict else None
    if type(uuid) is not str or not R.OBSERVED_ID_RE.fullmatch(uuid):
        raise bad("no account.uuid")
    return uuid


def read_record(ctx: H.Ctx, session: str) -> Optional[R.SessionState]:
    """CONTROL's state record (§7.2), or None when absent: read as a HINT for a CAS CONTROL checks (§7.1)."""
    data = ctx.store.read(("sessions",), f"{session}.json", trusted=False)
    return R.parse_state(data) if data is not None else None


def _cas_identity(ctx: H.Ctx, session: str, identity: Mapping[str, str]) -> R.SessionState:
    """A field-only CAS of ``identity`` on a running record (the state record read as the CAS's hint, §7.1): CONTROL
    refuses it ``lease-dead`` when the session's lease is gone and ``stale-generation`` when the record moved."""
    rec = read_record(ctx, session)
    if rec is None:
        raise Refused("no-session", session)
    if rec.state not in _RUNNING:
        raise Refused(f"not-active:{rec.state}", session)
    ctx.call("CAS", session=session, generation=rec.generation, **{"from": rec.state, "to": rec.state},
             fields={"identity": dict(identity)})
    return rec


def record_login_identity(ctx: H.Ctx, a: R.Alloc, exit_code: int) -> Dict[str, str]:
    """``start``'s last step for a ``login`` session, once the vendor exited: the identity call's answer (or its
    refusal's name) CASed into the state record. Returns what was recorded."""
    if exit_code != 0:
        ident = {"result": LOGIN_FAILED}
    else:
        try:
            ident = {"observed_id": identity_call(ctx, a.vendor, a)}
        except Refused as e:
            ident = {"result": e.name}
    _cas_identity(ctx, a.session, ident)
    return ident


# --- the baseline file (§2.1 ``L/slots/e<n>/baseline``) ----------------------------------------------------------------

def baseline_bytes(*, vendor: str, observed_id: str, confirmed_at: str, session: str) -> bytes:
    obj = {"format": BASELINE_FORMAT, "vendor": vendor, "observed_id": observed_id, "confirmed_at": confirmed_at,
           "session": session}
    check_baseline(obj)
    return canon.canonical_bytes(obj)


def check_baseline(obj: object) -> Dict[str, str]:
    """A :data:`BASELINE_FORMAT` object checked whole (exact keys, each value by its rule), else ``bad-baseline``."""
    if type(obj) is not dict or set(obj) != BASELINE_KEYS or obj["format"] != BASELINE_FORMAT:
        raise Refused("bad-baseline", "keys or format")
    if obj["vendor"] not in _VENDORS:
        raise Refused("bad-baseline", "vendor")
    if type(obj["observed_id"]) is not str or not R.OBSERVED_ID_RE.fullmatch(obj["observed_id"]):
        raise Refused("bad-baseline", "observed_id")
    if type(obj["session"]) is not str or not SESSION_RE.fullmatch(obj["session"]):
        raise Refused("bad-baseline", "session")
    try:
        R.check_timestamp(obj["confirmed_at"], "baseline.confirmed_at")
    except Refused:
        raise Refused("bad-baseline", "confirmed_at") from None
    return dict(obj)


def baseline_vendor(data: Optional[bytes]) -> Optional[str]:
    """The vendor a slot's baseline names: ``None`` when there is no file, ``"?"`` when it does not parse. A
    :data:`BASELINE_FORMAT` file must pass :func:`check_baseline` whole; a file with no ``format`` key is the
    earlier vendor-only shape, read by its ``vendor`` alone."""
    if data is None:
        return None
    try:
        obj = canon.parse(data)
    except ContractError:
        return "?"
    if type(obj) is not dict:
        return "?"
    if "format" in obj:
        try:
            return check_baseline(obj)["vendor"]
        except Refused:
            return "?"
    v = obj.get("vendor")
    return v if v in _VENDORS else "?"


# --- the start gate (CONTRACT §7.6; DESIGN g3 §1-§2) --------------------------------------------------------------------

#: DECISION: the most a baseline file may hold; ``baseline_bytes`` writes well under this.
BASELINE_MAX_BYTES = 4096
#: the record's ``identity.result`` for a gate that matched, that found another account, and a vendor with no check.
GATE_MATCHED = "matched"
GATE_CHANGED = "identity-changed"
#: the record's ``identity.result`` for a parked login the server no longer accepts (a refresh answered
#: ``invalid_grant`` or refused with an OAuth error body, or a profile 401/403 on the refreshed token), and for a
#: check that could not reach an answer (a refresh with no usable answer: network, 429, 5xx, a non-OAuth 403, an
#: unwritable file). The second asks for a retry, never a new login.
EXPIRED = "identity-expired"
UNREACHABLE = "identity-unreachable"
_BASELINE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_NOCTTY | os.O_CLOEXEC


def read_slot_baseline(ctx: H.Ctx, a: R.Alloc) -> Dict[str, str]:
    """``L/slots/<tag>/baseline`` for alloc's slot, read by dirfd with ``O_NOFOLLOW``: a regular file, trusted owner
    (``seams.trusted``), at most :data:`BASELINE_MAX_BYTES`, passing :func:`check_baseline` whole and naming alloc's
    vendor. Absent = ``no-baseline:<tag>``; any other failure (the earlier vendor-only shape included) =
    ``bad-baseline`` with the reason in the detail."""
    tag = a.slot.tag
    with ctx.store.dir("slots", tag) as sfd:
        try:
            fd = os.open(BASELINE_NAME, _BASELINE_FLAGS, dir_fd=sfd)
        except FileNotFoundError:
            raise Refused(f"no-baseline:{tag}", f"slots/{tag}/baseline is absent") from None
        except OSError as e:
            raise Refused("bad-baseline", f"open: {e.strerror or e}") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Refused("bad-baseline", "not a regular file")
        if not ctx.seams.trusted(st):
            raise Refused("bad-baseline", "owner")
        if st.st_size > BASELINE_MAX_BYTES:
            raise Refused("bad-baseline", f"{st.st_size} bytes")
        buf = bytearray()
        while len(buf) <= BASELINE_MAX_BYTES:   # one read may answer short: read to EOF, never past the cap + 1
            try:
                chunk = os.read(fd, BASELINE_MAX_BYTES + 1 - len(buf))
            except OSError as e:
                raise Refused("bad-baseline", f"read: {e.strerror or e}") from None
            if not chunk:
                break
            buf += chunk
        data = bytes(buf)
    finally:
        os.close(fd)
    if len(data) > BASELINE_MAX_BYTES:
        raise Refused("bad-baseline", "oversize")
    try:
        obj = canon.parse(data)
    except ContractError:
        raise Refused("bad-baseline", "not canonical JSON") from None
    base = check_baseline(obj)
    if base["vendor"] != a.vendor:
        raise Refused("bad-baseline", f"names {base['vendor']}, not {a.vendor}")
    return base


def _cas_gate_identity(ctx: H.Ctx, session: str, identity: Mapping[str, str]) -> None:
    """A field-only RENDERED -> RENDERED CAS of ``identity`` (the lease must be live: CONTROL's ``lease-dead``)."""
    rec = read_record(ctx, session)
    if rec is None:
        raise Refused("no-session", session)
    if rec.state != R.RENDERED:
        raise Refused("not-rendered", f"{session}: {rec.state}")
    ctx.call("CAS", session=session, generation=rec.generation, **{"from": R.RENDERED, "to": R.RENDERED},
             fields={"identity": dict(identity)})


def gate_check(ctx: H.Ctx, a: R.Alloc, baseline: Mapping[str, str]) -> Dict[str, str]:
    """§7.6 at start: the identity call (the caller holds the session END lock, ``held``), its answer recorded
    (:func:`_cas_gate_identity`) and compared with ``baseline``. Returns what was recorded, for an equal answer.
    A call that refused is recorded ``{result: <name>}`` and raised by that name; an answer that differs is recorded
    ``{observed_id, result: identity-changed}`` and raised ``identity-changed``; a refused record CAS is raised by
    its own name, so a launch never outruns the record (after a refused call, that call's name is kept in the
    detail). The answer is equal when the id and the vendor match and
    "not a check" belongs to the vendor with no check only."""
    try:
        oid = identity_call(ctx, a.vendor, a, held=True)
    except Refused as e:
        try:
            _cas_gate_identity(ctx, a.session, {"result": e.name})
        except Refused as ce:
            # the CAS's name is the answer (no launch outruns the record); the call's own cause stays in the detail
            raise Refused(ce.name, (f"{ce.detail}; " if ce.detail else "") + f"the identity call had refused {e.name}"
                          + (f": {e.detail}" if e.detail else "")) from None
        raise
    same = (oid == baseline["observed_id"] and baseline["vendor"] == a.vendor
            and (oid == NOT_A_CHECK) == (a.vendor == "codex"))
    ident = {"observed_id": oid, "result": (NOT_A_CHECK if oid == NOT_A_CHECK else GATE_MATCHED) if same
             else GATE_CHANGED}
    _cas_gate_identity(ctx, a.session, ident)
    if not same:
        raise Refused(GATE_CHANGED, f"{a.slot.tag}: the observed account is not the slot's baseline")
    return ident


# --- confirm-identity (§5.1) -------------------------------------------------------------------------------------------

def _no_id_name(result: object) -> str:
    name = f"no-observed-id:{result}" if type(result) is str else "no-observed-id"
    return name if _WIRE_NAME_RE.fullmatch(name) and len(name) <= 64 else "no-observed-id"


def _slot_names_lease(ctx: H.Ctx, a: R.Alloc) -> bool:
    raw = ctx.store.read(("slots", a.slot.tag), "state")
    if raw is None:
        return False
    try:
        st = H.parse_slot_state_of(raw, a.slot)
    except (ValueError, RecursionError):
        raise Refused("slot-state-malformed", a.slot.tag) from None
    return st.state == "leased" and st.session == a.session and st.lease_seq == a.lease_seq


def confirm_identity(ctx: H.Ctx, req: RuntimeRequest, blob: None) -> Dict[str, object]:
    """§5.1 ``confirm-identity <session> yes|no``. In order: a session and a yes|no answer (``no-session``,
    ``bad-answer``: refused, never asserted, though the grammar already checks both); the operator only (the grammar
    already refuses ``_levain_dispatch``); the caller's alloc (:func:`runtime_helper.read_alloc`), kind ``login`` (else
    ``confirm-kind:<kind>``); the session lock (TRY, ``session-busy``: END takes the same one, so a baseline write and
    the park never overlap); the record running (``not-active:<state>``). ``no`` ends there, writing nothing.
    ``yes``: the record's ``identity.observed_id`` (else ``no-observed-id[:<result>]``); the slot's state still names
    this lease (else ``slot-lease-moved``; a state file that does not parse, ``slot-state-malformed``); the
    field-only CAS of ``identity`` = ``{observed_id, result: confirmed}`` at the generation read (the lease proof,
    and the id is the one CONTROL holds at that generation); then the baseline written, and the answer says so
    (``{baseline: written}``). A crash between the CAS and the write leaves no baseline, and ``yes`` again writes
    it."""
    if req.session is None:
        raise Refused("no-session", "confirm-identity names its session")
    if req.answer not in ("yes", "no"):
        raise Refused("bad-answer", "confirm-identity wants yes|no")
    if ctx.caller != OPERATOR:
        raise Refused("verb-not-allowed-for-caller", "confirm-identity is the operator's")
    a = H.read_alloc(ctx, req.session)
    if a.kind != "login":
        raise Refused(f"confirm-kind:{a.kind}", a.session)
    with H.session_lock(ctx, a.session):
        if H.read_alloc(ctx, a.session) != a:
            raise Refused("no-session", f"{a.session}: the alloc changed")
        rec = read_record(ctx, a.session)
        if rec is None:
            raise Refused("no-session", a.session)
        if rec.state not in _RUNNING:
            raise Refused(f"not-active:{rec.state}", a.session)
        if req.answer == "no":
            return {}
        ident = rec.fields.get("identity") or {}
        oid = ident.get("observed_id") if isinstance(ident, Mapping) else None
        if oid is None:
            raise Refused(_no_id_name(ident.get("result") if isinstance(ident, Mapping) else None), a.session)
        if not _slot_names_lease(ctx, a):
            raise Refused("slot-lease-moved", f"{a.slot.tag} no longer names {a.session}")
        data = baseline_bytes(vendor=a.vendor, observed_id=oid, confirmed_at=R.timestamp(ctx.seams.clock()),
                              session=a.session)
        ctx.call("CAS", session=a.session, generation=rec.generation, **{"from": rec.state, "to": rec.state},
                 fields={"identity": {"observed_id": oid, "result": CONFIRMED}})
        ctx.store.replace(("slots", a.slot.tag), BASELINE_NAME, data, mode=BASELINE_MODE)
    return {"baseline": BASELINE_WRITTEN}


__all__ = ["BASELINE_FORMAT", "BASELINE_KEYS", "BASELINE_WRITTEN", "CONFIRMED", "IDENTITY_CALLS", "LOGIN_FAILED",
           "NOT_A_CHECK", "baseline_bytes", "baseline_vendor", "check_baseline", "confirm_identity", "gate_check",
           "identity_call", "read_slot_baseline", "record_login_identity", "EXPIRED", "UNREACHABLE"]
