"""CH-D2 S5b DONE runs: the decider serving srt's protocol for the REAL glue (`levain/chat_egress/node/glue.mjs`) over
srt 0.0.81, in-process, spawned as S8's launch will (a closed env, explicit fds), with a local recording origin on
127.0.0.1 (plain HTTP; port stand-ins and srt's literal carve-out are the glue's test seams, `--http-ports`,
`--carve`, `--resolve-map`; the decider's resolver gets a map seam that answers a public address for the names).

Runs (the S5b row's DONE): a thread per request (two cards held 20 s while a GET completes); the spool in decoded
bytes with no upstream byte before the verdict; a resolver hang -> that request denied, another completes; fuzz ->
a well-formed deny and the next GET served; caps -> 503 card_cap / body_cap; the service's threads after a close.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from levain.chat_egress import decider as dm
from levain.chat_egress import keys
from levain.chat_egress.lifecycle import Settled
from levain.chat_egress.resolver import Resolver

from .decider_rig import Desk, Live, Lookup, NoStore, make_launch
from .fixtures import env as envmod

pytestmark = pytest.mark.chat_egress_srt

REPO = Path(__file__).resolve().parents[2]
GLUE = REPO / "levain/chat_egress/node/glue.mjs"
TABLE = REPO / "levain/chat_egress/denied_table.json"
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "spike_glue"))
import decider_spike4 as spike  # noqa: E402  (its make_ca: an RSA CA srt's node-forge accepts)


class Origin:
    """127.0.0.1 HTTP origin: records each request's method, path and the body bytes it received; answers 200."""

    def __init__(self) -> None:
        self.reqs: list[dict] = []
        self.conns = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _any(self):
                body = b""
                if self.headers.get("transfer-encoding", "").lower() == "chunked":
                    while True:
                        n = int(self.rfile.readline().split(b";")[0], 16)
                        if n == 0:
                            self.rfile.readline()
                            break
                        body += self.rfile.read(n)
                        self.rfile.readline()
                elif self.headers.get("content-length"):
                    body = self.rfile.read(int(self.headers["content-length"]))
                outer.reqs.append({"method": self.command, "path": self.path, "body": body,
                                   "headers": [k.lower() for k in self.headers.keys()]})
                self.send_response(200)
                self.send_header("content-type", "text/plain")
                self.send_header("content-length", "2")
                self.end_headers()
                self.wfile.write(b"ok")

            do_GET = do_POST = do_PUT = _any

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.t = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.t.start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


