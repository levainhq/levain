"""P10b R1: `levain agents session-install`, the operator verb that writes the store's ``session_install.json``.

The verb writes through ``session_wire.write_install`` and prints what ``session_wire.read_install`` reads back; the
tests drive the real CLI entry (``levain.cli.main``) against a real agent store and read the file back with
``read_install``, the oracle. Refusals come first: each leaves the previous file byte for byte.
"""
from __future__ import annotations

import copy
import json
import os
import stat
from pathlib import Path

import pytest

from levain.agents import session_client, session_wire as W
from levain.agents.role import role_hash
from levain.agents.spec import parse_spec, register_spec
from levain.agents.store import open_agent_store
from levain.cli import main

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ROLES = json.loads((FIX / "roles.json").read_text())
AGENT = "ag-" + "a" * 32


@pytest.fixture(autouse=True)
def _launchers(monkeypatch):
    monkeypatch.setattr(W, "LAUNCHERS", {})
    W.register_launcher("stub", lambda store: None)


@pytest.fixture
def world(tmp_path):
    store = open_agent_store(tmp_path / "store")
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir()
    return store, cockpit, tmp_path


def _episodic(tmp_path) -> Path:
    from anneal_memory import Store
    p = tmp_path / "episodes.db"
    Store(p, audit=False).close()
    return p


def _run(store, capsys, *argv):
    capsys.readouterr()
    code = main(["agents", "--store-dir", str(store.directory), "session-install", *argv])
    out = capsys.readouterr()
    return code, out.out, out.err


def _file(store) -> Path:
    return store.directory / W.INSTALL_FILE


def _litter(store) -> list[str]:
    return sorted(p.name for p in store.directory.iterdir())


def _register(store) -> str:
    d = copy.deepcopy(SPECS["job_push_check"])
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    return register_spec(store, parse_spec(d), "America/New_York")[0]


def _show(store, capsys, ref):
    capsys.readouterr()
    code = main(["agents", "--store-dir", str(store.directory), "show", ref, "--json"])
    return code, json.loads(capsys.readouterr().out)


# --- refusals first ------------------------------------------------------------------------------------------------

def _good(world, capsys, **extra):
    """A first install the refusal tests must leave exactly as it is."""
    store, cockpit, tmp_path = world
    code, _, err = _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub",
                        "--episodic-path", str(_episodic(tmp_path)), "--values", '{"k": 1}')
    assert code == 0, err
    return _file(store).read_bytes(), _litter(store)


REFUSALS = {
    "relative-cockpit": lambda w: ["--cockpit-root", "rel/dir"],
    "missing-cockpit": lambda w: ["--cockpit-root", str(w[2] / "nope")],
    "cockpit-is-a-file": lambda w: ["--cockpit-root", str(w[2] / "episodes.db")],
    "unknown-launcher": lambda w: ["--cockpit-root", str(w[1]), "--launcher", "no-such-launcher"],
    "unwired-is-not-a-launcher": lambda w: ["--cockpit-root", str(w[1]), "--launcher", "unwired"],
    "blank-launcher": lambda w: ["--cockpit-root", str(w[1]), "--launcher", " "],
    "empty-launcher": lambda w: ["--cockpit-root", str(w[1]), "--launcher", ""],
    "values-nested-too-deep": lambda w: ["--cockpit-root", str(w[1]), "--values", '{"a":' * 20000],
    "relative-episodic": lambda w: ["--cockpit-root", str(w[1]), "--episodic-path", "rel/episodes.db"],
    "episodic-is-a-directory": lambda w: ["--cockpit-root", str(w[1]), "--episodic-path", str(w[2])],
    "values-not-json": lambda w: ["--cockpit-root", str(w[1]), "--values", "{nope"],
    "values-not-an-object": lambda w: ["--cockpit-root", str(w[1]), "--values", "[1, 2]"],
    "workspace-bad-agent-id": lambda w: ["--cockpit-root", str(w[1]), "--workspace", "not-an-agent", "/repo"],
    "workspace-relative-repo": lambda w: ["--cockpit-root", str(w[1]), "--workspace", AGENT, "repo"],
    "workspace-bad-branch": lambda w: ["--cockpit-root", str(w[1]), "--workspace", AGENT, "/repo", "bad branch"],
    "workspace-too-few": lambda w: ["--cockpit-root", str(w[1]), "--workspace", AGENT],
    "workspace-too-many": lambda w: ["--cockpit-root", str(w[1]), "--workspace", AGENT, "/repo", "main", "x"],
    "workspace-twice": lambda w: ["--cockpit-root", str(w[1]), "--workspace", AGENT, "/a", "--workspace", AGENT, "/b"],
}


