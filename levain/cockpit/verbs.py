"""levain.cockpit.verbs: one verb registry, one route, the tier ladder (design: flow
``projects/levain/reference/cockpit_manifest_DESIGN_1009.md`` §4.1, §4.2, §4.4, §5.1, §5.2, §9 K2a).

Every cockpit write is a ``VerbSpec``. ``POST /cockpit/verb``, ``POST /edit`` and ``POST /action`` all
end in :func:`dispatch`, so the legacy routes are aliases of the registry, never a bypass (§4.3). A
legacy ``/edit`` body is translated onto the steps below (its row found by id on the same panels) and
fires through the same ``VerbSpec.fire``; ``/action`` serves only a downstream ``ActionVerb``, which
has no tier function and is therefore T2. The steps of ``/cockpit/verb``:

1. the verb is registered and routed ``cockpit`` (a ``broker`` head is refused here, §4.1);
2. the panel named in the request offers it (else 404, so a retired panel takes its verb with it);
   a discovered panel's offer is re-read from its source for the write (``Cockpit.offer_spec``);
3. every param is in the verb's allowlist (§4.2: a field outside it is refused, never ignored);
4. the target is read from the SOURCE through the provider's ``read_one`` (or the panel value is read
   fresh) and its version compared with the one the caller rendered (409 on a mismatch, §4.4);
5. the tier is computed by the kernel from the verb, its params and the stored target, never taken
   from the caller; a ``dry_run`` returns it here and fires nothing (so it skips 6-8 and confirm);
6. params that raise the tier above the row's rendered tier without an ``intent`` are refused
   "needs intent" (§4.1);
7. a T2 or T3 never fires from this handler: until K2b's broker exists they are refused
   "needs the broker" (§4.3);
8. the verb's ``confirm`` is checked, and only a C1 reaches its handler.

Provenance: every ``tier_fn`` receives the row's label (``row["provenance"]["label"]``, ``"unverified"``
when null) from ONE authority snapshot per outer request (K2b-3b §0.1): the render's for the actions it
shows, a fresh one taken by ``dispatch`` and by ``retier`` for the tier that governs. With no labeller
attached every label is ``"unverified"``, as in K2a. The standing policy that may raise a tier comes
from that same snapshot (``RequestAuthority.policy``: the replay of the signed policy links, trust-tiering
DESIGN §4.1); there is no module-level policy. The snapshot's head (``RequestAuthority.revision``) is
hashed into every etag, so a signed raise moves every panel's validator and the manifest's.

The credential is verified by the HTTP layer before this module runs and handed in as a dict;
``dispatch`` refuses a ``none`` class itself as well, so a caller that forgets the check fails
closed."""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Literal

from levain.cockpit.authority import bound, current
from levain.cockpit.engine import DECISIONS_ID, NOTE_VERB, QUESTION_VERB, VALUE_ABSENT
from levain.cockpit.external import ID_PREFIX as EXTERNAL_PREFIX
from levain.cockpit.registry import NOTE_ROW_PREFIX, QUESTION_ROW_PREFIX, parse_date
from levain.cockpit.results import Absent, Fault, Read
from levain.writes import (ActionVerb, EditError, _apply_operator_state_edit, _normalize_expect_disposition,
                           apply_action, apply_edit)

if TYPE_CHECKING:
    from levain.cockpit.authority import RequestAuthority, TierPolicy
    from levain.cockpit.broker import Broker
    from levain.cockpit.engine import Cockpit
    from levain.jobs import JobRuntime
    from levain.writes import WriteScope

Tier = Literal["C1", "T2", "T3"]
TIERS: tuple[Tier, ...] = ("C1", "T2", "T3")
UNVERIFIED = "unverified"

# The verbs that are never cockpit verbs: a harness pending is answered on the broker's
# remote-control path (§4.6). Registering one as a cockpit verb is refused.
BROKER_HEADS: dict[str, dict[str, Any]] = {
    "harness_approve": {"label": "approve", "tier": "T2", "confirm": True, "idempotent": True,
                        "job": False, "reversible": False, "fields": [], "sign": False, "effect":
                        "answers a harness's pending tool call", "gesture": "sign", "route": "broker"},
}

# Keys every route may carry beside the params (the envelope), per route.
_ENVELOPE = frozenset({"verb", "panel_id", "row_id", "row_version", "panel_version", "params",
                       "intent", "dry_run", "confirm", "idempotency_key", "tier_seen", "gesture_seen"})
_INTENTS = ("reported", "sign")
# The one param a signed verb (VerbSpec.sign, DESIGN_SIGN D5) takes beside its fields: the id of the sign request the
# operator's passkey signed for these params. The kernel admits it only when the server says a compose route filed that
# request for this verb and exactly these params (the durable binding beside the Signer's requests, which outlives a
# restart) and it is signed, and the send's idempotency key is SIGN_KEY_PREFIX + the draft id that compose was filed under
# (_require_sign_request, ``signed_for``); the verb's own handler or effect still verifies the signature over the params,
# so the write token alone never sends a signed verb.
SIGN_PARAM = "sign_request"
SIGN_KEY_PREFIX = "sign-"
GESTURES = ("direct", "direct-reported", "sign", "refused", "laptop-only")
# ``signed_for(verb, request_id, params)``: the draft id (``client_txn``) a compose bound ``request_id`` to, durably, for
# ``verb`` and exactly ``params`` (the params without SIGN_PARAM), when the Signer holds that request signed; else None.
# It may raise ``EditError`` (503) when that binding cannot be read: a refusal would drop a signed draft that may have sent
SignedFor = Callable[[str, str, dict[str, Any]], "str | None"]


def _param_names(verb: "VerbSpec") -> "set[str]":
    """The params a verb takes: its fields, and on a signed verb the sign request it names (SIGN_PARAM)."""
    return set(verb.fields or ()) | ({SIGN_PARAM} if verb.sign else set())


def _at_most_once(verb: "VerbSpec") -> bool:
    """A verb whose fire is deduped by its idempotency key: one declared ``idempotent``, and every signed verb, whatever
    its tier (a signed send is retried with its draft's key after an answer that never came, so it must apply once)."""
    return verb.idempotent or verb.sign


def _require_sign_request(verb: "VerbSpec", params: dict[str, Any], signed_for: "SignedFor | None",
                          key: Any) -> None:
    """A signed verb fails closed in the kernel: unless ``signed_for`` says its SIGN_PARAM names a request filed for this
    verb and exactly these params and signed, and the request's idempotency ``key`` is SIGN_KEY_PREFIX + the draft id it
    was filed under, it never reaches its handler or effect (a dry run included). So one signed request is sent under one
    key, which the at-most-once gate dedupes: a re-post under a fresh key, or none, is refused. This check runs before
    that gate, so it reads the durable binding, never one server's memory: the retry of a send that landed passes it
    after a restart and reaches the gate's replay. A caller with no ``signed_for`` (no Signer to ask) sends no signed
    verb at all. The door still verifies the signature."""
    from levain.sign_request_id import request_id_ok

    if not verb.sign:
        return
    rid = params.get(SIGN_PARAM)
    rest = {k: v for k, v in params.items() if k != SIGN_PARAM}
    txn = signed_for(verb.name, rid, rest) if request_id_ok(rid) and signed_for is not None else None
    if txn is None or key != SIGN_KEY_PREFIX + txn:
        raise _refuse("sign_required", 403, f"{verb.name!r} is sent only after the operator's passkey signs it: "
                      f"{SIGN_PARAM!r} must name the sign request filed for these params and signed, and "
                      f"'idempotency_key' must be its draft's")


def tier_max(*tiers: Tier) -> Tier:
    return max(tiers, key=TIERS.index) if tiers else "C1"


def _raised(policy: "TierPolicy", verb: str, base: Tier) -> Tier:
    """``base`` under ``policy``: raised to its floor for ``verb``, and never lowered whatever the
    policy answers."""
    got = policy.apply(verb, base)
    if got not in TIERS:
        raise ValueError(f"the standing policy answered {got!r} for {verb!r}")
    return tier_max(base, got)  # type: ignore[arg-type]


@dataclass(frozen=True)
class VerbSpec:
    """A cockpit verb (§4.1): ``ActionVerb``'s fields plus the tier function, the field allowlist,
    reversibility, the one-sentence effect, and how the write binds to what was rendered.

    - ``binding``: ``row`` (names ``row_id`` + ``row_version``), ``value`` (names ``panel_version``),
      ``create`` (names only the panel) or ``panel`` (a downstream verb on an external panel).
    - ``tier_fn(params, row, provenance, today)``: the kernel's tier for these params against the
      row as stored (``row`` is the finished row dict ``read_one`` returned, or None). None means
      the verb registered no tier function: it is T2 (§4.4).
    - ``row_tier(row, provenance, today)``: the tier a row action renders before any escalating
      param (the floor the "needs intent" rule compares against); defaults to ``floor``.
    - ``escalates(row, provenance, today)``: typed predicates a renderer evaluates on its compose
      fields to show the tier a param will raise to (``RowAction.escalates``).
    - ``device_ok``: whether a ``device`` credential (a phone passkey, K8) may fire it; always signed.
    - ``fire(scope, params, target, confirm)``: the C1 write. ``target`` carries the row read at the
      source (``row``), the surface's ``source`` label, and for a value write a ``check`` to re-run
      under the source's lock. Kernel verbs build their legacy request here, supplying the CAS key
      from the stored row, so a client never chooses it."""

    name: str
    label: str
    effect: str
    binding: Literal["row", "value", "create", "panel"]
    fields: tuple[str, ...] | None = ()      # None: a wrapped downstream ActionVerb declared none (its handler validates)
    floor: Tier = "T2"
    tier_fn: Callable[[dict[str, Any], dict[str, Any] | None, str, date], Tier] | None = None
    row_tier: Callable[[dict[str, Any] | None, str, date], Tier] | None = None
    escalates: Callable[[dict[str, Any] | None, str, date], list[dict[str, Any]]] | None = None
    confirm_required: bool = False
    idempotent: bool = False
    job: bool = False
    reversible: bool = True
    route: Literal["cockpit"] = "cockpit"
    fire: Callable[..., dict[str, Any]] | None = None
    legacy_kind: str | None = None           # the ``/edit`` kind this verb aliases
    applies_to: Callable[[dict[str, Any]], bool] | None = None   # rows a row verb offers itself on
    options: Callable[[dict[str, Any]], dict[str, list[Any]]] | None = None   # a row action's allowed values per param
    # K8: a ``device`` credential (a phone passkey) may fire this verb, always as a signed broker pending. Off for
    # every verb unless named here: ``dispatch`` refuses a device credential on any other (a kernel allowlist, never
    # the phone's UI)
    device_ok: bool = False
    # DESIGN_SIGN D5: the page sends this verb only once the operator's passkey signed its params (a sign request the
    # server's compose_signer filed), adding the one param SIGN_PARAM; the verb's own handler or effect verifies it. A
    # signed verb is at-most-once whatever ``idempotent`` says (_at_most_once)
    sign: bool = False

    # ``policy`` is the request's standing policy (``RequestAuthority.policy``, trust-tiering DESIGN
    # §3.4, §4.1): it may raise a tier, never lower one

    def tier(self, params: dict[str, Any], row: dict[str, Any] | None, provenance: str, today: date, *,
             policy: "TierPolicy") -> Tier:
        base: Tier = "T2" if self.tier_fn is None else self.tier_fn(params, row, provenance, today)
        return _raised(policy, self.name, base)

    def rendered_tier(self, row: dict[str, Any] | None, provenance: str, today: date, *,
                      policy: "TierPolicy") -> Tier:
        if self.tier_fn is None:
            base: Tier = "T2"
        elif self.row_tier is not None:
            base = self.row_tier(row, provenance, today)
        else:
            base = self.floor
        return _raised(policy, self.name, base)

    def head(self, gesture: str, *, policy: "TierPolicy") -> dict[str, Any]:
        return {"label": self.label, "tier": _raised(policy, self.name, self.floor if self.tier_fn else "T2"),
                "confirm": self.confirm_required, "idempotent": self.idempotent, "job": self.job,
                "reversible": self.reversible, "fields": list(self.fields or ()), "sign": self.sign,
                "effect": self.effect, "gesture": gesture, "route": self.route}


