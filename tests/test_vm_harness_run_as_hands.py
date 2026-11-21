"""scripts/vm_harness/run_as_hands.py: the argv it builds and its refusals. sudo is NEVER run (subprocess.run is replaced
by a recorder); the no-hands-user refusal is the real loader reading a real entity directory with no confinement.json."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from levain.firing import confinement, hands_login
from levain.firing.confinement import HANDS_PATH, HandsIdentity
from scripts.vm_harness import run_as_hands as rah

H = HandsIdentity("_levain_ent_ab12cd", 431, "/Users/_levain_ent_ab12cd", Path("/Users/Shared/levain/_levain_ent_ab12cd/workspace"))


def test_argv_is_the_products_sudo_env_i_shape_with_the_hands_path(monkeypatch):
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    argv = rah.build_argv(H, [], ["codex", "mcp", "list"])
    assert argv[:7] == [confinement.SUDO, "-n", "-u", H.user, "/usr/bin/env", "-i", f"HOME={H.home}"]
    assert f"PATH={confinement.hands_shim_dir(H)}:{HANDS_PATH}" in argv  # Darwin (patched above): the shim dir first and f"USER={H.user}" in argv and f"LOGNAME={H.user}" in argv
    assert argv[-3:] == ["codex", "mcp", "list"]
    assert argv.index("codex") > max(i for i, a in enumerate(argv) if "=" in a and a.split("=")[0].isupper())


def test_extra_env_sits_before_the_fixed_assignments_which_win(monkeypatch):
    # leg 6 L3 r1: right after env -i (the fixed ones after it win, and a Linux start program stays the command)
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    argv = rah.build_argv(H, [f"CODEX_HOME={H.home}/.codex"], ["codex", "plugin", "list"])
    assert argv[argv.index("-i") + 1] == f"CODEX_HOME={H.home}/.codex"
    assert argv[-3:] == ["codex", "plugin", "list"]


@pytest.mark.parametrize("bad", ["HOME=/tmp/x", "PATH=/tmp", "USER=root", "LOGNAME=root", "no-equals", "1A=b"])
def test_env_that_overrides_the_fixed_ones_or_is_malformed_is_refused(bad):
    with pytest.raises(rah.RunAsHandsRefused):
        rah.build_argv(H, [bad], ["true"])


def test_no_command_is_refused():
    with pytest.raises(rah.RunAsHandsRefused, match="no command"):
        rah.build_argv(H, [], [])


def test_an_entity_with_no_hands_user_is_refused_and_nothing_runs(tmp_path, monkeypatch, capsys):
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    ran = []
    monkeypatch.setattr(rah.subprocess, "run", lambda *a, **k: ran.append(a))
    assert rah.main(["--path", str(ent), "--", "true"]) == 2
    assert hands_login.NO_HANDS in capsys.readouterr().err and ran == []


def test_a_confinement_json_without_the_hands_triple_is_refused_too(tmp_path, monkeypatch, capsys):
    ent = tmp_path / "ent"
    (ent / ".levain").mkdir(parents=True)
    (ent / ".levain" / "confinement.json").write_text(json.dumps({"deny_files": []}))
    ran = []
    monkeypatch.setattr(rah.subprocess, "run", lambda *a, **k: ran.append(a))
    assert rah.main(["--path", str(ent), "--", "true"]) == 2 and ran == []


def test_main_runs_the_built_argv_in_the_hands_workspace_and_returns_its_status(tmp_path, monkeypatch):
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(rah, "hands_of", lambda entity: H)
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 7)

    monkeypatch.setattr(rah.subprocess, "run", fake_run)
    assert rah.main(["--path", str(tmp_path), "--env", "CODEX_HOME=/h/.codex", "--", "codex", "mcp", "list"]) == 7
    assert seen["argv"][:4] == [confinement.SUDO, "-n", "-u", H.user] and seen["argv"][-3:] == ["codex", "mcp", "list"]
    assert seen["cwd"] == str(H.workspace)
    assert "CODEX_HOME=/h/.codex" in seen["argv"]


def test_print_shows_the_argv_and_runs_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(confinement.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(rah, "hands_of", lambda entity: H)
    monkeypatch.setattr(rah.subprocess, "run", lambda *a, **k: pytest.fail("ran"))
    assert rah.main(["--path", str(tmp_path), "--print", "--", "claude", "--version"]) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"{confinement.SUDO} -n -u {H.user} /usr/bin/env -i") and out.strip().endswith("claude --version")


# --- leg 6 L3 r1 -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cmd0", ["PATH=/tmp/evil", "HOME=/tmp/other", "FOO=bar", "", "=x"])
def test_a_command_that_env_would_read_as_an_assignment_is_refused(cmd0):
    """gemini MED: /usr/bin/env reads every leading NAME=VALUE as an assignment, so a command[0] holding "=" set
    HOME or PATH past _FORBIDDEN (or ran nothing and printed the environment)."""
    with pytest.raises(rah.RunAsHandsRefused, match="command"):
        rah.build_argv(H, [], [cmd0, "sh"])


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_extra_env_sits_right_after_env_i_so_the_linux_bootstrap_is_still_the_command(monkeypatch, system):
    """codex MED: on Linux the prefix ends with the python start program; extra env appended after it became the
    program's command to exec. It goes right after ``env -i`` now, as hands_login._launch_prefix does."""
    monkeypatch.setattr(confinement.platform, "system", lambda: system)
    base = confinement.hands_prefix(H)
    argv = rah.build_argv(H, ["CODEX_HOME=/h/.codex"], ["codex", "mcp", "list"])
    i = argv.index("-i")
    assert argv[i + 1] == "CODEX_HOME=/h/.codex"
    assert argv == [*base[:i + 1], "CODEX_HOME=/h/.codex", *base[i + 1:], "codex", "mcp", "list"]


@pytest.mark.parametrize("bad", ["A=line\nbreak", "A=nul\0", "A=tab\tok-but-control\x1b", "A-B=x", "A=b c=d\r"])
def test_extra_env_with_a_bad_name_or_a_control_character_in_its_value_is_refused(bad):
    with pytest.raises(rah.RunAsHandsRefused):
        rah.build_argv(H, [bad], ["true"])


@pytest.mark.parametrize("system", ["Darwin", "Linux"])
def test_the_hands_prefix_holds_one_dash_i_and_it_is_env_s(system):
    """Leg 6 L3 r2 (complement LOW, deepseek LOW), a known edge pinned, not built: run_as_hands and hands_login
    splice extra assignments at ``base.index("-i") + 1``, right only while the prefix's first ``-i`` is ``env -i``."""
    base = confinement.hands_prefix(H, system=system)
    assert base.count("-i") == 1 and base[base.index("-i") - 1] == "/usr/bin/env"
