"""Row 161: a confined Claude Code slot's repo settings are frozen into the root-owned ``--settings`` file at render
and never read live. Text and data tests: nothing here launches a vendor."""
from __future__ import annotations

import io
import json
import os
import subprocess

import pytest

from levain.contracts import canon
from levain.slot import Refused
from levain.slot import hook_run
from levain.slot import render as rd
from levain.slot import runtime_grammar as G
from levain.slot import supervisor as sup
from levain.slot.render import HookTemplate
from tests.test_slot_s2b_render import egress, verifier

GATE = {"levain_gate": HookTemplate(b"#!/bin/sh\nexit 0\n", "PreToolUse", None, 870)}
GUARD = {"levain_config": HookTemplate(b"#!/bin/sh\nexit 2\n", "ConfigChange", None, 30)}

REPO_PROJECT = {
    "env": {"NODE_OPTIONS": "--require /x.js", "LD_PRELOAD": "/x.so", "FOO": "1"},
    "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "repo-bash-hook"}]}],
              "PostToolUse": [{"hooks": [{"type": "command", "command": "repo-post"}]}]},
    "permissions": {"allow": ["Bash(npm test)"], "deny": ["Read(./.env)"], "defaultMode": "bypassPermissions",
                    "additionalDirectories": ["/"]},
    "enabledPlugins": {"fmt@market": True},
    "extraKnownMarketplaces": {"market": {"source": {"source": "github", "repo": "o/r"}}},
    "model": "some-model",
}
REPO_LOCAL = {"env": {"BAR": "2"}, "permissions": {"allow": ["Bash(npm test)", "Bash(make)"]},
              "enabledPlugins": {"fmt@market": False}}
DOCS = {".claude/settings.json": REPO_PROJECT, ".claude/settings.local.json": REPO_LOCAL}


def frozen_blob(host="linux", docs=DOCS, templates=None, **kw):
    fz = rd.freeze_repo_settings(docs)
    tmpl = dict(GATE if templates is None else templates)
    blob, facts = egress(host, hooks=frozenset(h for h in tmpl if h != "levain_config"), hook_templates=tmpl, **kw)
    import dataclasses
    return dataclasses.replace(blob, repo_settings=fz.settings), facts, fz


# --- 1: freeze at render ---------------------------------------------------------------------------------------

def test_freeze_takes_hooks_permissions_and_plugin_keys_from_both_files():
    fz = rd.freeze_repo_settings(DOCS)
    assert set(fz.settings) == {"hooks", "permissions", "enabledPlugins", "extraKnownMarketplaces"}
    assert fz.settings["permissions"] == {"allow": ["Bash(npm test)", "Bash(make)"], "deny": ["Read(./.env)"]}
    assert fz.settings["enabledPlugins"] == {"fmt@market": False}          # the local layer wins a key
    assert sorted(fz.settings["hooks"]) == ["PostToolUse", "PreToolUse"]
    assert fz.dropped_other == ("model", "permissions.additionalDirectories", "permissions.defaultMode")


def test_a_repo_with_no_settings_freezes_to_nothing():
    fz = rd.freeze_repo_settings({})
    assert fz.settings == {} and fz.dropped_env == () and fz.dropped_other == ()


@pytest.mark.parametrize("doc", [{"hooks": []}, {"hooks": {"PreToolUse": {}}}, {"permissions": []},
                                 {"permissions": {"allow": "Bash"}}, {"enabledPlugins": []}, [1]])
def test_a_repo_layer_of_the_wrong_shape_refuses_the_launch(doc):
    e = pytest.raises(Refused, rd.freeze_repo_settings, {".claude/settings.json": doc}).value
    assert e.name == "repo-settings-malformed"


