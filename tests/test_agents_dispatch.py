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
from levain.agents import spec as spec_mod
from levain.agents import supervise
from levain.autonomic.journal import RunJournal
from levain.coordinator.store import CoordStore
from levain.dream import agent as da
from levain.agents.role import add_role, parse_role
from tests.test_agents_launch import ENV, ROLES, SPECS, Rig, _launched, rig  # noqa: F401
from tests.test_agents_supervise_fire import Ex  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _dream_role(rig: Rig) -> None:
    # the executor is read from the role the occurrence's spec pins, so that role (a dream executor) is in the store
    add_role(rig.store, parse_role(ROLES[SPECS["job_push_check"]["role"]["name"]]))


def _rename(rig: Rig, name: str) -> None:
    # rewrites the mutable agents.name row only: that steers the launch-time input hook (which reads the row), but a
    # RUN reads the signed spec's name and refuses a row that differs (1104-8 F1), so run-path tests sign "dream"
    _dream_role(rig)
    with rig.store.write() as c:
        c.execute("UPDATE agents SET name = ? WHERE agent_id = ?", (name, rig.agent))


def _dream_rig(tmp_path) -> Rig:
    """A rig whose agent's spec is SIGNED under the name ``dream``, its dream-executor role in the store."""
    rig = Rig(tmp_path, name="dream")
    _dream_role(rig)
    return rig


def _fire_ready(rig: Rig):
    oid = rig.claim("k")
    assert rig.launch(oid, on_input=None).outcome == "launched"   # the dream's input hook is tested on its own below
    return oid    # no hand start: the launch admitted the run


def test_a_mutated_agents_name_row_never_gives_an_agent_the_dreams_hook_or_body(rig):
    # behaviour: which agent an occurrence IS comes from the frozen spec its spec_hash names, never from the mutable
    # agents.name row; a row renamed to "dream" after registration matches no signed name: no dream hook (the default
    # hook is dead input, the row stays admitted) and no run body (unwired)
    rig.link()
    oid = rig.claim("k")
    _rename(rig, "dream")
    hook = ctl._default_input_check(rig.store, oid)
    assert callable(hook)
    with pytest.raises(RuntimeError, match="cannot be read or verified"):
        hook({})
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": lambda: (lambda s, o, e: 0)}) == ctl.FIRE_UNWIRED
    res = rig.launch(oid)
    assert res.outcome == ctl.UNCLASSIFIED and rig.row(oid)["state"] == "admitted"


def test_a_dream_spec_whose_name_row_is_renamed_is_held_not_launched_unchecked(tmp_path):
    rig = _dream_rig(tmp_path)
    rig.link()
    oid = rig.claim("k")
    _rename(rig, "something-else")
    assert callable(ctl._default_input_check(rig.store, oid))    # dead input, never None (= input present)
    seen = []
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": lambda: (lambda s, o, e: seen.append(o) or 0)}) == \
        ctl.FIRE_UNWIRED and seen == []


def test_a_spec_that_does_not_hash_to_its_spec_hash_names_no_agent_and_the_launch_is_held(rig, monkeypatch):
    # behaviour: the spec is re-hashed on the way out; one that fails names no agent: no body (unwired) and the default
    # hook is dead input, so the row stays admitted and is never launched unchecked
    rig.link()
    oid = rig.claim("k")
    assert ctl._pinned_executor(rig.store, oid)[0] == "job_push_check"
    monkeypatch.setattr(spec_mod, "spec_hash", lambda agent_id, spec: "0" * 64)
    assert ctl._pinned_executor(rig.store, oid)[0] is None
    hook = ctl._default_input_check(rig.store, oid)
    assert callable(hook)
    with pytest.raises(RuntimeError, match="cannot be read or verified"):
        hook({})
    assert ctl.run_agent(rig.store, oid, 0) == ctl.FIRE_UNWIRED
    res = rig.launch(oid)
    assert res.outcome == ctl.UNCLASSIFIED and rig.row(oid)["state"] == "admitted"
    assert "input check failed" in res.reason


