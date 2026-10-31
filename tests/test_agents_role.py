"""P1: the RoleSpec registry (A0 §3.1). The misspelled-field run comes first."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from levain.agents import cli as agents_cli
from levain.agents.role import (RoleError, add_role, get_role, instance_ceiling, parse_role, parse_role_text,
                                role_hash, spawn_namespace)
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

    # a literal, computed independently from A0 §3.1 (sha256 of the prefix + the canonical JSON), not by the code
    literal = ('{"brief":{"slots":{"topic":"str"},"template":"Write about {topic}"},"ceiling":{"pushes":["origin"],'
               '"where":[{"paths":["a/"],"repo":"r"}]},"executor":"session","harness":"claude","model":"m",'
               '"name":"scribe"}')
    assert role_hash(json.loads(literal)) == "a0e4c6e91646508d12d070fc16baeb9d33601fa0c9bcb1c46a46d906407504b1"
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


def test_a_placeholder_is_exactly_the_slot_no_spec_no_conversion():
    def with_template(t, **slots):
        r = copy.deepcopy(ROLE)
        r["brief"]["template"] = t
        r["brief"]["slots"].update(slots)
        return r

    for bad, needle in (("Write {topic!z}", r"'topic'.*conversion !z"), ("Write {topic:d}", r"'topic'.*format spec :d"),
                        ("Write {topic!r}", r"'topic'.*conversion !r"), ("Write {topic:>10}", "format spec :>10"),
                        ("Write {topic:{width}}", r"'topic'.*a placeholder is exactly \{slot\}")):
        with pytest.raises(RoleError, match=needle):
            parse_role(with_template(bad))
        with pytest.raises(RoleError, match="a placeholder is exactly"):  # declaring the inner name changes nothing
            parse_role(with_template(bad, width="int"))
    # the checks can pass: a plain slot and escaped braces stay legal, and the template renders
    for ok in ("Write {topic}", "Write {topic} {{literal braces}}"):
        parse_role(with_template(ok))
    assert parse_role(with_template("Write {topic} {{x}}"))["brief"]["template"].format(topic="t") == "Write t {x}"


def test_lone_surrogate_and_huge_integer_are_refusals_not_tracebacks(tmp_path, capsys):
    text = json.dumps(ROLE).replace('"m"', '"\\ud800"')
    with pytest.raises(RoleError, match="lone surrogate"):
        parse_role_text(text)
    with pytest.raises(RoleError, match="lone surrogate"):
        parse_role(dict(ROLE, model="\ud800"))
    with pytest.raises(RoleError, match="not readable"):
        parse_role_text('{"name":' + "9" * 5000 + "}")
    store = open_agent_store(tmp_path / "store")
    with pytest.raises(RoleError):
        add_role(store, dict(ROLE, model="\ud800"))
    for body in (text, '{"name":' + "9" * 5000 + "}"):
        f = tmp_path / "bad.json"
        f.write_text(body)
        assert _run(tmp_path, "role", "add", str(f)) == 1
        assert "levain agents role add" in capsys.readouterr().err
    with pytest.raises(RoleError, match="no role named"):  # nothing was written
        get_role(store, "scribe")


def test_re_adding_older_content_says_it_is_not_the_latest(tmp_path, capsys):
    v1, v2 = _file(tmp_path, ROLE, "v1.json"), _file(tmp_path, dict(ROLE, model="other"), "v2.json")
    assert _run(tmp_path, "role", "add", str(v1)) == 0
    assert _run(tmp_path, "role", "add", str(v2)) == 0
    capsys.readouterr()
    assert _run(tmp_path, "role", "add", str(v1)) == 0
    out = capsys.readouterr().out
    assert "v1 is registered but the latest is v2" in out and "role show serves v2" in out
    assert _run(tmp_path, "role", "add", str(v2)) == 0  # the latest: the plain line
    out = capsys.readouterr().out
    assert "already registered" in out and "latest is" not in out


def test_role_name_column_must_match_the_hashed_text(tmp_path):
    store = open_agent_store(tmp_path / "store")
    add_role(store, ROLE)
    with store.write() as conn:
        conn.execute("UPDATE agent_roles SET name = 'impostor'")
    with pytest.raises(RoleError, match="names 'scribe', not 'impostor'"):
        get_role(store, "impostor")


def test_cli_store_errors_are_one_line_refusals(tmp_path, capsys, monkeypatch):
    f = _file(tmp_path, ROLE)
    assert _run(tmp_path, "role", "add", str(f)) == 0
    capsys.readouterr()
    from levain.autonomic.db import StoreFormatError
    import sqlite3

    for exc in (StoreFormatError("bad store"), sqlite3.OperationalError("disk I/O error")):
        def boom(*a, **k):
            raise exc
        monkeypatch.setattr(agents_cli, "get_role", boom)
        monkeypatch.setattr(agents_cli, "add_role", boom)
        assert _run(tmp_path, "role", "show", "scribe") == 1
        assert _run(tmp_path, "role", "add", str(f)) == 1
        err = capsys.readouterr().err
        assert err.count("the store refused it") == 2 and "Traceback" not in err


def _show(tmp_path, *argv):
    return _run(tmp_path, "role", "show", *argv)


def test_show_version_out_of_range_and_surrogate_name_are_refusals(tmp_path, capsys):
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, ROLE))) == 0
    capsys.readouterr()
    for v in ("99999999999999999999", "0", "-3"):
        with pytest.raises(SystemExit) as ei:  # argparse's own usage refusal
            _show(tmp_path, "scribe", "--version", v)
        assert ei.value.code == 2
        assert "a version is an integer from 1 to" in capsys.readouterr().err
    store = open_agent_store(tmp_path / "store")
    for bad in (0, -1, 2**63, 10**30, True, "1"):
        with pytest.raises(RoleError, match="version must be an integer"):
            get_role(store, "scribe", bad)
    assert get_role(store, "scribe", 1)[1] == 1
    assert _show(tmp_path, "\udcff") == 1
    err = capsys.readouterr().err
    assert "lone surrogate" in err and "Traceback" not in err
    with pytest.raises(RoleError, match="lone surrogate"):
        get_role(store, "\udcff")
    assert _show(tmp_path, "scribe", "--version", str(2**63 - 1)) == 1  # in range, simply not registered
    assert "no role named" in capsys.readouterr().err


def test_show_on_a_stdout_that_cannot_encode_is_a_one_line_refusal(tmp_path, capsys, monkeypatch):
    import io
    import sys
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, dict(ROLE, model="caf\u00e9")))) == 0
    capsys.readouterr()
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="ascii"))
    assert _show(tmp_path, "scribe") == 1
    sys.stdout.flush()
    assert raw.getvalue() == b""  # nothing half-printed
    err = capsys.readouterr().err
    assert "cannot encode" in err and "Traceback" not in err
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="utf-8"))  # the check can pass
    assert _show(tmp_path, "scribe") == 0
    sys.stdout.flush()
    assert "caf\u00e9".encode() in raw.getvalue()


def test_add_on_a_corrupt_row_is_a_refusal_not_already_registered(tmp_path, capsys):
    store = open_agent_store(tmp_path / "store")
    _, digest, _, _ = add_role(store, ROLE)
    assert add_role(store, ROLE)[3] is False  # an intact row is still "already registered"
    with store.write() as conn:  # same role_hash, changed spec
        conn.execute("UPDATE agent_roles SET spec = ? WHERE role_hash = ?",
                     (_canonical(dict(ROLE, model="swapped")), digest))
    with pytest.raises(RoleError, match="does not hash"):
        add_role(store, ROLE)
    capsys.readouterr()
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, ROLE))) == 1
    err = capsys.readouterr().err
    assert "does not hash" in err and "Traceback" not in err
    with store.write() as conn:  # a mismatched name column on the hit
        conn.execute("UPDATE agent_roles SET spec = ?, name = 'impostor' WHERE role_hash = ?",
                     (_canonical(ROLE), digest))
    with pytest.raises(RoleError, match="names 'scribe', not 'impostor'"):
        add_role(store, ROLE)


def test_a_blob_spec_is_refused_on_get_and_on_the_add_hash_hit(tmp_path, capsys):
    store = open_agent_store(tmp_path / "store")
    add_role(store, ROLE)
    with store.write() as conn:
        conn.execute("UPDATE agent_roles SET spec = X'FF'")
    with pytest.raises(RoleError, match="bytes, not text"):
        get_role(store, "scribe")
    with pytest.raises(RoleError, match="bytes, not text"):
        add_role(store, ROLE)
    capsys.readouterr()
    assert _show(tmp_path, "scribe") == 1
    err = capsys.readouterr().err
    assert "not text" in err and "Traceback" not in err


def test_public_functions_refuse_a_non_string_name_with_role_error():
    r = copy.deepcopy(ROLE)
    r["brief"]["slots"] = {1: "str"}
    with pytest.raises(RoleError, match="slot name 1"):
        parse_role(r)
    for bad in (None, 7, b"scribe"):
        with pytest.raises(RoleError, match="must be a string"):
            spawn_namespace(bad)
    assert spawn_namespace("scribe") == "@spawn/scribe"
    with pytest.raises(RoleError, match="missing field"):
        instance_ceiling({})
    with pytest.raises(RoleError, match="missing field"):
        instance_ceiling({"ceiling": "x"})


def test_store_write_holds_the_write_lock_so_add_role_is_serialised(tmp_path):
    import sqlite3
    store = open_agent_store(tmp_path / "store")
    with store.write() as conn:
        assert conn.in_transaction
        other = sqlite3.connect(store.path, timeout=0, isolation_level=None)
        try:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                other.execute("BEGIN IMMEDIATE")
        finally:
            other.close()
