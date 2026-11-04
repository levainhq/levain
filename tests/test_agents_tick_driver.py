"""P7-1 S11: the tick driver (S10 DESIGN section 5): the bound as arithmetic, the watchdog, the executor_of router, the
launchd unit, and a dead-fire session row settled end to end by one `run_tick` through a client-shaped launcher."""
from __future__ import annotations

import argparse
import os
import plistlib
import shutil
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain import daemon
from levain.agents import controller as ctl
from levain.agents import session as S
from levain.agents import supervise
from levain.agents import tick_driver as T
from levain.agents.accounting import StopResult, Usage
from tests.test_agents_launch import Rig
from tests.test_agents_session_launch import BINDING, Stub, rig  # noqa: F401
from tests.test_agents_session_reaper_stop import _revoke, _session_row
from tests.test_agents_supervise import Clock

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

REPO = Path(__file__).resolve().parent.parent
DEAD_PID = 4_194_297        # above macOS's pid ceiling and Linux's default one: no such process


# --- the bound, as arithmetic from the constants -----------------------------------------------------------------

def test_the_bound_is_arithmetic_from_the_constants():
    assert T.TICK_INTERVAL_S == 60 and T.TICK_HARD_BOUND_S == 600
    assert T.TICK_SOFT_BOUND_S < T.TICK_HARD_BOUND_S
    # a full stop phase fits in one tick
    assert T.TICK_HARD_BOUND_S >= supervise.STOP_BUDGET_S + supervise.STOP_DEADLINE_S
    # the worst gap between two renewals of one row is one interval plus a tick that ran to its hard bound
    assert T.WORST_RENEW_GAP_S == T.TICK_INTERVAL_S + T.TICK_HARD_BOUND_S == 660
    assert T.WORST_RENEW_GAP_S < ctl.LEASE_S == supervise.LEASE_S == 900
    assert T.TICK_INTERVAL_S + T.TICK_HARD_BOUND_S < ctl.LEASE_S


def test_the_unit_runs_every_interval_and_exits_each_time(tmp_path):
    spec = daemon.build_tick_spec(agent_store=tmp_path, interval=T.TICK_INTERVAL_S)
    doc = plistlib.loads(daemon.LaunchdProvider().render_unit(spec).encode())
    assert doc["Label"] == daemon.DEFAULT_TICK_LABEL
    assert doc["StartInterval"] == T.TICK_INTERVAL_S and doc["KeepAlive"] is False and doc["RunAtLoad"] is False
    assert doc["ProgramArguments"][-4:] == ["agents", "--store-dir", str(tmp_path.resolve()), "tick"]
    assert "ThrottleInterval" not in doc


def test_print_unit_prints_the_plist_and_installs_nothing(tmp_path, capsys, monkeypatch):
    from levain.agents import cli
    called = []
    monkeypatch.setattr(daemon.LaunchdProvider, "install", lambda *a, **k: called.append(a), raising=False)
    parser = argparse.ArgumentParser()
    cli.add_parsers(parser.add_subparsers())
    args = parser.parse_args(["agents", "--store-dir", str(tmp_path), "tick", "--print-unit"])
    assert args.func(args) == 0 and called == []
    doc = plistlib.loads(capsys.readouterr().out.encode())
    assert doc["StartInterval"] == T.TICK_INTERVAL_S


# --- the watchdog -------------------------------------------------------------------------------------------------

def test_the_watchdog_hard_exits_a_hung_reap(tmp_path):
    """A reap that swallows the graceful bound (the shape a hung C call or a retry loop has) is ended by `os._exit`."""
    script = tmp_path / "hang.py"
    script.write_text(textwrap.dedent("""
        import time
        from levain.agents import tick_driver as T

        def hang():
            while True:
                try:
                    time.sleep(5)
                except BaseException:       # swallows TurnTimeout: only the watchdog can end this
                    pass

        T.bounded(hang, soft_s=0.3, hard_s=1.0)
        raise SystemExit(0)                 # unreachable when the watchdog works
    """))
    started = time.monotonic()
    got = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, timeout=30,
                         env={**os.environ, "PYTHONPATH": str(REPO)})
    assert got.returncode == T.EXIT_HARD_TIMEOUT, got
    assert time.monotonic() - started < 20
    assert "still running" in got.stderr


