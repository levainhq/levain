"""14b Q5: the consent adapters protect the WHOLE levain home, the subtree the confinement floor denies, so a file
tool's write anywhere in it (the autonomic binding store among the rest) is agent config: a tagged consent request,
never a silent write. Bash stays unparsed (ruling 1)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from levain.autonomic.db import default_store_dir
from levain.consent.adapters import claude_code as cc
from levain.consent.queue import default_queue_dir
from levain.firing.confinement import LEVAIN_HOME_STATE_DIRS, levain_home_base
from tests._h4 import h4_env, h4_home


@pytest.fixture
def lhome(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".levain").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    return home


def _tagged(tool, inp, home):
    prot = cc.protected_paths(default_queue_dir(), cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    return cc.targets_agent_config(tool, inp, "/work/proj", home=home, protected=prot, environ=h4_env())


@pytest.mark.parametrize("tool,extra", [("Write", {"content": "x"}), ("Edit", {"old_string": "a", "new_string": "b"})])
def test_a_file_tool_write_to_the_binding_store_is_agent_config(lhome, tool, extra):
    """Reproduced RED before the fix (run_adapter.out, 10-25): protected_paths named only <home>/consent."""
    db = default_store_dir() / "autonomic.db"
    assert db.is_relative_to(lhome / ".levain")
    assert _tagged(tool, {"file_path": str(db), **extra}, lhome)


def test_an_edit_anywhere_in_the_levain_home_is_agent_config(lhome):
    assert _tagged("Edit", {"file_path": str(lhome / ".levain" / "notes" / "x.txt"), "old_string": "a",
                            "new_string": "b"}, lhome)


@pytest.mark.parametrize("rel", [".levain-other/autonomic/autonomic.db", ".levainx", "levain/autonomic.db"])
def test_a_name_that_only_looks_like_the_levain_home_is_not(lhome, rel):
    assert not _tagged("Write", {"file_path": str(lhome / rel), "content": "x"}, lhome)


def test_bash_into_the_levain_home_stays_unparsed(lhome):
    db = default_store_dir() / "autonomic.db"
    assert not _tagged("Bash", {"command": f"sqlite3 '{db}' 'update bindings set record=record'"}, lhome)


def test_the_queue_dir_stays_protected_inside_and_outside_the_home(lhome, tmp_path):
    inside = cc.protected_paths(default_queue_dir(), cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert Path(os.path.abspath(default_queue_dir())) in inside
    elsewhere = tmp_path / "q"
    got = cc.protected_paths(elsewhere, cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert Path(os.path.abspath(elsewhere)) in got
    assert cc.targets_agent_config("Write", {"file_path": str(elsewhere / "queue.db")}, "/work/proj",
                                   home=lhome, protected=got, environ=h4_env())


def test_the_adapter_and_the_floor_name_the_same_levain_home(lhome, tmp_path, monkeypatch):
    """One derivation (:func:`levain.firing.confinement.levain_home_base`), at $LEVAIN_HOME and at the default."""
    assert levain_home_base(Path.home(), environ=h4_env()) in cc.protected_paths(tmp_path / "q", cc.AdapterSettings(),
                                                                                 environ=h4_env(), home=h4_home())
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "elsewhere"))
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert Path(os.path.abspath(tmp_path / "elsewhere")) in got
    assert not any(p == lhome / ".levain" for p in got)


def test_protected_paths_and_the_floor_agree_on_a_non_default_levain_home(lhome, tmp_path, monkeypatch):
    """The floor (:func:`build_policy`, the deny list a confined shell runs under) and the adapter name one base."""
    from levain.firing.confinement import build_policy

    elsewhere = tmp_path / "state" / "lv"
    elsewhere.mkdir(parents=True)
    monkeypatch.setenv("LEVAIN_HOME", str(elsewhere))
    ent = tmp_path / "entities" / "coyote"
    (ent / ".levain").mkdir(parents=True)
    (ent / "workspace").mkdir()
    base = levain_home_base(Path.home(), environ=h4_env())
    assert base == Path(os.path.abspath(elsewhere))
    assert base in build_policy(ent).deny_read_write
    assert base in cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())


def test_a_levain_home_at_or_above_the_users_home_falls_back_to_its_state_dirs(lhome, monkeypatch):
    """As the floor does for a home it cannot hide whole: $LEVAIN_HOME=$HOME must not tag every file in the home."""
    monkeypatch.setenv("LEVAIN_HOME", str(lhome))
    got = cc.protected_paths(default_queue_dir(), cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert lhome not in got and Path(os.path.abspath(lhome)) not in got
    assert {lhome / name for name in LEVAIN_HOME_STATE_DIRS} <= set(got)
    assert not _tagged("Write", {"file_path": str(lhome / "project" / "main.py"), "content": "x"}, lhome)
    assert _tagged("Write", {"file_path": str(lhome / "autonomic" / "autonomic.db"), "content": "x"}, lhome)


def test_the_real_gate_denies_a_write_to_the_binding_store_when_nothing_can_decide(lhome):
    """The PreToolUse entry (:func:`main`) end to end, over a real queue in the temp home: with no authority set up
    nothing can sign T3, so outside ``default`` the floor denies and files nothing (no ping)."""
    db = default_store_dir() / "autonomic.db"
    ev = {"session_id": "s-1", "transcript_path": "/x.jsonl", "cwd": str(lhome), "permission_mode": "bypassPermissions",
          "hook_event_name": cc.GATE_EVENT, "tool_name": "Write", "tool_input": {"file_path": str(db), "content": "x"},
          "tool_use_id": "toolu_1"}
    out = io.StringIO()
    assert cc.main([], stdin=io.StringIO(json.dumps(ev)), stdout=out) == 0
    got = json.loads(out.getvalue())
    assert got["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert cc._queue_for(default_queue_dir(), cc.AdapterSettings(), home=h4_home()).pending() == []
    ev["tool_input"] = {"file_path": str(lhome / "work" / "a.txt"), "content": "x"}
    out = io.StringIO()
    cc.main([], stdin=io.StringIO(json.dumps(ev)), stdout=out)
    assert out.getvalue() == ""


# --- L3 r1 (1025+10 A_RULINGS_L3R1): one fallback predicate for the floor and the adapter; never raise; the
# injected home ---

def _case(kind, lhome, tmp_path):
    """(``$LEVAIN_HOME``, the event cwd) for each home the floor cannot deny whole."""
    if kind == "workspace":
        ws = tmp_path / "ws"
        (ws / "proj").mkdir(parents=True)
        return str(ws), str(ws / "proj")
    return {"root": "/", "tmp": "/tmp", "var_tmp": "/var/tmp", "two_parts": "/lane14b-two-parts",
            "home": str(lhome)}[kind], "/work/proj"


@pytest.mark.parametrize("kind", ["root", "tmp", "var_tmp", "two_parts", "home", "workspace"])
def test_a_levain_home_the_floor_cannot_deny_whole_falls_back_in_the_adapter_too(lhome, tmp_path, monkeypatch,
                                                                                  kind):
    """The floor's fallback (``_hides_the_entity``: a filesystem root, two path parts or fewer, a shared temp dir, or
    a directory holding the home or the workspace) is the adapter's: the state directories are agent config, an
    unrelated file under the same directory is not."""
    lh, cwd = _case(kind, lhome, tmp_path)
    monkeypatch.setenv("LEVAIN_HOME", lh)
    base = Path(os.path.abspath(lh))
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), cwd=cwd, environ=h4_env(), home=h4_home())
    assert base not in got
    assert {base / name for name in LEVAIN_HOME_STATE_DIRS} <= set(got)
    unrelated = str(Path(cwd) / "src" / "app.py") if kind == "workspace" else str(base / "lane14b-unrelated.txt")
    assert not cc.targets_agent_config("Write", {"file_path": unrelated, "content": "x"}, cwd, home=lhome,
                                       protected=got, environ=h4_env())
    assert cc.targets_agent_config("Write", {"file_path": str(base / "autonomic" / "autonomic.db"), "content": "x"},
                                   cwd, home=lhome, protected=got, environ=h4_env())


def test_the_floor_and_the_adapter_share_one_fallback_predicate(lhome, tmp_path, monkeypatch):
    """One function decides for both: forced true, the floor's ``_hides_the_entity`` and the adapter both fall back
    for an ordinary levain home."""
    from levain.firing import confinement as cf

    ordinary = tmp_path / "state" / "lv"
    monkeypatch.setenv("LEVAIN_HOME", str(ordinary))
    assert ordinary in cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    monkeypatch.setattr(cf, "too_wide_to_deny", lambda d, home, *within: True)
    assert cf._hides_the_entity(ordinary, lhome, tmp_path / "e", tmp_path / "e" / "workspace")
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert ordinary not in got and ordinary / "autonomic" in got


def test_protected_paths_does_not_raise_on_an_unresolvable_levain_home(lhome, tmp_path, monkeypatch):
    """It fails closed for the store, never an outage of every tool: the default levain home plus the queue dir."""
    monkeypatch.setenv("LEVAIN_HOME", "~lane14b_no_such_user/lv")
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert Path(os.path.abspath(tmp_path / "q")) in got
    assert lhome / ".levain" in got


def test_protected_paths_does_not_raise_when_the_home_cannot_be_found(lhome, tmp_path, monkeypatch):
    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=None)   # no home bound
    assert Path(os.path.abspath(tmp_path / "q")) in got
    assert cc.operator_home(h4_env()) / ".levain" in got


def test_an_unresolvable_levain_home_denies_no_read_and_still_gates_the_store(lhome, tmp_path, monkeypatch):
    """The hook entry over an explicit queue: before the fix the gate raised, ``gate_failed`` raised again on the
    same derivation and denied every call, a Read too."""
    qdir = tmp_path / "q"
    monkeypatch.setenv("LEVAIN_HOME", "~lane14b_no_such_user/lv")
    ev = {"session_id": "s-1", "transcript_path": "/x.jsonl", "cwd": str(lhome), "permission_mode": "bypassPermissions",
          "hook_event_name": cc.GATE_EVENT, "tool_name": "Read", "tool_input": {"file_path": str(lhome / "a.txt")},
          "tool_use_id": "toolu_1"}
    out = io.StringIO()
    assert cc.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(ev)), stdout=out) == 0
    assert out.getvalue() == ""
    ev["tool_name"], ev["tool_input"] = "Write", {"file_path": str(lhome / ".levain" / "autonomic" / "autonomic.db"),
                                                  "content": "x"}
    out = io.StringIO()
    cc.main(["--queue-dir", str(qdir)], stdin=io.StringIO(json.dumps(ev)), stdout=out)
    assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_protected_paths_takes_the_injected_home(lhome, tmp_path):
    alt = tmp_path / "alt"
    (alt / ".levain").mkdir(parents=True)
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=alt)
    assert alt / ".levain" in got
    assert lhome / ".levain" not in got


def test_the_gate_protects_the_levain_home_of_the_home_it_was_given(lhome, tmp_path):
    """``gate(home=...)``: the store under the injected home is agent config, as ``targets_agent_config`` already
    expands ``~`` under that home."""
    alt = tmp_path / "alt"
    (alt / ".levain").mkdir(parents=True)
    qdir = tmp_path / "q"
    settings = cc.AdapterSettings()
    ev = {"session_id": "s-1", "transcript_path": "/x.jsonl", "cwd": str(alt), "permission_mode": "bypassPermissions",
          "hook_event_name": cc.GATE_EVENT, "tool_name": "Write",
          "tool_input": {"file_path": "~/.levain/autonomic/autonomic.db", "content": "x"}, "tool_use_id": "toolu_1"}
    out = cc.gate(ev, cc._queue_for(qdir, settings, home=h4_home()), settings, home=alt, environ=h4_env())
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


# --- head rulings after L3 r1: the Codex adapter and the landing pass their home; an unresolvable queue directory
# still gates the levain home ---

def _patch(path):
    return {"command": f"*** Begin Patch\n*** Add File: {path}\n+x\n*** End Patch\n"}


def test_the_codex_gate_protects_the_levain_home_of_the_home_it_was_given(lhome, tmp_path):
    from levain.consent.adapters import codex as cx

    alt = tmp_path / "alt"
    (alt / ".levain").mkdir(parents=True)
    settings = cx.AdapterSettings()
    ev = {"hook_event_name": "PreToolUse", "session_id": "S1", "turn_id": "t1", "transcript_path": None,
          "cwd": str(alt), "model": "m", "permission_mode": "bypassPermissions", "tool_name": "apply_patch",
          "tool_input": _patch(alt / ".levain" / "autonomic" / "autonomic.db"), "tool_use_id": "call-S1"}
    out = cx.gate(ev, cx._queue_for(tmp_path / "q", settings, home=h4_home()), settings, home=alt, codex_dir=alt / ".codex", environ=h4_env())
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_the_landing_protects_the_levain_home_of_the_home_it_was_given(lhome, tmp_path):
    from levain.consent.landing import landing_tier

    alt = tmp_path / "alt"
    (alt / ".levain").mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    store = str(alt / ".levain" / "autonomic" / "autonomic.db")
    assert landing_tier(str(repo), [store], queue_dir=tmp_path / "q", home=alt, codex_dir=alt / ".codex") == "T3"
    assert landing_tier(str(repo), [str(repo / "src" / "app.py")], queue_dir=tmp_path / "q", home=alt,
                        codex_dir=alt / ".codex") == "T2"


def _installed_hook(adapter, tool, inp, cwd):
    """The hook as installed (no ``--queue-dir``): the default queue directory is derived from $LEVAIN_HOME."""
    ev = {"hook_event_name": "PreToolUse", "session_id": "S1", "turn_id": "t1", "transcript_path": "/x.jsonl",
          "cwd": str(cwd), "model": "m", "permission_mode": "bypassPermissions", "tool_name": tool, "tool_input": inp,
          "tool_use_id": "call-S1"}
    out = io.StringIO()
    assert adapter.main([], stdin=io.StringIO(json.dumps(ev)), stdout=out) == 0
    return json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] if out.getvalue() else None


def test_an_unresolvable_queue_dir_still_denies_a_write_to_the_levain_home_claude_code(lhome, monkeypatch):
    """Before: ``default_queue_dir()`` raised in ``main`` before the gate, so every call had no decision and a
    bypassPermissions session wrote the binding store."""
    monkeypatch.setenv("LEVAIN_HOME", "~lane14b_no_such_user/lv")
    store = str(lhome / ".levain" / "autonomic" / "autonomic.db")
    assert _installed_hook(cc, "Write", {"file_path": store, "content": "x"}, lhome) == "deny"
    assert _installed_hook(cc, "Read", {"file_path": str(lhome / "a.txt")}, lhome) is None
    assert _installed_hook(cc, "Write", {"file_path": str(lhome / "work" / "a.txt"), "content": "x"}, lhome) is None


def test_an_unresolvable_queue_dir_still_denies_a_write_to_the_levain_home_codex(lhome, monkeypatch):
    from levain.consent.adapters import codex as cx

    monkeypatch.setenv("LEVAIN_HOME", "~lane14b_no_such_user/lv")
    assert _installed_hook(cx, "apply_patch", _patch(lhome / ".levain" / "autonomic" / "autonomic.db"), lhome) == "deny"
    assert _installed_hook(cx, "apply_patch", _patch(lhome / "work" / "a.txt"), lhome) is None
    assert _installed_hook(cx, "exec_command", {"cmd": "ls"}, lhome) is None


# --- L3 r2: a failing temp-directory lookup or settings load never turns the store write ungated ---

def _custom_home(tmp_path, monkeypatch):
    custom = tmp_path / "opt" / "levain" / "state"
    custom.mkdir(parents=True)
    monkeypatch.setenv("LEVAIN_HOME", str(custom))
    return custom


def _no_tempdir(monkeypatch):
    import tempfile

    def boom():
        raise FileNotFoundError("No usable temporary directory found")

    monkeypatch.setattr(tempfile, "gettempdir", boom)


def test_a_failing_gettempdir_still_protects_the_resolved_custom_levain_home(lhome, tmp_path, monkeypatch):
    """Before: ``gettempdir()`` raised inside the predicate, the broad handler fell back to ``<home>/.levain`` and
    a store write under a custom ``$LEVAIN_HOME`` was ungated."""
    custom = _custom_home(tmp_path, monkeypatch)
    _no_tempdir(monkeypatch)
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())
    assert Path(os.path.abspath(custom)) in got
    assert lhome / ".levain" not in got


def test_a_failing_predicate_returns_the_resolved_base_never_the_default_home(lhome, tmp_path, monkeypatch):
    custom = _custom_home(tmp_path, monkeypatch)
    from levain.firing import confinement

    def boom(*a, **k):
        raise RuntimeError("predicate")

    monkeypatch.setattr(confinement, "too_wide_to_deny", boom)
    got = cc.levain_home_protected(None, None, environ=h4_env())
    assert got == [Path(os.path.abspath(custom))]


def test_too_wide_to_deny_keeps_the_fixed_temp_checks_when_gettempdir_fails(tmp_path, monkeypatch):
    from levain.firing.confinement import too_wide_to_deny

    _no_tempdir(monkeypatch)
    assert too_wide_to_deny(Path("/var/tmp").resolve(), tmp_path)
    assert not too_wide_to_deny(tmp_path / "a" / "b" / "c", tmp_path / "other")


def test_a_failing_settings_load_with_a_queue_dir_still_denies_a_store_write(lhome, tmp_path, monkeypatch):
    """``load_settings`` raising after ``queue_dir`` is set: ``main`` never read the event, so its handler read
    none and printed nothing."""
    from levain.consent.adapters import codex as cx

    def boom(*a, **k):
        raise OSError("settings unreadable")

    store = lhome / ".levain" / "autonomic" / "autonomic.db"
    for adapter, tool, inp in ((cc, "Write", {"file_path": str(store), "content": "x"}),
                               (cx, "apply_patch", _patch(store))):
        monkeypatch.setattr(adapter, "load_settings", boom)
        ev = {"hook_event_name": "PreToolUse", "session_id": "S1", "turn_id": "t1", "transcript_path": "/x.jsonl",
              "cwd": str(lhome), "model": "m", "permission_mode": "bypassPermissions", "tool_name": tool,
              "tool_input": inp, "tool_use_id": "call-S1"}
        out = io.StringIO()
        assert adapter.main(["--queue-dir", str(tmp_path / "q")], stdin=io.StringIO(json.dumps(ev)), stdout=out) == 0
        assert json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] == "deny", adapter.__name__


# --- 1025+16 deepseek whole-slice pass (14bA RULINGS_DEEPSEEK_WHOLE): a failing settings load is no outage; a
# levain home on a symlink is protected at every spelling a target can name it by ---

def _event(tool, inp, cwd):
    return {"hook_event_name": "PreToolUse", "session_id": "S1", "turn_id": "t1", "transcript_path": "/x.jsonl",
            "cwd": str(cwd), "model": "m", "permission_mode": "bypassPermissions", "tool_name": tool,
            "tool_input": inp, "tool_use_id": "call-S1"}


def _decision(adapter, argv, ev):
    out = io.StringIO()
    assert adapter.main(argv, stdin=io.StringIO(json.dumps(ev)), stdout=out) == 0
    return json.loads(out.getvalue())["hookSpecificOutput"]["permissionDecision"] if out.getvalue() else None


def test_a_failing_settings_load_with_a_queue_dir_is_no_outage_for_an_ordinary_call(lhome, tmp_path, monkeypatch):
    """deepseek MED 1: ``load_settings`` raising with ``--queue-dir`` set reaches ``gate_failed`` with the default
    settings, so the ordinary call gets no decision while the store write beside it is still denied."""
    from levain.consent.adapters import codex as cx

    def boom(*a, **k):
        raise OSError("settings unreadable")

    argv = ["--queue-dir", str(tmp_path / "q")]
    store = lhome / ".levain" / "autonomic" / "autonomic.db"
    for adapter, ordinary, gated in (
            (cc, [("Read", {"file_path": str(lhome / "a.txt")}),
                  ("Write", {"file_path": str(lhome / "work" / "a.txt"), "content": "x"})],
             ("Write", {"file_path": str(store), "content": "x"})),
            (cx, [("exec_command", {"cmd": "cat a.txt"}), ("apply_patch", _patch(lhome / "work" / "a.txt"))],
             ("apply_patch", _patch(store)))):
        monkeypatch.setattr(adapter, "load_settings", boom)
        for tool, inp in ordinary:
            assert _decision(adapter, argv, _event(tool, inp, lhome)) is None, (adapter.__name__, tool)
        assert _decision(adapter, argv, _event(*gated, lhome)) == "deny", adapter.__name__


def _linked_home(tmp_path, monkeypatch, at):
    """A levain home reached through a symlink: ``$LEVAIN_HOME`` names the link (``at="link"``) or the real
    directory (``at="real"``); returns (link, real)."""
    real = tmp_path / "mnt" / "state" / "levain"
    (real / "autonomic").mkdir(parents=True)
    link = tmp_path / "opt" / "levain"
    link.parent.mkdir(parents=True)
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("LEVAIN_HOME", str(link if at == "link" else real))
    return link, real


@pytest.mark.parametrize("at", ["link", "real"])
@pytest.mark.parametrize("via", ["link", "real", "real_parent"])
def test_a_levain_home_on_a_symlink_is_agent_config_at_every_spelling(lhome, tmp_path, monkeypatch, at, via):
    """deepseek MED 2: the floor denies the home at each of its spellings (``_spellings``); the adapter names one
    base and :func:`cc.targets_agent_config` checks both the protected roots and each target as written and as
    resolved, so a file tool naming the store through any spelling is tagged, in both adapters and the landing."""
    from levain.consent.adapters import codex as cx
    from levain.consent.landing import landing_tier

    link, real = _linked_home(tmp_path, monkeypatch, at)
    base = {"link": link, "real": real, "real_parent": Path(os.path.realpath(link.parent)) / link.name}[via]
    store = base / "autonomic" / "autonomic.db"
    qdir = tmp_path / "q"
    got = cc.protected_paths(qdir, cc.AdapterSettings(), cwd=str(lhome), environ=h4_env(), home=h4_home())
    assert cc.targets_agent_config("Edit", {"file_path": str(store), "old_string": "a", "new_string": "b"},
                                   str(lhome), home=lhome, protected=got, environ=h4_env())
    assert cx.targets_agent_config("apply_patch", _patch(store), str(lhome), home=lhome,
                                   codex_dir=lhome / ".codex", protected=got, environ=h4_env())
    repo = tmp_path / "repo"
    repo.mkdir()
    assert landing_tier(str(repo), [str(store)], queue_dir=qdir, home=lhome, codex_dir=lhome / ".codex") == "T3"
    assert not cc.targets_agent_config("Write", {"file_path": str(tmp_path / "mnt" / "state" / "other.txt"),
                                                 "content": "x"}, str(lhome), home=lhome, protected=got, environ=h4_env())


def test_a_levain_home_on_a_symlink_denies_the_resolved_store_write_through_the_hook(lhome, tmp_path, monkeypatch):
    """The installed hook end to end: ``$LEVAIN_HOME`` on the link, an Edit naming the resolved store is denied."""
    from levain.consent.adapters import codex as cx

    link, real = _linked_home(tmp_path, monkeypatch, "link")
    store = real / "autonomic" / "autonomic.db"
    assert _installed_hook(cc, "Edit", {"file_path": str(store), "old_string": "a", "new_string": "b"}, lhome) == "deny"
    assert _installed_hook(cx, "apply_patch", _patch(store), lhome) == "deny"


def test_a_levain_home_holding_its_venv_is_still_denied_whole(lhome, tmp_path, monkeypatch):
    """U4 (usite L3 f1, complement + deepseek MED): the installer's layout keeps the interpreter inside the levain
    home (``<LEVAIN_HOME>/venv``). The shared predicate must not call that home too wide, or the whole-home deny
    falls back to the state-dir list (fail-open)."""
    import sys

    from levain.firing import confinement as cf

    lh = tmp_path / "lv"
    exe = lh / "venv" / "bin" / "python"
    exe.parent.mkdir(parents=True)
    exe.touch()
    monkeypatch.setenv("LEVAIN_HOME", str(lh))
    monkeypatch.setattr(sys, "executable", str(exe))
    monkeypatch.setattr(sys, "prefix", str(lh / "venv"))
    assert not cf.too_wide_to_deny(lh, lhome)
    assert not cf._hides_the_entity(lh, lhome, tmp_path / "e", tmp_path / "e" / "workspace")
    assert lh in cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), environ=h4_env(), home=h4_home())
