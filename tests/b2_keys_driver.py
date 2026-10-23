"""keys in its launcher form for the 3c-1 session B2 process tests (T12, T13, the measurements).

`python tests/b2_keys_driver.py --runtime-root R --lib-root L --control C --test-single-uid` runs `levain.broker.keys.main`
with the same argv a launcher gives it. The ONLY difference from `python -m levain.broker.keys` is that the upstream
seam is pointed at a plain-TCP fake on 127.0.0.1:$B2_UPSTREAM_PORT (`upstream._resolve` and `upstream._handshake`,
the two seams `test_broker_keys_upstream.py` replaces in-process): a real `Upstream`, h11 and relay, no TLS and no DNS.
With $B2_MEASURE set, the process also records every writer flush's latency and the ledger queue's depth, and writes
them to that file when `main` returns (a SIGTERM stop; a SIGKILL writes nothing)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from levain.broker import keys as K          # noqa: E402
from levain.broker import ledger as L        # noqa: E402
from levain.broker import upstream as U      # noqa: E402

PORT = int(os.environ["B2_UPSTREAM_PORT"])


async def _resolve(host, port):
    return ["93.184.216.34"]


async def _handshake(addr, port, host):
    reader, writer = await U._connect("127.0.0.1", PORT)
    return reader, writer, writer


U._resolve = _resolve
U._handshake = _handshake


def _measure(path: str):
    """The hooks go in inside `Ledger.attach`, just before the writer thread starts: `Ledger.open`'s own header,
    directory and `opened` flushes (synchronous, on the opening thread) are never sampled as writer traffic. The
    sampler thread is stopped and joined before the dump."""
    flush_s: list = []
    depth: list = []
    batch_bytes: list = []
    real_flush, real_write_all = L._flush, L._write_all
    real_attach = L.Ledger.attach
    stop = threading.Event()
    samplers: list = []

    def flush(fd):
        t = time.perf_counter()
        try:
            return real_flush(fd)
        finally:
            flush_s.append(time.perf_counter() - t)

    def write_all(fd, data):
        batch_bytes.append(len(data))
        return real_write_all(fd, data)

    def attach(self, loop, **kw):
        L._flush, L._write_all = flush, write_all

        def sample():
            while not stop.is_set():
                depth.append(self._q.qsize())
                stop.wait(0.002)
        real_attach(self, loop, **kw)
        th = threading.Thread(target=sample, daemon=True)
        samplers.append(th)
        th.start()

    L.Ledger.attach = attach

    def dump():
        stop.set()
        for th in samplers:
            th.join(5)
        with open(path, "w") as f:
            json.dump({"flush_s": flush_s, "queue_depth_samples": depth, "batch_bytes": batch_bytes}, f)
    return dump


if __name__ == "__main__":
    dump = _measure(os.environ["B2_MEASURE"]) if os.environ.get("B2_MEASURE") else None
    rc = K.main(sys.argv[1:])
    if dump is not None:
        dump()
    raise SystemExit(rc)
