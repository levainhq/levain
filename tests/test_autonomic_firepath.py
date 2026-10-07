"""Phase-2 Slice-4a — the binding FIRE-PATH (the dispatcher + the gate's fire-path changes + the two
new BindingStore verbs). This is where everything 3a-3a.5 built gets CONSUMED: ``list_active``, the
sealed posture, ``binding_invocation``, ``effective_guard``, the kill substrate, the prediction
monitor, the receipt.

Three blocks:
  1. the GATE fire-path changes (seam #1 risk-threading, seam #2 ratified-posture fail-up, the
     known-danger KILL wiring closing codex's 3a.5 finding #1, ``by=binding``);
  2. the ``BindingStore`` verbs the fire path uses (``ratify`` PAUSED→ACTIVE, ``admit`` with a one-shot's
     atomic claim);
  3. the ``FireDispatcher`` end-to-end (match/skip, single-link cut, one-shot, kill, bookkeeping).
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, field

import pytest

from tests.test_autonomic_rawstore import admit_binding_run, dump, registry_of, rig, write_raw

from levain.autonomic import (
    ActionManifest,
    ActionRequest,
    ActionRisk,
    AuthorityScope,
    Binding,
    BindingStatus,
    BindingStore,
    EfferentGate,
    ExecutionResult,
    FireDispatcher,
    GateReceiptStore,
    Guard,
    IntentProvenance,
    Posture,
    RiskClass,
    SignalAuth,
    SubGoal,
    TightnessVector,
    TriggerSpec,
    TrustContext,
    manual_invocation,
)

FIXED = _dt.datetime(2026, 6, 30, 12, 0, 0, tzinfo=_dt.timezone.utc)
LOW_INTERNAL = ActionRisk(cls=RiskClass.LOW, reversible=True, external=False, financial=False)
HIGH_EXTERNAL = ActionRisk(cls=RiskClass.HIGH, reversible=False, external=True, financial=False)


# --- test doubles -------------------------------------------------------------------


@dataclass
class RecordingExecutor:
    name: str = "recording"
    ok: bool = True
    calls: list = field(default_factory=list)

    def execute(self, action_name: str, payload: str, *, context_id: str) -> ExecutionResult:
        self.calls.append((action_name, payload, context_id))
        return ExecutionResult(ok=self.ok, detail="recorded", downstream_id="out-1", error=None if self.ok else "boom")


@dataclass
class EchoObserver:
    """A trajectory observer that returns a fixed actual-state dict (the monitor checks a bound against it)."""
    name: str = "echo"
    actual: dict = field(default_factory=dict)

    def observe(self, action_name, payload, *, context_id):
        return dict(self.actual)


def _store(tmp_path) -> BindingStore:
    """The binding registry over the same store as every gate's run journal in this test."""
    return rig(tmp_path / "store")[1]


def _gate(tmp_path, *, executor=None, observer=None, auto_fire=None):
    return EfferentGate(
        manifest=ActionManifest({}),                  # binding fires thread risk → manifest unused
        store=GateReceiptStore(tmp_path / "r.jsonl"),
        executor=executor or RecordingExecutor(),
        clock=lambda: FIXED,
        trajectory_observer=observer,
        auto_fire_actions=auto_fire,
        journal=rig(tmp_path / "store")[0],
    )


def _binding_authority(binding_id="bind-x"):
    return AuthorityScope(grantor="binding", grant="binding:time@on_loop", binding_id=binding_id, hops=0)


def _bound(gate):
    """``(authority, run)`` for a gate-level binding fire: a real binding and an admitted run of it in the
    gate's journal (every binding fire is a journaled run)."""
    binding_id, run = admit_binding_run(gate.journal)
    return _binding_authority(binding_id), run


