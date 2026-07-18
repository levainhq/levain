"""levain.run — the `levain run` REPL for a sovereign entity (spore-277 step 3).

Splits into two tiers:
  - PURE / guard tests (no openhands extra): `_resolve_model`, `_latest_agent_text`, and the
    pre-flight guards `run_entity` runs BEFORE it ever imports the OpenHands SDK.
  - openhands-gated: the sovereignty-guard refusal path (needs a real `LLM` + the chokepoint),
    skipped cleanly where the extra is absent.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.firing.agent_reply import humanize_finish_json, tool_action_summary
from levain.run import (
    _latest_agent_text,
    _resolve_llm_kwargs,
    _resolve_model,
    _turn_tool_activity,
    run_entity,
)


def _openhands_entity(tmp_path: Path, name: str = "ent") -> Path:
    """A dir that passes `run_entity`'s pre-flight guards: has `.levain/` AND the openhands
    adapter marker (a real `levain init --adapter openhands` writes both)."""
    d = tmp_path / name
    (d / ".levain").mkdir(parents=True)
    (d / ".levain" / "config.json").write_text(json.dumps({"adapter": "openhands"}))
    return d


# ---------- pure helpers (no openhands) ----------

def test_resolve_model_prefixes_bare_name_with_ollama():
    assert _resolve_model("minimax-m3:cloud") == "ollama/minimax-m3:cloud"


def test_resolve_model_passes_through_provider_prefixed():
    assert _resolve_model("ollama/kimi-k2.6:cloud") == "ollama/kimi-k2.6:cloud"
    assert _resolve_model("openai/gpt-4o") == "openai/gpt-4o"


def test_resolve_llm_kwargs_routes_bare_ollama_via_v1_native():
    # Bake-off 2026-07-17: a bare (Ollama) model routes through the OpenAI-compatible /v1 endpoint with
    # NATIVE tool-calling — the exact config the harness proved reliable (glm/kimi/minimax 10/10).
    kw = _resolve_llm_kwargs("glm-5.2:cloud", "http://localhost:11434", None)
    assert kw == {
        "model": "openai/glm-5.2:cloud",
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",
        "native_tool_calling": True,
    }


def test_resolve_llm_kwargs_reroutes_an_explicit_ollama_prefix():
    # An explicit `ollama/…` name is the SAME open path — strip the prefix and route via /v1 (a bare
    # `ollama/` provider call is exactly the JSON-text-stall route we are leaving).
    kw = _resolve_llm_kwargs("ollama/kimi-k2.7-code:cloud", "http://localhost:11434", None)
    assert kw["model"] == "openai/kimi-k2.7-code:cloud"
    assert kw["base_url"].endswith("/v1")
    assert kw["native_tool_calling"] is True


def test_resolve_llm_kwargs_honors_a_provider_model_as_is():
    # A capable native-FC caller (openai/…, anthropic/…) is honored verbatim — NOT re-prefixed, NOT
    # /v1-rerouted, and its base_url/api_key pass through untouched.
    kw = _resolve_llm_kwargs("anthropic/claude-opus-4-8", "http://localhost:11434", "sk-xyz")
    assert kw == {
        "model": "anthropic/claude-opus-4-8",
        "base_url": "http://localhost:11434",
        "api_key": "sk-xyz",
        "native_tool_calling": True,
    }


def test_resolve_llm_kwargs_does_not_double_suffix_v1():
    # An operator base_url that already ends in /v1 is not doubled.
    kw = _resolve_llm_kwargs("minimax-m3:cloud", "http://host:11434/v1", None)
    assert kw["base_url"] == "http://host:11434/v1"


def test_resolve_llm_kwargs_rejects_a_prefix_only_or_empty_model():
    # L3 2026-07-17: a prefix-only ("ollama/") or empty --model would resolve to "openai/" — invalid,
    # failing on the first turn. Fail CLOSED at resolution (run_entity's except → clean exit 2).
    import pytest as _pytest
    for bad in ("ollama/", "", "  "):
        with _pytest.raises(ValueError, match="must name a model"):
            _resolve_llm_kwargs(bad, "http://localhost:11434", None)


def test_resolve_llm_kwargs_keeps_an_explicit_empty_api_key():
    # `api_key is not None` (not truthiness): a deliberately-empty key is preserved; only an UNSET
    # key falls back to the Ollama sentinel.
    assert _resolve_llm_kwargs("glm-5.2:cloud", "http://localhost:11434", "")["api_key"] == ""
    assert _resolve_llm_kwargs("glm-5.2:cloud", "http://localhost:11434", None)["api_key"] == "ollama"


class _Content:
    def __init__(self, text):
        self.text = text


class _Msg:
    def __init__(self, role, texts):
        self.role = role
        self.content = [_Content(t) for t in texts]


class _Event:
    """A minimal stand-in for an OpenHands MessageEvent (source + llm_message)."""

    def __init__(self, source, texts):
        self.source = source
        self.llm_message = _Msg(source, texts)


class _Action:
    def __init__(self, kind, message):
        self.kind = kind
        self.message = message


class _FinishEvent:
    """An ActionEvent(FinishAction) stand-in — no llm_message; the reply is action.message.
    This is how a no-tool agent answer actually arrives from the SDK (the built-in finish)."""

    def __init__(self, message, kind="FinishAction", source="agent"):
        self.source = source
        self.llm_message = None
        self.action = _Action(kind, message)


class _ToolAction:
    def __init__(self, kind, command, path):
        self.kind = kind
        self.command = command
        self.path = path


class _ToolEvent:
    """An agent ActionEvent that is a REAL tool call — has ``tool_name`` + a non-builtin action kind,
    so ``tool_action_summary`` returns non-None (the "it DID act this turn" signal)."""

    def __init__(self, tool_name="terminal", kind="ExecuteBashAction",
                 command="python3 test.py", path=None, source="agent"):
        self.source = source
        self.tool_name = tool_name
        self.llm_message = None
        self.action = _ToolAction(kind, command, path)


def test_latest_agent_text_joins_agent_messages_of_latest_turn():
    events = [
        _Event("user", ["earlier"]),
        _Event("agent", ["earlier reply"]),
        _Event("user", ["help me think"]),
        _Event("agent", ["first part"]),
        _Event("agent", ["second part"]),
    ]
    assert _latest_agent_text(events) == "first part\nsecond part"


def test_latest_agent_text_excludes_non_agent_sources():
    # system / environment (the vagus inject rides source="environment") must never echo back.
    events = [
        _Event("user", ["q"]),
        _Event("environment", ["INJECTED RECALL — do not echo"]),
        _Event("agent", ["the real answer"]),
        _Event("system", ["framing"]),
    ]
    assert _latest_agent_text(events) == "the real answer"


def test_latest_agent_text_extracts_finish_action_reply():
    # The common no-tool shape: the agent answers via the built-in finish tool.
    events = [_Event("user", ["say pong"]), _FinishEvent("PONG")]
    assert _latest_agent_text(events) == "PONG"


def test_latest_agent_text_ignores_non_finish_actions():
    # A real tool action (not a finish) is not assistant text → no reply surfaced.
    events = [_Event("user", ["do it"]), _FinishEvent("rm -rf", kind="ExecuteBashAction")]
    assert _latest_agent_text(events) is None


def test_latest_agent_text_none_when_no_agent_reply_yet():
    assert _latest_agent_text([_Event("user", ["just asked"])]) is None


def test_latest_agent_text_none_on_empty():
    assert _latest_agent_text([]) is None


# ---------- narrate-without-act backstop (spore-358 follow-through) ----------

def test_planned_without_acting_fires_on_a_zero_tool_plan_finish():
    # The dominant weak-model stall: the agent "finishes" with a plan and takes no action.
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["Fix the bug in calc.py so all tests pass."]),
        _FinishEvent("I'll run the tests and read the source file to diagnose the bug."),
    ]
    assert planned_without_acting(events) is True


def test_planned_without_acting_fires_on_a_zero_tool_plan_message():
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["Fix the bug."]),
        _Event("agent", ["Let me start by running the tests, then read the code and fix it."]),
    ]
    assert planned_without_acting(events) is True


def test_planned_without_acting_fires_on_a_curly_apostrophe_plan():
    # Measured: kimi-k2.7-code writes "I’ll" with U+2019, not a straight "'"; the detector must
    # normalize it or the backstop silently misses the stall (bake-off 2026-07-17).
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["Fix the bug."]),
        _FinishEvent("I’ll run the tests first to see the failure, then inspect the source file."),
    ]
    assert planned_without_acting(events) is True


def test_planned_without_acting_false_when_the_agent_actually_acted():
    # It ran a tool this turn → not a stall, even if the final message reads plan-ish.
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["Fix the bug."]),
        _ToolEvent(tool_name="terminal", command="python3 test_calc.py"),
        _FinishEvent("All tests pass now. The bug was a parity error; I'll note the float edge too."),
    ]
    assert planned_without_acting(events) is False


def test_planned_without_acting_false_on_a_plain_answer():
    # A genuine no-tool ANSWER (no forward-intent opener) must not be nudged.
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["Who are you?"]),
        _FinishEvent("I'm Ember, your sovereign coding partner running on an open model."),
    ]
    assert planned_without_acting(events) is False


def test_planned_without_acting_false_on_conversational_and_clarifying_replies():
    """L1+L2 false-positive fix: a conceptual answer or a clarifying question (zero tools, opening with
    a bare 'let me'/'I'll'/'I need to') must NOT be nudged — the markers require a tool-like ACTION
    verb, and any reply containing a '?' is treated as a legitimate pause. Nudging a clarifying
    question would override the agent's correct decision to wait for the human."""
    from levain.firing.agent_reply import planned_without_acting
    cases = [
        "Let me explain how the median function works.",
        "Let me know if you'd like anything else changed.",
        "Let me clarify — do you mean calc.py or calc_utils.py?",   # clarifying question
        "I need to know which file you mean. Which one?",           # clarifying question
        "I'll be happy to help whenever you're ready.",
        "I'll summarize the architecture: it has three layers.",
        "Let's see — the answer to your question is 42.",
    ]
    for reply in cases:
        events = [_Event("user", ["a task"]), _FinishEvent(reply)]
        assert planned_without_acting(events) is False, reply


def test_planned_without_acting_false_when_no_reply_yet():
    from levain.firing.agent_reply import planned_without_acting
    assert planned_without_acting([_Event("user", ["fix it"])]) is False


def test_planned_without_acting_keys_on_the_current_turn_only():
    # A tool action in a PRIOR turn does not suppress a stall in the current one.
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["earlier task"]),
        _ToolEvent(tool_name="terminal"),
        _FinishEvent("done earlier."),
        _Event("user", ["now fix calc.py"]),
        _FinishEvent("I'll run the tests and read the file."),
    ]
    assert planned_without_acting(events) is True