def test_a_scheduled_fire_of_the_dream_no_longer_ends_unwired(tmp_path):
    # behaviour: the module entry, run for a dream occurrence with no install facts, builds no run and exits 1 with
    # the row left launched; before the dispatch it exited FIRE_UNWIRED for every agent
    rig = _dream_rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    rig.link()
    oid = rig.claim("k")
    # launched with no input check: the dream's own input hook needs its install (below)
    assert rig.launch(oid, on_input=None).outcome == "launched"
    proc = ctl.spawn_fire(rig.store.directory, oid, env=ENV)
    assert proc.wait(60) == 1
    assert rig.row(oid)["state"] == "launched" and rig.row(oid)["pid"] == proc.pid
    assert "no run body is wired" not in (rig.store.directory / "fires" / f"{oid}.log").read_text()


def test_an_agent_with_no_run_body_still_refuses_unwired(tmp_path):
    rig = Rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    oid, _ = _launched(rig)
    assert ctl.spawn_fire(rig.store.directory, oid, env=ENV).wait(60) == ctl.FIRE_UNWIRED


def test_the_dream_occurrence_reaches_its_executor_once_and_ends_done(tmp_path, monkeypatch):
    # behaviour: run_agent resolves the dream's body by the executor its role pins; the body builds run_body's arguments from the
    # install, runs the one consolidate under the supervisor, and the row ends done
    rig = _dream_rig(tmp_path)
    rig.link()
    oid = _fire_ready(rig)
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


def test_run_agent_reads_the_executor_from_the_row_and_an_unknown_one_refuses_unwired(rig, tmp_path, capsys):
    seen = []
    rig.link()
    oid = rig.claim("k")
    assert rig.launch(oid).outcome == "launched"    # a fire runs a body only for a launched row it holds
    bodies = {"someone-else": lambda: (lambda s, o, e: seen.append(o) or 0)}
    assert ctl.run_agent(rig.store, oid, 0, bodies=bodies) == ctl.FIRE_UNWIRED and seen == []
    add_role(rig.store, parse_role(ROLES["notify-role"]))         # a dream executor: the name is no longer consulted
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": bodies["someone-else"]}) == ctl.FIRE_UNWIRED
    _rename(rig, "dream")                                          # the row alone: the signed name still differs
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": bodies["someone-else"]}) == ctl.FIRE_UNWIRED and seen == []
    (tmp_path / "signed").mkdir()
    dream = _dream_rig(tmp_path / "signed")                        # signed "dream": the dream's body runs
    dream.link()
    signed = _fire_ready(dream)                                    # launched: the launch admitted its run
    assert ctl.run_agent(dream.store, signed, 0, bodies={"dream": bodies["someone-else"]}) == 0 and seen == [signed]
    assert ctl.run_agent(rig.store, "oc-" + "0" * 32, 0, bodies=bodies) == ctl.FIRE_UNWIRED
    assert "no run body is wired" in capsys.readouterr().err


def test_only_the_dream_has_an_input_hook(rig, tmp_path):
    # behaviour: launch's default hook is the dream's for the dream (with no install facts it raises when run, so the
    # row stays admitted: dead input), and absent for any other agent, which launches as it always did
    rig.link()
    other = rig.claim("a")
    assert rig.launch(other).outcome == "launched"
    assert ctl._default_input_check(rig.store, other) is None
    (tmp_path / "d").mkdir()
    drig = Rig(tmp_path / "d", name="dream")
    drig.link()
    oid = drig.claim("b")
    assert callable(ctl._default_input_check(drig.store, oid))
    res = drig.launch(oid)
    assert res.outcome == ctl.UNCLASSIFIED and drig.row(oid)["state"] == "admitted"


def test_the_dream_hook_admits_through_the_verdict_and_a_quiet_day_ends_skipped(tmp_path, monkeypatch):
    rig = Rig(tmp_path, name="dream")
    rig.link()
    monkeypatch.setattr(da, "install_on_input", lambda agent_dir, **kw: (lambda row: False))
    oid = rig.claim("c")
    res = rig.launch(oid)
    assert res.outcome == ctl.SKIPPED_NO_INPUT and rig.row(oid)["state"] == "skipped-no-input"


def test_bool_on_input_is_the_verdicts_ok_and_nothing_else():
    ok = da.InputVerdict(True, "ready", "", "")
    no = da.InputVerdict(False, "quiet", "k", "r")
    assert da.bool_on_input(lambda row: ok)({}) is True and da.bool_on_input(lambda row: no)({}) is False


