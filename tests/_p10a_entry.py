"""The P10a rig's one entry point: ``levain``'s own ``main`` in a fresh process, with at most two gates in front of it.

A P10a case runs the CLI as a subprocess (the installed shape: argv in, exit code and text out). This file is that
subprocess's ``python <file> <argv...>``. It adds NOTHING to what runs except what the environment names:

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