def test_is_corrective_nudge_matches_both_sdk_and_levain_markers():
    from levain.firing.agent_reply import (
        CORRECTIVE_NUDGE_MARKER,
        LEVAIN_ACT_NUDGE,
        is_corrective_nudge,
    )
    assert is_corrective_nudge(_Event("user", [LEVAIN_ACT_NUDGE])) is True
    assert is_corrective_nudge(
        _Event("user", [f"...{CORRECTIVE_NUDGE_MARKER}..."])
    ) is True
    assert is_corrective_nudge(_Event("user", ["a real human question"])) is False


def test_is_corrective_nudge_matches_the_levain_marker_only_as_a_prefix():
    # codex + gpt-oss L3 2026-07-17: the levain nudge is a fixed string WE emit — match it by prefix,
    # not substring, so a genuine user turn that merely QUOTES the marker (a support question, a pasted
    # transcript) is not reclassified as synthetic and silently dropped from the boundary / capture /
    # recall / display. The SDK fragment stays substring-matched (its own long distinctive text).
    from levain.firing.agent_reply import LEVAIN_ACT_NUDGE_MARKER, is_corrective_nudge
    quoted = f"Why did {LEVAIN_ACT_NUDGE_MARKER} appear in my transcript?"
    assert is_corrective_nudge(_Event("user", [quoted])) is False