def _fire_request(gate, *, risk=LOW_INTERNAL, ratified=None, kill_predicates=(), trigger_event=None,
                  signal=SignalAuth.STRONG, predicted_trajectory=None, action="deliver",
                  confidence=1.0):
    """An autonomous binding fire request on an admitted run: intent-free, human-absent
    (STRONG+INTENT_FREE → ON_LOOP)."""
    authority, run = _bound(gate)
    return ActionRequest(
        action_name=action, payload="do it", context_id="ctx-1", query_text="q", query_date="2026-06-30",
        trust=TrustContext(signal_auth=signal, intent_provenance=IntentProvenance.INTENT_FREE,
                           hops=0, human_present=False),
        grounded=True, authority=authority, run=run,
        overall_confidence=confidence, directive_confidence=confidence,
        risk=risk, ratified_posture=ratified, kill_predicates=kill_predicates,
        trigger_event=trigger_event, predicted_trajectory=predicted_trajectory,
    )


# =====================================================================================
# Block 1 — the GATE fire-path changes
# =====================================================================================

def test_binding_fire_resolves_against_threaded_risk_not_manifest(tmp_path):
    """Seam #1: the gate resolves against ``request.risk`` (the sealed-tool risk), NOT the (empty)
    manifest — an action the manifest never declared still fires because the binding supplied its risk."""
    ex = RecordingExecutor()
    g = _gate(tmp_path, executor=ex)
    out = g.gate(_fire_request(g, risk=LOW_INTERNAL, action="never_declared"))
    assert out.fired and out.posture is Posture.ON_LOOP
    assert ex.calls == [("never_declared", "do it", "ctx-1")]


def test_binding_fire_records_by_binding(tmp_path):
    """``by`` is derived from authority.grantor — a standing binding fire records ``by=binding`` (the 3a
    integration-test contract), with the binding_id, NOT ``by=on-loop``."""
    store = GateReceiptStore(tmp_path / "r.jsonl")
    g = _gate(tmp_path)
    req = _fire_request(g)
    out = g.gate(req)
    assert out.fired
    face = store.read()[0].action_face
    assert face["gate"] == {"verdict": "auto", "by": "binding", "binding_id": req.authority.binding_id}


def test_manual_on_loop_fire_still_by_on_loop(tmp_path):
    """Regression: a non-binding (human/manual) on-loop fire keeps ``by=on-loop`` (only grantor=binding flips it)."""
    store = GateReceiptStore(tmp_path / "r.jsonl")
    g = EfferentGate(manifest=ActionManifest({"deliver": LOW_INTERNAL}), store=store,
                     executor=RecordingExecutor(), clock=lambda: FIXED)
    req = ActionRequest(action_name="deliver", payload="p", context_id="c", query_text="q",
                        query_date="2026-06-30",
                        trust=TrustContext(signal_auth=SignalAuth.STRONG,
                                           intent_provenance=IntentProvenance.INTENT_BEARING,
                                           hops=0, human_present=True),
                        grounded=True, authority=manual_invocation())
    out = g.gate(req)
    assert out.fired
    assert store.read()[0].action_face["gate"] == {"verdict": "auto", "by": "on-loop"}


def test_ratified_posture_floors_up_a_lowered_risk(tmp_path):
    """Seam #2 fail-UP: even if the re-resolved risk is trivially low, the gate never fires BELOW the
    binding's ratified rung — a confirm-class ratified floor makes it PROPOSE, not fire."""
    g = _gate(tmp_path, auto_fire=None)
    # risk re-resolves to ON_LOOP (fires immediately), but the binding was ratified at CONFIRM →
    # max(ON_LOOP, CONFIRM) = CONFIRM → needs the confirm transport (none wired) → DEFER, never fire.
    out = g.gate(_fire_request(g, risk=LOW_INTERNAL, ratified=Posture.CONFIRM))
    assert not out.fired and out.deferred and out.posture is Posture.CONFIRM


def test_ratified_posture_does_not_lower_a_higher_reresolve(tmp_path):
    """``max`` only RAISES: a low ratified floor can't pull a high re-resolved posture down (the safe
    direction holds both ways)."""
    g = _gate(tmp_path)
    # external/irreversible risk floors at CONFIRM regardless; a stale ON_LOOP ratified floor can't lower it.
    out = g.gate(_fire_request(g, risk=HIGH_EXTERNAL, ratified=Posture.ON_LOOP))
    assert out.posture is Posture.CONFIRM and not out.fired and out.deferred


