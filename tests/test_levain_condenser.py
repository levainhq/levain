"""LevainCondenser — the compaction-reinject fold (Slice 1).

Deterministic unit tests (no model) cover the fold's logic: the compaction→recovery sequence,
consume-once, the async path, serialization-safety, the fork-downgrade warning, and that the
re-anchor rides at recency as an excluded-from-capture system event. One gated integration test
proves the planted re-anchor REACHES + STEERS the model on the recovery turn (the moat check).

Guarded on the ``openhands`` extra (openhands-sdk) — skips cleanly where it's absent (e.g. levain's
own core .venv, which has no openhands-sdk). With the vagus→Levain fold-in, the firing adapter lives
in ``levain.firing`` — there is NO vagus dependency; the extra needs only openhands-sdk + anneal.

    <venv-with-openhands>/bin/python -m pytest tests/test_levain_condenser.py -p no:libtmux

(``-p no:libtmux`` disables the pytest-8-incompatible libtmux plugin openhands-tools pulls in.)
"""
from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import tempfile
import uuid

import pytest

pytest.importorskip("openhands.sdk", reason="openhands.sdk not importable (openhands extra absent)")
pytest.importorskip(
    "levain.firing.openhands",
    reason="levain.firing.openhands not importable (install levain[openhands] for openhands-sdk)",
)

from openhands.sdk import (  # noqa: E402
    LLM,
    Agent,
    Conversation,
    Message,
    MessageEvent,
    TextContent,
)
from openhands.sdk.context.condenser import CondenserBase, NoOpCondenser  # noqa: E402
from openhands.sdk.context.view.view import View  # noqa: E402
from openhands.sdk.event.condenser import Condensation  # noqa: E402
from pydantic import PrivateAttr  # noqa: E402

from levain.firing import StubFiring  # noqa: E402

from levain.firing import ReanchorRequest, StubPresence, register_presence  # noqa: E402
from levain.firing.openhands import LevainCondenser  # noqa: E402


# --- fakes (no model) ---------------------------------------------------------------

_A_CONDENSATION = dict(source="agent", forgotten_event_ids=[], summary="s", llm_response_id="r")


class _LViewInner(CondenserBase):
    """Passes the view through unchanged (no compaction)."""

    def condense(self, view: View, agent_llm: LLM | None = None) -> View:
        return view


class _LCondInner(CondenserBase):
    """Always signals a compaction (returns a Condensation)."""

    def condense(self, view: View, agent_llm: LLM | None = None) -> Condensation:
        return Condensation(**_A_CONDENSATION)


class _NCondenseThenView(CondenserBase):
    """Returns a Condensation for the first ``n`` calls, then passes the View through."""

    n: int = 1
    _calls: int = PrivateAttr(default=0)

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        self._calls += 1
        if self._calls <= self.n:
            return Condensation(**_A_CONDENSATION)
        return view


class _AsyncOnceCondThenView(CondenserBase):
    """Async: Condensation first, then View. Sync RAISES — so a green test proves the fold rode
    the async path (inner.acondense), not the base fallback that calls inner.condense."""

    _calls: int = PrivateAttr(default=0)

    def condense(self, view: View, agent_llm: LLM | None = None) -> View:
        raise AssertionError("sync condense called on the async path")

    async def acondense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        self._calls += 1
        if self._calls == 1:
            return Condensation(**_A_CONDENSATION)
        return view


class _MarkerPresence:
    def __init__(self, text: str = "MARK") -> None:
        self.text = text

    def reanchor(self, req: ReanchorRequest) -> str | None:
        return self.text or None


class _RaisingPresence:
    """A presence source that RAISES — to exercise the fail-soft boundary (re-arm + log)."""

    def reanchor(self, req: ReanchorRequest) -> str | None:
        raise RuntimeError("transient store read failed")


class _RecordingPresence:
    """Captures every ReanchorRequest — to assert the condenser's population of query/turn_index,
    which every other test source discards (a deleted `turn_index=self._turn` would stay green)."""

    def __init__(self) -> None:
        self.requests: list[ReanchorRequest] = []

    def reanchor(self, req: ReanchorRequest) -> str | None:
        self.requests.append(req)
        return "REC-REANCHOR"


def _user_view(text: str = "hi") -> View:
    return View.from_events(
        [MessageEvent(source="user", llm_message=Message(role="user", content=[TextContent(text=text)]))]
    )


def _last_text(view: View) -> str:
    return view.events[-1].llm_message.content[0].text


