"""The GateHandle (trust-tiering S2): a policy swap publishes a new gate, and every admission runs wholly
on the gate it captured.

Covered here: ``with_policy`` shares every stateful collaborator (checked against the constructor, so a
new collaborator added later fails here rather than being dropped by a swap); ``swap``'s frozen catalog
and its refusals; the HOLDERS (``FireDispatcher.dispatch``, ``ChainExecutor`` execute / resume and
``sweep_timeouts``) reading the handle exactly once per admission, counted by a probe handle; and the
atomicity CONTRACT test of the handle itself (``test_contract_*``).
"""
from __future__ import annotations

import datetime as _dt
import inspect
import re
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.autonomic import (
    ActionManifest, ActionRequest, ActionRisk, ChainExecutor, EfferentGate, ExecutionResult, FireDispatcher,
    GateHandle, GateReceiptStore, IntentProvenance, Posture, RiskClass, SignalAuth, TrustContext,
    manual_invocation,
)
from levain.autonomic import gate as gate_module
from levain.autonomic.handle import as_handle
from levain.autonomic.journal import run_id_for
from tests.autonomic_confirm_keys import signed_yes
from tests.test_autonomic_journal_wiring import FIXED, HIGH, LOW, World, _chain_builder, _single_builder, _trust

pytestmark = pytest.mark.usefixtures("test_only_confined_executor")

AUTO = ActionRisk(RiskClass.LOW, reversible=True, external=False, financial=False)
CONFIRM = ActionRisk(RiskClass.MEDIUM, reversible=False, external=True, financial=False)


class _Executor:
    name = "recording"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[str] = []

    def execute(self, action_name: str, payload: str, *, context_id: str) -> ExecutionResult:
        with self._lock:
            self.calls.append(action_name)
        return ExecutionResult(ok=True, detail="ok", downstream_id=f"out:{action_name}")


def _request(action_name: str, context_id: str = "ctx") -> ActionRequest:
    return ActionRequest(
        action_name=action_name, payload="p", context_id=context_id, query_text="q", query_date="2026-10-12",
        trust=TrustContext(signal_auth=SignalAuth.STRONG, intent_provenance=IntentProvenance.INTENT_BEARING,
                           human_present=True),
        grounded=True, authority=manual_invocation(), proposal_id="prop")


def _first_tool(tools, catalog):
    """The test aggregation: the risk of the link's first sealed tool in the catalog it is handed."""
    [subgoal] = tools
    return catalog[subgoal.tools[0]]


# --- with_policy: a swap changes policy, never state -------------------------------------------------

def test_the_state_table_names_every_constructor_argument_that_is_not_policy():
    params = set(inspect.signature(EfferentGate.__init__).parameters) - {"self"}
    assert gate_module._POLICY_ARGUMENTS <= params
    assert set(gate_module._STATE_COLLABORATORS) == params - gate_module._POLICY_ARGUMENTS


def test_with_policy_shares_every_collaborator_by_reference(tmp_path):
    old = EfferentGate(
        manifest=ActionManifest({"a": AUTO}), store=GateReceiptStore(tmp_path / "r.jsonl"), executor=_Executor(),
        clock=lambda: FIXED, transport=object(), pending_store=object(), summarize=lambda r: "s",
        confirm_window_s=17, auto_fire_actions=frozenset({"a"}), trajectory_observer=object(),
        journal=object(), binding_risk=lambda b, i: AUTO, confirm_signers=tmp_path / "signers",
        ssh_keygen=tmp_path / "ssh-keygen")
    resolver = lambda b, i: CONFIRM   # noqa: E731
    new = old.with_policy(ActionManifest({"a": CONFIRM}), resolver)
    # every attribute the constructor sets, not just the table's: an attribute set from no argument
    # would be reset by a swap without the table noticing
    assert set(vars(new)) == set(vars(old))
    for attr, value in vars(old).items():
        if attr == "_manifest":
            assert new._manifest is not value and new._manifest.risk_of("a") == CONFIRM
        elif attr == "_binding_risk":
            assert new._binding_risk is resolver
        elif isinstance(value, Path):
            assert new.__dict__[attr] == value   # the constructor rebuilds a Path from a Path (a value)
        else:
            assert new.__dict__[attr] is value, attr
    assert old._manifest.risk_of("a") == AUTO   # the old gate is untouched