def gesture_for(tier: Tier, credential_class: str, install_class: str) -> str:
    """§5.1, §5.2: the kernel decides the gesture; a surface may downgrade one, never upgrade it."""
    if credential_class not in ("token", "device"):
        return "refused"
    if tier == "C1":
        # a device credential is a fingerprint the client names: its C1 is a pending the passkey signs, applied
        # once the signature verifies, never a write on the bearer token alone (K8, B5 condition 1)
        return "sign" if credential_class == "device" else "direct"
    if tier == "T2":
        if credential_class == "token" and install_class == "reported":
            return "direct-reported"
        return "sign"
    return "refused" if credential_class == "device" else "laptop-only"


# --- the kernel's tier functions (§4.2) ---------------------------------------------------------


def _facet(row: dict[str, Any] | None, name: str) -> Any:
    return ((row or {}).get("facets") or {}).get(name)


def _held_date(row: dict[str, Any] | None, today: date) -> date | None:
    """The row's stored surface date when it is in the future (the row is held out of view)."""
    d = parse_date(_facet(row, "due"))
    return d if d is not None and d > today else None


def _date_tier(new: Any, row: dict[str, Any] | None, provenance: str, today: date) -> Tier:
    """A snooze is C1 on any row, except on an unverified row held to a future date: moving that
    date earlier (to any date) or clearing it shows the row sooner to readers who were not seeing
    it, so it is T2 (§4.2, RULED Q4 bounded below)."""
    held = _held_date(row, today)
    if provenance == "verified" or held is None:
        return "C1"
    if new is None:
        return "T2"
    d = parse_date(new)
    if d is None or d < held:
        return "T2"     # an unparseable date is refused by the handler; it never lowers a tier
    return "C1"


def _date_escalates(row: dict[str, Any] | None, provenance: str, today: date, param: str) -> list[dict[str, Any]]:
    held = _held_date(row, today)
    if provenance == "verified" or held is None:
        return []
    return [{"param": param, "op": "before", "value": held.isoformat(), "tier": "T2"},
            {"param": param, "op": "clear", "tier": "T2"}]


def _surface_at_tier(params: dict[str, Any], row: dict[str, Any] | None, prov: str, today: date) -> Tier:
    return _date_tier(params.get("surface_at"), row, prov, today)


def _update_tier(params: dict[str, Any], row: dict[str, Any] | None, prov: str, today: date) -> Tier:
    """A ``spore_update`` carrying fields of several tiers takes the highest (§4.2)."""
    tiers: list[Tier] = []
    if "text" in params or "domain" in params:
        tiers.append("T2")
    if "type" in params:
        tiers.append("C1" if prov == "verified" else "T2")
    if "tier" in params:
        old, new = _facet(row, "tier"), params.get("tier")
        moves_parked = new != old and "parked" in (old, new)
        tiers.append("T2" if moves_parked and prov != "verified" else "C1")
    if "next" in params:
        tiers.append(_date_tier(params.get("next"), row, prov, today))
    return tier_max(*tiers)


def _update_escalates(row: dict[str, Any] | None, prov: str, today: date) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"param": "text", "op": "present", "tier": "T2"},
                                 {"param": "domain", "op": "present", "tier": "T2"}]
    if prov != "verified":
        out.append({"param": "type", "op": "present", "tier": "T2"})
        if _facet(row, "tier") == "parked":
            out.append({"param": "tier", "op": "not_eq", "value": "parked", "tier": "T2"})
        else:
            out.append({"param": "tier", "op": "eq", "value": "parked", "tier": "T2"})
    return out


def _const(t: Tier) -> Callable[..., Tier]:
    def tier(*_a: Any) -> Tier:
        return t
    tier._const = True  # type: ignore[attr-defined]
    return tier


# The tier of an ``undo`` is the tier of the edit it reverses (§4.1). Only these kinds keep a backup
# the plane can restore (``writes._apply_undo``); anything else is T3, the fail-closed answer.
_UNDO_TIER: dict[str, Tier] = {"config": "T3", "entity_name": "T3", "state": "T2"}


def _undo_row_tier(row: dict[str, Any] | None, prov: str, today: date) -> Tier:
    return _UNDO_TIER.get(str(_facet(row, "edit_kind") or ""), "T3")


def _undo_tier(params: dict[str, Any], row: dict[str, Any] | None, prov: str, today: date) -> Tier:
    return _undo_row_tier(row, prov, today)


# --- firing a kernel verb through its legacy write -----------------------------------------------


def _spore_id(target: dict[str, Any]) -> str:
    row_id = str(target["row_id"])
    if not row_id.startswith("spore:"):
        raise EditError("bad_target", 400, f"{row_id!r} is not a spore row")
    return row_id[len("spore:"):]


def _spore_fire(kind: str) -> Callable[..., dict[str, Any]]:
    def fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any], confirm: bool) -> dict[str, Any]:
        # The CAS keys come from the row as read from the source, never from the client (§4.4:
        # "the cockpit always sends the key"): its disposition, and its version, which anneal
        # recomputes inside its write transaction, so the row the tier was computed on is the row
        # the write lands on (a write between read_one and here is refused 409).
        row = target["row"]
        req = {"kind": kind, **params, "spore_id": _spore_id(target),
               "expect_disposition": _facet(row, "disposition")}
        if confirm:
            req["confirm"] = True
        return apply_edit(scope, req, expected_version=row["version"])
    return fire


# the surface a credential names, recorded as the line's source (never a caller-chosen label)
_SURFACE_SOURCE = {"browser": "web", "flowconnect": "app", "phone": "phone"}


def _operator_state_fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any],
                         confirm: bool) -> dict[str, Any]:
    """The version compare and the write in one step under the state file's lock (§4.4,
    ``dashboard.write_state``). A writer outside levain (the embedder's own state CLI) does not take that
    lock: routed to M1."""
    return _apply_operator_state_edit(scope, {"kind": "operator_state", "text": params.get("text"),
                                              "source": target.get("source", "web")}, None,
                                      check=target.get("check"))


def _unbuilt_fire(why: str) -> Callable[..., dict[str, Any]]:
    """A verb whose C1 write path is not built in K2a: every one of these is T2 or T3 (or the
    handler's store has no write for the field), so it is refused before reaching here; this is the
    backstop should a policy ever let one through."""
    def fire(*_a: Any) -> dict[str, Any]:
        raise EditError("not_built", 501, why)
    return fire


_SPORE_ROW = lambda r: str(r.get("id", "")).startswith("spore:")   # noqa: E731
# an OPEN question row only: an answered one is a 409 (the row it was rendered on is no longer offered the verb)
_OPEN_QUESTION_ROW = lambda r: (str(r.get("id", "")).startswith("question:")   # noqa: E731
                                and _facet(r, "question_state") == "open")


# the kernel's agent-text rows on the composed decisions panel: (row-id prefix, the facet only the kernel writes, the
# one verb the row offers). Each pair binds BOTH ways: the row offers only its verb, and the verb fires only on it
_KERNEL_ROW_VERBS = ((QUESTION_ROW_PREFIX, "question_n", QUESTION_VERB), (NOTE_ROW_PREFIX, "note_n", NOTE_VERB))


def _applies_to_row(verb: "VerbSpec", panel_id: str, row: dict[str, Any]) -> bool:
    """Whether ``verb`` is offered on / fires against ``row`` of panel ``panel_id``. A ``question:`` or ``note:`` row
    on the composed decisions panel (:data:`DECISIONS_ID`) is the kernel's: only its own verb (``question_answer``,
    ``note_dismiss``) is ever offered or dispatched on it, never a verb the attached decision source declared without
    an ``applies_to`` (that source's verbs bind to its own rows; the engine skips a source row carrying a kernel
    prefix). The kernel's verb, in turn, fires only on a decisions row of its prefix that carries its kernel facet.
    Any other panel's row named ``note:…`` or ``question:…`` is that panel's own and keeps its own verbs."""
    rid = str(row.get("id", ""))
    for prefix, facet, owner in _KERNEL_ROW_VERBS:
        kernel_row = panel_id == DECISIONS_ID and rid.startswith(prefix)
        if kernel_row and verb.name != owner:
            return False
        if verb.name == owner and not (kernel_row and _facet(row, facet) is not None):
            return False
    return verb.applies_to is None or bool(verb.applies_to(row))


# an OPEN note row only: a dismissed one is a 409
_OPEN_NOTE_ROW = lambda r: (str(r.get("id", "")).startswith("note:")   # noqa: E731
                            and _facet(r, "note_state") == "open")


