"""14b S3a: the gate's graduation clamp, its fence and its admission (DESIGN_REV3 §4-§5, DESIGN_REV3_1 D1/D3/D4,
HEAD_RULINGS_R1 R9).

A binding's effect, or an unattended manual effect, runs below CONFIRM only while a live anchored graduation
policy signs its action at the risk the gate's manifest gives it. Everything here runs through a real run
journal, binding registry, pending store and gate; the coverage source is the test-only in-memory one
(``tests/autonomic_test_only_coverage.py``), since the authority-log source is S4's.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
from pathlib import Path

import pytest

import levain.autonomic.gate as gate_module
from levain.autonomic import (
    ActionManifest, ActionRisk, Binding, BindingStatus, BindingStore, ChainExecutor,
    ConfirmDecision, EfferentGate, FireDispatcher, GateReceiptStore, Guard, PendingActionStore, Posture,
    RiskClass, RunJournal, RunRef, SubGoal, TriggerSpec, hold_id_for, manual_invocation,
)
from levain.autonomic.binding import binding_invocation
from levain.autonomic.handle import GateHandle, as_handle
from levain.autonomic.journal import effect_digest
from levain.autonomic.pending import PendingAction
from tests.autonomic_confirm_keys import confirm_signers, signed_yes
from tests.autonomic_test_only_coverage import TestOnlyCoverage, policy, root_file
from tests.test_autonomic_journal_wiring import (
    FIXED, HIGH, LOW, TIGHT, OutboxExecutor, _chain_builder, _single_builder, _Transport, _trust,
)

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")

# a different risk dict at the same floor: the manifest reclassifies link0 without raising its rung
LOW_MEDIUM = ActionRisk(cls=RiskClass.MEDIUM, reversible=True, external=False, financial=False)
LATER = FIXED + _dt.timedelta(days=1)
EXPIRES = FIXED + _dt.timedelta(days=10)


class CWorld:
    """The fire path over one directory, on a gate with ``coverage`` (none: the gate's default)."""

    def __init__(self, work: Path, *, coverage: TestOnlyCoverage | None = None,
                 auto_fire: frozenset[str] = frozenset(), manifest: dict | None = None) -> None:
        work.mkdir(parents=True, exist_ok=True)
        self.work = work
        self.now = FIXED
        self.journal = RunJournal(work / "store")
        self.store = BindingStore(work / "store", journal=self.journal)
        self.pending = PendingActionStore(work / "pending.json")
        self.receipts = GateReceiptStore(work / "receipts.jsonl")
        self.tool_risk = {0: LOW, 1: HIGH}
        extra = {} if coverage is None else {"coverage": coverage}
        self.gate = EfferentGate(
            confirm_signers=confirm_signers(),
            manifest=ActionManifest(manifest or {"link0": LOW, "link1": HIGH, "evil": LOW}),
            store=self.receipts, executor=OutboxExecutor(work / "outbox.jsonl"), clock=lambda: self.now,
            transport=_Transport(), pending_store=self.pending, journal=self.journal,
            binding_risk=lambda b, i: self.tool_risk[i], auto_fire_actions=auto_fire, **extra)
        self.chains = ChainExecutor(gate=self.gate, request_builder=_chain_builder, trust_resolver=_trust,
                                    clock=lambda: self.now, binding_store=self.store)
        self.dispatcher = FireDispatcher(
            store=self.store, gate=self.gate,
            predicate_match=lambda p, e: e.get("fields", {}).get(p["field"]) == p["value"],
            request_builder=_single_builder, clock=lambda: self.now, chain_executor=self.chains)

    def mint(self, posture: Posture, *, chain: bool = False, created_at: str = "2026-10-07T09:00:00g") -> Binding:
        goal = ((SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),
                 SubGoal(goal="email it", tools=("mail.send",), output="email:self")) if chain
                else (SubGoal(goal="summarize", tools=("mail.read",), output="doc:s"),))
        b = Binding.create(
            created_by="operator", created_at=created_at,
            trigger=TriggerSpec(type="email", pattern={"field": "from", "value": "a@x.example"}),
            goal=goal, tightness=TIGHT, posture=posture,
            guard=(Guard(rationale="spoofed sender", dissent_author="codex",
                         kill_predicate={"op": "==", "field": "dmarc", "value": "fail"},
                         kill_drill={"dmarc": "fail"}, kill_authored_by="operator"),),
            status=BindingStatus.PAUSED)
        self.store.add(b)
        self.store.ratify(b.binding_id)
        return b

    def dispatch(self, event_id: str):
        [d] = self.dispatcher.dispatch({"type": "email", "id": event_id,
                                        "fields": {"from": "a@x.example", "dmarc": "pass"}})
        return d

    def plant_hold(self, b: Binding, action: str, *, posture: str = "COOLING_OFF", run: str = "run-planted"):
        """What a writer of the levain home can make: a run of ``b`` holding a sealed cooling-off pending of
        ``action``, past its window (DESIGN_REV3 X-H1, codex's laundering sequence)."""
        self.journal.start(run, binding_id=b.binding_id)
        p = PendingAction.create(
            created_at=FIXED.isoformat(), action_name=action, payload=f"{action} payload", context_id="planted",
            query_text="q", query_date="2026-10-07", posture=posture, fail_open=Posture[posture].fail_open,
            requires_typed=Posture[posture].requires_typed,
            authority=binding_invocation(self.store.get(b.binding_id)).to_dict(),
            expires_at=(FIXED - _dt.timedelta(hours=1)).isoformat(), risk_floor="ON_LOOP",
            hold_id=hold_id_for(run, "link-0"))
        self.journal.hold(run, "link-0", digest=effect_digest(action_name=action, payload=p.payload,
                                                              context_id=p.context_id),
                          pending=p.to_dict(), at=FIXED.isoformat())
        return p

    def sweep(self, at: _dt.datetime = LATER):
        self.now = at
        return as_handle(self.gate).sweep_timeouts(at)

    def outbox(self) -> list[str]:
        p = self.work / "outbox.jsonl"
        return [json.loads(line)["action"] for line in p.read_text().splitlines()] if p.exists() else []


