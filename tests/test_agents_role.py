"""P1: the RoleSpec registry (A0 §3.1). The misspelled-field run comes first."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from levain.agents import cli as agents_cli
from levain.agents.role import (RoleError, add_role, canonical_role, get_role, instance_ceiling, parse_role,
                                parse_role_text, role_by_hash, role_hash, spawn_namespace)
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


# K1 (key 1105-36): the hashes below are pinned LITERALS, read off origin/main before `kind` existed. A signed agent
# link pins them, so a slice that moves one re-signs every live agent on the operator's Touch ID.
DREAM_ROLE_HASH = "d8b10c8747ebe39945c0879dfd07221eaaa267c09aa6130dec37c1684f54be5b"
PARTNER_ROLE = {"name": "partner",
                "brief": {"template": "Work with the operator on {topic}; keep your partnership memory.",
                          "slots": {"topic": "str"}},
                "ceiling": {"lands": ["the install's anneal memory text"]},
                "model": "claude-opus-5-5", "harness": "claude", "executor": "session"}
PARTNER_ROLE_HASH = "5510fb834f0ee95a36e96d3c47d2e670f920dc920d46e41fbc61d67cf5306b57"
SCRIBE_ROLE_HASH = "a0e4c6e91646508d12d070fc16baeb9d33601fa0c9bcb1c46a46d906407504b1"


def test_the_shipped_hashes_do_not_move_and_kind_role_is_the_same_role():
    from levain.dream.agent import dream_role
    scribe = json.loads(
        '{"brief":{"slots":{"topic":"str"},"template":"Write about {topic}"},"ceiling":{"pushes":["origin"],'
        '"where":[{"paths":["a/"],"repo":"r"}]},"executor":"session","harness":"claude","model":"m","name":"scribe"}')
    for role, pinned in ((dream_role(), DREAM_ROLE_HASH), (PARTNER_ROLE, PARTNER_ROLE_HASH),
                         (scribe, SCRIBE_ROLE_HASH)):
        assert role_hash(role) == pinned
        # `kind: role` is the default's second spelling in the INPUT only: same hash, same canonical text, no key
        spelled = dict(role, kind="role")
        assert role_hash(spelled) == pinned
        assert canonical_role(spelled) == canonical_role(role)
        assert "kind" not in parse_role(spelled) and "kind" not in parse_role(role)
        assert '"kind"' not in canonical_role(spelled)
        # `kind: job` is a different role: a different hash, and the canonical text carries the field
        job = dict(role, kind="job")
        assert role_hash(job) != pinned
        assert parse_role(job)["kind"] == "job" and '"kind":"job"' in canonical_role(job)
        # and it round-trips: the canonical text parses back to the same hash
        assert role_hash(parse_role_text(canonical_role(job))) == role_hash(job)


def test_kind_is_optional_not_required_and_the_missing_field_message_does_not_name_it():
    # the trap: appending `kind` to the required tuple makes it mandatory and refuses every role there is
    assert "kind" not in ROLE
    assert "kind" not in parse_role(ROLE)
    with pytest.raises(RoleError, match="missing field 'model'") as exc:
        parse_role({k: v for k, v in ROLE.items() if k != "model"})
    assert "kind" not in str(exc.value).split("missing field")[1]


@pytest.mark.parametrize("value,needle", [
    (None, "kind"), ("agent", "agent"), ("Role", "kind"), ("", "kind"), ("jobs", "kind"), (1, "kind"),
    (True, "kind"), (["job"], "kind"), ({"x": 1}, "kind"),
])
def test_kind_other_than_job_or_role_is_refused_naming_the_field(value, needle):
    with pytest.raises(RoleError, match=r"field 'kind' .* is not one of job, role|field 'kind' = 'agent'") as exc:
        parse_role(dict(ROLE, kind=value))
    assert needle in str(exc.value) and "unknown field" not in str(exc.value)


AGENT_KIND_WORDS = ("`agent` is not a role kind: the one agent is materialised by the registry from the store's "
                    "operator record, and roles attach to it (`levain agents attach`)")


def test_kind_agent_is_refused_saying_the_agent_is_derived_not_accepted_as_an_inert_value():
    with pytest.raises(RoleError) as exc:
        parse_role(dict(ROLE, kind="agent"))
    msg = str(exc.value)
    assert "'kind'" in msg and AGENT_KIND_WORDS in msg
    # the ruling is answered (np-4e087a4f (b)): the refusal no longer says a ruling is pending
    assert "ruling" not in msg and "pending" not in msg
    with pytest.raises(RoleError, match="operator record"):
        parse_role_text(json.dumps(dict(ROLE, kind="agent")))
    with pytest.raises(RoleError, match="operator record"):
        role_hash(dict(ROLE, kind="agent"))


def test_a_pre_slice_stored_row_still_reads_and_a_second_spelling_of_the_default_is_refused(tmp_path):
    store = open_agent_store(tmp_path / "store")
    text = _canonical(ROLE)  # exactly the text a binary before `kind` wrote
    digest = hashlib.sha256(b"levain-rolespec/1\n" + text.encode()).hexdigest()
    with store.write() as conn:
        conn.execute("INSERT INTO agent_roles (role_hash, name, version, spec, created_at) VALUES (?,?,?,?,?)",
                     (digest, "scribe", 1, text, "2026-01-01T00:00:00Z"))
    got_digest, version, role = get_role(store, "scribe")
    assert (got_digest, version) == (digest, 1) and role == parse_role(ROLE) and "kind" not in role
    assert role_by_hash(store, digest) == role
    # a row whose text carries `"kind":"role"` is not the canonical text, so it is refused even under its own hash
    spelled = _canonical(dict(ROLE, kind="role", name="other"))
    spelled_digest = hashlib.sha256(b"levain-rolespec/1\n" + spelled.encode()).hexdigest()
    with store.write() as conn:
        conn.execute("INSERT INTO agent_roles (role_hash, name, version, spec, created_at) VALUES (?,?,?,?,?)",
                     (spelled_digest, "other", 1, spelled, "2026-01-01T00:00:00Z"))
    with pytest.raises(RoleError, match="does not hash"):
        get_role(store, "other")


def test_a_job_role_roundtrips_through_add_and_get(tmp_path):
    store = open_agent_store(tmp_path / "store")
    job = dict(ROLE, kind="job")
    name, digest, version, created = add_role(store, job)
    assert (name, version, created) == ("scribe", 1, True) and digest == role_hash(job) != role_hash(ROLE)
    assert get_role(store, "scribe") == (digest, 1, parse_role(job))
    assert role_by_hash(store, digest)["kind"] == "job"
    # the same words without the kind are the OTHER role: the next version, not a hit on the job's hash
    assert add_role(store, ROLE) == ("scribe", role_hash(ROLE), 2, True)


def test_role_show_prints_the_kind_and_role_when_absent(tmp_path, capsys):
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, ROLE))) == 0
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, dict(ROLE, name="chore", kind="job"), "job.json"))) == 0
    capsys.readouterr()
    assert _run(tmp_path, "role", "show", "scribe") == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"role scribe v1 {role_hash(ROLE)} kind=role"
    assert _run(tmp_path, "role", "show", "chore") == 0
    first = capsys.readouterr().out.splitlines()[0]
    assert first == f"role chore v1 {role_hash(dict(ROLE, name='chore', kind='job'))} kind=job"


def test_role_add_of_kind_agent_exits_1_saying_the_agent_is_derived(tmp_path, capsys):
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, dict(ROLE, kind="agent")))) == 1
    err = capsys.readouterr().err
    assert "'kind'" in err and AGENT_KIND_WORDS in err and "ruling" not in err
    assert _run(tmp_path, "role", "add", str(_file(tmp_path, dict(ROLE, kind="role")))) == 0


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


# --- ceiling.withhold (levain 1107, W2: the signed role declares what its workspace snapshot withholds) -----------------------

def test_ceiling_withhold_is_stored_sorted_and_moves_the_role_hash():
    base = role_hash(ROLE)
    r = copy.deepcopy(ROLE)
    r["ceiling"]["withhold"] = ["secrets/**", "docs/**"]
    assert parse_role(r)["ceiling"]["withhold"] == ["docs/**", "secrets/**"]
    assert role_hash(r) != base
    r2 = copy.deepcopy(ROLE)
    r2["ceiling"]["withhold"] = ["docs/**", "secrets/**"]
    assert role_hash(r2) == role_hash(r)                       # one spelling of a set is one hash
    from levain.agents.role import role_withhold
    assert role_withhold(r) == ("docs/**", "secrets/**") and role_withhold(ROLE) == ()


def test_a_role_without_withhold_hashes_exactly_as_before():
    # the field is absent from the canonical form, so every role registered before W2 keeps its hash (pinned value)
    assert role_hash(ROLE) == hashlib.sha256(b"levain-rolespec/1\n" + _canonical(parse_role(ROLE)).encode()).hexdigest()
    assert "withhold" not in canonical_role(ROLE)


@pytest.mark.parametrize("bad,why", [
    ("docs/**", "must be a list"), ([1], "must be a string"), (["docs/**", "docs/**"], "twice"),
    ([""], "ceiling.withhold[0]"), (["/abs"], "ceiling.withhold[0]"), ([f"p{i}" for i in range(257)], "more than 256"),
])
def test_a_malformed_withhold_is_refused_naming_the_field(bad, why):
    r = copy.deepcopy(ROLE)
    r["ceiling"]["withhold"] = bad
    with pytest.raises(RoleError, match="ceiling.withhold") as exc:
        parse_role(r)
    assert why in str(exc.value)


def test_withhold_never_reaches_the_envelope_narrowing():
    from levain.agents.role import envelope_ceiling
    from levain.coordinator.envelopes import narrow_scope
    r = copy.deepcopy(ROLE)
    r["ceiling"]["withhold"] = ["docs/**"]
    ceiling = instance_ceiling(r)
    scope = {"where": [{"repo": "r", "paths": ["a/"]}], "lands": [], "pushes": [], "publishes": []}
    with pytest.raises(Exception, match="withhold"):            # the raw ceiling is refused by narrow_scope ...
        narrow_scope(scope, ceiling)
    assert narrow_scope(scope, envelope_ceiling(ceiling))["where"] == [{"repo": "r", "paths": ["a/"]}]   # ... this view is not
    assert "withhold" not in envelope_ceiling(ceiling) and ceiling["withhold"] == ["docs/**"]