# --- dependency isolation (sterile: the adapter builds without flow) -----------------


def test_condenser_imports_without_flow():
    """The guardrail: the adapter must build WITHOUT flow on PYTHONPATH. A runtime lock — if a
    future edit adds `import flow`, this fails."""
    code = "import sys; import levain.firing.openhands; assert 'flow' not in sys.modules, 'flow leaked'"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr or r.stdout


# --- construction: both handles built (the pydantic inheritance assumption) ----------


def test_both_handles_built_on_construction():
    """LevainCondenser adds ``_ensure_presence`` (mode=after) alongside the INHERITED
    ``_ensure_firing`` — both must run, so a fresh condenser has both live handles. This locks the
    pydantic validator-inheritance assumption the whole design rests on."""
    cond = LevainCondenser(inner=_LViewInner())
    assert cond._firing is not None  # inherited VagusCondenser validator ran
    assert cond._presence is not None  # this class's validator ran


# --- the fold: normal turn / compaction / recovery -----------------------------------


def test_normal_turn_injects_firing_only_no_reanchor():
    cond = LevainCondenser.build(inner=_LViewInner(), presence=StubPresence(reanchor_text="RE"))
    out = cond.condense(_user_view())
    assert isinstance(out, View)
    # exactly ONE event added (the firing inject) — no compaction, so no re-anchor
    assert len(out.events) == len(_user_view().events) + 1
    assert "RE" not in _last_text(out)
    assert out.events[-1].llm_message.role == "system"


def test_compaction_turn_passes_through_and_arms_reanchor():
    cond = LevainCondenser.build(inner=_LCondInner(), presence=StubPresence())
    out = cond.condense(_user_view())
    assert isinstance(out, Condensation)  # passthrough, no inject
    assert cond.pending_reanchor is True  # armed for the recovery turn


def test_recovery_turn_injects_firing_AND_reanchor():
    cond = LevainCondenser.build(
        inner=_NCondenseThenView(n=1), presence=StubPresence(reanchor_text="REANCHOR-MARK")
    )
    turn1 = cond.condense(_user_view())  # compaction → passthrough
    turn2 = cond.condense(_user_view())  # recovery → firing inject + re-anchor
    assert isinstance(turn1, Condensation)
    assert isinstance(turn2, View)
    # user + firing-inject + re-anchor = 3 events; re-anchor is LAST (recency)
    assert len(turn2.events) == 3
    reanchor = turn2.events[-1]
    firing = turn2.events[-2]
    assert reanchor.llm_message.role == "system"
    assert reanchor.sender == "levain"  # the presence re-anchor
    assert "REANCHOR-MARK" in reanchor.llm_message.content[0].text
    assert firing.sender == "vagus"  # the firing recall (inherited)
    assert cond.pending_reanchor is False  # consumed


def test_reanchor_fires_once_then_stops():
    cond = LevainCondenser.build(
        inner=_NCondenseThenView(n=1), presence=StubPresence(reanchor_text="ONCE")
    )
    cond.condense(_user_view())  # compaction
    recovery = cond.condense(_user_view())  # re-anchor fires
    after = cond.condense(_user_view())  # normal turn — NO re-anchor
    assert "ONCE" in _last_text(recovery)
    assert len(after.events) == 2  # user + firing inject only
    assert after.events[-1].sender == "vagus"  # firing, not the re-anchor
    assert "ONCE" not in _last_text(after)


def test_consecutive_compactions_reanchor_once_on_recovery():
    cond = LevainCondenser.build(
        inner=_NCondenseThenView(n=2), presence=StubPresence(reanchor_text="AFTER-TWO")
    )
    t1 = cond.condense(_user_view())  # compaction 1
    t2 = cond.condense(_user_view())  # compaction 2
    t3 = cond.condense(_user_view())  # recovery → single re-anchor
    assert isinstance(t1, Condensation) and isinstance(t2, Condensation)
    assert isinstance(t3, View)
    assert "AFTER-TWO" in _last_text(t3)
    assert t3.events[-1].sender == "levain"