def _covering(*actions: str, lowest: str = "ON_LOOP", **kw) -> TestOnlyCoverage:
    risks = {"link0": LOW, "link1": HIGH, "evil": LOW}
    return TestOnlyCoverage([policy("gp-a", {a: risks[a] for a in actions}, expires_at=EXPIRES, lowest=lowest,
                                    **kw)])


# --- the laundering sequence (codex X-H1) ------------------------------------------------------------------

@pytest.mark.parametrize("wired", [False, True])
def test_a_planted_cooling_off_hold_with_an_unsigned_action_resolves_at_confirm_and_never_fires(tmp_path, wired):
    # the hold names binding B, which a policy covers for link0; its pending carries "evil", which no policy
    # signs. Without the clamp the silence default fired it (the pending's own rung, the action allowlisted).
    w = CWorld(tmp_path, coverage=_covering("link0") if wired else None, auto_fire=frozenset({"evil"}))
    b = w.mint(Posture.ON_LOOP)
    w.plant_hold(b, "evil")
    [out] = w.sweep()
    assert not out.fired and out.refused and out.reason == "denied:cover:none"
    assert w.outbox() == [] and w.gate.open_pendings() == []


def test_a_signed_action_at_a_changed_manifest_risk_resolves_at_confirm(tmp_path):
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=frozenset({"link0"}))
    w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    [p] = w.gate.open_pendings()
    assert p.posture == "COOLING_OFF"
    w.gate._manifest = ActionManifest({"link0": LOW_MEDIUM, "link1": HIGH})   # the same floor, another risk
    [out] = w.sweep()
    assert not out.fired and out.reason == "denied:cover:none" and w.outbox() == []


def test_a_covered_cooling_off_hold_fires_on_silence(tmp_path):
    # the control for the two above: the same hold, its action signed at the manifest's risk
    w = CWorld(tmp_path, coverage=_covering("link0", lowest="COOLING_OFF"), auto_fire=frozenset({"link0"}))
    w.mint(Posture.COOLING_OFF)
    w.dispatch("m1")
    [out] = w.sweep()
    assert out.fired and w.outbox() == ["link0"]