def test_the_merged_file_keeps_levain_entries_first_and_unchanged():
    for host in ("linux", "darwin"):
        blob, facts, _ = frozen_blob(host)
        base_blob, base_facts = egress(host, hooks=frozenset(GATE), hook_templates=GATE)
        base = rd.render_cc_settings(base_blob, base_facts)
        got = rd.render_cc_settings(blob, facts)
        groups = got["hooks"]["PreToolUse"]
        assert groups[0] == base["hooks"]["PreToolUse"][0]                 # levain's gate entry, first, unmodified
        assert groups[1]["hooks"][0]["command"] == "repo-bash-hook"          # a repo entry on the same event follows
        assert got["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "repo-post"
        for k in ("disableBypassPermissionsMode",):
            assert got["permissions"][k] == "disable"
        assert "defaultMode" not in got["permissions"] and "additionalDirectories" not in got["permissions"]
        assert got["permissions"]["allow"][-2:] == ["Bash(npm test)", "Bash(make)"]
        assert got["permissions"]["deny"] == ["Read(./.env)"]
        assert got["disableAllHooks"] is False and got["sandbox"] == base["sandbox"]
        assert "model" not in got and "env" not in got
        assert got["enabledPlugins"] == {"fmt@market": False}


def test_a_repo_hook_on_the_gates_own_matcher_does_not_displace_it():
    same = {".claude/settings.json": {"hooks": {"PreToolUse": [
        {"hooks": [{"type": "command", "command": "exit-0-allow"}]}]}}}
    blob, facts, _ = frozen_blob("linux", same)
    got = rd.render_cc_settings(blob, facts)["hooks"]["PreToolUse"]
    assert [g["hooks"][0]["command"] for g in got] == [f"{facts.S}/root/hooks/levain_gate", "exit-0-allow"]
    assert got[0]["hooks"][0]["onFailure"] == "block"


def test_the_blob_carries_the_frozen_layer_through_the_grammar():
    blob, facts, fz = frozen_blob("linux")
    raw = canon.canonical_bytes({"format": G.RENDER_FORMAT, "vendor": "claude", "routes": ["anthropic", "github"],
                                 "hooks": ["levain_gate"], "repo_settings": fz.settings})
    back = G.parse_stdin("render", raw)
    assert back.repo_settings == fz.settings
    for bad in ({"env": {"FOO": "1"}}, {"model": "x"}, {"permissions": {"defaultMode": "x"}}, {"hooks": []}):
        bad_raw = canon.canonical_bytes({"format": G.RENDER_FORMAT, "vendor": "claude", "routes": [],
                                         "repo_settings": bad})
        with pytest.raises(Refused):
            G.parse_stdin("render", bad_raw)


def test_the_supervisors_blob_carries_the_frozen_layer():
    fz = rd.freeze_repo_settings(DOCS)
    raw = sup.render_blob("claude", "brokered", ["anthropic"], None, "r", repo_settings=fz.settings)
    assert json.loads(raw)["repo_settings"] == fz.settings
    assert "repo_settings" not in json.loads(sup.render_blob("claude", "brokered", ["anthropic"], None, "r"))


# --- 2: the env allowlist, default empty -------------------------------------------------------------------------

def test_the_repo_env_allowlist_is_empty_and_every_dropped_key_is_named():
    assert rd.REPO_ENV_ALLOW == frozenset()
    fz = rd.freeze_repo_settings(DOCS)
    assert fz.dropped_env == ("BAR", "FOO", "LD_PRELOAD", "NODE_OPTIONS")      # names, never values
    assert "env" not in fz.settings
    assert "/x.so" not in repr(fz)


def test_an_allowlisted_name_would_pass_and_nothing_else(monkeypatch):
    monkeypatch.setattr(rd, "REPO_ENV_ALLOW", frozenset({"FOO"}))
    fz = rd.freeze_repo_settings(DOCS)
    assert fz.settings["env"] == {"FOO": "1"} and "FOO" not in fz.dropped_env and "BAR" in fz.dropped_env


# --- 3: never read live ----------------------------------------------------------------------------------------

def test_the_slot_launch_never_loads_a_settings_source():
    from levain.slot import start as st
    for host in ("linux", "darwin"):
        argv = st.vendor_argv(host, "claude", "s-00000000000000aa")
        i = argv.index("--setting-sources")
        assert argv[i + 1] == "" and "--settings" in argv and "user" not in argv
        ex = st.vendor_argv(host, "claude", "s-00000000000000aa", kind="dispatch", ticket_kind="executor")
        assert ex[ex.index("--setting-sources") + 1] == ""


# --- 4: the ConfigChange alarm ---------------------------------------------------------------------------------

def test_a_config_change_hook_blocks_and_is_wired_for_claude_only():
    blob, facts, _ = frozen_blob("linux", templates={**GATE, **GUARD})
    got = rd.render_cc_settings(blob, facts)["hooks"]["ConfigChange"]
    assert got[0]["hooks"][0]["command"] == f"{facts.S}/root/hooks/levain_config"
    assert got[0]["hooks"][0]["onFailure"] == "block"
    r = rd.render(blob, facts)
    assert r.file("root/hooks/levain_config").data == GUARD["levain_config"].body
    # a session whose templates do not include it has no ConfigChange entry
    blob2, facts2, _ = frozen_blob("linux")
    assert "ConfigChange" not in rd.render_cc_settings(blob2, facts2)["hooks"]


def test_the_production_templates_include_the_guard_for_a_claude_blob():
    from levain.slot import runtime_helper as rh
    from levain.slot.hook_templates import hook_templates

    class Seams:
        host = "linux"
        hook_templates = None
    blob, _ = egress("linux", hooks=frozenset())
    assert set(rh.hook_templates_for(Seams, blob)) == {"levain_config"}
    assert hook_templates("linux")["levain_config"].event == "ConfigChange"
    cblob, _ = egress("linux", vendor="codex")
    assert rh.hook_templates_for(Seams, cblob) == {}


def test_the_config_guard_blocks_every_change_and_records_one_alarm_line():
    seen = []
    err = io.StringIO()
    payload = json.dumps({"hook_event_name": "ConfigChange", "source": "local_settings",
                          "file_path": "/s/repo/.claude/settings.local.json", "session_id": "abc", "cwd": "/s/repo"})
    code = hook_run.config_guard("levain_config", stdin=io.StringIO(payload), stderr=err, environ={"HOME": "/h"},
                                 record=lambda environ, event: seen.append((environ, event)))
    assert code == hook_run.REFUSED_EXIT
    assert err.getvalue().count("\n") == 1
    assert "local_settings" in err.getvalue() and "/s/repo/.claude/settings.local.json" in err.getvalue()
    assert [e["source"] for _, e in seen] == ["local_settings"]
    for junk in ("", "not json", "[]", "{" * 10):                    # whatever arrives, the change is blocked
        seen.clear()
        assert hook_run.config_guard("levain_config", stdin=io.StringIO(junk), stderr=io.StringIO(), environ={},
                                     record=lambda environ, event: seen.append(event)) == hook_run.REFUSED_EXIT
    assert hook_run.config_guard("levain_config", stdin=io.StringIO(payload), stderr=io.StringIO(), environ={},
                                 record=lambda e, ev: 1 / 0) == hook_run.REFUSED_EXIT       # a record never unblocks


def test_the_config_guard_files_the_alarm_in_the_gate_ledger(tmp_path):
    from levain.consent.ledger import Ledger
    event = {"hook_event_name": "ConfigChange", "source": "project_settings", "file_path": "/r/.claude/settings.json",
             "session_id": "sess-1", "cwd": "/r"}
    row_id = hook_run.file_config_alarm({"HOME": str(tmp_path)}, event)
    assert row_id
    call = Ledger(tmp_path / ".levain" / "consent").get(row_id)
    assert (call.tool_name, call.phase, call.outcome) == ("ConfigChange:project_settings", "denied_by_gate", "refused")
    assert call.cwd == "/r" and call.agent_session_id == "sess-1"


# --- 5: the macOS wrapper --------------------------------------------------------------------------------------

def test_darwin_wraps_the_hook_in_env_i_and_linux_keeps_the_bare_path():
    blob, facts, _ = frozen_blob("darwin")
    cmd = rd.render_cc_settings(blob, facts)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert cmd.startswith("/usr/bin/env -i ") and cmd.endswith(f"{facts.S}/root/hooks/levain_gate")
    env = rd.render_env(blob, facts)["slot"]
    for k, v in env.items():
        assert f"{k}={v}" in cmd
    assert "${LEVAIN_QUEUE_CAP:+LEVAIN_QUEUE_CAP=\"$LEVAIN_QUEUE_CAP\"}" in cmd
    lblob, lfacts, _ = frozen_blob("linux")
    lcmd = rd.render_cc_settings(lblob, lfacts)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert lcmd == f"{lfacts.S}/root/hooks/levain_gate"


@pytest.mark.skipif(not os.path.exists("/usr/bin/env"), reason="needs /usr/bin/env")
def test_the_wrapper_command_runs_the_hook_with_exactly_the_rendered_env(tmp_path):
    blob, facts, _ = frozen_blob("darwin")
    cmd = rd.render_cc_settings(blob, facts)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    script = tmp_path / "hook"
    script.write_text("#!/bin/sh\n/usr/bin/env\n")
    script.chmod(0o755)
    cmd = cmd.replace(f"{facts.S}/root/hooks/levain_gate", str(script))
    poisoned = {"PATH": "/usr/bin:/bin", "DEVELOPER_DIR": "/evil", "LD_PRELOAD": "/evil.so", "NODE_OPTIONS": "x",
                "LEVAIN_QUEUE_CAP": "cap-value", "CLAUDE_PROJECT_DIR": "/proj", "HOME": "/evil-home"}
    out = subprocess.run(["/bin/sh", "-c", cmd], env=poisoned, capture_output=True, text=True, check=True).stdout
    got = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    rendered = rd.render_env(blob, facts)["slot"]
    assert {k: v for k, v in got.items() if k in rendered} == rendered
    assert got["LEVAIN_QUEUE_CAP"] == "cap-value" and got["CLAUDE_PROJECT_DIR"] == "/proj"
    assert set(got) - set(rendered) <= {"LEVAIN_QUEUE_CAP", "CLAUDE_PROJECT_DIR", "PWD", "_", "SHLVL", "OLDPWD"}
    assert "DEVELOPER_DIR" not in got and "LD_PRELOAD" not in got and "NODE_OPTIONS" not in got


# --- 6: what the freeze makes redundant ----------------------------------------------------------------------

def test_no_rendered_env_names_a_dynamic_loader_variable():
    from tests.test_slot_s2b_render import every_variant
    n = 0
    for blob, facts in every_variant():
        for env in rd.render_env(blob, facts).values():
            assert not [k for k in env if k.startswith(("LD_", "DYLD_"))]
        n += 1
    assert n and not hasattr(rd, "is_loader_env")
    from levain.slot import wall
    assert not hasattr(wall, "is_loader_env") and not hasattr(wall, "LOADER_ENV_PREFIXES")
