"""Q9: governing Claude Code cross-session messaging (DESIGN_Q9). The effective posture is read with Claude Code's
own precedence, levain's writes never loosen what a settings file already says, and the hook pin covers the
messaging controls without turning an upgraded install red."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from levain import doctor
from levain import host_guard as hg
from levain.consent.adapters import claude_code as cc

_TEMPLATE = Path(hg.__file__).parent / "templates" / "adapters" / "claude-code" / "settings.template.json"


class TEMPLATE:  # the template as the installer renders it for the default (governed) choice
    @staticmethod
    def read_text() -> str:
        from levain.install import _apply_messaging

        return _apply_messaging(_TEMPLATE.read_text().replace("{{PYTHON}}", "python3"), "governed")


@pytest.fixture
def env(tmp_path: Path, monkeypatch):
    ent = tmp_path / "ent"
    (ent / ".claude").mkdir(parents=True)
    (ent / ".levain").mkdir()
    (ent / "CLAUDE.md").write_text("x")
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setattr(hg, "load_managed_settings", lambda platform=None: [])
    return ent, home


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def posture(ent, home, managed=()):
    return hg.cc_messaging_posture(ent, home=home, managed=list(managed))


# --- inbound precedence (settings-reference crossSessionInbound) -------------------------------------------------

def test_a_project_hold_beats_a_user_accept(env):
    ent, home = env
    _write(home / ".claude" / "settings.json", {"crossSessionInbound": "accept"})
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold"})
    p = posture(ent, home)
    assert p.inbound == "hold" and p.inbound_source.endswith("ent/.claude/settings.json")


def test_a_project_accept_never_loosens_a_user_refuse(env):
    ent, home = env
    _write(home / ".claude" / "settings.json", {"crossSessionInbound": "refuse"})
    _write(ent / ".claude" / "settings.local.json", {"crossSessionInbound": "accept"})
    assert posture(ent, home).inbound == "refuse"


def test_a_project_accept_alone_applies_nothing(env):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "accept"})
    assert posture(ent, home).inbound is None


def test_managed_is_the_first_trusted_value_and_user_is_then_ignored(env):
    ent, home = env
    _write(home / ".claude" / "settings.json", {"crossSessionInbound": "refuse"})
    assert posture(ent, home, [("/managed.json", {"crossSessionInbound": "accept"})]).inbound == "accept"


def test_a_workspace_refuse_beats_a_managed_accept(env):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "refuse"})
    assert posture(ent, home, [("/managed.json", {"crossSessionInbound": "accept"})]).inbound == "refuse"


def test_an_unrecognised_value_holds_and_in_managed_refuses(env):
    ent, home = env
    _write(home / ".claude" / "settings.json", {"crossSessionInbound": "maybe"})
    assert posture(ent, home).inbound == "hold"
    assert posture(ent, home, [("/managed.json", {"crossSessionInbound": 7})]).inbound == "refuse"


def test_only_the_workspaces_shared_settings_file_is_credited(env, tmp_path):
    ent, home = env
    (tmp_path / ".git").mkdir()
    # Claude Code reads the shared settings.json from the session's own directory only (settings.md); a local file
    # can only tighten and its location depends on worktrees and ownership, so it is never credited
    _write(tmp_path / ".claude" / "settings.json", {"crossSessionInbound": "refuse",
                                                    "permissions": {"ask": ["SendMessage"]}})
    for d in (ent, tmp_path):
        _write(d / ".claude" / "settings.local.json", {"crossSessionInbound": "refuse",
                                                       "permissions": {"deny": ["SendMessage"]}})
    p = posture(ent, home)
    assert p.inbound is None and p.outbound == "open"
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold", "permissions": {"ask": ["SendMessage"]}})
    p = posture(ent, home)
    assert p.inbound == "hold" and p.outbound == "ask"


@pytest.mark.parametrize("rule", ["*", "Send*", "*Message"])
def test_a_glob_rule_naming_the_whole_tool_governs_and_is_pinned(env, rule):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold", "permissions": {"ask": [rule]}})
    assert posture(ent, home).outbound == "ask"
    hg.check_agent_hook_pins(ent)
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold"})
    status, diffs = hg.check_agent_hook_pins(ent)
    assert status == "changed" and any(d.startswith(".claude/settings.json messaging:") for d in diffs)


@pytest.mark.parametrize("rule", ["SendMessage(x)", "Send", "SendMessageX", "mcp__*"])
def test_a_rule_that_does_not_name_the_whole_tool_does_not_govern(env, rule):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"permissions": {"ask": [rule]}})
    assert posture(ent, home).outbound == "open"


def test_the_last_managed_file_setting_the_key_wins(env):
    ent, home = env
    docs = [("/m/managed-settings.json", {"crossSessionInbound": "refuse"}),
            ("/m/managed-settings.d/10.json", {"crossSessionInbound": "accept"})]
    p = posture(ent, home, docs)
    assert p.inbound == "accept" and p.inbound_source == "/m/managed-settings.d/10.json"


def test_a_user_typo_holds_even_under_a_managed_accept(env):
    ent, home = env
    _write(home / ".claude" / "settings.json", {"crossSessionInbound": "Hold"})
    assert posture(ent, home, [("/managed.json", {"crossSessionInbound": "accept"})]).inbound == "hold"


def test_a_non_string_value_reads_as_unrecognised(env):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": ["accept"]})
    assert posture(ent, home).inbound == "hold"
    s: dict = {"crossSessionInbound": {"a": 1}}
    cc.merge_messaging(s)
    assert s["crossSessionInbound"] == "hold"


# --- outbound ---------------------------------------------------------------------------------------------------

def test_outbound_deny_from_any_layer_beats_an_ask(env):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"permissions": {"ask": ["SendMessage"]}})
    _write(home / ".claude" / "settings.json", {"permissions": {"deny": ["SendMessage", "ListAgents"]}})
    p = posture(ent, home)
    assert p.outbound == "deny" and p.list_denied


def test_managed_rules_only_discards_the_workspace_ask_rule(env):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"permissions": {"ask": ["SendMessage"]}})
    p = posture(ent, home, [("/managed.json", {"allowManagedPermissionRulesOnly": True})])
    assert p.outbound == "open"
    p = posture(ent, home, [("/managed.json", {"allowManagedPermissionRulesOnly": True,
                                               "permissions": {"deny": ["SendMessage"]}})])
    assert p.outbound == "deny"


def test_outbound_open_when_no_rule_names_the_bare_tool(env):
    ent, home = env
    _write(ent / ".claude" / "settings.json", {"permissions": {"ask": ["SendMessage(x)", "Bash"]}})
    p = posture(ent, home)
    assert p.outbound == "open" and not p.isolate


# --- the writer never loosens -----------------------------------------------------------------------------------

def test_merge_defaults_write_hold_ask_and_isolate():
    s: dict = {"permissions": {"allow": ["Read"]}}
    cc.merge_messaging(s)
    cc.merge_messaging(s)  # idempotent
    assert s == {"crossSessionInbound": "hold", "isolatePeerMachines": True,
                 "permissions": {"allow": ["Read"], "ask": ["SendMessage"]}}


def test_merge_keeps_a_stricter_value_and_existing_rules():
    s: dict = {"crossSessionInbound": "refuse", "permissions": {"deny": ["SendMessage"]}}
    cc.merge_messaging(s, inbound="hold", outbound="ask")
    assert s["crossSessionInbound"] == "refuse" and s["permissions"]["deny"] == ["SendMessage"]


def test_merge_deny_turns_sending_and_listing_off():
    s: dict = {}
    cc.merge_messaging(s, inbound="refuse", outbound="deny")
    assert s == {"crossSessionInbound": "refuse", "permissions": {"deny": ["SendMessage", "ListAgents"]}}


def test_merge_unmanaged_writes_nothing():
    s: dict = {"crossSessionInbound": "accept"}
    cc.merge_messaging(s, inbound="unmanaged", outbound="unmanaged")
    assert s == {"crossSessionInbound": "accept"}


@pytest.mark.parametrize("kw", [{"inbound": "accept"}, {"outbound": "allow"}])
def test_merge_refuses_an_unknown_choice(kw):
    with pytest.raises(ValueError):
        cc.merge_messaging({}, **kw)


def test_merge_reads_a_null_permissions_block_as_no_rules():
    s: dict = {"permissions": None}
    cc.merge_messaging(s)
    assert s["permissions"] == {"ask": ["SendMessage"]}
    s = {"permissions": {"ask": None}}
    cc.merge_messaging(s)
    assert s["permissions"] == {"ask": ["SendMessage"]}


def test_merge_refuses_a_malformed_permissions_block():
    with pytest.raises(ValueError):
        cc.merge_messaging({"permissions": ["SendMessage"]})


@pytest.mark.parametrize("bad", [{"permissions": ["SendMessage"]}, {"permissions": {"ask": "SendMessage"}},
                                 {"hooks": []}])
def test_install_refuses_a_settings_file_it_cannot_merge_before_writing_the_script(tmp_path, bad):
    ws = tmp_path / "ws"
    _write(ws / ".claude" / "settings.json", bad)
    with pytest.raises(ValueError):
        cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=0,
                   allow_untested_agent_version=True)
    assert not (ws / ".claude" / "hooks" / cc.HOOK_SCRIPT_NAME).exists()
    assert json.loads((ws / ".claude" / "settings.json").read_text()) == bad


def test_doctor_names_listing_left_on_when_only_sending_is_denied(env):
    ent, _ = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "refuse", "permissions": {"deny": ["SendMessage"]}})
    (r,) = doctor._check_cc_messaging(ent)
    assert "ListAgents not denied" in r.detail
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "refuse",
                                               "permissions": {"deny": ["SendMessage", "ListAgents"]}})
    (r,) = doctor._check_cc_messaging(ent)
    assert "ListAgents not denied" not in r.detail


def test_install_writes_the_controls_and_refuses_an_unknown_choice_before_writing(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    with pytest.raises(ValueError):
        cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=0,
                   allow_untested_agent_version=True, inbound="accept")
    assert not (ws / ".claude").exists()
    _, settings = cc.install(ws, python="/py/bin/python3", queue_dir=tmp_path / "q", max_hold=0,
                             allow_untested_agent_version=True)
    data = json.loads(settings.read_text())
    assert data["crossSessionInbound"] == "hold" and data["permissions"]["ask"] == ["SendMessage"]
    assert data["isolatePeerMachines"] is True and data["hooks"]["PermissionRequest"]


# --- the template and doctor ------------------------------------------------------------------------------------

def test_the_template_is_governed_and_doctor_passes_it(env):
    ent, home = env
    (ent / ".claude" / "settings.json").write_text(TEMPLATE.read_text())
    (r,) = doctor._check_cc_messaging(ent)
    assert r.ok and not r.warn
    assert "inbound hold" in r.detail and "outbound asks" in r.detail and "isolatePeerMachines" in r.detail


def test_doctor_warns_naming_each_ungoverned_direction(env):
    ent, home = env
    _write(home / ".claude" / "settings.json", {"crossSessionInbound": "accept"})
    (r,) = doctor._check_cc_messaging(ent)
    assert r.ok and r.warn
    assert "inbound NOT governed: accept" in r.detail and "outbound NOT governed" in r.detail
    assert "--settings" in r.detail  # names what it cannot see


def test_doctor_messaging_line_is_claude_code_only(env):
    ent, home = env
    (ent / "CLAUDE.md").unlink()
    (ent / "AGENTS.md").write_text("x")
    assert doctor._check_cc_messaging(ent) == []


def test_doctor_does_not_write_the_settings(env):
    ent, home = env
    path = ent / ".claude" / "settings.json"
    path.write_text(TEMPLATE.read_text())
    before = path.read_bytes()
    doctor._check_cc_messaging(ent)
    assert path.read_bytes() == before


# --- the pin --------------------------------------------------------------------------------------------------

def test_removing_the_pinned_hold_is_a_named_change(env):
    ent, _ = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold", "permissions": {"ask": ["SendMessage"]}})
    assert hg.check_agent_hook_pins(ent)[0] == "recorded"
    _write(ent / ".claude" / "settings.json", {"permissions": {"ask": ["SendMessage"]}})
    status, diffs = hg.check_agent_hook_pins(ent)
    assert status == "changed" and any(d.startswith(".claude/settings.json messaging:") for d in diffs)


def test_an_unrelated_permission_edit_is_not_a_change(env):
    ent, _ = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold", "permissions": {"ask": ["SendMessage"]}})
    hg.check_agent_hook_pins(ent)
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "hold",
                                               "permissions": {"ask": ["SendMessage"], "allow": ["Bash(ls)"]}})
    assert hg.check_agent_hook_pins(ent) == ("match", [])


def _drop_messaging_components(ent: Path) -> None:
    """The pin file a levain from before Q9 wrote: no messaging components."""
    pf = ent / ".levain" / "agent_hook_pins.json"
    rec = json.loads(pf.read_text())
    rec["pins"] = {k: v for k, v in rec["pins"].items() if " messaging" not in k}
    pf.write_text(json.dumps(rec))


def test_an_upgrade_adding_the_component_is_not_a_change_when_nothing_is_set(env):
    ent, _ = env
    _write(ent / ".claude" / "settings.json", {"hooks": {}})
    hg.check_agent_hook_pins(ent)
    _drop_messaging_components(ent)
    assert hg.check_agent_hook_pins(ent) == ("match", [])
    assert hg.agent_pin_status(ent) == "match"


def test_an_upgrade_with_messaging_already_set_names_it_once(env):
    ent, _ = env
    _write(ent / ".claude" / "settings.json", {"crossSessionInbound": "accept"})
    hg.check_agent_hook_pins(ent)
    _drop_messaging_components(ent)
    status, diffs = hg.check_agent_hook_pins(ent)
    assert status == "changed" and diffs == [f".claude/settings.json messaging: was not pinned, now "
                                             f"{hg._messaging_digest(ent / '.claude' / 'settings.json')[:12]}"]
