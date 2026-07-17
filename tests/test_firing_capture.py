"""Sub-slice B wiring tests — the OpenHands Stop→capture run-wrapper.

``render_turn`` (events → raw episode text) is the fork-independent core; ``vagus_run`` is the
in-process trigger (run to completion, then capture the completed turn). A fake conversation
keeps these model-free; an end-to-end real-run verification lives in ``smoke_capture.py``.
"""
from __future__ import annotations

import socket
import tempfile

import pytest

pytest.importorskip("openhands.sdk", reason="openhands extra not installed")

from openhands.sdk import Message, MessageEvent, TextContent

from levain.firing import CaptureRequest, StubFiring
from levain.firing.agent_reply import CORRECTIVE_NUDGE_MARKER, finish_message
from levain.firing.openhands import render_turn, vagus_run


def _msg(role: str, text: str, sender: str | None = None, source: str | None = None) -> MessageEvent:
    src = source or {"user": "user", "assistant": "agent", "system": "environment"}.get(role, "environment")
    kw = {"sender": sender} if sender is not None else {}
    return MessageEvent(source=src, llm_message=Message(role=role, content=[TextContent(text=text)]), **kw)


class _FinishAction:
    def __init__(self, message):
        self.kind = "FinishAction"
        self.message = message


class _ToolAction:
    def __init__(self, kind):
        self.kind = kind
        self.message = "not-a-reply"


class _ActionEvent:
    """An ActionEvent stand-in (source=agent, carries .action). NOT a MessageEvent, so
    render_turn takes its else-branch — the FinishAction path that carries the no-tool reply."""

    def __init__(self, action, source: str = "agent"):
        self.source = source
        self.action = action


# --- render_turn ---------------------------------------------------------------------


def test_render_turn_renders_user_and_assistant():
    out = render_turn([_msg("user", "build the thing"), _msg("assistant", "built it")])
    assert out == "[user] build the thing\n[assistant] built it"


def test_render_turn_captures_finish_action_reply():
    # The no-tool conversational answer arrives as an ActionEvent(FinishAction), NOT a
    # MessageEvent. render_turn MUST capture it, or the entity's memory silently drops
    # the reply on the common shape (the L4 finding, 2026-07-08).
    out = render_turn([_msg("user", "say pong"), _ActionEvent(_FinishAction("PONG"))])
    assert out == "[user] say pong\n[assistant] PONG"


def test_render_turn_ignores_non_finish_tool_actions():
    # A real tool action (bash/file) is NOT assistant text — so a turn with only a tool
    # action and no finish/message has no assistant line → not a capturable turn.
    out = render_turn([_msg("user", "do it"), _ActionEvent(_ToolAction("ExecuteBashAction"))])
    assert out is None


def test_finish_message_only_matches_finish_actions():
    assert finish_message(_ActionEvent(_FinishAction("hi"))) == "hi"
    assert finish_message(_ActionEvent(_ToolAction("ExecuteBashAction"))) is None
    assert finish_message(_ActionEvent(_FinishAction("   "))) is None  # blank → None
    assert finish_message(_msg("assistant", "text")) is None  # no .action → None


def test_render_turn_excludes_sdk_corrective_nudge():
    # The SDK injects a synthetic MessageEvent(source="user") nudge when a weak model returns
    # an empty/reasoning-only response. It must NOT become the captured [user] line, or the
    # real question is dropped and the episode fabricates the nudge as the human turn.
    nudge = _msg("user", "Your last response " + CORRECTIVE_NUDGE_MARKER + ". Please use a tool.")
    events = [_msg("user", "who are you?"), nudge, _ActionEvent(_FinishAction("I am Coyote."))]
    out = render_turn(events)
    assert out == "[user] who are you?\n[assistant] I am Coyote."
    assert CORRECTIVE_NUDGE_MARKER not in out  # the nudge text is nowhere in the episode


def test_render_turn_dedups_message_and_finish_echo():
    # A model that emits the answer as a MessageEvent AND echoes it in the finish message
    # must not double the assistant line in the captured episode.
    events = [_msg("user", "q"), _msg("assistant", "the answer"),
              _ActionEvent(_FinishAction("the answer"))]
    out = render_turn(events)
    assert out == "[user] q\n[assistant] the answer"