@pytest.mark.parametrize("name", sorted(REFUSALS))
def test_a_refused_call_exits_1_and_leaves_the_previous_install_byte_for_byte(world, capsys, name):
    before, litter = _good(world, capsys)
    code, out, err = _run(world[0], capsys, *REFUSALS[name](world))
    assert code == 1 and out == "" and err.startswith("levain agents session-install: ")
    assert _file(world[0]).read_bytes() == before and _litter(world[0]) == litter


def test_a_refused_first_call_writes_no_file(world, capsys):
    code, out, err = _run(world[0], capsys, "--cockpit-root", str(world[1]), "--launcher", "no-such-launcher")
    assert code == 1 and "no-such-launcher" in err and "not registered" in err
    assert not any("install" in n for n in _litter(world[0]))


def test_an_unknown_launcher_error_names_the_registered_ones(world, capsys):
    _, _, err = _run(world[0], capsys, "--cockpit-root", str(world[1]), "--launcher", "nope")
    assert "stub" in err


def test_the_cockpit_root_is_required(world, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["agents", "--store-dir", str(world[0].directory), "session-install"])
    assert exc.value.code == 2 and not _file(world[0]).exists()


def test_a_store_that_is_not_an_agent_store_is_refused_and_nothing_is_written(tmp_path, capsys):
    bad = tmp_path / "store"
    bad.mkdir()
    (bad / "autonomic.db").write_bytes(b"not a database")
    cockpit = tmp_path / "cockpit"
    cockpit.mkdir()
    capsys.readouterr()
    code = main(["agents", "--store-dir", str(bad), "session-install", "--cockpit-root", str(cockpit)])
    assert code == 1 and not (bad / W.INSTALL_FILE).exists()


def test_a_write_that_fails_is_said_and_the_old_file_survives(world, capsys, monkeypatch):
    before, litter = _good(world, capsys)

    def boom(*a, **k):
        raise PermissionError("read-only")
    monkeypatch.setattr(W.os, "replace", boom)
    code, out, err = _run(world[0], capsys, "--cockpit-root", str(world[1]))
    assert code == 1 and "cannot write the install" in err and "read-only" in err
    assert _file(world[0]).read_bytes() == before and _litter(world[0]) == litter


# --- the write -----------------------------------------------------------------------------------------------------

def test_a_run_writes_a_file_read_install_accepts_and_prints_what_it_wrote(world, capsys):
    store, cockpit, tmp_path = world
    episodic = _episodic(tmp_path)
    repo = tmp_path / "repo"
    code, out, err = _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub",
                          "--episodic-path", str(episodic), "--values", '{"n": 3, "who": "me"}',
                          "--workspace", AGENT, str(repo), "main")
    assert code == 0 and err == ""
    install = W.read_install(store.directory)
    assert install.cockpit_root == cockpit and install.launcher == "stub" and install.episodic_path == episodic
    assert dict(install.values) == {"n": 3, "who": "me"}
    assert dict(install.workspaces) == {AGENT: W.WorkspaceSetting(str(repo), "main")}
    assert stat.S_IMODE(os.stat(_file(store)).st_mode) == 0o600
    lines = out.splitlines()
    assert lines[0] == f"wrote {_file(store)}"
    assert f"cockpit_root: {cockpit}" in lines and "launcher: stub" in lines and f"episodic_path: {episodic}" in lines
    assert 'values: {"n": 3, "who": "me"}' in lines and f"workspace {AGENT}: {repo} @ main" in lines
    assert not any(n.endswith(".tmp") for n in _litter(store))


def test_a_workspace_without_a_branch_names_none(world, capsys):
    store, cockpit, tmp_path = world
    code, out, _ = _run(store, capsys, "--cockpit-root", str(cockpit), "--workspace", AGENT, "/some/repo")
    assert code == 0 and f"workspace {AGENT}: /some/repo" in out.splitlines()
    assert W.read_install(store.directory).workspaces[AGENT] == W.WorkspaceSetting("/some/repo", None)


def test_only_the_cockpit_root_writes_an_install_naming_no_launcher(world, capsys):
    store, cockpit, _ = world
    code, out, _ = _run(store, capsys, "--cockpit-root", str(cockpit))
    install = W.read_install(store.directory)
    assert code == 0 and install.launcher is None and install.episodic_path is None and dict(install.values) == {}
    assert "launcher: (none)" in out and "episodic_path: (none)" in out