def _note_fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any], confirm: bool) -> dict[str, Any]:
    """Dismiss one note: written once, channel ``cockpit``. It tells the agent NOTHING: no delivery, no thread, no
    hook, no resume (``levain.consent.notes`` has no path that could), so it spends no agent's tokens; and it
    authorises nothing, so it is C1 and needs no broker."""
    from levain.cockpit.consent import ConsentError
    from levain.consent.notes import NoteStoreError

    store = target.get("notes")
    if store is None:
        raise EditError("not_built", 501, "this cockpit has no note store")
    if params:
        raise EditError("bad_request", 400, "a dismiss takes no parameters")
    n = _facet(target["row"], "note_n")
    try:
        note = store.get(n)
        if note is None or note.dismissed is not None:   # the version check ran on the row; this is the race behind it
            raise EditError("stale", 409, f"note #{n} was dismissed or is gone")
        store.dismiss(note.id, channel="cockpit")
    except ConsentError as exc:
        raise EditError("stale" if "already dismissed" in str(exc) else "bad_request",
                        409 if "already dismissed" in str(exc) else 400, str(exc)) from None
    except (NoteStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503, f"the note store cannot dismiss now: {exc}") from None
    return {"dismissed": n}


# a coordinator review queue row (levain.cockpit.coordinator_panels): clear on an open item that is not "yours to clear",
# reopen on a cleared one
_REVIEW_PREFIX = "review:"
_CLEARABLE_REVIEW_ROW = lambda r: (str(r.get("id", "")).startswith(_REVIEW_PREFIX)   # noqa: E731
                                   and _facet(r, "review_state") == "open" and _facet(r, "review_authority") is False)
_REOPENABLE_REVIEW_ROW = lambda r: (str(r.get("id", "")).startswith(_REVIEW_PREFIX)   # noqa: E731
                                    and _facet(r, "review_state") == "cleared")
# an open "yours to clear" item: cleared only on the operator's passkey signature (review_clear_signed)
_SIGNABLE_REVIEW_ROW = lambda r: (str(r.get("id", "")).startswith(_REVIEW_PREFIX)   # noqa: E731
                                  and _facet(r, "review_state") == "open" and _facet(r, "review_authority") is True)
DEVICE_REQUIRED = ("a review item is cleared or reopened from a paired device: this page carries the shared surface "
                   "token, which does not name who holds it (pair this browser: 'Pair a device' on the Paired devices "
                   "card, then open its link here)")
# a dispatch's ``paired``: the transport's own answer to "is the device this request came from still paired?", True or
# False, re-asked at the write; it raises EditError (503) when the paired-device store cannot be read now
Paired = Callable[[], bool]


def _broker_paired(credential: dict[str, Any], broker: "Broker | None") -> "Paired | None":
    """The cockpit's ``paired`` for a credential the server resolved to a paired device (``paired-<id>``): that device
    looked up in the broker's journal, the paired-device store, each time it is asked. None for any other credential, or
    with no broker (no paired device can exist without one)."""
    from levain.cockpit.firejournal import PAIRED_PREFIX

    name = str(credential.get("name") or "")
    if credential.get("class") != "token" or not name.startswith(PAIRED_PREFIX) or broker is None:
        return None
    device = name[len(PAIRED_PREFIX):]

    def live() -> bool:
        db = broker.active_db()
        if db is None:
            raise EditError("source_unavailable", 503, "the paired-device store cannot be read now (the broker is not "
                            "active); try again")
        return db.device_live(device)
    return live


def _review_fire(op: str) -> Callable[..., dict[str, Any]]:
    """Clear or reopen one review item, mode ``manual``, as the operator (head ruling 1025+25, c4b RULINGS_OBS item 1),
    for the cockpit and the phone alike (each is a transport into :func:`dispatch`). Only a PAIRED device writes: the
    transport names it in ``paired`` (the shared surface token has none, since any holder of it could clear as you).
    Inside the store's write, on the item as it is then, the device is asked again (a revoke that landed since the
    request was authenticated refuses it) and the item's version is compared with the one the row was drawn at (a
    clear and reopen behind the actor's back refuses it). The store refuses a "yours to clear" item on this path
    (``plain_only``): its clear is ruled signed-only. It authorises nothing and tells no agent anything, so it is C1."""
    def fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any], confirm: bool) -> dict[str, Any]:
        from levain.cockpit.coordinator_panels import review_version
        from levain.cockpit.providers import _dispatch_dir
        from levain.coordinator import review
        from levain.coordinator.store import CoordError, CoordStore, StoreAbsent

        if params:
            raise EditError("bad_request", 400, "a review clear or reopen takes no parameters")
        paired = target.get("paired")
        if paired is None:
            raise EditError("device_required", 403, DEVICE_REQUIRED)
        row = target["row"]
        rid = str(row["id"])
        if not rid.startswith(_REVIEW_PREFIX):
            raise EditError("bad_request", 400, f"{rid[:60]!r} is not a review queue row")
        directory = target.get("dispatch_dir") or _dispatch_dir(target.get("consent_reader"))
        if directory is None:
            raise EditError("not_built", 501, "this cockpit names no coordinator store")
        drawn = row["version"]

        def still(item: Any) -> None:
            if not paired():
                raise EditError("device_revoked", 403, "this device is no longer paired, so it clears nothing")
            if review_version(item) != drawn:
                raise EditError("stale", 409, f"row {rid!r} changed since it was rendered")

        act = review.clear_item if op == "clear" else review.reopen_item
        try:
            item = act(CoordStore(Path(directory)), rid[len(_REVIEW_PREFIX):], mode="manual", plain_only=True,
                       still=still)
        except (review.CoordNotFound, StoreAbsent) as exc:
            raise EditError("not_found", 404, str(exc)) from None
        except CoordError as exc:
            raise EditError("refused", 409, str(exc)) from None
        except (OSError, sqlite3.Error) as exc:
            raise EditError("source_unavailable", 503, f"the coordinator store cannot be written now: {exc}") from None
        # never a top-level "state": the page reads that key as a write's outcome (dashboard_boot.js resultWords)
        return {"item": item.item_id, "review_state": item.state}
    return fire


def _refuse_raised_review(verb: VerbSpec, tier: Tier, row: "dict[str, Any] | None") -> None:
    """A review verb a signed standing policy raised above C1 (head ruling 1025+32, fix r1 item 3): refused here, on
    every surface, with the paths that do clear it named. Above C1 a verb goes to the broker, and no broker effect
    applies a review write, so it would only ever be refused there, in words that name no way through."""
    from levain.cockpit.coordinator_panels import REVIEW_VERBS

    if verb.name not in REVIEW_VERBS:
        return
    item = str((row or {}).get("id", ""))[len(_REVIEW_PREFIX):] or "<item>"
    cmd = "reopen" if verb.name == "review_reopen" else "clear"
    signed = ("" if verb.name != "review_clear" else
              "the passkey-signed clear ('clear with passkey', offered on an item that is yours to clear) or ")
    # the laptop's clear of an item that is yours to clear is a manual one: say so where it stands in for the signed
    unsigned = " (recorded as a manual clear, unsigned)" if verb.name == "review_clear_signed" else ""
    raise _refuse("review_raised", 403,
                  f"{verb.name!r} is {tier} here (a signed standing policy raised it), and a review item is cleared "
                  f"or reopened from the cockpit only at C1, never as a pending: use {signed}the laptop's own "
                  f"command, levain coord review {cmd} {item}{unsigned}")


def refuse_raised_review(name: str, item_id: str, policy: "TierPolicy") -> None:
    """The kernel's raised-review refusal (:func:`_refuse_raised_review`, on the tier :meth:`VerbSpec.tier` gives under
    ``policy``) for a review door outside the kernel: the phone's signed clear, which files its request and clears at
    the store's door (``levain.phone_server``). :class:`levain.writes.EditError` ``review_raised`` (403) when ``policy``
    raised kernel verb ``name`` above C1 for the item ``item_id``."""
    verb = next(v for v in KERNEL_VERBS if v.name == name)
    row = {"id": _REVIEW_PREFIX + item_id}
    tier = verb.tier({}, row, "", datetime.now().astimezone().date(), policy=policy)
    if tier != "C1":
        _refuse_raised_review(verb, tier, row)


def _review_signed_fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any],
                        confirm: bool) -> dict[str, Any]:
    """Clear one "yours to clear" review item on the operator's passkey signature (c4-mut R3, R4): the kernel already
    checked that ``sign_request`` names the request a compose filed for exactly ``{item}`` and that it is signed
    (:func:`_require_sign_request`); the door (:func:`levain.coordinator.review.clear_signed`) verifies the signature
    itself against the Signer's enrolled passkeys and the item as it is now. Any credential that reached the kernel may
    send it: the passkey signature names who cleared, which the shared surface token cannot (head ruling obs 2)."""
    from levain.cockpit.providers import _dispatch_dir
    from levain.coordinator import review
    from levain.coordinator.store import CoordError, CoordStore, StoreAbsent

    item_id = str(target["row"]["id"])[len(_REVIEW_PREFIX):]
    if set(params) != {"item", SIGN_PARAM} or params["item"] != item_id:
        raise EditError("bad_request", 400, f"a signed clear names its item ({item_id!r}) and its sign request")
    signer = target.get("signer")
    directory = target.get("dispatch_dir") or _dispatch_dir(target.get("consent_reader"))
    if signer is None or directory is None:
        raise EditError("not_built", 501, "this cockpit runs no passkey ceremony or names no coordinator store")
    try:
        item = review.clear_signed(CoordStore(Path(directory)), item_id, queue_dir=signer.queue_dir,
                                   request_id=params[SIGN_PARAM], keys=signer.keys)
    except review.SignedClearRefused as exc:
        raise EditError(exc.error, exc.status, str(exc)) from None
    except (review.CoordNotFound, StoreAbsent) as exc:
        raise EditError("not_found", 404, str(exc)) from None
    except CoordError as exc:
        raise EditError("refused", 409, str(exc)) from None
    except (OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503, f"the coordinator store cannot be written now: {exc}") from None
    return {"item": item.item_id, "review_state": item.state, "mode": item.last[1] if item.last else None}


def _review_compose(params: dict[str, Any], cockpit: "Cockpit", signer: Any) -> str:
    """Levain's own composer for ``review_clear_signed`` (c4-mut R1): file the sign request over the item as it is now
    (:func:`levain.coordinator.review.file_clear_request`) and name it. ``ValueError`` (the compose route's 400) with
    the words the operator reads when there is nothing to sign."""
    from levain.cockpit.providers import _dispatch_dir
    from levain.consent.sign import SignRefused
    from levain.coordinator import review
    from levain.coordinator.store import CoordError, CoordStore

    item = params.get("item")
    if set(params) != {"item"} or not isinstance(item, str):
        raise ValueError("a signed clear names one review item, as 'item'")
    directory = _dispatch_dir(getattr(cockpit, "consent_reader", None))
    if directory is None:
        raise ValueError("this cockpit names no coordinator store")
    try:
        return review.file_clear_request(CoordStore(Path(directory)), item, queue_dir=signer.queue_dir)
    except (CoordError, SignRefused, OSError, sqlite3.Error) as exc:
        raise ValueError(f"nothing to sign: {exc}") from None


