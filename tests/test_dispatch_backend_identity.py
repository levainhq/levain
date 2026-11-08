"""F9a producer: the run record's ``backend_identity`` comes from the gateway's own route map for the run's OWN executor
ticket, read by the supervisor-owned ``ticket_for`` (never a VerifierRun field a ``resolve`` could set), never a
response label or a caller's argument, and fails closed when it cannot be read; the consumer's
K-contrast (:func:`levain.dispatch.adopt._contrast_problem`) trusts only a record that says so (``identity_source``).

The e2e proof (a real gateway run on the VM) is 3c-10's; here the route map is a real ``routemaps.Checked`` and the
records are written through the REAL :meth:`JobStore.record_verification`. The follower (its production caller) is
pinned in tests/test_dispatch_follow.py."""
from __future__ import annotations

import dataclasses
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from levain.broker.policy import DispatchRoute
from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical
from levain.dispatch import adopt
from levain.dispatch import jobs as J
from levain.dispatch.verifier_report import VerifierRun
from levain.slot.routemaps import Checked
from tests.test_dispatch_adopt import A_FIX, LandWorld, make_tree

DIG = "sha256:" + "a" * 64


def _route(backends: dict[str, str]) -> DispatchRoute:
    return DispatchRoute(policy=None, provider="p", backends=backends, body_policy={}, retention_claim="c")  # type: ignore[arg-type]


def _map(generation: int = 7) -> Checked:
    return Checked(generation=generation, item_sha256="e" * 64, store_id="s" * 32,
                   routes={"rt-a": _route({"glm-5": "backend-alpha", "claude-x": "backend-beta"}),
                           "rt-b": _route({"glm-5": "backend-gamma"})},
                   digests={"rt-a": DIG, "rt-b": DIG})


def _routemap(m: Checked | None = None):
    m = _map() if m is None else m
    return lambda gen: m if gen == m.generation else None


def _ticket(job_id: str, run: int, route: str, model: str = "glm-5", over: dict | None = None) -> dict:
    t = {"kind": "executor", "job_id": job_id, "run_index": run, "route": route, "routemap_generation": 7,
         "route_policy_digest": DIG, "model_requested": model}
    t.update(over or {})
    return t


#: verify_routed's default: a ticket_for answering each run's ticket from ``runs``
FROM_RUNS = object()


class World(LandWorld):
    def verify_routed(self, job_id, runs, *, routemap=None, ticket_for=FROM_RUNS):
        """``runs``: per run ``(ticket_or_builder, caller_model)``; a builder is called with ``(job_id, run)``. The
        supervisor's ``ticket_for(job_id, run_index)`` answers, for the run it is asked about, that run's entry (so a
        builder's ``over`` is a ticket_for answering with another job's or run's ticket); ``ticket_for=None`` wires
        none, and a callable replaces it. ``self.asked`` records what ticket_for was asked. The caller's model claims
        whatever it likes."""
        from tests import test_dispatch_control as tc
        from tests import test_dispatch_verifier_report as tvr

        job = self.jstore.get(job_id)
        for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited")):
            job = self.jstore._transition(job.id, to, cause)
        tree = make_tree(self.repo, job.contract.base, A_FIX, self.tmp)
        answers = {(job.id, i): (t(job.id, i) if callable(t) else t) for i, (t, _m) in enumerate(runs)}
        self.asked = []

        def from_runs(jid, run_index):
            self.asked.append((jid, run_index))
            return answers.get((jid, run_index))

        reader = from_runs if ticket_for is FROM_RUNS else ticket_for
        for i, (_ticket, model) in enumerate(runs):
            e = {k: v for k, v in tc.BB_ENTRY.items() if k not in ("steps", "driver_input")}
            r = VerifierRun(job_id=job.id, run=i, session=f"v-{i}", ticket=f"t-{i}", verifier=e,
                            steps=tuple(tc.BB_ENTRY["steps"]), driver_input_digest=e["driver_input_digest"],
                            input_bundle_digest="sha256:" + "e" * 64)
            raw, digest = tvr.envelope(r, tvr.body(("pass",)))
            job = self.jstore.record_verification(
                r, envelope=raw, event=tvr.event(r, at=utc_stamp(datetime.now(timezone.utc)),
                                                 verifier_report_digest=digest),
                repo=str(self.repo), tree=tree, model=model, routemap=routemap, ticket_for=reader)
            if job.state == "failed":
                break
        return job

    def contrast(self, job):
        return adopt._contrast_problem(self.jstore.run_records(job.id), job.contract.template)


