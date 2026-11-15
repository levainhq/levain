"""The hands Codex chat does not load a plugin's own MCP servers (hands launcher, board row 115, L3 r3 G4).

The launch files carry only the registry's servers (`merge_codex_config` sees `mcp_servers` and nothing else), but an
installed Codex plugin can carry a `.mcp.json` that Codex loads beside them. `codex_chat_argv` switches plugin loading
off on the command line. The argv test elsewhere pins the SPELLING; this one runs the REAL `codex` binary against a
scratch `CODEX_HOME` holding an installed probe plugin and reads which MCP servers it loads, so a codex that stops
honouring the flag, or a flag list that stops naming the load-bearing feature, fails here.

The control comes first and is asserted: with no flag the probe plugin's server IS listed. Without it the "not listed"
half would pass on a layout the binary no longer reads. The scratch home is under tmp, never the operator's `~/.codex`,
and `codex mcp list` starts no server and needs no login. The binary is the one the LAUNCHER would run: it is resolved
through `confinement.HANDS_PATH` (the hands account's fixed PATH, not the operator's), and every subprocess here runs
with that PATH. Skipped where no `codex` is on that PATH, and where that codex lacks the `plugin marketplace add` /
`plugin add` subcommands the fixture installs through (a codex version bump then reds only a real product regression,
never the fixture); what no instrument here holds (the hands uid's own chat, the `remote_plugin` half, a plugin
installed from a remote marketplace) is the VM sitting's."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys

import pytest

from levain.firing import confinement, hands_login, hands_mcp_login as hml


def resolve_codex(path: str = confinement.HANDS_PATH) -> str | None:
    """The `codex` the launcher would run: looked up on the hands account's PATH, never the operator's."""
    return shutil.which("codex", path=path)


CODEX = resolve_codex()
needs_codex = pytest.mark.skipif(CODEX is None, reason="needs a real codex binary on the hands account's PATH")

PROBE_SERVER = "unregistered-probe"
_TIMEOUT_S = 60
_HELP_TIMEOUT_S = 15

#: (the codex subcommand path, the subcommands its --help must list) for what the fixture installs through
_PLUGIN_CLI = ((["plugin"], ("add", "marketplace")), (["plugin", "marketplace"], ("add",)))


def _env(tmp_path) -> dict[str, str]:
    """The env every codex subprocess runs under: the hands PATH, and a scratch HOME / CODEX_HOME under tmp."""
    home = tmp_path / "codex_home"
    home.mkdir(exist_ok=True)
    return {"PATH": confinement.HANDS_PATH, "HOME": str(tmp_path), "CODEX_HOME": str(home)}


def plugin_cli_gap(codex: str, env: dict[str, str]) -> str | None:
    """Why `codex` cannot install the probe plugin (a --help that fails, hangs, or does not list `add` /
    `marketplace`), or None when it can. The caller skips on a reason: that is the fixture, not the product."""
    for sub, words in _PLUGIN_CLI:
        try:
            done = subprocess.run([codex, *sub, "--help"], env=env, capture_output=True, text=True,
                                  timeout=_HELP_TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"`codex {' '.join(sub)} --help` could not be read ({type(exc).__name__})"
        if done.returncode != 0:
            return f"`codex {' '.join(sub)} --help` exited {done.returncode}"
        for word in words:
            if not re.search(rf"^\s+{re.escape(word)}\s", done.stdout, re.MULTILINE):
                return f"`codex {' '.join(sub)}` lists no `{word}` subcommand"
    return None


def _install_probe_plugin(tmp_path, env) -> None:
    """A local marketplace holding one plugin whose `.mcp.json` names `PROBE_SERVER`, installed into `env`'s scratch
    `CODEX_HOME` through codex's own `plugin marketplace add` / `plugin add`."""
    mkt = tmp_path / "mkt"
    plugin = mkt / "plugins" / "probe"
    (mkt / ".agents" / "plugins").mkdir(parents=True)
    (plugin / ".codex-plugin").mkdir(parents=True)
    (mkt / ".agents" / "plugins" / "marketplace.json").write_text(json.dumps({
        "name": "probe-mkt", "interface": {"displayName": "Probe"},
        "plugins": [{"name": "probe", "source": {"source": "local", "path": "./plugins/probe"},
                     "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"}, "category": "Engineering"}]}))
    (plugin / ".codex-plugin" / "plugin.json").write_text(json.dumps(
        {"name": "probe", "version": "0.0.1", "description": "scratch probe", "mcpServers": "./.mcp.json"}))
    (plugin / ".mcp.json").write_text(json.dumps({"mcpServers": {PROBE_SERVER: {"command": "/usr/bin/true", "args": []}}}))
    for step in (["plugin", "marketplace", "add", str(mkt)], ["plugin", "add", "probe@probe-mkt"]):
        done = subprocess.run([CODEX, *step], env=env, capture_output=True, text=True, timeout=_TIMEOUT_S)
        assert done.returncode == 0, f"codex {' '.join(step)} failed ({done.returncode}): {done.stdout}{done.stderr}"


def _mcp_list(argv, env) -> str:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=_TIMEOUT_S)
    assert done.returncode == 0, f"{argv}: {done.returncode}: {done.stdout}{done.stderr}"
    return done.stdout