# --- swap ---------------------------------------------------------------------------------------------

def _gate(tmp_path, manifest=None):
    return EfferentGate(manifest=manifest or ActionManifest({"a": AUTO}),
                        store=GateReceiptStore(tmp_path / "r.jsonl"), executor=_Executor(), clock=lambda: FIXED)


def test_swap_publishes_a_new_gate_that_shares_the_old_ones_state(tmp_path):
    a = _gate(tmp_path)
    handle = GateHandle(a)
    assert handle.current() is a
    b = handle.swap(ActionManifest({"a": CONFIRM}), {"t": AUTO}, _first_tool)
    assert handle.current() is b and b is not a
    assert b._store is a._store and b._executor is a._executor
    assert a.gate(_request("a")).fired and b.gate(_request("a")).deferred


def test_swap_freezes_the_catalog_it_is_given(tmp_path):
    handle = GateHandle(_gate(tmp_path))
    catalog = {"mail.read": AUTO}
    gate = handle.swap(ActionManifest({"a": AUTO}), catalog, _first_tool)
    catalog["mail.read"] = CONFIRM   # the caller's mapping changes after the swap
    binding = SimpleNamespace(goal=(SimpleNamespace(tools=("mail.read",)),))   # the resolver reads only goal
    assert gate.binding_risk(binding, 0) == AUTO


def test_swap_hands_the_aggregation_a_read_only_catalog(tmp_path):
    seen = []
    handle = GateHandle(_gate(tmp_path))
    gate = handle.swap(ActionManifest({"a": AUTO}), {"t": AUTO}, lambda tools, cat: seen.append(cat) or AUTO)
    gate.binding_risk(SimpleNamespace(goal=("x",)), 0)
    with pytest.raises(TypeError):
        seen[0]["t"] = CONFIRM


@pytest.mark.parametrize("catalog", [lambda b, i: AUTO, {"t": "not a risk"}, [("t", AUTO)]])
def test_swap_refuses_a_resolver_or_a_malformed_catalog(tmp_path, catalog):
    a = _gate(tmp_path)
    handle = GateHandle(a)
    with pytest.raises(TypeError):
        handle.swap(ActionManifest({"a": AUTO}), catalog, _first_tool)
    assert handle.current() is a


def test_signers_are_never_a_swap_argument():
    assert list(inspect.signature(GateHandle.swap).parameters) == ["self", "manifest", "binding_catalog",
                                                                   "aggregate"]


def test_a_bare_gate_is_wrapped_in_a_handle_that_never_swaps(tmp_path):
    a = _gate(tmp_path)
    handle = as_handle(a)
    assert handle.current() is a and not handle.swappable
    assert as_handle(handle) is handle
    with pytest.raises(RuntimeError):
        handle.swap(ActionManifest({"a": AUTO}), {}, _first_tool)


# --- the holders: one handle, one capture per admission ----------------------------------------------

def test_no_holder_reads_a_gate_attribute_on_an_admission_path():
    root = Path(gate_module.__file__).parent
    for name in ("firepath.py", "chainpath.py"):
        assert not re.search(r"self\._gate\b", (root / name).read_text()), name


def test_the_dispatcher_and_the_chain_executor_share_one_handle(tmp_path):
    w = World(tmp_path / "w")
    # a bare gate given to both: the dispatcher shares the executor's fixed handle
    assert w.dispatcher.handle is w.chains.handle
    handle = GateHandle(w.gate)
    chains = ChainExecutor(gate=handle, request_builder=_chain_builder, trust_resolver=_trust,
                           clock=lambda: FIXED, binding_store=w.store)
    kw = dict(store=w.store, predicate_match=lambda p, e: True, request_builder=_single_builder,
              clock=lambda: FIXED, chain_executor=chains)
    assert FireDispatcher(gate=handle, **kw).handle is handle
    with pytest.raises(ValueError, match="same gate handle"):
        FireDispatcher(gate=w.gate, **kw)   # the bare gate, against an executor on a swappable handle
    with pytest.raises(ValueError, match="same gate handle"):
        FireDispatcher(gate=GateHandle(w.gate), **kw)