def test_planned_without_acting_fires_on_a_plan_opener_then_a_filler_line():
    # codex L3 2026-07-17 (unique catch): a zero-tool turn that OPENS with a plan and TRAILS a filler
    # line is still the narrate-without-act stall. The detector must key on the turn's OPENING move —
    # keying on the LAST text fragment ("One moment.") let this real stall slip past the backstop.
    from levain.firing.agent_reply import planned_without_acting
    events = [
        _Event("user", ["fix calc.py"]),
        _Event("agent", ["I'll run the tests first to see the failure."]),  # opening move: a plan
        _Event("agent", ["One moment."]),                                   # filler, still no action
    ]
    assert planned_without_acting(events) is True


def test_planned_without_acting_ignores_a_prior_levain_nudge_as_boundary():
    # A levain act-nudge in the history is synthetic — the boundary must skip it so the detector keys
    # on the real user turn (else a post-nudge acted turn could be mis-read).
    from levain.firing.agent_reply import LEVAIN_ACT_NUDGE, planned_without_acting
    events = [
        _Event("user", ["fix calc.py"]),
        _FinishEvent("I'll run the tests first."),        # stall
        _Event("user", [LEVAIN_ACT_NUDGE]),               # synthetic nudge (source=user)
        _ToolEvent(tool_name="terminal"),                 # then it acted
        _FinishEvent("All tests pass."),
    ]
    # boundary = the real user msg; the turn as a whole DID act → not a stall.
    assert planned_without_acting(events) is False


