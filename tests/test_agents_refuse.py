"""P2: the registration refusals (DESIGN 4.1 rules 1-6, refinements a-e, stop_at), each from a fixture row."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from levain.agents.refuse import AuditError, audit_verdicts, check_spec, derived_flags, refinement_of
from levain.agents.role import add_role, parse_role
from levain.agents.spec import SpecError, finalize_spec, parse_spec, register_spec, retire_agent, stored_specs
from levain.agents.store import get_default_zone, open_agent_store, set_default_zone
from levain.cli import main

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ROLES = json.loads((FIX / "roles.json").read_text())
ZONE = "America/New_York"


@pytest.fixture
def store(tmp_path):
    s = open_agent_store(tmp_path / "store")
    return s


def role_hash(store, key):
    from levain.agents.role import role_hash as rh
    add_role(store, ROLES[key])
    return rh(ROLES[key])


def row(store, job):
    """A fixture row with its role pinned to the registered role's real hash."""
    spec = copy.deepcopy(SPECS[job])
    spec["role"]["role_hash"] = role_hash(store, spec["role"]["name"])
    return spec


def verdict(store, spec_or_job, *, zone=ZONE, held=(), lookup=None, mutate=None):
    spec = row(store, spec_or_job) if isinstance(spec_or_job, str) else spec_or_job
    if mutate:
        mutate(spec)
    parsed = parse_spec(spec)
    role = parse_role(ROLES[spec["role"]["name"]])
    return check_spec(parsed, role=role, default_zone=zone, names_held=set(held), lookup=lookup)


def rules(refusals):
    return [r.rule for r in refusals]


def drop(*path):
    def f(spec):
        d = spec
        for p in path[:-1]:
            d = d[p]
        del d[path[-1]]
    return f


# ---- rule 1 -------------------------------------------------------------------------------------------------
def test_fixture_rows_that_pass_pass(store):
    for job in ("job_sync_check", "job_push_check", "job_feeder_downstream", "job_snapshot"):
        assert verdict(store, job) == [], job


@pytest.mark.parametrize("mutate,needle", [
    (drop("input"), "no input"),
    (lambda s: s["input"].update(liveness=""), "no input"),
    (drop("output", "reader"), "no output reader"),
    (drop("output", "moment"), "no output moment"),
    (drop("win"), "no win"),
    (lambda s: s["win"].update(check=" "), "win.check is empty"),
    (drop("output"), "no output"),
])
def test_rule_1_the_triple(store, mutate, needle):
    got = verdict(store, "job_push_check", mutate=mutate)
    assert rules(got) and set(rules(got)) == {"1"} and any(needle in r.message for r in got), got


# ---- refinement a ---------------------------------------------------------------------------------------------
def test_a_corpus_reader_is_the_query_path_read_when_asked(store):
    spec = row(store, "job_corpus_reader")
    spec["win"] = {"kind": "verifier", "check": "the coverage attestation is checked"}
    assert verdict(store, spec) == []                       # no push moment, still accepted
    assert finalize_spec(parse_spec(spec), ZONE)["output"]["moment"] == "when asked"


def test_a_still_leaves_the_win_rule_standing(store):
    # the audit's finding: (a) clears the reader and the moment, check 1 'no win' stays
    got = verdict(store, "job_corpus_reader")
    assert [r.rule for r in got] == ["1"] and got[0].message.startswith("no win: the spec has no `win` field; add")
    assert refinement_of(parse_spec(row(store, "job_corpus_reader"))) == "a"


def test_a_does_not_excuse_a_push_output_without_a_moment(store):
    got = verdict(store, "job_push_check", mutate=drop("output", "moment"))
    assert any("no output moment" in r.message for r in got)


# ---- refinement b ---------------------------------------------------------------------------------------------
def group(store, **override):
    out = {n: parse_spec(row(store, n)) for n in ("job_feeder_downstream", "job_corpus_reader",
                                                  "job_feeder_member")}
    for name, patch in override.items():
        out[name].update(patch)
    return out


def test_b_inherited_win_ends_at_a_downstream_with_no_win_and_is_refused_with_the_group(store):
    g = group(store)
    got = verdict(store, "job_feeder_member", lookup=g)
    assert [(r.rule, "job_corpus_reader" in r.message) for r in got] == [("1", True)]
    assert "inherited win ends at job_corpus_reader, which has no win" in got[0].message


def test_b_passes_once_the_group_has_a_real_win(store):
    g = group(store, job_corpus_reader={"win": {"kind": "verifier", "check": "attestation checked"}})
    assert verdict(store, "job_feeder_member", lookup=g) == []


