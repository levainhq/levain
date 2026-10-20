"""The alloc record, the state record, the session states and their legal edges (CONTRACT §7.1-§7.3).

Two records, two writers (CONTRACT §12 D1): ``L/alloc/<session>.json`` (``levain-alloc/1``) is root's,
written by the runtime helper at ``prepare`` (identity fields) and ``render`` (policy + front fields) and
the only facts root acts on; ``L/sessions/<session>.json`` (``levain-session-state/1``) is CONTROL's,
which is its sole writer. Each is built from typed values and parsed strictly: an unknown or missing key
is a refusal by name, and so is a record whose bytes are not canonical JSON (``levain.contracts.canon``),
so one record has one spelling.

The state set, the legal-edge table and the CAS-writable field set are DATA here, so CONTROL's CAS
(``levain.slot.control``) and every test read the same table. :func:`write_durably` is the write both
records use: a temp file in the same directory, fsync, rename, fsync of the directory.
"""
from __future__ import annotations

import calendar
import errno
import os
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from levain.contracts import ContractError, canon
from levain.contracts.grammar import JSON_INT_MAX
from levain.slot import Refused, write_all
from levain.slot.grammar import SESSION_RE
from levain.slot.ids import SlotId
from levain.slot.runtime_grammar import (AUTHS, HOOKS, KINDS, MAX_ROUTES, VENDORS, VerifierStep,
                                         _MODEL_RE, _ROUTE_ID_RE, _handle, _keys, _verifier_steps)

ALLOC_FORMAT = "levain-alloc/1"
STATE_FORMAT = "levain-session-state/1"

# --- §7.3 states and edges ---------------------------------------------------------------------------

ALLOCATED, RENDERING, RENDERED, ACTIVE, BRAKED = "ALLOCATED", "RENDERING", "RENDERED", "ACTIVE", "BRAKED"
REVOKED, FROZEN, DEAD, CREDENTIAL_PARKED = "REVOKED", "FROZEN", "DEAD", "CREDENTIAL_PARKED"
RETAINED, TRASH, PURGED, HELD = "RETAINED", "TRASH", "PURGED", "HELD"

STATES = (ALLOCATED, RENDERING, RENDERED, ACTIVE, BRAKED, REVOKED, FROZEN, DEAD, CREDENTIAL_PARKED,
          RETAINED, TRASH, PURGED, HELD)

#: The five states a lease EOF (and CONTROL's start pass) moves to REVOKED.
LIVE_STATES = frozenset({ALLOCATED, RENDERING, RENDERED, ACTIVE, BRAKED})

#: The states an END step runs FROM (each step's failure lands in HELD) and the states a HELD retry
#: lands IN. DECISION: §7.3 says "HELD -> the reaper retries END from that step" without naming the step
#: -> state map, so the table allows HELD into every END state and does not condition the edge on
#: ``failed_step`` (END's step names are lane 2c's).
END_FROM_STATES = frozenset({REVOKED, FROZEN, DEAD, CREDENTIAL_PARKED})
END_INTO_STATES = frozenset({FROZEN, DEAD, CREDENTIAL_PARKED, RETAINED})

#: Every state past REVOKED that the §4.5 revoker must still see (``REVOKED-SET``, §3.3), RETAINED
#: aside: RETAINED is in the set only while the mirror shows its slot still leased to the session.
REVOKED_SET_STATES = frozenset({REVOKED, FROZEN, DEAD, CREDENTIAL_PARKED, HELD})

EDGES: frozenset[tuple[str, str]] = frozenset(
    {(ALLOCATED, RENDERING), (RENDERING, RENDERED), (RENDERED, ACTIVE), (ACTIVE, BRAKED), (BRAKED, ACTIVE)}
    | {(s, REVOKED) for s in LIVE_STATES}
    | {(REVOKED, FROZEN), (FROZEN, DEAD), (DEAD, CREDENTIAL_PARKED), (CREDENTIAL_PARKED, RETAINED)}
    | {(RETAINED, TRASH), (TRASH, PURGED), (TRASH, RETAINED)}
    | {(s, HELD) for s in END_FROM_STATES}
    | {(HELD, s) for s in END_INTO_STATES}
)