def test_the_production_launcher_name_is_accepted(world, capsys, monkeypatch):
    monkeypatch.undo()          # the production registry, as it stands at import
    assert session_client.LAUNCHER_NAME in W.LAUNCHERS
    store, cockpit, _ = world
    code, _, err = _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", session_client.LAUNCHER_NAME)
    assert code == 0, err
    assert W.read_install(store.directory).launcher == session_client.LAUNCHER_NAME


def test_a_second_run_replaces_the_file_whole(world, capsys):
    store, cockpit, tmp_path = world
    _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub", "--values", '{"a": 1}',
         "--workspace", AGENT, "/repo")
    code, _, _ = _run(store, capsys, "--cockpit-root", str(cockpit))
    install = W.read_install(store.directory)
    assert code == 0 and install.launcher is None and dict(install.values) == {} and dict(install.workspaces) == {}


# --- show --json ---------------------------------------------------------------------------------------------------

def test_show_json_of_a_registered_agent_drops_the_no_episodic_store_note_after_the_verb(world, capsys):
    store, cockpit, tmp_path = world
    _register(store)
    code, before = _show(store, capsys, "job_push_check")
    assert code == 0 and before["episodes"] is None and "no episodic store" in before["episodes_note"]
    code, _, err = _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub",
                        "--episodic-path", str(_episodic(tmp_path)))
    assert code == 0, err
    code, after = _show(store, capsys, "job_push_check")
    assert code == 0 and after["episodes"] == [] and after["episodes_note"] is None


def test_an_install_with_no_episodic_path_still_says_so_in_show_json(world, capsys):
    store, cockpit, _ = world
    _register(store)
    assert _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub")[0] == 0
    _, doc = _show(store, capsys, "job_push_check")
    assert doc["episodes"] is None and "names no episodic_path" in doc["episodes_note"]


# --- the dream install's cockpit root, the replaced root (P10b R1 review round) ----------------------------------------

def _dream_install(store, root: Path) -> None:
    from levain.dream import agent as DA
    (root / "m.db").write_text("")
    (store.directory / DA.INSTALL_FILE).write_text(json.dumps(
        {"schema": DA.INSTALL_SCHEMA, "zone": "UTC", "cockpit_root": str(root), "coord_dir": str(root),
         "store_path": str(root / "m.db")}))
    assert DA.read_install(store.directory).cockpit_root == root


def test_a_cockpit_root_other_than_the_dream_installs_is_refused_naming_both(world, capsys):
    store, cockpit, tmp_path = world
    other = tmp_path / "other"
    other.mkdir()
    _dream_install(store, other)
    code, out, err = _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub")
    assert code == 1 and out == "" and err.startswith("levain agents session-install: ")
    assert str(cockpit) in err and str(other) in err and "nothing written" in err
    assert not _file(store).exists()


def test_a_cockpit_root_other_than_the_dream_installs_leaves_the_previous_file(world, capsys):
    store, cockpit, tmp_path = world
    before, litter = _good(world, capsys)
    _dream_install(store, cockpit)
    other = tmp_path / "other"
    other.mkdir()
    code, _, err = _run(store, capsys, "--cockpit-root", str(other))
    assert code == 1 and str(other) in err and str(cockpit) in err
    assert _file(store).read_bytes() == before and _litter(store) == sorted(litter + ["dream_install.json"])


def test_the_dream_installs_cockpit_root_is_written(world, capsys):
    store, cockpit, tmp_path = world
    _dream_install(store, cockpit)
    assert _run(store, capsys, "--cockpit-root", str(cockpit), "--launcher", "stub")[0] == 0
    link = tmp_path / "link"
    link.symlink_to(cockpit)
    code, _, err = _run(store, capsys, "--cockpit-root", str(link), "--launcher", "stub")
    assert code == 0, err
    assert W.read_install(store.directory).cockpit_root == link


def test_an_unreadable_dream_install_does_not_bar_the_write(world, capsys):
    from levain.dream import agent as DA
    store, cockpit, _ = world
    (store.directory / DA.INSTALL_FILE).write_text("{not json")
    code, _, err = _run(store, capsys, "--cockpit-root", str(cockpit))
    assert code == 0, err
    assert W.read_install(store.directory).cockpit_root == cockpit


def test_no_dream_install_writes(world, capsys):
    store, cockpit, _ = world
    code, _, err = _run(store, capsys, "--cockpit-root", str(cockpit))
    assert code == 0 and err == "" and W.read_install(store.directory).cockpit_root == cockpit