def _handled_world(tmp_path, builder_hook):
    """A ``World`` whose dispatcher and chain executor read one swappable handle; ``builder_hook(i)`` runs
    inside the request builder of link ``i``, after that admission captured its gate."""
    w = World(tmp_path / "w")
    handle = GateHandle(w.gate)

    def single(binding, event):
        builder_hook(0)
        return _single_builder(binding, event)

    def chain(binding, ctx, i):
        builder_hook(i)
        return _chain_builder(binding, ctx, i)

    w.chains = ChainExecutor(gate=handle, request_builder=chain, trust_resolver=_trust, clock=lambda: FIXED,
                             binding_store=w.store)
    w.dispatcher = FireDispatcher(
        store=w.store, gate=handle,
        predicate_match=lambda p, e: e.get("fields", {}).get(p["field"]) == p["value"],
        request_builder=single, clock=lambda: FIXED, chain_executor=w.chains)
    return w, handle


_TIGHTER = {"mail.read": HIGH, "mail.send": HIGH}


def test_a_single_link_admission_in_flight_completes_on_the_gate_it_captured(tmp_path):
    swapped = []

    def hook(i):
        if not swapped:
            swapped.append(handle.swap(ActionManifest({"link0": LOW, "link1": HIGH}), _TIGHTER, _first_tool))

    w, handle = _handled_world(tmp_path, hook)
    w.mint(chain=False)
    first = w.dispatch("e1")
    assert swapped and handle.current() is swapped[0]
    assert first.outcome.fired   # captured before the swap: the old policy, where mail.read is LOW
    second = w.dispatch("e2")
    assert second.outcome.pending and not second.outcome.fired   # the next admission reads the new gate


def test_each_chain_link_captures_its_own_gate(tmp_path):
    swapped = []

    def hook(i):
        if i == 0 and not swapped:
            swapped.append(handle.swap(ActionManifest({"link0": LOW, "link1": HIGH}),
                                       {"mail.read": HIGH, "mail.send": LOW}, _first_tool))

    w, handle = _handled_world(tmp_path, hook)
    w.mint(chain=True)
    d = w.dispatch("e1")
    links = d.chain.links
    # link 0 captured the old gate (mail.read LOW: fires); link 1 the new one (mail.send LOW: the terminal
    # link still pauses at the binding's ratified CONFIRM floor)
    assert links[0].outcome.fired
    assert d.chain.state == "paused" and d.chain.paused_at == 1
    assert w.outbox() == [("link0", "e1-0")]


# --- the holders, counted: exactly one read of the handle per admission -----------------------------
# A holder that reads the handle twice in one admission (a decision on one gate, its resolve on the next)
# is the mixed-policy shape the handle exists to prevent, and no swap-timing test reliably lands a swap
# between the two reads. So these count the reads instead, driven through each holder's public entry: a
# second read in any admission, or a capture skipped, changes the count.

class _CountingHandle(GateHandle):
    """TEST-ONLY: a handle that counts reads of :meth:`current`."""

    def __init__(self, gate: EfferentGate) -> None:
        super().__init__(gate)
        self.reads = 0

    def current(self) -> EfferentGate:
        self.reads += 1
        return super().current()

    def count(self, admit):
        """``admit()``'s result and how many times it read the handle."""
        self.reads = 0
        result = admit()
        return result, self.reads


def _counted_world(tmp_path):
    w = World(tmp_path / "w")
    handle = _CountingHandle(w.gate)
    w.chains = ChainExecutor(gate=handle, request_builder=_chain_builder, trust_resolver=_trust,
                             clock=lambda: FIXED, binding_store=w.store)
    w.dispatcher = FireDispatcher(
        store=w.store, gate=handle,
        predicate_match=lambda p, e: e.get("fields", {}).get(p["field"]) == p["value"],
        request_builder=_single_builder, clock=lambda: FIXED, chain_executor=w.chains)
    return w, handle


def test_a_single_link_dispatch_reads_the_handle_once(tmp_path):
    w, handle = _counted_world(tmp_path)
    w.mint(chain=False)
    d, reads = handle.count(lambda: w.dispatch("e1"))
    assert d.outcome.fired and reads == 1   # the effect refusal, the risk and the fire: one gate


def test_a_chain_dispatch_reads_the_handle_once_per_link(tmp_path):
    w, handle = _counted_world(tmp_path)
    w.mint(chain=True)
    d, reads = handle.count(lambda: w.dispatch("e1"))
    # the dispatch capture serves the effect refusal, the pre-flight and link 0; link 1 reads its own
    assert d.chain.paused and d.chain.paused_at == 1 and reads == 2


