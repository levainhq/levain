"""The P10a rig's one entry point: ``levain``'s own ``main`` in a fresh process, with at most two gates in front of it.

A P10a case runs the CLI as a subprocess (the installed shape: argv in, exit code and text out). This file is that
subprocess's ``python <file> <argv...>``. It adds NOTHING to what runs except what the environment names:

* ``P10A_KILL``: ``c1`` .. ``c5``. The first call of that step of ``levain agents revoke`` (c1 the bar's open, c2 the
  prepare, c3 the signing, c4 the append, c5 the bar's close) SIGKILLs the process instead: the revoke crash matrix's
  points, in a real process, with no product code changed (the steps are module attributes the verb looks up at call).

* ``P10A_NOW``: an ISO UTC instant. The two wall-clock reads a CLI verb makes before it hands a clock down (the tick
  driver's ``_utc`` and ``levain.dream.start``'s ``datetime.now``) answer it. Everything below those reads is the product
  as it runs: the schedule, the claims, the launch.
* ``P10A_NO_SPAWN``: a file path. ``controller.spawn_fire`` (the one door to a model-calling child) is replaced by a
  recorder that appends ``<store_dir> <occurrence>`` to that file and starts nothing: a launched row stays launched,
  with no fire, and no case can reach a key.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone


def _gates() -> None:
    now = os.environ.get("P10A_NOW")
    if now:
        fixed = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        from levain.agents import tick_driver
        from levain.dream import start as dstart

        class _At(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed.astimezone(tz) if tz else fixed

        tick_driver._utc = lambda: fixed
        dstart.datetime = _At
    kill = os.environ.get("P10A_KILL")
    if kill:
        import signal

        def _sigkill(*a, **k):
            os.kill(os.getpid(), signal.SIGKILL)

        from levain import agent_authority as aa
        from levain.agents import bars

        # the five points of the revoke's crash matrix (A0 section 2.2), each the first call of its step: SIGKILL there
        from levain.agents import controller as _controller

        # c9 (P6-5): the launcher, after its launch commit, at the one door to a fire (the row is committed 'launched'
        # with its slot and reservation; the process dies before any fire exists)
        point = {"c1": (bars, "open_bar"), "c2": (aa, "_prepare_onto"), "c3": (aa, "revoke_agent_sign"),
                 "c4": (aa, "revoke_agent_finish"), "c5": (bars, "close_bar"),
                 "c9": (_controller, "spawn_fire")}[kill]
        setattr(point[0], point[1], _sigkill)
    sink = os.environ.get("P10A_NO_SPAWN")
    if sink:
        from levain.agents import controller

        def spawn_fire(store_dir, occurrence_id, **kw):
            with open(sink, "a", encoding="utf-8") as out:
                out.write(f"{store_dir} {occurrence_id}\n")

            class _Nothing:
                pid = None
                returncode = None

                def wait(self, *a, **k):
                    return 0

            return _Nothing()

        controller.spawn_fire = spawn_fire


if __name__ == "__main__":
    _gates()
    from levain.cli import main

    sys.exit(main(sys.argv[1:]))