def test_corrective_nudge_marker_matches_installed_sdk():
    # DRIFT GUARD: if a future SDK bump changes the nudge text, this fails LOUDLY instead of
    # silently regressing the exclusion above to a no-op. Check the COMPILED string constants
    # (adjacent literals concatenate at compile time) — the source text splits the string
    # across lines, so the runtime string, not the source, is the truth.
    pytest.importorskip("openhands.sdk")
    from openhands.sdk.agent.response_dispatch import ResponseDispatchMixin

    consts = ResponseDispatchMixin._send_corrective_nudge.__code__.co_consts
    joined = " ".join(c for c in consts if isinstance(c, str))
    assert CORRECTIVE_NUDGE_MARKER in joined, (
        "the SDK's corrective-nudge text changed — update CORRECTIVE_NUDGE_MARKER in "
        "levain.firing.agent_reply or the nudge exclusion silently stops working"
    )


def test_render_turn_takes_only_the_latest_turn():
    events = [
        _msg("user", "first ask"),
        _msg("assistant", "first answer"),
        _msg("user", "SECOND ask"),
        _msg("assistant", "SECOND answer"),
    ]
    out = render_turn(events)
    assert "SECOND ask" in out and "SECOND answer" in out
    assert "first ask" not in out  # only the most recent completed turn is captured


def test_render_turn_excludes_system_and_vagus_injected():
    """The vagus firing inject is a system-role MessageEvent sent by 'vagus'. Capturing it would
    feed our own injected recall back into the store — capture must EXCLUDE it (and any system)."""
    events = [
        _msg("user", "the real ask"),
        _msg("system", "[recall] crystallized patterns ...", sender="vagus"),
        _msg("assistant", "the real answer"),
    ]
    out = render_turn(events)
    assert out == "[user] the real ask\n[assistant] the real answer"
    assert "recall" not in out


def test_render_turn_keys_boundary_on_source_not_role():
    """codex MED: OpenHands emits synthetic role='user' messages from source='environment' (e.g.
    denied Stop-hook feedback). The turn boundary must key on source=='user', NOT role, so such a
    synthetic message is neither the boundary nor rendered as the human turn."""
    events = [
        _msg("user", "the real human ask"),  # source="user"
        _msg("assistant", "the real answer"),  # source="agent"
        _msg("user", "SYNTHETIC HOOK FEEDBACK", source="environment"),  # role=user, source=environment
    ]
    out = render_turn(events)
    assert out == "[user] the real human ask\n[assistant] the real answer"
    assert "SYNTHETIC" not in out  # the synthetic env message is excluded


def test_render_turn_none_when_no_user_turn():
    assert render_turn([_msg("assistant", "orphan response")]) is None


def test_render_turn_none_when_no_assistant_response():
    # a lone user message is mid-turn (not yet completed) — capturing it would be a noise episode
    assert render_turn([_msg("user", "just asked, no reply yet")]) is None


def test_render_turn_empty_events():
    assert render_turn([]) is None


# --- vagus_run (the in-process trigger) ----------------------------------------------


class _FakeState:
    def __init__(self, events):
        self.events = events
        self.agent_state: dict = {}  # the fork-carried marker home (mirrors ConversationState)


class _FakeConversation:
    """Minimal stand-in: ``run()`` marks ran; ``state`` is a STABLE object (one per conversation,
    like the real LocalConversation) so the agent_state marker persists across vagus_run calls."""

    def __init__(self, events, conv_id="conv-123"):
        self._state = _FakeState(events)
        self.id = conv_id
        self.ran = False

    def run(self):
        self.ran = True

    @property
    def state(self):
        return self._state


def test_vagus_run_runs_then_captures_completed_turn():
    firing = StubFiring()
    conv = _FakeConversation([_msg("user", "do X"), _msg("assistant", "did X")])
    vagus_run(conv, firing=firing)
    assert conv.ran  # the conversation was actually run
    assert len(firing.captured) == 1
    req = firing.captured[0]
    assert req.content == "[user] do X\n[assistant] did X"
    assert req.episode_type == "observation"
    assert req.source == "vagus"
    assert req.session_id == "conv-123"  # defaults to the conversation id


