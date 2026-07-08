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

from levain.run import _latest_agent_text, _resolve_model, run_entity


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