def test_known_danger_kill_trips_and_records_killed_receipt(tmp_path):
    """The KNOWN-danger kill (codex 3a.5 finding #1 closure): a kill predicate that trips against the
    trigger event KILLS the fire — no executor call, a terminal_state=killed / refuse_class=kill_triggered
    receipt, by=binding."""
    ex = RecordingExecutor()
    store = GateReceiptStore(tmp_path / "r.jsonl")
    g = _gate(tmp_path, executor=ex)
    kill = {"op": "not", "clause": {"op": "==", "field": "dmarc", "value": "pass"}}
    out = g.gate(_fire_request(g, kill_predicates=(kill,), trigger_event={"fields": {"dmarc": "fail"}}))
    assert out.killed and not out.fired and not out.refused
    assert ex.calls == []                                  # the effect never ran
    face = store.read()[0].action_face
    assert face["terminal_state"] == "killed" and face["refuse_class"] == "kill_triggered"
    assert face["gate"]["by"] == "binding"


def test_known_danger_kill_fail_safe_on_absent_field(tmp_path):
    """DIVERSE-substrate fail-safe: a kill ``dmarc != pass`` on an event with NO dmarc field TRIPS
    (UNKNOWN→trip) — the common-mode absent-field footgun the 3a.5 substrate exists to invert."""
    g = _gate(tmp_path)
    kill = {"op": "!=", "field": "dmarc", "value": "pass"}
    out = g.gate(_fire_request(g, kill_predicates=(kill,), trigger_event={"fields": {"from_domain": "x.example"}}))
    assert out.killed


def test_known_danger_kill_does_not_trip_lets_fire(tmp_path):
    """A kill that is confidently FALSE (dmarc==pass, so ``dmarc != pass`` is False) does NOT trip — the
    binding fires normally."""
    ex = RecordingExecutor()
    g = _gate(tmp_path, executor=ex)
    out = g.gate(_fire_request(
        g, kill_predicates=({"op": "!=", "field": "dmarc", "value": "pass"},),
        trigger_event={"fields": {"dmarc": "pass"}}))
    assert out.fired and ex.calls


def test_known_danger_kill_inert_for_manual_fire(tmp_path):
    """No kill_predicates / no trigger_event ⇒ the kill check is inert (a manual fire is unaffected)."""
    g = _gate(tmp_path)
    out = g.gate(_fire_request(g, kill_predicates=(), trigger_event=None))
    assert out.fired and not out.killed


def test_prediction_monitor_lights_up_via_threaded_trajectory(tmp_path):
    """4a 'lights up' the 3a.5 monitor by threading ``predicted_trajectory`` + wiring an observer: an
    actual state that VIOLATES the predicted bound KILLS the autonomous fire."""
    ex = RecordingExecutor()
    obs = EchoObserver(actual={"status": "drifted"})
    g = _gate(tmp_path, executor=ex, observer=obs)
    traj = {"bound": {"op": "==", "field": "status", "value": "nominal"}}
    out = g.gate(_fire_request(g, predicted_trajectory=traj))
    assert out.killed and ex.calls == []                  # diverged → killed pre-execute


# =====================================================================================
# Block 2 — the BindingStore verbs the fire path uses (ratify + admit's one-shot claim)
# =====================================================================================

def _mk_binding(tmp_path, *, posture=Posture.ON_LOOP, status=BindingStatus.PAUSED, one_shot=False,
                guard=(), ttype="time", store=None):
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T12:00:00",
        trigger=TriggerSpec(type=ttype, pattern={"op": "exists", "field": "tick"}),
        goal=(SubGoal(goal="do the thing", tools=("doc.append",), output="doc:x"),),
        tightness=TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9),
        posture=posture, status=status, one_shot=one_shot, guard=guard,
    )
    st = store or _store(tmp_path)
    st.add(b)
    return st, b


def _kill_guard():
    return Guard(rationale="spoofable", dissent_author="codex",
                 kill_predicate={"op": "==", "field": "blocked", "value": True},
                 kill_drill={"blocked": True}, kill_authored_by="phill")


def test_ratify_paused_to_active(tmp_path):
    st, b = _mk_binding(tmp_path)
    out = st.ratify(b.binding_id)
    assert out is not None and out.status is BindingStatus.ACTIVE
    assert [x.binding_id for x in st.list_active()] == [b.binding_id]


