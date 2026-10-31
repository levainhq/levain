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
    final = finalize_spec(spec("job_push_check"), ZONE)
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
    base = finalize_spec(spec("job_push_check"), ZONE)
    other = copy.deepcopy(base)
    mutate(other)
    assert spec_hash(AID, base) != spec_hash(AID, other)
    assert spec_hash(AID, base) != spec_hash("ag-" + "cd" * 16, base)       # the id is hashed


def test_defaults_are_written_out_and_only_1_0_values_parse():
    s = finalize_spec(spec("job_push_check"), ZONE)
    assert (s["overlap"], s["missed"], s["placement"], s["memory"], s["on_failure"], s["feeds"]) == \
        ("skip", "skip", "local", "stateless", {"pause_after": 2}, [])
    for bad in ({"overlap": "queue"}, {"missed": "once_on_wake"}, {"placement": "vps"}, {"memory": "x"}):
        raw = copy.deepcopy(SPECS["job_push_check"])
        raw["role"]["role_hash"] = "0" * 64
        raw.update(bad)
        with pytest.raises(SpecError, match=next(iter(bad))):
            parse_spec(raw)


@pytest.mark.parametrize("cron", ["* * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a b c d e"])
def test_bad_cron_is_refused(cron):
    with pytest.raises(SpecError, match="cron"):
        spec("job_push_check", trigger={"kind": "schedule", "cron": cron, "zone": ZONE})


@pytest.mark.parametrize("cron", ["mon jan * * *", "0 jan * * *", "0 0 mon * *", "0 0 * mon *", "0 0 * * jan"])
def test_cron_names_only_in_their_own_field(cron):
    with pytest.raises(SpecError, match="cron"):
        spec("job_push_check", trigger={"kind": "schedule", "cron": cron, "zone": ZONE})


def test_cron_names_where_they_belong_parse():
    assert spec("job_push_check", trigger={"kind": "schedule", "cron": "0 7 * jan-mar mon-fri", "zone": ZONE})[
        "trigger"]["cron"] == "0 7 * jan-mar mon-fri"


def test_window_and_zone_are_checked():
    ok = spec("job_push_check", trigger={"kind": "schedule", "cron": "*/20 22-23,0-3 * * *", "zone": ZONE,
                                     "window": {"start": "22:00", "end": "04:00"}})
    assert ok["trigger"]["window"] == {"start": "22:00", "end": "04:00"}
    with pytest.raises(SpecError, match="window"):
        spec("job_push_check", trigger={"kind": "schedule", "cron": "* * * * *", "window": {"start": "25:00", "end": "04:00"}})
    with pytest.raises(SpecError, match="trigger.zone"):
        spec("job_push_check", trigger={"kind": "schedule", "cron": "* * * * *", "zone": "Mars/Olympus"})


def test_register_versions_are_immutable_and_idempotent(store):
    a, h1, v, created = reg(store, "job_push_check")
    assert a.startswith("ag-") and len(a) == 35 and v == 1 and created
    assert reg(store, "job_push_check") == (a, h1, 1, False)
    with pytest.raises(SpecError, match="--revise"):
        reg(store, "job_push_check", stop_at=7200)
    a2, h2, v2, created2 = register_spec(store, spec("job_push_check", stop_at=7200), ZONE, revise=True)
    assert (a2, v2, created2) == (a, 2, True) and h2 != h1
    got = get_agent(store, "job_push_check")
    assert got["version"] == 2 and got["versions"] == [1, 2] and got["spec"]["stop_at"] == 7200
    assert get_agent(store, "job_push_check", 1)["spec_hash"] == h1
    with store.write() as conn:
        for sql in ("UPDATE agent_specs SET spec = 'x'", "DELETE FROM agent_specs"):
            with pytest.raises(sqlite3.DatabaseError):
                conn.execute(sql)