# c4-mut R1: the KERNEL signed verbs levain files its own sign request for, ahead of the embedder's compose_signer
# (which files a request only for its own downstream verbs): verb -> (params, cockpit, signer) -> the request id
KERNEL_COMPOSERS: dict[str, Callable[[dict[str, Any], "Cockpit", Any], str]] = {
    "review_clear_signed": _review_compose,
}


def _question_fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any], confirm: bool) -> dict[str, Any]:
    """Answer one question (Q-2b): the operator's words or option key, recorded once and unsigned, channel
    ``cockpit``, then delivered to the agent's session on a daemon thread (a resume settles in seconds, and a verb
    handler does not hold its request for it). The answer authorises nothing (ruling 26a), so this is C1 and needs
    no broker. A delivery error never undoes the answer: ``levain question deliver`` retries it."""
    import threading

    from levain.cockpit.consent import ConsentError
    from levain.consent import band
    from levain.consent.questions import QuestionStoreError

    store = target.get("questions")
    if store is None:
        raise EditError("not_built", 501, "this cockpit has no question store")
    if ("option" in params) == ("text" in params):
        raise EditError("bad_request", 400, "answer with exactly one of an option key or free text")
    n = _facet(target["row"], "question_n")
    try:
        q = store.get(n)
        if q is None or q.answer is not None:      # the version check ran on the row; this is the race behind it
            raise EditError("stale", 409, f"question #{n} was answered or is gone")
        if "option" in params and (not isinstance(params["option"], str)
                                   or params["option"] not in {k for k, _ in q.options}):
            raise EditError("bad_request", 400, f"{params['option']!r} is not an option of question #{n}")
        store.answer(q.id, option=params.get("option"), text=params.get("text"), channel="cockpit")
    except ConsentError as exc:
        raise EditError("stale" if "already answered" in str(exc) else "bad_request",
                        409 if "already answered" in str(exc) else 400, str(exc)) from None
    except (QuestionStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503, f"the question store cannot answer now: {exc}") from None

    def deliver() -> None:
        try:
            band.deliver_answer(store, q.id)
        except Exception:  # noqa: BLE001 - the answer stands; `levain question deliver` retries it
            pass

    threading.Thread(target=deliver, name="levain-answer-delivery", daemon=True).start()
    return {"answered": n, "delivery": "started"}


def _band_fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any], confirm: bool) -> dict[str, Any]:
    """Answer the NEEDS YOU band by its shorthand (Q17-ALL S5a): ``{text, seen_through, store_id}``, recorded once per
    item, unsigned, channel ``cockpit``, viewer ``cockpit:<credential>``, bound to the view that viewer was last shown
    (:func:`levain.band_view.answer_text`), then delivered to each agent's session on a daemon thread. Q1 RULED (a):
    "Information answers stay unsigned and are accepted only from human channels; agent-typed or relayed answers are
    refused and the item stays open." The cockpit write credential is the human gate the dispatcher has already
    passed; no second check is made here, and an authority item records nothing (it names its signing sheet)."""
    import threading

    from levain import band_view
    from levain.consent.questions import QuestionStoreError

    store = target.get("questions")
    if store is None:
        raise EditError("not_built", 501, "this cockpit has no question store")
    text, seen, sid = params.get("text"), params.get("seen_through"), params.get("store_id")
    if (set(params) != {"text", "seen_through", "store_id"} or not isinstance(text, str) or not isinstance(sid, str)
            or not isinstance(seen, int) or isinstance(seen, bool)):
        raise EditError("bad_request", 400, "answer with exactly {text, seen_through, store_id}")
    try:
        done = band_view.answer_text(store, text, viewer=f"cockpit:{target.get('credential') or 'web'}",
                                     channel="cockpit", seen_through=seen, store_id=sid)
    except band_view.StaleBand as exc:
        raise EditError("stale_band", 409, str(exc)) from None
    except ValueError as exc:
        raise EditError("answer_refused", 422, str(exc)) from None
    except (QuestionStoreError, OSError, sqlite3.Error) as exc:
        raise EditError("source_unavailable", 503, f"the question store cannot answer now: {exc}") from None
    if not done.recorded and not done.sheets:
        raise EditError("answer_refused", 422, "; ".join(done.refused) or "nothing was recorded")
    if done.answered:
        threading.Thread(target=band_view.deliver_recorded, args=(store, done), name="levain-answer-delivery",
                         daemon=True).start()
    return {"recorded": done.recorded, "refused": done.refused, "sheets": list(done.sheets), "echo": done.echo,
            "delivery": "started" if done.answered else "none"}


def _resolve_kinds(direction: str) -> Callable[[dict[str, Any]], dict[str, list[Any]]]:
    """The ``spore_kind`` values anneal takes for this row's type, so a renderer offers only those."""
    def options(row: dict[str, Any]) -> dict[str, list[Any]]:
        from anneal_memory.spores import ASCEND_BY_TYPE, DESCEND_BY_TYPE

        by_type = ASCEND_BY_TYPE if direction == "ascend" else DESCEND_BY_TYPE
        return {"spore_kind": sorted(by_type.get(str(_facet(row, "spore_type")), ()))}
    return options

KERNEL_VERBS: tuple[VerbSpec, ...] = (
    VerbSpec("spore_touch", "touch", "marks the item seen now (moves its seen-recency)", "row",
             floor="C1", tier_fn=_const("C1"), fire=_spore_fire("spore_touch"), legacy_kind="spore_touch",
             applies_to=_SPORE_ROW),
    VerbSpec("spore_descend", "resolve", "resolves the item (it leaves the open set; recoverable in anneal)",
             "row", fields=("spore_kind",), floor="C1", tier_fn=_const("C1"), confirm_required=True,
             reversible=False, fire=_spore_fire("spore_descend"), legacy_kind="spore_descend", device_ok=True,
             applies_to=_SPORE_ROW, options=_resolve_kinds("descend")),
    VerbSpec("spore_surface_at", "snooze", "sets the date the item next surfaces", "row",
             fields=("surface_at",), floor="C1", tier_fn=_surface_at_tier,
             row_tier=_const("C1"),
             escalates=lambda row, prov, today: _date_escalates(row, prov, today, "surface_at"),
             fire=_spore_fire("spore_surface_at"), legacy_kind="spore_surface_at", device_ok=True, applies_to=_SPORE_ROW),
    VerbSpec("spore_update", "edit", "changes the item's text, domain, tier or type", "row",
             # design §4.2 also allowlists next; here a date moves only through spore_surface_at. domain
             # is always T2 (_update_tier), so it lands through the broker's effect and never reaches the
             # legacy write, which refuses it rather than drop it
             fields=("text", "domain", "tier", "type"), floor="C1", tier_fn=_update_tier,
             row_tier=_const("C1"), escalates=_update_escalates,
             fire=_spore_fire("spore_update"), legacy_kind="spore_update", device_ok=True, applies_to=_SPORE_ROW),
    VerbSpec("spore_set_disposition", "move", "moves the item to another list (Tray, Keep, loops)", "row",
             fields=("disposition", "surface_at"), floor="T2", tier_fn=_const("T2"),
             fire=_spore_fire("spore_set_disposition"), legacy_kind="spore_set_disposition", device_ok=True,
             applies_to=_SPORE_ROW),
    VerbSpec("spore_ascend", "promote", "resolves the loop into what it became, with a reference", "row",
             fields=("spore_kind", "ref"), floor="T2", tier_fn=_const("T2"), confirm_required=True,
             reversible=False, fire=_spore_fire("spore_ascend"), legacy_kind="spore_ascend", device_ok=True,
             applies_to=_SPORE_ROW, options=_resolve_kinds("ascend")),
    VerbSpec("spore_seed", "capture", "adds a new item that agents will read", "create",
             fields=("text", "type", "disposition"), floor="T2", tier_fn=_const("T2"),
             fire=_unbuilt_fire("spore_seed fires through the broker (K2b)"), legacy_kind="spore_seed",
             device_ok=True),
    VerbSpec("episode_tombstone", "tombstone", "deletes an episode (memory's graduation evidence)", "row",
             floor="T2", tier_fn=_const("T2"), confirm_required=True, reversible=False,
             fire=_unbuilt_fire("episode_tombstone fires through the broker (K2b)"),
             legacy_kind="episode_tombstone",
             # the entity's own episodes only: a federated feed's row (``feed:<name>:<id>``) is another
             # writer's, and this store cannot tombstone it
             applies_to=lambda r: str(r.get("id", "")).startswith("episode:")),
    VerbSpec("question_answer", "answer", "records your answer to an agent's question and sends it to the agent "
             "(it authorises nothing)", "row", fields=("option", "text"), floor="C1", tier_fn=_const("C1"),
             reversible=False, fire=_question_fire, applies_to=_OPEN_QUESTION_ROW),
    VerbSpec("band_answer", "answer", "records your shorthand answers to the NEEDS YOU band and sends them to the "
             "agents (it authorises nothing)", "create", fields=("text", "seen_through", "store_id"), floor="C1",
             tier_fn=_const("C1"), reversible=False, fire=_band_fire),
    VerbSpec("note_dismiss", "dismiss", "marks an agent's note read (it tells the agent nothing and authorises "
             "nothing)", "row", floor="C1", tier_fn=_const("C1"), reversible=False, fire=_note_fire,
             applies_to=_OPEN_NOTE_ROW),
    VerbSpec("review_clear", "clear", "clears a coordinator review item as you (it authorises nothing)", "row",
             floor="C1", tier_fn=_const("C1"), fire=_review_fire("clear"), applies_to=_CLEARABLE_REVIEW_ROW),
    VerbSpec("review_reopen", "reopen", "puts a cleared review item back in the queue", "row", floor="C1",
             tier_fn=_const("C1"), fire=_review_fire("reopen"), applies_to=_REOPENABLE_REVIEW_ROW),
    VerbSpec("review_clear_signed", "clear", "clears an item that is yours to clear, on your passkey's signature (it "
             "authorises nothing)", "row", fields=("item",), floor="C1", tier_fn=_const("C1"), reversible=False,
             fire=_review_signed_fire, applies_to=_SIGNABLE_REVIEW_ROW, sign=True),
    VerbSpec("operator_state", "set state", "sets your freeform state line", "value",
             fields=("text",), floor="C1", tier_fn=_const("C1"), fire=_operator_state_fire,
             legacy_kind="operator_state"),
    VerbSpec("section_edit", "edit", "rewrites the neocortex State section", "value",
             fields=("new_body",), floor="T2", tier_fn=_const("T2"),
             fire=_unbuilt_fire("section_edit fires through the broker (K2b)"), legacy_kind="state"),
    VerbSpec("config", "edit", "rewrites an operator config document", "value",
             fields=("new_body",), floor="T3", tier_fn=_const("T3"),
             fire=_unbuilt_fire("config needs a laptop-presence signature (slice S)"), legacy_kind="config"),
    VerbSpec("entity_name", "rename", "renames the entity", "value", fields=("value",), floor="T3",
             tier_fn=_const("T3"), fire=_unbuilt_fire("entity_name needs a laptop-presence signature (slice S)"),
             legacy_kind="entity_name"),
    VerbSpec("undo", "undo", "restores the content an edit replaced", "row", floor="T2", tier_fn=_undo_tier,
             row_tier=_undo_row_tier, fire=_unbuilt_fire("no C1 edit keeps a restorable backup"),
             legacy_kind="undo", applies_to=lambda r: _facet(r, "undoable") is True),
)

