"""P1: the RoleSpec registry (A0 §3.1). The misspelled-field run comes first."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from levain.agents import cli as agents_cli
from levain.agents.role import (RoleError, add_role, get_role, instance_ceiling, parse_role, parse_role_text,
                                role_hash)
from levain.agents.store import open_agent_store
from levain.authority import _canonical
from levain.cli import main

ROLE = {"name": "scribe", "brief": {"template": "Write about {topic}", "slots": {"topic": "str"}},
        "ceiling": {"pushes": ["origin"], "where": [{"repo": "r", "paths": ["a/"]}]},
        "model": "m", "harness": "claude", "executor": "session"}


def _file(tmp_path, role, name="role.json"):
    p = tmp_path / name
    p.write_text(json.dumps(role))
    return p


def _run(tmp_path, *argv):
    return main(["agents", "--store-dir", str(tmp_path / "store"), *argv])


def test_misspelled_field_exits_nonzero_and_names_it(tmp_path, capsys):
    bad = {k: v for k, v in ROLE.items() if k != "harness"} | {"harnes": "claude"}
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, bad))) == 1
    assert "'harnes'" in capsys.readouterr().err
    # the check can fail: the corrected file is accepted and registered
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, ROLE))) == 0


def test_add_show_roundtrip_and_versions(tmp_path, capsys):
    f = _file(tmp_path, ROLE)
    assert _run(tmp_path, "role", "add", str(f)) == 0
    assert _run(tmp_path, "role", "add", str(f)) == 0
    assert "already registered" in capsys.readouterr().out
    v2 = dict(ROLE, model="other")
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, v2))) == 0
    assert "v2 added" in capsys.readouterr().out
    assert _run(tmp_path, "role", "show", "scribe", "--version", "1") == 0
    out = capsys.readouterr().out
    assert out.startswith(f"role scribe v1 {role_hash(ROLE)}")
    assert _run(tmp_path, "role", "show", "nobody") == 1


def test_hash_matches_a0_formula_and_is_order_stable():
    expect = hashlib.sha256(b"levain-rolespec/1\n" + _canonical(ROLE).encode()).hexdigest()
    assert role_hash(ROLE) == expect
    shuffled = json.loads(json.dumps(dict(reversed(list(ROLE.items())))))
    shuffled["brief"] = dict(reversed(list(shuffled["brief"].items())))
    assert list(shuffled) != list(ROLE)
    assert role_hash(shuffled) == role_hash(ROLE)
    assert role_hash(parse_role_text(json.dumps(shuffled))) == role_hash(ROLE)


@pytest.mark.parametrize("path,value", [
    (("name",), "scribe2"), (("model",), "x"), (("harness",), "codex"),
    (("brief", "template"), "Write about {topic}!"), (("brief", "slots", "topic"), "int"),
    (("ceiling", "pushes"), []), (("ceiling", "where", 0, "paths"), ["b/"]),
])
def test_any_field_change_moves_the_hash(path, value):
    changed = copy.deepcopy(ROLE)
    node = changed
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    assert role_hash(changed) != role_hash(ROLE)


def test_dream_executor_has_empty_harness():
    dream = dict(ROLE, executor="dream", harness="")
    assert parse_role(dream)["harness"] == ""
    with pytest.raises(RoleError, match="harness must be empty"):
        parse_role(dict(dream, harness="claude"))
    with pytest.raises(RoleError, match="harness"):
        parse_role(dict(ROLE, harness=""))


@pytest.mark.parametrize("mutate,needle", [
    (lambda r: r.update(extra=1), "'extra'"),
    (lambda r: r["brief"].update(x=1), "'x'"),
    (lambda r: r["ceiling"].update(spends=[]), "'spends'"),
    (lambda r: r.pop("model"), "'model'"),
    (lambda r: r.update(name="Bad Name"), "name"),
    (lambda r: r["brief"].update(template="{nope}"), "undeclared slot 'nope'"),
    (lambda r: r["brief"]["slots"].update(unused="str"), "'unused'"),
    (lambda r: r["brief"]["slots"].update(topic="float"), "'float'"),
])
def test_refusals_name_the_field(mutate, needle):
    r = copy.deepcopy(ROLE)
    mutate(r)
    with pytest.raises(RoleError, match=needle):
        parse_role(r)


def test_floats_and_duplicate_keys_refused_at_parse():
    with pytest.raises(RoleError, match="float"):
        parse_role_text(json.dumps(ROLE).replace('"m"', '1.5'))
    with pytest.raises(RoleError, match="appears twice"):
        parse_role_text('{"name":"a","name":"b"}')


def test_instance_ceiling_is_a_copy():
    role = parse_role(ROLE)
    c = instance_ceiling(role)
    assert c == role["ceiling"]
    c["pushes"].append("evil")
    c["where"][0]["paths"].append("/")
    assert role["ceiling"]["pushes"] == ["origin"] and role["ceiling"]["where"][0]["paths"] == ["a/"]


def test_stored_text_is_rehashed(tmp_path):
    store = open_agent_store(tmp_path / "store")
    _, digest, _, _ = add_role(store, ROLE)
    assert get_role(store, "scribe")[0] == digest
    with store.write() as conn:
        conn.execute("UPDATE agent_roles SET spec = ?", (_canonical(dict(ROLE, model="swapped")),))
    with pytest.raises(RoleError, match="does not hash"):
        get_role(store, "scribe")
