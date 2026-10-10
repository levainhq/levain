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

Provenance: no row carries a label before K2b's fire journal exists, so every ``tier_fn`` receives
``"unverified"`` (§9 K2a). The standing policy that may raise a tier is a T3 store that does not
exist yet; :data:`POLICY` is the identity policy and its revision is hashed into every etag.

The credential is verified by the HTTP layer before this module runs and handed in as a dict;
``dispatch`` refuses a ``none`` class itself as well, so a caller that forgets the check fails
closed."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Callable, Literal

from levain.cockpit.engine import VALUE_ABSENT
from levain.cockpit.external import ID_PREFIX as EXTERNAL_PREFIX
from levain.cockpit.registry import parse_date
from levain.cockpit.results import Absent, Fault, Read
from levain.writes import (ActionVerb, EditError, _normalize_expect_disposition, apply_action, apply_edit,
                           context_lock)

if TYPE_CHECKING:
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
                        "job": False, "reversible": False, "fields": [], "effect":
                        "answers a harness's pending tool call", "gesture": "sign", "route": "broker"},
}

# Keys every route may carry beside the params (the envelope), per route.
_ENVELOPE = frozenset({"verb", "panel_id", "row_id", "row_version", "panel_version", "params",
                       "intent", "dry_run", "confirm", "idempotency_key"})
_INTENTS = ("reported", "sign")


def tier_max(*tiers: Tier) -> Tier:
    return max(tiers, key=TIERS.index) if tiers else "C1"


@dataclass(frozen=True)
class Policy:
    """The standing policy (a T3 act, §4.4): it may raise any verb's tier and never lower one.
    ``raise_to`` maps a verb to a minimum tier. ``revision`` is hashed into every etag, so a policy
    change that alters a gesture with rows unchanged still changes the panel's validator."""

    revision: int = 1
    raise_to: dict[str, Tier] = field(default_factory=dict)

    def apply(self, verb: str, tier: Tier) -> Tier:
        floor = self.raise_to.get(verb)
        return tier_max(tier, floor) if floor else tier


POLICY = Policy()


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

    def tier(self, params: dict[str, Any], row: dict[str, Any] | None, provenance: str, today: date) -> Tier:
        base: Tier = "T2" if self.tier_fn is None else self.tier_fn(params, row, provenance, today)
        return POLICY.apply(self.name, base)

    def rendered_tier(self, row: dict[str, Any] | None, provenance: str, today: date) -> Tier:
        if self.tier_fn is None:
            base: Tier = "T2"
        elif self.row_tier is not None:
            base = self.row_tier(row, provenance, today)
        else:
            base = self.floor
        return POLICY.apply(self.name, base)

    def head(self, gesture: str) -> dict[str, Any]:
        return {"label": self.label, "tier": POLICY.apply(self.name, self.floor if self.tier_fn else "T2"),
                "confirm": self.confirm_required, "idempotent": self.idempotent, "job": self.job,
                "reversible": self.reversible, "fields": list(self.fields or ()), "effect": self.effect,
                "gesture": gesture, "route": self.route}


def gesture_for(tier: Tier, credential_class: str, install_class: str) -> str:
    """§5.1, §5.2: the kernel decides the gesture; a surface may downgrade one, never upgrade it."""
    if credential_class not in ("token", "device"):
        return "refused"
    if tier == "C1":
        return "direct"
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
    out: list[dict[str, Any]] = [{"param": "text", "op": "present", "tier": "T2"}]
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
        # The CAS key comes from the row as read from the source, never from the client (§4.4:
        # "the cockpit always sends the key").
        req = {"kind": kind, **params, "spore_id": _spore_id(target),
               "expect_disposition": _facet(target.get("row"), "disposition")}
        if confirm:
            req["confirm"] = True
        return apply_edit(scope, req)
    return fire


# the surface a credential names, recorded as the line's source (never a caller-chosen label)
_SURFACE_SOURCE = {"browser": "web", "flowconnect": "app"}


