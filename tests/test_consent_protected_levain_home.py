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


@pytest.fixture
def lhome(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".levain").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("LEVAIN_HOME", raising=False)
    return home


def _tagged(tool, inp, home):
    prot = cc.protected_paths(default_queue_dir(), cc.AdapterSettings())
    return cc.targets_agent_config(tool, inp, "/work/proj", home=home, protected=prot)


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
    inside = cc.protected_paths(default_queue_dir(), cc.AdapterSettings())
    assert Path(os.path.abspath(default_queue_dir())) in inside
    elsewhere = tmp_path / "q"
    got = cc.protected_paths(elsewhere, cc.AdapterSettings())
    assert Path(os.path.abspath(elsewhere)) in got
    assert cc.targets_agent_config("Write", {"file_path": str(elsewhere / "queue.db")}, "/work/proj",
                                   home=lhome, protected=got)


def test_the_adapter_and_the_floor_name_the_same_levain_home(lhome, tmp_path, monkeypatch):
    """One derivation (:func:`levain.firing.confinement.levain_home_base`), at $LEVAIN_HOME and at the default."""
    assert levain_home_base(Path.home()) in cc.protected_paths(tmp_path / "q", cc.AdapterSettings())
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "elsewhere"))
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings())
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
    base = levain_home_base(Path.home())
    assert base == Path(os.path.abspath(elsewhere))
    assert base in build_policy(ent).deny_read_write
    assert base in cc.protected_paths(tmp_path / "q", cc.AdapterSettings())


def test_a_levain_home_at_or_above_the_users_home_falls_back_to_its_state_dirs(lhome, monkeypatch):
    """As the floor does for a home it cannot hide whole: $LEVAIN_HOME=$HOME must not tag every file in the home."""
    monkeypatch.setenv("LEVAIN_HOME", str(lhome))
    got = cc.protected_paths(default_queue_dir(), cc.AdapterSettings())
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
    assert cc._queue_for(default_queue_dir(), cc.AdapterSettings()).pending() == []
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
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), cwd=cwd)
    assert base not in got
    assert {base / name for name in LEVAIN_HOME_STATE_DIRS} <= set(got)
    unrelated = str(Path(cwd) / "src" / "app.py") if kind == "workspace" else str(base / "lane14b-unrelated.txt")
    assert not cc.targets_agent_config("Write", {"file_path": unrelated, "content": "x"}, cwd, home=lhome,
                                       protected=got)
    assert cc.targets_agent_config("Write", {"file_path": str(base / "autonomic" / "autonomic.db"), "content": "x"},
                                   cwd, home=lhome, protected=got)


def test_the_floor_and_the_adapter_share_one_fallback_predicate(lhome, tmp_path, monkeypatch):
    """One function decides for both: forced true, the floor's ``_hides_the_entity`` and the adapter both fall back
    for an ordinary levain home."""
    from levain.firing import confinement as cf

    ordinary = tmp_path / "state" / "lv"
    monkeypatch.setenv("LEVAIN_HOME", str(ordinary))
    assert ordinary in cc.protected_paths(tmp_path / "q", cc.AdapterSettings())
    monkeypatch.setattr(cf, "too_wide_to_deny", lambda d, home, *within: True)
    assert cf._hides_the_entity(ordinary, lhome, tmp_path / "e", tmp_path / "e" / "workspace")
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings())
    assert ordinary not in got and ordinary / "autonomic" in got


def test_protected_paths_does_not_raise_on_an_unresolvable_levain_home(lhome, tmp_path, monkeypatch):
    """It fails closed for the store, never an outage of every tool: the default levain home plus the queue dir."""
    monkeypatch.setenv("LEVAIN_HOME", "~lane14b_no_such_user/lv")
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings())
    assert Path(os.path.abspath(tmp_path / "q")) in got
    assert lhome / ".levain" in got


def test_protected_paths_does_not_raise_when_the_home_cannot_be_found(lhome, tmp_path, monkeypatch):
    def no_home(cls):
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(no_home))
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings())
    assert Path(os.path.abspath(tmp_path / "q")) in got
    assert cc.operator_home() / ".levain" in got


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
    got = cc.protected_paths(tmp_path / "q", cc.AdapterSettings(), home=alt)
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
    out = cc.gate(ev, cc._queue_for(qdir, settings), settings, home=alt)
    assert out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"
