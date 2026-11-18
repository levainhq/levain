"""The P10a rig (levain#121): a tmp install the ``levain`` CLI is run against as a SUBPROCESS.

DESIGN invariant 7's cases are run end to end through the command line an operator types: argv in, exit code and text
out, the stores on disk in between. This module is only the install those commands run against and the hands that run
them. It reaches no real key and no real ``~/.levain``: ``LEVAIN_HOME`` and ``HOME`` are tmp, the child's environment
carries no provider variable, and the one door to a model-calling child is closed by the gate in ``_p10a_entry.py``.

What is set up in-process (a setup, never the thing a case asserts about): the cockpit's authority log with an enrolled
laptop-presence key (``World`` of the graduation tests: real ssh-keygen keys, links verified through the real replay),
the anneal store with episodes, and the dream's first-run facts file, which has no CLI flag by design (it is the
operator's own file). Everything else, enabling the dream, signing a link, starting a run, ticking, revoking, is a
subprocess of the CLI.

Two gates, both named by the case that uses them (see ``_p10a_entry.py``): ``now=`` (the clock the tick and the hand
start read) and the spawn gate (on by default: a launched row stays launched with no fire).
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENTRY = Path(__file__).resolve().parent / "_p10a_entry.py"
MODEL = "claude-opus-5-5"
ZONE = "America/New_York"
_KEEP = ("PATH", "LANG", "LC_ALL", "TMPDIR")      # nothing else crosses: no provider key, no host config


@dataclass(frozen=True)
class Ran:
    rc: int
    out: str
    err: str

    @property
    def text(self) -> str:
        return self.out + self.err


def levain_bin() -> Path | None:
    """The ``levain`` console script of the interpreter running the tests (the installed shape), if it has one."""
    cand = Path(sys.executable).parent / "levain"
    return cand if cand.is_file() else None


class Install:
    def __init__(self, tmp: Path, *, episodes: int = 3, stub: bool = False) -> None:
        from tests.test_authority_graduation_kind import World
        from levain.cockpit.firejournal import CockpitDB

        self.tmp = tmp
        self.episodes = episodes
        self.world = World(tmp)                      # tmp/keys, tmp/cockpit (the authority log)
        CockpitDB(self.world.root).set_install_class("reported")
        self.home = tmp / "home"
        self.uhome = tmp / "uhome"
        self.anneal = tmp / "anneal"
        self.desk = tmp / "desk"
        self.spawns = tmp / "spawns.txt"
        for d in (self.home, self.uhome):
            d.mkdir()
        self.desk.mkdir(mode=0o700)                  # the desk queue refuses a directory open to group or others
        self.desk.chmod(0o700)
        self.key = tmp / "keys" / "laptop"
        self.store_dir = self.home / "autonomic"
        self._n = 0
        self.stub = None
        if stub:                                     # the install fact claude_bin names a stub program (_p10a_stub.py)
            from tests._p10a_stub import StubClaude

            self.stub = StubClaude(tmp / "stubclaude")

    # ------------------------------------------------------------------------------------------------- the CLI
    def env(self, now: str | None = None, spawn_gate: bool = True, kill: str | None = None) -> dict[str, str]:
        env = {k: os.environ[k] for k in _KEEP if k in os.environ}
        if self.stub is not None:                    # no real `claude` can be found by name
            env["PATH"] = "/usr/bin:/bin"
        env.update(LEVAIN_HOME=str(self.home), HOME=str(self.uhome), PYTHONPATH=str(REPO),
                   PYTHONDONTWRITEBYTECODE="1")
        if now:
            env["P10A_NOW"] = now
        if kill:
            env["P10A_KILL"] = kill
        if spawn_gate:
            env["P10A_NO_SPAWN"] = str(self.spawns)
        return env

    def cli(self, *argv: str, now: str | None = None, spawn_gate: bool = True, kill: str | None = None,
            timeout: int = 180) -> Ran:
        """``levain <argv>`` as a subprocess. With no gate it is the console script itself; with a gate it is the
        same ``main`` behind ``_p10a_entry.py``. A killed process has a negative return code (-9: SIGKILL)."""
        gated = now is not None or spawn_gate or kill is not None
        exe = levain_bin()
        cmd = [sys.executable, str(ENTRY), *argv] if gated or exe is None else [str(exe), *argv]
        p = subprocess.run(cmd, cwd=self.tmp, env=self.env(now, spawn_gate, kill), capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL)
        return Ran(p.returncode, p.stdout, p.stderr)

    # -------------------------------------------------------------------------------------------------- set up
    def make_anneal(self) -> None:
        from anneal_memory import FLOW_SCHEMA, Store

        self.anneal.mkdir()
        with Store(str(self.anneal / "memory.db"), section_schema=FLOW_SCHEMA) as s:
            for i in range(self.episodes):
                s.record(content=f"Turn {i}: helped the operator.", episode_type="observation", source="t")

    def enable(self, *extra: str) -> Ran:
        """``levain dream enable`` (the zone first, once): registers the dream and writes the install facts."""
        if not (self.store_dir / "autonomic.db").exists():
            assert self.cli("agents", "zone", ZONE).rc == 0
        if not self.anneal.exists():
            self.make_anneal()
        self._bind_stub()                            # before the enable, so the spec the enable registers binds it
        ran = self.cli("dream", "enable", "--store-dir", str(self.anneal), "--cockpit-root", str(self.world.root),
                       "--coord-dir", str(self.desk), *extra)
        self._write_facts()
        return ran

    def _bind_stub(self) -> None:
        """With a stub: the install fact ``claude_bin`` is written first (``enable`` merges it and keeps it)."""
        if self.stub is None:
            return
        from levain.dream import agent as da

        da.write_install(self.store_dir, store_path=self.anneal / "memory.db", cockpit_root=self.world.root,
                         coord_dir=self.desk, zone=ZONE, claude_bin=str(self.stub.path))

    def _write_facts(self) -> None:
        """The first-run facts file the install names: the operator's own file (no flag writes it)."""
        from levain.dream import agent as da
        from levain.dream import executor as ex

        facts = self.tmp / "facts.json"
        ex.write_first_run_facts(facts, model=MODEL, models_charged=[MODEL], m_usd_micros=ex.one_call_ceiling(MODEL))
        inst = da.read_install(self.store_dir)
        da.write_install(self.store_dir, store_path=inst.store_path, cockpit_root=inst.cockpit_root,
                         coord_dir=inst.coord_dir, zone=inst.zone, facts_path=facts,
                         claude_bin=inst.claude_bin)

    def agent(self) -> dict:
        """``levain agents show dream --json``: the ``agent`` object (agent_id, spec_hash, version, ...)."""
        got = self.cli("agents", "show", "dream", "--json", spawn_gate=False)
        assert got.rc == 0, got.text
        return json.loads(got.out)["agent"]

    def occurrences(self) -> list[dict]:
        got = self.cli("agents", "show", "dream", "--json", spawn_gate=False)
        assert got.rc == 0, got.text
        return json.loads(got.out)["occurrences"]

    def store_id(self) -> str:
        c = sqlite3.connect(self.store_dir / "autonomic.db")
        try:
            return c.execute("SELECT value FROM meta WHERE key = 'store_id'").fetchone()[0]
        finally:
            c.close()

    def rows(self, sql: str = "SELECT * FROM agent_occurrences ORDER BY admitted_at, occurrence_id") -> list[dict]:
        """A file-level read of the agent store (the assertion side): what the CLI wrote."""
        c = sqlite3.connect(self.store_dir / "autonomic.db")
        c.row_factory = sqlite3.Row
        try:
            return [dict(r) for r in c.execute(sql)]
        finally:
            c.close()

    def link_doc(self, *, run_ceiling: int = 3, **over) -> dict:
        agent = self.agent()
        doc = {"agent_id": agent["agent_id"], "store_id": self.store_id(), "spec_hash": agent["spec_hash"],
               "role_hash": json.loads(self.cli("agents", "show", "dream", "--json", spawn_gate=False).out)
               ["spec"]["role"]["role_hash"],
               "binding_id": "bind-0123456789abcdef",
               "capabilities": {"harness": "claude", "model": MODEL, "where": [], "lands": [], "pushes": [],
                                "publishes": []},
               "budget_run_usd_micros": 20_000_000, "budget_window_usd_micros": 40_000_000,
               "run_ceiling": run_ceiling, "guard": []}
        doc.update(over)
        return doc

    def sign(self, doc: dict, *extra: str) -> Ran:
        """``levain agents sign`` (a real ssh-keygen signature by the enrolled laptop key, at the wall clock)."""
        self._n += 1
        path = self.tmp / f"link{self._n}.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        return self.cli("agents", "sign", str(path), "--data-root", str(self.world.root), "--signing-key",
                        str(self.key), *extra, spawn_gate=False)

    def sign_in_log_at(self, when: datetime, doc: dict) -> str:
        """Set-up only: a link signed into the authority log AT ``when`` (the log's own clock), for a case whose clock
        is a faked instant the wall-clock ``agents sign`` cannot sign at. Same append, same replay, same key."""
        from datetime import timedelta
        from levain.cockpit.consent import utc_stamp

        self._n += 1
        gid = f"al-{self._n:012x}"
        self.world.clock.now = when
        grant = {"id": gid, "kind": "agent-link", "expires_at": utc_stamp(when + timedelta(days=29)), **doc}
        self.world.link({"op": "create_grant", "grant": grant}, "laptop")
        return gid

    def spawned(self) -> list[str]:
        """The occurrences the spawn gate was asked to start a fire for (it started none)."""
        return [ln.split()[1] for ln in self.spawns.read_text().splitlines()] if self.spawns.exists() else []