def _operator_state_fire(scope: "WriteScope", params: dict[str, Any], target: dict[str, Any],
                         confirm: bool) -> dict[str, Any]:
    """The version compare and the write in one step under the context file's lock (§4.4). A writer
    that does not take this lock (flow's own state CLI) is outside it: routed to M1."""
    with context_lock(scope):
        check = target.get("check")
        if check is not None:
            check()
        return apply_edit(scope, {"kind": "operator_state", "text": params.get("text"),
                                  "source": target.get("source", "web")})


def _unbuilt_fire(why: str) -> Callable[..., dict[str, Any]]:
    """A verb whose C1 write path is not built in K2a: every one of these is T2 or T3 (or the
    handler's store has no write for the field), so it is refused before reaching here; this is the
    backstop should a policy ever let one through."""
    def fire(*_a: Any) -> dict[str, Any]:
        raise EditError("not_built", 501, why)
    return fire


_SPORE_ROW = lambda r: str(r.get("id", "")).startswith("spore:")   # noqa: E731

KERNEL_VERBS: tuple[VerbSpec, ...] = (
    VerbSpec("spore_touch", "touch", "marks the item seen now (moves its seen-recency)", "row",
             floor="C1", tier_fn=_const("C1"), fire=_spore_fire("spore_touch"), legacy_kind="spore_touch",
             applies_to=_SPORE_ROW),
    VerbSpec("spore_descend", "resolve", "resolves the item (it leaves the open set; recoverable in anneal)",
             "row", fields=("spore_kind",), floor="C1", tier_fn=_const("C1"), confirm_required=True,
             reversible=False, fire=_spore_fire("spore_descend"), legacy_kind="spore_descend",
             applies_to=_SPORE_ROW),
    VerbSpec("spore_surface_at", "snooze", "sets the date the item next surfaces", "row",
             fields=("surface_at",), floor="C1", tier_fn=_surface_at_tier,
             row_tier=_const("C1"),
             escalates=lambda row, prov, today: _date_escalates(row, prov, today, "surface_at"),
             fire=_spore_fire("spore_surface_at"), legacy_kind="spore_surface_at", applies_to=_SPORE_ROW),
    VerbSpec("spore_update", "edit", "changes the item's text, tier or type", "row",
             # design §4.2 also allowlists domain and next; levain's handler has no write for either, and a
             # field the handler drops must be refused, never reported ok
             fields=("text", "tier", "type"), floor="C1", tier_fn=_update_tier,
             row_tier=_const("C1"), escalates=_update_escalates,
             fire=_spore_fire("spore_update"), legacy_kind="spore_update", applies_to=_SPORE_ROW),
    VerbSpec("spore_set_disposition", "move", "moves the item to another list (Tray, Keep, loops)", "row",
             fields=("disposition", "surface_at"), floor="T2", tier_fn=_const("T2"),
             fire=_spore_fire("spore_set_disposition"), legacy_kind="spore_set_disposition",
             applies_to=_SPORE_ROW),
    VerbSpec("spore_ascend", "promote", "resolves the loop into what it became, with a reference", "row",
             fields=("spore_kind", "ref"), floor="T2", tier_fn=_const("T2"), confirm_required=True,
             reversible=False, fire=_spore_fire("spore_ascend"), legacy_kind="spore_ascend",
             applies_to=_SPORE_ROW),
    VerbSpec("spore_seed", "capture", "adds a new item that agents will read", "create",
             fields=("text", "type", "disposition"), floor="T2", tier_fn=_const("T2"),
             fire=_unbuilt_fire("spore_seed fires through the broker (K2b)"), legacy_kind="spore_seed"),
    VerbSpec("episode_tombstone", "tombstone", "deletes an episode (memory's graduation evidence)", "row",
             floor="T2", tier_fn=_const("T2"), confirm_required=True, reversible=False,
             fire=_unbuilt_fire("episode_tombstone fires through the broker (K2b)"),
             legacy_kind="episode_tombstone"),
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
        if spec.name in BROKER_HEADS:
            raise ValueError(f"{spec.name!r} is answered on the broker path; it is never a cockpit verb")
        if spec.name in self._specs:
            raise ValueError(f"verb {spec.name!r} is already registered")
        if getattr(spec, "route", "cockpit") != "cockpit":
            raise ValueError(f"verb {spec.name!r}: only route 'cockpit' verbs register here")
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
            name, spec.label or name, spec.label or name, "panel", fields=None, floor="T2", tier_fn=None,
            confirm_required=spec.confirm_required, idempotent=spec.idempotent, job=spec.job,
            reversible=False))

    def get(self, name: str) -> VerbSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def action_verb(self, name: str) -> ActionVerb | None:
        return self._downstream.get(name)

    def is_downstream(self, name: str) -> bool:
        return name in self._downstream_names


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


