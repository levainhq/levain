"""S1 3c-7 fix round 1 (1025+10 head rulings, AMENDED after L3 r1: `1025-6/3c7/L3_r1.txt`).

- ONE ticket-facts shape: the facts CONTROL holds on its ticket record (`tickets.check_facts`, what `MINT`'s claims
  check reads) are what ATTACH answers and what alloc's `ticket_facts` parses; a verifier ticket's prepare writes them.
- render's second gate (CONTRACT §3.5 "Ticket facts reach render"): `ticket-facts-missing`, and the blob's routes,
  model, verifier steps and driver against the facts alloc carries, all before render's first CAS.
- CONTROL's start wakes the reaper always (its mirror is empty until root's first sync).
- the r1 LOWs: a bind line's slot, a SLOT-SYNC entry that is no JSON value, ATTACH's mark rolled back, the reaper's
  sync over a broken connection, POOL on a poisoned journal or with no ticket store."""
from __future__ import annotations

import hashlib
import os
import shutil
import socket
import tempfile
import threading
from pathlib import Path

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import records as R
from levain.slot import routemaps as RM
from levain.slot import tickets as T
from levain.slot.ids import SlotId
from tests.test_pool import EXEC_FACTS, F2_DISPATCH, attach, entry, pool, rig, sync  # noqa: F401 - fixtures
from tests.test_pool import dispatch  # noqa: F401 - fixture
from tests.test_routemaps import st  # noqa: F401 - the signed route-map store fixture
from tests.test_slot_s2f2_runtime_helper import BOOT
from tests.test_tickets import SESSION, refused, root, ticket_of

STEPS = [{"argv": ["pytest", "-q"], "wall_s": 60}]
VER_FACTS = {"job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64, "kind": "verifier",
             "egress_routes": [], "reserved_microdollars": 0, "verifier_generation": 1, "verifier_id": "pytest-report",
             "verifier_steps": STEPS, "input_bundle_digest": "sha256:" + "2" * 64,
             "driver_input_digest": "sha256:" + "3" * 64}
#: an executor ticket whose routes lane F2's base map holds (`anthropic` brokered, `github` egress)
F2_FACTS = dict(EXEC_FACTS, route="anthropic", model_requested="claude-opus-5-5")


def _blob(**kw) -> bytes:
    obj = {"format": "levain-session-render/1", "vendor": "claude", "routes": ["anthropic", "github"],
           "model": "claude-opus-5-5"}
    obj.update(kw)
    return canon.canonical_bytes({k: v for k, v in obj.items() if v is not None})


#: the active verifier entry VER_FACTS names (3c-8: a verifier ticket carries the entry CONTROL resolved at mint)
VER_ENTRY = {"verifier_id": "pytest-report", "verifier_artifact_digest": "sha256:" + "5" * 64,
             "declared_kind": "black-box", "driver_template": "pytest_report",
             "driver_template_digest": "sha256:" + "6" * 64, "steps": STEPS,
             "steps_digest": "sha256:" + hashlib.sha256(canon.canonical_bytes(STEPS)).hexdigest(),
             "driver_input": {}, "driver_input_digest": "sha256:" + "3" * 64}


