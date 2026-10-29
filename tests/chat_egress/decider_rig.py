"""CH-D2 S5b: a rig for the decider's reds: srt's end of the decider socket (its framing, by hand), the glue's end of
the observation socket, a real dispatch store with a launch row, a scripted card desk and a resolver map seam."""

from __future__ import annotations

import json
import socket
import struct
import threading
import time
import uuid
from pathlib import Path

from levain.chat_egress import decider as dm
from levain.chat_egress import keys, wire
from levain.chat_egress import records as rec
from levain.chat_egress.resolver import Resolver
from levain.coordinator.store import CoordStore, next_gseq

PUBLIC = "93.184.216.34"


def make_launch(store: CoordStore) -> str:
    lid = "ln-" + uuid.uuid4().hex[:24]
    conn = store._connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        now = store._stamp()
        conn.execute("INSERT INTO launches (launch_id, gseq, session_key, harness, role, threads, model, cwd, token, "
                     "placement, at) VALUES (?, ?, ?, 'claude', 'worker', '[]', NULL, NULL, 0, 'operator-uid', ?)",
                     (lid, next_gseq(conn, now), "s-" + uuid.uuid4().hex[:16], now))
        conn.execute("COMMIT")
    finally:
        conn.close()
    return lid


class Desk:
    """A card desk whose answers the test sets: `answer[card_id] = "allowed" | "denied" | ...`."""

    def __init__(self) -> None:
        self.filed: list[dict] = []
        self.answer: dict[str, str] = {}
        self.ended: list[str] = []
        self.lock = threading.Lock()

    def file(self, card: dict) -> str:
        with self.lock:
            self.filed.append(card)
            return f"card-{len(self.filed)}"

    def state(self, card_id: str) -> str:
        return self.answer.get(card_id, "pending")

    def end(self, card_id: str) -> None:
        self.ended.append(card_id)


class Live:
    def check(self, record_id, incarnation) -> bool:
        return True


class NoStore:
    def read(self, source, account):
        raise keys.CredentialUnreadable("no keychain in tests")


#: Every hanging lookup seam's release, so the suite's teardown (conftest) frees seams a test left hung: a hung
#: lookup holds one of the resolver's process-wide slots until it returns.
SEAM_RELEASES: list[threading.Event] = []


def seam_release() -> threading.Event:
    e = threading.Event()
    SEAM_RELEASES.append(e)
    return e


def release_seams() -> None:
    while SEAM_RELEASES:
        SEAM_RELEASES.pop().set()


class Lookup:
    def __init__(self, answers: dict[str, list[str]] | None = None, hang: set[str] = frozenset()) -> None:
        self.answers = answers or {}
        self.hang = set(hang)
        self.release = seam_release()

    def __call__(self, host: str, port: int) -> list[str]:
        if host in self.hang:
            self.release.wait(30)
        return self.answers.get(host, [PUBLIC])