def on(route, model="glm-5", over=None):
    return lambda job_id, run: _ticket(job_id, run, route, model, over)


def says(label, requested="glm-5"):
    return {"requested": requested, "answered": label, "backend_identity": label}


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def assert_unresolved(world, job):
    assert job.state == "failed" and (job.history[-1].cause, job.history[-1].evidence) == (
        "verifier_refused", J.BACKEND_UNRESOLVED)
    assert world.contrast(job) is not None


# --- R1: fail closed -----------------------------------------------------------------------------------------------

def test_caller_labels_on_two_runs_of_one_backend_cannot_pass_the_contrast(world):
    """The L3 r1 scenario (every lineage): two runs on ONE backend whose callers label them distinct. Before F9a's fix
    a caller with no route facts had its labels recorded and the contrast passed; now no map means no record."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("backend-x")), (on("rt-a"), says("backend-y"))],
                              routemap=None)
    assert_unresolved(world, job)
    assert world.jstore.run_records(job.id) == []


def test_with_the_map_the_same_two_runs_record_one_backend_and_the_contrast_escalates(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("backend-x")), (on("rt-a"), says("backend-y"))],
                              routemap=_routemap())
    recs = world.jstore.run_records(job.id)
    assert [r.model["backend_identity"] for r in recs] == ["backend-alpha"] * 2
    assert [r.model["answered"] for r in recs] == ["backend-x", "backend-y"]          # the label is kept as answered
    assert "same backend identity" in world.contrast(job)


def test_a_run_with_no_ticket_facts_is_unresolved(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(None, says("backend-x"))], routemap=_routemap())
    assert_unresolved(world, job)


def test_the_map_read_is_the_tickets_generation_and_a_moved_map_is_unresolved(world):
    asked = []

    def routemap(gen):
        asked.append(gen)
        return _map(generation=8)                    # the active map moved on; the ticket was minted at 7

    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("x"))], routemap=routemap)
    assert asked == [7]
    assert_unresolved(world, job)


# --- R2': the ticket is the supervisor's (ticket_for), never the resolve's --------------------------------------

def test_a_verifier_run_carries_no_ticket_facts_for_a_resolve_to_forge():
    """L3 r2 finding 2: a resolve builds the VerifierRun, so the run's executor ticket facts are not a field of it."""
    assert "run_ticket" not in {f.name for f in dataclasses.fields(VerifierRun)}
    with pytest.raises(TypeError):
        VerifierRun(job_id="dj-" + "0" * 32, run=0, session="v-0", ticket="t-0", verifier={}, steps=(),  # type: ignore[call-arg]
                    driver_input_digest="", input_bundle_digest="",
                    run_ticket=_ticket("dj-" + "0" * 32, 0, "rt-b"))


def test_no_ticket_for_is_unresolved_even_with_the_map(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("backend-x"))], routemap=_routemap(), ticket_for=None)
    assert_unresolved(world, job)
    assert world.jstore.run_records(job.id) == []


def test_a_ticket_for_that_answers_none_is_unresolved(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("backend-x"))], routemap=_routemap(),
                              ticket_for=lambda jid, ri: None)
    assert_unresolved(world, job)


def test_the_identity_follows_ticket_for_asked_for_this_job_and_run(world):
    """Same caller model both times; only ticket_for's answer moves the record (rt-a -> alpha, rt-b -> gamma)."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("backend-forged")), (on("rt-b"), says("backend-forged"))],
                              routemap=_routemap())
    assert world.asked == [(job.id, 0), (job.id, 1)]
    recs = world.jstore.run_records(job.id)
    assert [r.model["backend_identity"] for r in recs] == ["backend-alpha", "backend-gamma"]
    assert world.contrast(job) is None


# --- R2: bound to the ticket ---------------------------------------------------------------------------------------

def test_a_mislabelled_requested_is_unresolved(world):
    """The ticket asked glm-5 on rt-a (backend-alpha); the caller's requested claude-x would key backend-beta."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a", "glm-5"), says("x", requested="claude-x"))],
                              routemap=_routemap())
    assert_unresolved(world, job)


def test_requested_is_the_tickets_when_the_caller_names_none(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a", "claude-x"), {"answered": "a"}),
                                       (on("rt-a", "glm-5"), {"answered": "b"})], routemap=_routemap())
    recs = world.jstore.run_records(job.id)
    assert [(r.model["requested"], r.model["backend_identity"]) for r in recs] == [
        ("claude-x", "backend-beta"), ("glm-5", "backend-alpha")]
    assert all(r.model["identity_source"] == "routemap" for r in recs)
    assert world.contrast(job) is None


