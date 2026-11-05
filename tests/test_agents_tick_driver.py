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
    # the worst gap between two renewals of one row is one interval, a tick that ran to its hard bound, and the next
    # tick's phase before its renewals (L3 r1 codex: setup precedes the reaper, so it is in the sum)
    assert T.WORST_RENEW_GAP_S == T.TICK_INTERVAL_S + T.TICK_HARD_BOUND_S + T.TICK_PRE_RENEW_BOUND_S == 720
    assert T.WORST_RENEW_GAP_S < ctl.LEASE_S == supervise.LEASE_S == 900


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


# --- L3 r1 fix round -----------------------------------------------------------------------------------------------

class _UnprovenStop:
    """An executor whose stop uses its whole deadline (the clock moves on by it) and never proves the end."""

    def __init__(self, oid, clock, asked):
        self.oid, self.clock, self.asked = oid, clock, asked

    def stop(self, deadline_s):
        self.asked.append(self.oid)
        self.clock.advance(deadline_s)
        return StopResult(False, "the end was not proven")

    def usage(self):
        return Usage(0, 0, 0, "fake")


def test_stale_rows_are_not_starved_by_a_fresh_supervisor_each_tick(rig, monkeypatch):  # noqa: F811
    """codex HIGH :254: one process per tick loses the supervisor's deferred-first memory. With 100 rows to stop, each
    stop using its 10 s deadline, one pass asks 45 (STOP_BUDGET_S / STOP_DEADLINE_S); every row must still be asked
    within a bounded number of ticks, not the same oldest 45 forever."""
    n = 100
    rows = [{"occurrence_id": f"oc-{i:03d}", "agent_id": "ag-x", "run_id": None, "state": "launched", "epoch": 1,
             "launch_epoch": 1, "link_id": None, "link_incarnation": None, "spec_hash": "sha256:none",
             "binding_id": BINDING, "launched_at": f"2026-11-04T00:{i // 60:02d}:{i % 60:02d}Z",
             "session_key": f"s-{i:016x}", "pid": None} for i in range(n)]
    clock = Clock(rig)
    asked: list = []
    monkeypatch.setattr(supervise.Supervisor, "_launched", lambda self: [dict(r) for r in rows])
    monkeypatch.setattr(supervise.Supervisor, "_renew", lambda self, row, grace: True)
    monkeypatch.setattr(supervise.Supervisor, "_past_stop_at", lambda self, row, now: True)
    monkeypatch.setattr(supervise.Supervisor, "refresh_once", lambda self: False)
    monkeypatch.setattr(T, "executor_of_factory",
                        lambda store, **kw: (lambda row: _UnprovenStop(row["occurrence_id"], clock, asked)))
    ever: set = set()
    for _tick in range(n):
        asked.clear()
        T.run_tick(rig.store, clock=clock, authority=rig.authority, launcher_of=lambda: None)
        assert len(asked) == supervise.STOP_BUDGET_S // supervise.STOP_DEADLINE_S
        ever.update(asked)
        clock.advance(T.TICK_INTERVAL_S)
    assert ever == {r["occurrence_id"] for r in rows}


def test_the_renewals_run_before_the_coordinator_directory_is_read(rig, monkeypatch):  # noqa: F811
    """codex HIGH :248 / deepseek HIGH 1, 2, MED 4: nothing the renewals do not need (the dream install's coordinator
    directory, dispatch.db) runs before them."""
    order: list = []
    real = supervise.Supervisor.reap
    monkeypatch.setattr(supervise.Supervisor, "reap", lambda self, *a, **k: (order.append("reap"), real(self, *a, **k))[1])
    monkeypatch.setattr(T, "_install_coord_dir", lambda st, notes: (order.append("coord"), None)[1])
    assert T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None) == T.EXIT_OK
    assert order[0] == "reap"