class Verifiers:
    """A stand-in for CONTROL's verifier store with one active generation holding VER_ENTRY (3c-8; the signed
    CONFIG-SUBMIT path is tests/test_slot_3c8.py's)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.gen = RM.CheckedVerifiers(1, "0" * 64, "ab" * 16, {"pytest-report": VER_ENTRY})

    def active(self):
        return self.gen

    def start_pass(self) -> None:
        pass


def _mint(d, facts: dict) -> str:
    d.n += 1
    resolved = None
    if facts["kind"] == "verifier":
        if d.w.control.verifiers is None:
            d.w.control.verifiers = Verifiers()
        resolved = d.w.control.verifiers.active().resolved(facts["verifier_id"])
    return d.tickets.mint(f"a-{d.n}", T.check_facts(facts), C.new_handle, resolved=resolved).ticket


# ---- HIGH 1: one ticket-facts shape -----------------------------------------------------------------------------

@pytest.mark.parametrize("facts", [EXEC_FACTS, dict(EXEC_FACTS, kind="planner"), VER_FACTS],
                         ids=["executor", "planner", "verifier"])
def test_every_ticket_facts_shape_parses_in_alloc(facts):
    """What `check_facts` keeps on a ticket record (and ATTACH answers whole) is an alloc `ticket_facts` value."""
    kept = T.check_facts(facts)
    slot = SlotId("none", 0) if facts["kind"] == "verifier" else SlotId("egress", 1)
    a = R.Alloc(session=SESSION, slot=slot, lease_seq=1, slot_rev=1, slot_uid=893, slot_gid=893,
                kind=T.RENDERS_AS[facts["kind"]][0], vendor="claude",
                auth="none" if slot.slot_class == "none" else "brokered", operator_uid=501, supervisor_uid=502,
                staging_uid=502, created_at="2026-10-20T00:00:00Z", boot_id=BOOT,
                child_uid=892 if slot.slot_class == "none" else None, ticket=C.new_handle(), ticket_facts=kept)
    assert R.parse_alloc(R.alloc_bytes(a)).ticket_facts == kept


def test_check_facts_refuses_steps_alloc_cannot_hold():
    """`verifier_steps` is checked at mint by the §5.6 shape alloc parses with, so no ticket binds that root's alloc
    write would then refuse."""
    for bad in ([], [{"argv": [], "wall_s": 60}], [{"argv": ["x"], "wall_s": 0}], [{"argv": ["x"]}], ["x"]):
        with pytest.raises(Refused) as e:
            T.check_facts(dict(VER_FACTS, verifier_steps=bad))
        assert e.value.name == "ticket-field:verifier_steps", bad


def test_verifier_ticketed_prepare_writes_its_facts(dispatch):
    """codex r1 HIGH 1: a verifier ticket's prepare writes ATTACH's ticket_facts into alloc (it refused `unknown-key`
    with the ticket already bound)."""
    d, w = dispatch, dispatch.w
    ticket = _mint(d, VER_FACTS)
    line = d.prepare(ticket, "none", "verifier", "claude", "none")
    assert line["ok"], (line, w.last_err)
    a = w.alloc(line["session"])
    assert a.ticket == ticket
    assert a.ticket_facts == dict(T.check_facts(VER_FACTS), verifier=d.w.control.verifiers.active().resolved(
        "pytest-report"))


# ---- HIGH 2 / obs 1: render's second gate -----------------------------------------------------------------------

def _prepared(d, facts: dict = F2_FACTS) -> str:
    line = d.prepare(_mint(d, facts))
    assert line["ok"], (line, d.w.last_err)
    d.w.stage(line["session"])                                           # so only the gate can refuse the render
    return line["session"]


def _render(d, s: str, blob: bytes) -> dict:
    return d.w.main(["render", s], blob, uid=F2_DISPATCH)[1]


@pytest.fixture
def pinning_stand_in(monkeypatch):
    """3b-8a T5 refuses every ticketed egress render ``route-policy-unavailable`` until S2 builds route pinning
    (CONTRACT §3.3). A test of what a ticketed render does PAST that refusal (its first CAS, a post-CAS REVOKE) lifts
    it here, so the machinery S2 re-opens stays held; the refusal itself is held by ``test_slot_3b8a_f1``."""
    from levain.slot import runtime_helper as H

    monkeypatch.setattr(H, "refuse_ticketed_front", lambda a: None)


def test_prepare_failing_after_attach_leaves_render_refused(dispatch):
    """A prepare whose alloc write of the facts fails after ATTACH bound the ticket: CONTROL holds the session
    (ALLOCATED, ticket bound), alloc carries the ticket without the facts, and render refuses `ticket-facts-missing`
    before its first CAS (fail closed: nothing renders, the session ends with its lease)."""
    d, w = dispatch, dispatch.w

    def boom(step):
        if step == "ticket-facts":
            raise Refused("io-error", "injected")

    w.on_step = boom
    ticket = _mint(d, F2_FACTS)
    line = d.prepare(ticket)
    assert line["refused"] == "io-error", line
    w.on_step = None
    (s,) = [n[:-5] for n in os.listdir(w.L / "alloc") if n.endswith(".json") and not n.endswith(".intent.json")]
    a = w.alloc(s)
    assert a.ticket == ticket and a.ticket_facts is None
    assert d.tickets.bound(s) is not None and w.state(s).state == R.ALLOCATED
    calls = len(w.calls)
    assert _render(d, s, _blob())["refused"] == "ticket-facts-missing"
    assert w.calls[calls:] == [] and w.state(s).state == R.ALLOCATED


@pytest.mark.parametrize("change,name", [
    ({"routes": ["anthropic"]}, "ticket-routes-mismatch"),                      # an egress route dropped
    ({"routes": ["anthropic", "github", "claude-sub"]}, "ticket-routes-mismatch"),   # one the ticket never named
    ({"model": "claude-other"}, "ticket-model-mismatch"),
    ({"model": None}, "ticket-model-mismatch"),
])
def test_render_refuses_a_blob_off_its_ticket(dispatch, change, name):
    """§3.5: render refuses unless the blob's routes == the ticket's `route` + `egress_routes` and its model is the
    ticket's `model_requested`, before its first CAS (no RENDERING, no policy, no front)."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    calls = len(w.calls)
    assert _render(d, s, _blob(**change))["refused"] == name
    assert w.calls[calls:] == [] and w.state(s).state == R.ALLOCATED and w.alloc(s).policy is None


