"""S1 3c-8: verifier generations (CONFIG-SUBMIT), the verifier ticket resolved at mint and re-checked at ATTACH, and
the 3c-7 carried list (1025-10/3c7/HEAD_RULINGS.md): render's facts gate, a post-CAS render refusal, the start wake.

CONTROL runs through `Control.handle` on test_tickets' rig (a real signed route map and, here, a real signed verifier
item, real `ssh-keygen` keys, the ticket journal on disk). The render path runs the real runtime helper `main` on lane
F2's world (test_pool's `dispatch` fixture). What only a VM run can show (root's real uids, the installed units) is
3c-10's."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import tickets as T
from tests.test_pool import F2_DISPATCH, dispatch  # noqa: F401 - fixture
from tests.test_slot_3c7_fix_r1 import F2_FACTS, VER_FACTS, _blob, _mint, _prepared, _render
from tests.test_slot_3c7_fix_r1 import pinning_stand_in  # noqa: F401 - fixture


# ---- carried (b) + (c): render's facts gate never compares against an absent field --------------------------------

def _gate(facts: dict, kind: str, cls: str, blob_obj: dict) -> str | None:
    from levain.slot import runtime_grammar as G

    blob = G.parse_stdin("render", canon.canonical_bytes(blob_obj))
    try:
        RD.check_ticket(blob, kind, cls, C.new_handle(), facts)
    except Refused as e:
        return e.name
    return None


@pytest.mark.parametrize("missing", ["route", "model_requested"])
def test_executor_facts_without_a_required_field_refuse(missing):
    """deepseek LOW 4: an executor ticket's facts with no `model_requested` let a model-less blob pass the model gate
    (`None == None`); with no `route` the routes gate compared against the egress routes alone."""
    facts = {k: v for k, v in T.check_facts(F2_FACTS).items() if k != missing}
    blob = {"format": "levain-session-render/1", "vendor": "claude",
            "routes": ["github"] if missing == "route" else ["anthropic", "github"]}
    assert _gate(facts, "dispatch", "egress", blob) == "ticket-facts-missing"


def test_verifier_facts_without_steps_refuse_by_name():
    """deepseek MED 2: a verifier ticket's facts with no `verifier_steps` met the blob's steps as b"[]" vs b"null"
    (`ticket-steps-mismatch`, whatever the blob held); it is the facts that are short, so `ticket-facts-missing`."""
    from tests.test_slot_3c7_fix_r1 import STEPS, _verifier_resolved

    facts = {k: v for k, v in T.check_facts(VER_FACTS).items() if k != "verifier_steps"}
    facts["verifier"] = _verifier_resolved()
    blob = {"format": "levain-session-render/1", "vendor": "claude", "routes": [], "verifier_steps": STEPS,
            "driver": "pytest_report"}
    assert _gate(facts, "verifier", "none", blob) == "ticket-facts-missing"


# ---- carried (a): no render refusal leaves the session RENDERING ----------------------------------------------------

@pytest.mark.usefixtures("pinning_stand_in")
def test_render_policy_refusal_comes_before_the_cas(dispatch):
    """1025-10 obs 2: step (2)'s checks (here the hook template the blob names and the world does not hold) change
    with nothing the CAS writes, so they refuse before step (1)'s CAS: the session stays ALLOCATED."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    calls = len(w.calls)
    line = _render(d, s, _blob(hooks=["levain_queue"]))
    assert line["refused"] == "hook-template-missing", line
    assert w.calls[calls:] == [] and w.state(s).state == R.ALLOCATED


@pytest.mark.usefixtures("pinning_stand_in")
@pytest.mark.parametrize("step", ["policy", "files", "front", "mint"])
def test_render_refused_after_its_cas_revokes_the_session(dispatch, step):
    """A refusal after step (1)'s CAS answered: root REVOKEs the session (a state the reaper's END clears), never
    leaves it RENDERING, which no later render can take up. The caller is answered the render's own refusal."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)

    def boom(at):
        if at == step:
            raise Refused("io-error", "injected")

    w.on_step = boom
    line = _render(d, s, _blob())
    w.on_step = None
    assert line["refused"] == "io-error", (line, w.last_err)
    assert [op for op, _ in w.calls][-1] == "REVOKE"
    assert w.state(s).state == R.REVOKED


@pytest.mark.usefixtures("pinning_stand_in")
def test_render_refused_at_its_first_cas_revokes_nothing(dispatch):
    """A step (1) CAS CONTROL refused moved nothing: no REVOKE is sent and the record is what it was."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    ans = w.control.handle(C.Conn(0), {"v": 1, "op": "REVOKE", "id": 1, "session": s})   # another writer moved it
    assert ans["ok"], ans
    rec = w.state(s)
    line = _render(d, s, _blob())
    assert line["refused"] == "stale-generation", line
    assert "REVOKE" not in [op for op, _ in w.calls] and w.state(s) == rec