def test_a_late_renewal_phase_is_named(rig, capsys):  # noqa: F811
    """codex HIGH :248's constructed sequence: the next tick reaches its renewals 301 s in. The renewals still run, and
    the tick says the gap bound was overrun instead of passing silently."""
    from datetime import timedelta
    base = rig.now()
    calls = []

    def clock():
        calls.append(1)
        return base if len(calls) == 1 else base + timedelta(seconds=301)

    assert T.run_tick(rig.store, clock=clock, authority=rig.authority, launcher_of=lambda: None) == T.EXIT_OK
    err = capsys.readouterr().err
    assert "renewals began 301s into the tick" in err and "may have lapsed" in err


def test_an_install_naming_no_cockpit_still_renews_and_claims_nothing(rig, monkeypatch, capsys):  # noqa: F811
    """deepseek HIGH 1: a broken or absent install file stops the claims, never the renewals."""
    oid, _key = _session_row(rig)
    renewed = []
    real = supervise.Supervisor._renew
    monkeypatch.setattr(supervise.Supervisor, "_renew",
                        lambda self, row, grace: (renewed.append(row["occurrence_id"]), real(self, row, grace))[1])
    claimed = []
    monkeypatch.setattr(ctl, "tick", lambda *a, **k: claimed.append(1))
    clock = Clock(rig)
    clock.advance(60)
    code = T.run_tick(rig.store, clock=clock, launcher_of=lambda: FakeSessionClient())   # no install files: refused
    assert code == T.EXIT_REFUSED
    assert renewed == [oid] and claimed == []
    assert "no window was claimed" in capsys.readouterr().err


@pytest.mark.parametrize("launcher", ["not-registered", None], ids=["names-an-unregistered-one", "names-none"])
def test_a_session_row_with_no_registered_launcher_is_loud(rig, capsys, launcher):  # noqa: F811
    """complement MED :107/:119: through the real read_install + resolve_launcher (no injection). Nothing registers a
    launcher in the tick process yet (S10b), so a session row is renewed but can never be ENDed: never a clean exit."""
    from levain.agents import session_wire as W
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher=launcher)
    oid, _key = _session_row(rig)
    clock = Clock(rig)
    clock.advance(60)
    code = T.run_tick(rig.store, clock=clock, authority=rig.authority)
    assert code == T.EXIT_NO_LAUNCHER
    err = capsys.readouterr().err
    assert "no session launcher" in err and oid in err and "S10b" in err


def test_binding_of_does_not_arm_a_link_whose_signer_was_revoked(rig):  # noqa: F811
    """codex MED :173: a link signed by a second laptop key that is later revoked arms nothing (launch would refuse it
    signer_revoked after the window was claimed and its cursor moved)."""
    from tests.test_consent_queue import Key
    import levain.authority as auth
    second = Key(rig.world.tmp / "keys", "laptop2")
    rig.world.link({"op": "enrol", "fingerprint": second.fp, "public_key": second.public_key,
                    "class": "laptop-presence", "custody": "passphrase"})
    rig.world.keys["laptop2"] = (second.fp, lambda m: second.sign(m, auth.NAMESPACE))
    rig.link(signer="laptop2")
    assert T.binding_of_factory(rig.store, rig.authority, rig.now)(rig.agent, rig.spec_hash) == BINDING
    rig.world.link({"op": "revoke_key", "fingerprint": rig.world.fp("laptop2")})
    assert T.binding_of_factory(rig.store, rig.authority, rig.now)(rig.agent, rig.spec_hash) is None


def test_binding_of_arms_nothing_when_the_cover_moves(rig):  # noqa: F811
    rig.link()

    class Moving:
        def refresh(self):
            return rig.authority.refresh()

        def peek(self, *a, **k):
            raise __import__("levain.agent_authority", fromlist=["x"]).AgentCoverMoved("the log head moved")

    assert T.binding_of_factory(rig.store, Moving(), rig.now)(rig.agent, rig.spec_hash) is None


def test_expired_launch_slots_with_no_coordinator_directory_are_named(rig, capsys):  # noqa: F811
    """codex MED :257: a session-only store, no --coord-dir, no dream install: the expired launch slot is kept and the
    tick says why, instead of passing no probes silently."""
    from levain.agents import slots
    with rig.store.write() as c:
        c.execute("INSERT INTO agent_slots (slot, owner_kind, owner_id, taken_at, expires_at) VALUES (1, ?, 'ln-1', ?, ?)",
                  (slots.LAUNCH, "2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"))
    assert T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None) == T.EXIT_OK
    assert "no coordinator directory" in capsys.readouterr().err
    with rig.store.read() as c:
        assert c.execute("SELECT owner_id FROM agent_slots").fetchall() == [("ln-1",)]