@pytest.mark.usefixtures("pinning_stand_in")
def test_render_on_its_ticket_passes_the_gate(dispatch):
    """The blob the ticket names passes the second gate: render goes on to its first CAS."""
    d, w = dispatch, dispatch.w
    s = _prepared(d)
    calls = len(w.calls)
    line = _render(d, s, _blob(routes=["github", "anthropic"]))                # order is not a mismatch
    assert w.calls[calls][0] == "CAS", (line, w.last_err)


def _verifier_resolved(steps=STEPS) -> dict:
    return {"verifier_generation": 1, "verifier_id": "pytest-report", "verifier_artifact_digest": "sha256:" + "5" * 64,
            "declared_kind": "black-box", "driver_template": "pytest_report",
            "driver_template_digest": "sha256:" + "6" * 64,
            "steps_digest": "sha256:" + "7" * 64, "driver_input_digest": "sha256:" + "3" * 64}


@pytest.mark.parametrize("facts_change,blob_change,name", [
    ({}, {}, None),
    ({"verifier": None}, {}, "ticket-facts-missing"),                   # no resolved entry: no driver is authorised
    ({}, {"verifier_steps": [{"argv": ["pytest"], "wall_s": 60}]}, "ticket-steps-mismatch"),
    ({}, {"verifier_steps": [{"argv": ["pytest", "-q"], "wall_s": 61}]}, "ticket-steps-mismatch"),
    ({}, {"driver": "other_driver"}, "ticket-driver-mismatch"),
    ({}, {"model": "m"}, "ticket-model-mismatch"),
])
def test_render_checks_a_verifier_blob_against_its_ticket(facts_change, blob_change, name):
    """codex r1 HIGH 2: a verifier blob's `verifier_steps` and `driver` are the ticket's (its steps, its resolved
    entry's `driver_template`), never dispatch's later word."""
    from levain.slot import render as RD
    from levain.slot import runtime_grammar as G

    facts = dict(T.check_facts(VER_FACTS), verifier=_verifier_resolved())
    facts.update(facts_change)
    facts = {k: v for k, v in facts.items() if v is not None}
    obj = {"format": "levain-session-render/1", "vendor": "claude", "routes": [], "verifier_steps": STEPS,
           "driver": "pytest_report", **blob_change}
    blob = G.parse_stdin("render", canon.canonical_bytes(obj))
    if name is None:
        RD.check_ticket(blob, "verifier", "none", C.new_handle(), facts)
        return
    with pytest.raises(Refused) as e:
        RD.check_ticket(blob, "verifier", "none", C.new_handle(), facts)
    assert e.value.name == name


def test_render_ticket_kind_must_match_the_alloc():
    from levain.slot import render as RD
    from levain.slot import runtime_grammar as G

    blob = G.parse_stdin("render", _blob())
    with pytest.raises(Refused) as e:
        RD.check_ticket(blob, "planner", "egress", C.new_handle(), T.check_facts(F2_FACTS))
    assert e.value.name == "ticket-kind-mismatch"
    RD.check_ticket(blob, "dispatch", "egress", None, None)               # no ticket: nothing to check here