def test_vagus_run_no_capture_on_incomplete_turn():
    firing = StubFiring()
    conv = _FakeConversation([_msg("user", "asked, agent produced nothing")])
    vagus_run(conv, firing=firing)
    assert conv.ran
    assert firing.captured == []  # nothing capturable → no episode written


def test_vagus_run_is_idempotent_within_a_turn():
    """The HIGH-1 guard: calling vagus_run twice with no intervening user turn must NOT write a
    duplicate episode — the second call sees the same turn marker and no-ops."""
    firing = StubFiring()
    conv = _FakeConversation([_msg("user", "do X"), _msg("assistant", "did X")])
    vagus_run(conv, firing=firing)
    vagus_run(conv, firing=firing)  # same turn, no new user message
    assert len(firing.captured) == 1  # captured ONCE, not twice


def test_vagus_run_captures_again_on_a_new_turn():
    """The guard must not over-suppress: a genuinely new turn (new user message) captures again."""
    firing = StubFiring()
    conv = _FakeConversation([_msg("user", "first"), _msg("assistant", "a1")])
    vagus_run(conv, firing=firing)
    conv._state.events = [*conv._state.events, _msg("user", "second"), _msg("assistant", "a2")]
    vagus_run(conv, firing=firing)
    assert [c.content for c in firing.captured] == [
        "[user] first\n[assistant] a1",
        "[user] second\n[assistant] a2",
    ]


def test_vagus_run_stores_marker_in_agent_state():
    """codex MED-1: the idempotency marker lives in the fork-carried ``state.agent_state`` dict,
    not an arbitrary conversation attribute (which fork() would not copy)."""
    firing = StubFiring()
    conv = _FakeConversation([_msg("user", "q"), _msg("assistant", "a")])
    vagus_run(conv, firing=firing)
    assert "vagus_last_captured_turn" in conv.state.agent_state


def test_vagus_run_single_capture_after_a_nudge_records_the_work():
    """Narrate-without-act backstop memory fix (L1+L2 review 2026-07-17): capturing ONCE after the full
    nudge cycle — ``[user task][assistant PLAN][user NUDGE][assistant WORK]`` — records the completed
    WORK, not the abandoned plan. This is what the run.py loop now does (run turn 1, nudge, run turn 2,
    then a single capture_turn)."""
    from levain.firing.agent_reply import LEVAIN_ACT_NUDGE

    firing = StubFiring()
    conv = _FakeConversation([
        _msg("user", "fix the bug in calc.py"),
        _msg("assistant", "I'll run the tests first."),                 # the stall (plan)
        _msg("user", LEVAIN_ACT_NUDGE),                                 # synthetic nudge (source=user)
        _msg("assistant", "Fixed the parity bug; all 3 tests pass."),   # the rescued work
    ])
    vagus_run(conv, firing=firing)
    assert len(firing.captured) == 1
    content = firing.captured[0].content
    assert "Fixed the parity bug; all 3 tests pass." in content        # the WORK is captured
    assert LEVAIN_ACT_NUDGE not in content                             # the nudge is not a [user] line


def test_vagus_run_double_capture_across_a_nudge_drops_the_work():
    """WHY the fix captures once, not once-per-run: capturing after the PLAN (turn 1) sets the turn
    marker to the real user-message id; a second capture after NUDGE+WORK computes the SAME id (the
    nudge is skipped by `is_corrective_nudge`) and no-ops — so the old two-capture pattern kept only
    the stall and dropped the work. This locks that idempotency behavior in so the loop must never
    revert to capturing before the nudge cycle settles."""
    from levain.firing.agent_reply import LEVAIN_ACT_NUDGE

    firing = StubFiring()
    conv = _FakeConversation([
        _msg("user", "fix the bug in calc.py"),
        _msg("assistant", "I'll run the tests first."),
    ])
    vagus_run(conv, firing=firing)  # eager capture after the PLAN (the OLD behavior)
    conv._state.events = [
        *conv._state.events,
        _msg("user", LEVAIN_ACT_NUDGE),
        _msg("assistant", "Fixed; all pass."),
    ]
    vagus_run(conv, firing=firing)  # capture after the WORK — NO-OPS (same turn id)
    assert len(firing.captured) == 1
    assert "I'll run the tests first." in firing.captured[0].content
    assert "Fixed; all pass." not in firing.captured[0].content  # the work was dropped — the bug