#: §7.2's CAS-writable ``fields``, exactly. ``claims`` change only through ATTACH / MINT,
#: ``artifact_acked`` only through ARTIFACT-ACK, ``lease_live`` only through the lease.
CAS_FIELDS = frozenset({"failed_step", "held_attempts", "brake_reason", "touched", "manifest",
                        "artifact_digest", "artifact_failed", "verifier_report_digest",
                        "verifier_report_refused", "identity"})

#: The ``event_kind`` stored with each generation (§3.6, §7.2).
EVENT_KINDS = ("state", "artifact", "none")

#: The fields whose CAS is an ``artifact`` generation (§3.6).
ARTIFACT_FIELDS = frozenset({"artifact_digest", "artifact_failed"})

# --- shared value rules ------------------------------------------------------------------------------

_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}", re.ASCII)                 # header: every *_digest
_TS_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z", re.ASCII)   # header
#: DECISION: a UUID in either case (Linux ``boot_id`` is lowercase, macOS ``kern.bootsessionuuid`` upper).
_BOOT_ID_RE = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}", re.ASCII)
#: §7.6: the stable account id root takes from the identity call.
OBSERVED_ID_RE = re.compile(r"[A-Za-z0-9._@:+-]{1,128}", re.ASCII)
#: DECISION: the contract names END steps ("kill", "manifest_budget") but gives no pattern.
_STEP_RE = re.compile(r"[a-z0-9_]{1,64}", re.ASCII)
#: DECISION: a refusal or reason name held in a record (``artifact_failed:"<refusal name>"``, a brake
#: reason, an identity result): the contract gives none, so a bounded token that is never a path.
_REASON_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:_.-]{0,127}", re.ASCII)
#: DECISION: ``touched`` holds §5.5 discovery names (file names, never paths); the cap is ours.
_TOUCHED_NAME_RE = re.compile(r"[^\x00-\x1f\x7f/]{1,255}")
MAX_TOUCHED = 256
_ANY_RE = re.compile(r"[^\x00-\x1f\x7f]{1,512}")


def _uint(value: object, where: str) -> int:
    if type(value) is not int:
        raise Refused("wrong-type", f"{where} is an integer")
    if not 0 <= value <= JSON_INT_MAX:
        raise Refused("out-of-range", f"{where}: {value}")
    return value


def _bool(value: object, where: str) -> bool:
    if type(value) is not bool:
        raise Refused("wrong-type", f"{where} is a boolean")
    return value


def _match(value: object, where: str, pattern: re.Pattern[str]) -> str:
    if type(value) is not str:
        raise Refused("wrong-type", f"{where} is a string")
    if not pattern.fullmatch(value):
        raise Refused("bad-value", where)
    return value


def _obj(value: object, where: str) -> dict:
    if type(value) is not dict:
        raise Refused("wrong-type", f"{where} is an object")
    return value


def _str_list(value: object, where: str, pattern: re.Pattern[str], cap: int, *,
              unique: bool = True) -> tuple[str, ...]:
    if type(value) is not list:
        raise Refused("wrong-type", f"{where} is an array")
    if len(value) > cap:
        raise Refused("too-many", f"{where}: {len(value)} > {cap}")
    out = tuple(_match(x, f"{where}[]", pattern) for x in value)
    if unique and len(set(out)) != len(out):
        raise Refused("duplicate-value", where)
    return out


def check_timestamp(value: object, where: str) -> str:
    """Header shared encodings: UTC ``YYYY-MM-DDTHH:MM:SSZ``, a real calendar time."""
    _match(value, where, _TS_RE)
    try:
        time.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise Refused("bad-value", where) from None
    return value


def timestamp(epoch: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))


def epoch_of(ts: str) -> int:
    return calendar.timegm(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))


def check_session(value: object, where: str = "session") -> str:
    if type(value) is not str:
        raise Refused("wrong-type", f"{where} is a string")
    if not SESSION_RE.fullmatch(value):
        raise Refused("bad-token", where)
    return value


def _slot(value: object, where: str) -> SlotId:
    obj = _obj(value, where)
    _keys(obj, frozenset({"class", "index"}), frozenset({"class", "index"}), where)
    if type(obj["class"]) is not str or type(obj["index"]) is not int:
        raise Refused("wrong-type", f"{where} is {{class: string, index: integer}}")
    return SlotId(obj["class"], obj["index"])


