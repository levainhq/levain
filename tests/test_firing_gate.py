"""The EFFERENT GATE's pure classifier — K3, spore-295.

BASE LANE on purpose: no ``importorskip``. :mod:`levain.firing.gate` is a stdlib-only leaf
precisely so the rule that decides what an entity may do to the world can be exercised
exhaustively without standing up a conversation, a model, or the OpenHands extra. If these tests
ever need the SDK, the leaf has stopped being a leaf.

The adapter that mounts this on the runtime is tested in ``test_firing_gate_openhands.py``.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.firing.confinement import ConfinementError, load_confinement_config
from levain.firing.gate import (
    AFFERENT_FILE_EDITOR_COMMANDS,
    BASH_TOOL_NAMES,
    FILE_EDITOR_TOOL_NAMES,
    GATE_SETTINGS,
    ActionClass,
    PendingEfferent,
    classify_action,
    resolve_gate_mode,
)
from levain.session import (
    EXIT_GATED,
    EXIT_NO_REPLY,
    EXIT_OK,
    EXIT_TURN_FAILED,
    TurnResult,
)

# The file editor's FULL upstream command vocabulary (openhands CommandLiteral). Written out
# rather than imported so this test is a base-lane statement about what we believe the tool can
# do — if upstream adds a command, the allow-list test below is what notices.
ALL_FILE_EDITOR_COMMANDS = ("view", "create", "str_replace", "insert", "undo_edit")


# ---------- the tool-name tripwire (the near-miss this test exists for) ----------

def test_classifier_matches_the_STOCK_tool_names_an_action_event_actually_carries():
    """THE TRIPWIRE. The confined hands are REGISTERED as ``levain_file_editor`` / ``levain_bash``
    but keep the STOCK ``.name`` (``file_editor`` / ``terminal``) so a weak open model sees a
    familiar function name — and an ``ActionEvent`` carries ``tool_call.name``, i.e. the STOCK
    one.

    A classifier that matched only the registry keys would therefore never match a real action.
    It would still LOOK correct: everything would fall through to the fail-closed default, so
    "bash gates" would pass, and the only visible symptom would be `view` gating too — a
    usability nuisance nobody traces back to a broken lookup. Pin ``recognized`` (not just the
    class) so the fallthrough cannot masquerade as a hit."""
    bash = classify_action("terminal", {"command": "ls"})
    assert bash.action_class is ActionClass.EFFERENT
    assert bash.recognized is True, "matched by fallthrough, not by name — the tripwire fired"

    view = classify_action("file_editor", {"command": "view", "path": "/tmp/x"})
    assert view.action_class is ActionClass.AFFERENT
    assert view.recognized is True

    assert "terminal" in BASH_TOOL_NAMES
    assert "file_editor" in FILE_EDITOR_TOOL_NAMES


@pytest.mark.parametrize("name", sorted(BASH_TOOL_NAMES))
def test_bash_is_efferent_under_every_spelling(name):
    c = classify_action(name, {"command": "ls -la"})
    assert c.action_class is ActionClass.EFFERENT
    assert c.recognized is True


def test_bash_is_efferent_by_decision_not_by_fallthrough():
    """``ls`` is as gated as ``rm -rf`` — and the REASON says why, because "bash always fans in"
    and "unrecognized tool" are the same decision and completely different news."""
    harmless = classify_action("terminal", {"command": "ls"})
    catastrophic = classify_action("terminal", {"command": "rm -rf /"})
    assert harmless.action_class is catastrophic.action_class is ActionClass.EFFERENT
    assert harmless.reason == catastrophic.reason
    assert "always fans in" in harmless.reason
    assert harmless.recognized and catastrophic.recognized


def test_the_classifier_never_parses_the_command():
    """The gate must not acquire a shell parser. Commands that a naive classifier would 'obviously'
    call read-only get the SAME answer as the destructive ones — no allow-list of safe verbs, no
    quoting arms race to lose."""
    for cmd in ("ls", "cat x", "echo hi", "pwd", "$(rm -rf /)", "ls; rm -rf /", "eval $X"):
        assert classify_action("terminal", {"command": cmd}).action_class is ActionClass.EFFERENT


# ---------- the file editor ----------

@pytest.mark.parametrize("command", ALL_FILE_EDITOR_COMMANDS)
def test_only_view_is_afferent_across_the_whole_file_editor_vocabulary(command):
    c = classify_action("file_editor", {"command": command, "path": "/tmp/x"})
    expected = ActionClass.AFFERENT if command == "view" else ActionClass.EFFERENT
    assert c.action_class is expected, f"{command!r} classified {c.action_class}"


def test_undo_edit_is_efferent_because_restoring_is_still_writing():
    """The one that reads like a read. ``undo_edit`` changes the file on disk."""
    assert classify_action(
        "file_editor", {"command": "undo_edit", "path": "/tmp/x"}
    ).action_class is ActionClass.EFFERENT


@pytest.mark.parametrize("fields", [{}, {"command": None}, {"command": 7}, {"command": ""}])
def test_file_editor_without_a_readable_command_is_efferent(fields):
    """Fail-closed: an action whose command we cannot read must gate, never sail through as a
    view. The unreadable case is exactly where an attacker or a bug would live."""
    assert classify_action("file_editor", fields).action_class is ActionClass.EFFERENT


def test_afferent_commands_are_an_ALLOW_list_so_new_upstream_commands_default_to_gated():
    """If openhands adds a command tomorrow, it must arrive GATED, not afferent. Pinning the
    allow-list to exactly {"view"} is what makes the default safe rather than lucky."""
    assert AFFERENT_FILE_EDITOR_COMMANDS == frozenset({"view"})
    assert classify_action(
        "file_editor", {"command": "hypothetical_future_command"}
    ).action_class is ActionClass.EFFERENT


# ---------- fail-closed on the unknown ----------

@pytest.mark.parametrize(
    "name", ["some_mcp_tool", "browser", "execute_bash", "FileEditor", "  ", "", None, 42]
)
def test_an_unrecognized_tool_is_efferent_and_says_it_was_not_recognized(name):
    """``absence_of_signal_rendered_as_health`` is the way this gate would rot: someone adds a
    hand and the gate keeps quietly returning green. Unknown gates, AND flags itself."""
    c = classify_action(name, {})
    assert c.action_class is ActionClass.EFFERENT
    assert c.recognized is False
    assert "fail-closed" in c.reason


def test_finish_and_think_are_inert():
    """Turn control is neither perception nor action; gating it would halt every turn at its
    own ending."""
    for name in ("finish", "think"):
        c = classify_action(name, {})
        assert c.action_class is ActionClass.INERT
        assert c.recognized is True
        assert c.is_efferent is False


def test_case_and_whitespace_do_not_smuggle_a_tool_past_recognition():
    """A near-miss name must fail CLOSED (efferent) rather than be helpfully normalised into a
    match — normalising is how a lookalike tool would inherit an allow decision."""
    assert classify_action(" terminal ", {"command": "ls"}).recognized is True  # trimmed
    assert classify_action("TERMINAL", {"command": "ls"}).recognized is False   # not lowercased
    assert classify_action("Terminal", {}).action_class is ActionClass.EFFERENT


# ---------- mode resolution ----------

@pytest.mark.parametrize(
    ("setting", "human_present", "expected"),
    [
        ("auto", True, "ungated"),
        ("auto", False, "gated"),
        ("gated", True, "gated"),
        ("gated", False, "gated"),
        ("ungated", True, "ungated"),
        ("ungated", False, "ungated"),
    ],
)
def test_resolve_gate_mode_matrix(setting, human_present, expected):
    assert resolve_gate_mode(setting, human_present=human_present) == expected


def test_auto_binds_the_gate_to_the_ABSENCE_of_the_human():
    """The design claim, pinned: the gate supplies the fan-in that presence was already
    supplying. Unattended is the case K3 exists for, so unattended must be the gated one."""
    assert resolve_gate_mode("auto", human_present=False) == "gated"
    assert resolve_gate_mode("auto", human_present=True) == "ungated"


def test_an_unknown_setting_behaves_as_auto_rather_than_as_ungated():
    """Belt-and-braces for a direct caller (the config loader rejects unknowns first). The
    important half is that a garbage setting cannot resolve to UNGATED for an unattended run."""
    assert resolve_gate_mode("nonsense", human_present=False) == "gated"


# ---------- the config surface ----------

def _write_config(tmp_path: Path, payload) -> Path:
    store = tmp_path / ".levain"
    store.mkdir(parents=True, exist_ok=True)
    (store / "confinement.json").write_text(
        payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8"
    )
    return tmp_path


def test_efferent_gate_defaults_to_auto_when_absent(tmp_path: Path):
    assert load_confinement_config(tmp_path).efferent_gate == "auto"
    assert load_confinement_config(_write_config(tmp_path, {})).efferent_gate == "auto"


@pytest.mark.parametrize("value", ["auto", "gated", "ungated"])
def test_valid_efferent_gate_values_parse(tmp_path: Path, value):
    cfg = load_confinement_config(_write_config(tmp_path, {"efferent_gate": value}))
    assert cfg.efferent_gate == value


@pytest.mark.parametrize("value", ["gate", "GATED", "on", True, 1, None, ["gated"]])
def test_a_malformed_efferent_gate_fails_CLOSED(tmp_path: Path, value):
    """Held to the same standard as ssh_mode, and for the same reason: a mistyped governance
    declaration silently falling back to the default is a decision the operator believes they
    made and did not."""
    with pytest.raises(ConfinementError) as exc:
        load_confinement_config(_write_config(tmp_path, {"efferent_gate": value}))
    assert "efferent_gate" in str(exc.value)


def test_the_loader_and_the_gate_agree_on_the_vocabulary_by_construction(tmp_path: Path):
    """No second list to drift. Every GATE_SETTINGS value must load, and the error text must be
    generated from the same tuple."""
    for value in GATE_SETTINGS:
        assert load_confinement_config(
            _write_config(tmp_path, {"efferent_gate": value})
        ).efferent_gate == value


# ---------- TurnResult: what a halt reports ----------

def test_a_gated_turn_exits_4_not_1():
    """THE DISCRIMINATION THAT MATTERS. A halted turn has produced no reply — so without its own
    code it would surface as EXIT_NO_REPLY, and a supervisor would read "waiting for a human
    decision" as "the entity stalled". Those want opposite responses."""
    r = TurnResult(reply=None, tool_activity=[], gated=True)
    assert r.exit_code == EXIT_GATED == 4
    assert r.exit_code != EXIT_NO_REPLY


