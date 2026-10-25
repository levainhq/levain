"""The composed suite's own rig: RecordingOrigin's shutdown leaves no accept or worker thread behind."""

from __future__ import annotations

import socket
import threading

from .rig import RecordingOrigin


def test_close_joins_the_accept_thread_and_every_worker_even_with_connects_racing_it() -> None:
    for _ in range(20):
        o = RecordingOrigin()
        stop = threading.Event()
        socks: list[socket.socket] = []

        def hammer() -> None:
            while not stop.is_set():
                try:
                    socks.append(socket.create_connection(("127.0.0.1", o.port), timeout=1))
                except OSError:
                    return

        h = threading.Thread(target=hammer, daemon=True)
        h.start()
        while o.conns < 3:
            pass
        o.close()
        stop.set()
        h.join(5)
        assert not o._t.is_alive()
        assert not [t for t in o._threads if t.is_alive()]
        for s in socks:
            s.close()