class Launch:
    """One glue process + one in-process Decider on its sockets."""

    def __init__(self, tmp: Path, origin_port: int, *, limits: dm.Limits, lookup: Lookup, mode: str = "gated",
                 resolver_timeout: float = 1.0, names: str = "pub.rb.test=127.0.0.1;slow.rb.test=127.0.0.1") -> None:
        from levain.chat_egress import records as rec
        from levain.coordinator.store import CoordStore
        self.dir = tmp
        self.store = CoordStore(tmp / "dispatch")
        self.records = rec.EgressRecords(self.store)
        self.launch_id = make_launch(self.store)
        self.desk = Desk()
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(64)
        self.port = lsock.getsockname()[1]
        ours, theirs = socket.socketpair()
        obs_ours, obs_theirs = socket.socketpair()
        for s in (obs_ours, obs_theirs):
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
        ca_r, ca_w = os.pipe()
        life_r, self.life_w = os.pipe()
        fds = (lsock.fileno(), theirs.fileno(), obs_theirs.fileno(), ca_r, life_r)
        for fd in fds:
            os.set_inheritable(fd, True)
        P = origin_port
        srt = envmod.srt_package_dir() / "dist" / "sandbox"
        self.stderr = tmp / "glue.stderr"
        self.events = tmp / "events.jsonl"
        self.proc = subprocess.Popen(
            [envmod.node_bin(), str(GLUE), "--srt-dist", str(srt), "--listen-fd", str(lsock.fileno()),
             "--decider-fd", str(theirs.fileno()), "--obs-fd", str(obs_theirs.fileno()), "--ca-fd", str(ca_r),
             "--lifeline-fd", str(life_r), "--events", str(self.events), "--denied-table", str(TABLE),
             "--tls-ports", "443", "--http-ports", f"80,{P}", "--carve", f"127.0.0.1:{P}",
             "--resolve-map", names],
            pass_fds=fds, env={}, stdout=subprocess.DEVNULL, stderr=self.stderr.open("w"))
        key_pem, cert_pem = spike.make_ca()
        os.write(ca_w, key_pem + cert_pem)
        os.close(ca_w)
        for fd in (ca_r, life_r):
            os.close(fd)
        theirs.close()
        obs_theirs.close()
        lsock.close()
        self.d = dm.Decider(ours, obs_ours, launch_id=self.launch_id, records=self.records,
                            routes=keys.RouteSet.of([]), mode=dm.Fixed(mode), liveness=Live(),
                            credentials=keys.LaunchCredentials(NoStore()), cards=self.desk,
                            resolver=Resolver(lookup, max_lookups=4, timeout_s=resolver_timeout), limits=limits,
                            launch_mode=mode)
        self.d.start()
        for _ in range(400):
            if "glue: serving" in self.stderr.read_text():
                break
            time.sleep(0.025)
        assert "glue: serving" in self.stderr.read_text(), self.stderr.read_text()
        self.origin_port = P

    def request(self, method: str, path: str, *, body: bytes | None = None, headers: dict | None = None,
                chunked: bool = False, timeout: float = 30.0) -> tuple[int, bytes, float]:
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        h = {"Host": f"pub.rb.test:{self.origin_port}", **(headers or {})}
        t0 = time.monotonic()
        try:
            c.request(method, f"http://pub.rb.test:{self.origin_port}{path}", body=body, headers=h,
                      encode_chunked=chunked)
            r = c.getresponse()
            data = r.read()
            return r.status, data, time.monotonic() - t0
        finally:
            c.close()

    def raw(self, text: bytes, timeout: float = 10.0) -> bytes:
        s = socket.create_connection(("127.0.0.1", self.port), timeout=timeout)
        try:
            s.sendall(text)
            out = b""
            while b"\r\n\r\n" not in out:
                chunk = s.recv(65536)
                if not chunk:
                    break
                out += chunk
            return out
        finally:
            s.close()

    def close_life(self) -> None:
        """Close the lifeline write end once; the number is then -1, so no later close touches a reused descriptor."""
        if self.life_w >= 0:
            fd, self.life_w = self.life_w, -1
            os.close(fd)

    def stop(self) -> int:
        self.close_life()
        try:
            code = self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            code = self.proc.wait(timeout=5)
        assert self.d.close(timeout_s=10)
        return code


@pytest.fixture
def origin():
    o = Origin()
    yield o
    o.close()


@pytest.fixture
def launch(tmp_path, origin):
    made: list[Launch] = []

    def make(**kw) -> Launch:
        kw.setdefault("limits", dm.Limits(card_poll_s=0.1, stop_drain_s=2.0))
        kw.setdefault("lookup", Lookup())
        L = Launch(tmp_path / f"L{len(made)}", origin.port, **kw)
        made.append(L)
        return L

    yield make
    for L in made:
        if L.proc.poll() is None:
            L.stop()