def test_a_chain_that_pauses_at_link_0_reads_the_handle_once(tmp_path):
    w, handle = _counted_world(tmp_path)
    w.tool_risk[0] = HIGH
    w.mint(chain=True)
    d, reads = handle.count(lambda: w.dispatch("e1"))
    assert d.chain.paused and d.chain.paused_at == 0 and reads == 1


def test_a_chain_executed_without_a_captured_gate_reads_the_handle_once_at_its_start(tmp_path):
    w, handle = _counted_world(tmp_path)
    w.tool_risk[0] = HIGH
    b = w.mint(chain=True)
    event = {"type": "email", "id": "e1", "fields": {"from": "a@x.example", "dmarc": "pass"}}
    fresh = w.store.admit(b.binding_id, run_id_for(b.binding_id, event))
    out, reads = handle.count(lambda: w.chains.execute(fresh, event))
    assert out.paused and out.paused_at == 0 and reads == 1   # the pre-flight and link 0: one gate


def test_a_resume_reads_the_handle_once_per_admission(tmp_path):
    w, handle = _counted_world(tmp_path)
    w.tool_risk[0] = HIGH
    w.mint(chain=True)
    w.dispatch("e1")
    yes = signed_yes(w.gate, w.open_pending_id())
    out, reads = handle.count(lambda: w.resolve_open_as(yes))
    # link 0's resolve on one gate, then link 1 (which pauses) on its own
    assert out.paused and out.paused_at == 1 and reads == 2
    yes = signed_yes(w.gate, w.open_pending_id())
    out, reads = handle.count(lambda: w.resolve_open_as(yes))
    assert out.completed and reads == 1   # the terminal link's resolve alone


def test_the_chain_sweep_reads_the_handle_once_per_swept_hold(tmp_path):
    w, handle = _counted_world(tmp_path)
    w.mint(chain=True)
    w.dispatch("e1")
    out, reads = handle.count(lambda: w.chains.sweep_timeouts(FIXED + _dt.timedelta(minutes=30)))
    assert out == [] and reads == 1   # not expired: the silence decision's gate, and nothing more
    [out], reads = handle.count(lambda: w.chains.sweep_timeouts(FIXED + _dt.timedelta(hours=2)))
    # the silence decision and the resolve it drives: one gate
    assert out.aborted and out.reason == "denied:confirm_window_elapsed" and reads == 1


# --- the atomicity CONTRACT test ---------------------------------------------------------------------

class _ProbedHandle(GateHandle):
    """TEST-ONLY instrumentation: every read of the published gate takes a capture sequence number in
    the same step (under one probe lock), and the publish takes one too. An admitting thread records its
    FIRST read per admission, so admissions are classified by capture order, never completion time."""

    def __init__(self, gate: EfferentGate) -> None:
        self._probe = threading.Lock()
        self._seq = 0
        self.publish_seq: int | None = None
        self._tl = threading.local()
        super().__init__(gate)

    @property
    def _gate(self) -> EfferentGate:
        with self._probe:
            self._seq += 1
            gate = self.__dict__["_gate"]
            if getattr(self._tl, "capture", None) is None:
                self._tl.capture = (self._seq, gate)
            return gate

    @_gate.setter
    def _gate(self, gate: EfferentGate) -> None:
        with self._probe:
            self._seq += 1
            if "_gate" in self.__dict__:
                self.publish_seq = self._seq
            self.__dict__["_gate"] = gate

    def begin_admission(self) -> None:
        self._tl.capture = None

    def capture(self) -> tuple[int, EfferentGate]:
        return self._tl.capture


_MIX = ("act.same", "act.tighten", "act.gone", "act.confirm")
_POLICY_A = {"act.same": AUTO, "act.tighten": AUTO, "act.gone": AUTO, "act.confirm": CONFIRM}
_POLICY_B = {"act.same": AUTO, "act.tighten": CONFIRM, "act.confirm": CONFIRM}   # act.gone: refused
_THREADS = 8
_PER_THREAD = 30


def _decision(outcome):
    return (outcome.posture, outcome.fired, outcome.refused, outcome.deferred, outcome.reason)


