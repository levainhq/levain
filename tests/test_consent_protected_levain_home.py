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