def _until(fn, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if fn():
            return True
        time.sleep(0.05)
    return False


def test_two_cards_held_20_s_while_a_get_completes(launch, origin) -> None:
    L = launch()
    out: dict = {}

    def post(name: str) -> None:
        out[name] = L.request("POST", f"/{name}", body=b"hello", timeout=60)

    ths = [threading.Thread(target=post, args=(n,)) for n in ("c1", "c2")]
    for t in ths:
        t.start()
    assert _until(lambda: len(L.desk.filed) == 2)
    held_at = time.monotonic()
    st, body, took = L.request("GET", "/g")
    print(f"RUN A13: GET while two cards held: status {st} in {took:.3f} s; cards filed {len(L.desk.filed)}")
    assert st == 200 and body == b"ok" and took < 5.0
    while time.monotonic() - held_at < 20:
        time.sleep(0.5)
    assert [r["path"] for r in origin.reqs] == ["/g"]                 # nothing of the held POSTs reached the origin
    ids = {c["path"]: f"card-{i}" for i, c in enumerate(L.desk.filed, 1)}   # the two POSTs race to the desk
    L.desk.answer[ids["/c1"]] = "allowed"
    L.desk.answer[ids["/c2"]] = "denied"
    for t in ths:
        t.join(30)
    print(f"RUN A13: after 20 s held: c1 {out['c1'][0]} in {out['c1'][2]:.1f} s, c2 {out['c2'][0]} in "
          f"{out['c2'][2]:.1f} s; origin saw {[(r['method'], r['path'], r['body']) for r in origin.reqs]}")
    assert out["c1"][0] == 200 and out["c2"][0] == 403
    assert out["c1"][2] >= 20 and out["c2"][2] >= 20
    assert [(r["method"], r["path"], r["body"]) for r in origin.reqs] == [("GET", "/g", b""), ("POST", "/c1", b"hello")]
    rids = [c["request"] for c in L.desk.filed]
    assert L.stop() == 0
    by_path = {c["path"]: c["request"] for c in L.desk.filed}
    assert L.records.settled(L.launch_id, by_path["/c1"]) is Settled.COMPLETED
    assert L.records.settled(L.launch_id, by_path["/c2"]) is Settled.DENIED
    assert len(rids) == 2 and dm.service_threads() == []


def test_a_chunked_body_is_spooled_decoded_and_nothing_goes_upstream_before_the_verdict(launch, origin) -> None:
    L = launch()
    payload = b"A" * 7000 + b"B" * 5000
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("r", L.request("POST", "/up", body=iter(
        [payload[:3000], payload[3000:9000], payload[9000:]]), chunked=True, timeout=60)))
    t.start()
    assert _until(lambda: len(L.desk.filed) == 1)
    card = L.desk.filed[0]
    time.sleep(1.0)
    print(f"RUN B19-2: card body_len {card['body_len']} body_sha256 {card['body_sha256'][:23]}...; origin requests "
          f"while held: {len(origin.reqs)}")
    import hashlib
    assert card["body_len"] == len(payload)
    assert card["body_sha256"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert origin.reqs == []                                           # zero upstream bytes before the verdict
    L.desk.answer["card-1"] = "allowed"
    t.join(30)
    assert out["r"][0] == 200
    assert [(r["path"], r["body"]) for r in origin.reqs] == [("/up", payload)]
    assert L.stop() == 0


def test_a_hung_resolve_denies_that_request_and_another_completes(launch, origin) -> None:
    lk = Lookup(hang={"slow.rb.test"})
    L = launch(lookup=lk, resolver_timeout=4.0)
    out: dict = {}

    def slow() -> None:
        c = http.client.HTTPConnection("127.0.0.1", L.port, timeout=20)
        t0 = time.monotonic()
        c.request("GET", f"http://slow.rb.test:{L.origin_port}/s", headers={"Host": f"slow.rb.test:{L.origin_port}"})
        r = c.getresponse()
        out["slow"] = (r.status, r.read(), time.monotonic() - t0)
        c.close()

    t = threading.Thread(target=slow)
    t.start()
    time.sleep(0.1)
    st, _, took = L.request("GET", "/fast")
    t.join(20)
    print(f"RUN B15: hung name -> {out['slow'][0]} after {out['slow'][2]:.2f} s; another host -> {st} in {took:.3f} s")
    assert st == 200 and took < 3.0
    assert out["slow"][0] == 502 and out["slow"][2] >= 3.9
    assert [r["path"] for r in origin.reqs] == ["/fast"]
    lk.release.set()
    assert L.stop() == 0


FUZZ = [b"/f?a=%zz", b"/f?a=%ff%fe", b"/f?%", b"/f%zz", b"/f?a=%e2%28%a1&b=%c3"]


def test_fuzz_each_gets_a_well_formed_deny_and_the_next_get_is_served(launch, origin) -> None:
    L = launch()
    P = L.origin_port
    seen = []
    for target in FUZZ:
        head = L.raw(b"GET http://pub.rb.test:%d%s HTTP/1.1\r\nHost: pub.rb.test:%d\r\n\r\n" % (P, target, P))
        status = int(head.split(b" ")[1]) if head.startswith(b"HTTP/1.1 ") else 0
        st, body, _ = L.request("GET", "/next")
        seen.append((target.decode(), status, st))
        assert 400 <= status <= 599 and st == 200 and body == b"ok", (target, head[:120])
    print("RUN B8:", seen, "| decider counters:", L.d.counters)
    assert all(r["path"] == "/next" for r in origin.reqs) and len(origin.reqs) == len(FUZZ)
    assert L.stop() == 0


def test_caps_are_503_card_cap_and_body_cap_with_nothing_forwarded(launch, origin) -> None:
    L = launch(limits=dm.Limits(card_poll_s=0.1, max_cards=1, card_body_cap=1024, stop_drain_s=2.0))
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("held", L.request("POST", "/held", body=b"x", timeout=60)))
    t.start()
    assert _until(lambda: len(L.desk.filed) == 1)
    st, body, _ = L.request("POST", "/second", body=b"y")
    print(f"RUN R9-A4 card_cap: {st} {body[:60]!r}")
    assert st == 503 and len(L.desk.filed) == 1
    L.desk.answer["card-1"] = "denied"
    t.join(30)
    st2, body2, _ = L.request("POST", "/big", body=b"z" * 5000)
    print(f"RUN R9-A4 body_cap: {st2} {body2[:60]!r}")
    assert st2 == 503 and len(L.desk.filed) == 1
    assert origin.reqs == []
    rows = {r.deciding_step for r in L.records.rows(L.launch_id)}
    assert {"wall:card_cap", "wall:body_cap"} <= rows
    assert L.stop() == 0


