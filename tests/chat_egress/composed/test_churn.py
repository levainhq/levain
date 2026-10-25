"""CH-D2 S9: C3 (continuous pre-head churn on the listener: bounded memory, fds and log rows, a valid request still
completes) and C2's observable (a client that leaves at every point of a request leaves no open upstream socket).

C3 is run, not preloaded: four threads connect and leave, send garbage, send a half head and leave, or send a head and
leave without reading, for CHURN_S seconds, while the glue process's fds and resident set are sampled from outside.
C2's dial-completes-at-close race is srt's (Node) and not reproducible from here without a slow SYN: this file measures
its consequence (an origin connection that stays open after every client has left), not the interleaving. [judged by
lane-s9, 10-25]
"""

from __future__ import annotations

import random
import socket
import subprocess
import threading
import time

import pytest

pytestmark = pytest.mark.chat_egress_srt

CHURN_S = 6.0
FD_GROWTH = 60
RSS_GROWTH_KB = 150_000
STDERR_BYTES = 200_000


def _fds(pid: int) -> int:
    out = subprocess.run(["lsof", "-p", str(pid)], capture_output=True, text=True, timeout=20).stdout
    return max(0, len(out.splitlines()) - 1)


def _rss_kb(pid: int) -> int:
    return int(subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True,
                              timeout=10).stdout.strip() or 0)


def _churn(port: int, origin_port: int, stop: threading.Event, counts: list[int], seed: int) -> None:
    rng = random.Random(seed)
    head = b"GET http://pub.rb.test:%d/c HTTP/1.1\r\nHost: pub.rb.test:%d\r\n\r\n" % (origin_port, origin_port)
    while not stop.is_set():
        kind = rng.randrange(4)
        try:
            s = socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            time.sleep(0.02)        # a refused connect must not spin the loop either
            continue
        try:
            if kind == 1:
                s.sendall(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200))))
            elif kind == 2:
                s.sendall(head[:rng.randrange(1, len(head))])
            elif kind == 3:
                s.sendall(head)
                counts[1] += 1      # a whole head was sent: one request the decider may decide
        except OSError:
            pass
        finally:
            s.close()
        counts[0] += 1
        time.sleep(0.02)        # a rate cap: an unthrottled loop exhausted this Mac's ephemeral ports (EADDRNOTAVAIL, TIME_WAIT)


def test_continuous_churn_is_bounded_and_a_valid_request_still_completes(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    pid = L.proc.pid
    assert L.request("GET", "/warm")[0] == 200
    fds0, rss0 = _fds(pid), _rss_kb(pid)
    stop = threading.Event()
    counts = [0, 0]                 # connections made, whole heads sent
    ths = [threading.Thread(target=_churn, args=(L.port, origin.port, stop, counts, i), daemon=True) for i in range(4)]
    ok = []
    try:
        for t in ths:
            t.start()
        t_end = time.monotonic() + CHURN_S
        while time.monotonic() < t_end:
            st, body, took = L.request("GET", "/during", timeout=15)
            ok.append((st, took))
            time.sleep(0.3)
    finally:
        stop.set()
        for t in ths:
            t.join(10)
    assert not any(t.is_alive() for t in ths)
    st_after = L.request("GET", "/after")[0]
    fds1, rss1 = _fds(pid), _rss_kb(pid)
    print(f"C3 churn: {counts[0]} connections in {CHURN_S}s; valid requests {len(ok)} all {sorted({s for s, _ in ok})}, "
          f"slowest {max(t for _, t in ok):.2f}s; glue fds {fds0}->{fds1}, rss {rss0}->{rss1} KB")
    assert counts[0] > 200, counts            # the churn happened
    assert ok and all(s == 200 for s, _ in ok) and st_after == 200
    assert fds1 - fds0 <= FD_GROWTH, (fds0, fds1)
    assert rss1 - rss0 <= RSS_GROWTH_KB, (rss0, rss1)
    # every submitted request reaches a terminal row, then the origin holds no connection over a quiet interval
    # (no upstream socket outlives its client: C2's observable)
    quiet_since, last_rows = None, -1
    end = time.monotonic() + 20
    while time.monotonic() < end:
        n = len(L.records.rows(L.launch_id))
        if origin.open_conns == 0 and n == last_rows:
            quiet_since = quiet_since or time.monotonic()
            if time.monotonic() - quiet_since >= 1.5:
                break
        else:
            quiet_since = None
        last_rows = n
        time.sleep(0.2)
    else:
        raise AssertionError(f"never quiet: open_conns {origin.open_conns}, rows {last_rows}")
    assert origin.open_conns == 0, origin.open_conns
    assert L.stop() == 0
    assert L.stderr.stat().st_size <= STDERR_BYTES
    # the event file: the 100 detailed walls at most, plus one summary per minute and the final flush (R15-27-1)
    lines = L.events.read_text().splitlines()
    assert sum(1 for x in lines if '"t":"wall"' in x) <= 100
    assert sum(1 for x in lines if '"t":"summary"' in x) <= 3
    rows = L.records.rows(L.launch_id)
    valid = counts[1] + len(ok) + 2              # whole heads churned, the /during requests, /warm and /after
    attempts = [r for r in rows if r.kind == "egress_attempt"]
    print("C3 rows by kind:", {k: sum(1 for r in rows if r.kind == k) for k in {r.kind for r in rows}}, "valid", valid)
    assert len(attempts) <= valid, (len(attempts), valid)   # an attempt row per decided request, not per connection


def test_a_client_that_leaves_at_every_point_leaves_no_upstream_socket(make_origin, make_launch_) -> None:
    origin = make_origin()
    L = make_launch_(origin)
    P = origin.port
    head = b"GET http://pub.rb.test:%d/c HTTP/1.1\r\nHost: pub.rb.test:%d\r\n\r\n" % (P, P)
    for cut in range(1, len(head) + 1, 3):
        for _ in range(2):
            s = socket.create_connection(("127.0.0.1", L.port), timeout=5)
            s.sendall(head[:cut])
            if cut % 2:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")   # RST on close
            s.close()
    end = time.monotonic() + 10
    while origin.open_conns > 0 and time.monotonic() < end:
        time.sleep(0.1)
    assert origin.open_conns == 0, origin.open_conns
    assert L.request("GET", "/after")[0] == 200
    assert L.stop() == 0