# ---- obs 2: CONTROL's start wakes the reaper ---------------------------------------------------------------------

def test_control_start_wakes_the_reaper_with_no_records(tmp_path):
    """The mirror starts empty, so no mint is admitted until root's first sync: the start wakes the reaper even when
    no record needs it."""
    d = tmp_path / "sessions"
    d.mkdir(mode=0o700)
    wakes: list = []
    control = C.Control(C.StateStore(d), os.getuid(), wake_reaper=lambda: wakes.append(1))
    control.start()
    try:
        assert wakes == [1]
    finally:
        control.stop()


# ---- the r1 LOWs --------------------------------------------------------------------------------------------------

def test_bind_line_with_lease_seq_and_no_slot_is_inconsistent(rig):
    """complement 1 / deepseek MED: a bind line naming `lease_seq` without `slot` refuses the start
    `journal-inconsistent`, never a bare KeyError."""
    one = ticket_of(rig.mint("a-1"))
    rig.control.stop()
    with open(rig.sdir / T.JOURNAL, "ab") as f:
        f.write(canon.canonical_bytes({"op": "bind", "ticket": one, "session": SESSION, "lease_seq": 1}) + b"\n")
    store = T.TicketStore(rig.sdir / T.JOURNAL, rig.st.L / "policy" / "dispatch_ceiling", clock=rig.clock,
                          ceiling_uid=os.getuid())
    with pytest.raises(Refused) as e:
        store.start()
    assert e.value.name == "journal-inconsistent" and "slot" in e.value.detail


@pytest.mark.parametrize("value", [float("nan"), 1.5, {1, 2}, b"x"], ids=["nan", "float", "set", "bytes"])
def test_slot_sync_entry_that_is_no_json_value(rig, value):
    """complement 2: an in-process SLOT-SYNC entry canonical JSON cannot write answers `slot-state-malformed` (the
    wire never carries one: a frame is canonical JSON)."""
    good = entry("egress", 0, 5, 0)
    assert refused(root(rig, "SLOT-SYNC", slots=[good, dict(good, index=1, rev=value)])) == "slot-state-malformed"
    assert rig.control.seam_errors == []


def test_refused_attach_rolls_the_mirror_mark_back(rig):
    """complement 3: ATTACH marks the mirror entry leased before the bind; when it then refuses (`session-exists`
    here), the entry is what it was, so the slot is not under-counted until a restart."""
    one = ticket_of(rig.mint("a-1"))
    assert attach(rig, one, index=1, seq=1, rev=1)["ok"]
    two = ticket_of(rig.mint("a-2"))
    before = rig.control.pool.entry("egress", 2)
    free = pool(rig)
    assert refused(attach(rig, two, SESSION, index=2, seq=1, rev=1)) == "session-exists"
    assert rig.control.pool.entry("egress", 2) == before
    assert pool(rig) == free
    assert rig.ask("RUN-TICKET-STATUS", attempt_id="a-2")["state"] == "unbound"


def test_ambiguous_attach_refusal_keeps_the_mark(rig, monkeypatch):
    """An ATTACH refusal prepare reads as ambiguous (it keeps the slot leased, gives nothing back) keeps the mark: the
    mirror never shows reusable a slot root still holds leased. The two sides' ambiguous sets agree."""
    from levain.slot import runtime_helper as H

    assert {"record-unsettled", "internal-error"} <= H._ATTACH_AMBIGUOUS and "session-exists" not in H._ATTACH_AMBIGUOUS
    two = ticket_of(rig.mint("a-2"))
    real_get = rig.control.store.get

    def unsettled(session):
        if session == SESSION:
            raise Refused("record-unsettled", "injected")
        return real_get(session)

    monkeypatch.setattr(rig.control.store, "get", unsettled)
    assert refused(attach(rig, two, SESSION, index=2, seq=1, rev=1)) == "record-unsettled"
    e2 = rig.control.pool.entry("egress", 2)
    assert (e2.state, e2.session, e2.rev) == ("leased", SESSION, 1)