def test_empty_reanchor_consumes_and_does_not_retry():
    """A presence source with nothing to re-assert (empty text) → the firing inject only, no stray
    empty re-anchor event; a CLEAN empty result CONSUMES the flag (does not re-arm), so a later turn
    produces no re-anchor either. (Pins the consume-on-empty decision — distinct from re-arm-on-raise.)"""
    cond = LevainCondenser.build(
        inner=_NCondenseThenView(n=1), presence=StubPresence(reanchor_text="")
    )
    cond.condense(_user_view())  # compaction
    recovery = cond.condense(_user_view())  # recovery: empty → no event
    assert len(recovery.events) == 2  # user + firing inject; NO re-anchor event
    assert recovery.events[-1].sender == "vagus"
    assert cond.pending_reanchor is False  # empty CONSUMED (not re-armed)
    after = cond.condense(_user_view())  # a further normal turn — still no re-anchor
    assert len(after.events) == 2
    assert after.events[-1].sender == "vagus"


def test_reanchor_excluded_from_capture_via_render_turn():
    """MEANING test (not just the source label): the re-anchor the condenser produces must be
    dropped by vagus's actual capture renderer, so it can never be written to episodic memory and
    re-recalled (no memory-eats-its-own-tail loop). Uses the real render_turn, not an assertion on
    the source string."""
    from levain.firing.openhands import render_turn  # the real capture renderer

    cond = LevainCondenser.build(
        inner=_NCondenseThenView(n=1), presence=StubPresence(reanchor_text="SECRET-REANCHOR")
    )
    cond.condense(_user_view())  # compaction
    reanchor_event = cond.condense(_user_view()).events[-1]  # recovery → the actual re-anchor event
    assert reanchor_event.source == "environment"
    # A realistic capture turn containing the re-anchor: render_turn must include the genuine
    # user/assistant exchange but EXCLUDE the re-anchor (source="environment").
    turn = [
        MessageEvent(source="user", llm_message=Message(role="user", content=[TextContent(text="hi")])),
        MessageEvent(source="agent", llm_message=Message(role="assistant", content=[TextContent(text="hello")])),
        reanchor_event,
    ]
    rendered = render_turn(turn)
    assert rendered is not None
    assert "[assistant] hello" in rendered  # the genuine turn IS captured
    assert "SECRET-REANCHOR" not in rendered  # the re-anchor is NOT (no self-ingestion loop)


# --- the async path ------------------------------------------------------------------


def test_acondense_fold_reanchors_on_recovery():
    cond = LevainCondenser.build(
        inner=_AsyncOnceCondThenView(), presence=StubPresence(reanchor_text="ASYNC-REANCHOR")
    )
    t1 = asyncio.run(cond.acondense(_user_view()))  # compaction (async)
    t2 = asyncio.run(cond.acondense(_user_view()))  # recovery (async)
    assert isinstance(t1, Condensation)
    assert isinstance(t2, View)
    assert "ASYNC-REANCHOR" in _last_text(t2)
    assert t2.events[-1].sender == "levain"


# --- preservation of the inner's View properties -------------------------------------


class _FlaggedNCondenseThenView(CondenserBase):
    """Compaction first; then a View carrying unhandled_condensation_request=True."""

    _calls: int = PrivateAttr(default=0)

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:
        self._calls += 1
        if self._calls == 1:
            return Condensation(**_A_CONDENSATION)
        return View(events=list(view.events), unhandled_condensation_request=True)


def test_reanchor_preserves_unhandled_condensation_request():
    """The re-anchor's direct View() construction must preserve the inner's flag (the M5 lesson,
    inherited) — even on the recovery turn that appends the re-anchor."""
    cond = LevainCondenser.build(inner=_FlaggedNCondenseThenView(), presence=StubPresence(reanchor_text="F"))
    cond.condense(_user_view())  # compaction
    recovery = cond.condense(_user_view())  # recovery: View(flag=True) + re-anchor
    assert isinstance(recovery, View)
    assert recovery.unhandled_condensation_request is True
    assert "F" in _last_text(recovery)


# --- serialization-safety ------------------------------------------------------------


def test_presence_survives_serialization_roundtrip():
    """fork()/reload round-trip through model_dump()/model_validate(); presence_kind must survive
    and rebuild _presence (mirrors the firing HIGH-1 serialization contract)."""
    orig = LevainCondenser(inner=NoOpCondenser(), firing_kind="stub", presence_kind="stub")
    restored = LevainCondenser.model_validate(orig.model_dump())
    assert restored.presence_kind == "stub"
    assert restored._presence is not None
    assert isinstance(restored._presence, StubPresence)
    # the firing half still injects after the round-trip (inherited serialization-safety intact)
    out = restored.condense(_user_view())
    assert out.events[-1].llm_message.role == "system"