def test_ratify_idempotent_on_active(tmp_path):
    st, b = _mk_binding(tmp_path, status=BindingStatus.PAUSED)
    st.ratify(b.binding_id)
    again = st.ratify(b.binding_id)
    assert again is not None and again.status is BindingStatus.ACTIVE


def test_ratify_refuses_inert(tmp_path):
    st, b = _mk_binding(tmp_path)
    st.set_status(b.binding_id, BindingStatus.REVOKED)
    with pytest.raises(ValueError, match="revoked"):
        st.ratify(b.binding_id)


def test_ratify_refuses_confirm_class_without_kill(tmp_path):
    """A confirm-class candidate with no sealed kill would be barred by list_active — ratify refuses it
    EARLY (a clear error) rather than producing a silently-inert 'active' grant."""
    st, b = _mk_binding(tmp_path, posture=Posture.CONFIRM)   # no guard
    with pytest.raises(ValueError, match="sealed-floor kill"):
        st.ratify(b.binding_id)
    # WITH a sealed kill it ratifies fine
    st2, b2 = _mk_binding(tmp_path, posture=Posture.CONFIRM, guard=(_kill_guard(),),
                          store=BindingStore(tmp_path / "b2"))
    assert st2.ratify(b2.binding_id) is not None


def test_ratify_refuses_seal_broken(tmp_path):
    import json
    st, b = _mk_binding(tmp_path)
    raw = registry_of(st)
    next(iter(raw.values()))["posture"] = "ABOVE_LOOP"          # tamper the sealed core, leave the id stale
    write_raw(st, raw)
    with pytest.raises(ValueError, match="seals to"):          # the registry is corrupt: no write
        st.ratify(b.binding_id)


def test_ratify_absent_returns_none(tmp_path):
    st = BindingStore(tmp_path / "b")
    assert st.ratify("bind-nope") is None


def test_admit_claims_a_one_shot_for_one_run(tmp_path):
    st, b = _mk_binding(tmp_path, status=BindingStatus.ACTIVE, one_shot=True)
    snap = st.admit(b.binding_id, "run-1")
    assert snap is not None and snap.status is BindingStatus.ACTIVE     # the pre-claim snapshot
    assert st.get(b.binding_id).status is BindingStatus.REVOKED          # the store record is spent
    assert st.claimed_run(b.binding_id) == "run-1"
    assert st.admit(b.binding_id, "run-2") is None                       # at-most-once: another run
    assert st.admit(b.binding_id, "run-1") is not None                   # its own run, re-delivered


def test_admit_does_not_claim_a_standing_binding(tmp_path):
    st, b = _mk_binding(tmp_path, status=BindingStatus.ACTIVE, one_shot=False)
    assert st.admit(b.binding_id, "run-1") is not None and st.admit(b.binding_id, "run-2") is not None
    assert st.get(b.binding_id).status is BindingStatus.ACTIVE           # never spent


def test_admit_refuses_a_seal_broken_one_shot(tmp_path):
    st, b = _mk_binding(tmp_path, status=BindingStatus.ACTIVE, one_shot=True)
    raw = registry_of(st)
    next(iter(raw.values()))["posture"] = "ABOVE_LOOP"
    write_raw(st, raw)
    with pytest.raises(ValueError, match="seals to"):                     # tampered → not claimable
        st.admit(b.binding_id, "run-1")
    assert st.list_active() == []


# =====================================================================================
# Block 3 — the FireDispatcher end-to-end
# =====================================================================================


def _match_field_eq(pattern, event):
    """A tiny deterministic predicate evaluator for the dispatcher tests. {"op":"BAD"} raises (a
    malformed predicate); else field==value over event['fields']."""
    if pattern.get("op") == "BAD":
        raise ValueError("malformed predicate")
    return event.get("fields", {}).get(pattern.get("field")) == pattern.get("value")