def slot_obj(slot: SlotId) -> dict[str, object]:
    return {"class": slot.slot_class, "index": slot.index}


def _enum(value: object, where: str, allowed) -> str:
    if type(value) is not str:
        raise Refused("wrong-type", f"{where} is a string")
    if value not in allowed:
        raise Refused("bad-value", where)
    return value


def _parse_canonical(data: bytes, what: str) -> dict:
    """Parse ``data`` under canon's refusals, as an object whose bytes are canonical."""
    try:
        obj = canon.parse(data)
    except ContractError as e:
        raise Refused("record-malformed", f"{what}: {e}") from None
    if type(obj) is not dict:
        raise Refused("record-malformed", f"{what} is not an object")
    if canon.canonical_bytes(obj) != data:
        raise Refused("record-not-canonical", what)
    return obj


# --- §3.3 claims ---------------------------------------------------------------------------------------

#: §3.3 ATTACH claims, exactly (``ticket`` optional).
ATTACH_CLAIM_KEYS = frozenset({"operator_uid", "supervisor_uid", "slot", "class", "kind", "vendor", "auth",
                               "lease_seq", "slot_rev", "ticket"})
ATTACH_CLAIM_REQUIRED = ATTACH_CLAIM_KEYS - {"ticket"}
#: §3.3 MINT claims, exactly (``model`` and ``front`` optional).
MINT_CLAIM_KEYS = frozenset({"routes", "model", "queue_grant", "front", "incarnation", "egress_routes"})
MINT_CLAIM_REQUIRED = MINT_CLAIM_KEYS - {"model", "front"}
_FRONT_CLAIM_KEYS = frozenset({"uid", "pid", "start"})


def parse_attach_claims(value: object) -> dict[str, object]:
    """§3.3 ``ATTACH`` claims: the keys exactly, each typed; ``slot.class`` == ``class``. Returns the
    claims as a canonical-ready dict."""
    obj = _obj(value, "claims")
    _keys(obj, ATTACH_CLAIM_KEYS, ATTACH_CLAIM_REQUIRED, "claims")
    for k in ("operator_uid", "supervisor_uid", "lease_seq", "slot_rev"):
        _uint(obj[k], f"claims.{k}")
    slot = _slot(obj["slot"], "claims.slot")
    if _enum(obj["class"], "claims.class", ("egress", "none")) != slot.slot_class:
        raise Refused("claims-mismatch", "claims.class is not claims.slot.class")
    _enum(obj["kind"], "claims.kind", KINDS)
    _enum(obj["vendor"], "claims.vendor", VENDORS)
    _enum(obj["auth"], "claims.auth", AUTHS)
    if "ticket" in obj:
        _handle(obj["ticket"], "claims.ticket")
    return dict(obj)


def parse_mint_claims(value: object) -> dict[str, object]:
    """§3.3 ``MINT`` claims: the keys exactly, each typed."""
    obj = _obj(value, "claims")
    _keys(obj, MINT_CLAIM_KEYS, MINT_CLAIM_REQUIRED, "claims")
    _str_list(obj["routes"], "claims.routes", _ROUTE_ID_RE, MAX_ROUTES)
    _str_list(obj["egress_routes"], "claims.egress_routes", _ROUTE_ID_RE, MAX_ROUTES)
    if "model" in obj:
        _match(obj["model"], "claims.model", _MODEL_RE)
    _bool(obj["queue_grant"], "claims.queue_grant")
    _uint(obj["incarnation"], "claims.incarnation")   # DECISION: the contract gives no type; a uint
    if "front" in obj:
        front = _obj(obj["front"], "claims.front")
        _keys(front, _FRONT_CLAIM_KEYS, _FRONT_CLAIM_KEYS, "claims.front")
        for k in sorted(_FRONT_CLAIM_KEYS):
            _uint(front[k], f"claims.front.{k}")
    return dict(obj)


def parse_claims(value: object) -> dict[str, object]:
    """The state record's ``claims``: ``{}`` (a record ``REVOKE`` created, §3.1: CONTROL never saw an
    ATTACH), or the ATTACH claims, or the ATTACH claims merged with the MINT claims (§3.3)."""
    obj = _obj(value, "claims")
    if not obj:
        return {}
    attach = {k: v for k, v in obj.items() if k not in MINT_CLAIM_KEYS}
    mint = {k: v for k, v in obj.items() if k in MINT_CLAIM_KEYS}
    out = parse_attach_claims(attach)
    if mint:
        out.update(parse_mint_claims(mint))
    return out