def test_the_default_store_finds_the_default_coordinator_queue(rig, monkeypatch, tmp_path):  # noqa: F811
    from levain.autonomic import db
    from levain.consent import queue
    q = tmp_path / "consent"
    q.mkdir()
    monkeypatch.setattr(db, "default_store_dir", lambda: rig.store.directory.resolve())
    monkeypatch.setattr(queue, "default_queue_dir", lambda: q)
    assert T._coord_dir_for(rig.store, None) is None          # no dispatch.db there yet
    (q / "dispatch.db").write_bytes(b"")
    assert T._coord_dir_for(rig.store, None) == q
    assert T._coord_dir_for(rig.store, tmp_path / "given") == tmp_path / "given"


# --- L3 r2 fix round -----------------------------------------------------------------------------------------------

def test_a_refused_install_does_not_renew_a_row_it_cannot_stop(rig, monkeypatch, capsys):  # noqa: F811
    """codex HIGH :415 (r2): with no install files the production launcher cannot be built (it reads the same refused
    install), so the session row could be renewed every tick and never stopped. Such a row is NOT renewed: its lease
    lapses to takeover, as it did when the tick refused before reaping, and the tick names it."""
    oid, _key = _session_row(rig)
    renewed = []
    real = supervise.Supervisor._renew
    monkeypatch.setattr(supervise.Supervisor, "_renew",
                        lambda self, row, grace: (renewed.append(row["occurrence_id"]), real(self, row, grace))[1])
    clock = Clock(rig)
    clock.advance(60)
    code = T.run_tick(rig.store, clock=clock)          # production launcher_of and authority; no install files
    assert code == T.EXIT_REFUSED
    assert renewed == []
    err = capsys.readouterr().err
    assert "not renewed" in err and oid in err and "lapse" in err
    assert "can be renewed but never" not in err      # exit 4's sentence does not describe a row that was not renewed


def test_the_soft_bound_still_names_unlaunchable_rows(rig, monkeypatch, capsys):  # noqa: F811
    """deepseek MED 2 / complement C (r2): a pass the graceful bound ended after its reaper met session rows with no
    launcher still names them (exit 3 is kept: the pass did not finish)."""
    from levain.agents import session_wire as W
    W.write_install(rig.store.directory, cockpit_root=rig.world.root, launcher=None)
    oid, _key = _session_row(rig)

    def slow_tick(st, clock, binding_of, session_alive, launch_over, reaper):
        reaper()
        time.sleep(10)
    monkeypatch.setattr(ctl, "tick", slow_tick)
    clock = Clock(rig)
    clock.advance(60)
    code = T.run_tick(rig.store, clock=clock, authority=rig.authority, soft_s=3.0, hard_s=9.0)
    assert code == T.EXIT_SOFT_TIMEOUT
    err = capsys.readouterr().err
    assert "graceful bound" in err and "no session launcher" in err and oid in err


def test_the_soft_bound_is_not_swallowed_by_the_refused_branch(rig, monkeypatch):  # noqa: F811
    """complement MED (r2): the refused branch wraps the reaper in `except Exception`; TurnTimeout derives from
    BaseException, so the graceful bound passes through it (exit 3, never a reaper_error exit 2)."""
    from levain.firing.deadline import TurnTimeout
    assert issubclass(TurnTimeout, BaseException) and not issubclass(TurnTimeout, Exception)

    def timed_out(self, *a, **k):
        raise TurnTimeout(T.TICK_SOFT_BOUND_S)
    monkeypatch.setattr(supervise.Supervisor, "reap", timed_out)
    assert T.run_tick(rig.store, clock=rig.now, launcher_of=lambda: FakeSessionClient()) == T.EXIT_SOFT_TIMEOUT