LEGACY_KINDS: dict[str, str] = {v.legacy_kind: v.name for v in KERNEL_VERBS if v.legacy_kind}

# K8: the kernel verbs a device credential (a phone passkey) may fire, each as a signed broker pending; the broker
# adds ``pending_cancel`` (a device cancels only through a signed pending). Not here, by B5's review: section_edit
# (the dream's single-writer surface), episode_tombstone (irreversible), undo, question_answer (the phone answers on
# its consent path), operator_state (until Phill asks), spore_touch (no broker effect), every T3 verb, and any panel
# verb registered later (``device_ok`` defaults off)
DEVICE_VERBS: frozenset[str] = frozenset(v.name for v in KERNEL_VERBS if v.device_ok)

# The legacy ``/edit`` keys that are not params: the target, the CAS keys the kernel now supplies
# itself, and the confirm flag. Everything else in a legacy body is a param and meets the allowlist.
_LEGACY_BINDING = {"expect_disposition", "expected_body", "expected", "source", "heading", "panel_version"}


class VerbRegistry:
    """The kernel verbs plus a downstream's. A downstream ``ActionVerb`` without a tier function is
    T2 (§4.4); one may arrive already as a ``VerbSpec`` carrying its own."""

    def __init__(self, downstream: dict[str, ActionVerb | VerbSpec] | None = None) -> None:
        self._specs: dict[str, VerbSpec] = {}
        self._downstream: dict[str, ActionVerb] = {}
        self._downstream_names: set[str] = set()
        for v in KERNEL_VERBS:
            self._add(v)
        for name, spec in (downstream or {}).items():
            self.register_downstream(name, spec)

    def _add(self, spec: VerbSpec) -> None:
        if not isinstance(spec.name, str) or not spec.name or ":" in spec.name:
            # ':' is reserved: a cockpit fire's idempotency fingerprint is taken under "cockpit:<verb>"
            raise ValueError(f"verb name {spec.name!r} must be a non-empty string without ':'")
        if spec.name in BROKER_HEADS:
            raise ValueError(f"{spec.name!r} is answered on the broker path; it is never a cockpit verb")
        if spec.name in self._specs:
            raise ValueError(f"verb {spec.name!r} is already registered")
        if getattr(spec, "route", "cockpit") != "cockpit":
            raise ValueError(f"verb {spec.name!r}: only route 'cockpit' verbs register here")
        if type(spec.sign) is not bool:
            raise ValueError(f"verb {spec.name!r}: sign must be True or False, got {spec.sign!r}")
        if spec.sign and spec.job:
            # a job's propose is posted by the page's job path, which no passkey ceremony precedes
            raise ValueError(f"verb {spec.name!r} cannot be both sign and job: a signed verb is sent only after its "
                             "ceremony")
        self._specs[spec.name] = spec

    def register_downstream(self, name: str, spec: ActionVerb | VerbSpec) -> None:
        if isinstance(spec, VerbSpec):
            if spec.name != name:
                raise ValueError(f"verb registered as {name!r} names itself {spec.name!r}")
            if spec.fire is None:
                raise ValueError(f"downstream verb {name!r} has no fire handler")
            if spec.binding != "panel":
                # the kernel reads and versions only its own providers' rows and values; a downstream
                # verb binds to its panel (bounded, not a per-case read path)
                raise ValueError(f"downstream verb {name!r}: binding must be 'panel', not {spec.binding!r}")
            self._add(spec)
            self._downstream_names.add(name)
            return
        if not isinstance(spec, ActionVerb):
            raise TypeError(f"extra_verbs[{name!r}] must be an ActionVerb or a VerbSpec")
        self._downstream[name] = spec
        self._downstream_names.add(name)
        self._add(VerbSpec(
            name, spec.label or name, spec.label or name, "panel", fields=spec.fields, floor="T2", tier_fn=None,
            confirm_required=spec.confirm_required, idempotent=spec.idempotent, job=spec.job,
            reversible=False, sign=spec.sign))

    def get(self, name: str) -> VerbSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def action_verb(self, name: str) -> ActionVerb | None:
        return self._downstream.get(name)

    def is_downstream(self, name: str) -> bool:
        return name in self._downstream_names


# --- the served-verbs record --------------------------------------------------------------------

# The names of the verbs the last-built server serves (kernel and downstream), with the time it was
# built: ``levain cockpit policy raise`` checks a non-kernel verb against it by exact membership, as
# kubectl checks a resource name against the server's discovery API. It sits beside cockpit.db.
SERVED_VERBS_NAME = "served_verbs.json"
_SERVED_VERBS_MAX = 1024 * 1024


def record_served_verbs(data_root: Path | str, registry: VerbRegistry, served_at: datetime) -> Path:
    """Write ``registry``'s verb names and ``served_at`` to ``<data_root>/served_verbs.json``,
    atomically (a temp file in the same directory, fsynced, then renamed over the record)."""
    root = Path(data_root)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = json.dumps({"served_at": served_at.isoformat(), "verbs": sorted(registry.names())},
                      separators=(",", ":")).encode("utf-8")
    path = root / SERVED_VERBS_NAME
    tmp = root / f".{SERVED_VERBS_NAME}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def served_verbs(data_root: Path | str) -> tuple[frozenset[str], str]:
    """The verb names and the served-at time ``record_served_verbs`` wrote under ``data_root``.
    Raises ``FileNotFoundError`` when there is no record and ``ValueError`` when it is malformed."""
    path = Path(data_root) / SERVED_VERBS_NAME
    with open(path, "rb") as f:
        raw = f.read(_SERVED_VERBS_MAX + 1)
    if len(raw) > _SERVED_VERBS_MAX:
        raise ValueError(f"{path} is larger than {_SERVED_VERBS_MAX} bytes")
    try:
        doc = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not JSON: {exc}") from exc
    verbs = doc.get("verbs") if isinstance(doc, dict) else None
    served_at = doc.get("served_at") if isinstance(doc, dict) else None
    if not isinstance(verbs, list) or not all(isinstance(v, str) for v in verbs) or not isinstance(served_at, str):
        raise ValueError(f"{path} does not hold a list of verb names and a served-at time")
    return frozenset(verbs), served_at


# --- what a panel offers ------------------------------------------------------------------------


def offered(registry: VerbRegistry, cockpit: "Cockpit", panel_id: str, *, fresh: bool = False) -> list[VerbSpec]:
    """The verbs a panel offers. ``fresh`` (a write): a discovered panel's offer is re-read from its
    source, and a source that cannot confirm it refuses the write (503), never falls back to the
    rendered offer."""
    if fresh:
        got = cockpit.offer_spec(panel_id)
        if isinstance(got, Fault):
            raise _refuse("source_unavailable", 503, f"could not confirm what {panel_id!r} offers: {got.message}")
        spec = got
    else:
        spec = cockpit.spec(panel_id)
    if spec is None:
        return []
    # a downstream panel's feed names its own verb; it may offer only a downstream verb, never a kernel
    # one (a kernel verb binds to the kernel's own rows and values)
    external = panel_id.startswith(EXTERNAL_PREFIX)
    return [v for n in spec.verbs if (v := registry.get(n)) is not None
            and (not external or registry.is_downstream(n))]


def compose_refusal(registry: VerbRegistry, panel_id: str, compose: dict[str, Any]) -> str | None:
    """Why a list's compose card cannot post through its verb, or None. The card posts the panel's own
    action (``panel_actions``): a registered verb the panel may offer by ``offered``'s rule, bound to the
    panel and not a row, not a job (the card has no job transport), and every compose field a param the
    verb accepts (``dispatch`` refuses any other with ``field_not_allowed``)."""
    name = compose.get("verb")
    if not isinstance(name, str):
        return f"verb {name!r} is not a verb name"
    verb = registry.get(name)
    if verb is None:
        return f"verb {name!r} is not a registered verb"
    if panel_id.startswith(EXTERNAL_PREFIX) and not registry.is_downstream(name):
        return f"verb {name!r} is a kernel verb; a downstream panel offers only a downstream verb"
    if verb.binding == "row":
        return f"verb {name!r} binds to a row; a compose card posts the panel's own action"
    if verb.job:
        return f"verb {name!r} is a job verb; a compose card has no job transport"
    if verb.fields is not None:
        names = {str(f.get("name")) for f in compose.get("fields") or () if isinstance(f, dict)}
        bad = sorted(names - set(verb.fields))
        if bad:
            return f"fields {bad} are not fields of verb {name!r} (it takes {list(verb.fields)})"
    return None


def row_label(row: dict[str, Any] | None) -> str:
    """The label a row was rendered (or read) with: its provenance, which the engine applied from the
    request's one snapshot; ``unverified`` when it carries none."""
    prov = (row or {}).get("provenance")
    label = prov.get("label") if isinstance(prov, dict) else None
    return label if isinstance(label, str) and label else UNVERIFIED


def row_actions(registry: VerbRegistry, cockpit: "Cockpit", panel_id: str, row: dict[str, Any],
                credential_class: str, install_class: str, today: date, *,
                policy: "TierPolicy") -> list[dict[str, Any]]:
    out = []
    label = row_label(row)
    for v in offered(registry, cockpit, panel_id):
        if v.binding != "row" or not _applies_to_row(v, panel_id, row):
            continue
        t = v.rendered_tier(row, label, today, policy=policy)
        # an escalation names the tier the fire will take, so the policy raises it as it raises the fire
        escalates = [{**e, "tier": _raised(policy, v.name, e["tier"])} for e in v.escalates(row, label, today)] \
            if v.escalates else []
        action = {"verb": v.name, "tier": t, "gesture": gesture_for(t, credential_class, install_class),
                  "escalates": escalates,
                  "target": {"panel_id": panel_id, "row_id": row["id"], "row_version": row["version"]}}
        if v.options is not None:
            action["options"] = v.options(row)
        out.append(action)
    return out


