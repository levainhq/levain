"""P7-1 S1: ``run_agent`` selects the run body by the executor of the role the occurrence's signed spec pins, never by
the agent's name; ``dream`` is a reserved name. Stores are real ``/2`` agent stores; the launcher is a stub whose child
is a fake gate, so no process starts and no real session runs (the residue is the VM round's)."""
from __future__ import annotations

import copy
import shutil

import pytest

from levain.agents import controller as ctl
from levain.agents import refuse, session as S
from levain.agents.role import add_role, parse_role, role_hash
from levain.agents.spec import parse_spec
from tests.test_agents_launch import ROLES, SPECS, Rig
from tests.test_agents_session_launch import ROLE, Stub, _occurrence, _sup, rig  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _session_occurrence(rig, name="research-bot", **kw):
    """A launched occurrence of a session-executor agent whose spec is SIGNED under ``name``, its pinned role
    registered in the store."""
    oid = _occurrence(rig, name=name, **kw)
    add_role(rig.store, parse_role(ROLE))
    return oid


def _rename_row(rig, oid, name):
    """Rewrite the mutable ``agents.name`` row under a signed spec (a store writer's move): the run must not follow."""
    with rig.store.write() as c:
        c.execute("UPDATE agents SET name = ? WHERE agent_id = (SELECT agent_id FROM agent_occurrences"
                  " WHERE occurrence_id = ?)", (name, oid))


def _factory(rig, oid, launcher):
    def factory(store, occurrence_id, epoch):
        return {"sup": _sup(rig, oid), "role": ROLE, "values": {}, "recall": None, "launcher": launcher,
                "exposure": None, "max_exposure_usd_micros": 100_000, "clock": rig.now}
    return factory


def test_a_session_role_reaches_session_py_through_the_entry_point(rig, monkeypatch):
    # reproduced first: on the name-keyed dispatch this agent (not called "dream") ended FIRE_UNWIRED and the stub
    # launcher was never asked
    oid = _session_occurrence(rig)
    launcher = Stub()
    monkeypatch.setattr(ctl, "SESSION_FACTORY", _factory(rig, oid, launcher))
    assert ctl.run_agent(rig.store, oid, rig.row(oid)["launch_epoch"]) == 0
    assert len(launcher.launches) == 1 and rig.row(oid)["state"] == "done"


def test_the_executor_decides_not_the_name(rig, monkeypatch):
    # a session role under the name "dream" is still a session run
    oid = _session_occurrence(rig, name="dream")
    launcher = Stub()
    monkeypatch.setattr(ctl, "SESSION_FACTORY", _factory(rig, oid, launcher))
    assert ctl.run_agent(rig.store, oid, rig.row(oid)["launch_epoch"]) == 0
    assert len(launcher.launches) == 1


def test_a_session_occurrence_with_no_factory_override_uses_the_production_factory(rig, capsys):
    # S2b: the production default is session_wire.fire_factory; with no install file it builds no run (the row stays
    # launched for lapse recovery), where S1 ended it unwired by name
    oid = _session_occurrence(rig)
    assert ctl.SESSION_FACTORY is None
    assert ctl.run_agent(rig.store, oid, rig.row(oid)["launch_epoch"]) == 1
    assert rig.row(oid)["state"] == "launched"


def test_the_default_launcher_still_refuses(rig, monkeypatch):
    # the seam is open but nothing is wired: a factory that supplies the default launcher launches nothing
    oid = _session_occurrence(rig)
    monkeypatch.setattr(ctl, "SESSION_FACTORY", _factory(rig, oid, S.UnwiredLauncher()))
    ctl.run_agent(rig.store, oid, rig.row(oid)["launch_epoch"])
    assert rig.row(oid)["reason"] == "session_refused:launcher-not-wired"


def test_an_executor_with_no_body_refuses_naming_it(rig, capsys):
    oid = _session_occurrence(rig)
    assert ctl.run_agent(rig.store, oid, 1, bodies={"dream": lambda: (lambda s, o, e: 0)}) == ctl.FIRE_UNWIRED
    assert "no run body for executor 'session'" in capsys.readouterr().err


def test_a_pinned_role_that_is_not_in_the_store_has_no_executor(rig, capsys):
    oid = _occurrence(rig)          # the role is not added: the row cannot be resolved
    assert ctl.run_agent(rig.store, oid, 1) == ctl.FIRE_UNWIRED
    assert "is unreadable" in capsys.readouterr().err