def test_a_revert_to_an_earlier_version_is_refused_naming_it(store, tmp_path, capsys):
    a, *_ = reg(store, "job_push_check")
    register_spec(store, spec("job_push_check", stop_at=7200), ZONE, revise=True)
    with pytest.raises(SpecError, match=f"version 1 of agent {a}.*written once and never again"):
        register_spec(store, spec("job_push_check"), ZONE, revise=True)
    raw = copy.deepcopy(SPECS["job_push_check"])
    raw["role"]["role_hash"] = role_hash(ROLES["notify-role"])
    f = tmp_path / "v1.json"
    f.write_text(json.dumps(raw))
    for extra in ([], ["--revise"]):
        assert main(["agents", "--store-dir", str(store.directory), "register", "--from", str(f), *extra]) == 1
        out = capsys.readouterr().out
        assert f"rule 4: the content is version 1 of agent {a}" in out and "IntegrityError" not in out
        assert "retire" in out and "--revise" in out
    assert get_agent(store, "job_push_check")["versions"] == [1, 2]


def test_mint_agent_id_shape():
    ids = {mint_agent_id() for _ in range(4)}
    assert len(ids) == 4 and all(re.fullmatch(r"ag-[0-9a-f]{32}", i) for i in ids)


def test_the_stored_text_is_rehashed_on_read(store):
    a, *_ = reg(store, "job_push_check")
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
    a, *_ = reg(store, "job_push_check")
    set_operation(store, "job_push_check", "paused")
    assert axes(store, "job_push_check") == ("registered", "paused")
    reg(store, "job_push_check")                                                     # re-register, same content
    register_spec(store, spec("job_push_check", stop_at=90), ZONE, revise=True)      # a new version (an edit)
    assert axes(store, "job_push_check") == ("registered", "paused")
    set_operation(store, "job_push_check", "paused")                                 # pausing twice changes nothing
    retire_agent(store, "job_push_check")
    assert axes(store, a) == ("retired", "paused")                               # retire leaves operation alone
    set_operation(store, a, "active")
    assert axes(store, a) == ("retired", "active")


def test_resume_is_the_only_clear_and_resets_the_failure_streak(store):
    a, *_ = reg(store, "job_push_check")
    with store.write() as conn:
        conn.execute("UPDATE agents SET operation='paused', failure_streak=2 WHERE agent_id=?", (a,))
    assert get_agent(store, a)["failure_streak"] == 2
    set_operation(store, a, "active")
    got = get_agent(store, a)
    assert (got["operation"], got["failure_streak"]) == ("active", 0)


def test_resume_touches_only_members_it_moves(store):
    feeder_store(store)
    with store.write() as conn:
        conn.execute("UPDATE agents SET operation='paused', failure_streak=2 WHERE name='job_feeder_member'")
        conn.execute("UPDATE agents SET failure_streak=1 WHERE name='job_feeder_downstream'")   # active, not paused
    out = {a["name"]: changed for a, changed in set_operation(store, "job_feeder_member", "active")}
    assert out == {"job_feeder_member": True, "job_feeder_downstream": False, "job_corpus_reader": False}
    assert get_agent(store, "job_feeder_downstream")["failure_streak"] == 1      # untouched
    assert get_agent(store, "job_feeder_member")["failure_streak"] == 0          # moved from paused: reset


def test_a_retire_and_re_register_is_a_new_agent_that_starts_active(store):
    a, *_ = reg(store, "job_push_check")
    set_operation(store, "job_push_check", "paused")
    retire_agent(store, a)
    b, *_ = reg(store, "job_push_check")
    assert b != a and axes(store, b) == ("registered", "active") and axes(store, a) == ("retired", "paused")


def test_can_fire_needs_all_three_axes():
    assert can_fire("registered", True, "active")
    assert not can_fire("retired", True, "active")
    assert not can_fire("registered", False, "active")
    assert not can_fire("registered", True, "paused")


def feeder_store(store):
    for job in ("job_feeder_downstream", "job_corpus_reader"):
        reg(store, job, **({"win": {"kind": "verifier", "check": "attested"}} if job == "job_corpus_reader" else {}))
    reg(store, "job_feeder_member")
    reg(store, "job_push_check")


def test_a_feeder_group_pauses_and_resumes_together(store):
    feeder_store(store)
    names = {g["name"] for g in feeder_group(store, "job_feeder_downstream")}
    assert names == {"job_feeder_downstream", "job_corpus_reader", "job_feeder_member"}
    out = set_operation(store, "job_feeder_downstream", "paused")
    assert {a["name"] for a, changed in out if changed} == names
    ops = {a["name"]: a["operation"] for a in list_agents(store)}
    assert ops["job_push_check"] == "active" and {ops[n] for n in names} == {"paused"}
    set_operation(store, "job_feeder_member", "active")
    assert {a["operation"] for a in list_agents(store)} == {"active"}


