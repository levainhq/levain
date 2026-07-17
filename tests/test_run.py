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


def test_run_entity_uses_prompt_tool_calling_for_ollama(
    tmp_path: Path, monkeypatch, _clean_entity_env
):
    """spore-358: the default (Ollama) path forces PROMPT-based tool calling — native fn-calling is
    unreliable on open models and stalls a multi-step task after 1-2 actions. Pins the flag against
    silent SDK drift: `LLM.model_config` is `extra: ignore`, so a future rename would swallow the
    kwarg unnoticed and revert to the broken native mode (L1 review F2)."""
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    assert run_entity(entity) == 2  # default model minimax-m3:cloud → ollama/ → prompt mode
    assert captured["llm"].native_tool_calling is False


def test_run_entity_keeps_native_tool_calling_for_a_provider_model(
    tmp_path: Path, monkeypatch, _clean_entity_env
):
    """A strong native-FC model an operator points --model at (openai/…, anthropic/…) keeps its
    structured tool-call channel — prompt mode is scoped to the Ollama weakness, not forced on a
    capable caller (L2 review F2)."""
    pytest.importorskip("openhands.sdk", reason="openhands extra absent")
    entity = _openhands_entity(tmp_path)
    captured: dict = {}
    _spy_build_entity_agent(monkeypatch, captured)
    assert run_entity(entity, model="openai/gpt-5.5") == 2
    assert captured["llm"].native_tool_calling is True


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
