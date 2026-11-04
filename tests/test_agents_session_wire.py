"""P7-1 S2: the production factory for ``session.make_run``. ``fire`` -> ``run_agent`` -> ``SESSION_FACTORY`` (the
factory under test) -> ``run_session``, with the launcher reaching the run only through the install-fact file
(``session_install.json``) and the launcher registry. Stores are real ``/2`` agent stores; the launcher is a stub whose
child is a fake gate, so nothing starts (the real launcher is S4's)."""
from __future__ import annotations

import json
import shutil

import pytest

from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import session_wire as W
from tests.test_agents_run_by_executor import _session_occurrence as _signed_occurrence
from tests.test_agents_session_launch import Stub, rig  # noqa: F401


def _session_occurrence(rig, **kw):
    return _signed_occurrence(rig, name="job_feeder_downstream", **kw)

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


@pytest.fixture(autouse=True)
def _wired(monkeypatch):
    monkeypatch.setattr(W, "LAUNCHERS", {})


@pytest.fixture(autouse=True)
def _factory(rig, monkeypatch):
    # the rig's authority log is dated in its own clock, so the supervisor and the run read the rig's time
    monkeypatch.setattr(ctl, "SESSION_FACTORY", lambda s, o, e: W.fire_factory(s, o, e, clock=rig.now))


def _admit_run(rig, oid):
    """The agent journal's admission of the run: nothing in the factory's closure does it (see RETURN observation 2),
    the test stands in for the admitting path as ``test_agents_session_launch._sup`` does."""
    from levain.autonomic.journal import RunJournal
    from tests.test_agents_session_launch import BINDING
    RunJournal(rig.store.directory).start(rig.row(oid)["run_id"], binding_id=BINDING)


def _fire(rig, oid, monkeypatch):
    _admit_run(rig, oid)
    from levain.consent import delivery
    monkeypatch.setattr(delivery, "process_identity", lambda pid: ("2026-11-04T00:00:00", f"levain agents _fire {oid}"))
    return ctl.fire(rig.store, oid, run=ctl.run_agent, clock=rig.now)


def _install(rig, **kw):
    return W.write_install(rig.store.directory, cockpit_root=rig.world.root, **kw)


# --- refusals first -------------------------------------------------------------------------------------------------

def test_with_no_launcher_configured_the_occurrence_settles_at_zero_as_not_wired(rig, monkeypatch):
    oid = _session_occurrence(rig)
    _install(rig)
    assert _fire(rig, oid, monkeypatch) == 1
    row = rig.row(oid)
    assert row["state"] == "failed" and row["reason"] == "session_refused:launcher-not-wired"
    assert rig.taken(oid)[0] == [] and all(r[0] != "open" for r in rig.taken(oid)[1])   # slot and reservation released


def test_a_launcher_name_nobody_registered_is_refused_not_defaulted(rig, monkeypatch):
    oid = _session_occurrence(rig)
    _install(rig, launcher="ghost")
    assert _fire(rig, oid, monkeypatch) == 1
    assert rig.row(oid)["state"] == "launched"      # the run was not built: nothing settled, nothing spawned


def test_a_launcher_that_is_not_a_session_launcher_is_refused(rig, monkeypatch):
    oid = _session_occurrence(rig)
    W.register_launcher("junk", lambda store: object())
    _install(rig, launcher="junk")
    assert _fire(rig, oid, monkeypatch) == 1
    assert rig.row(oid)["state"] == "launched"


def test_a_registered_launcher_off_the_dispatch_route_is_refused(rig, monkeypatch):
    oid = _session_occurrence(rig)
    stub = Stub(route="coordinator")
    W.register_launcher("odd", lambda store: stub)
    _install(rig, launcher="odd")
    assert _fire(rig, oid, monkeypatch) == 1
    assert stub.launches == [] and rig.row(oid)["state"] == "launched"


def test_the_unwired_launcher_cannot_be_registered_as_wired(rig):
    with pytest.raises(W.InstallRefused):
        W.register_launcher("unwired", lambda store: S.UnwiredLauncher())
    W.register_launcher("disguised", lambda store: S.UnwiredLauncher())
    with pytest.raises(W.InstallRefused):
        W.resolve_launcher(rig.store, W.SessionInstall(rig.world.root, "disguised", None, {}))


def test_a_launcher_only_comes_from_the_install_source(rig, monkeypatch):
    # a registered launcher that no install file names is never used: the default stands
    oid = _session_occurrence(rig)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig)
    assert _fire(rig, oid, monkeypatch) == 1
    assert stub.launches == [] and rig.row(oid)["reason"] == "session_refused:launcher-not-wired"


def test_no_install_file_builds_no_run(rig, monkeypatch):
    oid = _session_occurrence(rig)
    assert _fire(rig, oid, monkeypatch) == 1
    assert rig.row(oid)["state"] == "launched"