def test_retire_lists_the_reader_and_the_group_and_frees_the_name(store):
    feeder_store(store)
    agent, reader, left = retire_agent(store, "job_feeder_member")
    assert agent["registration"] == "retired" and reader == "job_feeder_downstream"
    assert left == ["job_corpus_reader", "job_feeder_downstream"]      # what the retirement leaves behind
    agent2, reader2, left2 = retire_agent(store, "job_feeder_downstream")
    assert reader2 == "job_actuator" and left2 == []      # the feeder member, its only link, is retired
    assert [a["name"] for a in list_agents(store)] == ["job_corpus_reader", "job_push_check"]
    assert len(list_agents(store, include_retired=True)) == 4
    with pytest.raises(SpecError, match="already retired"):
        retire_agent(store, agent["agent_id"])
    with pytest.raises(SpecError, match="no live agent named 'job_feeder_member'; retired"):
        get_agent(store, "job_feeder_member")


def test_cli_verbs_round_trip(tmp_path, capsys):
    d = ["agents", "--store-dir", str(tmp_path / "store")]
    s = open_agent_store(tmp_path / "store")
    add_role(s, ROLES["notify-role"])
    raw = copy.deepcopy(SPECS["job_push_check"])
    raw["role"]["role_hash"] = role_hash(ROLES["notify-role"])
    f = tmp_path / "s.json"
    f.write_text(json.dumps(raw))
    assert main([*d, "register", "--from", str(f)]) == 0
    assert "registered" in capsys.readouterr().out
    assert main([*d, "register", "--from", str(f)]) == 0
    assert "already registered" in capsys.readouterr().out
    assert main([*d, "list"]) == 0 and "job_push_check" in capsys.readouterr().out
    assert main([*d, "show", "job_push_check"]) == 0
    shown = capsys.readouterr().out
    assert "review: required" in shown and "acts_outward=True" in shown and '"stop_at": 3600' in shown
    assert main([*d, "pause", "job_push_check"]) == 0 and "paused" in capsys.readouterr().out
    assert main([*d, "pause", "job_push_check"]) == 0 and "(already)" in capsys.readouterr().out
    assert main([*d, "resume", "job_push_check"]) == 0 and "active" in capsys.readouterr().out
    assert main([*d, "retire", "job_push_check"]) == 0 and "retired" in capsys.readouterr().out
    assert main([*d, "list"]) == 0 and capsys.readouterr().out == ""
    assert main([*d, "show", "nobody"]) == 1 and "no agent" in capsys.readouterr().err
    raw["stop_at"] = 5
    f.write_text(json.dumps(raw))
    assert main([*d, "register", "--from", str(f), "--warn"]) == 0
    assert "pass     job_push_check" in capsys.readouterr().out
    assert main([*d, "list"]) == 0 and capsys.readouterr().out == ""            # --warn stored nothing


# ---- L3 round 1 -----------------------------------------------------------------------------------------------
def test_a_retired_id_acts_on_itself_never_on_the_live_successor_or_its_group(store, capsys):
    old, *_ = reg(store, "job_push_check")
    retire_agent(store, old)
    reg(store, "job_sync_check")
    new, *_ = reg(store, "job_push_check", feeds=["job_sync_check"])          # same name, a new agent in a group
    assert [g["agent_id"] for g in feeder_group(store, old)] == [old]
    assert [(a["agent_id"], changed) for a, changed in set_operation(store, old, "paused")] == [(old, True)]
    assert axes(store, new) == ("registered", "active") and axes(store, "job_sync_check") == ("registered", "active")
    assert axes(store, old) == ("retired", "paused")
    assert main(["agents", "--store-dir", str(store.directory), "resume", old]) == 0
    assert capsys.readouterr().out.strip().splitlines() == [f"job_push_check {old}: active"]
    assert {g["agent_id"] for g in feeder_group(store, new)} == {new, get_agent(store, "job_sync_check")["agent_id"]}


