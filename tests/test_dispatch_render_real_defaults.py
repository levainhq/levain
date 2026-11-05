"""Pre-VM integration test (1105-32 DESIGN_render_wiring section 7 closing paragraph, section 8 row 4): a dispatch
session driven through the root helper's ``render`` with the REAL defaults on both sides, nothing injected.

Why it exists. The VM fire of 1105-32 found five defects that share one shape: two pieces of code that must agree, each
tested against its own fake (the launcher's mint facts vs the runtime's blob; the runtime's hook request vs the
helper's templates; the staging seam vs its stub). Every runtime test injected ``routes=``, the hook templates and the
``workspace`` seam, so the defaults never met the checks. Here none of them is injected:

- the mint facts are ``RealLauncher._facts`` (checked by ``tickets.check_facts``, minted by the real ticket store);
- the base map is the REAL ``vendor_routes.merge`` of what ``setup`` would write (it holds ``egress-default`` and the
  Claude passthrough route), written where both CONTROL and the helper read it;
- the blob is ``HelperSlotRuntime._render_blob`` with ``routes=None`` and ``workspace=None`` (its defaults);
- the helper's seams leave ``hook_templates`` at ``None`` (the production producer);
- ``render`` then runs ``check_ticket`` and ``render_facts`` -> ``check_facts`` for real, on the F2 helper world.

The workspace seam is unwired by design (the stager is lane 3's). The one refusal this pipeline is EXPECTED to end on is
therefore ``staging-missing`` at render step (3). :func:`test_KNOWN_NEXT_REFUSAL_staging_missing` pins that as a named
marker: when the stager lands it goes RED on purpose, and the fix is to update it (stage through the default seam and
assert the files step passes), never to skip it.
"""
from __future__ import annotations

import pytest

from levain.agents import session_launcher as L
from levain.contracts import canon
from levain.slot import control as C
from levain.slot import dispatch_runtime as DR
from levain.slot import render as RD
from levain.slot import runtime_helper as H
from levain.slot import stage
from levain.slot import tickets as T
from levain.slot import vendor_routes as VR
from levain.slot.supervisor import pick_routes
from levain.slot.ids import SlotId
from tests.test_agents_session_launch import EXP
from tests.test_agents_session_launcher import KEY, RUN, FakeControl, FakeRuntime, FakeSources, launch
from tests.test_slot_p7a1_passthrough_ticket import mark, ptd  # noqa: F401 - the ticketed passthrough world fixture
from tests.test_pool import F2_DISPATCH

#: the refusal the pipeline ends on while the workspace seam is unwired, and the render steps it reaches before it
KNOWN_NEXT = "staging-missing"
STEPS_REACHED = ["cas-rendering", "policy", "files"]

TWO_EGRESS = {"format": VR.BASE_FORMAT, "routes": [
    {"route": "egress-operator", "type": "egress", "hosts": ["docs.example.org"]},
    {"route": "egress-second", "type": "egress", "hosts": ["pkgs.example.org"]}]}


class Pipe:
    """One dispatch launch pushed as far as the unwired pipeline goes. Built by :func:`pipe`."""

    def __init__(self, d, w, rt, req, session, blob_bytes, ticket):
        self.d, self.w, self.rt, self.req = d, w, rt, req
        self.session, self.blob_bytes, self.ticket = session, blob_bytes, ticket

    def render(self):
        self._before = len(self.w.steps)                      # prepare's own steps are already in the list
        return self.w.main(["render", self.session], self.blob_bytes, uid=F2_DISPATCH)

    @property
    def steps(self):
        """The render steps ``seams.at`` saw since :meth:`render` began."""
        return self.w.steps[self._before:]

    def ctx(self):
        w = self.w
        store = H.Store(w.seams)
        return H.Ctx(w.seams, store, F2_DISPATCH, "dispatch", w.control.operator_uid, {}), store


def pipe(ptd, monkeypatch, tmp_path, doc=None):  # noqa: F811
    """Launcher facts -> real mint -> real prepare -> the runtime's default blob -> staged exactly as ``start`` stages
    (the brief, then the workspace seam only if the runtime has a default one)."""
    d, w = ptd, ptd.w
    merged, _ids, _notes = VR.merge(doc)                      # setup's own merge; None = an absent map
    (w.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(merged))
    base = w.L / "routes" / "base.json"
    monkeypatch.setattr(DR.layout, "base_routes_path", lambda host: base)
    rt = DR.HelperSlotRuntime(host="linux", helper=("true",), store=w.L, control_socket=tmp_path / "c.sock")
    assert rt._routes is None                                 # the default routes: never injected

    ln = L.RealLauncher(FakeControl(), FakeRuntime(), FakeSources(), L.SessionIndex(tmp_path / "idx"))
    lau = launch()
    facts = ln._facts(lau)
    assert facts["egress_routes"] == []                       # the launcher lists none (Phill 11-02)
    ticket = d.tickets.mint(str(facts["attempt_id"]), T.check_facts(facts), C.new_handle).ticket
    mark(w, SlotId("egress", 0))
    line = d.prepare(ticket)
    assert line["ok"], (line, w.last_err)
    session = line["session"]
    req = L.SlotStart(ticket, RUN, KEY, "claude", lau.model, lau.brief, str(facts["brief_digest"]), L.HOOKS)
    blob_bytes = rt._render_blob(req)
    staging = w.L / "staging" / session
    L.stage_brief_for(req, staging)                           # start()'s order: the brief ...
    if rt._workspace is not None:                             # ... then the workspace seam, when the runtime has one
        rt._workspace(req, staging)
    return Pipe(d, w, rt, req, session, blob_bytes, ticket)