def test_vagus_run_failed_capture_stays_retryable():
    """codex MED-3: a fail-soft-swallowed write (capture()->False) must NOT set the dedup marker,
    so the turn is retried on the next vagus_run instead of being silently lost."""

    class _FailFiring:
        def __init__(self):
            self.calls = 0

        def capture(self, req):
            self.calls += 1
            return False  # fail-soft: logged + swallowed inside a real firing

    fail = _FailFiring()
    conv = _FakeConversation([_msg("user", "q"), _msg("assistant", "a")])
    vagus_run(conv, firing=fail)
    vagus_run(conv, firing=fail)  # same turn, but the first write failed → retry, not skip
    assert fail.calls == 2  # retried (would be 1 if the marker had been set on failure)


def test_capture_returns_bool():
    firing = StubFiring()
    assert firing.capture(CaptureRequest(content="x")) is True


def test_vagus_run_explicit_session_id_overrides_conv_id():
    firing = StubFiring()
    conv = _FakeConversation([_msg("user", "q"), _msg("assistant", "a")])
    vagus_run(conv, firing=firing, session_id="explicit-sess")
    assert firing.captured[0].session_id == "explicit-sess"


def test_vagus_run_default_firing_kind_is_anneal(monkeypatch):
    """capture is a WRITE, so the default firing_kind must be 'anneal' (persist), not 'stub'
    (which would no-op into memory and silently vanish)."""
    built: list[str] = []

    class _SpyFiring:
        def capture(self, req):
            built.append("captured")

    monkeypatch.setattr(
        "levain.firing.openhands.capture.build_firing",
        lambda kind: built.append(kind) or _SpyFiring(),
    )
    conv = _FakeConversation([_msg("user", "q"), _msg("assistant", "a")])
    vagus_run(conv)  # no firing, no kind → default
    assert "anneal" in built  # built the anneal firing by default


# --- L4 end-to-end: vagus_run lands a REAL episode through the real OpenHands path -------


def _ollama_up() -> bool:
    try:
        socket.create_connection(("localhost", 11434), timeout=2).close()
        return True
    except OSError:
        return False


@pytest.mark.skipif(not _ollama_up(), reason="local Ollama :11434 not available")
def test_l4_vagus_run_lands_real_episode(tmp_path, monkeypatch):
    """The delivered-cure proof: a REAL model turn through ``vagus_run`` → ``render_turn`` →
    ``AnnealFiring.capture`` → ``Store.record`` lands an episode in a REAL anneal store, read
    back FROM DISK (the un-fakeable oracle — not the model's self-report). Uses a tmp store
    (VAGUS_EPISODIC_PATH), never the real ~/.anneal-memory."""
    from anneal_memory import Store
    from openhands.sdk import LLM, Agent, Conversation

    db = tmp_path / "memory.db"
    monkeypatch.setenv("VAGUS_EPISODIC_PATH", str(db))
    llm = LLM(
        model="openai/minimax-m3:cloud",
        base_url="http://localhost:11434/v1",
        api_key="ollama",
        drop_params=True,
        usage_id="cap-l4",
        temperature=0.0,
    )
    agent = Agent(llm=llm, tools=[], include_default_tools=[])
    with tempfile.TemporaryDirectory() as wd:
        conv = Conversation(agent, workspace=wd, visualizer=None)
        conv.send_message("Reply with exactly the single word PONG.")
        vagus_run(conv, firing_kind="anneal")  # production path: real anneal capture

    with Store(db) as store:
        episodes = store.episodes_since_wrap()
    captured = [e for e in episodes if e.source == "vagus"]
    assert captured, "no vagus episode landed in the store"
    assert any("PONG" in e.content.upper() for e in captured), [e.content for e in captured]
    assert any("[user]" in e.content and "[assistant]" in e.content for e in captured)  # raw turn