def test_a_lowered_pending_posture_resolves_at_confirm(tmp_path):
    # the sealed binding is CONFIRM; a writer re-seals the pending of its run's first link (not the terminal one,
    # whose sealed rung the resolve re-derives itself) at COOLING_OFF. Uncovered, the clamp puts it back at
    # CONFIRM, whose silence default is a drop.
    w = CWorld(tmp_path, auto_fire=frozenset({"link0"}))
    b = w.mint(Posture.CONFIRM, chain=True)
    w.plant_hold(b, "link0")
    [out] = w.sweep()
    assert not out.fired and out.reason == "denied:cover:none" and w.outbox() == []


def test_a_direct_unattended_resolve_of_an_uncovered_binding_hold_fires_nothing(tmp_path):
    # codex r3.1: the public resolve, not only the sweep, is an unattended path (P4)
    w = CWorld(tmp_path, auto_fire=frozenset({"link0"}))
    b = w.mint(Posture.CONFIRM)
    p = w.plant_hold(b, "link0")
    w.now = LATER
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by="on-loop"))
    assert not out.fired and out.refused and w.outbox() == []


# --- the admission: what moves between the decision and the intent ------------------------------------------

def test_a_covered_on_loop_link_fires(tmp_path):
    w = CWorld(tmp_path, coverage=_covering("link0"))
    w.mint(Posture.ON_LOOP)
    assert w.dispatch("f1").outcome.fired and w.outbox() == ["link0"]


@pytest.mark.parametrize("move", ["revoke", "root_edit", "expiry"])
def test_authority_moving_between_decision_and_admission_is_stale_then_held(tmp_path, move):
    src = _covering("link0")
    w = CWorld(tmp_path, coverage=src)
    w.mint(Posture.ON_LOOP)
    if move == "revoke":
        mover = lambda: src.revoke("gp-a")                                  # noqa: E731 — the head moves
    elif move == "root_edit":
        mover = lambda: src.set_root(root_file())                           # noqa: E731 — the signer gone
    else:
        mover = lambda: setattr(w, "now", EXPIRES)                          # noqa: E731 — it lapses
    real = w.gate._fire_check
    moved: list[bool] = []

    def check_then_move(*args):
        # the authority moves right after the fire check (A1c2), once, so the admission's fence reads the move
        got = real(*args)
        if not moved:
            moved.append(True)
            mover()
        return got
    w.gate._fire_check = check_then_move
    first = w.dispatch("s1").outcome
    assert moved and first.held and first.reason == "journal:stale" and not first.fired
    again = w.dispatch("s1").outcome                                         # decided again, under what holds now
    assert again.pending and again.posture is Posture.CONFIRM and not again.fired
    assert w.outbox() == []


def test_an_expired_policy_holds_the_effect(tmp_path):
    w = CWorld(tmp_path, coverage=_covering("link0"))
    w.mint(Posture.ON_LOOP)
    w.now = EXPIRES
    out = w.dispatch("x1").outcome
    assert out.pending and out.posture is Posture.CONFIRM and w.outbox() == []


def test_a_failed_refresh_holds_at_confirm_and_a_signed_approval_still_runs(tmp_path):
    src = _covering("link0")
    src.fail_refresh = True
    w = CWorld(tmp_path, coverage=src)
    w.mint(Posture.ON_LOOP)
    out = w.dispatch("r1").outcome
    assert out.pending and out.posture is Posture.CONFIRM and "admission_raced" not in out.reason
    [p] = w.gate.open_pendings()
    done = w.gate.resolve(p.pending_id, signed_yes(w.gate, p.pending_id))
    assert done.fired and "admission_raced" not in done.reason and w.outbox() == ["link0"]
    assert src.refreshes >= 1


def test_a_ratified_posture_other_than_the_sealed_bindings_is_refused(tmp_path):
    w = CWorld(tmp_path, coverage=_covering("link0"))
    b = w.mint(Posture.ON_LOOP)
    w.journal.start("run-forged", binding_id=b.binding_id)
    request = dataclasses.replace(
        _single_builder(b, {"id": "z1"}), risk=LOW, authority=binding_invocation(w.store.get(b.binding_id)),
        ratified_posture=Posture.ABOVE_LOOP, run=RunRef("run-forged", "link-0"))
    out = w.gate.gate(request)
    assert out.refused and out.reason == "ratified_posture_mismatch" and w.outbox() == []