def test_the_dream_stays_reachable_by_its_own_executor(tmp_path):
    # a dream-executor role (notify-role) signed under the name "dream" gets the dream's body
    seen = []
    rig = Rig(tmp_path, name="dream")
    add_role(rig.store, parse_role(ROLES["notify-role"]))
    oid = rig.claim("k")
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": lambda: (lambda s, o, e: seen.append(o) or 0)}) == 0
    assert seen == [oid]


# --- 1104-8 F1: the name is the SIGNED spec's, never the mutable agents.name row ------------------------------------

def test_a_row_renamed_dream_over_a_signed_non_dream_spec_does_not_run_the_dream(rig, capsys):
    # reproduced first on 451b197c: the dream body ran (the name was read from agents.name)
    add_role(rig.store, parse_role(ROLES["notify-role"]))     # the rig's spec pins this dream-executor role
    oid = rig.claim("k")
    with rig.store.write() as c:
        c.execute("UPDATE agents SET name = 'dream' WHERE agent_id = ?", (rig.agent,))
    seen = []
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": lambda: (lambda s, o, e: seen.append(o) or 0)}) \
        == ctl.FIRE_UNWIRED and seen == []
    err = capsys.readouterr().err
    assert "its signed spec is refused" in err and "the stored spec names 'job_push_check', not 'dream'" in err


def test_a_dream_spec_under_another_row_name_does_not_run_as_a_session(rig, monkeypatch, capsys):
    # reproduced first on 451b197c: a spec signed "dream" pinning a session role, its row renamed, ran as a session
    oid = _session_occurrence(rig, name="dream")
    _rename_row(rig, oid, "research-bot")
    launcher = Stub()
    monkeypatch.setattr(ctl, "SESSION_FACTORY", _factory(rig, oid, launcher))
    assert ctl.run_agent(rig.store, oid, rig.row(oid)["launch_epoch"]) == ctl.FIRE_UNWIRED
    assert launcher.launches == [] and rig.row(oid)["state"] == "launched"
    assert "the stored spec names 'dream', not 'research-bot'" in capsys.readouterr().err


def test_a_renamed_session_row_does_not_run_either(rig, monkeypatch):
    oid = _session_occurrence(rig)
    _rename_row(rig, oid, "someone-else")
    launcher = Stub()
    monkeypatch.setattr(ctl, "SESSION_FACTORY", _factory(rig, oid, launcher))
    assert ctl.run_agent(rig.store, oid, rig.row(oid)["launch_epoch"]) == ctl.FIRE_UNWIRED
    assert launcher.launches == []


def test_a_dream_executor_under_another_name_ends_unwired(rig, capsys):
    add_role(rig.store, parse_role(ROLES["notify-role"]))
    oid = rig.claim("k")
    seen = []
    assert ctl.run_agent(rig.store, oid, 0, bodies={"dream": lambda: (lambda s, o, e: seen.append(o) or 0)}) \
        == ctl.FIRE_UNWIRED and seen == []
    assert "runs only the agent named 'dream'" in capsys.readouterr().err


# --- the reserved name -----------------------------------------------------------------------------------------------

def _spec_named(name, role_name):
    d = copy.deepcopy(SPECS["job_push_check"])
    d["name"] = name
    d["role"] = {"name": role_name, "role_hash": role_hash(ROLES[role_name])}
    return parse_spec(d)


def _reasons(spec, role_name, **kw):
    return [str(r) for r in refuse.check_spec(spec, role=parse_role(ROLES[role_name]), default_zone="America/New_York",
                                              **kw)]


def test_an_operator_cannot_register_the_name_dream_for_a_session_role():
    reasons = _reasons(_spec_named("dream", "model-role"), "model-role")
    assert any("name 'dream' is reserved for the dream executor" in r for r in reasons)


def test_an_operator_registration_of_a_dream_executor_role_named_dream_is_refused_too():
    reasons = _reasons(_spec_named("dream", "notify-role"), "notify-role")
    assert any("reserved for the dream executor" in r for r in reasons)


def test_the_dreams_own_registration_may_take_the_name():
    reasons = _reasons(_spec_named("dream", "notify-role"), "notify-role", as_dream=True)
    assert not any("reserved" in r for r in reasons)


def test_as_dream_does_not_open_the_name_to_a_session_role():
    reasons = _reasons(_spec_named("dream", "model-role"), "model-role", as_dream=True)
    assert any("reserved for the dream executor" in r for r in reasons)


def test_any_other_name_is_not_reserved():
    assert not any("reserved" in r for r in _reasons(_spec_named("research-bot", "model-role"), "model-role"))
