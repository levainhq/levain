"""The rig for S1 3c-6d's lineage crash proofs (`tests/test_lineage_process.py`): every party real, every one as its
program starts it, except the dispatch supervisor and root, which are this process's in-process calls.

- CONTROL: `control.serve` on a thread (`tests/c3_rig.ControlThread`) over a REAL signed route map (generation 1, one
  brokered dispatch route `glm`), a real ticket store with its ceiling, and its REAL lineage sink at `L/lineage`. A
  spy on the running `Control` records every `LINEAGE-APPEND` triple it receives, in order (`received`), so a test
  tells a RESEND (received twice, one row) from a second APPEND (two rows).
- keys: a child process (`tests/c6_keys_driver.py`) on the real ledger, with at most one writer fault gate, SIGKILLed
  at it and restarted on the same lib root.
- the run: `RUN-TICKET-MINT` (as `_levain_dispatch`), `LEASE-OPEN {ticket}`, root's `ATTACH` (binding it), CAS, MINT
  (CONTROL's FRONT-REGISTER to keys), FRONT-ARM, CAS, SECRETS: one TICKETED session whose front is this process, whose
  `Q`s keys resolves with TOKEN-RESOLVE's `run` and books under the run's scope. The upstream is the plain-TCP fake.

Undischarged here (3c-10's VM): the real uids, the installed unit's restart, CONTROL as its own process."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from levain.broker import creds as CR
from levain.broker import gwwire, peer
from levain.contracts import canon
from levain.slot import control as C
from levain.slot import lineage as LN
from levain.slot import records as R
from levain.slot import routemaps as RM
from tests import b2_keys_rig as B2
from tests import c3_rig as R3
from tests.test_routemaps import DISPATCH_UID, Signer, Store, item, route, submit
from tests.test_tickets import d_attach_claims, d_mint_claims, mint_args, ticket_of, write_ceiling

ROOT = Path(__file__).resolve().parents[1]
DRIVER = str(ROOT / "tests" / "c6_keys_driver.py")
HOST = "api.z.ai"
MODEL = "z-ai/glm-5.2"
ROUTE = route(host=HOST, codec="anthropic-messages")
POLICY_DIGEST = RM.policy_digest(ROUTE["policy"])
BODY = b'{"model":"%s","max_tokens":10,"messages":[]}' % MODEL.encode()
PATH = ROUTE["policy"]["requests"]["templates"][0]["path"]


def response(model: str = MODEL, provider: str = "Z.AI") -> bytes:
    body = json.dumps({"type": "message", "model": model, "provider": provider,
                       "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()
    return (b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: %d\r\nconnection: close\r\n\r\n"
            % len(body) + body)


def _ok(ans: Dict[str, Any]) -> Dict[str, Any]:
    assert ans.get("ok") is True, ans
    return ans


class Run:
    """One ticketed dispatch session bound through CONTROL's own verbs (test_tickets' `bound_session`, then c3_rig's
    `Session` steps): `token` is its token, `run_id` the run's id."""

    def __init__(self, control: C.Control, session: str, attempt: str) -> None:
        dispatch = C.Conn(DISPATCH_UID)
        ans = control.handle(dispatch, {"v": 1, "op": "RUN-TICKET-MINT", "id": 1,
                                        **mint_args(attempt, route_policy_digest=POLICY_DIGEST)})
        self.ticket, self.run_id, self.session = ticket_of(ans), ans["run_id"], session
        self.lease = C.Conn(DISPATCH_UID)
        nonce = _ok(control.handle(self.lease, {"v": 1, "op": "LEASE-OPEN", "id": 2,
                                                "ticket": self.ticket}))["lease_nonce"]
        root = C.Conn(0)

        def ask(op: str, **kw) -> Dict[str, Any]:
            return _ok(control.handle(root, {"v": 1, "op": op, "id": 3, **kw}))

        ask("ATTACH", lease_nonce=nonce, sudo_uid=DISPATCH_UID, session=session,
            claims=d_attach_claims(self.ticket))
        gen = ask("CAS", session=session, generation=0, fields={}, **{"from": R.ALLOCATED, "to": R.RENDERING})
        pid = os.getpid()
        front = {"uid": os.geteuid(), "pid": pid, "start": peer.start_of(pid)}
        gen = ask("MINT", session=session, generation=gen["generation"],
                  claims=d_mint_claims(front=front))["generation"]
        ask("FRONT-ARM", session=session, generation=gen)
        gen = ask("CAS", session=session, generation=gen, fields={},
                  **{"from": R.RENDERING, "to": R.RENDERED})["generation"]
        self.token = ask("SECRETS", session=session)["token"]


class Front(R3.Front):
    """c3_rig's front, with the dispatch route's `Q`."""

    async def q(self, body: bytes = BODY, token: Optional[str] = None) -> Dict[str, Any]:
        q = {"v": 1, "token": token or self.session.token, "route": "glm", "method": "POST", "path": PATH,
             "query": "", "headers": [], "body": "length:%d" % len(body),
             "upgrade": "none"}
        frames: List[tuple] = []
        r, w = await asyncio.open_unix_connection(self.keys.front_path)
        try:
            w.write(gwwire.encode(b"Q", q) + gwwire.encode(b"B", body) + gwwire.encode(b"E"))
            await w.drain()
            while True:
                try:
                    head = await asyncio.wait_for(r.readexactly(5), 30)
                except (asyncio.IncompleteReadError, ConnectionError):
                    break
                n = int.from_bytes(head[1:], "big")
                data = await r.readexactly(n) if n else b""
                frames.append((head[:1], data))
                if head[:1] in (b"e", b"F"):
                    break
        finally:
            w.close()
        out: Dict[str, Any] = {"frames": [t for t, _ in frames]}
        for t, b in frames:
            if t == b"F":
                out["refused"] = json.loads(b)["refused"]
            elif t == b"R":
                out["status"] = json.loads(b)["status"]
        return out