@pytest.mark.parametrize("where", ["name", "feeds"])
def test_a_name_of_the_agent_id_shape_is_refused(where):
    shaped = "ag-" + "ab" * 16
    kw = {"name": shaped} if where == "name" else {"feeds": [shaped]}
    with pytest.raises(SpecError, match=r"shape of an agent_id \(ag-<32 hex>\)"):
        spec("job_sync_check", **kw)
    assert spec("job_sync_check", name="ag-" + "ab" * 15)["name"] == "ag-" + "ab" * 15     # not the id shape


def _tamper(store, name, text):
    """Rewrite a stored spec under the freeze trigger, then put the trigger back so the store still opens."""
    with store.write() as conn:
        ddl = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'agent_specs_frozen'").fetchone()[0]
        conn.execute("DROP TRIGGER agent_specs_frozen")
        conn.execute("UPDATE agent_specs SET spec = ? WHERE agent_id = (SELECT agent_id FROM agents WHERE name = ?)",
                     (text, name))
        conn.execute(ddl)


def test_a_corrupt_row_in_the_group_walk_is_a_refusal_not_a_traceback(store, capsys):
    reg(store, "job_sync_check")
    reg(store, "job_push_check")
    _tamper(store, "job_sync_check", "x")
    for fn in (lambda: set_operation(store, "job_push_check", "paused"), lambda: feeder_group(store, "job_push_check"),
               lambda: retire_agent(store, "job_push_check")):
        with pytest.raises(SpecError, match="not valid JSON"):
            fn()
    assert main(["agents", "--store-dir", str(store.directory), "pause", "job_push_check"]) == 1
    assert "not valid JSON" in capsys.readouterr().err and axes(store, "job_push_check") == ("registered", "active")


def test_a_stored_feeds_that_is_not_a_list_is_refused_never_iterated(store):
    reg(store, "job_sync_check")
    reg(store, "job_push_check")
    raw = copy.deepcopy(get_agent(store, "job_sync_check")["spec"])
    raw["feeds"] = "job_push_check"
    _tamper(store, "job_sync_check", json.dumps(raw))
    with pytest.raises(SpecError, match="feeds must be a list"):
        feeder_group(store, "job_push_check")


def test_a_corpus_moment_blank_or_absent_is_one_stored_text_and_one_hash():
    out = dict(SPECS["job_corpus_reader"]["output"])
    win = {"kind": "verifier", "check": "x"}
    absent = finalize_spec(spec("job_corpus_reader", win=win, output=out), ZONE)
    blank = finalize_spec(spec("job_corpus_reader", win=win, output={**out, "moment": ""}), ZONE)
    assert blank["output"]["moment"] == absent["output"]["moment"] == "when asked"
    assert spec_hash(AID, blank) == spec_hash(AID, absent)


def test_a_zone_is_stored_in_its_canonical_spelling(store):
    got = spec("job_push_check", trigger={"kind": "schedule", "cron": "0 7 * * *", "zone": "america/new_york"})
    assert got["trigger"]["zone"] == "America/New_York"
    with pytest.raises(SpecError, match="trigger.zone"):
        spec("job_push_check", trigger={"kind": "schedule", "cron": "0 7 * * *", "zone": "america/nowhere"})


def test_an_invalid_stored_default_zone_is_refused_naming_the_verb(store, tmp_path, capsys):
    with store.write() as conn:
        conn.execute("INSERT INTO agent_settings (key, value) VALUES ('default_zone', 'Mars/Olympus')")
    raw = copy.deepcopy(SPECS["job_sync_check"])
    del raw["trigger"]["zone"]
    raw["role"]["role_hash"] = role_hash(ROLES["local-role"])
    f = tmp_path / "s.json"
    f.write_text(json.dumps(raw))
    for extra in ([], ["--warn"]):
        assert main(["agents", "--store-dir", str(store.directory), "register", "--from", str(f), *extra]) == 1
        err = capsys.readouterr().err
        assert "'Mars/Olympus'" in err and "levain agents zone" in err
    assert stored_specs(store) == {}
    assert main(["agents", "--store-dir", str(store.directory), "zone"]) == 1
    assert "levain agents zone" in capsys.readouterr().err
    with pytest.raises(SpecError, match="levain agents zone"):
        finalize_spec(spec("job_sync_check", trigger={"kind": "schedule", "cron": "0 7 * * *"}), "Mars/Olympus")
