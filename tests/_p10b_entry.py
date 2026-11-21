"""The P10b rig's one entry point: a levain process (the CLI, or a session agent's fire) with a stub session launcher.

``tests/_p10a_entry.py`` closes the one door to a model-calling child for the dream: the CLI process it starts is the only
process a case runs, and ``spawn_fire`` is replaced there. A session agent's fire is its own process, and the launcher its
install names must be registered in THAT process (``test_p10b_a_fire_without_the_entry_cannot_reach_the_stub`` shows what a
fire without this entry does). The tick's reaper builds its session executor from the same install in the CLI process
(``test_p10b_a_revoke_mid_session_ends_the_row_stopped`` runs it), so the CLI entry registers the stub too. This file is both:

* ``python <this file> <levain argv>``: the stub registered, then ``levain``'s own ``main``.
* ``python <this file> _fire --store <dir> <occurrence>``: the stub registered, then the controller's own ``_main``. The fire's
  identity check reads the occurrence id off this process's command line, so the occurrence stays an argument.

Everything else is ``_p10a_entry._gates`` (the clock, the shift, the kill points). This file adds only what the environment
names, ``P10B_STUB_DIR``: a directory the stub launcher keeps its records and its controls in.

The stub's vendor is a tiny python program behind the real ``GatedChild`` gate, so the exit status a run reads is a real
process's. Controls (files in ``P10B_STUB_DIR``, written by the case): ``hold`` (the vendor waits while it exists), ``exit``
(its exit status), ``usage`` (``unreadable``: the launcher answers no usage), ``subscription`` (the launcher also serves the
subscription route's auth). Records: ``calls`` (the number of spawns),
``launch-<n>.json`` (what the launcher was asked to start) and ``gate-<n>.pid``.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

STUB_LAUNCHER = "p10b-stub"      # the name the install names for the stub
STUB_USAGE_USD_MICROS = 0        # the dollars the stub reports when it can read its usage (a readable run debits this)

_VENDOR = (
    "import os, sys, time\n"
    "d = sys.argv[1]\n"
    "while os.path.exists(os.path.join(d, 'hold')):\n"
    "    time.sleep(0.05)\n"
    "try:\n"
    "    code = int(open(os.path.join(d, 'exit')).read().strip())\n"
    "except OSError:\n"
    "    code = 0\n"
    "sys.exit(code)\n"
)


class StubLauncher:
    """A session launcher on the S1 dispatch route whose child is a real gated process (see the module docstring)."""

    def __init__(self, directory: Path | str) -> None:
        from levain.agents import session as S

        self.dir = Path(directory)
        self.route = S.S1_DISPATCH
        self.auth = "brokered"
        if (self.dir / "subscription").exists():     # the control a subscription case writes: serve that route too
            self.auths = frozenset({"brokered", S.AUTH_FOR_BILLING["subscription"]})

    def spawn(self, launch):
        from levain.agents import controller as ctl

        n = self._count() + 1
        (self.dir / "calls").write_text(str(n), encoding="utf-8")
        (self.dir / f"launch-{n}.json").write_text(json.dumps(
            {"occurrence_id": launch.occurrence_id, "session_key": launch.session_key, "auth": launch.auth,
             "usd_micros": launch.usd_micros, "harness": launch.harness, "model": launch.model}), encoding="utf-8")
        child = ctl.GatedChild([sys.executable, "-c", _VENDOR, str(self.dir)])
        pid_file = self.dir / f"gate-{n}.pid"
        pending = self.dir / f"gate-{n}.pid.tmp"
        pending.write_text(str(child.process.pid), encoding="utf-8")
        os.replace(pending, pid_file)                # a reader sees the whole pid or no file
        return child

    def _count(self) -> int:
        try:
            return int((self.dir / "calls").read_text(encoding="utf-8").strip() or 0)
        except (OSError, ValueError):
            return 0

    def usage(self, session_key: str):
        from levain.agents.accounting import Usage

        if (self.dir / "usage").exists() and (self.dir / "usage").read_text(encoding="utf-8").strip() == "unreadable":
            return Usage(None, None, None, "p10b-stub:unreadable")
        return Usage(STUB_USAGE_USD_MICROS, 10, 20, "p10b-stub")

    def end_session(self, session_key: str, deadline_s: float):
        """The S1 END a reaper (a process that holds no child) asks: terminate the gate of the launch that carries this key
        and answer proven only when its process group is gone."""
        from levain.agents.accounting import StopResult

        for n in range(1, self._count() + 1):
            try:
                rec = json.loads((self.dir / f"launch-{n}.json").read_text(encoding="utf-8"))
                pgid = int((self.dir / f"gate-{n}.pid").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if rec.get("session_key") != session_key:
                continue
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):   # macOS answers EPERM for a group of unreaped zombies
                return StopResult(True, "p10b-stub: the group was already gone")
            until = time.monotonic() + max(0.0, deadline_s)
            while time.monotonic() < until:
                try:
                    os.killpg(pgid, 0)
                except (ProcessLookupError, PermissionError):   # macOS answers EPERM for a group of unreaped zombies
                    return StopResult(True, "p10b-stub: the group is gone")
                time.sleep(0.05)
            return StopResult(False, "p10b-stub: the group is still there")
        return StopResult(False, "p10b-stub: no launch carries that session key")


def _register_stub() -> None:
    directory = os.environ.get("P10B_STUB_DIR")
    if not directory:
        return
    from levain.agents import session_wire

    session_wire.register_launcher(STUB_LAUNCHER, lambda store: StubLauncher(directory))


if __name__ == "__main__":
    from tests._p10a_entry import _gates

    _gates()
    _register_stub()
    if sys.argv[1:2] == ["_fire"]:
        from levain.agents import controller
        from levain.home_guard import mark_live

        mark_live()
        sys.exit(controller._main(sys.argv[1:]))
    from levain.cli import main

    sys.exit(main(sys.argv[1:]))