def test_b_inherited_needs_a_feeder_group_and_a_registered_downstream(store):
    no_feeds = verdict(store, "job_feeder_member", mutate=lambda s: s.update(feeds=[]))
    assert any("feeds is empty" in r.message for r in no_feeds)
    alone = verdict(store, "job_feeder_member", lookup={})
    assert any("not registered" in r.message for r in alone)


def test_b_inheritance_chains_through_a_middle_stage(store):
    g = group(store, job_feeder_downstream={"win": {"kind": "inherited", "check": ""},
                                             "feeds": ["job_corpus_reader"]})
    assert any("job_corpus_reader, which has no win" in r.message
               for r in verdict(store, "job_feeder_member", lookup=g))


# ---- refinement c ---------------------------------------------------------------------------------------------
def test_c_recovery_reader_is_a_drill_and_the_win_a_verified_restore(store):
    assert verdict(store, "job_snapshot") == []
    assert refinement_of(parse_spec(row(store, "job_snapshot"))) == "c"
    no_cadence = verdict(store, "job_snapshot", mutate=drop("output", "drill_cadence"))
    assert any("drill_cadence" in r.message for r in no_cadence)
    plain = verdict(store, "job_snapshot", mutate=lambda s: s["win"].update(kind="verifier"))
    assert any("verified-restore" in r.message for r in plain)
    wrong = verdict(store, "job_push_check", mutate=lambda s: s["win"].update(kind="verified-restore"))
    assert any("belongs to a recovery output" in r.message for r in wrong)


def test_c_drill_cadence_only_on_a_recovery_output(store):
    spec = row(store, "job_push_check")
    spec["output"]["drill_cadence"] = 60
    with pytest.raises(SpecError, match="drill_cadence"):
        parse_spec(spec)


# ---- refinement d ---------------------------------------------------------------------------------------------
def test_d_review_is_a_derived_flag_never_stored(store):
    flags = {k: derived_flags(parse_role(ROLES[k])) for k in ROLES}
    # every fixture role but model-role is a dream executor with model "": the dream calls a model (D1 §1.3)
    assert flags["local-role"] == {"calls_model": True, "acts_outward": False, "review_required": True}
    assert flags["notify-role"] == {"calls_model": True, "acts_outward": True, "review_required": True}
    assert flags["model-role"] == {"calls_model": True, "acts_outward": False, "review_required": True}
    assert flags["all-role"]["acts_outward"] is True            # publishes counts
    # the flag itself: a role that neither names a model nor runs the dream and does not act outward needs no review
    quiet = {"ceiling": {"lands": ["local data"]}, "model": "", "executor": "session"}
    assert derived_flags(quiet) == {"calls_model": False, "acts_outward": False, "review_required": False}
    spec = row(store, "job_push_check")
    spec["review_by"] = "2026-12-01"
    with pytest.raises(SpecError, match="review_by"):
        parse_spec(spec)


# ---- refinement e and rule 6 ------------------------------------------------------------------------------------
def test_e_no_zone_and_no_default_is_refused_naming_the_verb(store):
    got = verdict(store, "job_push_check", zone=None, mutate=lambda s: s["trigger"].pop("zone"))
    assert rules(got) == ["6"] and "levain agents zone" in got[0].message


def test_e_a_default_zone_fills_a_zone_less_schedule(store):
    spec = row(store, "job_push_check")
    del spec["trigger"]["zone"]
    assert verdict(store, spec) == []
    assert finalize_spec(parse_spec(spec), "Europe/Paris")["trigger"]["zone"] == "Europe/Paris"
    with pytest.raises(SpecError, match="trigger.zone"):
        finalize_spec(parse_spec(spec), None)


def test_e_zone_verb_and_the_register_refusal(tmp_path, capsys):
    d = str(tmp_path / "store")
    spec = copy.deepcopy(SPECS["job_sync_check"])
    del spec["trigger"]["zone"]
    s = open_agent_store(tmp_path / "store")
    spec["role"]["role_hash"] = role_hash(s, "local-role")
    f = tmp_path / "spec.json"
    f.write_text(json.dumps(spec))
    assert main(["agents", "--store-dir", d, "zone"]) == 0
    assert "unset" in capsys.readouterr().out
    assert main(["agents", "--store-dir", d, "register", "--from", str(f)]) == 1
    assert "levain agents zone" in capsys.readouterr().out
    assert main(["agents", "--store-dir", d, "zone", "Not/AZone"]) == 1
    assert "IANA" in capsys.readouterr().err and get_default_zone(s) is None
    assert main(["agents", "--store-dir", d, "zone", ZONE]) == 0
    assert get_default_zone(s) == ZONE
    assert main(["agents", "--store-dir", d, "zone"]) == 0
    assert capsys.readouterr().out.strip().endswith(ZONE)
    assert main(["agents", "--store-dir", d, "register", "--from", str(f)]) == 0
    assert stored_specs(s)["job_sync_check"]["trigger"]["zone"] == ZONE


