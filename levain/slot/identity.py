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

The baseline file is root's, 0600, replaced atomically (temp, fsync, rename, directory fsync). :func:`baseline_vendor`
reads it, and also the earlier vendor-only shape with no ``format`` key.
"""
from __future__ import annotations

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

def _codex_call(ctx: Optional[H.Ctx], a: Optional[R.Alloc]) -> str:
    return NOT_A_CHECK


def _claude_call(ctx: Optional[H.Ctx], a: Optional[R.Alloc]) -> str:
    """IG-3: the child spawned as the slot (start's :func:`~levain.slot.start.identity_capture`), its line judged
    here. Only for a session: with no ``ctx`` or alloc there is nothing to run as."""
    if ctx is None or a is None:
        raise Refused("internal", "the Claude identity call runs for a session")
    from levain.slot import start as SS

    return parse_profile_answer(*SS.identity_capture(ctx, a))


IDENTITY_CALLS: Mapping[str, Callable[[Optional[H.Ctx], Optional[R.Alloc]], str]] = {
    "codex": _codex_call, "claude": _claude_call}


def identity_call(ctx: Optional[H.Ctx], vendor: str, a: Optional[R.Alloc] = None) -> str:
    """The stable account id the vendor's identity call observed, checked against §7.6's pattern (else
    ``identity-unparsable``), or :class:`Refused` by the call's own name."""
    call = IDENTITY_CALLS.get(vendor)
    if call is None:
        raise Refused("identity-call-unbuilt", vendor[:32])
    oid = call(ctx, a)
    if type(oid) is not str or not R.OBSERVED_ID_RE.fullmatch(oid):
        raise Refused("identity-unparsable", vendor)
    return oid


#: §7.6: root reads at most this much of the identity call's stdout.
PROFILE_ANSWER_MAX = 64 * 1024


def parse_profile_answer(exit_code: int, out: bytes) -> str:
    """IG-3: root's judgement of :mod:`levain.slot.identity_child`'s line. The observed id is the profile's
    ``account.uuid`` when the child exited 0, its line is within :data:`PROFILE_ANSWER_MAX`, the server answered 200
    and the body is JSON holding a uuid that fits ``OBSERVED_ID_RE``; anything else is ``identity-unparsable``."""
    import json

    def bad(why: str) -> Refused:
        return Refused("identity-unparsable", why)

    if exit_code != 0:
        raise bad(f"identity call exit {exit_code}")
    if len(out) > PROFILE_ANSWER_MAX:
        raise bad("answer over 64 KiB")
    try:
        ans = json.loads(out.decode("utf-8"))
    except (ValueError, RecursionError):      # UnicodeDecodeError is a ValueError
        raise bad("answer is not JSON") from None
    if type(ans) is not dict or type(ans.get("status")) is not int:
        raise bad("no status")
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
           "NOT_A_CHECK", "baseline_bytes", "baseline_vendor", "check_baseline", "confirm_identity", "identity_call",
           "record_login_identity"]