def test_fold_works_after_serialization_with_registered_kind():
    """A registered presence kind rebuilds from the serialized dump, and the fold fires post-restore
    — proving presence_kind (not a live handle) is what survives fork."""
    register_presence("_rt_presence", lambda: _MarkerPresence("RT-REANCHOR"))
    orig = LevainCondenser(inner=_NCondenseThenView(n=1), presence_kind="_rt_presence")
    restored = LevainCondenser.model_validate(orig.model_dump())
    t1 = restored.condense(_user_view())  # inner _calls reset on restore → first call compacts
    t2 = restored.condense(_user_view())
    assert isinstance(t1, Condensation)
    assert "RT-REANCHOR" in _last_text(t2)


def test_pending_reanchor_survives_serialization():
    """pending_reanchor is a SERIALIZED field (not a PrivateAttr): a fork/reload that lands between
    a compaction and its recovery MUST still fire the re-anchor (codex L3 HIGH — dropping it is a
    miss at the exact boundary the adapter exists for). Arm it, round-trip, and the restored
    condenser fires the surviving re-anchor on its next recovery View."""
    register_presence("_surv_presence", lambda: _MarkerPresence("SURVIVED"))
    armed = LevainCondenser.build(inner=_LViewInner(), presence_kind="_surv_presence")
    armed.pending_reanchor = True  # arm, as if a compaction just happened before the fork
    restored = LevainCondenser.model_validate(armed.model_dump())
    assert restored.pending_reanchor is True  # THE FIX: survives (was a PrivateAttr → reset to False)
    out = restored.condense(_user_view())  # _LViewInner → recovery View → fires the surviving re-anchor
    assert "SURVIVED" in _last_text(out)
    assert restored.pending_reanchor is False  # consumed after firing


def test_reanchor_failure_rearms_and_logs(caplog):
    """A presence source that RAISES must NOT crash the agent turn (fail-soft), must inject NO
    event, must LOG the loss LOUD, and must RE-ARM (keep pending_reanchor True) so the next recovery
    turn retries — distinct from a clean empty, which consumes. (codex/complement L3: the build/call
    fail-soft boundary + the consume-before-call drop.)"""
    import logging

    cond = LevainCondenser.build(inner=_NCondenseThenView(n=1), presence=_RaisingPresence())
    cond.condense(_user_view())  # compaction → arms
    with caplog.at_level(logging.WARNING, logger="levain.firing.openhands"):
        recovery = cond.condense(_user_view())  # recovery: source raises
    assert isinstance(recovery, View)  # did NOT crash the turn
    assert len(recovery.events) == 2  # user + firing inject; NO re-anchor event
    assert recovery.events[-1].sender == "vagus"
    assert cond.pending_reanchor is True  # RE-ARMED (not consumed) → retry next recovery
    assert any("re-anchor FAILED" in r.message for r in caplog.records)  # logged LOUD


def test_bad_presence_kind_self_heal_does_not_crash_turn():
    """The fail-OPEN codex+complement reproduced: model_construct bypasses the validator, so
    _presence is None with an unregistered presence_kind; the recovery self-heal calls build_presence
    (which RAISES on an unknown kind). It must degrade to no-re-anchor, never raise out of condense()."""
    cond = LevainCondenser.model_construct(
        inner=_NCondenseThenView(n=1), firing_kind="stub", presence_kind="typo-does-not-exist"
    )
    cond.condense(_user_view())  # compaction → arms
    recovery = cond.condense(_user_view())  # recovery self-heal build_presence("typo") → must NOT crash
    assert isinstance(recovery, View)
    assert len(recovery.events) == 2  # firing inject only; the failed re-anchor injected nothing


def test_non_str_reanchor_return_does_not_crash(caplog):
    """A source DEFECT (returns a non-str, outside the str|None contract) must not crash the turn at
    TextContent(text=...) downstream — the seam guards it: log + drop + consume (codex L3 round-2 LOW)."""
    import logging

    class _BadTypePresence:
        def reanchor(self, req: ReanchorRequest):
            return ["not", "a", "string"]  # contract violation (str | None)

    cond = LevainCondenser.build(inner=_NCondenseThenView(n=1), presence=_BadTypePresence())
    cond.condense(_user_view())  # compaction
    with caplog.at_level(logging.WARNING, logger="levain.firing.openhands"):
        recovery = cond.condense(_user_view())  # recovery: bad-type return
    assert isinstance(recovery, View)  # did NOT crash
    assert len(recovery.events) == 2  # firing inject only; no re-anchor event
    assert cond.pending_reanchor is False  # consumed (a type defect won't self-heal → no retry-spam)
    assert any("expected str|None" in r.message for r in caplog.records)  # logged