def test_the_graceful_bound_ends_a_slow_pass_and_names_it(rig, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(ctl, "tick", lambda *a, **k: time.sleep(10))
    code = T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None,
                      soft_s=0.3, hard_s=5.0)
    assert code == T.EXIT_SOFT_TIMEOUT and "graceful bound" in capsys.readouterr().err


def test_run_tick_arms_the_production_bounds(rig, monkeypatch):  # noqa: F811
    seen = {}
    real = T.bounded
    monkeypatch.setattr(T, "bounded", lambda fn, **kw: (seen.update(kw), real(fn, **kw))[1])
    assert T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None) == T.EXIT_OK
    assert seen["soft_s"] == T.TICK_SOFT_BOUND_S and seen["hard_s"] == T.TICK_HARD_BOUND_S


# --- the executor_of router ---------------------------------------------------------------------------------------

def _router(store, kind, *, launcher=None, dream=None, built=None):
    def pinned(_store, _oid):
        return ("name", kind, "")
    def launcher_of():
        if built is not None:
            built.append(1)
        if isinstance(launcher, BaseException):
            raise launcher
        return launcher
    return T.executor_of_factory(store, launcher_of=launcher_of, dream_executor_of=dream, pinned_of=pinned)


def _row(**over):
    return {"occurrence_id": "oc-1", "agent_id": "ag-1", "session_key": "s-0000000000000001", "pid": 5, **over}


def test_router_session_rows_get_a_row_session_executor(rig):  # noqa: F811
    got = _router(rig.store, "session", launcher=SimpleNamespace())(_row())
    assert type(got) is S.RowSessionExecutor and got.session_key == "s-0000000000000001"


def test_router_dream_rows_get_the_dreams_executor(rig):  # noqa: F811
    dream = SimpleNamespace(tag="dream-executor")
    asked = []
    got = _router(rig.store, "dream", dream=lambda row: (asked.append(row["occurrence_id"]), dream)[1])(_row(session_key=None))
    assert got is dream and asked == ["oc-1"]


@pytest.mark.parametrize("kind", [None, "spawn", "model", ""], ids=["unreadable", "spawn", "model", "empty"])
def test_router_anything_else_is_none(rig, kind):  # noqa: F811
    built = []
    assert _router(rig.store, kind, launcher=SimpleNamespace(), dream=lambda r: 1 / 0, built=built)(_row()) is None
    assert built == []                      # no launcher is built for a row that is not a session's


def test_router_session_without_a_key_or_a_launcher_is_none(rig):  # noqa: F811
    assert _router(rig.store, "session", launcher=SimpleNamespace())(_row(session_key=None)) is None
    assert _router(rig.store, "session", launcher=None)(_row()) is None
    assert _router(rig.store, "session", launcher=RuntimeError("not registered"))(_row()) is None


def test_router_a_dream_builder_that_raises_is_none(rig):  # noqa: F811
    def boom(row):
        raise ValueError("install refused")
    assert _router(rig.store, "dream", dream=boom)(_row()) is None


def test_router_builds_the_launcher_once_per_driver(rig):  # noqa: F811
    built = []
    of = _router(rig.store, "session", launcher=SimpleNamespace(), built=built)
    of(_row()), of(_row(occurrence_id="oc-2"))
    assert built == [1]


def test_router_reads_the_signed_role_of_a_real_row(rig):  # noqa: F811
    oid, key = _session_row(rig)
    row = {"occurrence_id": oid, "agent_id": rig.row(oid)["agent_id"], "session_key": key, "pid": 4100}
    of = T.executor_of_factory(rig.store, launcher_of=lambda: SimpleNamespace())
    got = of(row)
    assert type(got) is S.RowSessionExecutor and got.occurrence_id == oid


# --- binding_of ---------------------------------------------------------------------------------------------------

def test_binding_of_is_the_live_links_binding_and_none_once_revoked(rig):  # noqa: F811
    rig.link()
    of = T.binding_of_factory(rig.store, rig.authority, rig.now)
    assert of(rig.agent, rig.spec_hash) == BINDING
    assert of(rig.agent, "sha256:other") is None
    rig.world.link(__import__("tests.test_authority_agent_link", fromlist=["x"]).revoke_agent(rig.agent, rig.sid), "laptop")
    fresh = T.binding_of_factory(rig.store, rig.authority, rig.now)
    assert fresh(rig.agent, rig.spec_hash) is None