def _builder(*, signal=SignalAuth.STRONG, action="deliver", builder_risk=None):
    """A test request_builder. ``builder_risk`` (default None) is what the builder puts on the request —
    the dispatcher OVERLAYS it from the risk_resolver, so a non-None value here proves the overlay wins."""
    def build(binding, event):
        return ActionRequest(
            action_name=action, payload="fire", context_id=event.get("id", "ctx"),
            query_text="q", query_date="2026-06-30",
            trust=TrustContext(signal_auth=signal, intent_provenance=IntentProvenance.INTENT_FREE,
                               hops=0, human_present=False),
            grounded=True, authority=manual_invocation(),    # placeholder — the dispatcher overlays it
            risk=builder_risk, overall_confidence=1.0, directive_confidence=1.0,
        )
    return build


def _active_binding(tmp_path, *, pattern, posture=Posture.ON_LOOP, one_shot=False, guard=(),
                    goal=None, store=None):
    st = store or _store(tmp_path)
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T12:00:00",
        trigger=TriggerSpec(type="time", pattern=pattern),
        goal=goal or (SubGoal(goal="deliver", tools=("doc.append",), output="doc:x"),),
        tightness=TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9),
        posture=posture, status=BindingStatus.PAUSED, one_shot=one_shot, guard=guard,
    )
    st.add(b)
    st.ratify(b.binding_id)
    return st, b


def _dispatcher(tmp_path, st, *, executor=None, observer=None, request_builder=None,
                risk_resolver=None):
    return FireDispatcher(
        store=st, gate=_gate(tmp_path, executor=executor, observer=observer),
        predicate_match=_match_field_eq, request_builder=request_builder or _builder(),
        risk_resolver=risk_resolver or (lambda b: LOW_INTERNAL), clock=lambda: FIXED,
    )


def test_dispatch_fires_matching_single_link_binding(tmp_path):
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    fd = _dispatcher(tmp_path, st, executor=ex)
    out = fd.dispatch({"type": "time", "id": "evt-1", "fields": {"tick": "1"}})
    assert len(out) == 1 and out[0].binding_id == b.binding_id
    assert out[0].outcome.fired and out[0].outcome.posture is Posture.ON_LOOP
    assert ex.calls == [("deliver", "fire", "evt-1")]


def test_dispatch_non_match_does_not_fire(tmp_path):
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    out = _dispatcher(tmp_path, st, executor=ex).dispatch({"type": "time", "fields": {"tick": "9"}})
    assert out == [] and ex.calls == []


def test_dispatch_skips_binding_with_malformed_predicate(tmp_path):
    """Fail-closed: a stored predicate that fails to VALIDATE bars the binding (never fires on an
    unvalidatable predicate)."""
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "BAD", "field": "tick"})
    out = _dispatcher(tmp_path, st, executor=ex).dispatch({"type": "time", "fields": {"tick": "1"}})
    assert out == [] and ex.calls == []


def test_dispatch_defers_chain_binding_to_4b(tmp_path):
    """Single-link cut: a >1-link binding MATCHES but is deferred (4a never half-executes a chain)."""
    ex = RecordingExecutor()
    chain = (SubGoal(goal="step 1", tools=("doc.append",), output="doc:a"),
             SubGoal(goal="step 2", tools=("doc.append",), output="doc:b"))
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"}, goal=chain)
    out = _dispatcher(tmp_path, st, executor=ex).dispatch({"type": "time", "fields": {"tick": "1"}})
    assert out == [] and ex.calls == []                   # deferred, not fired


def test_dispatch_one_shot_fires_once_then_revoked(tmp_path):
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"}, one_shot=True)
    fd = _dispatcher(tmp_path, st, executor=ex)
    ev = {"type": "time", "id": "e1", "fields": {"tick": "1"}}
    first = fd.dispatch(ev)
    assert first[0].outcome.fired
    assert st.get(b.binding_id).status is BindingStatus.REVOKED          # spent (claimed atomically)
    again = fd.dispatch(ev)                                              # the same event: its run replays
    assert again[0].outcome.replayed and len(ex.calls) == 1
    other = fd.dispatch({"type": "time", "id": "e2", "fields": {"tick": "1"}})   # another event: spent
    assert other == [] and len(ex.calls) == 1


