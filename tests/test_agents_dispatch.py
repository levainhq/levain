"""D1-2's dispatch hunk: a dream occurrence's fire reaches the dream's run body, and the dream alone has its input
hook (BUILD_PLAN D1-2; D1_EXECUTOR §12). Every store is a real ``/2`` agent store; nothing here composes: the executor
is a stub and the consolidate a counter."""
from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from levain.agents import controller as ctl
from levain.agents import supervise
from levain.autonomic.journal import RunJournal
from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from tests.test_agents_launch import ENV, Rig, _launched, rig  # noqa: F401
from tests.test_agents_supervise_fire import Ex  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _rename(rig: Rig, name: str) -> None:
    with rig.store.write() as c:
        c.execute("UPDATE agents SET name = ? WHERE agent_id = ?", (name, rig.agent))


def _fire_ready(rig: Rig, name: str):
    oid = rig.claim("k")
    assert rig.launch(oid).outcome == "launched"
    _rename(rig, name)
    RunJournal(rig.store.directory).start(rig.row(oid)["run_id"], binding_id="bind-0123456789abcdef")
    return oid


def test_a_scheduled_fire_of_the_dream_no_longer_ends_unwired(tmp_path):
    # behaviour: the module entry, run for a dream occurrence with no install facts, builds no run and exits 1 with
    # the row left launched; before the dispatch it exited FIRE_UNWIRED for every agent
    rig = Rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    rig.link()
    oid = rig.claim("k")
    assert rig.launch(oid).outcome == "launched"
    _rename(rig, "dream")        # launched under the stub's name: the dream's own launch needs its install (below)
    proc = ctl.spawn_fire(rig.store.directory, oid, env=ENV)
    assert proc.wait(60) == 1
    assert rig.row(oid)["state"] == "launched" and rig.row(oid)["pid"] == proc.pid
    assert "no run body is wired" not in (rig.store.directory / "fires" / f"{oid}.log").read_text()


def test_an_agent_with_no_run_body_still_refuses_unwired(tmp_path):
    rig = Rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    oid, _ = _launched(rig)
    assert ctl.spawn_fire(rig.store.directory, oid, env=ENV).wait(60) == ctl.FIRE_UNWIRED


def test_the_dream_occurrence_reaches_its_executor_once_and_ends_done(rig, tmp_path, monkeypatch):
    # behaviour: run_agent resolves the dream's body by the agent's name; the body builds run_body's arguments from the
    # install, runs the one consolidate under the supervisor, and the row ends done
    rig.link()
    oid = _fire_ready(rig, "dream")
    calls = []

    def factory(store, occurrence_id, epoch):
        sup = supervise.Supervisor(store, authority=rig.authority, clock=rig.now, journal=RunJournal(store.directory),
                                   hooks=supervise.Hooks(verify=lambda end: None))
        q = tmp_path / "q"
        q.mkdir(mode=0o700)
        return {"sup": sup, "store_path": tmp_path / "memory.db", "occ_dir": tmp_path / "occ", "executor": Ex(),
                "consolidate": lambda: calls.append(occurrence_id) or {"outcome": "empty"},
                "review_store": CoordStore(q)}

    monkeypatch.setattr(da, "fire_factory", factory)
    assert ctl.run_agent(rig.store, oid, 0) == 0
    assert calls == [oid] and rig.row(oid)["state"] == "done"


def test_run_agent_reads_the_agent_from_the_row_and_an_unknown_one_refuses_unwired(rig, capsys):
    seen = []
    oid = rig.claim("k")
    bodies = {"someone-else": lambda: (lambda s, o, e: seen.append(o) or 0)}
    assert ctl.run_agent(rig.store, oid, 0, bodies=bodies) == ctl.FIRE_UNWIRED and seen == []
    _rename(rig, "someone-else")
    assert ctl.run_agent(rig.store, oid, 0, bodies=bodies) == 0 and seen == [oid]
    assert ctl.run_agent(rig.store, "oc-" + "0" * 32, 0, bodies=bodies) == ctl.FIRE_UNWIRED
    assert "no run body is wired" in capsys.readouterr().err


def test_only_the_dream_has_an_input_hook(rig):
    # behaviour: launch's default hook is the dream's for the dream (with no install facts it raises when run, so the
    # row stays admitted: dead input), and absent for any other agent, which launches as it always did
    rig.link()
    other = rig.claim("a")
    assert rig.launch(other).outcome == "launched"
    assert ctl._default_input_check(rig.store, other) is None
    rig.finish(other)
    _rename(rig, "dream")
    oid = rig.claim("b")
    assert callable(ctl._default_input_check(rig.store, oid))
    res = rig.launch(oid)
    assert res.outcome == ctl.UNCLASSIFIED and rig.row(oid)["state"] == "admitted"


def test_the_dream_hook_admits_through_the_verdict_and_a_quiet_day_ends_skipped(rig, monkeypatch):
    rig.link()
    _rename(rig, "dream")
    monkeypatch.setattr(da, "install_on_input", lambda agent_dir, **kw: (lambda row: False))
    oid = rig.claim("c")
    res = rig.launch(oid)
    assert res.outcome == ctl.SKIPPED_NO_INPUT and rig.row(oid)["state"] == "skipped-no-input"


def test_bool_on_input_is_the_verdicts_ok_and_nothing_else():
    ok = da.InputVerdict(True, "ready", "", "")
    no = da.InputVerdict(False, "quiet", "k", "r")
    assert da.bool_on_input(lambda row: ok)({}) is True and da.bool_on_input(lambda row: no)({}) is False


def test_read_install_refuses_what_it_cannot_read(tmp_path):
    with pytest.raises(ValueError, match="cannot be read"):
        da.read_install(tmp_path)
    (tmp_path / da.INSTALL_FILE).write_text(json.dumps({"store_path": "/x"}))
    with pytest.raises(ValueError, match="lacks cockpit_root"):
        da.read_install(tmp_path)
    (tmp_path / da.INSTALL_FILE).write_text(json.dumps({"store_path": "/x", "cockpit_root": "/c", "coord_dir": "/q",
                                                       "zone": "America/New_York"}))
    assert da.read_install(tmp_path).zone == "America/New_York"
