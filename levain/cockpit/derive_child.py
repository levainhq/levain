"""The warm derive child: one process that derives panels for a parent that keeps the derive loop (levain#157 S4, option (c)).

Run as ``python -P -m levain.cockpit.derive_child --factory MODULE:CALLABLE [--syspath DIR ...]``. The factory builds the
cockpit once (an UNSTARTED one: no refresher threads, no server), and the child then answers one request at a time, so a
provider's in-memory state stays warm between derives.

One derivation, two hosts. The child does not know how a panel is derived: it calls ``derive_panel_result``, the
one function the parent's own derive calls too, so the status, the body and the ``valid_until`` it answers are the ones the
loop would have made for that panel. Only the loop's ``retire`` is a no-op here: the parent's plan set is the parent's.

Protocol (JSON lines, one object per line, UTF-8):

  parent -> child   {"id": N, "op": "derive", "panel": PANEL_ID}   -> one answer with the same id
                    {"op": "ping"}                                  -> {"op": "pong"}
                    {"op": "stop"}                                  -> the child exits 0 (so does stdin EOF)
  child -> parent   {"op": "ready", "pid": P, "protocol": PROTOCOL, "panels": [ID, ...], "attached": [NAME, ...]}   once, when
                    the factory has built: the panels this child holds (a sorted list, at most READY_PANELS_MAX), so the parent
                    routes only those, and which of consent, questions and notes its cockpit holds (a sorted list), so the
                    parent routes only while the child holds every one its own cockpit holds
                    {"op": "fatal", "error": TEXT}                  once, then exit 2, when the factory could not build
                    {"id": N, "status": S, "body": B, "valid_until": U, "error": E, "duration_ms": D}

A derive that raises answers ``status`` "error" with the exception as ``error`` (the text the loop puts on its own error
row) and the child lives on. The pseudo-plans of the host (discovery, the entity) are refused: they change the cockpit's own
state, which in the parent is the one that counts.

The frames own the real stdout. Before anything else is imported, file descriptor 1 is pointed at stderr and ``sys.stdout``
with it, so a provider's ``print`` (or a subprocess it starts) cannot write into the protocol. Descriptor 0 is pointed at the
null device for the same reason, so a provider's subprocess cannot read the parent's requests.

Parent death. Stdin EOF is the primary signal (the parent's pipe closes when it dies, however it dies). macOS has no
parent-death signal, so a watchdog thread also compares ``os.getppid()`` with the parent the child started under (the
parent names itself with ``--parent-pid``, since the child could be started after its parent already died), every
``PARENT_TICK_S``. The watchdog is the first thing started, before the factory is imported or called, and it runs while a
derive runs too: an orphan stuck in a factory or in a hung derive is the case that matters. An orphan ends its whole process
group (it is the group's leader) before it exits, so a provider's subprocess does not outlive it. A broken pipe on an answer
or on the factory-failure frame is the same signal: with the parent gone, the group ends then too, not at the watchdog's tick.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import threading
import time
from typing import Any, BinaryIO

from levain.cockpit.derive_child_client import PROTOCOL, READY_PANELS_MAX
from levain.cockpit.snapshot_host import PSEUDO_PREFIX, attached_of, derive_panel_result

# How often the watchdog looks at the parent (seconds).
PARENT_TICK_S = 1.0
# What the child exits with when its factory cannot build.
EXIT_FACTORY = 2
# How long the child waits for the cockpit's own ``stop`` before it exits without it (seconds).
COCKPIT_STOP_BOUND_S = 2.0
# After stdin closes without a stop request, how long the child looks for its parent to be gone before deciding the close
# was an orderly one (seconds): a dying parent's pipe closes a moment before the child is reparented.
ORPHAN_SETTLE_S = 0.3


def _claim_stdio() -> "tuple[int, int]":
    """``(frames_out_fd, frames_in_fd)``: duplicates of the real stdout and stdin, with fd 1 pointed at stderr and fd 0 at the
    null device, so nothing but this module's own frames reaches the parent's pipe."""
    sys.stdout.flush()
    out_fd, in_fd = os.dup(1), os.dup(0)
    os.set_inheritable(out_fd, False)
    os.set_inheritable(in_fd, False)
    os.dup2(2, 1)
    null = os.open(os.devnull, os.O_RDONLY)
    os.dup2(null, 0)
    os.close(null)
    sys.stdout = sys.stderr
    return out_fd, in_fd


def _load_factory(spec: str) -> Any:
    module, sep, name = spec.partition(":")
    if not sep or not module or not name:
        raise ValueError(f"--factory wants MODULE:CALLABLE, not {spec!r}")
    target: Any = importlib.import_module(module)
    for part in name.split("."):
        target = getattr(target, part)
    return target


def _frame(obj: "dict[str, Any]") -> bytes:
    return json.dumps(obj, allow_nan=False, separators=(",", ":"), ensure_ascii=False).encode("utf-8", "surrogatepass") + b"\n"


def _safe_id(rid: Any) -> "int | str | None":
    """The request id as the frame may echo it: a plain integer or string, else None (a NaN, a float, a list cannot crash the writer)."""
    if isinstance(rid, bool) or not isinstance(rid, (int, str)):
        return None
    return rid


def _send(out: BinaryIO, obj: "dict[str, Any]") -> None:
    try:
        data = _frame(obj)
    except (TypeError, ValueError, RecursionError) as exc:
        # a result that is not JSON is an error answer, never a broken frame
        data = _frame({"id": _safe_id(obj.get("id")), "status": "error", "body": None, "valid_until": None,
                       "error": f"the answer is not JSON: {type(exc).__name__}: {exc}", "duration_ms": 0})
    out.write(data)
    out.flush()


def _derive(cockpit: Any, req: "dict[str, Any]") -> "dict[str, Any]":
    rid, panel = _safe_id(req.get("id")), req.get("panel")
    started = time.monotonic()

    def done(status: str, body: Any, until: Any, error: "str | None") -> "dict[str, Any]":
        return {"id": rid, "status": status, "body": body, "valid_until": until, "error": error,
                "duration_ms": max(0, int(round((time.monotonic() - started) * 1000)))}

    if not isinstance(panel, str) or panel.startswith(PSEUDO_PREFIX):
        return done("error", None, None, f"not a panel this child derives: {panel!r}")
    try:
        res = derive_panel_result(cockpit, panel, lambda pid: None)    # the one derivation the in-process loop runs
    except (Exception, SystemExit) as exc:  # noqa: BLE001 - one bad derive (a provider's sys.exit included) must not end the child; the text is the loop's own error text
        return done("error", None, None, f"{type(exc).__name__}: {exc}")
    return done(res.status, res.body, res.valid_until, res.error)


def _exit_orphan() -> None:
    """Leave because the parent is gone: the process group first (this child leads it), so a provider's subprocess that
    ignores a polite stop does not outlive the child; the kill reaches this process too, which is the exit."""
    try:
        if os.getpgrp() == os.getpid():
            os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        pass
    os._exit(0)


def _watch_parent(parent: int, tick_s: float) -> None:
    while True:
        if os.getppid() != parent:
            _exit_orphan()
        time.sleep(tick_s)


def _parent_gone(parent: int, settle_s: float) -> bool:
    end = time.monotonic() + settle_s
    while True:
        if os.getppid() != parent:
            return True
        if time.monotonic() >= end:
            return False
        time.sleep(0.02)


def serve(cockpit: Any, in_fd: int, out: BinaryIO, parent: "int | None" = None) -> int:
    """Answer requests from ``in_fd`` until a stop request or EOF; the exit status. An EOF that comes with the parent gone
    ends the process group as the watchdog does (``parent`` is the pid the child started under; None skips the check)."""
    buf = b""
    while True:
        chunk = os.read(in_fd, 65536)
        if not chunk:
            if parent is not None and _parent_gone(parent, ORPHAN_SETTLE_S):
                _exit_orphan()
            return 0
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line)
            except ValueError:
                req = None
            if not isinstance(req, dict):
                _send(out, {"id": None, "status": "error", "body": None, "valid_until": None,
                            "error": "a request that is not a JSON object", "duration_ms": 0})
                continue
            op = req.get("op")
            if op == "stop":
                return 0
            if op == "ping":
                _send(out, {"op": "pong"})
            elif op == "derive":
                _send(out, _derive(cockpit, req))
            else:
                _send(out, {"id": _safe_id(req.get("id")), "status": "error", "body": None, "valid_until": None,
                            "error": f"unknown op {op!r}", "duration_ms": 0})


def _panels_of(cockpit: Any) -> "list[str]":
    """The panel ids this child holds, as the ready frame states them: sorted, strings only, at most READY_PANELS_MAX."""
    return sorted(p for p in cockpit.panel_ids if isinstance(p, str))[:READY_PANELS_MAX]


def _attached_of(cockpit: Any) -> "list[str]":
    """Which of the cockpit's attachments this child holds, as the ready frame states them: sorted names from ``snapshot_host.ATTACHMENTS``."""
    return sorted(attached_of(cockpit))


