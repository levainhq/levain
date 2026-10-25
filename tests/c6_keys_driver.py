"""keys in its launcher form for S1 3c-6d's lineage crash proofs (`tests/test_lineage_process.py`; 3c design r3 §2.4
"The crash table" as R4 §3 amends it: seven rows, one fault gate per keys row).

`python tests/c6_keys_driver.py <keys argv>` is `tests/c3_keys_driver.py` (the plain-TCP upstream seam, the scaled
attachment clocks) plus ONE optional writer gate in the REAL ledger, chosen from the environment:

  $C6_GATE = "<stage>:<kind>"   stage w1: block before `os.write` of the first batch holding a `<kind>` line (nothing of
                                it reaches the file); w2: write that batch, block before its flush; w3: write and flush
                                it, block before the writer posts its durable callback (on disk, never counted).
  $C6_EVENTS = <path>           where the gate writes ONE JSON line `{"at": "gate", "gate": .., "size": <the file's size
                                before the batch>}` (one `os.write`, so a `kill -9` keeps it) just before it blocks.

The gate blocks the writer thread for ever; the loop runs on (nothing after the gated batch becomes durable), and the
test SIGKILLs the process once it reads the event. `<kind>` is matched as the canonical `"kind":"<kind>"` text of a
line (`ledger._encode`), so a payload field cannot match it."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from levain.broker import ledger as L   # noqa: E402
from tests import c3_keys_driver        # noqa: E402,F401 - the upstream seam and the scaled clocks

GATE = os.environ.get("C6_GATE", "")


def _event(**kw) -> None:
    kw["t"] = time.time()
    fd = os.open(os.environ["C6_EVENTS"], os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.write(fd, (json.dumps(kw, sort_keys=True) + "\n").encode())
    finally:
        os.close(fd)


def _block() -> None:
    while True:
        time.sleep(3600)


def install(gate: str) -> None:
    stage, _, kind = gate.partition(":")
    assert stage in ("w1", "w2", "w3") and kind, gate
    needle = ('"kind":"%s"' % kind).encode()
    real_write_all, real_flush = L._write_all, L._flush
    state = {"armed": True, "flushing": False}
    writer = {"tid": None}

    def write_all(fd, data):
        # only the writer thread's batches (Ledger.open's own synchronous writes run on the opening thread)
        if state["armed"] and writer["tid"] == threading.get_ident() and needle in bytes(data):
            state["armed"] = False
            size = os.fstat(fd).st_size
            if stage == "w1":
                _event(at="gate", gate=gate, size=size)
                _block()
            real_write_all(fd, data)
            if stage == "w2":
                _event(at="gate", gate=gate, size=size)
                _block()
            state["flushing"] = size
            return None
        return real_write_all(fd, data)

    def flush(fd):
        out = real_flush(fd)
        if state["flushing"] is not False and writer["tid"] == threading.get_ident():
            size, state["flushing"] = state["flushing"], False
            _event(at="gate", gate=gate, size=size)
            _block()
        return out

    real_writer_main = L.Ledger._writer_main

    def writer_main(self):
        writer["tid"] = threading.get_ident()
        return real_writer_main(self)

    L._write_all, L._flush = write_all, flush
    L.Ledger._writer_main = writer_main


if GATE:
    install(GATE)

if __name__ == "__main__":
    from levain.broker import keys as K
    raise SystemExit(K.main(sys.argv[1:]))