def test_rule_6_feeder_cycle(store):
    g = group(store, job_feeder_downstream={"feeds": ["job_feeder_member"]})
    got = verdict(store, "job_feeder_member", lookup=g)
    assert any(r.rule == "6" and "feeder cycle: job_feeder_member -> job_feeder_downstream -> job_feeder_member"
               in r.message for r in got), got
    assert any(r.rule == "6" for r in verdict(store, "job_push_check", mutate=lambda s: s.update(feeds=["job_push_check"])))


def test_set_default_zone_persists_in_agent_settings(store):
    set_default_zone(store, ZONE)
    with store.read() as conn:
        assert conn.execute("SELECT value FROM agent_settings WHERE key='default_zone'").fetchone()[0] == ZONE
    set_default_zone(store, "UTC")
    assert get_default_zone(store) == "UTC"


# ---- rules 3, 4, 5 ------------------------------------------------------------------------------------------------
def test_rule_3_unknown_field_and_unresolved_role(store):
    spec = row(store, "job_push_check")
    spec["colour"] = "red"
    with pytest.raises(SpecError, match="unknown field 'colour'"):
        parse_spec(spec)
    parsed = parse_spec(row(store, "job_push_check"))
    got = check_spec(parsed, role=None, default_zone=ZONE)
    assert rules(got) == ["3"]


def test_rule_4_a_held_name_is_refused_and_a_retired_name_is_free_with_a_new_id(store):
    spec = row(store, "job_push_check")
    assert rules(verdict(store, spec, held={"job_push_check"})) == ["4"]
    a1, *_ = register_spec(store, parse_spec(spec), ZONE)
    with pytest.raises(SpecError, match="rule 4"):
        register_spec(store, parse_spec({**spec, "stop_at": 99}), ZONE)
    retire_agent(store, a1)
    a2, _, v, created = register_spec(store, parse_spec(spec), ZONE)
    assert a2 != a1 and v == 1 and created


def test_d_a_dream_executor_spec_is_review_required_through_register(tmp_path, capsys):
    s = open_agent_store(tmp_path / "store")
    spec = copy.deepcopy(SPECS["job_sync_check"])          # local-role: executor dream, model "", lands only
    spec["role"]["role_hash"] = role_hash(s, "local-role")
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec))
    assert main(["agents", "--store-dir", str(tmp_path / "store"), "register", "--warn", "--json", "--from",
                 str(f)]) == 0
    assert json.loads(capsys.readouterr().out)["jobs"]["job_sync_check"]["review_required"] is True


def test_rule_3_role_name_must_match_the_role_the_hash_pins(store):
    spec = row(store, "job_sync_check")                    # pins local-role's hash
    spec["role"]["name"] = "notify-role"
    parsed = parse_spec(spec)
    got = check_spec(parsed, role=parse_role(ROLES["local-role"]), default_zone=ZONE)
    assert rules(got) == ["3"]
    assert "'notify-role' does not match" in got[0].message and "which is 'local-role'" in got[0].message


def put(tmp_path, objs):
    f = tmp_path / "batch.json"
    f.write_text(json.dumps(objs))
    return str(f)


def test_rule_4_two_specs_of_one_name_in_a_file_refuse_the_file(store, tmp_path, capsys):
    d = ["agents", "--store-dir", str(store.directory), "register"]
    a, b = row(store, "job_sync_check"), row(store, "job_sync_check")
    b["stop_at"] = 5
    for extra in (["--warn"], [], ["--revise"]):
        code = main([*d, "--json", "--from", put(tmp_path, [a, b]), *extra])
        got = json.loads(capsys.readouterr().out)["jobs"]
        assert code == (0 if extra == ["--warn"] else 1)
        assert {v["verdict"] for v in got.values()} == {"refused"}
        assert all("appears 2 times in this file" in v["reasons"][-1] for v in got.values())
        assert stored_specs(store) == {}