def has_mint_claims(claims: Mapping[str, object]) -> bool:
    return any(k in claims for k in MINT_CLAIM_REQUIRED)


# --- §7.1 the alloc record -----------------------------------------------------------------------------

#: The identity fields ``prepare`` writes (plus ``ticket_facts`` from ATTACH's answer), and the policy
#: fields ``render`` adds (§5.1 render step (2) and (4)).
ALLOC_IDENTITY_KEYS = frozenset({"format", "session", "slot", "lease_seq", "slot_rev", "slot_uid", "slot_gid",
                                 "child_uid", "class", "kind", "vendor", "auth", "operator_uid",
                                 "supervisor_uid", "staging_uid", "ticket", "ticket_facts", "netns",
                                 "cgroup", "created_at", "boot_id"})
ALLOC_IDENTITY_REQUIRED = ALLOC_IDENTITY_KEYS - {"child_uid", "ticket", "ticket_facts", "netns", "cgroup"}
ALLOC_POLICY_KEYS = frozenset({"routes", "egress_routes", "model", "queue_grant", "hooks", "verifier_steps",
                               "front", "incarnation"})
#: DECISION: render writes these together (step (2)), so an alloc holds all of them or none.
ALLOC_POLICY_REQUIRED = frozenset({"routes", "egress_routes", "queue_grant", "hooks", "incarnation"})
_FRONT_KEYS = frozenset({"uid", "pending", "pid", "start"})

#: §3.3 ATTACH's ``ticket_facts`` answer, written into alloc by ``prepare`` (§5.1).
TICKET_FACT_KEYS = frozenset({"job_id", "run_index", "contract_digest", "kind", "route", "routemap_generation",
                              "route_policy_digest", "model_requested", "egress_routes", "reserved_microdollars",
                              "verifier", "verifier_steps", "driver_input_digest", "input_bundle_digest"})
TICKET_FACT_REQUIRED = frozenset({"job_id", "run_index", "contract_digest", "kind", "egress_routes",
                                  "reserved_microdollars"})
TICKET_VERIFIER_KEYS = frozenset({"verifier_generation", "verifier_id", "verifier_artifact_digest",
                                  "declared_kind", "driver_template", "driver_template_digest", "steps_digest",
                                  "driver_input_digest"})
_TICKET_KINDS = ("executor", "planner", "verifier")
#: DECISION: ticket facts are dispatch's (session 3c checks their values against the ticket); here the
#: key sets are exact, digests are digests, counters are uints and every other value is a bounded string.
_FACT_STR_RE = re.compile(r"[^\x00-\x1f\x7f]{1,256}")


def _ticket_facts(value: object) -> dict[str, object]:
    obj = _obj(value, "ticket_facts")
    _keys(obj, TICKET_FACT_KEYS, TICKET_FACT_REQUIRED, "ticket_facts")
    for k, v in obj.items():
        where = f"ticket_facts.{k}"
        if k == "kind":
            _enum(v, where, _TICKET_KINDS)
        elif k.endswith("_digest"):
            _match(v, where, _DIGEST_RE)
        elif k in ("run_index", "reserved_microdollars", "routemap_generation"):
            _uint(v, where)
        elif k == "egress_routes":
            _str_list(v, where, _ROUTE_ID_RE, MAX_ROUTES)
        elif k == "route":
            _match(v, where, _ROUTE_ID_RE)
        elif k == "verifier_steps":
            _verifier_steps(v)
        elif k == "verifier":
            ver = _obj(v, where)
            _keys(ver, TICKET_VERIFIER_KEYS, TICKET_VERIFIER_KEYS, where)
            for vk, vv in ver.items():
                if vk.endswith("_digest"):
                    _match(vv, f"{where}.{vk}", _DIGEST_RE)
                elif vk == "verifier_generation":
                    _uint(vv, f"{where}.{vk}")
                else:
                    _match(vv, f"{where}.{vk}", _FACT_STR_RE)
        else:
            _match(v, where, _FACT_STR_RE)
    return dict(obj)


