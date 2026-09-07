"""Slice 1 tests — the firing contract + the VagusCondenser composition.

Deterministic unit tests (no model) cover the moat's logic: the dependency-isolation
guard, directive rotation, and the View-vs-Condensation composition branches. One gated
integration test proves the injected system content actually REACHES the model.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import urllib.request
import uuid

import pytest

pytest.importorskip("openhands.sdk", reason="openhands extra not installed")

from openhands.sdk import (
    LLM,
    Agent,
    Conversation,
    LLMSummarizingCondenser,
    Message,
    MessageEvent,
    TextContent,
)
from openhands.sdk.context.condenser import CondenserBase
from openhands.sdk.context.view.view import View
from openhands.sdk.event.condenser import Condensation

from levain.firing import CaptureRequest, DIRECTIVES, InjectRequest, StubFiring
from levain.firing.openhands import VagusCondenser


# --- fakes (no model) ---------------------------------------------------------------


class _ViewInner(CondenserBase):
    """Inner condenser that passes the view through unchanged (no summarization)."""

    def condense(self, view: View, agent_llm: LLM | None = None) -> View:
        return view


class _CondInner(CondenserBase):
    """Inner condenser that signals a compaction (returns a Condensation)."""

    cond: Condensation

    def condense(self, view: View, agent_llm: LLM | None = None) -> Condensation:
        return self.cond


def _user_view(text: str = "hi") -> View:
    return View.from_events(
        [MessageEvent(source="user", llm_message=Message(role="user", content=[TextContent(text=text)]))]
    )


# --- dependency isolation (stress-test correction b) --------------------------------


def test_firing_leaf_imports_without_openhands():
    """The firing contract is a dependency-isolated leaf: a fresh interpreter importing
    only vagus.firing must NOT pull OpenHands into sys.modules (correction b).
    A runtime check, not a source grep — prose may mention OpenHands; imports may not."""
    code = (
        "import sys; import levain.firing; "
        "leaked = sorted(m for m in sys.modules if 'openhands' in m); "
        "assert not leaked, leaked"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- StubFiring -------------------------------------------------------------------


def test_stub_capture_appends_in_memory():
    f = StubFiring()
    f.capture(CaptureRequest(content="turn one"))
    # use a REAL anneal EpisodeType ("decision") — not flow's "finding", which anneal rejects
    f.capture(CaptureRequest(content="turn two", episode_type="decision"))
    assert [c.content for c in f.captured] == ["turn one", "turn two"]
    assert f.captured[1].episode_type == "decision"


def test_stub_firing_shape_and_rotation():
    f = StubFiring(constitution="CONSTI")
    a = f.inject(InjectRequest())  # default lifecycle = per_turn
    b = f.inject(InjectRequest())
    # per-turn shape: recall slot present; constitution is NOT repeated here (it lives in
    # the session_start suffix) — the firing-lifecycle split.
    assert "CONSTI" not in a
    assert "recall" in a.lower()
    # drift-defense rotation: consecutive injects differ
    assert a != b


# --- VagusCondenser composition ----------------------------------------------------


def test_view_branch_injects_system_at_recency():
    base = _user_view()
    cond = VagusCondenser.build(inner=_ViewInner(), firing=StubFiring(constitution="MARKER"))
    out = cond.condense(base)

    assert isinstance(out, View)
    # exactly one event added (the inject), original untouched
    assert len(out.events) == len(base.events) + 1
    injected = out.events[-1]  # at recency
    assert isinstance(injected, MessageEvent)
    assert injected.llm_message.role == "system"
    text = injected.llm_message.content[0].text
    # the per-turn inject lands at recency: recall slot present; the constitution does NOT
    # ride per-turn (it's the session_start suffix, set once).
    assert "recall" in text.lower()
    assert "MARKER" not in text


def test_view_branch_does_not_mutate_input_view():
    base = _user_view()
    before = len(base.events)
    VagusCondenser.build(inner=_ViewInner()).condense(base)
    assert len(base.events) == before  # input view is not mutated in place


def test_condensation_branch_passes_through_unchanged():
    cond_obj = Condensation(
        source="agent", forgotten_event_ids=[], summary="s", llm_response_id="resp-1"
    )
    cond = VagusCondenser.build(inner=_CondInner(cond=cond_obj))
    out = cond.condense(_user_view())
    # compaction turn: returned unchanged, NO injection (nothing to inject into)
    assert out is cond_obj


def test_default_firing_is_stub():
    # The default firing is StubFiring — identified by its distinctive per-turn recall SLOT
    # (the constitution moved to session_start, so it no longer appears in the per-turn inject).
    cond = VagusCondenser.build(inner=_ViewInner())
    out = cond.condense(_user_view())
    text = out.events[-1].llm_message.content[0].text
    assert "anneal crystallized + episodic recall wires in here" in text  # StubFiring's recall slot


# --- moat verification: injection reaches the model (gated) -------------------------


# The model these live tests actually call. It is a CLOUD model proxied by the local
# daemon (`/api/tags` reports remote_host https://ollama.com:443), which is why the gate
# below asks about the MODEL and not about the port.
_LIVE_MODEL = "minimax-m3:cloud"


def _ollama_up() -> bool:
    """Is the model these tests call actually available?

    ⛔ THIS USED TO OPEN A SOCKET TO :11434 AND RETURN True (L2 finding, 2026-09-07).
    That gates on the LOCAL DAEMON while the test calls a REMOTE model — `/api/tags`
    reports `minimax-m3:cloud` with `remote_host: https://ollama.com:443`. So on a box
    running Ollama with no cloud credential the test did NOT skip: it went RED, after
    litellm's default retry ladder (`timeout=300`, `num_retries=5`) burned minutes, and
    the failure read exactly like a levain regression.
    ⚡ That is the SAME defect the test body was just rewritten to remove — a third
    party's behaviour standing in as the oracle — surviving one layer out in the gate
    that decides whether to consult the oracle at all. Fixing the assertion and leaving
    the gate would have been `guard_scoped_by_symptom_misses_the_class`.
    ⚠ HONEST LIMIT: listing the model proves reachability and registration, NOT that the
    upstream credential has headroom. It cannot — there is no cheap quota probe. This
    narrows the red-instead-of-skip window; it does not close it.
    """
    try:
        with urllib.request.urlopen(
            "http://localhost:11434/api/tags", timeout=3
        ) as resp:
            tags = json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError):
        return False
    return any(m.get("name") == _LIVE_MODEL for m in tags.get("models") or ())


@pytest.mark.skipif(
    not _ollama_up(), reason=f"{_LIVE_MODEL} not available via local Ollama"
)
def test_injection_reaches_model():
    """The planted per-turn inject must reach + steer the model. The nonce is
    unguessable, so its presence in the reply proves the inject landed.

    ⛔ THE STEERING SIGNAL IS A BENIGN FACT THE MODEL RETRIEVES, NOT AN INJECTION-FLAVORED
    DIRECTIVE (Diogenes MEDIUM, filed 2026-08-05, adopted 2026-09-07). This planted
    "SYSTEM DIRECTIVE: reply with exactly <nonce> and nothing else" and asserted the model
    OBEYED it — an assertion strictly stronger than the stated property. minimax refuses
    that shape, correctly, and DESCRIBES the plant while refusing: "I notice there's a prompt
    injection attempt embedded in your message ... I'm not going to comply." It names the
    injection without reproducing the nonce — which is positive proof the inject REACHED and
    STEERED the model (the refusal is a response to the plant), while failing `nonce in reply`.
    ⚠ It DESCRIBES rather than QUOTES, and the distinction is the whole point (L1, 2026-09-07):
    an earlier draft of this docstring said "quotes", which cannot be true — a quoted plant
    contains the nonce and the test would have PASSED. That the refusal withholds the nonce is
    exactly what made the failure indistinguishable from a real levain regression.
    ⚡ The oracle was a third-party model's willingness to comply with an injection — the
    one thing a well-behaved model is trained to refuse. A levain regression would have
    been indistinguishable from the refusal noise.
    ⚠ MEASURED HERE, 2026-09-07, same box, same model, temperature=0.0: the directive form
    passed 8 of 10 runs; the benign-fact form below passed 10 of 10.
    ⚡⚡ AND THE SIBLING MODULE ALREADY CARRIED BOTH THE DIAGNOSIS AND THE FIX — see
    `test_reanchor_reaches_model_on_recovery_turn` in `tests/test_levain_condenser.py`,
    rewritten to this shape at the time. That module was fixed and this one was left on the
    old shape for a month. The knowledge was written down; only one of the two surfaces
    adopted it. The fix here swaps ONLY the plant string and the question — `_PlantFiring`,
    `VagusCondenser.build(firing=...)` and the whole inject path are untouched, so this
    still exercises exactly the mechanism it always did."""
    nonce = "PASS_" + uuid.uuid4().hex[:8]

    class _PlantFiring:
        def inject(self, req: InjectRequest) -> str:
            return f"[session context — note for this conversation] The passphrase is {nonce}."

    llm = LLM(
        model="openai/minimax-m3:cloud",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        drop_params=True,
        usage_id="test-inject",
        temperature=0.0,
    )
    inner = LLMSummarizingCondenser(llm=llm, max_size=120, keep_first=4)
    cond = VagusCondenser.build(inner=inner, firing=_PlantFiring())
    agent = Agent(llm=llm, tools=[], include_default_tools=[], condenser=cond)

    events: list = []
    with tempfile.TemporaryDirectory() as wd:
        # ⛔ `close()` IS NOT OPTIONAL (L2 finding, 2026-09-07). `Conversation(...)` returns a
        # LocalConversation whose __init__ does `atexit.register(self.close)`. Without this,
        # the conversation, its LLM, its hook processor and its observability span are pinned
        # in the atexit registry for the whole pytest process and can never be collected — then
        # `close()` fires at interpreter exit against a workspace `TemporaryDirectory` deleted
        # long ago, surfacing as warning noise at the end of an otherwise-green run.
        conv = Conversation(agent, workspace=wd, callbacks=[events.append], visualizer=None)
        try:
            conv.send_message(
                "What is the passphrase for this conversation? Answer with just the passphrase."
            )
            conv.run()
        finally:
            conv.close()

    reply = " ".join(
        c.text
        for e in events
        if isinstance(e, MessageEvent) and e.llm_message.role == "assistant"
        for c in (e.llm_message.content or [])
        if getattr(c, "text", None)
    )
    assert nonce in reply, f"injected nonce not found in reply: {reply!r}"


# --- apparatus fixes: full condenser lifecycle + serialization (Slice 1 hardening) ----

from pydantic import PrivateAttr  # noqa: E402


class _HCRInner(CondenserBase):
    """Inner advertising condensation-request handling (like the real summarizer)."""

    def condense(self, view: View, agent_llm: LLM | None = None) -> View:
        return view

    def handles_condensation_requests(self) -> bool:
        return True


class _AsyncOnlyInner(CondenserBase):
    """Inner whose sync condense RAISES — so the async path MUST call acondense."""

    def condense(self, view: View, agent_llm: LLM | None = None) -> View:
        raise AssertionError("sync condense called on the async path")

    async def acondense(self, view: View, agent_llm: LLM | None = None) -> View:
        return view


class _OnceCondenseThenView(CondenserBase):
    """Returns a Condensation on the first call, then passes the View through."""

    _calls: int = PrivateAttr(default=0)

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        self._calls += 1
        if self._calls == 1:
            return Condensation(
                source="agent", forgotten_event_ids=[], summary="s", llm_response_id="r"
            )
        return view


class _FlaggedViewInner(CondenserBase):
    """Returns a View carrying unhandled_condensation_request=True."""

    def condense(self, view: View, agent_llm: LLM | None = None) -> View:
        return View(events=list(view.events), unhandled_condensation_request=True)


def test_handles_condensation_requests_delegates_to_inner():
    # masking the inner's capability would silently disable condensation recovery (codex HIGH)
    assert VagusCondenser.build(inner=_HCRInner()).handles_condensation_requests() is True
    assert VagusCondenser.build(inner=_ViewInner()).handles_condensation_requests() is False


def test_acondense_uses_inner_acondense_and_injects():
    # _AsyncOnlyInner.condense raises — so this passing proves the async path delegates to
    # inner.acondense (not the base fallback that calls inner.condense) AND injects (codex HIGH)
    import asyncio

    cond = VagusCondenser.build(inner=_AsyncOnlyInner(), firing=StubFiring(constitution="ASYNC"))
    out = asyncio.run(cond.acondense(_user_view()))
    assert isinstance(out, View)
    injected = out.events[-1].llm_message
    assert injected.role == "system"
    # the async path injected the per-turn content at recency (recall slot present)
    assert "recall" in injected.content[0].text.lower()


def test_firing_survives_serialization_roundtrip():
    """fork()/reload round-trip the agent through model_dump()/model_validate(); the real
    firing must survive — rebuilt from the serialized firing_kind (apparatus HIGH-1)."""
    from openhands.sdk.context.condenser import NoOpCondenser
    from levain.firing import register_firing

    # The marker must ride the PER-TURN inject (what the condenser produces), and the
    # constitution no longer does — so register a firing whose per_turn output carries a
    # unique marker, proving the right firing_kind was rebuilt from the serialized dump.
    class _RoundtripMarkerFiring:
        def inject(self, req: InjectRequest) -> str:
            return "ROUNDTRIP-MARKER-PER-TURN"

    register_firing("_rt", _RoundtripMarkerFiring)
    orig = VagusCondenser(inner=NoOpCondenser(), firing_kind="_rt")
    restored = VagusCondenser.model_validate(orig.model_dump())  # == what fork() does
    out = restored.condense(_user_view())
    assert "ROUNDTRIP-MARKER-PER-TURN" in out.events[-1].llm_message.content[0].text


def test_multi_turn_injection_rotates_directive():
    cond = VagusCondenser.build(inner=_ViewInner())
    first = cond.condense(_user_view()).events[-1].llm_message.content[0].text
    second = cond.condense(_user_view()).events[-1].llm_message.content[0].text
    assert first != second  # drift-defense directive rotates per turn


def test_condensation_then_view_restores_injection():
    cond = VagusCondenser.build(inner=_OnceCondenseThenView())
    turn1 = cond.condense(_user_view())
    turn2 = cond.condense(_user_view())
    assert isinstance(turn1, Condensation)  # compaction turn: passthrough, no inject
    assert isinstance(turn2, View)
    assert turn2.events[-1].llm_message.role == "system"  # inject restored next turn


def test_unhandled_condensation_request_preserved():
    # the M5 fix (direct View() instead of from_events) must preserve the inner's flag
    cond = VagusCondenser.build(inner=_FlaggedViewInner())
    out = cond.condense(_user_view())
    assert isinstance(out, View)
    assert out.unhandled_condensation_request is True


# --- Slice 2: the query + turn_index wiring (the condenser's new integration point) -----

from pathlib import Path  # noqa: E402

from levain.firing import select_directive  # noqa: E402
from levain.firing.anneal import AnnealFiring  # noqa: E402


class _RecordingFiring:
    """Captures every InjectRequest — to assert the wiring a StubFiring would silently discard
    (query + turn_index). Without this, deleting the condenser's query/turn_index wiring would
    leave every test green (apparatus L1 MED-1)."""

    def __init__(self) -> None:
        self.requests: list[InjectRequest] = []

    def inject(self, req: InjectRequest) -> str:
        self.requests.append(req)
        return "REC"


def _msg_view(*role_text: tuple[str, str]) -> View:
    # `source` (who created the event) is a distinct Literal from `role` (the message role):
    # a user message → source 'user'; an assistant message → source 'agent'. _recent_user_text
    # keys off llm_message.role, so only the role drives the assertions; source just stays valid.
    src = {"user": "user", "assistant": "agent"}
    return View.from_events(
        [
            MessageEvent(source=src.get(r, "environment"), llm_message=Message(role=r, content=[TextContent(text=t)]))
            for r, t in role_text
        ]
    )


def test_query_is_most_recent_user_text():
    rec = _RecordingFiring()
    VagusCondenser.build(inner=_ViewInner(), firing=rec).condense(
        _msg_view(("user", "first"), ("assistant", "reply"), ("user", "SECOND"))
    )
    assert rec.requests[-1].query == "SECOND"


def test_query_prefers_user_even_when_assistant_is_terminal():
    rec = _RecordingFiring()
    VagusCondenser.build(inner=_ViewInner(), firing=rec).condense(
        _msg_view(("user", "ASKED"), ("assistant", "answered"))
    )
    assert rec.requests[-1].query == "ASKED"


def test_query_falls_back_to_recent_any_role_when_no_user():
    rec = _RecordingFiring()
    VagusCondenser.build(inner=_ViewInner(), firing=rec).condense(
        _msg_view(("assistant", "ONLY-ASSISTANT"))
    )
    assert rec.requests[-1].query == "ONLY-ASSISTANT"  # post-compaction fallback


def test_turn_index_is_monotonic_independent_of_view_size():
    """The rotation-regression fix: turn_index advances 1,2,3 even when the View size is
    CONSTANT each turn (a fixed-window inner). On the old len(events) source it was constant."""
    rec = _RecordingFiring()
    cond = VagusCondenser.build(inner=_ViewInner(), firing=rec)
    for _ in range(3):
        cond.condense(_user_view())  # same 1-event view every turn
    assert [r.turn_index for r in rec.requests] == [1, 2, 3]


def test_build_warns_on_non_stub_live_firing_with_default_kind():
    """codex L3 MED: a non-stub live firing passed with the default firing_kind='stub' would
    silently downgrade to StubFiring on fork — build() must warn loudly. A StubFiring override
    (which the default kind correctly reconstructs) must NOT warn."""
    import warnings as _w

    with _w.catch_warnings(record=True) as caught:
        _w.simplefilter("always")
        VagusCondenser.build(inner=_ViewInner(), firing=AnnealFiring(crystal_path=Path("/x")))
    assert any("will NOT survive fork" in str(x.message) for x in caught)

    with _w.catch_warnings(record=True) as caught2:
        _w.simplefilter("always")
        VagusCondenser.build(inner=_ViewInner(), firing=StubFiring())
    assert not any("survive fork" in str(x.message) for x in caught2)


def test_anneal_directive_advances_through_condenser_with_constant_view():
    """End-to-end regression (L1/L2 MED): with a constant-size View each turn, AnnealFiring's
    drift-defense directive must still ADVANCE — proving rotation rides the monotonic counter,
    not the freezable event count. On the old code all three directives would be identical."""
    cond = VagusCondenser.build(
        inner=_ViewInner(), firing=AnnealFiring(crystal_path=Path("/no/such/store"))
    )
    injected = [
        cond.condense(_user_view()).events[-1].llm_message.content[0].text
        for _ in range(3)
    ]
    expected = [select_directive(1), select_directive(2), select_directive(3)]
    # The directive is injected at the TAIL (``f"{recall}\n{directive}"``). Directives are now
    # MULTI-LINE (the full recency bodies), so match the whole directive by suffix — a last-line
    # heuristic (the prior ``rsplit("\n", 1)[-1]``) would compare only its final line.
    assert [t.endswith(e) for t, e in zip(injected, expected)] == [True, True, True]
    assert len(set(injected)) == 3  # all differ — rotation lives


# NB: the pure-constant drift-lock tests (directive parity + operator-neutrality) live in the
# base-lane ``test_firing_contract.py`` — NOT here — because this module is
# ``pytest.importorskip("openhands.sdk")``-gated and the drift-lock must run without the extra.


@pytest.mark.skipif(
    not (Path.home() / ".anneal-memory" / "memory.crystal.json").exists(),
    reason="no live crystal store",
)
def test_l4_production_path_condenser_injects_real_recall():
    """L4 integration-semantics: the PRODUCTION path — firing_kind='anneal' (serialization-safe,
    reconstructed from the kind, NOT a live override) — injects REAL crystallized recall into the
    agent's context via the condenser. Proves the end-to-end afferent claim against the live
    anneal store with no model in the loop (deterministic: this query verifiably hits a pattern)."""
    cond = VagusCondenser.build(inner=_ViewInner(), firing_kind="anneal")
    out = cond.condense(
        _msg_view(("user", "invisible infrastructure failure: parse the real running signal not the surface-green status"))
    )
    injected = out.events[-1].llm_message.content[0].text
    assert "[recall — crystallized patterns relevant to this turn]" in injected  # real recall ran
    assert "invisible_infrastructure_failure" in injected  # a real pattern, recalled in production


# --- Slice 2 lifecycle: the session_start / per_turn split + the constitution suffix -----

from levain.firing.openhands import vagus_agent_context  # noqa: E402
from openhands.sdk.context.agent_context import AgentContext  # noqa: E402


def test_session_start_returns_constitution_only_stub():
    f = StubFiring(constitution="THE-CONSTITUTION")
    out = f.inject(InjectRequest(lifecycle_point="session_start"))
    assert out == "THE-CONSTITUTION"  # constitution alone — no recall slot, no directive
    assert "recall" not in out.lower()
    assert not any(d in out for d in DIRECTIVES)


def test_session_start_returns_constitution_only_anneal():
    f = AnnealFiring(constitution="ANNEAL-CONSTI", crystal_path=Path("/no/such/store"))
    out = f.inject(InjectRequest(lifecycle_point="session_start"))
    assert out == "ANNEAL-CONSTI"
    assert "recall" not in out.lower()  # session_start never touches the (read-only) recall path


def test_per_turn_omits_constitution_both_firings():
    stub = StubFiring(constitution="STUB-CONSTI")
    anneal = AnnealFiring(constitution="ANNEAL-CONSTI", crystal_path=Path("/no/such/store"))
    assert "STUB-CONSTI" not in stub.inject(InjectRequest(lifecycle_point="per_turn"))
    assert "ANNEAL-CONSTI" not in anneal.inject(InjectRequest(lifecycle_point="per_turn"))


def test_session_start_does_not_advance_stub_rotation():
    """session_start must not consume a drift-defense directive — it returns before the
    rotation increment, so a following per_turn still starts at directive index 0."""
    f = StubFiring()
    f.inject(InjectRequest(lifecycle_point="session_start"))
    f.inject(InjectRequest(lifecycle_point="session_start"))
    per_turn = f.inject(InjectRequest(lifecycle_point="per_turn"))
    assert DIRECTIVES[0] in per_turn  # first per_turn still gets the first directive


def test_vagus_agent_context_default_is_stub_constitution():
    ctx = vagus_agent_context()  # default firing_kind="stub"
    assert isinstance(ctx, AgentContext)
    assert "governed cognitive substrate" in ctx.system_message_suffix  # StubFiring default


def test_vagus_agent_context_uses_explicit_firing():
    ctx = vagus_agent_context(firing=StubFiring(constitution="EXPLICIT-CONSTI"))
    assert ctx.system_message_suffix == "EXPLICIT-CONSTI"


def test_vagus_agent_context_merges_base_suffix_without_clobbering():
    base = AgentContext(system_message_suffix="ADOPTER-FRAMING")
    ctx = vagus_agent_context(firing=StubFiring(constitution="VAGUS-CONSTI"), base=base)
    # both blocks survive, adopter's first then vagus's — never clobbered
    assert "ADOPTER-FRAMING" in ctx.system_message_suffix
    assert "VAGUS-CONSTI" in ctx.system_message_suffix
    assert ctx.system_message_suffix.index("ADOPTER-FRAMING") < ctx.system_message_suffix.index("VAGUS-CONSTI")


def test_vagus_agent_context_preserves_other_base_fields():
    base = AgentContext(system_message_suffix="X", user_message_suffix="USER-SUFFIX")
    ctx = vagus_agent_context(firing=StubFiring(constitution="C"), base=base)
    assert ctx.user_message_suffix == "USER-SUFFIX"  # non-suffix base fields preserved


def test_anneal_session_start_does_not_open_store(tmp_path):
    """session_start is pure static framing — it must NOT touch the (read-only) recall path,
    even when a crystal store EXISTS. A sentinel store that would raise if read proves it."""
    sentinel = tmp_path / "memory.crystal.json"
    sentinel.write_text("{ this is not valid json — reading it would raise }")
    f = AnnealFiring(constitution="SAFE", crystal_path=sentinel)
    # no exception, no recall — session_start returns the constitution untouched
    assert f.inject(InjectRequest(lifecycle_point="session_start")) == "SAFE"