def test_a_feeder_group_registers_whole_or_not_at_all(store, tmp_path, capsys):
    d = ["agents", "--store-dir", str(store.directory), "register", "--json", "--from"]
    sensor, classifier, reconciler = (row(store, n) for n in ("job_feeder_member", "job_feeder_downstream",
                                                              "job_corpus_reader"))
    assert main([*d, put(tmp_path, [sensor, classifier, reconciler])]) == 1      # the reconciler has no win
    got = json.loads(capsys.readouterr().out)["jobs"]
    assert got["job_feeder_downstream"]["reasons"][0].startswith("held back: its own checks pass")
    assert got["job_corpus_reader"]["reasons"][0].startswith("rule 1: no win: the spec has no `win` field")
    assert stored_specs(store) == {}                                              # nothing written, no dangling feeds
    reconciler["win"] = {"kind": "verifier", "check": "attestation checked"}
    assert main([*d, put(tmp_path, [sensor, classifier, reconciler])]) == 0
    got = json.loads(capsys.readouterr().out)["jobs"]
    assert {v["version"] for v in got.values()} == {1} and set(stored_specs(store)) == set(got)


def test_a_cycle_in_a_file_writes_nothing(store, tmp_path, capsys):
    base = row(store, "job_sync_check")
    specs = [{**copy.deepcopy(base), "name": n, "feeds": f} for n, f in (("a", ["b"]), ("b", ["c"]), ("c", ["b"]))]
    assert main(["agents", "--store-dir", str(store.directory), "register", "--json", "--from",
                 put(tmp_path, specs)]) == 1
    got = json.loads(capsys.readouterr().out)["jobs"]
    assert any("feeder cycle: b -> c -> b" in r for r in got["b"]["reasons"])
    assert any("feeder cycle: c -> b -> c" in r for r in got["c"]["reasons"])
    assert got["a"]["reasons"][0].startswith("held back")                         # a is not in the cycle
    assert stored_specs(store) == {}


def test_warn_writes_nothing_and_creates_no_store(tmp_path, capsys):
    spec = copy.deepcopy(SPECS["job_sync_check"])
    spec["role"]["role_hash"] = "0" * 64
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec))
    fresh = tmp_path / "fresh"
    assert main(["agents", "--store-dir", str(fresh), "register", "--warn", "--from", str(f)]) == 0
    assert "is not registered" in capsys.readouterr().out and not fresh.exists()
    s = open_agent_store(tmp_path / "store")
    spec["role"]["role_hash"] = role_hash(s, "local-role")
    f.write_text(json.dumps(spec))
    db = tmp_path / "store" / "autonomic.db"
    before = db.read_bytes()
    assert main(["agents", "--store-dir", str(tmp_path / "store"), "register", "--warn", "--from", str(f)]) == 0
    assert capsys.readouterr().out.startswith("pass")
    assert db.read_bytes() == before and stored_specs(s) == {}


def test_b_a_chain_must_end_in_a_real_win(store):
    up = row(store, "job_feeder_member")
    up["feeds"] = ["down"]
    down = row(store, "job_feeder_member")
    down.update(name="down", feeds=[])
    lookup = {"down": parse_spec(down)}
    got_up = verdict(store, up, lookup=lookup)
    assert [r.rule for r in got_up] == ["1"] and "whose own win is inherited and which feeds nothing" in got_up[0].message
    got_down = verdict(store, down, lookup={"up": parse_spec(up)})
    assert any("feeds is empty" in r.message for r in got_down)


@pytest.mark.parametrize("entry", ["everything", "ANY", "anything", ".", "./", "~", "$HOME", "/*", "/", "**"])
@pytest.mark.parametrize("where", ["lands", "pushes", "publishes", "repo", "paths"])
def test_rule_5_spellings_of_all(entry, where):
    from levain.agents.refuse import ceiling_problem
    if where == "repo":
        ceiling = {"where": [{"repo": entry, "paths": ["src"]}]}
    elif where == "paths":
        ceiling = {"where": [{"repo": "levain", "paths": [entry]}]}
    else:
        ceiling = {"lands": ["local data"], where: [entry]}
    problem = ceiling_problem(ceiling)
    assert problem and ("spelling of 'all'" in problem or "says 'all'" in problem), (entry, where, problem)