def test_a_gated_turn_is_not_ok_and_not_an_error():
    r = TurnResult(reply=None, tool_activity=[], gated=True)
    assert r.ok is False, "a halted turn has not completed"
    assert r.error is None, "the gate firing is the governor working, not a failure"
    assert r.gated is True


def test_error_dominates_a_gated_turn():
    """If the turn raised we cannot trust what the pending list even is."""
    r = TurnResult(reply=None, tool_activity=[], error="boom", gated=True)
    assert r.exit_code == EXIT_TURN_FAILED


def test_gated_dominates_a_reply():
    """A halt mid-turn can carry earlier assistant text. It is still a halt: the actions did not
    run, so reporting EXIT_OK would tell a supervisor the work is done."""
    r = TurnResult(reply="I'll push that for you", tool_activity=[], gated=True)
    assert r.exit_code == EXIT_GATED
    assert r.exit_code != EXIT_OK
    assert r.ok is False


def test_gated_is_AUTHORITY_and_pending_is_only_DESCRIPTION():
    """The coupling that must not exist. If `gated` were derived from `pending` being non-empty,
    any failure to DESCRIBE a halt would report the turn as complete — and the driver would then
    capture and exit on a turn whose actions never ran."""
    r = TurnResult(reply=None, tool_activity=[], gated=True, pending=())
    assert r.gated is True
    assert r.exit_code == EXIT_GATED


def test_an_ungated_turn_is_unaffected():
    """The default path must be untouched by K3 — an ungated REPL session still reports 0/1."""
    assert TurnResult(reply="hi", tool_activity=[]).exit_code == EXIT_OK
    assert TurnResult(reply=None, tool_activity=[]).exit_code == EXIT_NO_REPLY
    assert TurnResult(reply="hi", tool_activity=[]).ok is True


# ---------- the operator-facing report ----------

def test_pending_line_shows_the_ACT_then_the_reason():
    """The operator judges the command, not the tool's name. A line that showed
    ``TerminalAction`` would name the tool while withholding the only content they can weigh."""
    line = PendingEfferent(
        tool_name="terminal", detail="git push --force origin main",
        reason="bash always fans in", recognized=True,
    ).line()
    assert "git push --force origin main" in line
    assert "bash always fans in" in line


def test_an_unrecognized_pending_action_is_marked_for_the_operator():
    line = PendingEfferent(
        tool_name="mystery", detail="?", reason="unrecognized", recognized=False
    ).line()
    assert "⚠" in line, "an unrecognized tool must be visibly flagged in the decision"