def _stop_cockpit(cockpit: Any) -> None:
    """The cockpit's own ``stop``, bounded: a stop that waits on a provider thread must not hold the exit."""
    def run() -> None:
        try:
            cockpit.stop()
        except Exception:  # noqa: BLE001
            pass

    t = threading.Thread(target=run, name="derive-child-cockpit-stop", daemon=True)
    t.start()
    t.join(COCKPIT_STOP_BOUND_S)


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="python -P -m levain.cockpit.derive_child", description=__doc__.split("\n")[0])
    ap.add_argument("--factory", required=True, help="MODULE:CALLABLE returning an unstarted Cockpit")
    ap.add_argument("--syspath", action="append", default=[], metavar="DIR", help="a directory put on sys.path before the factory imports")
    ap.add_argument("--parent-pid", type=int, default=None, metavar="PID", help="the parent to watch (default: the process that started this one)")
    args = ap.parse_args(argv)
    parent = args.parent_pid if args.parent_pid is not None else os.getppid()
    out_fd, in_fd = _claim_stdio()
    # first, before anything that can take long or hang: a parent that dies from here on cannot leave this child running
    threading.Thread(target=_watch_parent, args=(parent, PARENT_TICK_S), name="derive-child-parent-watch", daemon=True).start()
    out = os.fdopen(out_fd, "wb")        # buffered: a large answer is written whole, never as a partial pipe write
    for i, d in enumerate(args.syspath):
        sys.path.insert(i, d)
    try:
        cockpit = _load_factory(args.factory)()
        panels = _panels_of(cockpit)
        attached = _attached_of(cockpit)
    except BaseException as exc:  # noqa: BLE001 - whatever stopped the build is said to the parent, once
        try:
            _send(out, {"op": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        except OSError:
            if _parent_gone(parent, ORPHAN_SETTLE_S):
                _exit_orphan()      # a helper the factory started must not outlive this exit
        return EXIT_FACTORY
    try:
        _send(out, {"op": "ready", "pid": os.getpid(), "protocol": PROTOCOL, "panels": panels, "attached": attached})
        return serve(cockpit, in_fd, out, parent)
    except (BrokenPipeError, ConnectionResetError):
        # the pipe to the parent is broken: if the parent is gone nobody is left to answer, and the process group (a provider's
        # helper that ignores SIGTERM included) ends with this child, the same as on stdin EOF, without waiting for the watchdog
        if _parent_gone(parent, ORPHAN_SETTLE_S):
            _exit_orphan()
        return 0
    finally:
        _stop_cockpit(cockpit)


if __name__ == "__main__":
    code = main()
    # Leave without the interpreter's thread join: a provider thread still hung in a read must not hold an exiting child open.
    try:
        sys.stderr.flush()
    finally:
        os._exit(code)