# ---------- spore-297: finish/think-as-JSON-text reply (observed live on minimax-m3) ----------

# The EXACT shape observed live (2026-07-09): the model emits its think + finish tool calls as
# concatenated JSON TEXT in a MessageEvent instead of structured tool calls.
_FINISH_JSON_REPLY = (
    '{"name": "think", "arguments": {"summary": "s", "thought": "the scratchpad"}}\n'
    '{"name": "finish", "arguments": {"summary": "s", "message": "Can\'t do it, Phill — straight up."}}'
)


def test_humanize_finish_json_extracts_message_and_drops_think():
    assert humanize_finish_json(_FINISH_JSON_REPLY) == "Can't do it, Phill — straight up."


def test_humanize_finish_json_single_finish_object():
    blob = '{"name": "finish", "arguments": {"message": "hello there"}}'
    assert humanize_finish_json(blob) == "hello there"


def test_humanize_finish_json_leaves_normal_reply_untouched():
    assert humanize_finish_json("Done. Created hello.txt in your workspace.") == (
        "Done. Created hello.txt in your workspace."
    )


def test_humanize_finish_json_leaves_json_with_trailing_prose_untouched():
    # A legit reply that merely CONTAINS a JSON snippet (trailing prose) is never mangled.
    text = '{"answer": 42} is the JSON you asked for.'
    assert humanize_finish_json(text) == text


def test_humanize_finish_json_leaves_non_finish_json_untouched():
    # A pure JSON answer that is not a finish tool call is preserved as-is (not eaten).
    text = '{"answer": 42}'
    assert humanize_finish_json(text) == text


def test_latest_agent_text_unwraps_finish_json_reply():
    # The fix reaches the display path: an agent MessageEvent carrying the JSON blob renders the
    # finish message, not raw JSON.
    events = [_Event("user", ["read ../notes"]), _Event("agent", [_FINISH_JSON_REPLY])]
    assert _latest_agent_text(events) == "Can't do it, Phill — straight up."


# ---------- tool-activity render (step 6 — the entity's hands are visible) ----------


class _FileAction:
    def __init__(self, command, path, kind="FileEditorAction"):
        self.command = command
        self.path = path
        self.kind = kind


class _ActionEvent:
    """An ActionEvent stand-in — a real tool call (tool_name + action), not a message."""

    def __init__(self, tool_name, action, source="agent"):
        self.source = source
        self.tool_name = tool_name
        self.action = action
        self.llm_message = None


def test_tool_action_summary_reads_command_and_path():
    ev = _ActionEvent("file_editor", _FileAction("create", "/ws/plan.md"))
    assert tool_action_summary(ev) == ("file_editor", "create /ws/plan.md")


