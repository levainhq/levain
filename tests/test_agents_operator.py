"""K2b (key 1105-36): the operator agent record's registry side, `levain/agents/operator.py`.

Word trap, kept in every name here: a SPEC is the registered `ag-` thing (an AgentSpec); "the operator agent" is the one
record `agent_operator` (`oa-...`) the registry materialises. A spec is ATTACHED to it as a role; attaching confers
nothing in 1.0 (the last block pins that by behaviour and by import).

Real pieces throughout: a real agent store (schema `/4`, K2a), the real parsers and `check_spec`, the real dream
registration (`register_dream`), the real controller tick. Corruption is made the way only a direct writer could: around
the store's own triggers, which are restored.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from levain.agents import controller as ctl, operator as op, refuse, role as role_mod, spec as spec_mod
from levain.agents.store import open_agent_store, set_default_zone
from levain.dream import agent as da
from tests.test_agents_claim import _clock

FIX = Path(__file__).parent / "fixtures" / "agents_p2"
SPECS = json.loads((FIX / "specs.json").read_text())
ZONE = "America/New_York"
OA = re.compile(r"oa-[0-9a-f]{32}")
LEVAIN = Path(__file__).resolve().parents[1] / "levain"


@pytest.fixture
def store(tmp_path):
    s = open_agent_store(tmp_path / "store")
    set_default_zone(s, ZONE)
    return s


def role_doc(name="partner-role", *, kind=None, lands=("the partnership memory",), model="") -> dict:
    doc = {"name": name, "brief": {"template": "run", "slots": {}}, "ceiling": {"lands": list(lands)}, "model": model,
           "harness": "", "executor": "dream"}
    if kind is not None:
        doc["kind"] = kind
    return doc


def add(store, doc) -> str:
    return role_mod.add_role(store, doc)[1]


def register(store, name, doc, **kw):
    """Register a spec of ``name`` pinning role ``doc`` (added first); returns ``(agent_id, role_hash)``."""
    digest = add(store, doc)
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"] = name
    d["role"] = {"name": doc["name"], "role_hash": digest}
    d.update(kw)
    return spec_mod.register_spec(store, spec_mod.parse_spec(d), ZONE)[0], digest


def view(store, install_dir=None):
    with store.read() as conn:
        return op.materialise(conn, install_dir=install_dir)


def raw(store):
    c = sqlite3.connect(store.path, isolation_level=None)
    c.row_factory = None
    return c


def tamper(store, triggers, sql, params=()):
    """Run ``sql`` around the named triggers on a direct connection, then put the triggers back."""
    c = raw(store)
    try:
        saved = [c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name = ?", (t,)).fetchone()[0]
                 for t in triggers]
        for t in triggers:
            c.execute(f"DROP TRIGGER {t}")
        c.execute(sql, params)
        for s in saved:
            c.execute(s)
    finally:
        c.close()


def count(store, table) -> int:
    with store.read() as conn:
        return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


# ------------------------------------------------------------------------- the 1a reproduction and the dream heal

def test_the_1a_reproduction_register_the_dream_then_materialise_gives_one_agent_with_the_dream_attached(store):
    assert view(store) is None                       # no record yet: there is no operator agent to show
    reg = da.register_dream(store)
    agent = view(store)
    assert agent is not None and OA.fullmatch(agent.record_id)
    assert [(r.name, r.agent_id, r.role_name) for r in agent.roles] == [("dream", reg.agent_id, "dream")]
    assert agent.retired_roles == () and agent.dream_unattached is None
    assert count(store, "agent_operator") == 1 and count(store, "agent_attachments") == 1


def test_a_pre_k2b_dream_registered_unattached_attaches_when_re_enabled_with_unchanged_content(store):
    # what register_dream did before K2b: add the role, register the spec, attach nothing
    role = role_mod.parse_role(da.dream_role())
    role_mod.add_role(store, role)
    parsed = spec_mod.parse_spec(da.dream_spec(role_mod.role_hash(role)))
    agent_id, _, _, created = spec_mod.register_spec(store, parsed, ZONE)
    assert created and view(store) is None
    with store.read() as conn:
        assert op.unattached_dream(conn) == agent_id          # the fact, shown as a fact
    again = da.register_dream(store)                          # the identical-content exit: nothing is written but this
    assert again.agent_id == agent_id and again.created is False
    agent = view(store)
    assert [r.agent_id for r in agent.roles] == [agent_id] and agent.dream_unattached is None
    with store.read() as conn:
        assert op.unattached_dream(conn) is None
    assert da.register_dream(store).created is False and count(store, "agent_attachments") == 1   # and again: no second row


def test_an_unattached_dream_is_reported_beside_a_record_made_by_another_role(store):
    role = role_mod.parse_role(da.dream_role())
    role_mod.add_role(store, role)
    spec_mod.register_spec(store, spec_mod.parse_spec(da.dream_spec(role_mod.role_hash(role))), ZONE)
    other, _ = register(store, "partner-a", role_doc())
    with store.write() as conn:
        op.attach_in(conn, other)
    agent = view(store)
    assert [r.name for r in agent.roles] == ["partner-a"] and agent.dream_unattached is not None


def test_a_revise_of_the_dream_to_a_newer_version_keeps_it_attached(store, monkeypatch):
    da.register_dream(store)
    real = da.dream_spec
    monkeypatch.setattr(da, "dream_spec", lambda h, binding=None: {**real(h, binding), "stop_at": 3000})
    revised = da.register_dream(store, revise=True)
    assert revised.version == 2
    agent = view(store)
    assert [(r.agent_id, r.version) for r in agent.roles] == [(revised.agent_id, 2)]
    assert count(store, "agent_attachments") == 1


# ------------------------------------------------------------------------------------- D-LANDS (a): the 1a pair

def test_the_1a_pair_registers_as_two_roles_and_the_agent_count_stays_one(store):
    a, _ = register(store, "partner-a", role_doc("partner-role"))
    b, _ = register(store, "partner-b", role_doc("partner-role-b"))
    assert {x["name"] for x in spec_mod.list_agents(store)} == {"partner-a", "partner-b"}   # both register, as in 1a
    assert view(store) is None and count(store, "agent_operator") == 0                      # neither is an agent
    with store.write() as conn:
        op.attach_in(conn, a)
        op.attach_in(conn, b)
    agent = view(store)
    assert sorted(r.name for r in agent.roles) == ["partner-a", "partner-b"]
    assert count(store, "agent_operator") == 1                                              # ONE agent, two roles


def test_the_second_attach_reuses_the_record(store):
    a, _ = register(store, "partner-a", role_doc("partner-role"))
    b, _ = register(store, "partner-b", role_doc("partner-role-b"))
    with store.write() as conn:
        first = op.attach_in(conn, a)
    with store.write() as conn:
        second = op.attach_in(conn, b)
    assert first.record_created and not second.record_created and first.record_id == second.record_id


# ------------------------------------------------------------------------------------------------- what is refused

def test_attaching_a_job_is_refused_in_the_rulings_words(store):
    a, _ = register(store, "worker", role_doc("job-role", kind="job"))
    with store.write() as conn, pytest.raises(spec_mod.SpecError, match="a job is not attached to the agent"):
        op.attach_in(conn, a)
    assert count(store, "agent_operator") == 0 and count(store, "agent_attachments") == 0     # not even the record


def test_attaching_a_retired_spec_is_refused_naming_the_id(store):
    a, _ = register(store, "partner-a", role_doc())
    spec_mod.retire_agent(store, "partner-a")
    with store.write() as conn, pytest.raises(spec_mod.SpecError, match=a):
        op.attach_in(conn, a)
    assert count(store, "agent_attachments") == 0


def test_attaching_what_is_not_registered_is_refused(store):
    with store.write() as conn, pytest.raises(spec_mod.SpecError, match="ag-"):
        op.attach_in(conn, "ag-" + "0" * 32)


def test_attaching_a_spec_whose_role_row_is_unreadable_is_refused_and_writes_nothing(store):
    a, digest = register(store, "partner-a", role_doc())
    tamper(store, [], "UPDATE agent_roles SET spec = 'not a role' WHERE role_hash = ?", (digest,))
    with store.write() as conn, pytest.raises(op.OperatorAgentUnreadable, match="partner-role"):
        op.attach_in(conn, a)
    assert count(store, "agent_operator") == 0


def test_a_failed_attach_leaves_no_record_behind_in_its_own_transaction(store):
    a, _ = register(store, "worker", role_doc("job-role", kind="job"))
    with pytest.raises(spec_mod.SpecError):
        with store.write() as conn:
            op.attach_in(conn, a)
    assert count(store, "agent_operator") == 0


def test_attaching_the_same_spec_twice_says_already_and_writes_nothing(store):
    a, _ = register(store, "partner-a", role_doc())
    with store.write() as conn:
        first = op.attach_in(conn, a)
    with store.write() as conn:
        again = op.attach_in(conn, a)
    assert first.attached and not again.attached and again.record_id == first.record_id
    assert count(store, "agent_attachments") == 1


# ------------------------------------------------------------------------------------------ the revise refusal

def revise(store, name, doc, **kw):
    digest = add(store, doc)
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"], d["role"] = name, {"name": doc["name"], "role_hash": digest}
    d.update(kw)
    return spec_mod.register_spec(store, spec_mod.parse_spec(d), ZONE, revise=True)


def attached(store, name="partner-a", doc=None):
    a, _ = register(store, name, doc or role_doc())
    with store.write() as conn:
        op.attach_in(conn, a)
    return a


def test_revising_an_attached_spec_into_a_job_is_refused(store):
    attached(store)
    with pytest.raises(spec_mod.SpecError, match="is attached to the agent as role 'partner-role'"):
        revise(store, "partner-a", role_doc("partner-role", kind="job", lands=("something else",)), stop_at=100)
    assert [x["current_version"] for x in spec_mod.list_agents(store)] == [1]


def test_revising_an_attached_spec_into_another_role_is_refused(store):
    attached(store)
    with pytest.raises(spec_mod.SpecError, match="a revise keeps that role"):
        revise(store, "partner-a", role_doc("a-different-role"), stop_at=100)
    assert [x["current_version"] for x in spec_mod.list_agents(store)] == [1]


def test_revising_an_attached_spec_into_a_role_the_store_does_not_hold_is_refused(store):
    a = attached(store)
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"], d["role"] = "partner-a", {"name": "partner-role", "role_hash": "ab" * 32}
    with pytest.raises(spec_mod.SpecError, match="not registered"):
        spec_mod.register_spec(store, spec_mod.parse_spec(d), ZONE, revise=True)
    assert spec_mod.get_agent(store, a)["current_version"] == 1


def test_revising_an_attached_spec_into_a_newer_version_of_its_role_passes_and_stays_attached(store):
    a = attached(store)
    got = revise(store, "partner-a", role_doc("partner-role", lands=("the partnership memory", "its notes")))
    assert got[0] == a and got[2] == 2
    agent = view(store)
    assert [(r.agent_id, r.role_name, r.role_version, r.version) for r in agent.roles] == [(a, "partner-role", 2, 2)]


def test_revising_a_spec_that_is_not_attached_may_change_its_role(store):
    register(store, "partner-a", role_doc())
    assert revise(store, "partner-a", role_doc("a-different-role"), stop_at=100)[2] == 2


# --------------------------------------------------- a corrupt or orphan attachment is never skipped, never "already"

def test_an_attachment_with_no_agent_is_unreadable_never_skipped(store):
    attached(store)
    ghost = "ag-" + "9" * 32
    tamper(store, ["agent_attachments_no_replace"],
           "INSERT INTO agent_attachments (agent_id, role_name, attached_at) VALUES (?,?,?)",
           (ghost, "partner-role", "2026-11-05T00:00:00Z"))
    with pytest.raises(op.OperatorAgentUnreadable, match=ghost):
        view(store)


def test_an_attachment_whose_spec_row_is_corrupt_is_unreadable(store):
    a = attached(store)
    tamper(store, ["agent_specs_frozen"], "UPDATE agent_specs SET spec = '{}' WHERE agent_id = ?", (a,))
    with pytest.raises(op.OperatorAgentUnreadable, match=a):
        view(store)


def test_an_attachment_naming_another_role_than_its_spec_pins_is_unreadable(store):
    a = attached(store)
    tamper(store, ["agent_attachments_frozen"], "UPDATE agent_attachments SET role_name = 'impostor' WHERE agent_id = ?",
           (a,))
    with pytest.raises(op.OperatorAgentUnreadable, match="impostor"):
        view(store)


def test_an_attached_role_row_that_fails_its_hash_is_unreadable(store):
    attached(store)
    tamper(store, [], "UPDATE agent_roles SET spec = replace(spec, 'run', 'ran')")
    with pytest.raises(op.OperatorAgentUnreadable, match="partner-role"):
        view(store)


def test_a_record_id_that_is_not_an_oa_id_is_unreadable(store):
    attached(store)
    tamper(store, ["agent_operator_frozen"], "UPDATE agent_operator SET record_id = 'x'")
    with pytest.raises(op.OperatorAgentUnreadable, match="record"):
        view(store)


def test_attaching_over_a_corrupt_existing_attachment_raises_and_never_says_already(store):
    a = attached(store)
    tamper(store, ["agent_attachments_frozen"], "UPDATE agent_attachments SET role_name = 'impostor' WHERE agent_id = ?",
           (a,))
    with store.write() as conn, pytest.raises(op.OperatorAgentUnreadable, match="impostor"):
        op.attach_in(conn, a)


def test_register_dream_over_a_corrupt_attachment_is_an_enable_refusal_not_a_silent_pass(store):
    reg = da.register_dream(store)
    tamper(store, ["agent_attachments_frozen"], "UPDATE agent_attachments SET role_name = 'impostor' WHERE agent_id = ?",
           (reg.agent_id,))
    with pytest.raises(da.EnableRefused, match="impostor"):
        da.register_dream(store)


def test_an_attachment_whose_valid_role_is_a_job_is_unreadable_never_shown_as_an_attached_role(store):
    """fixk2 (L3 k2 r1, codex MED): a direct writer attaches a valid spec pinning a valid JOB; attach_in refuses exactly
    that state, and materialise reported it as an attached role with exit 0."""
    attached(store)                                      # the record exists
    job, _ = register(store, "worker", role_doc("job-role", kind="job"))
    c = raw(store)
    c.execute("INSERT INTO agent_attachments (agent_id, role_name, attached_at) VALUES (?,?,?)",
              (job, "job-role", "2026-11-05T00:00:00Z"))   # the schema permits it: no trigger judges the role's kind
    c.close()
    with pytest.raises(op.OperatorAgentUnreadable, match=rf"{job}.*job"):
        view(store)


def test_a_retired_attached_spec_is_listed_under_retired_roles_not_dropped(store):
    a = attached(store)
    spec_mod.retire_agent(store, "partner-a")
    agent = view(store)
    assert agent.roles == () and [r.agent_id for r in agent.retired_roles] == [a]


# ------------------------------------------------------------------------------------------------ install facts

def test_the_install_is_not_consulted_without_a_directory_and_says_so_with_none(store, tmp_path):
    da.register_dream(store)
    assert view(store).install is None
    assert view(store, tmp_path / "store").install.label == "no install facts"


def test_an_unreadable_install_file_is_shown_as_its_own_refusal_never_as_missing(store):
    da.register_dream(store)
    (store.directory / da.INSTALL_FILE).write_text("{not json")
    got = view(store, store.directory).install
    assert got.label.startswith("install facts unreadable: ") and "cannot be read" in got.label and got.kind is None


def test_a_missing_install_directory_is_no_install_facts_and_does_not_raise(store, tmp_path):
    """fixk2 (L3 k2 r1, deepseek LOW): materialise with an install_dir that does not exist."""
    da.register_dream(store)
    missing = tmp_path / "no-such-dir"
    assert not missing.exists()
    got = view(store, missing).install
    assert (got.kind, got.label, got.memory_path) == (None, "no install facts", None)


def test_the_install_kind_and_memory_path_come_from_one_read_of_the_facts_file(store, tmp_path, monkeypatch):
    """fixk2 (L3 k2 r1, codex LOW): the kind came from one read and the memory path from another, so a facts file
    replaced between them (`dream enable` live -> shadow) gave a kind that is not the file's. Here a first read that
    would have answered "shadow" while the file says live: the kind must be the one the validated read saw."""
    da.register_dream(store)
    mem, cockpit, coord = tmp_path / "memory.db", tmp_path / "cockpit", tmp_path / "coord"
    mem.write_text("")
    cockpit.mkdir()
    coord.mkdir()
    da.write_install(store.directory, store_path=mem, cockpit_root=cockpit, coord_dir=coord, zone=ZONE)
    monkeypatch.setattr(da, "install_kind", lambda _d: "shadow")     # the other read, had it run, saw another file
    got = view(store, store.directory).install
    assert (got.kind, got.label, got.memory_path) == ("live", "live", mem)


def test_a_live_install_names_its_kind_and_memory_path(store, tmp_path):
    da.register_dream(store)
    mem, cockpit, coord = tmp_path / "memory.db", tmp_path / "cockpit", tmp_path / "coord"
    mem.write_text("")
    cockpit.mkdir()
    coord.mkdir()
    da.write_install(store.directory, store_path=mem, cockpit_root=cockpit, coord_dir=coord, zone=ZONE)
    got = view(store, store.directory).install
    assert (got.kind, got.label, got.memory_path) == ("live", "live", mem)


# ------------------------------------------------------------------------- is_dream, the one shared predicate

def test_is_dream_is_the_predicate_refuse_and_the_read_side_both_call(store, monkeypatch):
    role = role_mod.parse_role(da.dream_role())
    parsed = spec_mod.parse_spec(da.dream_spec(role_mod.role_hash(role)))
    assert refuse.is_dream(parsed, role)
    assert not refuse.is_dream(parsed, {**role, "executor": "session"})
    assert not refuse.is_dream({**parsed, "name": "other"}, role) and not refuse.is_dream(parsed, None)
    # refuse.check_spec still lets the dream's own registration pass and refuses an operator's
    assert refuse.check_spec(parsed, role=role, default_zone=ZONE, as_dream=True) == []
    assert [r.rule for r in refuse.check_spec(parsed, role=role, default_zone=ZONE)] == ["name"]
    # and both sides go through the one name: with it answering no, the registration refuses and the read side sees none
    pre = open_agent_store(store.directory.parent / "pre")
    role_mod.add_role(pre, role)
    spec_mod.register_spec(pre, parsed, ZONE)
    with pre.read() as conn:
        assert op.unattached_dream(conn) is not None
    monkeypatch.setattr(refuse, "is_dream", lambda spec, role: False)
    assert [r.rule for r in refuse.check_spec(parsed, role=role, default_zone=ZONE, as_dream=True)] == ["name"]
    with pre.read() as conn:
        assert op.unattached_dream(conn) is None


# --------------------------------------------------------------------------------- ATTACHMENT CONFERS NOTHING

START = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)


def fires(store, armed):
    box = [START]
    ctl.tick(store, _clock(box), armed)                 # the first look sets the cursor
    for _ in range(5):
        box[0] += timedelta(minutes=10)
        ctl.tick(store, _clock(box), armed)
    with store.read() as c:
        return c.execute("SELECT key, state, reason FROM agent_occurrences ORDER BY key").fetchall()


def twin(tmp_path, name, *, attach):
    s = open_agent_store(tmp_path / name)
    set_default_zone(s, ZONE)
    a, _ = register(s, "partner-a", role_doc(), trigger={"kind": "schedule", "cron": "*/20 * * * *", "zone": ZONE})
    if attach:
        with s.write() as conn:
            op.attach_in(conn, a)
    return s


def test_an_attached_spec_with_no_signed_link_cannot_fire(tmp_path):
    s = twin(tmp_path, "att", attach=True)
    assert view(s) is not None
    assert fires(s, lambda agent_id, spec_hash: None) == []          # no link: nothing is admitted
    with s.read() as c:
        assert c.execute("SELECT COUNT(*) FROM agent_occurrences").fetchone()[0] == 0


def test_an_unattached_spec_with_its_link_behaves_exactly_as_before_and_as_an_attached_one(tmp_path):
    armed = lambda agent_id, spec_hash: "b1"   # noqa: E731 - the link, as the controller sees it
    plain = fires(twin(tmp_path, "plain", attach=False), armed)
    joined = fires(twin(tmp_path, "joined", attach=True), armed)
    assert plain and plain == joined and plain[0][1] == "admitted"


def test_recall_stays_keyed_by_the_ag_id_and_the_record_id_recalls_nothing(store, tmp_path):
    from levain.agents.recall import recall_agent_episodes_at
    from levain.firing import CaptureRequest
    from levain.firing.anneal import AnnealFiring

    a = attached(store)
    record_id = view(store).record_id
    db = tmp_path / "memory.db"
    assert AnnealFiring(episodic_path=db, identity={"agent": a, "run": "occ-1"}).capture(
        CaptureRequest(content="mine", episode_type="observation", source="t", session_id=None, metadata=None))
    assert [e.content for e in recall_agent_episodes_at(db, a).episodes] == ["mine"]
    with pytest.raises(ValueError):                     # an oa- id is not an agent id to recall, and never becomes one
        recall_agent_episodes_at(db, record_id)


# the modules of the fire, link, binding and recall paths: none may import the operator module or name the table.
# (the registry's own files are `spec.py` and `cli.py`, which reach it lazily; `dream/agent.py` reaches it only through
# `register_spec(..., attach=True)`; the cockpit's agents panel is a reader, not a fire path)
NOT_THE_REGISTRY = ("controller", "schedule", "signing", "recall", "session_wire", "session_launcher", "session_client",
                    "session", "supervise", "tick_driver", "bars", "slots", "accounting", "authority_replica",
                    "instantiate", "gauges", "store")


def fire_path_files() -> list[Path]:
    files = [LEVAIN / "agents" / f"{m}.py" for m in NOT_THE_REGISTRY]
    files += [LEVAIN / "agent_authority.py", LEVAIN / "authority.py", LEVAIN / "authority_sig.py",
              LEVAIN / "dream" / "agent.py"]
    files += sorted((LEVAIN / "firing").glob("*.py"))
    return files


def test_no_module_of_the_fire_link_binding_or_recall_paths_imports_the_operator_module_or_names_its_table():
    files = fire_path_files()
    assert len(files) >= 20 and all(f.is_file() for f in files)      # the list is read, not assumed
    offenders = []
    for f in files:
        text = f.read_text()
        if re.search(r"agents\.operator|agents import[^\n]*\boperator\b|from \.operator|import operator", text):
            offenders.append((f.name, "imports"))
        if "agent_attachments" in text or "agent_operator" in text:
            offenders.append((f.name, "names the table"))
    assert offenders == []


def test_the_only_modules_that_name_the_attachment_table_are_the_schema_and_the_registry():
    named = sorted(p.relative_to(LEVAIN).as_posix() for p in LEVAIN.rglob("*.py")
                   if "agent_attachments" in p.read_text())
    assert named == ["agents/operator.py", "autonomic/agent_tables.py"]
