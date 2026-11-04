"""P7-1 S5: the register-to-done path of a session agent. Register (the CLI), sign (the test's own test key, as every
agents test does: the rig's world links the grant), fire (the production factory, a stub launcher whose child is a fake
gate), and read back with ``levain agents show <agent> --json``: one ``done`` occurrence and its episodes under the
agent id. The spec names the REAL win check (``session_completed``); nothing here stubs ``gauges._CHECKS``.

Not held here (the residue): his signing (R-H3) and the ``--dispatch`` sign-in (R-H1) are Phill's hands; a real launcher
and a real ``claude -p`` are S4/S7's.
"""
from __future__ import annotations

import copy
import json
import shutil

import pytest

from levain.agents import controller as ctl
from levain.agents import gauges
from levain.agents import session_wire as W
from levain.cli import main
from tests.test_agents_launch import SPECS
from tests.test_agents_session_launch import ROLE, BINDING, Stub, rig  # noqa: F401
from tests.test_agents_session_wire import _install
from tests.test_agents_run_by_executor import _session_occurrence as _renamed_occurrence

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

NAME = "job_feeder_downstream"      # the signed spec's name: a renamed agent no longer re-hashes


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(W, "LAUNCHERS", {})
    monkeypatch.setattr(gauges, "_CHECKS", {})          # a fresh registry: only what the fire itself registers
    real = W.fire_factory
    monkeypatch.setattr(W, "fire_factory", lambda s, o, e, clock=None: real(s, o, e, clock=_clock[0]))
    assert ctl.SESSION_FACTORY is None


_clock: list = [None]


@pytest.fixture(autouse=True)
def _pin_clock(rig):
    _clock[0] = rig.now


@pytest.fixture(autouse=True)
def _real_win_check(monkeypatch):
    """The fixture spec names ``compute_anomalies``; a session agent names the real check."""
    monkeypatch.setitem(SPECS[NAME], "win", {"kind": "verifier", "check": W.SESSION_WIN_CHECK})


def _occ(rig):
    return _renamed_occurrence(rig, name=NAME)


def _fire(rig, oid, monkeypatch):
    from levain.consent import delivery
    monkeypatch.setattr(delivery, "process_identity", lambda pid: ("2026-11-04T00:00:00", f"levain agents _fire {oid}"))
    return ctl.fire(rig.store, oid, run=ctl.run_agent, clock=rig.now)


def _episodic(tmp_path):
    from anneal_memory import Store
    p = tmp_path / "episodes.db"
    Store(p, audit=False).close()
    return p


def _agent_id(rig, oid):
    return rig.row(oid)["agent_id"]


def _show(rig, capsys, ref):
    capsys.readouterr()
    code = main(["agents", "--store-dir", str(rig.store.directory), "show", ref, "--json"])
    out = capsys.readouterr().out
    return code, (json.loads(out) if code == 0 else None)


# --- refusals first ------------------------------------------------------------------------------------------------

def test_an_unsigned_session_agent_never_fires(rig, monkeypatch):
    """No grant is linked: the claim is made but launch refuses, so no occurrence is ever ``launched`` and the stub is
    never asked, whatever the install names."""
    from levain.agents.role import add_role, parse_role, role_hash
    from levain.agents.spec import parse_spec, register_spec
    from tests.test_agents_session_launch import ApiExec, EXP, WINDOW
    d = copy.deepcopy(SPECS[NAME])
    d["role"]["role_hash"] = role_hash(ROLE)
    add_role(rig.store, parse_role(ROLE))
    agent, spec_hash, *_ = register_spec(rig.store, parse_spec(d), "America/New_York")
    with rig.store.write() as c:
        oid, outcome = ctl.claim(c, agent_id=agent, kind="manual", key="s", spec_hash=spec_hash, binding_id=BINDING,
                                 window_key="m:2026-10-25", now=rig.now())
    assert outcome == ctl.CLAIMED
    res = ctl.launch(rig.store, oid, authority=rig.authority, executor=ApiExec(0, EXP), window=WINDOW, now=rig.now())
    assert res.outcome != "launched"
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub")
    _fire(rig, oid, monkeypatch)      # the fire finds nothing it holds: whatever it exits, nothing ran
    assert stub.launches == [] and rig.row(oid)["state"] == "stopped" and res.outcome != "launched"


def test_a_signed_session_agent_with_no_launcher_ends_launcher_not_wired(rig, monkeypatch):
    oid = _occ(rig)
    _install(rig)
    assert _fire(rig, oid, monkeypatch) == 1
    row = rig.row(oid)
    assert row["state"] == "failed" and row["reason"] == "session_refused:launcher-not-wired"
    assert rig.taken(oid)[0] == []


def test_the_session_win_check_refuses_a_run_that_never_spawned(rig):
    oid = _occ(rig)
    ctx = gauges.WinContext(rig.store, oid, _agent_id(rig, oid), {}, None, None, rig.now())
    assert rig.row(oid)["session_key"] is None
    assert W.session_win_check(ctx) == "session_never_started"
    assert W.session_win_check(gauges.WinContext(rig.store, "no-such", "x", {}, None, None, rig.now())) \
        == "session_never_started"