def row_actions(registry: VerbRegistry, cockpit: "Cockpit", panel_id: str, row: dict[str, Any],
                credential_class: str, install_class: str, today: date) -> list[dict[str, Any]]:
    out = []
    for v in offered(registry, cockpit, panel_id):
        if v.binding != "row" or (v.applies_to is not None and not v.applies_to(row)):
            continue
        t = v.rendered_tier(row, UNVERIFIED, today)
        out.append({"verb": v.name, "tier": t, "gesture": gesture_for(t, credential_class, install_class),
                    "escalates": v.escalates(row, UNVERIFIED, today) if v.escalates else [],
                    "target": {"panel_id": panel_id, "row_id": row["id"], "row_version": row["version"]}})
    return out


def panel_actions(registry: VerbRegistry, cockpit: "Cockpit", panel_id: str,
                  credential_class: str, install_class: str, today: date,
                  value_version: str | None = None) -> list[dict[str, Any]]:
    """A value write's target names the version of the value it was rendered with (§4.4), so a
    renderer sends back what it showed; with no readable value there is none to name."""
    out = []
    for v in offered(registry, cockpit, panel_id):
        if v.binding == "row":
            continue
        t = v.rendered_tier(None, UNVERIFIED, today)
        target: dict[str, Any] = {"panel_id": panel_id}
        if v.binding == "value":
            target["panel_version"] = value_version
        out.append({"verb": v.name, "tier": t, "gesture": gesture_for(t, credential_class, install_class),
                    "escalates": [], "target": target})
    return out


def verbs_map(registry: VerbRegistry, cockpit: "Cockpit", credential_class: str,
              install_class: str) -> dict[str, dict[str, Any]]:
    """The manifest's ``verbs`` map (§4.1): only verbs some panel offers, by the same rule a write
    checks (``offered``). A broker head never comes from a panel's declared verbs: it is the
    broker's to publish (K2b), so a feed naming one adds nothing here."""
    out: dict[str, dict[str, Any]] = {}
    for pid in cockpit.panel_ids:
        for v in offered(registry, cockpit, pid):
            if v.name not in out:
                tier = v.head("")["tier"]
                out[v.name] = v.head(gesture_for(tier, credential_class, install_class))
    return out


# --- dispatch -----------------------------------------------------------------------------------


def _refuse(code: str, status: int, message: str) -> EditError:
    return EditError(code, status, message)


def _read_target(cockpit: "Cockpit", verb: VerbSpec, panel_id: str, row_id: str | None) -> dict[str, Any] | None:
    if verb.binding != "row":
        return None
    if not isinstance(row_id, str) or not row_id:
        raise _refuse("bad_request", 400, f"{verb.name!r} names a row: 'row_id' is required")
    res = cockpit.read_one(panel_id, row_id)
    if isinstance(res, Absent):
        raise _refuse("stale", 409, f"row {row_id!r} is gone")
    if isinstance(res, Fault):
        raise _refuse("source_unavailable", 503, f"could not read {row_id!r} from its source: {res.message}")
    assert isinstance(res, Read)
    return res.value