def pending_actions(row: dict[str, Any], credential_class: str, install_class: str, *,
                    policy: "TierPolicy") -> list[dict[str, Any]]:
    """A ``pend:`` row's one action (K2b-3b §3): ``pending_cancel`` at the PENDING's own tier for every
    caller, raised to ``policy``'s ``pending_cancel`` floor (the upper bound: the broker lowers it to
    that floor over C1 for the caller's own principal at the POST), answered on the broker's pending
    route, never ``/cockpit/verb``."""
    pid = str(row["id"])[len("pend:"):]
    tier = (row.get("facets") or {}).get("pending_tier")
    if tier not in TIERS:
        return []
    tier = _raised(policy, "pending_cancel", tier)
    return [{"verb": "pending_cancel", "tier": tier, "gesture": gesture_for(tier, credential_class, install_class),
             "escalates": [], "target": {"kind": "pending", "pending_id": pid},
             "route": f"/cockpit/pending/{pid}/cancel"}]


def panel_actions(registry: VerbRegistry, cockpit: "Cockpit", panel_id: str,
                  credential_class: str, install_class: str, today: date,
                  value_version: str | None = None, label: str = UNVERIFIED, *,
                  policy: "TierPolicy") -> list[dict[str, Any]]:
    """A value write's target names the version of the value it was rendered with (§4.4), so a
    renderer sends back what it showed; with no readable value there is none to name."""
    out = []
    for v in offered(registry, cockpit, panel_id):
        if v.binding == "row":
            continue
        t = v.rendered_tier(None, label, today, policy=policy)
        target: dict[str, Any] = {"panel_id": panel_id}
        if v.binding == "value":
            target["panel_version"] = value_version
        out.append({"verb": v.name, "tier": t, "gesture": gesture_for(t, credential_class, install_class),
                    "escalates": [], "target": target})
    return out


def verbs_map(registry: VerbRegistry, cockpit: "Cockpit", credential_class: str,
              install_class: str, *, policy: "TierPolicy") -> dict[str, dict[str, Any]]:
    """The manifest's ``verbs`` map (§4.1): only verbs some panel offers, by the same rule a write
    checks (``offered``). A broker head never comes from a panel's declared verbs: it is the
    broker's to publish (K2b), so a feed naming one adds nothing here."""
    out: dict[str, dict[str, Any]] = {}
    for pid in cockpit.panel_ids:
        for v in offered(registry, cockpit, pid):
            if v.name not in out:
                tier = v.head("", policy=policy)["tier"]
                out[v.name] = v.head(gesture_for(tier, credential_class, install_class), policy=policy)
    return out


# --- dispatch -----------------------------------------------------------------------------------


def _refuse(code: str, status: int, message: str) -> EditError:
    return EditError(code, status, message)


def _read_target(cockpit: "Cockpit", verb: VerbSpec, panel_id: str, row_id: str | None) -> dict[str, Any] | None:
    if verb.binding != "row":
        return None
    if not isinstance(row_id, str) or not row_id:
        raise _refuse("bad_request", 400, f"{verb.name!r} names a row: 'row_id' is required")
    res = cockpit.read_one(panel_id, row_id)     # under the request's bound authority
    if isinstance(res, Absent):
        raise _refuse("stale", 409, f"row {row_id!r} is gone")
    if isinstance(res, Fault):
        raise _refuse("source_unavailable", 503, f"could not read {row_id!r} from its source: {res.message}")
    assert isinstance(res, Read)
    return res.value


def _check_value_version(cockpit: "Cockpit", panel_id: str, supplied: Any) -> None:
    res = cockpit.read_value_version(panel_id)
    if isinstance(res, Fault):
        # first: a value no one can read (a corrupt file) has no version to name, so say that
        raise _refuse("source_unavailable", 503, f"could not read {panel_id!r} from its source: {res.message}")
    if not isinstance(supplied, str) or not supplied:
        raise _refuse("bad_request", 400, "'panel_version' is required for a write to a stored value")
    # a value that is not stored yet binds to VALUE_ABSENT, so the first write has something to name
    current = VALUE_ABSENT if isinstance(res, Absent) else res.value
    if current != supplied:
        raise _refuse("stale", 409, f"panel {panel_id!r} changed since it was rendered")


def _value_label(cockpit: "Cockpit", verb: VerbSpec, panel_id: str, authority: "RequestAuthority") -> str:
    """A stored value's label for a value write's tier, from the request's snapshot. A verb whose
    tier reads no label (a constant, or none) takes ``unverified`` without a second source read."""
    if verb.tier_fn is None or _is_const(verb):
        return authority.labels.label(None)
    res = cockpit.read_value_head(panel_id, authority=authority)
    if isinstance(res, Fault):
        raise _refuse("source_unavailable", 503, f"could not read {panel_id!r} from its source: {res.message}")
    return res.value[1] if isinstance(res, Read) else UNVERIFIED


def dispatch(
    *, registry: VerbRegistry, cockpit: "Cockpit", scope: "WriteScope | None", req: Any,
    credential: dict[str, Any], install_class: str, route: Literal["cockpit", "edit", "action"],
    job_runtime: "JobRuntime | None" = None, broker: "Broker | None" = None, signed_for: "SignedFor | None" = None,
    signer: Any = None, paired: "Paired | None" = None, dispatch_dir: "Path | None" = None,
) -> dict[str, Any]:
    """``paired``: a transport that authenticated the request as a paired device on its own store (the phone app,
    for the phone port) names the check here; the cockpit's server passes none and the kernel asks its broker's journal
    (:func:`_broker_paired`). ``dispatch_dir``: the coordinator store's directory for a cockpit with no consent reader
    to name it (the phone app's)."""
    # the tier at issue governs, from a FRESH snapshot taken once for this request and bound for its
    # source reads (K2b-3b §0.1, §8d row 6)
    auth = cockpit.authority()
    with bound(auth):
        return _dispatch(registry, cockpit, scope, req, credential, install_class, route, job_runtime, broker, auth,
                         signed_for, signer, paired, dispatch_dir)


def _dispatch(registry: VerbRegistry, cockpit: "Cockpit", scope: "WriteScope | None", req: Any,
              credential: dict[str, Any], install_class: str, route: Literal["cockpit", "edit", "action"],
              job_runtime: "JobRuntime | None", broker: "Broker | None", auth: "RequestAuthority",
              signed_for: "SignedFor | None" = None, signer: Any = None, paired: "Paired | None" = None,
              dispatch_dir: "Path | None" = None) -> dict[str, Any]:
    if credential.get("class") not in ("token", "device"):
        raise _refuse("credential_required", 403, "a write needs a credential (the write token)")
    if not isinstance(req, dict):
        raise _refuse("bad_request", 400, "request must be a JSON object")
    if scope is None:
        raise _refuse("read_only", 422, "this source is read-only; nothing editable")
    today = auth.now.astimezone().date()   # the snapshot's instant: a second clock read could cross midnight
    device = credential.get("class") == "device"
    if device and route != "cockpit":
        raise _refuse("laptop_only", 403, f"a device credential writes on /cockpit/verb only, never on /{route}")

    if route == "edit":
        return _legacy_edit(registry, cockpit, scope, req, credential, install_class, today, broker, auth)

    if route == "action":
        name = req.get("verb")
        verb = registry.get(name) if isinstance(name, str) and registry.action_verb(name) is not None else None
        if verb is None:
            # a downstream VerbSpec is a cockpit verb: it is offered by a panel and bound to what that
            # panel rendered, which a legacy /action body cannot name, so it fires on /cockpit/verb only
            raise _refuse("unknown_verb", 404, f"no such action verb: {name!r}")
        params = req.get("params", {})
        if not isinstance(params, dict):
            raise _refuse("bad_request", 400, "'params' must be a JSON object")
        bad = sorted(set(params) - _param_names(verb)) if verb.fields is not None else []
        if bad:
            raise _refuse("field_not_allowed", 400, f"{verb.name!r} does not take {bad}")
        _require_sign_request(verb, params, signed_for, req.get("idempotency_key"))
        tier = verb.tier(params, None, auth.labels.label(None), today, policy=auth.policy)    # no row, no label
        _refuse_unless_c1(verb, tier, credential, install_class)
        return _fire_downstream(registry, verb, scope, params, req, job_runtime)

    # route == "cockpit"
    unknown = sorted(set(req) - _ENVELOPE)
    if unknown:
        raise _refuse("bad_request", 400, f"unknown request keys {unknown}")
    name = req.get("verb")
    if isinstance(name, str) and name in BROKER_HEADS:
        raise _refuse("route_broker", 400, f"{name!r} is answered on the broker's remote-control path")
    verb = registry.get(name) if isinstance(name, str) else None
    if verb is None:
        raise _refuse("unknown_verb", 404, f"no such verb: {name!r}")
    if device and not verb.device_ok:
        raise _refuse("laptop_only", 403, f"{verb.name!r} is not a device verb: it is fired on the laptop")
    panel_id = req.get("panel_id")
    if not isinstance(panel_id, str) or verb not in offered(registry, cockpit, panel_id, fresh=True):
        raise _refuse("not_offered", 404, f"panel {panel_id!r} does not offer {verb.name!r}")
    params = req.get("params", {})
    if not isinstance(params, dict):
        raise _refuse("bad_request", 400, "'params' must be a JSON object")
    bad = sorted(set(params) - _param_names(verb)) if verb.fields is not None else []
    if bad:
        raise _refuse("field_not_allowed", 400, f"{verb.name!r} does not take {bad}")
    _require_sign_request(verb, params, signed_for, req.get("idempotency_key"))
    if "dry_run" in req and not isinstance(req["dry_run"], bool):
        raise _refuse("bad_request", 400, "'dry_run' must be true or false")
    intent = req.get("intent")
    if intent is not None and intent not in _INTENTS:
        raise _refuse("bad_request", 400, f"'intent' must be one of {list(_INTENTS)}")
    _check_seen(req, intent)

    asked_target: dict[str, Any] = {"kind": verb.binding, "panel_id": panel_id}
    if verb.binding == "row":
        if not isinstance(req.get("row_id"), str) or not req["row_id"]:
            raise _refuse("bad_request", 400, f"{verb.name!r} names a row: 'row_id' is required")
        asked_target["row_id"] = req["row_id"]
    # a dry run never touches the journal: a replay could resume (decide) a pending a dead request left
    replayed = None if req.get("dry_run") is True else _broker_replay(
        broker, verb, params, intent, credential, req.get("idempotency_key"), asked_target)
    if replayed is not None:
        return replayed
    row = _read_target(cockpit, verb, panel_id, req.get("row_id"))
    label = row_label(row)
    if row is not None:
        if not _applies_to_row(verb, panel_id, row):
            raise _refuse("stale", 409, f"panel {panel_id!r} no longer offers {verb.name!r} on this row")
        if req.get("row_version") != row["version"]:
            raise _refuse("stale", 409, f"row {row['id']!r} changed since it was rendered")
    elif verb.binding == "value":
        _check_value_version(cockpit, panel_id, req.get("panel_version"))
        label = _value_label(cockpit, verb, panel_id, auth)

    tier = verb.tier(params, row, label, today, policy=auth.policy)
    floor = verb.rendered_tier(row, label, today, policy=auth.policy)
    gesture = gesture_for(tier, credential["class"], install_class)
    if tier != "C1":            # its preview too: no gesture would ever apply it
        _refuse_raised_review(verb, tier, row)
    # before the key check: a fire previewed at T2 carries a key, so a T2->C1 move must answer as one
    _check_moved(req, tier, gesture)
    # a broker pending's replay is keyed by it (K2b §2), so a T2/T3 takes one on any verb, and so does a device's C1;
    # a C1 fire takes one when its verb is at-most-once (idempotent, or signed)
    if "idempotency_key" in req and not _at_most_once(verb) and tier == "C1" and not device:
        raise _refuse("bad_request", 400, f"{verb.name!r} is not idempotent; it takes no idempotency_key")
    if req.get("dry_run") is True:
        return {"ok": True, "dry_run": True, "verb": verb.name, "tier": tier, "rendered_tier": floor,
                "gesture": gesture, "route": verb.route}
    if TIERS.index(tier) > TIERS.index(floor) and intent is None:
        raise _refuse("needs_intent", 409, f"these params raise {verb.name!r} from {floor} to {tier}; "
                      "send 'intent' to proceed")
    if device and gesture == "refused":
        raise _refuse("laptop_only", 403, f"{verb.name!r} is {tier} here: a device never signs a {tier}; "
                                          "it needs the laptop")
    if tier != "C1" or device:
        # a device's C1 too: a pending its passkey signs, applied once the signature verifies (K8)
        _require_confirm(verb, req)
        return _to_broker(verb, tier, credential, install_class, broker, params=params, row=row,
                          panel_id=panel_id, intent=intent, idempotency_key=req.get("idempotency_key"),
                          panel_version=req.get("panel_version"))
    _require_confirm(verb, req)
    if registry.is_downstream(verb.name):
        return _fire_downstream(registry, verb, scope, params, req, job_runtime, panel_id)
    if verb.fire is None:
        raise _refuse("not_built", 501, f"{verb.name!r} has no handler")
    target: dict[str, Any] = {"panel_id": panel_id, "row_id": req.get("row_id"), "row": row,
                              "source": _SURFACE_SOURCE.get(str(credential.get("name")), "web"),
                              "questions": cockpit.question_store, "notes": getattr(cockpit, "note_store", None),
                              # the credential's name: the viewer a band answer binds to (``cockpit:<name>``)
                              "credential": str(credential.get("name") or ""),
                              # whether the request came from a paired device, asked of that device's store (a review
                              # verb writes only for one), and where the coordinator store is (the directory the review
                              # panel reads)
                              "paired": paired if paired is not None else _broker_paired(credential, broker),
                              "consent_reader": getattr(cockpit, "consent_reader", None),
                              "dispatch_dir": dispatch_dir,
                              # the server's Signer (levain.consent.sign): a signed review clear is verified against
                              # its passkeys; None on a server that runs no passkey ceremony
                              "signer": signer}
    if verb.binding == "value":
        supplied = req.get("panel_version")
        target["check"] = lambda: _check_value_version(cockpit, panel_id, supplied)
    return verb.fire(scope, params, target, req.get("confirm") is True)


