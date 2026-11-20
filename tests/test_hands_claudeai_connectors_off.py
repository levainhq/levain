"""Every hands-uid start carries ``ENABLE_CLAUDEAI_MCP_SERVERS=false`` (levain#139, leg 6 step 12).

The hands Claude signs in as the operator, and Claude Code then adds the account's claude.ai connectors (mail, drive,
calendar) to the session even under ``--strict-mcp-config``: measured as the hands user, ``claude mcp list`` showed five
connected connectors; with the variable it said "No MCP servers configured". The variable rides the hands environment
(``confinement._hands_env``), so each builder below, which is how login, status, logout, the ``claude mcp`` verbs and the
chat start the vendor, is checked on its production argv for both hosts.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from levain.firing import confinement, hands_chat, hands_login, hands_mcp_login
from levain.firing.confinement import HandsIdentity

NAME = "ENABLE_CLAUDEAI_MCP_SERVERS"
WANT = f"{NAME}=false"


def _hands() -> HandsIdentity:
    return HandsIdentity("levain-hands", 4_000_018, "/Users/levain-hands", Path("/srv/ws"))


def _env_assignments(argv: list[str]) -> list[str]:
    """The ``NAME=value`` words the ``env -i`` before the command sets (the words right after ``-i`` that hold an ``=``)."""
    i = next(k for k in range(len(argv) - 1) if argv[k] == "/usr/bin/env" and argv[k + 1] == "-i") + 2
    out = []
    while i < len(argv) and "=" in argv[i] and argv[i].split("=", 1)[0].isupper():
        out.append(argv[i])
        i += 1
    return out


def _starts(h: HandsIdentity) -> dict[str, list[str]]:
    """Every builder that starts a vendor, or anything, as the hands uid through the shared prefix."""
    return {
        "login claude": hands_login.login_argv(h, "claude"),
        "login codex": hands_login.login_argv(h, "codex"),
        "status claude": hands_login.status_argv(h, "claude"),
        "status codex": hands_login.status_argv(h, "codex"),
        "logout claude": hands_login.logout_argv(h, "claude"),
        "remove credential": hands_login.remove_argv(h, "claude"),
        "as-hands (mkdir, lstat, security)": hands_login._as_hands_argv(h, "claude", "/bin/true"),
        "claude mcp verb": hands_mcp_login._argv(h, "list"),
        "claude chat": hands_mcp_login.claude_chat_argv(h),
        "claude chat through the chat module": hands_chat.chat_argv(h, "claude"),
        "codex chat": hands_mcp_login.codex_chat_argv(h),
        "the shared prefix": confinement.hands_prefix(h),
    }


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_every_hands_start_sets_the_connectors_off_exactly_once(monkeypatch, system):
    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    for what, argv in _starts(_hands()).items():
        assigned = _env_assignments(argv)
        assert assigned.count(WANT) == 1, (what, assigned)
        assert [a for a in argv if a.startswith(f"{NAME}=")] == [WANT], what      # no second, different value anywhere


def test_the_hands_environment_is_where_it_comes_from():
    assert confinement._hands_env(_hands())[NAME] == "false"
    assert confinement.HANDS_NO_CLAUDEAI_CONNECTORS == (NAME, "false")


def test_only_the_hands_environment_names_it_under_firing():
    """One source: a second place setting it could drift from the first."""
    firing = Path(confinement.__file__).parent
    named = sorted(p.name for p in firing.glob("*.py") if NAME in p.read_text())
    assert named == ["confinement.py"], named


def test_the_variable_is_a_name_claude_code_documents():
    from levain.claude_code_env_names import VENDOR_ENV_NAMES
    assert NAME in VENDOR_ENV_NAMES