Triple = Tuple[str, str, int]


def triple_of(src: Dict[str, Any]) -> Triple:
    return (src["ledger"], src["epoch"], src["ref"])


class World:
    """The whole rig. `start_keys(gate)` (re)starts keys with that writer gate (None: none); `kill_at_gate()` waits
    for the gate's event and SIGKILLs keys there; `rows()` is CONTROL's sink as on disk; `received` every triple
    CONTROL was sent, in order."""

    def __init__(self, py: str, hold: bool = False) -> None:
        self.py, self.hold = py, hold

    async def __aenter__(self) -> "World":
        self.d = Path(tempfile.mkdtemp(prefix="c6", dir="/tmp"))
        try:
            self.st = Store(self.d, Signer(self.d, "op"))
            self.one, sig = self.st.signed(item(1, [ROUTE]))
            submit(RM.ControlRoutes(self.st.routes), self.one, sig)
            write_ceiling(self.st.L)
            cred = self.d / "keys" / "cred"
            cred.mkdir(mode=0o700, exist_ok=True)
            (cred / "or-key.json").write_text(json.dumps(
                {"format": CR.FORMAT, "name": "or-key", "rev": "0" * 32, "hosts": [HOST],
                 "inject": {"header": "authorization", "scheme": "Bearer"}, "kind": "static",
                 "secret": B2.SECRET}))
            self.lineage = self.d / "lineage"
            self.lineage.mkdir(mode=0o750)
            self.events = str(self.d / "gate.events")
            self.received: List[Triple] = []
            self.keys: Optional[B2.KeysProc] = None
            self.fronts: List[Front] = []
            self.start_control()
            self.up = await B2.FakeUpstream(response()).start(hold=self.hold)
        except BaseException:
            shutil.rmtree(self.d, ignore_errors=True)
            raise
        return self

    def start_control(self) -> None:
        self.ctl = R3.ControlThread(str(self.d), routes_dir=self.st.routes, dispatch_uid=DISPATCH_UID,
                                    lineage_dir=self.lineage).start()
        control = self.ctl.control
        real = control._op_lineage_append

        def spy(conn, args):
            self.received.extend(triple_of(r["source"]) for r in args["envelope"]["records"])
            return real(conn, args)

        control._op_lineage_append = spy

    @property
    def control(self) -> C.Control:
        return self.ctl.control

    async def start_keys(self, gate: Optional[str] = None) -> B2.KeysProc:
        if self.keys is not None:
            self.keys.cleanup()
        env = {"C6_EVENTS": self.events}
        if gate:
            env["C6_GATE"] = gate
        self.keys = B2.KeysProc(str(self.d), self.py, self.up.port, driver=DRIVER, env=env)
        await self.keys.start()
        await R3.aio_until(lambda: self.control.keys_attached() is not None, what="CONTROL sees keys attached")
        return self.keys

    async def run(self, session: str = "s-00000000000000a1", attempt: str = "a-1") -> Tuple[Run, Front]:
        loop = asyncio.get_running_loop()
        r = await loop.run_in_executor(None, lambda: Run(self.control, session, attempt))
        assert self.keys is not None
        f = await Front(self.keys, r).claim()
        self.fronts.append(f)
        return r, f

    def gate_events(self) -> List[dict]:
        try:
            with open(self.events) as f:
                return [json.loads(x) for x in f if x.strip()]
        except FileNotFoundError:
            return []

    async def kill_at_gate(self, timeout: float = 30.0) -> dict:
        await R3.aio_until(lambda: any(e.get("at") == "gate" for e in self.gate_events()), timeout,
                           "keys reached its gate")
        assert self.keys is not None and self.keys.p is not None and self.keys.p.poll() is None, self.keys.err[-8:]
        self.keys.kill()
        for f in self.fronts:
            await f.close()
        self.fronts = []
        (ev,) = [e for e in self.gate_events() if e.get("at") == "gate"]
        return ev

    @property
    def ledger_path(self) -> str:
        return str(self.d / "keys" / "ledger.jsonl")

    def ledger(self) -> List[dict]:
        return B2.ledger_lines(self.ledger_path)

    def rows(self) -> List[dict]:
        p = self.lineage / LN.SINK
        return [canon.parse(x) for x in p.read_bytes().split(b"\n")[:-1]] if p.exists() else []

    def rows_by_triple(self) -> Dict[Triple, List[dict]]:
        out: Dict[Triple, List[dict]] = {}
        for r in self.rows():
            out.setdefault(triple_of(r["source"]), []).append(r)
        return out

    def stop_keys(self) -> int:
        assert self.keys is not None
        rc = self.keys.stop()
        self.keys.cleanup()
        return rc

    async def __aexit__(self, *exc) -> None:
        for f in self.fronts:
            await f.close()
        if self.keys is not None:
            self.keys.cleanup()
        await self.up.close()
        if self.ctl.thread is not None and self.ctl.thread.is_alive():
            self.ctl.stop()
        shutil.rmtree(self.d, ignore_errors=True)