@dataclass(frozen=True)
class Front:
    """§7.1 ``front:{uid, pending, pid?, start?}``: render step (2) writes ``uid`` + ``pending``, step (4)
    adds the started front's ``pid`` + ``start``."""

    uid: int
    pending: bool
    pid: int | None = None
    start: int | None = None


@dataclass(frozen=True)
class AllocPolicy:
    """The fields ``render`` writes (§5.1 render (2), (4))."""

    routes: tuple[str, ...]
    egress_routes: tuple[str, ...]
    queue_grant: bool
    hooks: frozenset[str]
    incarnation: int
    model: str | None = None
    verifier_steps: tuple[VerifierStep, ...] | None = None
    front: Front | None = None


@dataclass(frozen=True)
class Alloc:
    """``L/alloc/<session>.json`` (§7.1). ``policy`` is None until ``render`` writes it."""

    session: str
    slot: SlotId
    lease_seq: int
    slot_rev: int
    slot_uid: int
    slot_gid: int
    kind: str
    vendor: str
    auth: str
    operator_uid: int
    supervisor_uid: int
    staging_uid: int
    created_at: str
    boot_id: str
    child_uid: int | None = None
    ticket: str | None = None
    ticket_facts: Mapping[str, object] | None = None
    netns: str | None = None
    cgroup: str | None = None
    policy: AllocPolicy | None = None

    def __post_init__(self) -> None:
        check_alloc(alloc_record(self, check=False))


def cgroup_path(slot: SlotId, session: str) -> str:
    """§7.3: the session's Linux cgroup. DECISION: no trailing ``/``."""
    return f"/sys/fs/cgroup/levain.slice/{slot.cgroup}/{session}"


def alloc_record(a: Alloc, *, check: bool = True) -> dict[str, object]:
    rec: dict[str, object] = {
        "format": ALLOC_FORMAT, "session": a.session, "slot": slot_obj(a.slot), "lease_seq": a.lease_seq,
        "slot_rev": a.slot_rev, "slot_uid": a.slot_uid, "slot_gid": a.slot_gid, "class": a.slot.slot_class,
        "kind": a.kind, "vendor": a.vendor, "auth": a.auth, "operator_uid": a.operator_uid,
        "supervisor_uid": a.supervisor_uid, "staging_uid": a.staging_uid, "created_at": a.created_at,
        "boot_id": a.boot_id,
    }
    for k in ("child_uid", "ticket", "netns", "cgroup"):
        if getattr(a, k) is not None:
            rec[k] = getattr(a, k)
    if a.ticket_facts is not None:
        rec["ticket_facts"] = dict(a.ticket_facts)
    p = a.policy
    if p is not None:
        rec.update(routes=list(p.routes), egress_routes=list(p.egress_routes), queue_grant=p.queue_grant,
                   hooks=sorted(p.hooks), incarnation=p.incarnation)
        if p.model is not None:
            rec["model"] = p.model
        if p.verifier_steps is not None:
            rec["verifier_steps"] = [{"argv": list(s.argv), "wall_s": s.wall_s} for s in p.verifier_steps]
        if p.front is not None:
            f: dict[str, object] = {"uid": p.front.uid, "pending": p.front.pending}
            if p.front.pid is not None:
                f["pid"] = p.front.pid
            if p.front.start is not None:
                f["start"] = p.front.start
            rec["front"] = f
    if check:
        check_alloc(rec)
    return rec


def alloc_bytes(a: Alloc) -> bytes:
    return canon.canonical_bytes(alloc_record(a))