def _facts(tmp_path):
    db = tmp_path / "st" / "memory.db"
    db.parent.mkdir()
    db.write_bytes(b"")
    ck, cd = tmp_path / "ck", tmp_path / "cd"
    ck.mkdir()
    cd.mkdir()
    return dict(store_path=db, cockpit_root=ck, coord_dir=cd, zone="America/New_York")


def test_read_install_refuses_what_it_cannot_read(tmp_path):
    with pytest.raises(da.InstallRefused, match="cannot be read"):
        da.read_install(tmp_path)
    (tmp_path / da.INSTALL_FILE).write_text(json.dumps({"schema": 1, "store_path": "/x"}))
    with pytest.raises(da.InstallRefused, match="lacks zone"):
        da.read_install(tmp_path)
    (tmp_path / da.INSTALL_FILE).write_text(json.dumps({"store_path": "/x", "zone": "z"}))
    with pytest.raises(da.InstallRefused, match="schema None"):
        da.read_install(tmp_path)


def test_write_install_is_atomic_0600_and_round_trips(tmp_path):
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    path = da.write_install(ad, **f)
    assert path.stat().st_mode & 0o777 == 0o600 and [p.name for p in ad.iterdir()] == [da.INSTALL_FILE]
    inst = da.read_install(ad)
    assert inst.store_path == f["store_path"] and inst.zone == "America/New_York"


def test_a_stale_or_relative_install_is_refused_by_name(tmp_path):
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    da.write_install(ad, **f)
    f["coord_dir"].rmdir()
    with pytest.raises(da.InstallRefused, match="stale: coord_dir"):
        da.read_install(ad)
    with pytest.raises(da.InstallRefused, match="coord_dir"):
        da.write_install(ad, **{**f, "coord_dir": "rel/dir"})
    doc = json.loads((ad / da.INSTALL_FILE).read_text())
    doc["cockpit_root"] = "rel"
    (ad / da.INSTALL_FILE).write_text(json.dumps(doc))
    with pytest.raises(da.InstallRefused, match="not an absolute path"):
        da.read_install(ad)