def test_rollback_never_undoes_a_newer_sync(rig):
    """The rollback restores the entry only while it is still ATTACH's own mark: a sync that merged in between
    stands."""
    m = rig.control.pool
    prior, mark = m.mark_leased("egress", 3, SESSION, 1, 5)
    assert mark is not None and m.entry("egress", 3).state == "leased"
    m.merge([R.parse_slot_state(canon.canonical_bytes(entry("egress", 3, 6, 1, "scrubbed")))])
    m.unmark("egress", 3, prior, mark)
    assert m.entry("egress", 3).state == "scrubbed"
    prior, mark = m.mark_leased("egress", 3, SESSION, 1, 5)              # not above rev 6: no mark, nothing to undo
    assert mark is None


def test_control_client_answers_a_transport_break_as_control_down():
    """complement 5, the check it asked for first: `Ctx.call` reaches CONTROL through `ControlClient`, which answers a
    broken connection `Refused("control-down")`, so the reaper's step 7 names it `slot-sync:control-down` in the
    heartbeat (test_pool's `test_pool_mirror_rebuilt_from_sync`), never an escaped OSError."""
    from levain.slot.control_client import ControlClient

    d = Path(tempfile.mkdtemp(prefix="lv7", dir="/tmp"))                  # short: AF_UNIX path limit
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        srv.bind(str(d / "c.sock"))
        srv.listen(1)
        client = ControlClient(d / "c.sock", timeout=5)
        peer, _ = srv.accept()
        peer.close()                                                      # CONTROL's end goes away
        with pytest.raises(Refused) as e:
            client.call("SLOT-SYNC", slots=[])
        assert e.value.name == "control-down"
    finally:
        srv.close()
        shutil.rmtree(d, ignore_errors=True)


def test_reaper_step7_os_error_is_named_and_the_pass_ends(tmp_path):
    """A client that raised a bare OSError anyway (none of levain's does) is named in the heartbeat by the pass's
    per-step guard, and the pass still writes its heartbeat."""
    from levain.slot import reaper as RP
    from tests.test_slot_s2f3_start import _world

    w, _la, _table = _world(tmp_path)

    def on_call(op, fields):
        if op == "SLOT-SYNC":
            raise BrokenPipeError("injected")

    w.on_call = on_call
    rep = RP.Reaper(w.seams, tuning=RP.Tuning(reserve_bytes=4096)).run_pass()
    assert "step7:BrokenPipeError" in rep["skipped"] and rep["ended_at"]


def test_pool_on_a_poisoned_journal_refuses(rig):
    """codex 3 / deepseek LOW: POOL answers `journal-unavailable` like every other journal verb once an append
    failed, never a count from memory the journal may not hold."""
    assert pool(rig)
    rig.tickets._poisoned = True
    assert refused(rig.ask("POOL")) == "journal-unavailable"


def test_pool_with_no_ticket_store_refuses(tmp_path):
    """deepseek LOW: a CONTROL built with no ticket journal answers POOL by name (`journal-unavailable`), never an
    internal error."""
    d = tmp_path / "sessions"
    d.mkdir(mode=0o700)
    control = C.Control(C.StateStore(d), os.getuid(), dispatch_uid=os.getuid() + 1)
    control.start()
    try:
        ans = control.handle(C.Conn(os.getuid() + 1), {"v": 1, "op": "POOL", "id": 1})
        assert ans == {"id": 1, "ok": False, "refused": "journal-unavailable"}
        assert control.seam_errors == []
    finally:
        control.stop()


# ---- r3: the start wake runs only after CONTROL's socket is listening --------------------------------------------

def test_server_start_wakes_the_reaper_only_after_the_socket_listens():
    """codex r2 MED: a wake that connects to CONTROL (the reaper's first pass dials it) must find the listener up."""
    d = Path(tempfile.mkdtemp(prefix="lv3c7", dir="/tmp"))
    (d / "sessions").mkdir(mode=0o700)
    path = d / "c.sock"
    reached: list = []

    def wake() -> None:
        c = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            c.connect(str(path))
            reached.append(True)
        except OSError:
            reached.append(False)
        finally:
            c.close()

    control = C.Control(C.StateStore(d / "sessions"), os.getuid(), wake_reaper=wake)
    server = C.ControlServer(control, path)
    server.start()
    try:
        assert reached == [True]
    finally:
        server.stop()
        shutil.rmtree(d, ignore_errors=True)
