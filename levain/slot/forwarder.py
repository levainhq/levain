"""The egress forwarder (CONTRACT §9.1, §3.4; plan §2d 1): inside an egress slot's network namespace, TCP
``127.0.0.1:47100`` -> the slot's front socket ``R/gw/slot<n>/gw.sock``, so the vendors get the TCP proxy URL they
need while the slot's only path out stays the one unix socket bound into its view.

It is the SLOT's own process, started by ``start`` through the same wall as the vendor (drop + assert, the netns,
the §9.2 view): the front's ``SO_PEERCRED`` check then names the slot (plan §2d 1: a forwarder run as root or as
the front would make the peer check name the wrong principal). A byte relay and nothing else: one accepted TCP
connection = one unix connection, bytes copied both ways until each side's EOF, no parsing. Killing it loses the
slot its own egress, nothing more (§1 egress-slot row).

Measured shape: R-L1 N4 ran ``socat TCP-LISTEN:3128,bind=127.0.0.1 UNIX-CONNECT:<gw.sock>`` in the netns and got
the allowed host's 200 (runs/linux-netns). This is that relay in the root import tree's Python, so no socat.
"""
from __future__ import annotations

import socket
import sys
import threading
from typing import Callable, Optional, Sequence

LISTEN_HOST = "127.0.0.1"
#: DECISION: concurrent connections past this are closed at accept (a slot cannot pin unbounded threads).
MAX_CONNS = 256
_CHUNK = 65536


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy until ``src`` reads EOF, then half-close ``dst`` (so a request body's end reaches the front while
    its answer still flows back). An error on either side ends both directions."""
    try:
        while True:
            data = src.recv(_CHUNK)
            if not data:
                break
            dst.sendall(data)
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        for s in (src, dst):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _relay(tcp: socket.socket, unix_path: str, slots: threading.BoundedSemaphore) -> None:
    try:
        try:
            up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            up.connect(unix_path)
        except OSError:
            tcp.close()
            return
        back = threading.Thread(target=_pump, args=(up, tcp), daemon=True)
        back.start()
        _pump(tcp, up)
        back.join()
        up.close()
        tcp.close()
    finally:
        slots.release()


def serve(unix_path: str, *, port: int = 47100, max_conns: int = MAX_CONNS,
          ready: Optional[Callable[[int], None]] = None, stop: Optional[threading.Event] = None) -> None:
    """Listen on ``127.0.0.1:port`` (loopback only, never another address) and relay each connection to
    ``unix_path``. ``ready`` gets the bound port (tests pass ``port=0``); ``stop`` ends the accept loop."""
    lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    lst.bind((LISTEN_HOST, port))
    lst.listen(128)
    lst.settimeout(0.2 if stop is not None else None)
    slots = threading.BoundedSemaphore(max_conns)
    if ready is not None:
        ready(lst.getsockname()[1])
    try:
        while stop is None or not stop.is_set():
            try:
                conn, _ = lst.accept()
            except socket.timeout:
                continue
            conn.settimeout(None)
            if not slots.acquire(blocking=False):
                conn.close()
                continue
            threading.Thread(target=_relay, args=(conn, unix_path, slots), daemon=True).start()
    finally:
        lst.close()


def main(argv: Sequence[str]) -> int:
    """``--port N --unix PATH`` (the argv :func:`levain.slot.wall.launch_spec` builds), nothing else."""
    if len(argv) != 4 or argv[0] != "--port" or argv[2] != "--unix" or not argv[1].isdigit() \
            or not 0 < int(argv[1]) < 65536 or not argv[3].startswith("/"):
        sys.stderr.write("levain forwarder: usage: --port N --unix /abs/path\n")
        return 2
    serve(argv[3], port=int(argv[1]))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))


__all__ = ["LISTEN_HOST", "MAX_CONNS", "main", "serve"]
