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

The workspace seam was unwired when this file was written (the stager was a later lane), so the pipeline ended on
``staging-missing`` at render step (3), and that end was pinned by a marker that was to go RED once the stager landed.
It did not: ``HelperSlotRuntime``'s own default for ``workspace`` stayed ``None`` (the server wires the seam through
``build_server``, not through the class default), so the marker kept passing while the production path had moved on.
The marker was therefore split in two, never skipped or deleted:

- :func:`test_the_production_wired_workspace_renders_every_step` builds the runtime through ``dispatch_run.build_server``
  (the seam production wires), uploads a real snapshot of a real git repo through the operator half
  (``stager.build_upload``) and the server half (``WorkspaceUploads`` begin / chunk / verify), and pins the whole
  render: every step of ``H.RENDER_STEPS``, the verified bundle in the slot's ``in/``, staging emptied.
- :func:`test_control_an_unwired_workspace_seam_still_ends_at_staging_missing` keeps the old end as a control: the
  same pipeline with no seam must still refuse by name, which shows the wired test's pass is the seam's doing.
"""
from __future__ import annotations

import hashlib
import os
import shutil

import pytest

from levain.agents import session_launcher as L
from levain.contracts import canon
from levain.dispatch import stager
from levain.dispatch.policy import ProviderPolicy
from levain.slot import control as C
from levain.slot import dispatch_runtime as DR
from levain.slot import render as RD
from levain.slot import runtime_helper as H
from levain.slot import stage
from levain.slot import tickets as T
from levain.slot import vendor_routes as VR
from levain.slot import workspace as WS
from levain.slot.services import dispatch_run as D
from levain.slot.supervisor import pick_routes
from levain.slot.ids import SlotId
from tests.test_agents_session_launch import EXP
from tests.test_agents_session_launcher import KEY, RUN, FakeControl, FakeRuntime, FakeSources, launch
from tests.test_slot_p7a1_passthrough_ticket import mark, ptd  # noqa: F401 - the ticketed passthrough world fixture
from tests.test_pool import F2_DISPATCH
from tests.test_dispatch_stager_send import member, op_repo

#: the refusal a pipeline with NO workspace seam ends on, and the render steps it reaches before it
UNWIRED_END = "staging-missing"
STEPS_REACHED = ["cas-rendering", "policy", "files"]
#: every render step, from the helper's own table: a step added or removed there changes what a complete render is
ALL_STEPS = [name for name, _fn in H.RENDER_STEPS]
#: the operator's NEVER list for the real-upload tests (the stager's policy argument; the signed store is not read)
POLICY = ProviderPolicy.from_mapping({"never": ["**/.env", "**/*.pem"], "providers": {}})
needs_git = pytest.mark.skipif(shutil.which("git") is None or shutil.which("ssh-keygen") is None,
                               reason="needs git and ssh-keygen")

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


def wired_runtime(w, tmp_path):
    """The runtime and uploads the dispatch run server builds in production (``build_server``), over this world's
    store: the seam is whatever production wires, not a stand-in. Returns ``(runtime, uploads)``."""
    srv = D.build_server("linux", w.L, tmp_path / "c.sock", os.getuid())
    return srv.launcher.runtime, srv._uploads


def upload_snapshot(uploads, tmp_path):
    """A real operator repo, snapshotted and bundled by the operator half, then through the server half's WSBEGIN /
    WSDATA / verify under ``KEY``. Returns ``(the verified Workspace member, the Upload the operator built)``."""
    ws = member(op_repo(tmp_path))
    up = stager.build_upload(ws, policy=POLICY)
    dg = lambda b: "sha256:" + hashlib.sha256(b).hexdigest()  # noqa: E731
    owner = WS.UploadOwner()
    uploads.begin(KEY, owner, {"manifest": len(up.manifest), "bundle": len(up.bundle)},
                  {"manifest": dg(up.manifest), "bundle": dg(up.bundle)})
    uploads.chunk(KEY, owner, "manifest", 0, up.manifest)
    uploads.chunk(KEY, owner, "bundle", 0, up.bundle)
    return uploads.verify(KEY, owner, ws), up


def pipe(ptd, monkeypatch, tmp_path, doc=None, wired=False):  # noqa: F811
    """Launcher facts -> real mint -> real prepare -> the runtime's default blob -> staged exactly as ``start`` stages
    (the brief, then the workspace seam only if the runtime has one). ``wired`` builds the runtime as production does
    and uploads a real snapshot first; the others use the class defaults, whose workspace seam is None."""
    d, w = ptd, ptd.w
    merged, _ids, _notes = VR.merge(doc)                      # setup's own merge; None = an absent map
    (w.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(merged))
    base = w.L / "routes" / "base.json"
    monkeypatch.setattr(DR.layout, "base_routes_path", lambda host: base)
    upload = verified = None
    if wired:
        rt, uploads = wired_runtime(w, tmp_path)
        verified, upload = upload_snapshot(uploads, tmp_path)
    else:
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
    req = L.SlotStart(ticket, RUN, KEY, "claude", lau.model, lau.brief, str(facts["brief_digest"]), L.HOOKS, verified)
    blob_bytes = rt._render_blob(req)
    staging = w.L / "staging" / session
    L.stage_brief_for(req, staging)                           # start()'s order: the brief ...
    if rt._workspace is not None:                             # ... then the workspace seam, when the runtime has one
        rt._workspace(req, staging)
    p = Pipe(d, w, rt, req, session, blob_bytes, ticket)
    p.upload = upload
    return p


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
    # the ConfigChange alarm and the repo hook filter go to every Claude session, whatever the blob names
    assert set(facts.hook_templates) == set(blob.hooks) | {"levain_config", "levain_repo_filter"}
    assert set(blob.hooks) == set(L.HOOKS)
    assert {n: t.event for n, t in facts.hook_templates.items() if n != "levain_repo_filter"} == {
        "levain_gate": "PreToolUse", "levain_queue": "PermissionRequest", "levain_config": "ConfigChange"}
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


def test_control_an_unwired_workspace_seam_still_ends_at_staging_missing(ptd, monkeypatch, tmp_path):  # noqa: F811
    """The old marker, kept as a control. A runtime built from the class defaults has no workspace seam, so nothing
    stages ``repo.bundle`` and render ends exactly at step (3) 'files', refused ``staging-missing``: past the ticket
    gate, past ``render_facts`` -> ``check_facts``, past step (1)'s CAS and step (2)'s policy, and short of every later
    step. It exists so the wired test below cannot pass for any reason but the seam: with the seam removed the same
    pipeline must end here, by name."""
    p = pipe(ptd, monkeypatch, tmp_path)
    assert p.rt._workspace is None                            # the class default: nothing wired
    code, ans = p.render()
    assert (code, ans.get("refused")) == (H.EXIT_REFUSED, UNWIRED_END), (ans, p.w.last_err)
    assert p.steps == STEPS_REACHED
    assert not (p.w.L / "live" / p.session / "in" / stage.BUNDLE_NAME).exists()


@needs_git
def test_the_production_wired_workspace_renders_every_step(ptd, monkeypatch, tmp_path):  # noqa: F811
    """The pipeline's end once the stager was wired through ``build_server``. The first marker was to go red when the
    stager landed and be updated to the NEW first refusal; measured on the merged tree there is none: a real snapshot
    uploaded through both halves, staged by the server's own seam, passes ticket gate, ``check_facts`` and every render
    step. The pin is the whole outcome: ``ok`` after every step of ``H.RENDER_STEPS``, the bytes the operator built
    sitting in the slot's ``in/`` (the root-side copy), the staging file consumed, and the base the header named
    recorded on the alloc from those same bytes. The next refusal this pipeline can meet is past ``render``
    (``start``, in the VM), which nothing here drives."""
    p = pipe(ptd, monkeypatch, tmp_path, wired=True)
    assert p.rt._workspace is not None, "the production runtime has no workspace seam"
    staging_file = p.w.L / "staging" / p.session / stage.BUNDLE_NAME
    assert staging_file.read_bytes() == p.upload.bundle       # the server's seam staged what the operator built
    code, ans = p.render()
    assert (code, ans) == (0, {"ok": True}), (ans, p.w.last_err)
    assert p.steps == ALL_STEPS
    assert (p.w.L / "live" / p.session / "in" / stage.BUNDLE_NAME).read_bytes() == p.upload.bundle
    assert not staging_file.exists()                          # step (3) emptied staging after the copy was durable
    base = p.w.alloc(p.session).base
    assert base is not None and base.bundle_bytes == len(p.upload.bundle)
    assert base.bundle_sha256 == "sha256:" + hashlib.sha256(p.upload.bundle).hexdigest()
