"""The direct mint, run (autogrant DESIGN r2.8 R7): the automation's sealed core (DESIGN §3.5) minted at ON_LOOP into
a temp store, a graduation policy covering it (DESIGN §4.1, ``min_clean_fires: 0``) signed with an enrolled laptop key
through the real authority append, and one ``inbox.file`` event fired through a real ``FireDispatcher`` +
``EfferentGate`` + :class:`levain.graduation_policy.AuthorityCoverage` with the test-only confined executor. The first
event FIRES at ON_LOOP; after the policy is revoked the next event is HELD at CONFIRM. Nothing here is new code: the
test pins that the direct path needs no gate change (DESIGN §4.1 "No gate change")."""
from __future__ import annotations

import shutil

import pytest

from levain.autonomic import (
    ActionManifest, ActionRequest, Binding, BindingStatus, BindingStore, EfferentGate, FireDispatcher,
    GateReceiptStore, Guard, IntentProvenance, PendingActionStore, Posture, RunJournal, SignalAuth, SubGoal,
    TightnessVector, TriggerSpec, TrustContext, manual_invocation,
)
from levain.autonomic.binding import binding_family
from levain.autonomic.db import AutonomicDB
from levain.graduation_policy import AuthorityCoverage

pytest.importorskip("cryptography")
from tests.autonomic_confirm_keys import confirm_signers  # noqa: E402
from tests.test_authority_graduation_kind import World, grad  # noqa: E402
from tests.test_autonomic_journal_wiring import LOW, OutboxExecutor, _Transport  # noqa: E402
from tests.test_graduation_policy import write_root  # noqa: E402

pytestmark = [pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen"),
              pytest.mark.usefixtures("test_only_confined_executor")]

MAX_ENTRY_BYTES = 1 << 20     # a stand-in: the constant is A2's; the guard only needs a number
INBOX, HANDS_USER, TARGET = "notes", "levain-hands-x", "inbox-notes"
GP = "gp-0a1b2c3d4e5f"


# the policy's signed guard (A1c2 evaluates it on each event at the fire): a kill an inbox.file event carries the
# fields of; a kill naming a field the event lacks (an email's dmarc) is UNKNOWN there, which trips (fail-safe)
INBOX_GUARD = {"rationale": "an oversized entry is the case this could be wrong about",
               "kill_predicate": {"op": ">", "field": "size", "value": MAX_ENTRY_BYTES},
               "kill_drill": {"fields": {"size": MAX_ENTRY_BYTES + 1}}}


def template(posture: Posture, *, created_at: str) -> Binding:
    """DESIGN §3.5's sealed core, as written there."""
    return Binding.create(
        created_by="operator:tester", created_at=created_at,
        trigger=TriggerSpec("inbox.file", {"inbox": INBOX, "hands_user": HANDS_USER}),
        goal=(SubGoal("write each inbox entry as a new document", ("write_document",), TARGET),),
        tightness=TightnessVector(1, 1, 1, 1), posture=posture, one_shot=False, status=BindingStatus.ACTIVE,
        guard=(Guard(rationale="the watcher's refusals, repeated on the gate's substrate", dissent_author="design",
                     kill_predicate={"op": "or", "clauses": [
                         {"op": ">", "field": "size", "value": MAX_ENTRY_BYTES},
                         {"op": "!=", "field": "utf8", "value": True}]},
                     kill_drill={"size": MAX_ENTRY_BYTES + 1, "utf8": True},
                     kill_authored_by="operator:tester (template)"),))


def match(pattern: dict, event: dict) -> bool:
    """DESIGN §3.5's predicate matcher: type, inbox and hands user; any other pattern shape raises."""
    if set(pattern) != {"inbox", "hands_user"}:
        raise ValueError(f"not an inbox.file pattern: {sorted(pattern)}")
    f = event.get("fields", {})
    return (event.get("type") == "inbox.file" and f.get("inbox") == pattern["inbox"]
            and f.get("hands_user") == pattern["hands_user"])