class _LossyClients:
    """The real ``ControlClient``'s loss shape over the test CONTROL: a call named in ``drop`` is committed by CONTROL,
    then its reply is lost, so the client closes itself and raises ``control-down``; every later call on that client
    is ``control-down`` too (``ControlClient._exchange`` on a closed socket). A new client from the factory works."""

    def __init__(self, world, drop: set[str]) -> None:
        from tests.test_slot_s2f2_runtime_helper import Client

        self.world, self.drop, self.made, self._base = world, set(drop), 0, Client

    def __call__(self, path):
        self.made += 1
        clients, base = self, self._base(self.world)

        class Lossy:
            closed = False

            def call(self, op, **fields):
                if self.closed:
                    clients.world.calls.append((op + " (closed)", fields))
                    raise Refused("control-down", "closed")
                ans = base.call(op, **fields)
                if op in clients.drop:
                    clients.drop.discard(op)
                    self.closed = True
                    raise Refused("control-down", f"{op}: EOF")
                return ans

            def close(self):
                self.closed = True

        return Lossy()


def _lossy(w, drop: set[str]) -> _LossyClients:
    import dataclasses

    clients = _LossyClients(w, drop)
    w.seams = dataclasses.replace(w.seams, client_factory=clients)
    return clients


@pytest.mark.usefixtures("pinning_stand_in")
@pytest.mark.parametrize("lost", ["CAS", "MINT"])
def test_render_whose_reply_was_lost_revokes_on_a_new_connection(dispatch, lost):
    """codex HIGH r1: CONTROL commits a render's CAS (step 1) or MINT (step 5) and the reply is lost. The cached
    client is closed, and at step 1 the generation was never answered, so a REVOKE gated on it, or sent on the cached
    client, never reaches CONTROL: the session stays RENDERING, ticket and slot held. The REVOKE goes through a NEW
    connection whenever the first CAS may have landed."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    clients = _lossy(w, {lost})
    made = clients.made
    line = _render(d, s, _blob())
    assert line["refused"] == "control-down", (line, w.last_err)
    assert [op for op, _ in w.calls][-1] == "REVOKE", w.calls
    assert clients.made == made + 2                         # the render's client, then the REVOKE's own
    assert w.state(s).state == R.REVOKED


@pytest.mark.usefixtures("pinning_stand_in")
def test_render_revokes_when_killing_its_front_raises(dispatch):
    """codex HIGH r1: a front cleanup that raises cannot skip the REVOKE (it runs in a finally)."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)

    def boom(at):
        if at == "mint":
            raise Refused("io-error", "injected")

    killed = []

    def kill(pid):
        killed.append(pid)
        raise OSError("the kill failed")

    import dataclasses

    w.seams = dataclasses.replace(w.seams, kill=kill)
    w.on_step = boom
    line = _render(d, s, _blob())
    w.on_step = None
    assert not line["ok"] and killed, line
    assert [op for op, _ in w.calls][-1] == "REVOKE"
    assert w.state(s).state == R.REVOKED


# ---- carried (c): CONTROL's start wake after the listener ----------------------------------------------------------

def test_server_start_survives_a_wake_that_raises():
    """deepseek LOW 3: `ControlServer.start` wakes the reaper after its accept thread runs. `Control._wake` records a
    raising seam in `seam_errors` and returns, so start returns with the server serving (nothing is left half-up)."""
    d = Path(tempfile.mkdtemp(prefix="lv3c8", dir="/tmp"))
    (d / "sessions").mkdir(mode=0o700)

    def wake() -> None:
        raise OSError("the reaper's wake socket is gone")

    control = C.Control(C.StateStore(d / "sessions"), os.getuid(), wake_reaper=wake)
    server = C.ControlServer(control, d / "c.sock")
    try:
        server.start()
        assert [n for n, _s, _e in control.seam_errors] == ["wake_reaper"]
        import socket

        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(str(d / "c.sock"))                                 # listening and accepting
        finally:
            c.close()
    finally:
        server.stop()
        shutil.rmtree(d, ignore_errors=True)


# ---- CONFIG-SUBMIT: verifier generations through the identity rule ------------------------------------------------

from levain.slot import routemaps as RM  # noqa: E402
from tests.test_routemaps import STAMP, STORE_ID, Signer, Store  # noqa: E402,F401
from tests.test_routemaps import st  # noqa: E402,F401 - the signed store fixture
from tests.test_tickets import (DISPATCH_UID, KEYS_UID, OP_UID, Clock, Rig, d_attach_claims, lease,  # noqa: E402
                                refused, root, sync_free, ticket_of)