def _gates(p):
    """render's two pre-CAS gates, called directly with the pipeline's own blob and alloc."""
    a = p.w.alloc(p.session)
    blob = H.parse_stdin("render", p.blob_bytes)
    RD.check_ticket(blob, a.kind, a.slot.slot_class, a.ticket, a.ticket_facts, auth=a.auth)
    ctx, store = p.ctx()
    try:
        facts = H.render_facts(ctx, a, blob)
        RD.check_facts(blob, facts)
    finally:
        ctx.close()
        store.close()
    return blob, facts


@pytest.mark.parametrize("doc,egress_ids", [
    (None, {VR.EGRESS_DEFAULT}),
    (TWO_EGRESS, {"egress-operator", "egress-second", VR.EGRESS_DEFAULT}),
], ids=["default-base-map", "two-egress-routes"])
def test_the_real_defaults_pass_both_render_gates(ptd, monkeypatch, tmp_path, doc, egress_ids):  # noqa: F811
    p = pipe(ptd, monkeypatch, tmp_path, doc)
    routes_in_map = {r["route"] for r in VR.merge(doc)[0]["routes"] if r["type"] == "egress"}
    assert egress_ids <= routes_in_map                        # the map really carries the egress routes under test
    blob, facts = _gates(p)                                   # red when pick_routes keeps the egress routes, or when
    assert len(blob.routes) == 1                              # the helper has no hook templates by default
    assert not set(blob.routes) & routes_in_map               # only the Claude passthrough route
    assert facts.route_types[blob.routes[0]] == "subscription_passthrough"


def test_the_hook_templates_the_helper_defaults_to_satisfy_check_facts(ptd, monkeypatch, tmp_path):  # noqa: F811
    p = pipe(ptd, monkeypatch, tmp_path)
    assert p.w.seams.hook_templates is None                   # the production path: nothing injected
    blob, facts = _gates(p)
    assert set(facts.hook_templates) == set(blob.hooks) == set(L.HOOKS)
    assert {n: t.event for n, t in facts.hook_templates.items()} == {"levain_gate": "PreToolUse",
                                                                     "levain_queue": "PermissionRequest"}
    assert all(type(t.body) is bytes and t.body for t in facts.hook_templates.values())


def test_control_a_helper_without_templates_is_refused_by_name(ptd, monkeypatch, tmp_path):  # noqa: F811
    """The check is live: the same pipeline with ``hook_templates={}`` (what the VM saw) refuses by name."""
    p = pipe(ptd, monkeypatch, tmp_path)
    p.w.seams.hook_templates = {}
    code, ans = p.render()
    assert (code, ans["refused"]) == (H.EXIT_REFUSED, "hook-template-missing")
    assert p.steps == []                                      # refused before step (1)'s CAS: nothing was acted on


def test_control_the_old_blob_with_the_egress_routes_is_refused(ptd, monkeypatch, tmp_path):  # noqa: F811
    """The check is live: the pre-fix blob (``pick_routes`` keeping the base map's egress routes) is the VM's
    ``ticket-routes-mismatch``."""
    p = pipe(ptd, monkeypatch, tmp_path)
    old = pick_routes(DR.read_base_map(p.w.L / "routes" / "base.json"), "claude", "passthrough")   # egress=True
    assert VR.EGRESS_DEFAULT in old and len(old) > 1
    blob = canon.canonical_bytes({"format": DR.RENDER_FORMAT, "vendor": "claude", "routes": list(old),
                                  "model": p.req.model, "hooks": list(p.req.hooks)})
    p.blob_bytes = blob
    code, ans = p.render()
    assert (code, ans["refused"]) == (H.EXIT_REFUSED, "ticket-routes-mismatch")
    assert p.steps == []


def test_KNOWN_NEXT_REFUSAL_staging_missing(ptd, monkeypatch, tmp_path):  # noqa: F811
    """THE NAMED MARKER. The workspace seam is unwired by design (``workspace=None`` is the runtime's default), so
    nothing stages ``repo.bundle`` and render ends exactly at step (3) 'files', refused ``staging-missing``: past the
    ticket gate, past ``render_facts`` -> ``check_facts``, past step (1)'s CAS and step (2)'s policy. It reaches no
    later step.

    WHEN THIS GOES RED the stager has landed (the runtime has a default workspace seam, or something now stages the
    bundle) and the pipeline has moved past its last known refusal. That is progress, not a regression: update this
    test to stage through the default seam and assert the files step now passes and the next refusal (if any) is the
    new marker. Do NOT skip, xfail or delete it: the red is how the next lane learns where the pipeline now ends."""
    p = pipe(ptd, monkeypatch, tmp_path)
    assert p.rt._workspace is None, "a default workspace seam is wired: the stager landed, update this marker"
    code, ans = p.render()
    assert (code, ans.get("refused")) == (H.EXIT_REFUSED, KNOWN_NEXT), (ans, p.w.last_err)
    assert p.steps == STEPS_REACHED
    assert not (p.w.L / "live" / p.session / "in" / stage.BUNDLE_NAME).exists()