@pytest.mark.parametrize("doc", [{"schema": 2, "cockpit_root": "/"}, {"schema": 1, "cockpit_root": "rel"},
                                 {"schema": 1, "cockpit_root": "/", "extra": 1}, {"schema": 1},
                                 {"schema": 1, "cockpit_root": "/", "launcher": ""}, ["x"]])
def test_a_malformed_install_is_refused(rig, doc):
    (rig.store.directory / W.INSTALL_FILE).write_text(json.dumps(doc))
    with pytest.raises(W.InstallRefused):
        W.read_install(rig.store.directory)


def test_a_pinned_role_missing_from_the_store_builds_no_run(rig, monkeypatch):
    oid = _session_occurrence(rig)
    _install(rig)
    with rig.store.write() as c:
        c.execute("PRAGMA foreign_keys=OFF")
        c.execute("DELETE FROM agent_roles")
    # run_agent reads the executor off that role, so the entry point ends unwired before the factory; the factory
    # itself also refuses
    with pytest.raises(W.InstallRefused):
        W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])
    assert _fire(rig, oid, monkeypatch) == ctl.FIRE_UNWIRED and rig.row(oid)["state"] == "launched"


# --- the working path -----------------------------------------------------------------------------------------------

class _AcceptAll(dict):
    """Stands in for the spec's registered win check (P8b/S5's, not this slice's): every named check accepts."""

    def get(self, name, default=None):
        return lambda ctx: None


def test_with_the_stub_installed_fire_ends_one_occurrence_done(rig, monkeypatch):
    from levain.agents import gauges
    monkeypatch.setattr(gauges, "_CHECKS", _AcceptAll())
    oid = _session_occurrence(rig)
    stub = Stub()
    W.register_launcher("stub", lambda store: stub)
    _install(rig, launcher="stub")
    code = _fire(rig, oid, monkeypatch)
    row = rig.row(oid)
    assert (code, row["state"], row["reason"]) == (0, "done", "completed") and len(stub.launches) == 1
    assert stub.launches[0].occurrence_id == oid


def test_the_factory_derives_exposure_and_dollars_from_the_reservation(rig):
    oid = _session_occurrence(rig)
    _install(rig)
    kw = W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])
    reserved = S.reserved_terms(rig.store, oid)
    assert kw["exposure"] == reserved.exposure and kw["max_exposure_usd_micros"] == reserved.usd_micros
    assert isinstance(kw["launcher"], S.UnwiredLauncher) and kw["recall"] is None and kw["values"] == {}
    assert S.render_brief  # the keys are exactly run_session's keywords
    import inspect
    assert set(kw) - {"sup"} <= set(inspect.signature(S.run_session).parameters)


# --- 1104-8 F3: recall follows the SIGNED spec's memory, never the install alone --------------------------------------

@pytest.fixture
def _recalled(monkeypatch):
    """Every recall the factory asks for, answered with a sentinel (the recall itself is P4's, tested there)."""
    from levain.agents import recall as R
    asked = []
    monkeypatch.setattr(R, "recall_agent_episodes_at", lambda path, agent, **kw: asked.append((path, agent)) or "EP")
    return asked


def test_a_stateless_agent_gets_no_recall_even_when_the_install_names_episodes(rig, tmp_path, _recalled):
    # reproduced first on 451b197c: recall was "EP" (the install's episodic_path alone decided)
    oid = _session_occurrence(rig)                       # the fixture's spec leaves memory at its default, stateless
    _install(rig, episodic_path=tmp_path / "episodes.db")
    kw = W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])
    assert kw["recall"] is None and _recalled == []


def test_a_stateful_agent_gets_its_recall_from_the_install(rig, tmp_path, _recalled):
    oid = _session_occurrence(rig, memory="stateful")
    _install(rig, episodic_path=tmp_path / "episodes.db")
    kw = W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])
    assert kw["recall"] == "EP" and _recalled == [(tmp_path / "episodes.db", rig.row(oid)["agent_id"])]


def test_a_stateful_agent_with_no_episodes_named_gets_no_recall(rig, _recalled):
    oid = _session_occurrence(rig, memory="stateful")
    _install(rig)
    assert W.fire_factory(rig.store, oid, rig.row(oid)["launch_epoch"])["recall"] is None


# --- 1104-8: write_install validates before it replaces ---------------------------------------------------------------

def test_a_bad_rewrite_leaves_the_working_install(rig, tmp_path):
    # reproduced first on 451b197c: the bad file replaced the good one, then the read-back raised
    path = _install(rig, launcher="stub")
    before = path.read_bytes()
    with pytest.raises(W.InstallRefused):
        W.write_install(rig.store.directory, cockpit_root=tmp_path / "no-such-dir")
    with pytest.raises(W.InstallRefused):
        W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher="")
    assert path.read_bytes() == before and W.read_install(rig.store.directory).launcher == "stub"
    assert [p.name for p in rig.store.directory.iterdir() if p.name.startswith(".session_install.")] == []


def test_an_episodic_path_that_is_a_directory_is_refused(rig, tmp_path):
    with pytest.raises(W.InstallRefused, match="is a directory"):
        _install(rig, episodic_path=tmp_path)