VSTEPS = [{"argv": ["pytest", "-q"], "wall_s": 60}]
SESSION_V = "s-00000000000000a1"


def ventry(vid: str = "pytest-report", *, steps=None, driver_input=None, template: str = "output_compare",
           artifact: str = "5") -> dict:
    steps = VSTEPS if steps is None else steps
    driver_input = {"steps": [{"exit": 0}] * len(steps)} if driver_input is None else driver_input
    return {"verifier_id": vid, "verifier_artifact_digest": "sha256:" + artifact * 64, "declared_kind": "black-box",
            "driver_template": template, "driver_template_digest": "sha256:" + "6" * 64, "steps": steps,
            "steps_digest": RM.sha256_canonical(steps), "driver_input": driver_input,
            "driver_input_digest": RM.sha256_canonical(driver_input)}


def vitem(gen: int, entries=None, *, store_id: str = STORE_ID) -> dict:
    return {"format": RM.CONFIG_FORMAT, "store_id": store_id, "generation": gen,
            "prev": "" if gen == 1 else "sha256:" + "0" * 64, "issued_at": STAMP, "kind": RM.VERIFIER_KIND,
            "body": {"format": RM.VERIFIER_FORMAT, "verifiers": [ventry()] if entries is None else entries}}


class VRig(Rig):
    """test_tickets' rig (route map generation 1, keys attached, free slots synced) with CONTROL's verifier store."""

    def restart(self, *, fault=None, attach: bool = True) -> None:
        if self.control is not None:
            self.control.stop()
        self.tickets = T.TicketStore(self.sdir / T.JOURNAL, self.st.L / "policy" / "dispatch_ceiling",
                                     clock=self.clock, ceiling_uid=os.getuid())
        self.cr = RM.ControlRoutes(self.st.routes)
        self.cv = RM.ControlVerifiers(self.st.routes)
        self.control = C.Control(C.StateStore(self.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                                 routes=self.cr, tickets=self.tickets, clock=self.clock, verifiers=self.cv)
        self.control.start()
        sync_free(self.control)
        if attach:
            self.attach(self.cr.head())

    def submit_v(self, it: dict, signer=None) -> dict:
        it, sig = self.st.signed(it, signer)
        return self.ask("CONFIG-SUBMIT", item=it, sig=sig)

    def vmint(self, attempt: str = "v-1", **kw) -> dict:
        args = {"attempt_id": attempt, "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
                "kind": "verifier", "egress_routes": [], "reserved_microdollars": 0, "verifier_generation": 1,
                "verifier_id": "pytest-report", "verifier_steps": VSTEPS, "input_bundle_digest": "sha256:" + "2" * 64,
                "driver_input_digest": ventry()["driver_input_digest"]}
        args.update(kw)
        return self.ask("RUN-TICKET-MINT", **{k: v for k, v in args.items() if v is not None})

    def vattach(self, ticket: str, session: str = SESSION_V) -> dict:
        _conn, lo = lease(self, ticket)
        return root(self, "ATTACH", lease_nonce=lo["lease_nonce"], sudo_uid=DISPATCH_UID, session=session,
                    claims=d_attach_claims(ticket, slot={"class": "none", "index": 0}, kind="verifier",
                                           auth="none", **{"class": "none"}))




@pytest.fixture
def vrig(st):  # noqa: F811
    return VRig(st, Clock())


def test_verifier_formats_are_dispatchs():
    """CONTROL's verifier body is the one dispatch signs (`levain.dispatch.registry`), and the resolved object is
    what alloc parses (`records.TICKET_VERIFIER_KEYS`) and the ticket journal checks."""
    from levain.dispatch import registry

    assert RM.VERIFIER_ENTRY_KEYS == registry.VERIFIER_KEYS and RM.VERIFIER_FORMAT == registry.VERIFIER_FORMAT
    assert RM.VERIFIER_KIND in registry.KINDS
    assert R.TICKET_VERIFIER_KEYS == T.RESOLVED_KEYS == {"verifier_generation", *RM.RESOLVED_ENTRY_KEYS}
    assert (RM.VERIFIER_STEPS_MAX, RM.DRIVER_INPUT_MAX) == (registry.MAX_STEPS_BYTES, registry.MAX_DRIVER_INPUT_BYTES)


def test_an_entry_dispatch_accepts_control_accepts(vrig):
    """Derived, not invented: an entry dispatch's own accept rule takes (the shipped template's real digest, the
    artifact digest by dispatch's formula) is activated by CONFIG-SUBMIT."""
    from levain.dispatch import registry

    e = ventry()
    e["driver_template_digest"] = registry.driver_template_digest("output_compare")
    e["verifier_artifact_digest"] = registry.verifier_artifact_digest(e)
    body = {"format": RM.VERIFIER_FORMAT, "verifiers": [e]}
    assert registry.body_refusal("verifier", body, {}) is None
    assert vrig.submit_v(vitem(1, [e])) == {"id": 1, "ok": True, "generation": 1}


@pytest.mark.parametrize("change,name", [
    (lambda e: e.update(verifier_id="Bad_Id"), "config-invalid:verifier_id"),
    (lambda e: e.update(driver_template="Bad-Template"), "config-invalid:driver_template"),
    (lambda e: e.update(steps_digest="sha256:" + "0" * 64), "config-invalid:steps_digest"),
    (lambda e: e.update(driver_input_digest="sha256:" + "0" * 64), "config-invalid:driver_input_digest"),
    (lambda e: e.update(steps=[]), "config-invalid:steps"),
    (lambda e: e.update(declared_kind="white-box"), "config-invalid:declared_kind"),
    (lambda e: e.pop("driver_input"), "config-invalid:entry"),
])
def test_config_submit_refuses_a_bad_entry(vrig, change, name):
    e = ventry()
    change(e)
    assert refused(vrig.submit_v(vitem(1, [e]))) == name
    assert vrig.cv.active() is None


def test_config_submit_refuses_a_duplicate_id_and_another_kind(vrig):
    assert refused(vrig.submit_v(vitem(1, [ventry(), ventry()]))) == "config-invalid:verifier_id"
    it = vitem(1)
    it["kind"] = "routemap"
    assert refused(vrig.submit_v(it)) == "config-invalid:kind"


def test_config_submit_identity_rule(vrig, st):  # noqa: F811
    """The route map's identity rule, for verifiers: a lower generation, or an equal one with other content, refused
    against CONTROL's head (even with the active file gone); gaps accepted; another store's item and another key's
    signature refused; an equal resubmission is the convergent repair."""
    assert vrig.submit_v(vitem(3))["generation"] == 3
    assert refused(vrig.submit_v(vitem(2))) == "config-generation"
    assert refused(vrig.submit_v(vitem(3, [ventry(artifact="9")]))) == "config-generation"
    (st.routes / "verifiers" / "3.json").unlink()                        # the active file pruned or lost
    assert refused(vrig.submit_v(vitem(2))) == "config-generation"
    assert vrig.submit_v(vitem(3))["generation"] == 3                    # the same item again: written again
    assert (st.routes / "verifiers" / "3.json").exists()
    assert refused(vrig.submit_v(vitem(9, store_id="cd" * 16))) == "config-store"
    other = Signer(st.L, "intruder")
    assert refused(vrig.submit_v(vitem(9), signer=other)) == "config-signature"
    assert vrig.submit_v(vitem(7))["generation"] == 7
    head = canon.parse((st.routes / "verifiers" / "head").read_bytes())
    assert head["generation"] == 7 and int((st.routes / "verifiers" / "current").read_bytes()) == 7


def test_config_submit_only_from_dispatch(vrig):
    it, sig = vrig.st.signed(vitem(1))
    for uid in (0, OP_UID, KEYS_UID):
        ans = vrig.control.handle(C.Conn(uid), {"v": 1, "op": "CONFIG-SUBMIT", "id": 1, "item": it, "sig": sig})
        assert refused(ans) == "verb-not-allowed-for-peer"


def test_verifier_generation_survives_restart_and_a_torn_activation(vrig, st):  # noqa: F811
    """CONTROL's start pass, for verifiers: the head re-verified and served; `current` repaired; files newer than the
    head deleted; a head whose files do not verify leaves no generation active (every verifier mint refused)."""
    vrig.submit_v(vitem(2))
    vd = st.routes / "verifiers"
    (vd / "current").write_bytes(b"1")
    (vd / "5.json").write_bytes(b"{}")
    vrig.restart()
    assert vrig.cv.active().generation == 2 and (vd / "current").read_bytes() == b"2"
    assert not (vd / "5.json").exists()
    (vd / "2.json").write_bytes((vd / "2.json").read_bytes()[:-1])
    vrig.restart()
    assert vrig.cv.active() is None and vrig.cv.problem == "verifier-head-unverifiable"
    assert refused(vrig.vmint(verifier_generation=2)) == "verifier-unknown"


# ---- the verifier ticket: resolved at mint, re-checked at ATTACH ------------------------------------------------

def test_verifier_resolved_not_minted(vrig):
    """T:3c/test_verifier_resolved_not_minted. The ticket names only the generation and the id; the entry its record
    carries (and ATTACH answers in `ticket_facts.verifier`) is CONTROL's own projection of the ACTIVE entry, never a
    request field: a mint naming a resolved field is refused by the verb's key set, and steps or a driver input
    other than the active entry's are refused by name."""
    vrig.submit_v(vitem(1))
    assert refused(vrig.ask("RUN-TICKET-MINT", **{**_vargs(), "verifier": {"driver_template": "evil"}})) \
        == "unknown-key"
    assert refused(vrig.vmint(verifier_steps=[{"argv": ["sh", "-c", "true"], "wall_s": 60}])) \
        == "ticket-field:verifier_steps"
    assert refused(vrig.vmint(driver_input_digest="sha256:" + "8" * 64)) == "ticket-field:driver_input_digest"
    assert refused(vrig.vmint(verifier_id="not-registered")) == "verifier-unknown"
    ticket = ticket_of(vrig.vmint())
    want = {"verifier_generation": 1, **{k: ventry()[k] for k in RM.RESOLVED_ENTRY_KEYS}}
    (line,) = [x for x in vrig.lines() if x["op"] == "mint"]
    assert line["verifier"] == want
    att = vrig.vattach(ticket)
    assert att["ok"], att
    assert att["ticket_facts"]["verifier"] == want
    assert att["ticket_facts"]["verifier_steps"] == VSTEPS
    vrig.restart()                                                       # the journal rebuilds the resolved entry
    assert vrig.tickets.bound(SESSION_V).resolved == want


def _vargs(**kw) -> dict:
    args = {"attempt_id": "v-9", "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
            "kind": "verifier", "egress_routes": [], "reserved_microdollars": 0, "verifier_generation": 1,
            "verifier_id": "pytest-report", "verifier_steps": VSTEPS, "input_bundle_digest": "sha256:" + "2" * 64,
            "driver_input_digest": ventry()["driver_input_digest"]}
    args.update(kw)
    return args


def test_superseded_verifier_generation_refused(vrig):
    """T:3c/test_superseded_verifier_generation_refused: a ticket names the ACTIVE generation at mint, never a
    superseded one, so a hijacked dispatch cannot pick an older signed driver the operator replaced. A replay of an
    attempt minted before the supersession still answers its ticket (idempotent mint)."""
    vrig.submit_v(vitem(1))
    first = vrig.vmint("v-1")
    vrig.submit_v(vitem(2, [ventry(artifact="9")]))
    assert refused(vrig.vmint("v-2", verifier_generation=1)) == "ticket-field:verifier_generation"
    assert vrig.vmint("v-1") == first
    assert ticket_of(vrig.vmint("v-3", verifier_generation=2))


def test_withdrawn_verifier_refused_at_attach(vrig):
    """T:3c/test_withdrawn_verifier_refused_at_attach: a ticket minted while its id was active, whose id a later
    generation removed, is refused `verifier-unknown` at ATTACH (withdrawal stops it, as for routes); nothing binds."""
    vrig.submit_v(vitem(1, [ventry(), ventry("other")]))
    ticket = ticket_of(vrig.vmint())
    vrig.submit_v(vitem(2, [ventry("other")]))
    assert refused(vrig.vattach(ticket)) == "verifier-unknown"
    assert vrig.tickets.bound(SESSION_V) is None and vrig.control.store.get(SESSION_V) is None
    assert vrig.control.pool.entry("none", 0).state == "reusable"        # the mirror's mark rolled back


def test_replaced_verifier_refused_at_attach(vrig):
    """T:3c/test_replaced_verifier_refused_at_attach: the same id with another entry in the active generation is
    `verifier-superseded` at ATTACH (a verifier has no network to keep pinned, so a replaced entry stops pre-minted
    tickets too). A later generation that re-signs the SAME entry does not."""
    vrig.submit_v(vitem(1))
    stale = ticket_of(vrig.vmint("v-1"))
    same = ticket_of(vrig.vmint("v-2"))
    vrig.submit_v(vitem(2))                                              # the same entry, a new generation
    assert vrig.vattach(same)["ok"]
    vrig.submit_v(vitem(3, [ventry(artifact="9")]))                      # replaced: another artifact under the id
    assert refused(vrig.vattach(stale, "s-00000000000000a2")) == "verifier-superseded"


def test_verifier_mint_without_a_store_or_a_generation(st):  # noqa: F811
    """No verifier store, or none active: every verifier mint is `verifier-unknown` (fail closed), CONFIG-SUBMIT with
    no store `config-verifier`."""
    rig = Rig(st, Clock())
    assert refused(rig.ask("RUN-TICKET-MINT", **_vargs())) == "verifier-unknown"
    it, sig = st.signed(vitem(1))
    assert refused(rig.ask("CONFIG-SUBMIT", item=it, sig=sig)) == "config-verifier"
    rig.control.stop()
    v = VRig(st, Clock())
    assert refused(v.vmint()) == "verifier-unknown"


def test_verifier_generations_pruned_by_the_route_rule(vrig, st):  # noqa: F811
    """CONTRACT §3.5: a verifier generation's files go once a newer one is active and no OPEN ticket names it."""
    vrig.submit_v(vitem(1))
    t1 = ticket_of(vrig.vmint("v-1"))
    vrig.submit_v(vitem(2))
    names = sorted(os.listdir(st.routes / "verifiers"))
    assert "1.json" in names and "2.json" in names                       # an open ticket names 1
    assert vrig.ask("RUN-TICKET-CANCEL", ticket=t1)["ok"]
    vrig.submit_v(vitem(3))
    names = sorted(os.listdir(st.routes / "verifiers"))
    assert "1.json" not in names and "2.json" not in names and "3.json" in names


def test_verifier_journal_line_tampered_refuses_start(vrig):
    """The rebuild checks a verifier mint line's resolved entry against the line's own facts: an entry edited on disk
    (another driver) refuses the start `journal-inconsistent`, as a tampered fact does."""
    vrig.submit_v(vitem(1))
    ticket_of(vrig.vmint())
    path = vrig.sdir / T.JOURNAL
    lines = [canon.parse(x) for x in path.read_bytes().splitlines()]
    for x in lines:
        if x["op"] == "mint":
            x["verifier"]["steps_digest"] = "sha256:" + "0" * 64
    path.write_bytes(b"".join(canon.canonical_bytes(x) + b"\n" for x in lines))
    with pytest.raises(Refused) as e:
        vrig.restart()
    assert e.value.name == "journal-inconsistent"
    vrig.control = None


def test_store_mint_requires_the_resolved_entry_for_a_verifier(vrig):
    facts = T.check_facts({k: v for k, v in _vargs().items() if k != "attempt_id"})
    with pytest.raises(Refused) as e:
        vrig.tickets.mint("v-5", facts, C.new_handle)
    assert e.value.name == "ticket-field:verifier"
    exec_facts = T.check_facts({k: v for k, v in __import__("tests.test_tickets", fromlist=["mint_args"])
                                .mint_args("x").items() if k != "attempt_id"})
    with pytest.raises(Refused) as e:
        vrig.tickets.mint("v-6", exec_facts, C.new_handle, resolved={"verifier_generation": 1})
    assert e.value.name == "ticket-field:verifier"


# ---- (e): a verifier ticket renders through the real resolved entry ---------------------------------------------

def _shipped_entry(**kw) -> dict:
    from levain.dispatch import registry

    e = ventry()
    e["driver_template_digest"] = registry.driver_template_digest("output_compare")
    e.update(kw)
    return e


def _verifier_session(d, entry: dict) -> str:
    """A verifier ticket minted by RUN-TICKET-MINT against an active generation holding `entry` (the stand-in store
    of test_slot_3c7_fix_r1, so no signer is needed in lane F2's world), prepared, its driver input staged."""
    from tests.test_slot_3c7_fix_r1 import Verifiers

    v = Verifiers()
    v.gen = RM.CheckedVerifiers(1, "0" * 64, "ab" * 16, {entry["verifier_id"]: entry})
    d.w.control.verifiers = v
    sync_free(d.w.control)
    ans = d.w.control.handle(C.Conn(F2_DISPATCH), {"v": 1, "op": "RUN-TICKET-MINT", "id": 1, **_vargs(
        attempt_id=f"v-{d.n}", driver_input_digest=entry["driver_input_digest"])})
    d.n += 1
    line = d.prepare(ticket_of(ans), "none", "verifier", "claude", "none")
    assert line["ok"], (line, d.w.last_err)
    d.w.stage(line["session"])
    return line["session"]


def _vblob(**kw) -> bytes:
    obj = {"format": "levain-session-render/1", "vendor": "claude", "routes": [], "verifier_steps": VSTEPS,
           "driver": "output_compare"}
    obj.update(kw)
    return canon.canonical_bytes(obj)


def test_verifier_ticket_renders_through_its_resolved_entry(dispatch):
    """deepseek HIGH 1 (3c-7, by design until 3c-8): RUN-TICKET-MINT -> prepare -> render, end to end. The resolved
    entry reaches alloc through ATTACH's `ticket_facts`, the gate passes it, the installed template hashes to the
    entry's digest, and the render completes (its first act after the gate is the CAS)."""
    d, w = dispatch, dispatch.w
    s = _verifier_session(d, _shipped_entry())
    assert w.alloc(s).ticket_facts["verifier"]["driver_template"] == "output_compare"
    calls = len(w.calls)
    line = _render(d, s, _vblob())
    assert w.calls[calls][0] == "CAS", (line, w.last_err)
    assert line["ok"], (line, w.last_err)


def test_verifier_render_refuses_a_template_off_its_digest(dispatch):
    """CONTRACT §3.5 `driver-template-digest-mismatch`: an entry pinning another digest than the installed template's
    (a levain upgrade changed it) refuses before the CAS; so does a template this levain does not ship."""
    d, w = dispatch, dispatch.w
    s = _verifier_session(d, _shipped_entry(driver_template_digest="sha256:" + "6" * 64))
    calls = len(w.calls)
    assert _render(d, s, _vblob())["refused"] == "driver-template-digest-mismatch"
    assert w.calls[calls:] == [] and w.state(s).state == R.ALLOCATED
    assert RD.read_driver_template("no_such_template") is None and RD.read_driver_template("__init__") is None
    assert RD.read_driver_template("output_compare") is not None


# ---- 3c-8 r1 fixes 2-6 -----------------------------------------------------------------------------------------

def _sized_item(gen: int, size: int) -> dict:
    """A verifier item whose canonical bytes are exactly `size`: one entry, its driver_input padded by an
    output_compare `stdout_equals` expectation (an input dispatch's own reader takes)."""
    def make(n: int) -> dict:
        return vitem(gen, [ventry(driver_input={"steps": [{"exit": 0, "stdout_equals": "x" * n}]})])

    it = make(size - len(canon.canonical_bytes(make(0))))
    assert len(canon.canonical_bytes(it)) == size
    return it


def test_config_submit_refuses_an_item_no_frame_carries(vrig):
    """codex MED r1: CONTROL's own validation took a verifier item of up to 1 MiB (256 KiB of driver_input), which no
    65,536-byte CONTROL frame can carry; here codex's 70,000-byte item, judged without the frame, refuses by name."""
    assert refused(vrig.submit_v(_sized_item(1, 70_000))) == "config-invalid:item"
    assert vrig.cv.active() is None


def test_the_largest_verifier_item_fits_one_frame_with_the_largest_signature():
    """The bound is derived from the frame cap: the largest item with a signature at `VERIFIER_SIG_MAX` as the frame
    carries it, at the widest request id, is exactly one frame; a byte more in either is over it."""
    from levain.contracts.grammar import JSON_INT_MAX

    item = _sized_item(1, RM.VERIFIER_ITEM_MAX_BYTES)
    sig = "A" * (RM.VERIFIER_SIG_MAX - 2)                  # its canonical JSON string is the quotes plus these
    frame = C.encode_frame({"v": 1, "op": "CONFIG-SUBMIT", "id": JSON_INT_MAX, "item": item, "sig": sig})
    assert len(frame) - 4 == C.FRAME_MAX
    with pytest.raises(C.FrameTooLarge):
        C.encode_frame({"v": 1, "op": "CONFIG-SUBMIT", "id": JSON_INT_MAX, "item": item, "sig": sig + "A"})
    from levain.dispatch import registry

    from levain.slot import stage

    assert registry.MAX_DRIVER_INPUT_BYTES == RM.DRIVER_INPUT_MAX <= RM.VERIFIER_ITEM_MAX_BYTES
    assert RM.DRIVER_INPUT_MAX < stage.DRIVER_INPUT_CAP                  # V25 bounds what a run reads; this is below it


def test_config_submit_boundary_through_the_real_framed_client(vrig):
    """codex MED r1, through the real `ControlClient` to a real `ControlServer`: the largest verifier item CONTROL
    accepts is activated; one byte over is refused `config-invalid:item` by CONTROL's validation, never
    `frame-too-large` (a real ed25519 signature leaves the frame room for it)."""
    from levain.slot.control_client import ControlClient

    vrig.control.stop()
    vrig.control = None
    d = Path(tempfile.mkdtemp(prefix="lv3c8f", dir="/tmp"))
    cv = RM.ControlVerifiers(vrig.st.routes)
    control = C.Control(C.StateStore(vrig.sdir), OP_UID, dispatch_uid=DISPATCH_UID, keys_uid=KEYS_UID,
                        routes=RM.ControlRoutes(vrig.st.routes), tickets=vrig.tickets, clock=vrig.clock, verifiers=cv)
    server = C.ControlServer(control, d / "c.sock", peer_uid=lambda s: DISPATCH_UID)
    try:
        server.start()
        with ControlClient(d / "c.sock") as client:
            it, sig = vrig.st.signed(_sized_item(1, RM.VERIFIER_ITEM_MAX_BYTES))
            assert client.call("CONFIG-SUBMIT", item=it, sig=sig) == {"generation": 1}
            it, sig = vrig.st.signed(_sized_item(2, RM.VERIFIER_ITEM_MAX_BYTES + 1))
            with pytest.raises(Refused) as e:
                client.call("CONFIG-SUBMIT", item=it, sig=sig)
            assert e.value.name == "config-invalid:item"
        assert cv.active().generation == 1
    finally:
        server.stop()
        control.stop()
        shutil.rmtree(d, ignore_errors=True)


def test_dispatch_refuses_a_verifier_item_control_cannot_be_handed():
    """codex MED r1, dispatch's half: its accept judges the verifier item by S1's frame bounds, so it never stores a
    verifier configuration no CONFIG-SUBMIT can carry."""
    from levain.dispatch import registry

    raw = canon.canonical_bytes(_sized_item(1, RM.VERIFIER_ITEM_MAX_BYTES))
    sig = "A" * (RM.VERIFIER_SIG_MAX - 2)
    assert registry.verifier_item_refusal(raw, sig) is None
    assert "CONFIG-SUBMIT" in registry.verifier_item_refusal(canon.canonical_bytes(
        _sized_item(1, RM.VERIFIER_ITEM_MAX_BYTES + 1)), sig)
    assert registry.verifier_item_refusal(raw, sig + "A") is not None
    assert registry.verifier_item_refusal(raw, "é") is not None


def test_a_failing_current_write_leaves_mints_on_the_new_head(vrig):
    """complement LOW 2 r1: the head is the authority. A `current` write that fails after the head is durable still
    publishes the new generation (mints resolve against it), and the start pass repairs `current`."""
    assert vrig.submit_v(vitem(1))["ok"]
    real = vrig.cv._write

    def write(path, data):
        if path.name == RM.CURRENT:
            raise OSError(28, "No space left on device")
        real(path, data)

    vrig.cv._write = write
    ans = vrig.submit_v(vitem(2, [ventry(artifact="8")]))
    vrig.cv._write = real
    assert ans == {"id": 1, "ok": True, "generation": 2}
    assert vrig.cv.active().generation == 2
    assert (vrig.cv.dir / RM.CURRENT).read_bytes() == b"1"
    assert refused(vrig.vmint(verifier_generation=1)) == "ticket-field:verifier_generation"
    assert vrig.vmint("v-2", verifier_generation=2)["ok"]
    vrig.restart()
    assert (vrig.cv.dir / RM.CURRENT).read_bytes() == b"2"


def test_config_submit_refuses_a_non_ascii_signature_by_name(vrig):
    """deepseek LOW 2 r1: a signature string that is not ASCII is `config-invalid:item`, never an uncaught
    UnicodeEncodeError (here one that a stand-in verifier would accept, so only the encode could catch it)."""
    vrig.cv._verify = lambda *a, **k: True
    ans = vrig.ask("CONFIG-SUBMIT", item=vitem(1), sig="é" * 8)
    assert refused(ans) == "config-invalid:item", ans
    assert vrig.cv.active() is None


def _resolved_for(facts: dict) -> dict:
    return {"verifier_generation": 1, "verifier_id": "pytest-report", "verifier_artifact_digest": "sha256:" + "5" * 64,
            "declared_kind": "black-box", "driver_template": "output_compare",
            "driver_template_digest": "sha256:" + "6" * 64, "steps_digest": RM.sha256_canonical(facts["verifier_steps"]),
            "driver_input_digest": facts["driver_input_digest"]}


@pytest.mark.parametrize("change", [
    {"verifier_generation": True},                 # complement LOW 3: `True == 1`
    {"driver_template": "_private"},               # complement LOW 3: CONFIG-SUBMIT and read_driver_template refuse it
])
def test_check_resolved_refuses_what_config_submit_refuses(change):
    facts = T.check_facts({k: v for k, v in _vargs().items() if k != "attempt_id"})
    assert T.check_resolved(facts, _resolved_for(facts))
    with pytest.raises(Refused) as e:
        T.check_resolved(facts, dict(_resolved_for(facts), **change))
    assert e.value.name == "ticket-field:verifier"


@pytest.mark.parametrize("missing", ["verifier_generation", "verifier_id", "driver_input_digest"])
def test_check_resolved_names_missing_ticket_fields(missing):
    """deepseek LOW 3 r1: a verifier ticket's facts short of a field the compare reads refuse
    `ticket-field:verifier`, never KeyError."""
    facts = T.check_facts({k: v for k, v in _vargs().items() if k != "attempt_id"})
    resolved = _resolved_for(facts)
    with pytest.raises(Refused) as e:
        T.check_resolved({k: v for k, v in facts.items() if k != missing}, resolved)
    assert e.value.name == "ticket-field:verifier"