# --- D4: every binding link, the intermediate ones included -------------------------------------------------

@pytest.mark.parametrize("covered", [False, True])
def test_an_intermediate_on_loop_link_runs_only_when_covered(tmp_path, covered):
    w = CWorld(tmp_path, coverage=_covering("link0") if covered else None)
    w.mint(Posture.CONFIRM, chain=True)
    out = w.dispatch("c1")
    if covered:
        assert w.outbox() == ["link0"] and len(w.gate.open_pendings()) == 1   # link1 (HIGH) asks
    else:
        assert w.outbox() == [] and not out.outcome.fired
        [p] = w.gate.open_pendings()
        assert p.action_name == "link0" and p.posture == "CONFIRM"


@pytest.mark.parametrize("names_it", [False, True])
def test_a_family_scoped_policy_covers_only_its_family(tmp_path, names_it):
    src = TestOnlyCoverage()
    w = CWorld(tmp_path, coverage=src)
    b = w.mint(Posture.ON_LOOP)
    family = gate_module.binding_family(b) if names_it else "fam-" + "0" * 32
    src.grant(policy("gp-f", {"link0": LOW}, expires_at=EXPIRES, families=[family], triggers=["email"]))
    assert w.dispatch("f1").outcome.fired is names_it


def test_the_family_ignores_the_rung_and_follows_the_trigger():
    def make(posture, value):
        return Binding.create(created_by="operator", created_at="2026-10-07T09:00:00", trigger=TriggerSpec(
            type="email", pattern={"field": "from", "value": value}), goal=(SubGoal(goal="s", tools=("t",),
            output="o"),), tightness=TIGHT, posture=posture, status=BindingStatus.PAUSED)
    a, b, c = make(Posture.CONFIRM, "a"), make(Posture.ON_LOOP, "a"), make(Posture.CONFIRM, "b")
    fam = gate_module.binding_family
    assert fam(a) == fam(b) != fam(c) and fam(a).startswith("fam-") and len(fam(a)) == 36


# --- D1 and R9: the unattended manual resolve ---------------------------------------------------------------

def _manual(tmp_path, coverage=None, posture="COOLING_OFF"):
    w = CWorld(tmp_path, coverage=coverage, auto_fire=frozenset({"link0"}))
    p = PendingAction.create(
        created_at=FIXED.isoformat(), action_name="link0", payload="manual payload", context_id="m",
        query_text="q", query_date="2026-10-07", posture=posture, fail_open=Posture[posture].fail_open,
        requires_typed=Posture[posture].requires_typed, authority=manual_invocation().to_dict(),
        expires_at=(FIXED + _dt.timedelta(hours=1)).isoformat())
    w.pending.add(p)
    return w, p


def test_an_uncovered_manual_cooling_off_pending_is_denied_by_the_sweep(tmp_path):
    w, p = _manual(tmp_path)
    [out] = w.sweep()
    assert not out.fired and out.reason == "cover:none" and w.outbox() == []
    assert w.pending.get(p.pending_id) is None
    [r] = w.receipts.read()
    assert r.fired is False and r.action_face["terminal_state"] == "timed_out"


def test_a_covered_manual_cooling_off_pending_fires_on_silence(tmp_path):
    w, _ = _manual(tmp_path, _covering("link0"))
    [out] = w.sweep()
    assert out.fired and w.outbox() == ["link0"]


@pytest.mark.parametrize("change", ["revoked", "signer_removed", "family_scoped", "trigger_scoped"])
def test_a_manual_effect_no_null_null_policy_covers_is_denied(tmp_path, change):
    if change == "family_scoped":
        src = _covering("link0", families=["fam-" + "1" * 32])
    elif change == "trigger_scoped":
        src = _covering("link0", triggers=["email"])
    else:
        src = _covering("link0")
    w, _ = _manual(tmp_path, src)
    if change == "revoked":
        src.revoke("gp-a")
    elif change == "signer_removed":
        src.set_root(root_file())
    [out] = w.sweep()
    assert not out.fired and out.reason == "cover:none" and w.outbox() == []