def test_tool_action_summary_ignores_finish():
    # The finish IS the reply (surfaced by finish_message), not tool activity.
    assert tool_action_summary(_FinishEvent("done")) is None


def test_tool_action_summary_ignores_message_event():
    assert tool_action_summary(_Event("agent", ["just talking"])) is None


def test_tool_action_summary_ignores_think_builtin():
    # The SDK auto-adds `think` to EVERY agent (even tools=None) — it's the model's scratchpad, not
    # workspace activity. Rendering it would spam `⚙ think: ThinkAction` and, in --no-tools, would
    # contradict the "tools: none" banner (apparatus L1 note #3).
    assert tool_action_summary(_ActionEvent("think", _FileAction("", "", kind="ThinkAction"))) is None


def test_turn_tool_activity_shows_this_turns_ops_workspace_relative(tmp_path):
    ws = tmp_path / "ws"
    events = [
        _Event("user", ["earlier"]),
        _ActionEvent("file_editor", _FileAction("create", f"{ws}/old.md")),
        _Event("user", ["now edit it"]),  # the latest turn starts here
        _ActionEvent("file_editor", _FileAction("str_replace", f"{ws}/plan.md")),
        _FinishEvent("done"),
    ]
    lines = _turn_tool_activity(events, ws)
    # Only THIS turn's action, path shown relative to the workspace, the finish excluded.
    assert lines == ["⚙ file_editor: str_replace plan.md"]


def test_turn_tool_activity_empty_for_a_pure_conversation_turn(tmp_path):
    events = [_Event("user", ["hi"]), _FinishEvent("hello")]
    assert _turn_tool_activity(events, tmp_path / "ws") == []


# ---------- pre-flight guards (run BEFORE the openhands import) ----------

def test_run_entity_refuses_non_entity_dir(tmp_path: Path, capsys):
    # A dir with no .levain/ is not an initialized entity → exit 2, friendly hint, and it
    # NEVER reaches the openhands import (so this passes even without the extra).
    rc = run_entity(tmp_path / "not-an-entity")
    assert rc == 2
    out = capsys.readouterr().out
    assert "not an initialized Levain entity" in out
    assert "levain init --adapter openhands" in out


def test_run_entity_missing_extra_is_a_friendly_hint(tmp_path: Path, monkeypatch, capsys):
    # A valid entity dir, but the openhands import fails → exit 2 + install hint, no traceback.
    entity = _openhands_entity(tmp_path)  # marker present → reaches the (mocked) import
    import builtins

    real_import = builtins.__import__

    def _no_openhands(name, *a, **k):
        if name.startswith("openhands") or name == "levain.firing.openhands.entity":
            raise ImportError("No module named 'openhands'")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _no_openhands)
    rc = run_entity(entity)
    assert rc == 2
    assert "pip install 'levain[openhands]'" in capsys.readouterr().out


def test_run_entity_refuses_store_without_openhands_marker(tmp_path: Path, capsys):
    # A .levain/ store that is NOT an openhands entity (no marker — e.g. a claude-code/codex
    # install) is refused BEFORE the openhands import, so `levain run` never drives an
    # OpenHands agent against another adapter's store. Pure guard — no extra needed.
    entity = tmp_path / "ent"
    (entity / ".levain").mkdir(parents=True)  # store, but no adapter marker
    rc = run_entity(entity)
    assert rc == 2
    out = capsys.readouterr().out
    assert "not a clean OpenHands entity" in out
    assert "levain init --adapter openhands" in out


def test_run_entity_refuses_residue_bearing_openhands_marker(tmp_path: Path, capsys):
    # The RESIDUAL fix: an openhands marker sitting on top of hosted residue (a --force
    # adapter switch left a stale marker + a CLAUDE.md) is NOT a clean entity — `run` uses
    # the shared `effective_adapter` (files dominate the stale marker) and refuses, agreeing
    # with doctor + verify. Pure guard — no extra needed.
    entity = _openhands_entity(tmp_path)  # openhands marker...
    (entity / "CLAUDE.md").write_text("# stale claude residue")  # ...on top of hosted residue
    rc = run_entity(entity)
    assert rc == 2
    assert "not a clean OpenHands entity" in capsys.readouterr().out