def check_alloc(obj: object) -> Alloc:
    """Validate an alloc object (already parsed) and return it typed."""
    obj = _obj(obj, "alloc")
    _keys(obj, ALLOC_IDENTITY_KEYS | ALLOC_POLICY_KEYS, ALLOC_IDENTITY_REQUIRED, "alloc")
    if obj["format"] != ALLOC_FORMAT:
        raise Refused("bad-format", "alloc")
    session = check_session(obj["session"], "alloc.session")
    slot = _slot(obj["slot"], "alloc.slot")
    if _enum(obj["class"], "alloc.class", ("egress", "none")) != slot.slot_class:
        raise Refused("bad-value", "alloc.class is not alloc.slot.class")
    ints = {k: _uint(obj[k], f"alloc.{k}") for k in ("lease_seq", "slot_rev", "slot_uid", "slot_gid",
                                                     "operator_uid", "supervisor_uid", "staging_uid")}
    if ints["slot_gid"] != ints["slot_uid"]:            # §1.1: gid == uid for every account
        raise Refused("bad-value", "alloc.slot_gid is not alloc.slot_uid")
    kind = _enum(obj["kind"], "alloc.kind", KINDS)
    vendor = _enum(obj["vendor"], "alloc.vendor", VENDORS)
    auth = _enum(obj["auth"], "alloc.auth", AUTHS)
    child_uid = _uint(obj["child_uid"], "alloc.child_uid") if "child_uid" in obj else None
    if (child_uid is not None) != (slot.slot_class == "none"):   # §1: a verifier pair has a child
        raise Refused("bad-value", "alloc.child_uid is present exactly for the none class")
    ticket = _handle(obj["ticket"], "alloc.ticket") if "ticket" in obj else None
    facts = _ticket_facts(obj["ticket_facts"]) if "ticket_facts" in obj else None
    if facts is not None and ticket is None:
        raise Refused("bad-value", "alloc.ticket_facts without alloc.ticket")
    netns = obj.get("netns")
    if netns is not None and _match(netns, "alloc.netns", _ANY_RE) != slot.netns:
        raise Refused("bad-value", "alloc.netns is not the slot's")
    cgroup = obj.get("cgroup")
    if cgroup is not None and _match(cgroup, "alloc.cgroup", _ANY_RE) != cgroup_path(slot, session):
        raise Refused("bad-value", "alloc.cgroup is not the session's")
    created_at = check_timestamp(obj["created_at"], "alloc.created_at")
    boot_id = _match(obj["boot_id"], "alloc.boot_id", _BOOT_ID_RE)
    policy = None
    present = ALLOC_POLICY_KEYS & set(obj)
    if present:
        missing = sorted(ALLOC_POLICY_REQUIRED - present)
        if missing:
            raise Refused("missing-key", f"alloc: {missing[0]}")
        hooks = _str_list(obj["hooks"], "alloc.hooks", re.compile(r"[a-z_]{1,32}"), len(HOOKS))
        if not set(hooks) <= HOOKS:
            raise Refused("bad-value", "alloc.hooks")
        if list(hooks) != sorted(hooks):
            raise Refused("bad-value", "alloc.hooks are not sorted")
        steps = None
        if "verifier_steps" in obj:
            if kind != "verifier":                          # §5.3: verifier only
                raise Refused("bad-value", "alloc.verifier_steps outside a verifier session")
            steps = _verifier_steps(obj["verifier_steps"])
        front = None
        if "front" in obj:
            if slot.slot_class != "egress":                 # §5.1 render (5b): a none class has no front
                raise Refused("bad-value", "alloc.front for a none-class session")
            fo = _obj(obj["front"], "alloc.front")
            _keys(fo, _FRONT_KEYS, frozenset({"uid", "pending"}), "alloc.front")
            if ("pid" in fo) != ("start" in fo):
                raise Refused("bad-value", "alloc.front carries pid and start together")
            front = Front(_uint(fo["uid"], "alloc.front.uid"), _bool(fo["pending"], "alloc.front.pending"),
                          _uint(fo["pid"], "alloc.front.pid") if "pid" in fo else None,
                          _uint(fo["start"], "alloc.front.start") if "start" in fo else None)
        policy = AllocPolicy(
            routes=_str_list(obj["routes"], "alloc.routes", _ROUTE_ID_RE, MAX_ROUTES),
            egress_routes=_str_list(obj["egress_routes"], "alloc.egress_routes", _ROUTE_ID_RE, MAX_ROUTES),
            queue_grant=_bool(obj["queue_grant"], "alloc.queue_grant"),
            hooks=frozenset(hooks),
            incarnation=_uint(obj["incarnation"], "alloc.incarnation"),
            model=_match(obj["model"], "alloc.model", _MODEL_RE) if "model" in obj else None,
            verifier_steps=steps, front=front)
    return _alloc_unchecked(session=session, slot=slot, kind=kind, vendor=vendor, auth=auth,
                            created_at=created_at, boot_id=boot_id, child_uid=child_uid, ticket=ticket,
                            ticket_facts=facts, netns=netns, cgroup=cgroup, policy=policy, **ints)