def test_a_changed_cockpit_root_is_named_as_replaced(world, capsys):
    store, cockpit, tmp_path = world
    other = tmp_path / "other"
    other.mkdir()
    assert _run(store, capsys, "--cockpit-root", str(cockpit))[0] == 0
    code, out, _ = _run(store, capsys, "--cockpit-root", str(other))
    assert code == 0 and f"replaced cockpit_root {cockpit} -> {other}" in out.splitlines()


def test_the_same_cockpit_root_or_a_first_write_names_no_replacement(world, capsys):
    store, cockpit, _ = world
    _, first, _ = _run(store, capsys, "--cockpit-root", str(cockpit))
    _, second, _ = _run(store, capsys, "--cockpit-root", str(cockpit))
    assert "replaced" not in first and "replaced" not in second


def test_an_unreadable_previous_install_names_no_replacement(world, capsys):
    store, cockpit, _ = world
    _file(store).write_text("{not json")
    code, out, err = _run(store, capsys, "--cockpit-root", str(cockpit))
    assert code == 0, err
    assert "replaced" not in out and W.read_install(store.directory).cockpit_root == cockpit


def test_a_tilde_cockpit_root_other_than_the_dream_installs_is_refused(world, capsys, monkeypatch):
    store, cockpit, tmp_path = world
    home = tmp_path / "home"
    (home / "b").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    _dream_install(store, cockpit)
    code, out, err = _run(store, capsys, "--cockpit-root", "~/b", "--launcher", "stub")
    assert code == 1 and out == "" and "nothing written" in err
    assert str(home / "b") in err and str(cockpit) in err
    assert not _file(store).exists()


def test_a_tilde_cockpit_root_naming_the_dream_installs_root_is_written(world, capsys, monkeypatch):
    store, cockpit, tmp_path = world
    monkeypatch.setenv("HOME", str(tmp_path))
    _dream_install(store, cockpit)
    code, out, err = _run(store, capsys, "--cockpit-root", "~/cockpit")
    assert code == 0, err
    assert W.read_install(store.directory).cockpit_root == cockpit and f"cockpit_root: {cockpit}" in out.splitlines()


def test_a_tilde_cockpit_root_is_named_expanded_as_replaced(world, capsys, monkeypatch):
    store, cockpit, tmp_path = world
    monkeypatch.setenv("HOME", str(tmp_path))
    other = tmp_path / "other"
    other.mkdir()
    assert _run(store, capsys, "--cockpit-root", str(cockpit))[0] == 0
    code, out, _ = _run(store, capsys, "--cockpit-root", "~/other")
    assert code == 0 and f"replaced cockpit_root {cockpit} -> {other}" in out.splitlines()


def test_an_os_error_reading_the_dream_install_does_not_bar_the_write(world, capsys, monkeypatch):
    from levain.dream import agent as DA
    store, cockpit, _ = world
    _dream_install(store, cockpit)

    def _raise(_):
        raise PermissionError("scratch: unreadable")
    monkeypatch.setattr(DA, "read_install", _raise)
    code, _, err = _run(store, capsys, "--cockpit-root", str(cockpit))
    assert code == 0, err
    assert W.read_install(store.directory).cockpit_root == cockpit


def test_an_os_error_reading_the_previous_install_names_no_replacement(world, capsys, monkeypatch):
    store, cockpit, tmp_path = world
    other = tmp_path / "other"
    other.mkdir()
    assert _run(store, capsys, "--cockpit-root", str(cockpit))[0] == 0
    real, calls = W.read_install, []

    def _first_raises(d):
        calls.append(d)
        if len(calls) == 1:
            raise PermissionError("scratch: unreadable")
        return real(d)
    monkeypatch.setattr(W, "read_install", _first_raises)
    code, out, err = _run(store, capsys, "--cockpit-root", str(other))
    assert code == 0, err
    assert "replaced" not in out and real(store.directory).cockpit_root == other


def test_a_too_deep_values_is_said_not_raised(world, capsys):
    code, out, err = _run(world[0], capsys, "--cockpit-root", str(world[1]), "--values", '{"a":' * 20000)
    assert code == 1 and out == "" and "recursion" in err and "nothing written" in err
    assert not _file(world[0]).exists()


def test_an_unknown_launcher_error_reads_as_a_list(world, capsys):
    _, _, err = _run(world[0], capsys, "--cockpit-root", str(world[1]), "--launcher", "nope")
    assert "registered launchers: stub" in err and " are " not in err