def _check_value_version(cockpit: "Cockpit", panel_id: str, supplied: Any) -> None:
    if not isinstance(supplied, str) or not supplied:
        raise _refuse("bad_request", 400, "'panel_version' is required for a write to a stored value")
    res = cockpit.read_value_version(panel_id)
    if isinstance(res, Fault):
        raise _refuse("source_unavailable", 503, f"could not read {panel_id!r} from its source: {res.message}")
    # a value that is not stored yet binds to VALUE_ABSENT, so the first write has something to name
    current = VALUE_ABSENT if isinstance(res, Absent) else res.value
    if current != supplied:
        raise _refuse("stale", 409, f"panel {panel_id!r} changed since it was rendered")


def dispatch(
    *, registry: VerbRegistry, cockpit: "Cockpit", scope: "WriteScope | None", req: Any,
    credential: dict[str, Any], install_class: str, route: Literal["cockpit", "edit", "action"],
    job_runtime: "JobRuntime | None" = None,
) -> dict[str, Any]:
    if credential.get("class") not in ("token", "device"):
        raise _refuse("credential_required", 403, "a write needs a credential (the write token)")
    if not isinstance(req, dict):
        raise _refuse("bad_request", 400, "request must be a JSON object")
    if scope is None:
        raise _refuse("read_only", 422, "this source is read-only; nothing editable")
    today = cockpit.today()

    if route == "edit":
        return _legacy_edit(registry, cockpit, scope, req, credential, install_class, today)

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
        bad = sorted(set(params) - set(verb.fields)) if verb.fields is not None else []
        if bad:
            raise _refuse("field_not_allowed", 400, f"{verb.name!r} does not take {bad}")
        tier = verb.tier(params, None, UNVERIFIED, today)
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
    panel_id = req.get("panel_id")
    if not isinstance(panel_id, str) or verb not in offered(registry, cockpit, panel_id, fresh=True):
        raise _refuse("not_offered", 404, f"panel {panel_id!r} does not offer {verb.name!r}")
    params = req.get("params", {})
    if not isinstance(params, dict):
        raise _refuse("bad_request", 400, "'params' must be a JSON object")
    bad = sorted(set(params) - set(verb.fields)) if verb.fields is not None else []
    if bad:
        raise _refuse("field_not_allowed", 400, f"{verb.name!r} does not take {bad}")
    if "dry_run" in req and not isinstance(req["dry_run"], bool):
        raise _refuse("bad_request", 400, "'dry_run' must be true or false")
    intent = req.get("intent")
    if intent is not None and intent not in _INTENTS:
        raise _refuse("bad_request", 400, f"'intent' must be one of {list(_INTENTS)}")

    row = _read_target(cockpit, verb, panel_id, req.get("row_id"))
    if row is not None:
        if verb.applies_to is not None and not verb.applies_to(row):
            raise _refuse("stale", 409, f"panel {panel_id!r} no longer offers {verb.name!r} on this row")
        if req.get("row_version") != row["version"]:
            raise _refuse("stale", 409, f"row {row['id']!r} changed since it was rendered")
    elif verb.binding == "value":
        _check_value_version(cockpit, panel_id, req.get("panel_version"))
    if "idempotency_key" in req and not verb.idempotent:
        raise _refuse("bad_request", 400, f"{verb.name!r} is not idempotent; it takes no idempotency_key")

    tier = verb.tier(params, row, UNVERIFIED, today)
    floor = verb.rendered_tier(row, UNVERIFIED, today)
    gesture = gesture_for(tier, credential["class"], install_class)
    if req.get("dry_run") is True:
        return {"ok": True, "dry_run": True, "verb": verb.name, "tier": tier, "rendered_tier": floor,
                "gesture": gesture, "route": verb.route}
    if TIERS.index(tier) > TIERS.index(floor) and intent is None:
        raise _refuse("needs_intent", 409, f"these params raise {verb.name!r} from {floor} to {tier}; "
                      "send 'intent' to proceed")
    _refuse_unless_c1(verb, tier, credential, install_class)
    _require_confirm(verb, req)
    if registry.is_downstream(verb.name):
        return _fire_downstream(registry, verb, scope, params, req, job_runtime, panel_id)
    if verb.fire is None:
        raise _refuse("not_built", 501, f"{verb.name!r} has no handler")
    target: dict[str, Any] = {"panel_id": panel_id, "row_id": req.get("row_id"), "row": row,
                              "source": _SURFACE_SOURCE.get(str(credential.get("name")), "web")}
    if verb.binding == "value":
        supplied = req.get("panel_version")
        target["check"] = lambda: _check_value_version(cockpit, panel_id, supplied)
    return verb.fire(scope, params, target, req.get("confirm") is True)


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
                        confirm_required=verb.confirm_required, idempotent=verb.idempotent, job=verb.job,
                        label=verb.label)
    body: dict[str, Any] = {"verb": verb.name, "params": params, "confirm": req.get("confirm") is True}
    if "idempotency_key" in req:
        body["idempotency_key"] = req["idempotency_key"]
    # a cockpit fire is bound to its panel, so the panel is part of what an idempotency key names
    return apply_action(scope, {verb.name: av}, body, job_runtime=job_runtime,
                        bind=None if panel_id is None else {"panel_id": panel_id})