@pytest.mark.parametrize("over", [
    {"job_id": "dj-" + "f" * 32},                    # another job's ticket
    {"run_index": 1},                                # another run's ticket
    {"run_index": True},                             # a JSON true is never a run index
    {"kind": "verifier"},                            # not the run's executor ticket
    {"route_policy_digest": "sha256:" + "b" * 64},   # the route's policy is not the one minted
    {"routemap_generation": True},
    {"route": "rt-missing"},
])
def test_a_ticket_that_is_not_this_runs_or_not_the_maps_is_unresolved(world, over):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a", over=over), says("x"))], routemap=_routemap())
    assert_unresolved(world, job)


def test_the_route_must_carry_the_tickets_model(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-b", "claude-x"), says("x", requested="claude-x"))],
                              routemap=_routemap())
    assert_unresolved(world, job)


def test_a_resolved_run_beside_an_unresolved_one_never_passes_the_contrast(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("x")), (on("rt-missing"), says("y"))],
                              routemap=_routemap())
    assert_unresolved(world, job)
    assert [r.run for r in world.jstore.run_records(job.id)] == [0]


def test_record_verification_takes_no_free_route_facts():
    with pytest.raises(TypeError):
        J.JobStore.record_verification(None, None, envelope=None, event={}, repo="", tree="", model={},  # type: ignore[arg-type]
                                       route_facts={"route": "rt-a", "routemap_generation": 7})


# --- the resolver never raises on a malformed map ------------------------------------------------------------------

def test_resolve_backend_identity_is_the_route_maps_entry_only():
    m, t = _map(), _ticket("dj-" + "0" * 32, 0, "rt-a")
    assert J.resolve_backend_identity(m, t) == "backend-alpha"
    assert J.resolve_backend_identity(m, {**t, "model_requested": "nope"}) is None
    assert J.resolve_backend_identity(m, {**t, "routemap_generation": 1}) is None
    assert J.resolve_backend_identity(None, t) is None
    assert J.resolve_backend_identity(m, None) is None
    assert J.resolve_backend_identity(m, {**t, "route": 5}) is None
    for broken in (object(), {"routes": {}}, dataclasses.replace(m, routes=None),          # type: ignore[arg-type]
                   dataclasses.replace(m, routes={"rt-a": object()}),
                   dataclasses.replace(m, routes={"rt-a": _route(None)}),                  # type: ignore[arg-type]
                   dataclasses.replace(m, generation=None),                                # type: ignore[arg-type]
                   dataclasses.replace(m, generation=True),                                # type: ignore[arg-type]
                   dataclasses.replace(m, digests={})):
        assert J.resolve_backend_identity(broken, t) is None
    gen_less = {k: v for k, v in t.items() if k != "routemap_generation"}
    assert J.resolve_backend_identity(dataclasses.replace(m, generation=None), gen_less) is None  # type: ignore[arg-type]


# --- R3: provenance ------------------------------------------------------------------------------------------------

def test_a_record_without_identity_source_escalates_and_still_reads(world):
    """A record written before F9a (no identity_source) stays readable, but the contrast never trusts its identity."""
    job, _ = world.file()
    job = world.verify_routed(job.id, [(on("rt-a"), says("x")), (on("rt-b"), says("y"))], routemap=_routemap())
    assert world.contrast(job) is None
    conn = sqlite3.connect(world.jstore.directory / J.DB_NAME)
    try:
        conn.execute("DROP TRIGGER IF EXISTS run_records_frozen")   # the test rewrites history; dispatch never does
        for run, text in conn.execute("SELECT run, record FROM run_records WHERE job_id = ?", (job.id,)).fetchall():
            rec = json.loads(text)
            del rec["model"]["identity_source"]
            legacy = adopt.ArtifactRecord(**rec)
            conn.execute("UPDATE run_records SET record = ?, digest = ? WHERE job_id = ? AND run = ?",
                         (canonical(legacy.to_mapping()), adopt.artifact_digest(legacy), job.id, run))
        conn.commit()
    finally:
        conn.close()
    recs = world.jstore.run_records(job.id)
    assert [set(r.model) for r in recs] == [{"requested", "answered", "backend_identity"}] * 2
    assert "not read from the gateway's route map" in world.contrast(job)