def test_rule_5_blank_entries():
    from levain.agents.refuse import ceiling_problem
    assert ceiling_problem({"lands": [""]}) == "the role's ceiling is empty"         # blank is not something named
    assert ceiling_problem({"pushes": ["  "], "where": [{"repo": "", "paths": []}]}) == "the role's ceiling is empty"
    assert ceiling_problem({"lands": ["local data", ""]}) == "the role's ceiling.lands[1] has an empty entry"
    assert "paths[0] has an empty entry" in ceiling_problem({"where": [{"repo": "levain", "paths": [""]}]})
    assert ceiling_problem({"lands": ["local data"], "where": [{"repo": "levain", "paths": ["src"]}]}) is None


def test_messages_name_the_fix(store, tmp_path, capsys):
    held = verdict(store, "job_push_check", held={"job_push_check"})
    assert "retire it (`levain agents retire job_push_check`), or pass --revise to write its next version" in held[0].message
    unreg = check_spec(parse_spec(row(store, "job_push_check")), role=None, default_zone=ZONE)
    assert "`levain agents role add <role file>`" in unreg[0].message
    no_win = verdict(store, "job_push_check", mutate=drop("win"))
    assert "`win` field" in no_win[0].message and '"kind": "verifier" or "human"' in no_win[0].message
    good = row(store, "job_push_check")
    named_bad = {**good, "colour": "red"}
    nameless = {"stop_at": 1}
    weird = {"name": 7}
    assert main(["agents", "--store-dir", str(store.directory), "register", "--warn", "--json", "--from",
                 put(tmp_path, [good, named_bad, nameless, weird])]) == 0
    got = json.loads(capsys.readouterr().out)["jobs"]
    assert set(got) == {"job_push_check", "job_push_check#1", "spec[2]", "spec[3]"}
    assert "unknown field 'colour'" in got["job_push_check#1"]["reasons"][0]
    assert got["spec[2]"]["reasons"] == ["rule 3: missing field 'name'"]


@pytest.mark.parametrize("role,needle", [("empty-role", "empty"), ("all-role", "says 'all'")])
def test_rule_5_empty_or_all_ceiling(store, role, needle):
    got = verdict(store, "job_sync_check", mutate=lambda s: s["role"].update(
        name=role, role_hash=role_hash(store, role)))
    assert rules(got) == ["5"] and needle in got[0].message


# ---- stop_at --------------------------------------------------------------------------------------------------
def test_a_spec_with_no_stop_at_is_refused_naming_it(store, tmp_path, capsys):
    got = verdict(store, "job_push_check", mutate=drop("stop_at"))
    assert rules(got) == ["stop_at"] and "stop_at" in got[0].message
    spec = row(store, "job_push_check")
    del spec["stop_at"]
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec))
    assert main(["agents", "--store-dir", str(store.directory), "register", "--from", str(f)]) == 1
    assert "stop_at" in capsys.readouterr().out
    assert stored_specs(store) == {}


@pytest.mark.parametrize("bad", [0, -5, 1.5, "3600", True])
def test_stop_at_is_a_positive_integer(store, bad):
    spec = row(store, "job_push_check")
    spec["stop_at"] = bad
    with pytest.raises(SpecError, match="stop_at"):
        parse_spec(spec)


# ---- the audit run ---------------------------------------------------------------------------------------------
def test_register_warn_over_the_audit_subset_matches_the_expected_verdicts(tmp_path, capsys):
    d = str(tmp_path / "store")
    assert main(["agents", "--store-dir", d, "register", "--warn", "--json", "--from",
                 str(FIX / "audit_subset.json")]) == 0
    got = json.loads(capsys.readouterr().out)["jobs"]
    want = json.loads((FIX / "expected_subset.json").read_text())["jobs"]
    assert {k: {f: v[f] for f in want[k]} for k, v in got.items()} == want
    assert got["job_corpus_reader"]["reason"] == "1: no win"
    assert main(["agents", "--store-dir", d, "register", "--from", str(FIX / "audit_subset.json")]) == 1


def test_the_audit_checks_fail_on_the_broken_shapes():
    doc = json.loads((FIX / "audit_subset.json").read_text())
    for patch, needle in (
        (lambda r: r["review"].update(required=not r["review"]["required"]), "derived flag"),
        (lambda r: r.update(refinement="c"), "does not fit"),
        (lambda r: r.update(verdict="refused", reasons=[]), "verdict"),
        (lambda r: r.update(**{"class": "banana"}), "class"),
    ):
        bad = copy.deepcopy(doc)
        patch(bad["jobs"][0])
        with pytest.raises(AuditError, match=needle):
            audit_verdicts(bad)
    audit_verdicts(doc)