def test_a_spec_that_names_another_check_still_ends_win_unverified(rig, monkeypatch):
    """Registering the session check registers ONE name; a spec naming a check nobody registered is still unverified."""
    monkeypatch.setitem(SPECS[NAME], "win", {"kind": "verifier", "check": "compute_anomalies"})
    oid = _occ(rig)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub")
    assert _fire(rig, oid, monkeypatch) == 1
    assert rig.row(oid)["state"] == "failed" and rig.row(oid)["reason"].startswith("win_unverified")


def test_show_json_says_why_when_there_is_no_episodic_store(rig, capsys):
    oid = _occ(rig)
    code, doc = _show(rig, capsys, _agent_id(rig, oid))
    assert code == 0 and doc["episodes"] is None and "no episodic store" in doc["episodes_note"]
    assert [o["state"] for o in doc["occurrences"]] == ["launched"]


# --- the path ------------------------------------------------------------------------------------------------------

def test_register_sign_fire_show_one_done_occurrence_with_its_episode_under_the_agent_id(rig, monkeypatch, capsys,
                                                                                         tmp_path):
    oid = _occ(rig)
    aid = _agent_id(rig, oid)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub", episodic_path=_episodic(tmp_path))
    assert _fire(rig, oid, monkeypatch) == 0
    row = rig.row(oid)
    assert (row["state"], row["reason"]) == ("done", "completed") and len(stub.launches) == 1
    code, doc = _show(rig, capsys, NAME)
    assert code == 0
    assert doc["agent"]["agent_id"] == aid and doc["spec"]["win"]["check"] == W.SESSION_WIN_CHECK
    assert [(o["occurrence_id"], o["state"]) for o in doc["occurrences"]] == [(oid, "done")]
    assert len(doc["episodes"]) == 1
    ep = doc["episodes"][0]
    assert ep["metadata"]["agent"] == aid and ep["metadata"]["run"] == oid and len(ep["metadata"]["role"]) == 64
    assert "ended done: completed" in ep["content"] and doc["episodes_note"] is None


def test_a_failed_run_also_leaves_its_episode_under_the_agent_id(rig, monkeypatch, capsys, tmp_path):
    oid = _occ(rig)
    stub = Stub(rc=1)
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub", episodic_path=_episodic(tmp_path))
    assert _fire(rig, oid, monkeypatch) == 1
    assert rig.row(oid)["state"] == "failed"
    _, doc = _show(rig, capsys, _agent_id(rig, oid))
    assert [o["state"] for o in doc["occurrences"]] == ["failed"] and len(doc["episodes"]) == 1
    assert "ended failed" in doc["episodes"][0]["content"]


def test_another_agents_episodes_are_not_shown_under_this_one(rig, monkeypatch, capsys, tmp_path):
    from levain.firing.anneal import AnnealFiring
    from levain.firing.contract import CaptureRequest
    ep = _episodic(tmp_path)
    AnnealFiring(episodic_path=ep, identity={"agent": "ag-" + "0" * 32, "run": "other"}).capture(
        CaptureRequest(content="somebody else's run", episode_type="outcome"))
    oid = _occ(rig)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub", episodic_path=ep)
    assert _fire(rig, oid, monkeypatch) == 0
    _, doc = _show(rig, capsys, NAME)
    assert [e["metadata"]["agent"] for e in doc["episodes"]] == [_agent_id(rig, oid)]


def test_the_session_win_check_registers_once_and_idempotently(rig):
    W.register_session_win_check()
    W.register_session_win_check()
    assert gauges._CHECKS[W.SESSION_WIN_CHECK] is W.session_win_check


def test_no_install_episodic_path_means_no_episode_hook(rig, monkeypatch, capsys):
    oid = _occ(rig)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub")
    assert _fire(rig, oid, monkeypatch) == 0 and rig.row(oid)["state"] == "done"
    _, doc = _show(rig, capsys, NAME)
    assert doc["episodes"] is None


def test_the_cli_registers_a_session_agent_spec(tmp_path, capsys):
    """The register verb accepts a session agent whose win names the session check (role first, then the spec)."""
    from levain.agents.role import role_hash
    d = ["agents", "--store-dir", str(tmp_path / "store")]
    rf = tmp_path / "role.json"
    rf.write_text(json.dumps(ROLE))
    assert main([*d, "role", "add", str(rf)]) == 0
    raw = copy.deepcopy(SPECS[NAME])
    raw["role"]["role_hash"] = role_hash(ROLE)
    sf = tmp_path / "spec.json"
    sf.write_text(json.dumps(raw))
    capsys.readouterr()
    assert main([*d, "zone", "America/New_York"]) == 0
    assert main([*d, "register", "--from", str(sf)]) == 0
    capsys.readouterr()
    assert main([*d, "show", NAME, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["agent"]["name"] == NAME and doc["occurrences"] == [] and doc["episodes"] is None