def _check_seen(req: dict[str, Any], intent: str | None) -> None:
    """A fire carrying ``intent`` names the tier and gesture its dry run showed (``tier_seen``,
    ``gesture_seen``), so the server, not the page, refuses a fire whose preview no longer holds. This
    binds the tier and the gesture, not the params. A dry run is the preview, so it carries neither.
    A fire with no ``intent`` is the sign path and carries no preview binding: the operator signs the
    record itself, and the record names the tier."""
    named = [k for k in ("tier_seen", "gesture_seen") if k in req]
    if req.get("dry_run") is True:
        if named:
            raise _refuse("bad_request", 400, f"a dry run is the preview; it takes no {named}")
        return
    if intent is not None and len(named) < 2:
        raise _refuse("bad_request", 400, "a fire with 'intent' names its preview: 'tier_seen' and "
                                          "'gesture_seen' are required")
    if "tier_seen" in req and req["tier_seen"] not in TIERS:
        raise _refuse("bad_request", 400, f"'tier_seen' must be one of {list(TIERS)} (got {req['tier_seen']!r})")
    if "gesture_seen" in req and req["gesture_seen"] not in GESTURES:
        raise _refuse("bad_request", 400,
                      f"'gesture_seen' must be one of {list(GESTURES)} (got {req['gesture_seen']!r})")


def _check_moved(req: dict[str, Any], tier: Tier, gesture: str) -> None:
    if req.get("dry_run") is True:
        return
    if "tier_seen" in req and req["tier_seen"] != tier:
        raise EditError("tier_moved", 409, f"the tier moved from {req['tier_seen']} to {tier} since the "
                        "preview; preview again", {"tier": tier, "tier_seen": req["tier_seen"]})
    if "gesture_seen" in req and req["gesture_seen"] != gesture:
        raise EditError("gesture_moved", 409, f"the gesture moved from {req['gesture_seen']} to {gesture} "
                        "since the preview; preview again", {"gesture": gesture, "gesture_seen": req["gesture_seen"]})


def _fire_downstream(registry: VerbRegistry, verb: VerbSpec, scope: "WriteScope", params: dict[str, Any],
                     req: dict[str, Any], job_runtime: "JobRuntime | None",
                     panel_id: str | None = None) -> dict[str, Any]:
    """A downstream ``ActionVerb`` keeps ``apply_action``'s confirm, idempotency, job and audit
    envelope; a downstream ``VerbSpec`` fires its own handler."""
    av = registry.action_verb(verb.name)
    if av is None:
        # a downstream VerbSpec rides the same envelope as an ActionVerb (confirm, idempotency, job,
        # the audit receipt), so a C1 downstream fire is recorded and replay-guarded like any action
        fire, target, confirmed = verb.fire, {"panel_id": req.get("panel_id"), "row_id": None, "row": None}, \
            req.get("confirm") is True
        assert fire is not None
        av = ActionVerb(handler=lambda p: fire(scope, p, target, confirmed),
                        confirm_required=verb.confirm_required, idempotent=_at_most_once(verb), job=verb.job,
                        label=verb.label)
    elif _at_most_once(verb) and not av.idempotent:
        # a signed ActionVerb runs through the at-most-once gate whatever it declared (its key is its draft's identity)
        av = dataclasses.replace(av, idempotent=True)
    body: dict[str, Any] = {"verb": verb.name, "params": params, "confirm": req.get("confirm") is True}
    if "idempotency_key" in req:
        body["idempotency_key"] = req["idempotency_key"]
    # a cockpit fire is bound to its panel, so the panel is part of what an idempotency key names
    return apply_action(scope, {verb.name: av}, body, job_runtime=job_runtime,
                        bind=None if panel_id is None else {"panel_id": panel_id})


def _refuse_unless_c1(verb: VerbSpec, tier: Tier, credential: dict[str, Any], install_class: str,
                      broker: "Broker | None" = None) -> None:
    """§4.3: T2 and T3 never fire from the POST handler; with no write-enabled broker they are
    refused, naming why. A device's C1 is a signed pending too (K8), so it is refused the same way."""
    if tier == "C1" and credential.get("class") != "device":
        return
    gesture = gesture_for(tier, credential["class"], install_class)
    if broker is None:
        why = "which this build does not have"
    else:
        from levain.cockpit.broker import Lifecycle

        state = broker.state
        if state is Lifecycle.STANDBY:
            raise _refuse("standby", 503, f"{verb.name!r} fires only through the broker, and another "
                                          "broker holds this data root (write-disabled)")
        why = (f"which is disabled here: {broker.disabled_reason}" if state is Lifecycle.INSTALL_DISABLED
               else f"which is {state.value}")
    raise _refuse("needs_broker", 403,
                  f"{verb.name!r} is {tier} here (gesture {gesture}); it fires only through the broker, {why}")


def _broker_replay(broker: "Broker | None", verb: VerbSpec, params: dict[str, Any], intent: str | None,
                   credential: dict[str, Any], key: Any, target: dict[str, Any]) -> dict[str, Any] | None:
    """K2b §2: a replayed POST returns its recorded pending and outcome. It is looked up before the
    target's version is compared, because the fire it made moved that version."""
    if broker is None or not broker.enabled or not isinstance(key, str) or not key:
        return None
    from levain.cockpit.broker import BrokerError

    try:
        return broker.replay(verb=verb.name, params=params, intent=intent, credential=credential,
                             idempotency_key=key, target=target)
    except BrokerError as exc:
        raise EditError(exc.code, exc.status, str(exc), exc.extra) from exc


def _to_broker(verb: VerbSpec, tier: Tier, credential: dict[str, Any], install_class: str,
               broker: "Broker | None", *, params: dict[str, Any], row: dict[str, Any] | None,
               panel_id: str | None, intent: str | None, idempotency_key: Any,
               panel_version: Any = None) -> dict[str, Any]:
    """§4.3, K2b §3.1: a T2/T3 becomes a broker pending. ``intent`` absent is ``sign``; ``reported``
    fires in this request in a reported install (T2 on a token only, the broker's check)."""
    if broker is None or not broker.enabled:
        _refuse_unless_c1(verb, tier, credential, install_class, broker)
    assert broker is not None
    from levain.cockpit.broker import BrokerError

    if verb.binding == "row":
        assert row is not None
        target: dict[str, Any] = {"kind": "row", "panel_id": panel_id, "row_id": row["id"],
                                  "row_version": row["version"]}
    elif verb.binding == "create":
        target = {"kind": "create", "panel_id": panel_id}
    elif verb.binding == "value":
        # the version the operator was shown: the broker compares it with its own read of the source
        # and binds the effect to that read (SECTION_ADAPTER_DESIGN.md)
        target = {"kind": "value", "panel_id": panel_id, "panel_version": panel_version}
    else:
        target = {"kind": verb.binding, "panel_id": panel_id}
    try:
        return broker.issue(verb=verb.name, params=params, target=target, tier=tier, credential=credential,
                            intent=intent, idempotency_key=idempotency_key)
    except BrokerError as exc:
        raise EditError(exc.code, exc.status, str(exc), exc.extra) from exc