def test_an_unreadable_dream_install_is_named_not_read_as_absent(rig, capsys):  # noqa: F811
    """deepseek MED 3 (r2): a dream install that exists but cannot be read is named as such when the probes need it,
    instead of the tick saying only "no coordinator directory"."""
    from levain.agents import slots
    from levain.dream import agent as DA
    (rig.store.directory / DA.INSTALL_FILE).write_text("{not json")
    with rig.store.write() as c:
        c.execute("INSERT INTO agent_slots (slot, owner_kind, owner_id, taken_at, expires_at) VALUES (1, ?, 'ln-1', ?, ?)",
                  (slots.LAUNCH, "2026-01-01T00:00:00Z", "2026-01-01T00:15:00Z"))
    assert T.run_tick(rig.store, clock=rig.now, authority=rig.authority, launcher_of=lambda: None) == T.EXIT_OK
    err = capsys.readouterr().err
    assert "dream install" in err and "cannot be read" in err
    with rig.store.read() as c:
        assert c.execute("SELECT owner_id FROM agent_slots").fetchall() == [("ln-1",)]


def test_the_real_reap_stops_rows_in_the_rotated_order(rig, monkeypatch):  # noqa: F811
    """UNSETTLED (i), r2: Supervisor.reap's stop loop is a STABLE sort on (deferred, withheld); with neither (a fresh
    supervisor, no proven-dead fire) it keeps the order `_RotatingSupervisor._launched` returns. Driven through the real
    reap, a fake executor recording the order."""
    n = 5
    rows = [{"occurrence_id": f"oc-{i:03d}", "agent_id": "ag-x", "run_id": None, "state": "launched", "epoch": 1,
             "launch_epoch": 1, "link_id": None, "link_incarnation": None, "spec_hash": "sha256:none",
             "binding_id": BINDING, "launched_at": f"2026-11-04T00:00:{i:02d}Z", "session_key": f"s-{i:016x}",
             "pid": None} for i in range(n)]
    clock = Clock(rig)
    asked: list = []
    monkeypatch.setattr(supervise.Supervisor, "_launched", lambda self: [dict(r) for r in rows])
    monkeypatch.setattr(supervise.Supervisor, "_renew", lambda self, row, grace: True)
    monkeypatch.setattr(supervise.Supervisor, "_past_stop_at", lambda self, row, now: True)
    monkeypatch.setattr(supervise.Supervisor, "refresh_once", lambda self: False)
    monkeypatch.setattr(T, "executor_of_factory",
                        lambda store, **kw: (lambda row: _UnprovenStop(row["occurrence_id"], clock, asked)))
    for _tick in range(n):
        asked.clear()
        k = (int(clock().timestamp()) // T.TICK_INTERVAL_S) % n
        T.run_tick(rig.store, clock=clock, authority=rig.authority, launcher_of=lambda: None)
        ids = [r["occurrence_id"] for r in rows]
        assert asked == ids[k:] + ids[:k]
        clock.advance(T.TICK_INTERVAL_S)


def test_an_expired_newest_link_does_not_disarm_a_live_older_one(rig):  # noqa: F811
    """UNSETTLED (iii), r2: `AgentAuthority.peek` answers (False, incarnation) for a link that is merely not live
    (expired); it raises only when the cover moved. So an expired newest link is skipped and the older live one arms."""
    from datetime import timedelta
    from tests.test_authority_agent_link import link as link_record
    rig.link()                                                     # older, 29 days
    rig.n += 1
    rig.world.link({"op": "create_grant", "grant": link_record(
        rig.world, rig.n, agent=rig.agent, store=rig.sid, spec_hash=rig.spec_hash,
        binding_id="bind-fedcba9876543210", days=1)}, "laptop")    # newest, 1 day
    assert T.binding_of_factory(rig.store, rig.authority, rig.now)(rig.agent, rig.spec_hash) == "bind-fedcba9876543210"
    later = lambda: rig.now(timedelta(days=2))  # noqa: E731
    assert T.binding_of_factory(rig.store, rig.authority, later)(rig.agent, rig.spec_hash) == BINDING