def test_condenser_populates_reanchor_request():
    """The condenser's population of ReanchorRequest (turn_index=self._turn, query="") has no
    coverage otherwise — every other source discards req, so a deleted `turn_index=self._turn` stays
    green. A recording source pins it."""
    rec = _RecordingPresence()
    cond = LevainCondenser.build(inner=_NCondenseThenView(n=1), presence=rec)
    cond.condense(_user_view())  # compaction
    cond.condense(_user_view())  # recovery → calls rec.reanchor with the populated request
    assert len(rec.requests) == 1
    assert rec.requests[-1].turn_index == cond._turn  # the just-advanced firing turn
    assert rec.requests[-1].query == ""  # Slice 1: query not yet wired (Slice 4)


# --- the fork-downgrade warning ------------------------------------------------------


def test_build_warns_on_non_stub_live_presence_with_default_kind():
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        LevainCondenser.build(inner=_LViewInner(), presence=_MarkerPresence("live"))
    assert any("will NOT survive fork" in str(w.message) and "presence" in str(w.message) for w in caught)

    with warnings.catch_warnings(record=True) as caught2:
        warnings.simplefilter("always")
        LevainCondenser.build(inner=_LViewInner(), presence=StubPresence())
    assert not any("survive fork" in str(w.message) for w in caught2)


# --- inherited firing behavior still works (light) ----------------------------------


def test_inherited_firing_directive_still_rotates():
    """Subclassing must not break the inherited per-turn firing inject / directive rotation."""
    cond = LevainCondenser.build(inner=_LViewInner())
    first = _last_text(cond.condense(_user_view()))
    second = _last_text(cond.condense(_user_view()))
    assert first != second  # the inherited drift-defense directive rotates


# --- moat: the re-anchor reaches + steers the model on the recovery turn (gated) -----


def _ollama_up() -> bool:
    try:
        socket.create_connection(("localhost", 11434), timeout=2).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ollama_up(), reason="local Ollama :11434 not available")
def test_reanchor_reaches_model_on_recovery_turn():
    """L4 moat: the behavioral re-anchor injected on the recovery turn must reach + steer the model.

    OpenHands re-invokes the condenser WITHIN a single run() after a Condensation (empirically:
    call1→Condensation, then call2→View, both before the completion). So a compaction and its
    recovery both happen inside the run that triggers them — exactly the real deployment, where a
    turn that overflows max_size compacts and recovers in-place. `_NCondenseThenView(n=1)` forces
    that compaction onto THIS run; the recovery View carries the planted fact in the re-anchor.

    The steering signal is a BENIGN FACT the model retrieves, NOT an injection-flavored directive.
    An earlier "SYSTEM DIRECTIVE: reply with exactly <nonce>" tripped minimax's prompt-injection
    refusal (L2 apparatus finding) — the model correctly identified it as an injection "at the end"
    of context (which itself proved reaches-at-recency), but refused to comply, making the STEER
    half flaky. A benign session fact the user then asks about is answered from recency context,
    robustly proving reaches + steers without an adversarial directive."""
    nonce = "REANCHOR_" + uuid.uuid4().hex[:8]

    class _PlantPresence:
        def reanchor(self, req: ReanchorRequest) -> str:
            return f"[session context — note for this conversation] The project codename is {nonce}."

    llm = LLM(
        model="openai/minimax-m3:cloud",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        drop_params=True,
        usage_id="test-reanchor",
        temperature=0.0,
    )
    # n=1: within this run, condense call-1 → Condensation (compaction), call-2 → View (recovery,
    # fires the re-anchor) → the completion sees the planted fact.
    cond = LevainCondenser.build(
        inner=_NCondenseThenView(n=1), firing=StubFiring(), presence=_PlantPresence()
    )
    agent = Agent(llm=llm, tools=[], include_default_tools=[], condenser=cond)

    events: list = []
    with tempfile.TemporaryDirectory() as wd:
        conv = Conversation(agent, workspace=wd, callbacks=[events.append], visualizer=None)
        conv.send_message("What is the project codename for this conversation? Answer with just the codename.")
        conv.run()  # compaction + recovery (with the re-anchor) both happen in this run

    reply = " ".join(
        c.text
        for e in events
        if isinstance(e, MessageEvent) and e.llm_message.role == "assistant"
        for c in (e.llm_message.content or [])
        if getattr(c, "text", None)
    )
    assert nonce in reply, f"re-anchor codename not found in reply: {reply!r}"