def test_dispatch_known_danger_kill_via_effective_guard(tmp_path):
    """The dispatcher threads the binding's effective_guard kills → the gate kills a dangerous trigger."""
    ex = RecordingExecutor()
    guard = Guard(rationale="spoof", dissent_author="codex",
                  kill_predicate={"op": "==", "field": "blocked", "value": True},
                  kill_drill={"blocked": True}, kill_authored_by="phill")
    # ON_LOOP doesn't require a kill mandate, but the guard's kill is still EVALUATED at fire (effective_guard)
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"}, guard=(guard,))
    out = _dispatcher(tmp_path, st, executor=ex).dispatch(
        {"type": "time", "id": "e2", "fields": {"tick": "1", "blocked": True}})
    assert out[0].outcome.killed and ex.calls == []


def test_dispatch_records_fire_bookkeeping_for_standing_binding(tmp_path):
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    _dispatcher(tmp_path, st, executor=ex).dispatch({"type": "time", "id": "e", "fields": {"tick": "1"}})
    after = st.get(b.binding_id)
    assert after.graduation.fire_count == 1 and after.graduation.clean_count == 1
    assert after.graduation.last_fired_at == FIXED.isoformat()


def test_dispatch_ignores_paused_binding(tmp_path):
    """Only ACTIVE bindings fire — a PAUSED candidate is not in list_active, so dispatch never fires it."""
    ex = RecordingExecutor()
    st = _store(tmp_path)
    b = Binding.create(
        created_by="phill", created_at="2026-06-30T12:00:00",
        trigger=TriggerSpec(type="time", pattern={"op": "==", "field": "tick", "value": "1"}),
        goal=(SubGoal(goal="deliver", tools=("doc.append",), output="doc:x"),),
        tightness=TightnessVector(goal_spec=0.9, tool_min=0.9, pattern_precision=0.9, output_bound=0.9),
        posture=Posture.ON_LOOP, status=BindingStatus.PAUSED,
    )
    st.add(b)   # NOT ratified
    out = _dispatcher(tmp_path, st, executor=ex).dispatch({"type": "time", "fields": {"tick": "1"}})
    assert out == [] and ex.calls == []


def test_dispatch_non_dict_event_is_safe(tmp_path):
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    assert _dispatcher(tmp_path, st).dispatch("not-an-event") == []      # type: ignore[arg-type]


def test_dispatch_event_without_type_is_safe(tmp_path):
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    assert _dispatcher(tmp_path, st).dispatch({"fields": {"tick": "1"}}) == []


# =====================================================================================
# Block 4 — the L3 hardening fixes (risk overlay, fresh re-acquire, kill-before-screen)
# =====================================================================================

def test_risk_is_overlaid_from_resolver_not_the_request_builder(tmp_path):
    """Seam #1 made structural (codex/complement/nemotron L3): the dispatcher overlays risk from the
    injected risk_resolver, so a request_builder that sets a HIGH/external risk is IGNORED — the binding
    fires at the LOW (resolver) risk's ON_LOOP, not the HIGH risk's CONFIRM-defer."""
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    fd = FireDispatcher(
        store=st, gate=_gate(tmp_path, executor=ex),
        predicate_match=_match_field_eq,
        request_builder=_builder(builder_risk=HIGH_EXTERNAL),   # the builder LIES high...
        risk_resolver=lambda binding: LOW_INTERNAL,             # ...the resolver derives low (wins)
        clock=lambda: FIXED,
    )
    out = fd.dispatch({"type": "time", "id": "e", "fields": {"tick": "1"}})
    assert out[0].outcome.fired and out[0].outcome.posture is Posture.ON_LOOP   # the resolver's risk won
    assert ex.calls


def test_risk_resolver_raise_skips_the_binding_fail_closed(tmp_path):
    """An undeclared-tool binding: the resolver raises (UnknownAction-equivalent) → the dispatcher's
    per-binding net SKIPS it (never fires an unclassifiable grant)."""
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})

    def _boom(binding):
        raise KeyError("undeclared tool")
    fd = FireDispatcher(store=st, gate=_gate(tmp_path, executor=ex), predicate_match=_match_field_eq,
                        request_builder=_builder(), risk_resolver=_boom, clock=lambda: FIXED)
    out = fd.dispatch({"type": "time", "fields": {"tick": "1"}})
    assert out == [] and ex.calls == []