# ---------- sovereignty-guard refusal (needs the openhands extra — this test ONLY) ----------
# NB: the skip is per-test, NOT module-scope, so the pure guard tier above always runs
# (that tier proves the pre-flight sovereignty guards fire WITHOUT the SDK — the exact thing
# a module-level importorskip would silently skip).


@pytest.fixture
def _clean_entity_env(monkeypatch):
    from levain.firing.isolation import LEVAIN_ENTITY_DIR_ENV

    monkeypatch.delenv(LEVAIN_ENTITY_DIR_ENV, raising=False)


def test_run_entity_returns_2_on_isolation_refusal(
    tmp_path: Path, monkeypatch, capsys, _clean_entity_env
):
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    from levain.firing.isolation import IsolationError

    entity = _openhands_entity(tmp_path)

    def _refuse(*_a, **_k):
        raise IsolationError("store would reach the flow laptop memory")

    monkeypatch.setattr("levain.firing.openhands.entity.build_entity_agent", _refuse)
    rc = run_entity(entity)
    assert rc == 2
    assert "sovereignty guard REFUSED" in capsys.readouterr().out


def _spy_build_entity_agent(monkeypatch, captured):
    """Spy `build_entity_agent` to capture the `tools=` it was handed, then bail out early (raise
    IsolationError → run_entity returns 2), so we assert the tool-building branch WITHOUT a live
    model."""
    from levain.firing.isolation import IsolationError

    def _spy(entity_dir, llm, *, tools=None, **_kw):
        captured["tools"] = tools
        captured["llm"] = llm
        raise IsolationError("captured — stop here")

    monkeypatch.setattr("levain.firing.openhands.entity.build_entity_agent", _spy)


def test_run_entity_passes_confined_tools_by_default(
    tmp_path: Path, monkeypatch, capsys, _clean_entity_env
):
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    # Force the OS-floor-present branch so this is platform-independent — the entity gets BOTH confined
    # hands (file editor + sandboxed bash).
    monkeypatch.setattr("levain.run.confinement_supported", lambda: True)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    rc = run_entity(entity, with_tools=True)
    assert rc == 2
    assert captured["tools"] is not None
    assert [t.name for t in captured["tools"]] == ["levain_file_editor", "levain_bash"]


def test_run_entity_drops_bash_without_an_os_sandbox(
    tmp_path: Path, monkeypatch, capsys, _clean_entity_env
):
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    # No OS confinement floor → bash is dropped, the file-editor hand stays (honesty floor: NEVER an
    # unconfined shell as a fallback).
    monkeypatch.setattr("levain.run.confinement_supported", lambda: False)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    rc = run_entity(entity, with_tools=True)
    assert rc == 2
    assert [t.name for t in captured["tools"]] == ["levain_file_editor"]


def test_run_entity_no_tools_passes_none(
    tmp_path: Path, monkeypatch, capsys, _clean_entity_env
):
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    rc = run_entity(entity, with_tools=False)
    assert rc == 2
    assert captured["tools"] is None  # --no-tools → a pure conversational partner


def test_run_entity_routes_ollama_via_v1_native_tool_calling(
    tmp_path: Path, monkeypatch, _clean_entity_env
):
    """Bake-off 2026-07-17: the default (Ollama) path now routes through Ollama's OpenAI-compatible
    /v1 endpoint with NATIVE tool-calling — the `ollama/` litellm provider dropped glm/kimi tool-calls
    to JSON-text (turn ends), a ROUTE artifact the /v1 endpoint fixes (structured tool_calls; glm/kimi/
    minimax all 10/10). Pins the flag against silent SDK drift (`LLM.model_config` is `extra: ignore`,
    so a rename would swallow the kwarg unnoticed) AND the route (a regression to `ollama/` reverts the
    JSON-text stall)."""
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    assert run_entity(entity) == 2  # default model minimax-m3:cloud → openai/…:cloud via /v1, native
    llm = captured["llm"]
    assert llm.native_tool_calling is True
    assert llm.model == "openai/minimax-m3:cloud"           # /v1 OpenAI-compat route, not ollama/
    assert str(llm.base_url).rstrip("/").endswith("/v1")     # the /v1 endpoint, not the bare host


