"""The warm derive child: one process that derives panels for a parent that keeps the derive loop (levain#157 S4, option (c)).

Run as ``python -P -m levain.cockpit.derive_child --factory MODULE:CALLABLE [--syspath DIR ...]``. The factory builds the
cockpit once (an UNSTARTED one: no refresher threads, no server), and the child then answers one request at a time, so a
provider's in-memory state stays warm between derives.

One derivation, two hosts. The child does not know how a panel is derived: it calls ``SnapshotHost._derive``, the same
function the in-process loop calls, so the status, the body and the ``valid_until`` it answers are the ones the loop would
have made for that panel. Only the host's own bookkeeping is stubbed (``retire`` has nothing to retire here: the parent's
plan set is the parent's).

Protocol (JSON lines, one object per line, UTF-8):

  parent -> child   {"id": N, "op": "derive", "panel": PANEL_ID}   -> one answer with the same id
                    {"op": "ping"}                                  -> {"op": "pong"}
                    {"op": "stop"}                                  -> the child exits 0 (so does stdin EOF)
  child -> parent   {"op": "ready", "pid": P, "protocol": "levain-derive-child/1"}   once, when the factory has built
                    {"op": "fatal", "error": TEXT}                  once, then exit 2, when the factory could not build
                    {"id": N, "status": S, "body": B, "valid_until": U, "error": E, "duration_ms": D}

A derive that raises answers ``status`` "error" with the exception as ``error`` (the text the loop puts on its own error
row) and the child lives on. The pseudo-plans of the host (discovery, the entity) are refused: they change the cockpit's own
state, which in the parent is the one that counts.

The frames own the real stdout. Before anything else is imported, file descriptor 1 is pointed at stderr and ``sys.stdout``
with it, so a provider's ``print`` (or a subprocess it starts) cannot write into the protocol. Descriptor 0 is pointed at the
null device for the same reason, so a provider's subprocess cannot read the parent's requests.

Parent death. Stdin EOF is the primary signal (the parent's pipe closes when it dies, however it dies). macOS has no
parent-death signal, so a watchdog thread also compares ``os.getppid()`` with the parent the child started under, every
``PARENT_TICK_S``, and it runs while a derive is running too: a hung derive in an orphan is the case that matters.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import select
import sys
import threading
import time
from typing import Any, BinaryIO

from levain.cockpit.snapshot_host import PSEUDO_PREFIX, SnapshotHost

PROTOCOL = "levain-derive-child/1"
# How often the watchdog looks at the parent (seconds).
PARENT_TICK_S = 1.0
# What the child exits with when its factory cannot build.
EXIT_FACTORY = 2


class _NoLoop:
    """The part of a host's loop ``SnapshotHost._derive`` may call: a panel that is no longer registered is retired from the
    plan set, and this child holds none (the parent's loop sees the error answer and retires it itself)."""

    def retire(self, pid: str) -> None:
        return None


class _DeriveHost:
    """Stands in for ``self`` of ``SnapshotHost._derive``: the cockpit, and the loop's one call."""

    def __init__(self, cockpit: Any) -> None:
        self._cockpit = cockpit
        self._loop = _NoLoop()


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


def _send(out: BinaryIO, obj: "dict[str, Any]") -> None:
    try:
        data = _frame(obj)
    except (TypeError, ValueError, RecursionError) as exc:
        # a result that is not JSON is an error answer, never a broken frame
        data = _frame({"id": obj.get("id"), "status": "error", "body": None, "valid_until": None,
                       "error": f"the answer is not JSON: {type(exc).__name__}: {exc}", "duration_ms": obj.get("duration_ms", 0)})
    out.write(data)
    out.flush()


def _derive(host: _DeriveHost, req: "dict[str, Any]") -> "dict[str, Any]":
    rid, panel = req.get("id"), req.get("panel")
    started = time.monotonic()

    def done(status: str, body: Any, until: Any, error: "str | None") -> "dict[str, Any]":
        return {"id": rid, "status": status, "body": body, "valid_until": until, "error": error,
                "duration_ms": max(0, int(round((time.monotonic() - started) * 1000)))}

    if not isinstance(panel, str) or panel.startswith(PSEUDO_PREFIX):
        return done("error", None, None, f"not a panel this child derives: {panel!r}")
    try:
        res = SnapshotHost._derive(host, panel)    # type: ignore[arg-type]  # the one derivation the in-process loop runs
    except Exception as exc:  # noqa: BLE001 - one bad derive must not end the child; the text is the loop's own error text
        return done("error", None, None, f"{type(exc).__name__}: {exc}")
    return done(res.status, res.body, res.valid_until, res.error)


def _watch_parent(parent: int, tick_s: float) -> None:
    while True:
        time.sleep(tick_s)
        if os.getppid() != parent:
            os._exit(0)


def serve(host: _DeriveHost, in_fd: int, out: BinaryIO) -> int:
    """Answer requests from ``in_fd`` until a stop request or EOF; the exit status."""
    buf = b""
    while True:
        select.select([in_fd], [], [])
        chunk = os.read(in_fd, 65536)
        if not chunk:
            return 0
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line)
                op = req.get("op") if isinstance(req, dict) else None
            except ValueError:
                _send(out, {"id": None, "status": "error", "body": None, "valid_until": None,
                            "error": "a request that is not a JSON line", "duration_ms": 0})
                continue
            if op == "stop":
                return 0
            if op == "ping":
                _send(out, {"op": "pong"})
            elif op == "derive":
                _send(out, _derive(host, req))
            else:
                _send(out, {"id": req.get("id"), "status": "error", "body": None, "valid_until": None,
                            "error": f"unknown op {op!r}", "duration_ms": 0})


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(prog="python -P -m levain.cockpit.derive_child", description=__doc__.split("\n")[0])
    ap.add_argument("--factory", required=True, help="MODULE:CALLABLE returning an unstarted Cockpit")
    ap.add_argument("--syspath", action="append", default=[], metavar="DIR", help="a directory put on sys.path before the factory imports")
    args = ap.parse_args(argv)
    parent = os.getppid()
    out_fd, in_fd = _claim_stdio()
    out = os.fdopen(out_fd, "wb")        # buffered: a large answer is written whole, never as a partial pipe write
    for i, d in enumerate(args.syspath):
        sys.path.insert(i, d)
    try:
        cockpit = _load_factory(args.factory)()
    except BaseException as exc:  # noqa: BLE001 - whatever stopped the build is said to the parent, once
        try:
            _send(out, {"op": "fatal", "error": f"{type(exc).__name__}: {exc}"})
        except OSError:
            pass
        return EXIT_FACTORY
    threading.Thread(target=_watch_parent, args=(parent, PARENT_TICK_S), name="derive-child-parent-watch", daemon=True).start()
    try:
        _send(out, {"op": "ready", "pid": os.getpid(), "protocol": PROTOCOL})
        return serve(_DeriveHost(cockpit), in_fd, out)
    except (BrokenPipeError, ConnectionResetError):
        return 0      # the parent is gone: nobody is left to answer
    finally:
        try:
            cockpit.stop()
        except Exception:  # noqa: BLE001
            pass


if __name__ == "__main__":
    code = main()
    # Leave without the interpreter's thread join: a provider thread still hung in a read must not hold an exiting child open.
    try:
        sys.stderr.flush()
    finally:
        os._exit(code)