def _alloc_unchecked(**kw) -> Alloc:
    a = object.__new__(Alloc)
    for name in Alloc.__dataclass_fields__:
        object.__setattr__(a, name, kw.get(name))
    return a


def parse_alloc(data: bytes) -> Alloc:
    """Strict parse of ``L/alloc/<session>.json``."""
    return check_alloc(_parse_canonical(data, "alloc"))


# --- §7.2 the state record -----------------------------------------------------------------------------

def _manifest(value: object) -> dict[str, object]:
    obj = _obj(value, "manifest")
    keys = frozenset({"digest", "entries", "bytes"})
    _keys(obj, keys, keys, "manifest")
    _match(obj["digest"], "manifest.digest", _DIGEST_RE)
    _uint(obj["entries"], "manifest.entries")
    _uint(obj["bytes"], "manifest.bytes")
    return dict(obj)


def _identity(value: object) -> dict[str, object]:
    obj = _obj(value, "identity")
    _keys(obj, frozenset({"observed_id", "result"}), frozenset(), "identity")
    if "observed_id" in obj:
        _match(obj["observed_id"], "identity.observed_id", OBSERVED_ID_RE)
    if "result" in obj:
        _match(obj["result"], "identity.result", _REASON_RE)
    return dict(obj)


#: One validator per optional state-record field; the CAS-writable ones are exactly :data:`CAS_FIELDS`.
FIELD_CHECKS: Mapping[str, Callable[[object], object]] = {
    "failed_step": lambda v: _match(v, "failed_step", _STEP_RE),
    "held_attempts": lambda v: _uint(v, "held_attempts"),
    "brake_reason": lambda v: _match(v, "brake_reason", _REASON_RE),
    "touched": lambda v: list(_str_list(v, "touched", _TOUCHED_NAME_RE, MAX_TOUCHED)),
    "manifest": _manifest,
    "artifact_digest": lambda v: _match(v, "artifact_digest", _DIGEST_RE),
    "artifact_failed": lambda v: _match(v, "artifact_failed", _REASON_RE),
    "verifier_report_digest": lambda v: _match(v, "verifier_report_digest", _DIGEST_RE),
    "verifier_report_refused": lambda v: _match(v, "verifier_report_refused", _REASON_RE),
    "identity": _identity,
    "artifact_acked": lambda v: _bool(v, "artifact_acked"),
}
assert CAS_FIELDS <= set(FIELD_CHECKS)

#: The optional fields (``touched`` is required: ``touched:[names]``, §7.2).
STATE_OPTIONAL = frozenset(FIELD_CHECKS) - {"touched"}
STATE_KEYS = frozenset({"format", "session", "generation", "state", "claims", "lease_live", "touched",
                        "event_kind", "updated_at"}) | STATE_OPTIONAL
STATE_REQUIRED = STATE_KEYS - STATE_OPTIONAL

#: DECISION: §7.2 says the verifier pair is "exactly one of the two"; the artifact pair reads the same
#: way (§7.7 3b: "CAS artifact_digest (or artifact_failed ...)"), so a record never holds both of a pair.
EXCLUSIVE_PAIRS = (("artifact_digest", "artifact_failed"), ("verifier_report_digest", "verifier_report_refused"))


@dataclass(frozen=True)
class SessionState:
    """``L/sessions/<session>.json`` (§7.2). ``fields`` holds the optional fields that are set
    (every key in :data:`STATE_OPTIONAL`), already validated."""

    session: str
    generation: int
    state: str
    claims: Mapping[str, object]
    lease_live: bool
    event_kind: str
    updated_at: str
    touched: tuple[str, ...] = ()
    fields: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        check_state(state_record(self, check=False))


def state_record(s: SessionState, *, check: bool = True) -> dict[str, object]:
    rec: dict[str, object] = {
        "format": STATE_FORMAT, "session": s.session, "generation": s.generation, "state": s.state,
        "claims": dict(s.claims), "lease_live": s.lease_live, "touched": list(s.touched),
        "event_kind": s.event_kind, "updated_at": s.updated_at,
    }
    rec.update(s.fields)
    if check:
        check_state(rec)
    return rec


def state_bytes(s: SessionState) -> bytes:
    return canon.canonical_bytes(state_record(s))