def test_binding_of_arms_nothing_when_the_log_cannot_be_replayed(rig):  # noqa: F811
    class Broken:
        def refresh(self):
            raise OSError("cockpit db missing")
    assert T.binding_of_factory(rig.store, Broken(), rig.now)(rig.agent, rig.spec_hash) is None


# --- a dead-fire session row, settled end to end -----------------------------------------------------------------

class FakeSessionClient:
    """The interface S10a's `DispatchSessionLauncher` presents to the reaper (S10 DESIGN 3.2), as a fake: `usage(key)`
    (never raises) and `end_session(key, deadline_s) -> StopResult`, `StopResult(True, ...)` only for a proven END.
    The real client is exercised by `test_the_real_client_variant` once S10a has landed."""

    def __init__(self):
        self.ended: list = []

    def usage(self, session_key):
        return Usage(0, 0, 0, "fake")

    def end_session(self, session_key, deadline_s):
        self.ended.append((session_key, deadline_s))
        return StopResult(True, "fake server proved END")


def test_a_dead_fire_session_row_is_settled_by_one_tick(rig):  # noqa: F811
    oid, key = _session_row(rig, pid=DEAD_PID)
    _revoke(rig, oid)
    client = FakeSessionClient()
    clock = Clock(rig)
    clock.advance(60)
    code = T.run_tick(rig.store, clock=clock, authority=rig.authority, launcher_of=lambda: client)
    assert code == T.EXIT_OK
    assert client.ended and client.ended[0][0] == key          # the reaper asked the client to END that session
    assert rig.row(oid)["state"] != "launched"                 # and settled the row on the proof


def test_a_live_fire_is_not_ended_by_the_tick(rig):  # noqa: F811
    """The router and the driver add no stop rule: a row whose fire is alive is left to session.py's rules (S11b)."""
    import json
    from levain.consent.delivery import process_identity
    oid, key = _session_row(rig)
    fire = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", oid])   # a live "fire" naming the row
    try:
        started, args = process_identity(fire.pid)
        for _ in range(50):             # a venv python re-execs at start: wait for the argv to settle
            time.sleep(0.1)
            again = process_identity(fire.pid)
            if again == (started, args):
                break
            started, args = again
        ctl.fire_record_path(rig.store.directory, oid).write_text(json.dumps(
            {"occurrence_id": oid, "pid": fire.pid, "started": started, "args": args}))
        _revoke(rig, oid)
        client = FakeSessionClient()
        clock = Clock(rig)
        clock.advance(60)
        T.run_tick(rig.store, clock=clock, authority=rig.authority, launcher_of=lambda: client)
        assert client.ended == []
    finally:
        fire.kill()
        fire.wait()


def test_the_real_client_variant(rig):  # noqa: F811
    """INTEGRATION (S10a/S11, the head's after both land): the same dead-fire settle with the real
    `levain.agents.session_client` talking to an in-process dispatch server. Until S10a lands this skips; once it has,
    the head replaces the body with the end-to-end drive (the fake above names the interface it must satisfy)."""
    client = pytest.importorskip("levain.agents.session_client")
    assert callable(getattr(client.DispatchSessionLauncher, "end_session", None))
    pytest.skip("S10a/S11 integration: drive run_tick through the real client + in-process server (head, after both land)")


def test_a_reaper_that_raised_is_not_a_clean_exit(rig, monkeypatch, capsys):  # noqa: F811
    monkeypatch.setattr(supervise.Supervisor, "reap", lambda self, *a, **k: 1 / 0)
    code = T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None)
    assert code == T.EXIT_REAPER_ERROR and "not clean" in capsys.readouterr().err


def test_install_files_naming_two_cockpits_are_refused(rig, tmp_path):  # noqa: F811
    from levain.agents import session_wire as W
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    store = rig.store
    W.write_install(store.directory, cockpit_root=a)
    from levain.dream import agent as DA
    (store.directory / DA.INSTALL_FILE).write_text(__import__("json").dumps(
        {"schema": DA.INSTALL_SCHEMA, "zone": "UTC", "cockpit_root": str(b), "coord_dir": str(b), "store_path": str(b / "m.db")}))
    (b / "m.db").write_text("")
    with pytest.raises(W.InstallRefused, match="will not guess"):
        T._cockpit_root(store)