def _oracle(tmp_path, policy: dict[str, ActionRisk]) -> dict[str, tuple]:
    """Each request of the mix decided single-threaded by a fresh gate on ``policy``."""
    gate = EfferentGate(manifest=ActionManifest(policy), store=GateReceiptStore(tmp_path / "oracle.jsonl"),
                        executor=_Executor(), clock=lambda: FIXED)
    return {name: _decision(gate.gate(_request(name))) for name in _MIX}


def test_contract_every_admission_decides_on_the_one_gate_it_captured(tmp_path):
    """A CONTRACT test, not a reproduction: the 10-09 probe (e3e5010) produced no mixed decision against
    the gate as it was, so this pins the property a GateHandle must keep rather than a bug seen in the
    wild. N threads admit a fixed request mix while one thread swaps policy A for B (B tightens one action
    from auto to confirm and refuses one more). Each admission's decision must equal the single-threaded
    oracle of the gate it captured, and every capture after the publish must be B.

    Each thread's FIRST admission is held inside the gate's own step (the shared clock, the first thing
    ``_resolve`` reads) until the swap has published, so it is deterministic that admissions captured on A
    are decided after B is live. A swap that changed the gate an admission already holds (an edit in place
    rather than a new gate) decides those on B and fails here. The admissions here read the handle
    directly, so this tests the HANDLE, never the holders: that every holder reads it exactly once per
    admission is pinned by the counting tests (``test_*_reads_the_handle_*``)."""
    oracle = {"A": _oracle(tmp_path / "a", _POLICY_A), "B": _oracle(tmp_path / "b", _POLICY_B)}
    assert oracle["A"]["act.tighten"] != oracle["B"]["act.tighten"]
    assert oracle["A"]["act.gone"] != oracle["B"]["act.gone"] and oracle["B"]["act.gone"][2]

    arrived = threading.Semaphore(0)
    published = threading.Event()
    held = threading.local()

    def clock():
        if threading.current_thread().name.startswith("admit") and not getattr(held, "done", False):
            held.done = True
            arrived.release()
            assert published.wait(30), "the swap never published"
        return FIXED

    gate_a = EfferentGate(manifest=ActionManifest(_POLICY_A), store=GateReceiptStore(tmp_path / "r.jsonl"),
                          executor=_Executor(), clock=clock)
    handle = _ProbedHandle(gate_a)
    gate_b: list[EfferentGate] = []
    records: list[tuple[int, EfferentGate, str, tuple]] = []
    records_lock = threading.Lock()
    errors: list[BaseException] = []

    def admit(t: int) -> None:
        try:
            for k in range(_PER_THREAD):
                action = _MIX[(t + k) % len(_MIX)]
                handle.begin_admission()
                outcome = handle.current().gate(_request(action, f"t{t}-{k}"))
                seq, captured = handle.capture()
                with records_lock:
                    records.append((seq, captured, action, _decision(outcome)))
        except BaseException as e:  # noqa: BLE001 — surfaced by the main thread
            errors.append(e)

    def swap() -> None:
        try:
            for _ in range(_THREADS):
                assert arrived.acquire(timeout=30), "an admitting thread never reached the gate"
            gate_b.append(handle.swap(ActionManifest(_POLICY_B), {}, _first_tool))
        except BaseException as e:  # noqa: BLE001
            errors.append(e)
        finally:
            published.set()

    threads = [threading.Thread(target=admit, args=(t,), name=f"admit-{t}") for t in range(_THREADS)]
    threads.append(threading.Thread(target=swap, name="swap"))
    for th in threads:
        th.start()
    for th in threads:
        th.join(60)
    assert not errors, errors
    assert not any(th.is_alive() for th in threads)

    assert handle.publish_seq is not None
    assert len(records) == _THREADS * _PER_THREAD
    names = {id(gate_a): "A", id(gate_b[0]): "B"}
    records = [(seq, names.get(id(g), "?"), action, decision) for seq, g, action, decision in records]
    for seq, captured, action, decision in records:
        assert captured in ("A", "B")
        assert decision == oracle[captured][action], (seq, captured, action, decision)
        assert captured == ("B" if seq > handle.publish_seq else "A"), (seq, captured, handle.publish_seq)
    # the swap landed mid-run, with admissions captured on each side of it, the changed actions included
    for side in ("A", "B"):
        assert {a for _, c, a, _ in records if c == side} >= {"act.tighten", "act.gone"}, side