def check_exclusive(fields: Mapping[str, object]) -> None:
    for a, b in EXCLUSIVE_PAIRS:
        if a in fields and b in fields:
            raise Refused("bad-value", f"{a} and {b} together")


def check_state(obj: object) -> SessionState:
    obj = _obj(obj, "state record")
    _keys(obj, STATE_KEYS, STATE_REQUIRED, "state")
    if obj["format"] != STATE_FORMAT:
        raise Refused("bad-format", "state")
    fields = {k: FIELD_CHECKS[k](obj[k]) for k in sorted(STATE_OPTIONAL & set(obj))}
    check_exclusive(fields)
    s = object.__new__(SessionState)
    vals = dict(
        session=check_session(obj["session"], "state.session"),
        generation=_uint(obj["generation"], "state.generation"),
        state=_enum(obj["state"], "state.state", STATES),
        claims=parse_claims(obj["claims"]),
        lease_live=_bool(obj["lease_live"], "state.lease_live"),
        event_kind=_enum(obj["event_kind"], "state.event_kind", EVENT_KINDS),
        updated_at=check_timestamp(obj["updated_at"], "state.updated_at"),
        touched=tuple(FIELD_CHECKS["touched"](obj["touched"])),
        fields=fields)
    for k, v in vals.items():
        object.__setattr__(s, k, v)
    return s


def parse_state(data: bytes) -> SessionState:
    """Strict parse of ``L/sessions/<session>.json``."""
    return check_state(_parse_canonical(data, "state"))


# --- the durable write -----------------------------------------------------------------------------------

def write_durably(path: Path, data: bytes, *, mode: int = 0o600,
                  fsync: Callable[[int], None] = os.fsync) -> None:
    """Replace ``path`` with ``data`` so a reader sees the old record or the whole new one, and the new one
    survives a crash once this returns: a fresh temp file in the same directory (``O_EXCL``, never
    following a link), written whole, fsynced, renamed over ``path``, the directory fsynced. The caller
    holds whatever lock makes it the only writer of ``path`` (CONTROL: the session lock; root: the slot
    lock). A failure raises ``OSError`` with ``path`` untouched and the temp file removed.

    ``fsync`` is the seam a test uses to fail the write at the durability step."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    try:
        os.unlink(tmp)          # a temp file a dead writer left
    except FileNotFoundError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        os.fchmod(fd, mode)
        write_all(fd, data)
        fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    try:
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        fsync(dfd)
    finally:
        os.close(dfd)


def read_record(path: Path, *, max_bytes: int = 1 << 20) -> bytes | None:
    """A record's bytes, ``None`` when absent. A link or a non-regular file is refused (``bad-root``);
    DECISION: a record past ``max_bytes`` is refused rather than read whole."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.EMLINK):
            raise Refused("bad-root", f"{path} is a link") from None
        raise
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise Refused("bad-root", f"{path} is not a regular file")
        if st.st_size > max_bytes:
            raise Refused("record-too-large", str(path))
        return fh.read(max_bytes + 1)


__all__ = [
    "ACTIVE", "ALLOCATED", "ALLOC_FORMAT", "ARTIFACT_FIELDS", "ATTACH_CLAIM_KEYS", "Alloc", "AllocPolicy",
    "BRAKED", "CAS_FIELDS", "CREDENTIAL_PARKED", "DEAD", "EDGES", "END_FROM_STATES", "END_INTO_STATES",
    "EVENT_KINDS", "EXCLUSIVE_PAIRS", "FIELD_CHECKS", "FROZEN", "Front", "HELD", "LIVE_STATES",
    "MINT_CLAIM_KEYS", "OBSERVED_ID_RE", "PURGED", "RENDERED", "RENDERING", "RETAINED", "REVOKED",
    "REVOKED_SET_STATES", "STATES", "STATE_FORMAT", "SessionState", "TRASH", "alloc_bytes", "alloc_record",
    "cgroup_path", "check_alloc", "check_exclusive", "check_session", "check_state", "check_timestamp",
    "epoch_of", "has_mint_claims", "parse_alloc", "parse_attach_claims", "parse_claims", "parse_mint_claims",
    "parse_state", "read_record", "slot_obj", "state_bytes", "state_record", "timestamp", "write_durably",
]