class SrtEnd:
    """srt's side of the decider socket: frames in, verdicts out, collected by id."""

    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.verdicts: dict[int, list[dict]] = {}
        self.cv = threading.Condition()
        self.eof = False
        self.next_id = 0
        self.raw_tail = b""
        self._t = threading.Thread(target=self._read, daemon=True)

    def handshake(self, *, reads: bool = True) -> None:
        self.send({"t": "hello", "proto": 1})
        assert self._frame() == {"t": "hello", "proto": 1, "allowedDomains": [], "deniedDomains": []}
        if reads:                                    # False: srt stops reading after the hello (its buffer fills)
            self._t.start()

    def send(self, msg: dict) -> None:
        raw = json.dumps(msg).encode()
        self.sock.sendall(struct.pack(">I", len(raw)) + raw)

    def _exact(self, n: int) -> bytes | None:
        buf = b""
        while len(buf) < n:
            c = self.sock.recv(n - len(buf))
            if not c:
                self.raw_tail = buf
                return None
            buf += c
        return buf

    def _frame(self) -> dict | None:
        h = self._exact(4)
        if h is None:
            return None
        body = self._exact(struct.unpack(">I", h)[0])
        return None if body is None else json.loads(body)

    def _read(self) -> None:
        while True:
            try:
                m = self._frame()
            except OSError:
                m = None
            with self.cv:
                if m is None:
                    self.eof = True
                    self.cv.notify_all()
                    return
                self.verdicts.setdefault(m["id"], []).append(m)
                self.cv.notify_all()

    def req(self, method="GET", host="ok.test", port=443, path="/", query=None, headers=None, sni=True,
            rid: str | None = "auto") -> tuple[int, str | None]:
        self.next_id += 1
        h = {k.lower(): list(v) for k, v in (headers or {}).items()}
        if rid == "auto":
            rid = str(uuid.uuid4())
        if rid is not None:
            h[wire.AUDIT_HEADER] = [rid]
        f = {"t": "req", "id": self.next_id, "method": method, "host": host, "port": port, "path": path}
        if sni:
            f["sni"] = host
        if query:
            f["query"] = query
        if h:
            f["headers"] = h
        self.send(f)
        return self.next_id, rid

    def body(self, srt_id: int, data: bytes, cut: bool = False) -> None:
        import base64
        m = {"t": "body", "id": srt_id, "data": base64.b64encode(data).decode()}
        if cut:
            m["cut"] = True
        self.send(m)

    def wait(self, srt_id: int, n: int = 1, timeout: float = 10.0) -> list[dict]:
        end = time.monotonic() + timeout
        with self.cv:
            while len(self.verdicts.get(srt_id, [])) < n and not self.eof:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self.cv.wait(left)
            return list(self.verdicts.get(srt_id, []))

    def got(self, srt_id: int) -> list[dict]:
        with self.cv:
            return list(self.verdicts.get(srt_id, []))


class Rig:
    def __init__(self, tmp: Path, *, mode: str = "gated", limits: dm.Limits | None = None,
                 lookup: Lookup | None = None, routes: keys.RouteSet | None = None, start_obs: bool = True,
                 link_fault=None, resolver_timeout: float = 1.0, srt_reads: bool = True, setting=None, launch_mode: str | None = None) -> None:
        self.store = CoordStore(Path(tmp) / "dispatch")
        self.records = rec.EgressRecords(self.store)
        self.launch = make_launch(self.store)
        a, b = socket.socketpair()
        oa, ob = socket.socketpair()
        self.desk = Desk()
        self.lookup = lookup or Lookup()
        setting = setting or dm.Fixed(mode)
        if launch_mode is None:              # the rig stands in for start_egress: one read, before the decider
            launch_mode = setting.get()
        self.d = dm.Decider(a, oa, launch_id=self.launch, records=self.records,
                            routes=routes or keys.RouteSet.of([]), mode=setting, liveness=Live(),
                            credentials=keys.LaunchCredentials(NoStore()), cards=self.desk,
                            resolver=Resolver(self.lookup, max_lookups=4, timeout_s=resolver_timeout),
                            limits=limits or dm.Limits(card_poll_s=0.05, stop_drain_s=0.5), link_fault=link_fault,
                            launch_mode=launch_mode)
        if not start_obs:
            self.d._obs_loop = lambda: None          # no reader thread: only the decisions drain
        self.srt = SrtEnd(b)
        self.obs = ob
        self.d.start()
        self.srt.handshake(reads=srt_reads)

    def obs_line(self, line: bytes) -> None:
        self.obs.sendall(line)

    def rows(self, rid: str | None = None):
        return self.records.rows(self.launch, rid)

    def kinds(self, rid: str) -> list[str]:
        return sorted(r.kind for r in self.rows(rid))

    def close(self) -> bool:
        try:
            self.obs.close()
        except OSError:
            pass
        ok = self.d.close(timeout_s=5)
        self.lookup.release.set()
        return ok