def retier(registry: "VerbRegistry", cockpit: "Cockpit", verb_name: str, params: dict[str, Any],
           target: dict[str, Any]) -> Tier | Absent | None:
    """The kernel's tier for a pending's verb against its target read fresh (K2b §3.2.1), the
    broker's ``tier_of``. ``Absent`` when the row is gone (the decision ends STALE); ``None`` when
    the read faulted (the decision leaves a signed pending unspent, and refuses a reported one).

    One snapshot per decide (K2b-3b §0.1). A snapshot that could not read the journal is ``None`` in a
    governed install, where a label can lower a tier, so a decision never settles on a guessed label;
    in a reported install no label is ever ``verified`` (``labels.label_class``), so the tier is the
    one ``unverified`` gives. A value target tiers as unlabelled (every kernel value verb's tier is a
    constant; a downstream value verb whose tier reads a label is a known edge)."""
    verb = registry.get(verb_name)
    if verb is None:
        return None
    # the request's own authority when its boundary bound one (the broker's submit), never a second read
    auth = current() or cockpit.authority()
    if auth.labels.error is not None and _governed(cockpit):
        return None
    row = None
    if target.get("kind") == "row":
        with bound(auth):
            res = cockpit.read_one(str(target.get("panel_id")), str(target.get("row_id")))
        if isinstance(res, Absent):
            return res
        if not isinstance(res, Read):
            return None
        row = res.value
    return verb.tier(dict(params), row, row_label(row), auth.now.astimezone().date(),   # one instant per decide
                     policy=auth.policy)


def _governed(cockpit: "Cockpit") -> bool:
    """Whether a label can change a tier here: any install class but ``reported`` (fail closed on an
    unknown one: the decision waits rather than settle on a label it could not read)."""
    from levain.cockpit.install import REPORTED

    return cockpit.install_class() != REPORTED


def _require_confirm(verb: VerbSpec, req: dict[str, Any]) -> None:
    if verb.confirm_required and req.get("confirm") is not True:
        raise _refuse("confirm_required", 409, f"{verb.name!r} requires confirm:true")


_ROW_KEYS = {"spore_id": ("spore:", ("tray", "loops", "keep")), "edit_id": ("edit:", ("edits",)),
             "episode_id": ("episode:", ("episodes",))}


def _legacy_target(cockpit: "Cockpit", verb: VerbSpec, req: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """The stored target of a legacy ``/edit`` body, read from the source on the same panels
    ``/cockpit/verb`` reads, so the tier and the CAS key come from one row read. A row that is not
    found is refused (409), never tiered as "no row"; a row that more than one panel or store entry
    answers for with different content is refused too."""
    key = next((k for k in _ROW_KEYS if k in req), None)
    if key is None:
        raise _refuse("bad_request", 400, f"{verb.name!r} names a row: one of {sorted(_ROW_KEYS)} is required")
    prefix, panels = _ROW_KEYS[key]
    rid = f"{prefix}{req.get(key)}"
    found: list[tuple[str, dict[str, Any]]] = []
    for pid in panels:
        if cockpit.spec(pid) is None:
            continue
        res = cockpit.read_one(pid, rid)    # the request's bound authority: one snapshot across the panels
        if isinstance(res, Read):
            found.append((pid, res.value))
        elif isinstance(res, Fault):
            raise _refuse("source_unavailable", 503, f"could not read {rid!r} from its source: {res.message}")
    if not found:
        raise _refuse("stale", 409, f"row {rid!r} is gone or not readable")
    if len({r["version"] for _p, r in found}) != 1:
        raise _refuse("stale", 409, f"row {rid!r} reads differently on {[p for p, _r in found]}")
    return found[0]


# The panel a legacy value write binds to. A value verb not named here is refused on /edit.
_LEGACY_VALUE_PANEL = {"operator_state": "state"}
# Legacy verbs that act on what the client rendered and cannot be undone: the body must name the
# disposition it rendered, so a confirm given on an old render never lands on a row that changed list.
_LEGACY_NEEDS_SNAPSHOT = frozenset({"spore_descend"})


def _legacy_edit(registry: VerbRegistry, cockpit: "Cockpit", scope: "WriteScope", req: dict[str, Any],
                 credential: dict[str, Any], install_class: str, today: date,
                 broker: "Broker | None", authority: "RequestAuthority") -> dict[str, Any]:
    """``POST /edit``: the legacy body translated onto the cockpit path. One row read, one tier, the
    kernel's CAS key and the verb's own ``fire``; ``apply_edit`` never receives the client's body.
    What the client rendered still binds: a body's ``expect_disposition`` must match the row as read
    (409), and a value write names its ``panel_version`` (§4.4: an alias is never a bypass)."""
    kind = req.get("kind")
    name = LEGACY_KINDS.get(kind) if isinstance(kind, str) else None
    if name is None:
        raise _refuse("bad_kind", 400, f"unknown edit kind {kind!r}")
    verb = registry.get(name)
    assert verb is not None
    params = {k: v for k, v in req.items()
              if k not in ("kind", "confirm") and k not in _ROW_KEYS and k not in _LEGACY_BINDING}
    bad = sorted(set(params) - set(verb.fields or ()))
    if bad:
        raise _refuse("field_not_allowed", 400, f"{verb.name!r} does not take {bad}")
    _require_sign_request(verb, params, None, None)     # /edit asks no Signer: a signed verb never fires here
    if (verb.tier_fn is None or _is_const(verb)) and (broker is None or not broker.enabled):
        # a verb whose tier does not depend on the row: refuse a T2/T3 before reading anything
        _refuse_unless_c1(verb, verb.tier(params, None, authority.labels.label(None), today, policy=authority.policy),
                          credential, install_class, broker)
    panel_id, row = (None, None)
    label = authority.labels.label(None)
    if verb.binding == "row":
        panel_id, row = _legacy_target(cockpit, verb, req)
        label = row_label(row)
        if verb.name in _LEGACY_NEEDS_SNAPSHOT and "expect_disposition" not in req:
            raise _refuse("bad_request", 400, f"{verb.name!r} on /edit names the disposition it rendered "
                          "('expect_disposition')")
        if "expect_disposition" in req and (_normalize_expect_disposition(req["expect_disposition"])
                                            != _normalize_expect_disposition(_facet(row, "disposition"))):
            raise _refuse("stale", 409, f"row {row['id']!r} moved to another list since it was rendered")
    elif verb.binding == "value":
        panel_id = _LEGACY_VALUE_PANEL.get(verb.name)
        if panel_id is None:
            raise _refuse("not_offered", 404, f"{verb.name!r} is not a /edit value write")
        _check_value_version(cockpit, panel_id, req.get("panel_version"))
        label = _value_label(cockpit, verb, panel_id, authority)
    tier = verb.tier(params, row, label, today, policy=authority.policy)
    floor = verb.rendered_tier(row, label, today, policy=authority.policy)
    # A legacy body carries no intent: a reported install sends "reported" for a write that escalates
    # nothing and refuses an escalating one; a governed one sends "sign" (§9 Order).
    if TIERS.index(tier) > TIERS.index(floor):
        raise _refuse("needs_intent", 409,
                      f"these params raise {verb.name!r} to {tier}; a legacy /edit body carries no intent")
    if tier != "C1":
        _require_confirm(verb, req)
        from levain.cockpit.install import REPORTED

        # §9 Order: a legacy body carries no intent; a reported install sends "reported", a governed
        # one "sign"
        legacy_intent = "reported" if broker is not None and broker.install_class == REPORTED else "sign"
        return _to_broker(verb, tier, credential, install_class, broker, params=params, row=row,
                          panel_id=panel_id, intent=legacy_intent, idempotency_key=None)
    _require_confirm(verb, req)
    if verb.fire is None:
        raise _refuse("not_built", 501, f"{verb.name!r} has no handler")
    target: dict[str, Any] = {"panel_id": panel_id, "row_id": row["id"] if row else None, "row": row,
                              "source": _SURFACE_SOURCE.get(str(credential.get("name")), "web")}
    if verb.binding == "value":
        supplied, vpanel = req.get("panel_version"), panel_id
        assert vpanel is not None
        target["check"] = lambda: _check_value_version(cockpit, vpanel, supplied)
    return verb.fire(scope, params, target, req.get("confirm") is True)


def _is_const(verb: VerbSpec) -> bool:
    return getattr(verb.tier_fn, "_const", False)


class VerbView:
    """What the engine asks of the registry at render time (``Cockpit.attach_verbs``). The install
    class and the request's authority head are hashed into every etag through ``revision``, and every
    tier reads the request's standing policy (``auth``: the render's one ``RequestAuthority``).
    ``install_class`` may be a callable, read at every render: a server passes its broker's live class,
    so a broker that is disabled renders no reported gesture and one that heals changes the etag."""

    def __init__(self, registry: VerbRegistry, cockpit: "Cockpit",
                 install_class: "str | Callable[[], str]") -> None:
        self.registry = registry
        self.cockpit = cockpit
        self._install_class = install_class

    @property
    def install_class(self) -> str:
        ic = self._install_class
        return ic() if callable(ic) else ic

    def revision(self, auth: "RequestAuthority") -> list[Any]:
        return [self.install_class, auth.revision, sorted(self.registry.names())]

    def row_actions(self, panel_id: str, row: dict[str, Any], cred: str, today: date, *,
                    auth: "RequestAuthority") -> list[dict[str, Any]]:
        return row_actions(self.registry, self.cockpit, panel_id, row, cred, self.install_class, today,
                           policy=auth.policy)

    def pending_actions(self, row: dict[str, Any], cred: str, *, auth: "RequestAuthority") -> list[dict[str, Any]]:
        return pending_actions(row, cred, self.install_class, policy=auth.policy)

    def panel_actions(self, panel_id: str, cred: str, today: date,
                      value_version: str | None = None, label: str = UNVERIFIED, *,
                      auth: "RequestAuthority") -> list[dict[str, Any]]:
        return panel_actions(self.registry, self.cockpit, panel_id, cred, self.install_class, today,
                             value_version, label, policy=auth.policy)

    def verbs_map(self, cred: str, *, auth: "RequestAuthority") -> dict[str, dict[str, Any]]:
        return verbs_map(self.registry, self.cockpit, cred, self.install_class, policy=auth.policy)

    def compose_refusal(self, panel_id: str, compose: dict[str, Any]) -> str | None:
        return compose_refusal(self.registry, panel_id, compose)