@pytest.fixture(scope="module")
def git_rig(tmp_path_factory):
    from .conftest import build_rig
    r = build_rig(tmp_path_factory, "s5b-git", origin="git", dns=False)
    yield r
    r.stop()


@pytest.mark.parametrize("v0", [False, True], ids=["v2", "v0"])
def test_real_clones_n_runs_zero_cards(git_rig, tmp_path, tmp_path_factory, v0) -> None:
    """R15-19-1's red: real v0 and v2 clones, N runs, zero cards: the advertisement's observation is drained at the
    upload-pack POST's decision, every run."""
    from .fixtures.repos import build_bare, git_env, write_streams
    streams = tmp_path_factory.mktemp("s5b-streams")
    write_streams(streams)
    home = tmp_path / "home"
    home.mkdir()
    name = "r0" if v0 else "r2"
    build_bare(git_rig.repo_root, name, streams / "small.fi", home)
    port = git_rig.ports.http
    L = Launch(tmp_path / "L", port, limits=dm.Limits(card_poll_s=0.1, stop_drain_s=2.0), lookup=Lookup(),
               names="git.rb.test=127.0.0.1")
    N = 5
    try:
        for i in range(N):
            cfg = ["-c", f"http.proxy=http://127.0.0.1:{L.port}"] + (["-c", "protocol.version=0"] if v0 else [])
            p = subprocess.run(["git", *cfg, "clone", "-q", f"http://git.rb.test:{port}/{name}.git",
                                str(tmp_path / f"c{i}")], env=git_env(home), capture_output=True, text=True,
                               timeout=120)
            assert p.returncode == 0, p.stderr[-800:]
    finally:
        code = L.stop()
    rows = L.records.rows(L.launch_id)
    attempts = [r for r in rows if r.kind == "egress_attempt"]
    posts = [r for r in attempts if r.method == "POST"]
    print(f"RUN R15-19-1 ({'v0' if v0 else 'v2'}): {N} clones; {len(attempts)} decisions, {len(posts)} upload-pack "
          f"POSTs, cards {len(L.desk.filed)}, steps {sorted({r.deciding_step for r in attempts})}")
    assert code == 0
    assert L.desk.filed == [] and posts and all(r.deciding_step == "silent" for r in attempts)
    assert len(posts) >= N
    assert all(L.records.settled(L.launch_id, r.request_id) is Settled.COMPLETED for r in attempts)