@needs_codex
def test_the_hands_codex_chat_argv_leaves_an_installed_plugins_mcp_server_unloaded(tmp_path, monkeypatch):
    env = _env(tmp_path)
    gap = plugin_cli_gap(CODEX, env)
    if gap is not None:
        pytest.skip(f"the fixture cannot install a plugin with this codex: {gap}")
    _install_probe_plugin(tmp_path, env)
    # the control: no flag, and codex lists the plugin's server beside whatever the config holds
    assert PROBE_SERVER in _mcp_list([CODEX, "mcp", "list"], env)
    # the real argv builder, its sudo/env prefix stripped (the prefix is the hands uid's, not codex's)
    monkeypatch.setattr(hands_login, "_launch_prefix", lambda hands, vendor: [])
    argv = hml.codex_chat_argv(None, "mcp", "list")
    assert argv[0] == "codex"
    out = _mcp_list([CODEX, *argv[1:]], env)
    assert PROBE_SERVER not in out, out


#: Every Codex feature that can bring a tool surface or a command the registry does not hold (leg 6 fix, L1 MED 1 +
#: L2 HIGH 1): ``apps`` mounts the hosted ``codex_apps`` MCP server (a ChatGPT-signed account's connectors),
#: ``skill_mcp_dependency_install`` lets a skill install an MCP server into config, ``hooks`` runs commands a config
#: or project names, and the two plugin features load a plugin's ``.mcp.json``.
MUST_BE_OFF = ("apps", "skill_mcp_dependency_install", "hooks", "plugins", "remote_plugin")


def _features(argv, env) -> dict[str, str]:
    done = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=_TIMEOUT_S)
    assert done.returncode == 0, f"{argv}: {done.returncode}: {done.stdout}{done.stderr}"
    return {p[0]: p[-1] for p in (ln.split() for ln in done.stdout.splitlines()) if len(p) >= 3}


@needs_codex
def test_the_hands_codex_chat_argv_switches_off_every_server_mounting_feature(tmp_path, monkeypatch):
    env = _env(tmp_path)
    # a config that asks for every one of them: the command line must win over the file
    (tmp_path / "codex_home" / "config.toml").write_text(
        "[features]\n" + "".join(f"{f} = true\n" for f in MUST_BE_OFF))
    control = _features([CODEX, "features", "list"], env)
    assert all(control.get(f) == "true" for f in MUST_BE_OFF), control       # the control: on without the flags
    monkeypatch.setattr(hands_login, "_launch_prefix", lambda hands, vendor: [])
    argv = hml.codex_chat_argv(None, "features", "list")
    got = _features([CODEX, *argv[1:]], env)
    assert {f: got.get(f) for f in MUST_BE_OFF} == {f: "false" for f in MUST_BE_OFF}


def test_the_disabled_list_names_every_server_mounting_feature():
    assert set(MUST_BE_OFF) <= set(hml.CODEX_DISABLED_FEATURES)


# ---- the fixture's own pins: they need no real codex ----

def _stub(dirpath, body: str):
    dirpath.mkdir(exist_ok=True)
    codex = dirpath / "codex"
    codex.write_text("#!/bin/sh\n" + body)
    codex.chmod(0o755)
    return str(codex)


_GOOD_HELP = """case "$*" in
  "plugin --help") printf 'Commands:\\n  add          x\\n  marketplace  y\\n';;
  "plugin marketplace --help") printf 'Commands:\\n  add   x\\n';;
  *) exit 2;;
esac
"""


def test_codex_is_resolved_on_the_hands_path_not_the_operators(tmp_path, monkeypatch):
    # a codex found only on the operator's PATH is not the launcher's (L3 r1 codex MED): the lookup ignores PATH
    only_operator = tmp_path / "operator_bin"
    _stub(only_operator, "exit 0\n")
    assert str(only_operator) not in confinement.HANDS_PATH.split(":")
    monkeypatch.setenv("PATH", f"{only_operator}:/usr/bin:/bin")
    found = resolve_codex()
    assert found is None or not found.startswith(str(only_operator))
    # and the same stub IS found when its dir is on the path handed in: the lookup uses that path, nothing else
    assert resolve_codex(f"{only_operator}:{confinement.HANDS_PATH}") == str(only_operator / "codex")


def test_the_probe_runs_every_codex_subprocess_under_the_hands_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "/some/operator/only/dir:/usr/bin")
    assert _env(tmp_path)["PATH"] == confinement.HANDS_PATH


def test_a_codex_with_the_plugin_subcommands_has_no_gap(tmp_path):
    assert plugin_cli_gap(_stub(tmp_path / "good", _GOOD_HELP), _env(tmp_path)) is None


@pytest.mark.parametrize("body, why", [
    ("exit 2\n", "exited 2"),
    ("printf 'Commands:\\n  list  x\\n'\n", "lists no `add`"),
    ("printf 'Commands:\\n  add  x\\n'\n", "lists no `marketplace`"),
    (_GOOD_HELP.replace("'Commands:\\n  add   x\\n'", "'Commands:\\n  list  x\\n'"), "lists no `add`"),
])
def test_a_codex_without_the_plugin_subcommands_is_a_gap_so_the_probe_skips(tmp_path, body, why):
    gap = plugin_cli_gap(_stub(tmp_path / "old", body), _env(tmp_path))
    assert gap is not None and why in gap, gap


def test_a_codex_whose_help_hangs_is_a_gap_not_a_hang(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "_HELP_TIMEOUT_S", 0.5)
    gap = plugin_cli_gap(_stub(tmp_path / "slow", "exec sleep 5\n"), _env(tmp_path))
    assert gap is not None and "TimeoutExpired" in gap, gap
