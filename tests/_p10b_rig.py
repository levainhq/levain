"""The P10b rig (levain#215): P7's cases for a SESSION agent, run through the ``levain`` CLI as a subprocess with a stub launcher.

It reuses ``tests/_p10a_rig.py`` by import (the tmp install, the real ssh-keygen authority world, ``sign``, the file-level
reads) and changes only what a session agent needs: the entry point (``_p10b_entry.py``: the stub launcher registered in every
levain process the case starts, the fire child included), the agent (a registered session role instead of the dream), and the
fire (``controller.spawn_fire`` itself, over the entry's argv, so a case runs the one door to a fire with the product's own
detached, session-leading child).

What a case sets up in-process, never the thing it asserts about: the install file (``session_wire.write_install``), the
claim of a MANUAL row (``levain agents`` has no hand start) and its LAUNCH (``controller.launch``). A scheduled row is
claimed and launched by the CLI's own tick (levain#219), whose fire spawn the spawn gate records instead of starting.
Everything else is a subprocess of the CLI or of the fire: the zone, the role, the registration, the signature, the tick,
the revoke, the fire and the reads of ``agents show --json``.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from tests._p10a_rig import Install, Ran, real_now

ENTRY = Path(__file__).resolve().parent / "_p10b_entry.py"
NAME = "p10b-agent"
ROLE = {"name": "p10b-role", "brief": {"template": "run", "slots": {}}, "ceiling": {"lands": ["local data"]},
        "model": "sonnet", "harness": "claude", "executor": "session"}
_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "agents_p2"
CAPS = {"harness": "claude", "model": ROLE["model"], "where": [], "lands": [], "pushes": [], "publishes": []}
BINDING = "bind-0123456789abcdef"      # the link's own binding id (``link_doc``)
EXPOSURE_USD_MICROS = 100_000        # what the stub's executor states at the launch (the api_key route's reserved dollars)


def _spec(zone: str) -> dict:
    from levain.agents.role import role_hash
    from levain.agents.session_wire import SESSION_WIN_CHECK

    doc = json.loads((_FIXTURES / "specs.json").read_text(encoding="utf-8"))["job_feeder_downstream"]
    doc["name"] = NAME
    doc["role"] = {"name": ROLE["name"], "role_hash": role_hash(ROLE)}
    doc["trigger"] = {"kind": "schedule", "zone": zone, "cron": "5 * * * *"}
    doc["win"] = {"kind": "verifier", "check": SESSION_WIN_CHECK}
    return doc


class NoExposure:
    """An executor that cannot state its exposure: the launch refuses it ``no_exposure`` before anything is taken."""

    max_exposure_usd_micros = None
    action_exposure = None

    def usage(self):
        from levain.agents.accounting import Usage

        return Usage(None, None, None, "p10b:unreadable")

    def stop(self, deadline_s):
        raise AssertionError("an executor that was refused is never stopped")


class Session(Install):
    """A tmp install with ONE registered session agent (``NAME``), its launcher the stub of ``_p10b_entry.py``."""

    def __init__(self, tmp: Path, *, zone: str = "UTC") -> None:
        super().__init__(tmp, episodes=0, zone=zone)
        self.stubdir = tmp / "p10b-stub"
        self.stubdir.mkdir()
        self.episodic = tmp / "episodes.db"
        self._claims = 0
        self._agent: dict | None = None
        self._fires: list[subprocess.Popen] = []

    # ------------------------------------------------------------------------------------------------- the CLI
    def env(self, now: str | None = None, spawn_gate: bool = False, kill: str | None = None) -> dict[str, str]:
        env = super().env(now, spawn_gate, kill)
        env["P10B_STUB_DIR"] = str(self.stubdir)
        return env

    def cli(self, *argv: str, now: str | None = None, spawn_gate: bool = True, kill: str | None = None,
            timeout: int = 180) -> Ran:
        """``levain <argv>`` as a subprocess behind ``_p10b_entry.py`` (the stub launcher registered; ``now`` and the
        other P10a gates as named). ``spawn_gate`` (on unless a caller turns it off) is P10a's ``P10A_NO_SPAWN``: the
        process's ``controller.spawn_fire`` records the occurrence in ``spawns.txt`` (``spawned()``) and starts nothing,
        because a fire started the product's way cannot reach the stub; a case fires a recorded row itself (``fire``)."""
        p = subprocess.run([sys.executable, str(ENTRY), *argv], cwd=self.tmp, env=self.env(now, spawn_gate, kill),
                           capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL)
        return Ran(p.returncode, p.stdout, p.stderr)

    # -------------------------------------------------------------------------------------------------- set up
    def register(self, *, episodic: bool = True, signed: bool = True, run_ceiling: int = 3, **over) -> None:
        """The zone, the role and the spec through the CLI, the install file in-process, then (``signed``) ``agents sign``."""
        from anneal_memory import Store
        from levain.agents import session_wire
        from tests._p10b_entry import STUB_LAUNCHER

        assert self.cli("agents", "zone", self.zone).rc == 0
        role = self.tmp / "role.json"
        role.write_text(json.dumps(ROLE), encoding="utf-8")
        added = self.cli("agents", "role", "add", str(role))
        assert added.rc == 0, added.text
        spec = self.tmp / "spec.json"
        spec.write_text(json.dumps(_spec(self.zone)), encoding="utf-8")
        registered = self.cli("agents", "register", "--from", str(spec))
        assert registered.rc == 0, registered.text
        if episodic:
            Store(self.episodic, audit=False).close()
        session_wire.write_install(self.store_dir, cockpit_root=self.world.root, launcher=STUB_LAUNCHER,
                                   episodic_path=self.episodic if episodic else None)
        if signed:
            signed_out = self.sign(self.link_doc(run_ceiling=run_ceiling, **over))
            assert signed_out.rc == 0, signed_out.text

    def shown(self) -> dict:
        got = self.cli("agents", "show", NAME, "--json")
        assert got.rc == 0, got.text
        return json.loads(got.out)

    def agent(self) -> dict:
        """The registered agent (``agent_id``, ``spec_hash``, ...): read once, from the first ``show`` after the registration,
        since neither moves while a case runs. ``shown`` reads afresh."""
        if self._agent is None:
            self._agent = self.shown()["agent"]
        return self._agent

    def occurrences(self) -> list[dict]:
        return self.shown()["occurrences"]

    def link_doc(self, *, run_ceiling: int = 3, **over) -> dict:
        agent = self.agent()
        doc = {"agent_id": agent["agent_id"], "store_id": self.store_id(), "spec_hash": agent["spec_hash"],
               "role_hash": _spec(self.zone)["role"]["role_hash"], "binding_id": BINDING,
               "capabilities": dict(CAPS), "budget_run_usd_micros": 20_000_000, "budget_window_usd_micros": 40_000_000,
               "run_ceiling": run_ceiling, "guard": []}
        doc.update(over)
        return doc

    # ------------------------------------------------------------------------------------------- claim and launch
    def claim(self, *, now: datetime | None = None) -> tuple[str, str]:
        """A MANUAL occurrence claimed in-process under the link's binding (``levain agents`` has no hand start):
        ``(occurrence_id, the claim's outcome)``."""
        from levain.agents import controller as ctl
        from levain.agents.store import open_agent_store

        now = now or real_now()
        agent = self.agent()
        self._claims += 1
        with open_agent_store(self.store_dir).write() as conn:
            return ctl.claim(conn, agent_id=agent["agent_id"], kind="manual", key=f"hand-{self._claims}",
                             spec_hash=agent["spec_hash"], binding_id=BINDING,
                             window_key=f"m:{now.date().isoformat()}", now=now)

    def claimed(self, *, now: datetime | None = None) -> str:
        """A manual occurrence claimed (asserted ``claimed``): its id."""
        oid, outcome = self.claim(now=now)
        assert outcome == "claimed", outcome
        return oid

    def launch(self, oid: str, *, executor=None, now: datetime | None = None):
        """``controller.launch`` of an admitted row (the module docstring says why this is in-process), with the real
        ``AgentAuthority`` over the install's cockpit and the window the row counts over: for a scheduled row the span its
        window key names in the trigger's zone (``schedule.window_span``), for a manual one the day around ``now``. The
        executor is the session's own, stating ``EXPOSURE_USD_MICROS`` unless a case passes another."""
        from levain.agent_authority import AgentAuthority
        from levain.agents import controller as ctl
        from levain.agents import schedule
        from levain.agents.session import SessionExecutor
        from levain.agents.store import open_agent_store

        now = now or real_now()
        row = self.row(oid)
        if row["kind"] == "scheduled":
            lo, hi = schedule.window_span(self.shown()["spec"]["trigger"], self.zone, row["window_key"])
        else:
            lo, hi = now - timedelta(days=1), now + timedelta(days=1)
        return ctl.launch(open_agent_store(self.store_dir), oid, authority=AgentAuthority(self.world.root),
                          window=ctl.Window(ctl._stamp(lo), ctl._stamp(hi), None), now=now, on_input=None,
                          executor=(executor if executor is not None
                                    else SessionExecutor(None, max_exposure_usd_micros=EXPOSURE_USD_MICROS)))

    def launched(self) -> str:
        """A manual occurrence claimed and launched (asserted ``launched``): its id."""
        oid = self.claimed()
        res = self.launch(oid)
        assert res.outcome == "launched", res
        return oid

    # --------------------------------------------------------------------------------------------------- the fire
    def fire(self, oid: str) -> subprocess.Popen:
        """The occurrence's fire started through ``controller.spawn_fire``, the one door to a fire: a detached process leading
        its own session, here over ``_p10b_entry.py _fire`` (the stub registered) and the rig's scrubbed environment."""
        from levain.agents import controller as ctl

        argv = [sys.executable, str(ENTRY), "_fire", "--store", str(self.store_dir), oid]
        proc = ctl.spawn_fire(self.store_dir, oid, argv=argv, env=self.env())
        self._fires.append(proc)
        return proc

    def close(self) -> None:
        """The case's teardown, pass or fail: drop ``hold`` (a vendor still waiting on it ends) and kill, as its own group,
        every fire this session started that still runs. A fire's gate holds the vendor's lifeline, so the vendor goes with
        it."""
        self.release()
        for proc in self._fires:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):   # the group is gone or going (macOS: EPERM for unreaped zombies)
                    pass
            proc.wait(30)

    def fired(self, oid: str, timeout: float = 120) -> int:
        """Fire ``oid`` and wait for its exit status."""
        proc = self.fire(oid)
        try:
            return proc.wait(timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise

    def fire_log(self, oid: str) -> str:
        path = self.store_dir / "fires" / f"{oid}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""

    # ------------------------------------------------------------------------------------------- the stub's side
    def hold(self) -> None:
        (self.stubdir / "hold").write_text("", encoding="utf-8")

    def release(self) -> None:
        (self.stubdir / "hold").unlink(missing_ok=True)

    def vendor_exit(self, code: int) -> None:
        (self.stubdir / "exit").write_text(str(code), encoding="utf-8")

    def usage_unreadable(self) -> None:
        (self.stubdir / "usage").write_text("unreadable", encoding="utf-8")

    def started(self) -> int:
        try:
            return int((self.stubdir / "calls").read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def start_records(self) -> list[dict]:
        return [json.loads((self.stubdir / f"launch-{n}.json").read_text(encoding="utf-8"))
                for n in range(1, self.started() + 1)]

    # --------------------------------------------------------------------------------------------------- the reads
    def row(self, oid: str) -> dict:
        (got,) = self.rows(f"SELECT * FROM agent_occurrences WHERE occurrence_id = '{oid}'")
        return got

    def reservation(self, oid: str) -> dict:
        """The occurrence's one reservation row (``state``, ``settled_reason``, ``reserved_usd_micros``, ``debited_usd_micros``)."""
        (got,) = self.rows(f"SELECT * FROM agent_reservations WHERE occurrence_id = '{oid}'")
        return got

    def held(self, oid: str) -> tuple[list, list]:
        """``(slots held, reservations)`` of the occurrence: what a run still holds, as ``(state,)`` rows."""
        return (self.rows(f"SELECT * FROM agent_slots WHERE owner_id = '{oid}'"),
                [(r["state"],) for r in self.rows(f"SELECT state FROM agent_reservations WHERE occurrence_id = '{oid}'")])


def until(what: str, check, timeout: float = 60.0, step: float = 0.05):
    """Poll ``check()`` until it answers truthy, and return the answer; a case waits on what it observes, never a guess."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = check()
        if got:
            return got
        time.sleep(step)
    raise AssertionError(f"timed out waiting for {what}")

