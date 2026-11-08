"""F9a producer: the run record's ``backend_identity`` comes from the gateway's own route map, never a response label,
and the consumer's K-contrast (:func:`levain.dispatch.adopt._contrast_problem`) judges the records this produces.

The e2e proof (a real gateway run on the VM) is 3c-10's; here the route map is a real ``routemaps.Checked`` and the
records are written through the REAL :meth:`JobStore.record_verification`."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from levain.cockpit.consent import utc_stamp
from levain.dispatch import adopt
from levain.dispatch import jobs as J
from levain.dispatch.verifier_report import VerifierRun
from levain.slot.routemaps import Checked
from levain.broker.policy import DispatchRoute
from tests.test_dispatch_adopt import A_FIX, LandWorld, make_tree


def _route(backends: dict[str, str]) -> DispatchRoute:
    return DispatchRoute(policy=None, provider="p", backends=backends, body_policy={}, retention_claim="c")  # type: ignore[arg-type]


def _map(generation: int = 7) -> Checked:
    return Checked(generation=generation, item_sha256="e" * 64, store_id="s" * 32,
                   routes={"rt-a": _route({"glm-5": "backend-alpha", "claude-x": "backend-beta"}),
                           "rt-b": _route({"glm-5": "backend-gamma"})},
                   digests={})


def _facts(route: str, generation: int = 7) -> dict:
    return {"route": route, "routemap_generation": generation}


class World(LandWorld):
    def verify_routed(self, job_id, runs, *, routes):
        """``runs``: per run ``(route_facts, requested, caller_model_extra)``; the caller's model claims whatever
        label it likes."""
        from tests import test_dispatch_control as tc
        from tests import test_dispatch_verifier_report as tvr

        job = self.jstore.get(job_id)
        for to, cause in (("signed", "signature"), ("running", "slot_taken"), ("verifying", "runs_exited")):
            job = self.jstore._transition(job.id, to, cause)
        tree = make_tree(self.repo, job.contract.base, A_FIX, self.tmp)
        for i, (facts, requested, label) in enumerate(runs):
            e = {k: v for k, v in tc.BB_ENTRY.items() if k not in ("steps", "driver_input")}
            r = VerifierRun(job_id=job.id, run=i, session=f"v-{i}", ticket=f"t-{i}", verifier=e,
                            steps=tuple(tc.BB_ENTRY["steps"]), driver_input_digest=e["driver_input_digest"],
                            input_bundle_digest="sha256:" + "e" * 64)
            raw, digest = tvr.envelope(r, tvr.body(("pass",)))
            job = self.jstore.record_verification(
                r, envelope=raw, event=tvr.event(r, at=utc_stamp(datetime.now(timezone.utc)),
                                                 verifier_report_digest=digest),
                repo=str(self.repo), tree=tree,
                model={"requested": requested, "answered": label, "backend_identity": label},
                routes=routes, route_facts=facts)
        return job

    def contrast(self, job):
        records = self.jstore.run_records(job.id)
        return adopt._contrast_problem(records, job.contract.template)


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_identity_comes_from_the_route_map_even_when_the_response_label_claims_another(world):
    job, _ = world.file()
    # both runs' responses LABEL themselves a different, distinct model; a label-reading producer would record those
    job = world.verify_routed(job.id, [(_facts("rt-a"), "glm-5", "label-one"), (_facts("rt-b"), "glm-5", "label-two")],
                              routes=_map())
    got = [r.model["backend_identity"] for r in world.jstore.run_records(job.id)]
    assert got == ["backend-alpha", "backend-gamma"]
    assert all(r.model["answered"].startswith("label-") for r in world.jstore.run_records(job.id))   # label kept as `answered`


def test_a_label_claiming_the_other_backend_cannot_split_one_backend(world):
    job, _ = world.file()
    # same route twice; the labels claim two distinct backends, the map says one: the contrast must refuse
    job = world.verify_routed(job.id, [(_facts("rt-a"), "glm-5", "label-one"), (_facts("rt-a"), "glm-5", "label-two")],
                              routes=_map())
    assert [r.model["backend_identity"] for r in world.jstore.run_records(job.id)] == ["backend-alpha"] * 2
    assert "same backend identity" in world.contrast(job)


def test_two_runs_on_distinct_backends_satisfy_the_contrast(world):
    job, _ = world.file()
    job = world.verify_routed(job.id, [(_facts("rt-a"), "glm-5", "x"), (_facts("rt-a"), "claude-x", "x")],
                              routes=_map())
    assert world.contrast(job) is None


@pytest.mark.parametrize("facts, routes", [
    (_facts("rt-missing"), _map()),                  # route not in the map
    (_facts("rt-a", generation=6), _map()),          # the map moved since the mint
    (_facts("rt-b"), None),                          # no map at all
    (_facts("rt-b"), _map()),                        # the route does not carry the requested model (claude-x)
])
def test_an_unresolved_route_writes_no_record_and_the_consumer_escalates(world, facts, routes):
    job, _ = world.file()
    requested = "claude-x" if facts["route"] == "rt-b" else "glm-5"   # rt-b does not carry claude-x
    if routes is None:
        requested = "glm-5"
    job = world.verify_routed(job.id, [(facts, requested, "label-one")], routes=routes)
    assert job.state == "failed" and (job.history[-1].cause, job.history[-1].evidence) == (
        "verifier_refused", J.BACKEND_UNRESOLVED)
    stored = world.jstore.run_records(job.id)
    assert [r.run for r in stored] == []   # the failed run 0 wrote nothing; the job is terminal
    assert "runs are not 0.." in world.contrast(job)   # the consumer escalates on the missing run


def test_a_resolved_run_beside_an_unresolved_one_never_passes_the_contrast(world):
    job, _ = world.file()
    # run 1 is the unresolved one (run 0 resolves and is recorded first)
    job = world.verify_routed(job.id, [(_facts("rt-a"), "glm-5", "x"), (_facts("rt-missing"), "glm-5", "y")],
                              routes=_map())
    assert job.state == "failed" and job.history[-1].evidence == J.BACKEND_UNRESOLVED
    assert [r.run for r in world.jstore.run_records(job.id)] == [0]
    assert world.contrast(job) is not None


def test_resolve_backend_identity_is_the_route_maps_entry_only():
    m = _map()
    assert J.resolve_backend_identity(m, _facts("rt-a"), "glm-5") == "backend-alpha"
    assert J.resolve_backend_identity(m, _facts("rt-a"), "nope") is None
    assert J.resolve_backend_identity(m, _facts("rt-a", generation=1), "glm-5") is None
    assert J.resolve_backend_identity(None, _facts("rt-a"), "glm-5") is None
    assert J.resolve_backend_identity(m, None, "glm-5") is None
    assert J.resolve_backend_identity(m, {"route": 5, "routemap_generation": 7}, "glm-5") is None