def test_a_transient_refresh_failure_leaves_the_manual_pending_open(tmp_path):
    src = _covering("link0")
    w, p = _manual(tmp_path, src)
    src.fail_refresh = True
    within = FIXED + _dt.timedelta(minutes=90)               # expired, and less than a window past it
    assert w.sweep(within) == []                             # not a decision: nothing claimed
    assert w.pending.get(p.pending_id) is not None and w.outbox() == []
    src.fail_refresh = False
    [out] = w.sweep(within)
    assert out.fired and w.outbox() == ["link0"]


def test_a_direct_unattended_resolve_of_a_manual_pending_is_guarded(tmp_path):
    w, p = _manual(tmp_path)
    w.now = LATER
    out = w.gate.resolve(p.pending_id, ConfirmDecision(approved=True, by="on-loop"))
    assert not out.fired and out.reason == "cover:none" and w.outbox() == []


def _outcome_and_receipt(w, out):
    [r] = w.receipts.read()
    return dataclasses.replace(out, receipt_id=None), r.action_face, r.fired, r.posture


def test_a_human_signed_manual_resolve_is_unchanged_by_coverage(tmp_path):
    seen = []
    for name, src in (("none", None), ("source", TestOnlyCoverage())):
        w, p = _manual(tmp_path / name, src, posture="CONFIRM")
        out = w.gate.resolve(p.pending_id, signed_yes(w.gate, p.pending_id))
        assert out.fired
        seen.append(_outcome_and_receipt(w, out))
        if src is not None:
            assert src.refreshes == 0 and src.peeks == 0
    assert seen[0] == seen[1]


def test_a_manual_on_loop_forward_fire_is_unchanged_by_coverage(tmp_path):
    seen = []
    for name, src in (("none", None), ("source", TestOnlyCoverage())):
        w = CWorld(tmp_path / name, coverage=src)
        out = w.gate.gate(_single_builder(None, {"id": "h1"}))
        assert out.fired and out.posture is Posture.ON_LOOP
        seen.append(_outcome_and_receipt(w, out))
        if src is not None:
            assert src.refreshes == 0 and src.peeks == 0
    assert seen[0] == seen[1]


# --- every fence at or above CONFIRM is the bytes it was ----------------------------------------------------

def _fence_before_the_clamp(manifest: ActionRisk | None, binding: ActionRisk | None) -> str:
    def d(r):
        return None if r is None else {"cls": int(r.cls), "reversible": r.reversible, "external": r.external,
                                       "financial": r.financial}
    text = json.dumps({"manifest": d(manifest), "binding": d(binding)}, sort_keys=True, separators=(",", ":"))
    return "fence-" + hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("m,b", [(LOW, LOW), (HIGH, HIGH), (None, HIGH), (LOW, None), (None, None)])
def test_the_fence_without_a_cover_is_the_old_bytes(m, b):
    assert gate_module._risk_fence(m, b) == _fence_before_the_clamp(m, b)
    assert gate_module._risk_fence(m, b, "none") != _fence_before_the_clamp(m, b)


@pytest.mark.parametrize("wired", [False, True])
def test_a_confirm_holds_challenge_carries_the_old_fence(tmp_path, wired):
    w = CWorld(tmp_path, coverage=_covering("link1") if wired else None)
    w.mint(Posture.CONFIRM, chain=True)
    w.tool_risk[0] = HIGH                                     # link0 too is at CONFIRM: never clamped
    w.dispatch("k1")
    [p] = w.gate.open_pendings()
    body = json.loads(w.gate.confirm_challenge(p.pending_id))
    assert body["fence"] == _fence_before_the_clamp(LOW, HIGH)


# --- the policy arguments ------------------------------------------------------------------------------------

def test_coverage_is_a_policy_argument_and_a_handle_swap_keeps_it(tmp_path):
    assert "coverage" in gate_module._POLICY_ARGUMENTS
    src = TestOnlyCoverage()
    w = CWorld(tmp_path, coverage=src)
    assert w.gate.coverage is src
    assert w.gate.with_policy(w.gate._manifest, w.gate.binding_risk).coverage is None
    handle = GateHandle(w.gate)
    new = handle.swap(ActionManifest({"link0": LOW}), {"mail.read": LOW}, lambda tools, cat: cat[tools[0].tools[0]])
    assert new.coverage is src and handle.current() is new