def _refuse_unless_c1(verb: VerbSpec, tier: Tier, credential: dict[str, Any], install_class: str) -> None:
    """§4.3: T2 and T3 never fire from the POST handler. Until K2b's broker exists, refuse them."""
    if tier == "C1":
        return
    gesture = gesture_for(tier, credential["class"], install_class)
    raise _refuse("needs_broker", 403,
                  f"{verb.name!r} is {tier} here (gesture {gesture}); it fires only through the broker, "
                  "which this build does not have")


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
        res = cockpit.read_one(pid, rid)
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
                 credential: dict[str, Any], install_class: str, today: date) -> dict[str, Any]:
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
    if verb.tier_fn is None or _is_const(verb):
        # a verb whose tier does not depend on the row: refuse a T2/T3 before reading anything
        _refuse_unless_c1(verb, verb.tier(params, None, UNVERIFIED, today), credential, install_class)
    panel_id, row = (None, None)
    if verb.binding == "row":
        panel_id, row = _legacy_target(cockpit, verb, req)
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
    tier = verb.tier(params, row, UNVERIFIED, today)
    floor = verb.rendered_tier(row, UNVERIFIED, today)
    # A legacy body carries no intent: a reported install sends "reported" for a write that escalates
    # nothing and refuses an escalating one; a governed one sends "sign" (§9 Order).
    if TIERS.index(tier) > TIERS.index(floor):
        raise _refuse("needs_intent", 409,
                      f"these params raise {verb.name!r} to {tier}; a legacy /edit body carries no intent")
    _refuse_unless_c1(verb, tier, credential, install_class)
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
    class and the policy revision are hashed into every etag through ``revision``."""

    def __init__(self, registry: VerbRegistry, cockpit: "Cockpit", install_class: str) -> None:
        self.registry = registry
        self.cockpit = cockpit
        self.install_class = install_class

    def revision(self) -> list[Any]:
        return [self.install_class, POLICY.revision, sorted(self.registry.names())]

    def row_actions(self, panel_id: str, row: dict[str, Any], cred: str, today: date) -> list[dict[str, Any]]:
        return row_actions(self.registry, self.cockpit, panel_id, row, cred, self.install_class, today)

    def panel_actions(self, panel_id: str, cred: str, today: date,
                      value_version: str | None = None) -> list[dict[str, Any]]:
        return panel_actions(self.registry, self.cockpit, panel_id, cred, self.install_class, today, value_version)

    def verbs_map(self, cred: str) -> dict[str, dict[str, Any]]:
        return verbs_map(self.registry, self.cockpit, cred, self.install_class)