def build(binding: Binding, event: dict) -> ActionRequest:
    return ActionRequest(
        action_name="write_document", payload=f"doc for {event['id']}", context_id=event["id"],
        query_text=f"automation {INBOX}: {event['id']}", query_date="2026-10-25",
        trust=TrustContext(signal_auth=SignalAuth.AUTHENTICATED, intent_provenance=IntentProvenance.INTENT_FREE,
                           hops=0, human_present=False),
        grounded=True, authority=manual_invocation(), overall_confidence=1.0, directive_confidence=1.0)


def test_direct_mint_fires_on_loop_then_holds_at_confirm_after_revoke(tmp_path):
    (tmp_path / "auth").mkdir()
    auth = World(tmp_path / "auth")
    work = tmp_path / "fire"
    work.mkdir()
    store_dir = work / "store"
    store_id = AutonomicDB(store_dir).meta("store_id")
    coverage = AuthorityCoverage(auth.root, allowed_signers=write_root(auth, "laptop"), store_id=store_id)
    journal = RunJournal(store_dir)
    store = BindingStore(store_dir, journal=journal)
    gate = EfferentGate(
        confirm_signers=confirm_signers(), manifest=ActionManifest({"write_document": LOW}),
        store=GateReceiptStore(work / "receipts.jsonl"), executor=OutboxExecutor(work / "outbox.jsonl"),
        clock=lambda: auth.clock.now, transport=_Transport(), pending_store=PendingActionStore(work / "pending.json"),
        journal=journal, binding_risk=lambda b, i: LOW, coverage=coverage)
    dispatcher = FireDispatcher(store=store, gate=gate, predicate_match=match, request_builder=build,
                                clock=lambda: auth.clock.now)

    b = template(Posture.ON_LOOP, created_at=auth.clock.now.isoformat())
    assert store.add(b)
    auth.link({"op": "create_grant", "grant": grad(
        auth, GP, days=30, actions={"write_document": {"cls": 1, "reversible": True, "external": False,
                                                       "financial": False}},
        triggers=["inbox.file"], families=[binding_family(b)], store_id=store_id, min_clean_fires=0,
        guard=[INBOX_GUARD])})

    def fire(n: int):
        [d] = dispatcher.dispatch({"type": "inbox.file", "id": f"clm-{n}",
                                   "fields": {"inbox": INBOX, "hands_user": HANDS_USER, "size": 10, "utf8": True}})
        o = d.outcome
        print(f"  event {n}: fired={o.fired} posture={o.posture.name} pending={o.pending} held={o.held} "
              f"reason={o.reason}")
        return o

    first = fire(1)
    assert first.fired and first.posture is Posture.ON_LOOP
    auth.link({"op": "revoke_grant", "grant_id": GP})
    second = fire(2)
    assert not second.fired and second.pending and second.posture is Posture.CONFIRM
    assert [line.count("write_document") for line in (work / "outbox.jsonl").read_text().splitlines()] == [1]


def test_a_policys_min_clean_fires_governs_the_engine_through_authority_coverage(tmp_path, monkeypatch):
    # DESIGN r2.6 (R5): the pin, green on main. The leaf's GraduationPolicy carries no min_clean_fires, but the
    # production coverage source wraps each policy with its signed one, and graduate_due reads it from there.
    from tests.test_graduation_journey import GP as JGP
    from tests.test_graduation_journey import Journey
    j = Journey(tmp_path, monkeypatch)
    assert j.cli("policy", "create", "--action", "link0", "--lowest", "ON_LOOP", "--days", "30", "--id", JGP,
                 "--min-clean", "2") == 0
    b = j.fire.mint(Posture.COOLING_OFF)
    j.fire.store.record_fire(b.binding_id, clean=True, fired_at="2026-10-25T00:00:01")
    assert [s.outcome for s in j.engine(apply=False)] == []
    j.fire.store.record_fire(b.binding_id, clean=True, fired_at="2026-10-25T00:00:02")
    [step] = j.engine(apply=False)
    assert step.outcome == "preview" and step.uncovered == ()