def utc(*a: int) -> str:
    return datetime(*a, tzinfo=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prepare_link(inst: Install, doc: dict):
    """Set-up only: a link PREPARED and signed (not appended) at the log's current head, by the enrolled laptop key. Returns
    ``(connection, pending, signature)``; the caller appends it later, after the head has moved."""
    from datetime import timedelta
    from levain import authority as auth
    from levain.cockpit.consent import utc_stamp

    inst._n += 1
    now = datetime.now(timezone.utc)
    grant = {"id": f"al-{inst._n:012x}", "kind": "agent-link", "expires_at": utc_stamp(now + timedelta(days=29)), **doc}
    conn = sqlite3.connect(inst.world.db, isolation_level=None)
    pending = auth.prepare(conn, {"op": "create_grant", "grant": grant}, signer_fp=inst.world.fp("laptop"), now=now,
                           cache=auth.ReplayCache())
    return conn, pending, inst.world.keys["laptop"][1](pending.message)


def popen(inst: Install, *argv: str, now: str | None = None, spawn_gate: bool = False) -> subprocess.Popen:
    """``levain <argv>`` started and NOT waited for (two of these are a race between two real processes)."""
    gated = now is not None or spawn_gate
    exe = levain_bin()
    cmd = [sys.executable, str(ENTRY), *argv] if gated or exe is None else [str(exe), *argv]
    return subprocess.Popen(cmd, cwd=inst.tmp, env=inst.env(now, spawn_gate), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, stdin=subprocess.DEVNULL)


def finish(p: subprocess.Popen, timeout: int = 180) -> Ran:
    out, err = p.communicate(timeout=timeout)
    return Ran(p.returncode, out, err)