def test_enable_with_a_store_dir_writes_the_facts_a_fire_then_gets_past(tmp_path, capsys):
    import argparse
    from levain.agents import store as agent_store
    from levain.dream.cli import add_parsers

    f = _facts(tmp_path)
    ag = tmp_path / "ag"
    agent_store.set_default_zone(agent_store.open_agent_store(ag), "America/New_York")
    p = argparse.ArgumentParser()
    add_parsers(p.add_subparsers())
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(ag), "--json"])
    assert args.func(args) == 0 and json.loads(capsys.readouterr().out)["install_facts"] is None
    with pytest.raises(da.InstallRefused, match="cannot be read"):      # the state before: nothing wrote the file
        da.read_install(ag)
    args = p.parse_args(["dream", "enable", "--agent-store-dir", str(ag), "--store-dir", str(f["store_path"].parent),
                         "--cockpit-root", str(f["cockpit_root"]), "--coord-dir", str(f["coord_dir"]), "--json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["install_facts"] == str(ag / da.INSTALL_FILE)
    assert da.read_install(ag).coord_dir == f["coord_dir"].resolve()
    bad = p.parse_args(["dream", "enable", "--agent-store-dir", str(ag), "--store-dir", str(tmp_path / "nope"),
                        "--cockpit-root", str(f["cockpit_root"]), "--coord-dir", str(f["coord_dir"])])
    assert bad.func(bad) == 1 and "no memory.db" in capsys.readouterr().err


def test_a_fire_with_the_enabled_facts_gets_past_install_to_its_next_refusal(tmp_path):
    # behaviour: with the facts file `dream enable` writes, the fire no longer ends on the missing install; whatever
    # it meets next is named in its log (no Opus compose runs: the facts file's exposure check refuses first)
    rig = _dream_rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    rig.link()
    oid = rig.claim("k")
    assert rig.launch(oid, on_input=None).outcome == "launched"
    da.write_install(rig.store.directory, **_facts(tmp_path))
    proc = ctl.spawn_fire(rig.store.directory, oid, env=ENV)
    code = proc.wait(60)
    log = (rig.store.directory / "fires" / f"{oid}.log").read_text()
    assert "cannot be read" not in log and "stale" not in log
    assert code == 1 and "ExposureRefused" in log


# --- D1 L3 r1 fixes (G1-G4) ------------------------------------------------------------------------------------------

def test_bool_on_input_raises_on_an_unreadable_verdict_so_the_day_stays_retryable():
    unread = da.InputVerdict(False, "unreadable", "unreadable", "OperationalError: locked")
    with pytest.raises(da.InputUnreadable, match="locked"):
        da.bool_on_input(lambda row: unread)({})
    unwired = da.InputVerdict(False, "unreadable", "unwired", "not wired")
    with pytest.raises(da.InputUnreadable):
        da.bool_on_input(lambda row: unwired)({})


def test_an_unreadable_memory_db_is_not_recorded_as_a_quiet_day(tmp_path):
    rig = Rig(tmp_path, name="dream")
    rig.link()
    f = _facts(tmp_path)
    f["store_path"].write_bytes(b"this is not a sqlite database" * 50)
    da.write_install(rig.store.directory, **f)
    oid = rig.claim("g1")
    res = rig.launch(oid)
    assert res.outcome == ctl.UNCLASSIFIED and rig.row(oid)["state"] == "admitted"
    assert "input check failed" in res.reason


def test_a_manual_dream_checked_while_admitted_is_judged_on_its_own_day_not_unreadable_for_launched_at(tmp_path):
    f = _facts(tmp_path)
    row = {"occurrence_id": "oc-" + "a" * 32, "agent_id": "ag-x", "kind": "manual", "state": "admitted",
           "launched_at": None}
    v = da.day_ready(f["store_path"], row, now=datetime(2026, 11, 3, 18, 0, tzinfo=timezone.utc),
                     zone=f["zone"], agent_dir=tmp_path, coord_dir=f["coord_dir"])
    assert "launched_at" not in v.reason


def test_an_explicit_none_on_input_still_means_no_input_check(tmp_path):
    rig = Rig(tmp_path, name="dream")
    rig.link()
    oid = rig.claim("g3")
    assert callable(ctl._default_input_check(rig.store, oid))
    res = rig.launch(oid, on_input=None)
    assert res.outcome == "launched"


def test_read_install_refuses_non_string_optional_facts_by_name(tmp_path):
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    da.write_install(ad, **f)
    base = json.loads((ad / da.INSTALL_FILE).read_text())
    for key, bad in (("facts_path", 5), ("facts_path", ["x"]), ("model", 5), ("claude_bin", ["claude"]),
                     ("model", {"a": 1})):
        (ad / da.INSTALL_FILE).write_text(json.dumps({**base, key: bad}))
        with pytest.raises(da.InstallRefused, match=key):
            da.read_install(ad)


# --- D1 L3 r2 fixes, commit A (A1-A3) ----------------------------------------------------------------------------------

def test_rewriting_the_install_keeps_the_facts_the_operator_set(tmp_path):
    # A1: a second `enable` names only the paths; facts_path, model and claude_bin set earlier must survive it
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    facts = tmp_path / "facts.json"
    facts.write_text("{}")
    da.write_install(ad, **f, facts_path=facts, model="claude-x", claude_bin="/opt/claude")
    da.write_install(ad, **f)
    inst = da.read_install(ad)
    assert inst.facts_path == facts and inst.model == "claude-x" and inst.claude_bin == "/opt/claude"
    other = tmp_path / "facts2.json"
    other.write_text("{}")
    da.write_install(ad, **f, facts_path=other)         # a flag that is passed overwrites
    assert da.read_install(ad).facts_path == other and da.read_install(ad).model == "claude-x"


def test_a_relative_or_missing_facts_path_is_refused_by_name_at_write_and_read(tmp_path):
    # A3
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    with pytest.raises(da.InstallRefused, match="facts_path.*not an absolute path"):
        da.write_install(ad, **f, facts_path="rel/facts.json")
    with pytest.raises(da.InstallRefused, match="facts_path.*does not exist"):
        da.write_install(ad, **f, facts_path=tmp_path / "nope.json")
    assert not (ad / da.INSTALL_FILE).exists()
    da.write_install(ad, **f)
    doc = json.loads((ad / da.INSTALL_FILE).read_text())
    doc["facts_path"] = "rel/facts.json"
    (ad / da.INSTALL_FILE).write_text(json.dumps(doc))
    with pytest.raises(da.InstallRefused, match="facts_path.*not an absolute path"):
        da.read_install(ad)


def test_a_never_created_default_dir_says_never_created_not_stale(tmp_path):
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    with pytest.raises(da.InstallRefused, match=r"coord_dir.*does not exist \(never created\)"):
        da.write_install(ad, **{**f, "coord_dir": tmp_path / "never"})


def _fire_row_rig(tmp_path):
    rig = Rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    rig.link()
    oid = rig.claim("k")
    assert rig.launch(oid).outcome == "launched"
    da.write_install(rig.store.directory, **_facts(tmp_path))
    return rig, oid


def test_fire_factory_gives_the_card_the_occurrences_day_manual_and_slot(tmp_path, monkeypatch):
    # A2: the occurrence row (manual, launched) decides what the card says; no executor is built for real here
    from levain.dream import executor as _ex
    monkeypatch.setattr(_ex, "DreamExecutor", lambda **kw: object())
    rig, oid = _fire_row_rig(tmp_path)
    kw = da.fire_factory(rig.store, oid, 0)
    row = rig.row(oid)
    assert kw["manual"] is True and kw["slot"] is None
    from levain.dream import ready as _r
    assert kw["day"] == _r.occurrence_dream_day(row, "America/New_York").isoformat()
    with pytest.raises(ValueError, match="not in the agent store"):
        da.fire_factory(rig.store, "oc-" + "0" * 32, 0)


def test_fire_factory_gives_a_scheduled_card_its_window_key_as_the_slot(tmp_path, monkeypatch):
    # C1: the occurrence row carries window_key, never a "key" column named slot
    from levain.dream import executor as _ex
    monkeypatch.setattr(_ex, "DreamExecutor", lambda **kw: object())
    rig = Rig(tmp_path)
    rig.world.clock.now = datetime.now(timezone.utc) - (rig.now() - rig.world.clock.now)
    rig.link()
    with rig.store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=rig.agent, kind="scheduled", key="s1", spec_hash=rig.spec_hash,
                                 binding_id="bind-0123456789abcdef", window_key="w:2026-11-02:22:00", now=rig.now())
    assert outcome == ctl.CLAIMED
    da.write_install(rig.store.directory, **_facts(tmp_path))
    kw = da.fire_factory(rig.store, oid, 0)
    assert kw["manual"] is False and kw["slot"] == "w:2026-11-02:22:00"


def test_write_install_checks_the_merged_model_before_replacing_a_working_install(tmp_path):
    # C2: the check ran after os.replace, so a bad value corrupted a working install although the call raised
    f = _facts(tmp_path)
    ad = tmp_path / "ag"
    ad.mkdir()
    da.write_install(ad, **f, model="claude-x")
    before = (ad / da.INSTALL_FILE).read_bytes()
    for bad in ({"model": ""}, {"claude_bin": "  "}):
        with pytest.raises(da.InstallRefused):
            da.write_install(ad, **f, **bad)
        assert (ad / da.INSTALL_FILE).read_bytes() == before
    assert [p.name for p in ad.iterdir()] == [da.INSTALL_FILE]


def test_fire_factory_binds_a_consolidate_that_handles_a_stop(tmp_path, monkeypatch):
    from levain.dream import executor as ex
    monkeypatch.setattr(ex, "DreamExecutor", lambda **kw: object())
    rig, oid = _fire_row_rig(tmp_path)
    seen = {}
    monkeypatch.setattr(ex, "consolidate_store", lambda *a, **kw: seen.update(kw))
    da.fire_factory(rig.store, oid, 0)["consolidate"]()
    assert seen["handle_stop"] is True


def test_an_ordinary_agent_gets_no_hook_whatever_its_role_row_holds_and_a_mismatch_still_does(rig, monkeypatch):
    # behaviour: the default hook reads only the signed name; an unreadable role row never gives a non-dream agent the
    # raising hook (it launches as before); a signed-name mismatch still gets the raising one
    rig.link()
    oid = rig.claim("k")
    from levain.agents import role as role_mod

    def boom(*a, **k):
        raise sqlite3.OperationalError("role row unreadable")
    monkeypatch.setattr(role_mod, "role_by_hash", boom)
    assert ctl._default_input_check(rig.store, oid) is None
    assert rig.launch(oid).outcome == "launched"
    _rename(rig, "other")
    assert callable(ctl._default_input_check(rig.store, oid))
