"""P2: AgentSpec versions, hashes, and the three lifecycle axes."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import sqlite3
from pathlib import Path

import pytest

from levain.agents.role import add_role, role_hash
from levain.agents.spec import (SpecError, can_fire, canonical_spec, feeder_group, finalize_spec, get_agent,
                                list_agents, mint_agent_id, parse_spec, register_spec, retire_agent, set_operation,
                                spec_hash, stored_specs)
from levain.agents.store import open_agent_store
from levain.authority import _canonical
from levain.cli import main

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ROLES = json.loads((FIX / "roles.json").read_text())
ZONE = "America/New_York"
AID = "ag-" + "ab" * 16


@pytest.fixture
def store(tmp_path):
    s = open_agent_store(tmp_path / "store")
    for r in ROLES.values():
        add_role(s, r)
    return s


def spec(job, **kw):
    d = copy.deepcopy(SPECS[job])
    d["role"]["role_hash"] = role_hash(ROLES[d["role"]["name"]])
    d.update(kw)
    return parse_spec(d)


def reg(store, job, **kw):
    return register_spec(store, spec(job, **kw), ZONE)


def test_hash_matches_the_formula_and_ignores_key_order():
    final = finalize_spec(spec("disk_floor"), ZONE)
    want = hashlib.sha256(b"levain-agentspec/1\n" + _canonical({"agent_id": AID, "spec": final}).encode()).hexdigest()
    assert spec_hash(AID, final) == want
    shuffled = json.loads(json.dumps(final), object_pairs_hook=lambda p: dict(reversed(p)))
    assert spec_hash(AID, shuffled) == want and canonical_spec(final) == canonical_spec(shuffled)


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(stop_at=3601), lambda s: s["trigger"].update(cron="1 * * * *"),
    lambda s: s["trigger"].update(zone="UTC"), lambda s: s["win"].update(check="other"),
    lambda s: s["on_failure"].update(pause_after=3), lambda s: s.update(memory="stateful"),
    lambda s: s.update(feeds=["x"]), lambda s: s["trigger"].update(window={"start": "22:00", "end": "04:00"}),
])
def test_any_field_change_moves_the_hash(mutate):
    base = finalize_spec(spec("disk_floor"), ZONE)
    other = copy.deepcopy(base)
    mutate(other)
    assert spec_hash(AID, base) != spec_hash(AID, other)
    assert spec_hash(AID, base) != spec_hash("ag-" + "cd" * 16, base)       # the id is hashed


def test_defaults_are_written_out_and_only_1_0_values_parse():
    s = finalize_spec(spec("disk_floor"), ZONE)
    assert (s["overlap"], s["missed"], s["placement"], s["memory"], s["on_failure"], s["feeds"]) == \
        ("skip", "skip", "local", "stateless", {"pause_after": 2}, [])
    for bad in ({"overlap": "queue"}, {"missed": "once_on_wake"}, {"placement": "vps"}, {"memory": "x"}):
        raw = copy.deepcopy(SPECS["disk_floor"])
        raw["role"]["role_hash"] = "0" * 64
        raw.update(bad)
        with pytest.raises(SpecError, match=next(iter(bad))):
            parse_spec(raw)


@pytest.mark.parametrize("cron", ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a b c d e"])
def test_bad_cron_is_refused(cron):
    with pytest.raises(SpecError, match="cron"):
        spec("disk_floor", trigger={"kind": "schedule", "cron": cron, "zone": ZONE})


@pytest.mark.parametrize("cron", ["mon jan * * *", "0 jan * * *", "0 0 mon * *", "0 0 * mon *", "0 0 * * jan"])
def test_cron_names_only_in_their_own_field(cron):
    with pytest.raises(SpecError, match="cron"):
        spec("disk_floor", trigger={"kind": "schedule", "cron": cron, "zone": ZONE})


def test_cron_names_where_they_belong_parse():
    assert spec("disk_floor", trigger={"kind": "schedule", "cron": "0 7 * jan-mar mon-fri", "zone": ZONE})[
        "trigger"]["cron"] == "0 7 * jan-mar mon-fri"


def test_window_and_zone_are_checked():
    ok = spec("disk_floor", trigger={"kind": "schedule", "cron": "*/20 22-23,0-3 * * *", "zone": ZONE,
                                     "window": {"start": "22:00", "end": "04:00"}})
    assert ok["trigger"]["window"] == {"start": "22:00", "end": "04:00"}
    with pytest.raises(SpecError, match="window"):
        spec("disk_floor", trigger={"kind": "schedule", "cron": "* * * * *", "window": {"start": "25:00", "end": "04:00"}})
    with pytest.raises(SpecError, match="trigger.zone"):
        spec("disk_floor", trigger={"kind": "schedule", "cron": "* * * * *", "zone": "Mars/Olympus"})


def test_register_versions_are_immutable_and_idempotent(store):
    a, h1, v, created = reg(store, "disk_floor")
    assert a.startswith("ag-") and len(a) == 35 and v == 1 and created
    assert reg(store, "disk_floor") == (a, h1, 1, False)
    with pytest.raises(SpecError, match="--revise"):
        reg(store, "disk_floor", stop_at=7200)
    a2, h2, v2, created2 = register_spec(store, spec("disk_floor", stop_at=7200), ZONE, revise=True)
    assert (a2, v2, created2) == (a, 2, True) and h2 != h1
    got = get_agent(store, "disk_floor")
    assert got["version"] == 2 and got["versions"] == [1, 2] and got["spec"]["stop_at"] == 7200
    assert get_agent(store, "disk_floor", 1)["spec_hash"] == h1
    with store.write() as conn:
        for sql in ("UPDATE agent_specs SET spec = 'x'", "DELETE FROM agent_specs"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)


def test_a_revert_to_an_earlier_version_is_refused_naming_it(store, tmp_path, capsys):
    a, *_ = reg(store, "disk_floor")
    register_spec(store, spec("disk_floor", stop_at=7200), ZONE, revise=True)
    with pytest.raises(SpecError, match=f"version 1 of agent {a}.*written once and never again"):
        register_spec(store, spec("disk_floor"), ZONE, revise=True)
    raw = copy.deepcopy(SPECS["disk_floor"])
    raw["role"]["role_hash"] = role_hash(ROLES["notify-role"])
    f = tmp_path / "v1.json"
    f.write_text(json.dumps(raw))
    for extra in ([], ["--revise"]):
        assert main(["agents", "--store-dir", str(store.directory), "register", "--from", str(f), *extra]) == 1
        out = capsys.readouterr().out
        assert f"rule 4: the content is version 1 of agent {a}" in out and "IntegrityError" not in out
        assert "retire" in out and "--revise" in out
    assert get_agent(store, "disk_floor")["versions"] == [1, 2]


def test_mint_agent_id_shape():
    ids = {mint_agent_id() for _ in range(4)}
    assert len(ids) == 4 and all(re.fullmatch(r"ag-[0-9a-f]{32}", i) for i in ids)


def test_the_stored_text_is_rehashed_on_read(store):
    a, *_ = reg(store, "disk_floor")
    with store.write() as conn:
        conn.execute("DROP TRIGGER agent_specs_frozen")
        conn.execute("UPDATE agent_specs SET spec = replace(spec, '3600', '1')")
    with pytest.raises(SpecError, match="does not hash"):
        get_agent(store, a)
    with pytest.raises(SpecError, match="does not hash"):
        stored_specs(store)


# ---- the three lifecycle axes -------------------------------------------------------------------------------------
def axes(store, name):
    a = get_agent(store, name)
    return a["registration"], a["operation"]


def test_a_pause_survives_everything_except_resume(store):
    a, *_ = reg(store, "disk_floor")
    set_operation(store, "disk_floor", "paused")
    assert axes(store, "disk_floor") == ("registered", "paused")
    reg(store, "disk_floor")                                                     # re-register, same content
    register_spec(store, spec("disk_floor", stop_at=90), ZONE, revise=True)      # a new version (an edit)
    assert axes(store, "disk_floor") == ("registered", "paused")
    set_operation(store, "disk_floor", "paused")                                 # pausing twice changes nothing
    retire_agent(store, "disk_floor")
    assert axes(store, a) == ("retired", "paused")                               # retire leaves operation alone
    set_operation(store, a, "active")
    assert axes(store, a) == ("retired", "active")


def test_resume_is_the_only_clear_and_resets_the_failure_streak(store):
    a, *_ = reg(store, "disk_floor")
    with store.write() as conn:
        conn.execute("UPDATE agents SET operation='paused', failure_streak=2 WHERE agent_id=?", (a,))
    assert get_agent(store, a)["failure_streak"] == 2
    set_operation(store, a, "active")
    got = get_agent(store, a)
    assert (got["operation"], got["failure_streak"]) == ("active", 0)


def test_resume_touches_only_members_it_moves(store):
    feeder_store(store)
    with store.write() as conn:
        conn.execute("UPDATE agents SET operation='paused', failure_streak=2 WHERE name='email_sensor_argus'")
        conn.execute("UPDATE agents SET failure_streak=1 WHERE name='email_classifier_argus'")   # active, not paused
    out = {a["name"]: changed for a, changed in set_operation(store, "email_sensor_argus", "active")}
    assert out == {"email_sensor_argus": True, "email_classifier_argus": False, "email_event_reconciler": False}
    assert get_agent(store, "email_classifier_argus")["failure_streak"] == 1      # untouched
    assert get_agent(store, "email_sensor_argus")["failure_streak"] == 0          # moved from paused: reset


def test_a_retire_and_re_register_is_a_new_agent_that_starts_active(store):
    a, *_ = reg(store, "disk_floor")
    set_operation(store, "disk_floor", "paused")
    retire_agent(store, a)
    b, *_ = reg(store, "disk_floor")
    assert b != a and axes(store, b) == ("registered", "active") and axes(store, a) == ("retired", "paused")


def test_can_fire_needs_all_three_axes():
    assert can_fire("registered", True, "active")
    assert not can_fire("retired", True, "active")
    assert not can_fire("registered", False, "active")
    assert not can_fire("registered", True, "paused")


def feeder_store(store):
    for job in ("email_classifier_argus", "email_event_reconciler"):
        reg(store, job, **({"win": {"kind": "verifier", "check": "attested"}} if "reconciler" in job else {}))
    reg(store, "email_sensor_argus")
    reg(store, "disk_floor")


def test_a_feeder_group_pauses_and_resumes_together(store):
    feeder_store(store)
    names = {g["name"] for g in feeder_group(store, "email_classifier_argus")}
    assert names == {"email_classifier_argus", "email_event_reconciler", "email_sensor_argus"}
    out = set_operation(store, "email_classifier_argus", "paused")
    assert {a["name"] for a, changed in out if changed} == names
    ops = {a["name"]: a["operation"] for a in list_agents(store)}
    assert ops["disk_floor"] == "active" and {ops[n] for n in names} == {"paused"}
    set_operation(store, "email_sensor_argus", "active")
    assert {a["operation"] for a in list_agents(store)} == {"active"}


def test_retire_lists_the_reader_and_the_group_and_frees_the_name(store):
    feeder_store(store)
    agent, reader, left = retire_agent(store, "email_sensor_argus")
    assert agent["registration"] == "retired" and reader == "email_classifier_argus"
    assert left == ["email_classifier_argus", "email_event_reconciler"]      # what the retirement leaves behind
    agent2, reader2, left2 = retire_agent(store, "email_classifier_argus")
    assert reader2 == "email_actuator_argus" and left2 == []      # the sensor, its only link, is retired
    assert [a["name"] for a in list_agents(store)] == ["disk_floor", "email_event_reconciler"]
    assert len(list_agents(store, include_retired=True)) == 4
    with pytest.raises(SpecError, match="already retired"):
        retire_agent(store, agent["agent_id"])
    with pytest.raises(SpecError, match="no live agent named 'email_sensor_argus'; retired"):
        get_agent(store, "email_sensor_argus")


def test_cli_verbs_round_trip(tmp_path, capsys):
    d = ["agents", "--store-dir", str(tmp_path / "store")]
    s = open_agent_store(tmp_path / "store")
    add_role(s, ROLES["notify-role"])
    raw = copy.deepcopy(SPECS["disk_floor"])
    raw["role"]["role_hash"] = role_hash(ROLES["notify-role"])
    f = tmp_path / "s.json"
    f.write_text(json.dumps(raw))
    assert main([*d, "register", "--from", str(f)]) == 0
    assert "registered" in capsys.readouterr().out
    assert main([*d, "register", "--from", str(f)]) == 0
    assert "already registered" in capsys.readouterr().out
    assert main([*d, "list"]) == 0 and "disk_floor" in capsys.readouterr().out
    assert main([*d, "show", "disk_floor"]) == 0
    shown = capsys.readouterr().out
    assert "review: required" in shown and "acts_outward=True" in shown and '"stop_at": 3600' in shown
    assert main([*d, "pause", "disk_floor"]) == 0 and "paused" in capsys.readouterr().out
    assert main([*d, "pause", "disk_floor"]) == 0 and "(already)" in capsys.readouterr().out
    assert main([*d, "resume", "disk_floor"]) == 0 and "active" in capsys.readouterr().out
    assert main([*d, "retire", "disk_floor"]) == 0 and "retired" in capsys.readouterr().out
    assert main([*d, "list"]) == 0 and capsys.readouterr().out == ""
    assert main([*d, "show", "nobody"]) == 1 and "no agent" in capsys.readouterr().err
    raw["stop_at"] = 5
    f.write_text(json.dumps(raw))
    assert main([*d, "register", "--from", str(f), "--warn"]) == 0
    assert "pass     disk_floor" in capsys.readouterr().out
    assert main([*d, "list"]) == 0 and capsys.readouterr().out == ""            # --warn stored nothing