def test_dispatch_skips_standing_binding_revoked_after_list_active(tmp_path):
    """The stale-snapshot close (codex HIGH / complement MED-2 / L2 MED-1): if the binding stops being
    fireable BETWEEN the (stale) list_active and the fire, the fresh re-acquire returns None → the
    dispatcher does NOT fire (a human STOP is honored, the unsafe direction closed)."""
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})

    class _RevokedAfterList:
        """list_active returns the (stale) binding; the admission reflects the concurrent revoke."""
        journal = st.journal

        def list_active(self, **kw):
            return st.list_active(**kw)

        def list_all(self, **kw):
            return st.list_all(**kw)

        def admit(self, bid, run_id):
            return None                                          # the revoke landed since list_active

        def record_fire(self, *a, **k):
            return st.record_fire(*a, **k)
    fd = FireDispatcher(store=_RevokedAfterList(), gate=_gate(tmp_path, executor=ex),
                        predicate_match=_match_field_eq, request_builder=_builder(),
                        risk_resolver=lambda binding: LOW_INTERNAL, clock=lambda: FIXED)
    out = fd.dispatch({"type": "time", "fields": {"tick": "1"}})
    assert out == [] and ex.calls == []                          # the stale binding did NOT fire


def test_dispatch_picks_up_a_freshly_tightened_kill(tmp_path):
    """The dispatcher builds the kill set from the FRESH re-acquired snapshot, so a kill added by
    ``tighten_guard`` (an unsealed addition) IS evaluated at fire-time — not the construction snapshot."""
    ex = RecordingExecutor()
    st, b = _active_binding(tmp_path, pattern={"op": "==", "field": "tick", "value": "1"})
    # tighten with a kill that trips the event (an addition — never breaks the seal)
    tightening = Guard(rationale="late danger", dissent_author="codex",
                       kill_predicate={"op": "==", "field": "blocked", "value": True},
                       kill_drill={"blocked": True}, kill_authored_by="phill")
    st.tighten_guard(b.binding_id, tightening)
    out = _dispatcher(tmp_path, st, executor=ex).dispatch(
        {"type": "time", "id": "e", "fields": {"tick": "1", "blocked": True}})
    assert out[0].outcome.killed and ex.calls == []              # the tightened kill was honored


def test_ratify_refuses_seal_broken_even_when_already_active(tmp_path):
    """codex LOW: a seal-broken ACTIVE grant is REFUSED (a clear error), never blessed as 'already
    ACTIVE'. Since S1h-4 the refusal comes from the read: the registry's identity check fails."""
    import json
    st, b = _mk_binding(tmp_path, status=BindingStatus.ACTIVE)
    raw = registry_of(st)
    next(iter(raw.values()))["posture"] = "ABOVE_LOOP"          # tamper the sealed core of an ACTIVE binding
    write_raw(st, raw)
    with pytest.raises(ValueError, match="seals to"):
        st.ratify(b.binding_id)


def test_known_danger_kill_preempts_a_low_confidence_screen(tmp_path):
    """complement L3 HIGH-2: a kill-worthy trigger ALWAYS produces a KILL receipt, never a §1.5 REFUSE.
    An autonomous fire with a tripping kill AND a below-floor confidence is KILLED (kill before screen),
    not refused — so the operator's specific danger signal is never masked by the generic screen."""
    store = GateReceiptStore(tmp_path / "r.jsonl")
    g = _gate(tmp_path)
    kill = {"op": "==", "field": "danger", "value": True}
    req = _fire_request(g, confidence=0.1,                      # WELL below the §1.5 floor
                        kill_predicates=(kill,), trigger_event={"fields": {"danger": True}})
    out = g.gate(req)
    assert out.killed and not out.refused
    face = store.read()[0].action_face
    assert face["terminal_state"] == "killed" and face["refuse_class"] == "kill_triggered"


def test_low_confidence_without_kill_still_refused(tmp_path):
    """The reorder does not weaken the screen: a non-killed low-confidence autonomous fire still REFUSES."""
    g = _gate(tmp_path)
    req = _fire_request(g, confidence=0.1)
    out = g.gate(req)
    assert out.refused and not out.killed and out.reason.startswith("screen:")