def test_run_entity_prefix_only_model_exits_2_cleanly(
    tmp_path: Path, monkeypatch, capsys, _clean_entity_env
):
    """A prefix-only --model (`ollama/`) fails CLOSED at startup: `_resolve_llm_kwargs` raises, the
    construction guard converts it to a clean exit 2 with the --model hint — not a first-turn crash."""
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    rc = run_entity(entity, model="ollama/")
    assert rc == 2
    assert "could not start the entity" in capsys.readouterr().out


def test_run_entity_keeps_native_tool_calling_for_a_provider_model(
    tmp_path: Path, monkeypatch, _clean_entity_env
):
    """A strong native-FC model an operator points --model at (openai/…, anthropic/…) is honored
    as-is with native FC — the Ollama /v1 reroute is scoped to bare/ollama models, never forced on a
    capable provider caller (L2 review F2)."""
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    assert run_entity(entity, model="openai/gpt-5.5") == 2
    llm = captured["llm"]
    assert llm.native_tool_calling is True
    assert llm.model == "openai/gpt-5.5"  # honored as-is — not re-prefixed, not /v1-rerouted


# ---------- the honesty-floor banner ----------


class _FakeBinding:
    def __init__(self, tmp_path: Path) -> None:
        self.episodic_path = tmp_path / ".levain" / "memory.db"
        self.crystal_path = tmp_path / ".levain" / "memory.crystal.json"


def test_banner_shows_both_hands_and_the_crown_jewels_floor(tmp_path: Path, capsys):
    from levain.run import _print_banner

    _print_banner(
        tmp_path, _FakeBinding(tmp_path), model="ollama/minimax-m3:cloud",
        with_tools=True, bash_ok=True, ssh_mode="agent",
    )
    out = capsys.readouterr().out
    assert "file_editor + terminal (bash)" in out
    assert "crown-jewels floor" in out
    # the honesty floor names exactly what it denies
    assert ".anneal-memory/" in out and "flow store" in out
    assert "~/.ssh key material" in out       # agent mode → keys usable, not readable
    assert "confinement.json" in out


def test_banner_ssh_line_reflects_raw_mode_not_a_static_lie(tmp_path: Path, capsys):
    # apparatus L1: under ssh_mode="raw" the floor does NOT protect ~/.ssh — the banner (the operator's
    # ground truth) must SAY so, not statically claim protection.
    from levain.run import _print_banner

    _print_banner(
        tmp_path, _FakeBinding(tmp_path), model="ollama/minimax-m3:cloud",
        with_tools=True, bash_ok=True, ssh_mode="raw",
    )
    out = capsys.readouterr().out
    assert "ssh_mode=raw" in out and "NOT confined" in out
    assert "key material (agent-auth" not in out  # the protective claim is NOT shown under raw


def test_banner_says_bash_dropped_without_a_sandbox(tmp_path: Path, capsys):
    from levain.run import _print_banner

    _print_banner(
        tmp_path, _FakeBinding(tmp_path), model="ollama/minimax-m3:cloud",
        with_tools=True, bash_ok=False, ssh_mode="agent",
    )
    out = capsys.readouterr().out
    assert "bash dropped" in out
    assert "terminal (bash)" not in out  # not offered when there's no floor
    assert "crown-jewels floor" in out   # the file editor still rides the floor


def test_run_entity_rejects_a_malformed_confinement_config(
    tmp_path: Path, monkeypatch, capsys, _clean_entity_env
):
    # apparatus L1/L2: a malformed .levain/confinement.json fails CLOSED at startup with a clear,
    # crown-jewels-specific message (not a stack trace on the first turn).
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    (entity / ".levain" / "confinement.json").write_text("{ not json")
    rc = run_entity(entity, with_tools=True)
    assert rc == 2
    assert "confinement config is invalid" in capsys.readouterr().out
