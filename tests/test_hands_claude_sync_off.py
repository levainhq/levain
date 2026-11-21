"""The hands Claude chat does not sync account skills or plugins and does not start built-in plugins (levain#139 family).

Evidence (Claude Code 2.1.329, fresh macOS VM, 11-20; project_memory/1118-vm/leg6_fresh_evidence/): ``plugins/marketplaces``,
``plugins/synced``, ``plugins/known_marketplaces.json`` and ``skills/synced`` were found in the hands home after the first
chat (easel_probe2.txt and easel_probe.txt), after which ``claude_user_layer`` refused every later chat; the two built-in ids
``cc-plugin-easel@builtin`` and ``claude-platform@builtin`` are in the hands ``.claude.json`` (easel_probe5.txt); a loopback
socket was listening, owned by the ``claude`` process only (easel_listener.txt). That the chat's ``/mcp`` listed ``easel``
was reported in the leg 6 route (levain-leg6 2, fanin log 14:30, read by Phill from the chat's ``/mcp``), not in the
evidence files. The names used to stop that are read from the binary and the vendor's references, NOT yet run on a hands
account (the acceptance is the second chat on the same home); these tests pin that the names are carried, once, by the one
start that is meant to carry them.

- the environment line rides the hands environment (``confinement._hands_env``), the one dict every hands start takes;
- the settings object is one constant and the chat argv passes it as ``--settings <compact JSON>``;
- no second builder passes ``--settings`` for the hands Claude chat (the codex chat is untouched).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain.firing import confinement, hands_chat, hands_mcp_login
from levain.firing.confinement import HandsIdentity
from tests.test_hands_claudeai_connectors_off import _env_assignments, _starts

NAME = "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL"
WANT = f"{NAME}=1"
CONNECTORS = "ENABLE_CLAUDEAI_MCP_SERVERS=false"


def _hands() -> HandsIdentity:
    return HandsIdentity("levain-hands", 4_000_018, "/Users/levain-hands", Path("/srv/ws"))


# (a) the environment ---------------------------------------------------------------------------------------------

def test_the_hands_environment_carries_both_lines_with_their_exact_names_and_values():
    env = confinement._hands_env(_hands())
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert env[NAME] == "1"
    assert confinement.HANDS_NO_MARKETPLACE_AUTOINSTALL == (NAME, "1")


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_every_hands_start_sets_the_autoinstall_off_exactly_once_beside_the_connectors_line(monkeypatch, system):
    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    for what, argv in _starts(_hands()).items():
        assigned = _env_assignments(argv)
        assert assigned.count(WANT) == 1 and assigned.count(CONNECTORS) == 1, (what, assigned)
        assert [a for a in argv if a.startswith(f"{NAME}=")] == [WANT], what


def test_only_the_hands_environment_names_the_autoinstall_variable_under_firing():
    firing = Path(confinement.__file__).parent
    named = sorted(p.name for p in firing.rglob("*.py") if f'"{NAME}"' in p.read_text())
    assert named == ["confinement.py"], named


def test_the_autoinstall_variable_is_a_name_claude_code_documents():
    from levain.claude_code_env_names import VENDOR_ENV_NAMES
    assert NAME in VENDOR_ENV_NAMES


# (b) the settings flag -------------------------------------------------------------------------------------------

def test_the_flag_settings_name_exactly_the_keys_the_research_names():
    s = hands_mcp_login.HANDS_CLAUDE_FLAG_SETTINGS
    assert set(s) == {"syncClaudeAiSkills", "syncClaudeAiPlugins", "disableClaudeAiConnectors", "enabledPlugins"}
    assert s["syncClaudeAiSkills"] is False and s["syncClaudeAiPlugins"] is False
    assert s["disableClaudeAiConnectors"] is True
    assert s["enabledPlugins"] == {"cc-plugin-easel@builtin": False, "claude-platform@builtin": False}


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_the_chat_argv_passes_the_constant_as_compact_sorted_json_before_any_extra(monkeypatch, system):
    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    for argv in (hands_mcp_login.claude_chat_argv(_hands(), "-p"), hands_chat.chat_argv(_hands(), "claude")):
        assert argv.count("--settings") == 1
        i = argv.index("--settings")
        text = argv[i + 1]
        assert json.loads(text) == hands_mcp_login.HANDS_CLAUDE_FLAG_SETTINGS
        assert text == json.dumps(hands_mcp_login.HANDS_CLAUDE_FLAG_SETTINGS, sort_keys=True, separators=(",", ":"))
        assert " " not in text
        assert argv.index("--mcp-config") < i
    argv = hands_mcp_login.claude_chat_argv(_hands(), "-p")
    assert argv[-1] == "-p" and argv.index("--settings") < argv.index("-p")


def test_the_codex_chat_argv_takes_no_settings_flag(monkeypatch):
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    assert "--settings" not in hands_mcp_login.codex_chat_argv(_hands())


def test_the_chat_argv_takes_none_of_the_modes_that_cost_the_login_or_the_mcp_discovery(monkeypatch):
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    argv = hands_mcp_login.claude_chat_argv(_hands())
    assert not {"--safe-mode", "--bare"} & set(argv)
    assert not any(a.startswith("CLAUDE_CODE_SIMPLE=") for a in argv)


# (c) one source --------------------------------------------------------------------------------------------------

def test_no_other_builder_under_firing_passes_settings_to_the_hands_claude():
    """Source-level: ``"--settings"`` as a literal argument is written in ``hands_mcp_login.py`` only (the chat argv)."""
    firing = Path(confinement.__file__).parent
    named = sorted(p.name for p in firing.rglob("*.py") if '"--settings"' in p.read_text())
    assert named == ["hands_mcp_login.py"], named
    src = (firing / "hands_mcp_login.py").read_text()
    assert src.count('"--settings"') == 1


def test_the_other_hands_starts_take_no_settings_flag(monkeypatch):
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    for what, argv in _starts(_hands()).items():
        if what in ("claude chat", "claude chat through the chat module"):
            continue
        assert "--settings" not in argv, what


# (e) the refusal of an already-poisoned home stays (its own test is in test_hands_chat_launch)

def test_the_user_layer_list_still_names_the_marketplaces_tree():
    assert "plugins/marketplaces" in hands_chat.CLAUDE_USER_LAYER
