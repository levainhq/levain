"""3c-9 session D: keys' read-only `STATUS` on its admin socket, CONTROL's operator verb `KEYS-STATUS` that relays it,
and the two `levain doctor` rows that read "held by keys" for the operator's account (the spend ledger's size and the
dispatch routes' credentials), which now show keys' own report when CONTROL can ask keys, and keep the held wording
when it cannot."""
from __future__ import annotations

import asyncio
import os
import shutil
import socket
import tempfile
from types import SimpleNamespace

import pytest

from levain.broker import keys as K
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import routemaps
from tests.ledger_support import keys_ledger

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="unix sockets")

from tests import test_broker_keys_admin as _ADM   # noqa: E402  (the shared L fixture: base map `r` -> c1, a secret)


# --- keys ---------------------------------------------------------------------------------------------------------

class _Routes:
    """keys' dispatch route maps as `status` reads them: every generation's routes and digests, keys' pin rule (here:
    generation 2 drops `old`), and the ids a pin may name."""

    def __init__(self, ids=frozenset({"d1", "old"})) -> None:
        pol = {"d1": SimpleNamespace(type="brokered", credential="or1", upstream=SimpleNamespace(host="openrouter.ai")),
               "old": SimpleNamespace(type="brokered", credential="gone", upstream=SimpleNamespace(host="x.example"))}
        self._pol, self._ids = pol, ids
        self.maps = {1: SimpleNamespace(routes={"d1": pol["d1"], "old": pol["old"]}, digests={"d1": "a", "old": "b"}),
                     2: SimpleNamespace(routes={"d1": pol["d1"]}, digests={"d1": "c"})}

    def route_ids(self):
        return self._ids

    def policy(self, route, pin):
        if route == "old":
            raise routemaps.Refused("route-not-allowed", "rule 2")
        return self._pol[route]


async def _status_of(d: str, routes=None, args=None) -> tuple:
    """keys' answer to one STATUS on its admin path, and the ledger's size on disk when it was asked."""
    led = keys_ledger(os.path.join(d, "keys", "ledger.jsonl"))
    try:
        k = K.Keys(ledger=led, lib_root=d, routes=routes)
        k._in_loop()
        ans = await k._admin_answer(k.admin_epoch, 7, "STATUS", {} if args is None else args)
        return ans, os.stat(led.path).st_size
    finally:
        await led.aclose(2.0)


#: STATUS's `limits` entry for an OpenRouter credential keys serves but has not read (no refresher in process)
NOT_READ_OR1 = {"credential": "or1", "age_s": None, "limit_microdollars": None, "remaining_microdollars": None,
                "reset": None, "byok": None, "failed": None}


def _lib() -> str:
    d = tempfile.mkdtemp(prefix="ks", dir="/tmp")
    _ADM._frame_lib(d)
    return d


def test_keys_status_answers_the_ledger_size_and_the_routes_it_serves():
    d = _lib()
    try:
        ans, size = asyncio.run(_status_of(d, routes=_Routes()))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert ans == {"id": 7, "ok": True, "ledger_bytes": size, "limits": [NOT_READ_OR1], "limits_truncated": 0,
                   "routes": [["api.example.com", "c1"], ["openrouter.ai", "or1"]], "routes_problem": None,
                   "routes_truncated": 0}
    assert _ADM._SECRET.encode() not in K.encode_admin(ans)        # names only, never a credential's bytes


def test_keys_status_takes_no_argument():
    d = _lib()
    try:
        ans, _size = asyncio.run(_status_of(d, args={"key_limits": True}))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert ans == {"id": 7, "ok": False, "refused": "unknown-key"}


def test_keys_status_names_a_route_set_it_cannot_tell():
    d = _lib()
    try:
        with open(os.path.join(d, "routes", "base.json"), "wb") as f:
            f.write(b"{not json")
        bad_base, _ = asyncio.run(_status_of(d))
        os.unlink(os.path.join(d, "routes", "base.json"))
        unknown_ids, _ = asyncio.run(_status_of(d, routes=_Routes(ids=None)))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert bad_base["ok"] is True and bad_base["routes"] == [] and "base map" in bad_base["routes_problem"]
    assert unknown_ids["routes"] == [] and "dispatch route" in unknown_ids["routes_problem"]


def test_keys_status_over_the_admin_socket_and_a_base_route_a_dispatch_id_shadows():
    # the real admin listener (its peer check admits this process as CONTROL); a base id that is also a dispatch id is
    # refused at use (`_route_policy`'s collision rule), so it is not a route keys serves
    d = _lib()

    async def go() -> dict:
        me = os.getuid()
        led = keys_ledger(os.path.join(d, "keys", "ledger.jsonl"))
        k = K.Keys(single_uid=me, control_uid=me, ledger=led, lib_root=d, routes=_Routes(ids=frozenset({"r"})))
        socks = K.bind_listeners([(os.path.join(d, "f.sock"), None), (os.path.join(d, "a.sock"), None)])
        await k.start(socks[:1], admin=socks[1])
        try:
            r, w = await asyncio.open_unix_connection(os.path.join(d, "a.sock"))
            await _ADM._send(w, 3, "STATUS")
            ans = await _ADM._answer(r)
            w.close()
            return ans
        finally:
            await k.shutdown()
            await led.aclose(2.0)

    try:
        ans = asyncio.run(go())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert ans["ok"] is True and ans["id"] == 3
    assert ans["routes"] == [["openrouter.ai", "or1"]]


# --- CONTROL ------------------------------------------------------------------------------------------------------

OPERATOR, QBROKER = 501, 502


class _Admin:
    def __init__(self, answer=None, refuse=None) -> None:
        self.answer, self.refuse, self.calls = answer, refuse, []
        self.on_eof = None

    def call(self, op, **args):
        self.calls.append((op, args))
        if self.refuse is not None:
            raise Refused(self.refuse, op)
        return dict(self.answer)

    drop_session = front_register = front_arm = call


def _control(tmp_path, admin) -> C.Control:
    (tmp_path / "sessions").mkdir(parents=True, exist_ok=True)
    store = C.StateStore(tmp_path / "sessions")
    return C.Control(store, OPERATOR, qbroker_uid=QBROKER, keys_admin=admin)


def _ask(ctl, uid, **args) -> dict:
    return ctl.handle(C.Conn(uid), {"v": 1, "op": "KEYS-STATUS", "id": 9, **args})


GOOD = {"ledger_bytes": 120, "limits": [], "limits_truncated": 0, "routes": [["openrouter.ai", "or1"]],
        "routes_problem": None, "routes_truncated": 0}


def test_control_relays_keys_status_to_the_operator_only(tmp_path):
    admin = _Admin(GOOD)
    ctl = _control(tmp_path, admin)
    assert _ask(ctl, OPERATOR) == {"id": 9, "ok": True, **GOOD}
    assert admin.calls == [("STATUS", {"connect": False})]
    for uid in (QBROKER, 777, 0):               # every other peer, root included, as for every operator verb
        assert _ask(ctl, uid)["refused"] == "verb-not-allowed-for-peer"
    assert _ask(ctl, OPERATOR, key_limits=True)["refused"] == "unknown-key"
    assert len(admin.calls) == 1


def test_control_keys_status_without_keys_or_with_an_off_shape_answer(tmp_path):
    assert _ask(_control(tmp_path / "a", None), OPERATOR)["refused"] == "keys-unavailable"
    assert _ask(_control(tmp_path / "b", _Admin(refuse="keys-unavailable")), OPERATOR)["refused"] == "keys-unavailable"
    for bad in ({**GOOD, "secret": "sk-x"}, {**GOOD, "ledger_bytes": -1}, {**GOOD, "ledger_bytes": True},
                {**GOOD, "routes": [["h"]]}, {**GOOD, "routes": [["h", 3]]}, {**GOOD, "routes_problem": 4},
                {k: v for k, v in GOOD.items() if k != "routes"}):
        assert _ask(_control(tmp_path / str(len(str(bad))), _Admin(bad)), OPERATOR)["refused"] == "keys-answer"


# --- doctor -------------------------------------------------------------------------------------------------------

def _held_store(tmp_path):
    """A store whose keys/ the operator's account cannot open (chmod 000 as a non-root test user)."""
    store = tmp_path / "L"
    (store / "keys").mkdir(parents=True)
    (store / "keys" / "ledger.jsonl").write_bytes(b"x" * 10)
    (store / "routes").mkdir()
    (store / "keys").chmod(0)
    return store


needs_non_root = pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 directory")


@needs_non_root
def test_doctor_spend_ledger_row_shows_keys_report_or_stays_held(tmp_path):
    from levain import doctor as D

    store = _held_store(tmp_path)
    try:
        shown = D._check_spend_ledger(store, keys_status=lambda: dict(GOOD))
        held = D._check_spend_ledger(store, keys_status=lambda: None)
        keys_said_none = D._check_spend_ledger(store, keys_status=lambda: {**GOOD, "ledger_bytes": None})
    finally:
        (store / "keys").chmod(0o700)
    assert shown.ok and not shown.warn and "120 bytes" in shown.detail and "as keys reports" in shown.detail
    assert held.neutral and held.detail.startswith("held by keys")
    assert "120" not in keys_said_none.detail                       # never a number keys did not give
    assert keys_said_none.neutral and "keys could not tell" in keys_said_none.detail   # not "held": keys answered


@needs_non_root
def test_doctor_dispatch_pairs_come_from_keys_report_or_stay_held(tmp_path):
    from levain import doctor as D

    store = _held_store(tmp_path)
    try:
        pairs, row = D._dispatch_pairs(store, D._clock() + 5, keys_status=lambda: dict(GOOD))
        held_pairs, held_row = D._dispatch_pairs(store, D._clock() + 5, keys_status=lambda: None)
        bad_pairs, bad_row = D._dispatch_pairs(store, D._clock() + 5,
                                               keys_status=lambda: {**GOOD, "routes": [],
                                                                    "routes_problem": "base map unusable"})
    finally:
        (store / "keys").chmod(0o700)
    assert pairs == {("openrouter.ai", "or1")} and row is None
    assert held_pairs == set() and held_row.neutral and held_row.detail.startswith("held by keys")
    assert bad_pairs == set() and bad_row.warn and "base map unusable" in bad_row.detail


def test_doctor_asks_control_once_and_any_failure_is_none(tmp_path, monkeypatch):
    from levain import doctor as D

    calls = []

    def fake(run_dir, deadline):
        calls.append((run_dir, deadline - D._clock()))
        return dict(GOOD)

    monkeypatch.setattr(D, "_ask_keys_status", fake)
    ask = D._keys_status_asker(tmp_path / "run", 5)
    assert ask() == GOOD and ask() == GOOD and len(calls) == 1
    assert calls[0][0] == tmp_path / "run" and 0 < calls[0][1] <= 5
    monkeypatch.undo()
    assert D._keys_status_asker(tmp_path / "run", 5)() is None      # no CONTROL socket there
    assert D._ask_keys_status(tmp_path / "run", D._clock()) is None  # no time left: nothing is asked


# --- the whole path ------------------------------------------------------------------------------------------------

@needs_non_root
def test_doctor_reads_keys_status_through_a_real_control_and_a_real_keys():
    """doctor's own ask (`_ask_keys_status`) over CONTROL's real socket (ControlServer, the kernel's peer uid: this
    process is the operator), CONTROL's real admin client, and keys' real admin listener (its peer check admits this
    process as CONTROL); then keys stops, and the same ask is None (the row keeps its held wording)."""
    import threading
    from pathlib import Path
    from levain import doctor as D

    d = Path(_lib())
    (d / "run" / "control").mkdir(parents=True)
    (d / "sessions").mkdir()
    loop = asyncio.new_event_loop()
    started = threading.Event()
    box: dict = {}

    async def keys_main() -> None:
        me = os.getuid()
        box["led"] = keys_ledger(str(d / "keys" / "ledger.jsonl"))
        k = box["k"] = K.Keys(single_uid=me, control_uid=me, ledger=box["led"], lib_root=str(d),
                              routes=_Routes(ids=frozenset()))
        socks = K.bind_listeners([(str(d / "f.sock"), None), (str(d / "a.sock"), None)])
        await k.start(socks[:1], admin=socks[1])
        started.set()

    t = threading.Thread(target=lambda: (loop.run_until_complete(keys_main()), loop.run_forever()), daemon=True)
    t.start()
    assert started.wait(10)
    admin = C.KeysAdmin(d / "a.sock", answer_s=1.0, window_s=3.0)
    control = C.Control(C.StateStore(d / "sessions"), os.getuid(), keys_admin=admin)
    control.start()
    server = C.ControlServer(control, d / "run" / "control" / "control.sock")
    server.start()
    try:
        admin.call("STATUS")   # CONTROL's wire to keys is up (on an install the attach RELOAD or a MINT opens it)
        told = D._ask_keys_status(d / "run", D._clock() + 5)
        size = os.stat(d / "keys" / "ledger.jsonl").st_size

        async def stop_keys() -> None:
            await box["k"].shutdown()
            await box["led"].aclose(2.0)
        asyncio.run_coroutine_threadsafe(stop_keys(), loop).result(10)
        gone = D._ask_keys_status(d / "run", D._clock() + 5)
    finally:
        server.stop()
        control.stop()
        loop.call_soon_threadsafe(loop.stop)
        t.join(10)
        shutil.rmtree(d, ignore_errors=True)
    assert told == {"ledger_bytes": size, "limits": [NOT_READ_OR1], "limits_truncated": 0,
                    "routes": [["api.example.com", "c1"], ["openrouter.ai", "or1"]],
                    "routes_problem": None, "routes_truncated": 0}
    assert gone is None


# --- L3 r1 fixes (K1-K4) -------------------------------------------------------------------------------------------

class _ManyRoutes(_Routes):
    """codex's K1 construction: 256 dispatch routes, each a 251-character host and a 48-character credential name."""

    def __init__(self, n: int = 256) -> None:
        self._ids = frozenset(f"d{i}" for i in range(n))
        self._pol = {f"d{i}": SimpleNamespace(type="brokered", credential=f"{i:03d}".ljust(48, "c"),
                                              upstream=SimpleNamespace(host=f"{i:03d}".ljust(251, "h")))
                     for i in range(n)}
        self.maps = {1: SimpleNamespace(routes=dict(self._pol), digests={r: "a" for r in self._pol})}

    def policy(self, route, pin):
        return self._pol[route]


def test_keys_status_answer_stays_inside_its_budget_and_says_what_it_left_out():
    # K1: unbounded, this answer is past the admin wire's frame cap, and CONTROL's reader closing the shared admin
    # connection on it revokes every session
    many = _ManyRoutes()
    whole = [[p.upstream.host, p.credential] for p in many._pol.values()]
    assert len(K.canon.canonical_bytes(whole)) > K.ADMIN_FRAME_MAX
    d = _lib()
    try:
        ans, _size = asyncio.run(_status_of(d, routes=many))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert K.STATUS_ANSWER_MAX < K.ADMIN_FRAME_MAX
    assert len(K.encode_admin(ans)) - 4 <= K.STATUS_ANSWER_MAX
    shown = len(ans["routes"])
    assert 0 < shown < 257 and ans["routes_truncated"] == 257 - shown      # 256 dispatch pairs + the base map's c1
    assert ans["routes"] == sorted(ans["routes"])                          # the kept ones are the sorted list's head
    relayed = C._keys_status_answer({k: v for k, v in ans.items() if k not in ("id", "ok")})
    assert len(C.encode_frame({**relayed, "v": 1, "id": 2 ** 53 - 1, "ok": True})) <= C.FRAME_MAX


@needs_non_root
def test_doctor_says_when_keys_left_routes_out(tmp_path):
    from levain import doctor as D

    store = _held_store(tmp_path)
    try:
        pairs, row = D._dispatch_pairs(store, D._clock() + 5,
                                       keys_status=lambda: {**GOOD, "routes_truncated": 3})
    finally:
        (store / "keys").chmod(0o700)
    assert pairs == {("openrouter.ai", "or1")} and row.warn and "3 more" in row.detail


@needs_non_root
def test_doctor_takes_keys_route_set_whole_when_it_cannot_read_keys_head(tmp_path):
    # K2: base.json's `r` names c1, but `r` is also a dispatch id, so keys refuses it and its STATUS leaves c1 out: the
    # operator's own read of base.json must not add it back
    from levain import doctor as D

    store = tmp_path / "L"
    _ADM._frame_lib(str(store))
    (store / "keys" / "ledger.jsonl").write_bytes(b"")
    (store / "keys").chmod(0)
    try:
        rows = D._check_invoice_ceiling(store, None, keys_status=lambda: dict(GOOD))
    finally:
        (store / "keys").chmod(0o700)
    names = [r.name for r in rows]
    assert not any("c1" in n for n in names), names
    assert "invoice ceiling (or1)" in names


def test_doctor_keys_status_ask_has_one_total_deadline(tmp_path):
    # K3: a CONTROL that answers one byte at a time, each inside a per-operation timeout, must not hold the ask past
    # its one deadline
    import pathlib
    import threading
    import time as _t
    from levain import doctor as D

    run = tempfile.mkdtemp(prefix="kd", dir="/tmp")
    os.makedirs(os.path.join(run, "control"))
    path = os.path.join(run, "control", "control.sock")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    stop = threading.Event()

    def serve() -> None:
        c, _ = srv.accept()
        with c:
            C.read_frame(c)
            frame = C.encode_frame({**GOOD, "id": 1, "ok": True})
            for i in range(len(frame)):
                if stop.wait(0.2):
                    return
                try:
                    c.sendall(frame[i:i + 1])
                except OSError:
                    return

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        t0 = _t.monotonic()
        told = D._ask_keys_status(pathlib.Path(run), D._clock() + 1.0)
        took = _t.monotonic() - t0
    finally:
        stop.set()
        t.join(5)
        srv.close()
        shutil.rmtree(run, ignore_errors=True)
    assert told is None and took < 1.5


def test_control_keys_status_never_opens_the_admin_connection(tmp_path):
    # K4 (complement MED 1): an operator read must not open (or back off) CONTROL's authority wire to keys
    admin = C.KeysAdmin(tmp_path / "no-keys.sock", answer_s=0.5, window_s=1.0)
    ctl = _control(tmp_path, admin)
    assert _ask(ctl, OPERATOR)["refused"] == "keys-unavailable"
    assert not admin.connected() and admin._retry_at == 0.0          # no connect was tried, so no backoff was set


def test_doctor_slot_keys_rows_for_an_unknown_host_are_none():
    from levain import doctor as D

    assert D._check_slot_keys(host="plan9") == []


def test_doctor_root_read_leaves_out_a_base_route_a_dispatch_id_shadows(tmp_path, monkeypatch):
    # K2's class on the path that reads keys' head itself: base `anthropic` is also a dispatch id, so keys refuses it
    from tests import test_doctor_3c9_rows as T
    from levain.slot import vendor_routes as VR

    store = T._store(tmp_path)
    T._base(store, [VR.anthropic_route()])
    T._cred(store, "orkey", "openrouter.ai")
    (store / "keys").mkdir(exist_ok=True)
    (store / "keys" / "routemap.head").write_text("x")
    checked = T._checked(1, {"anthropic": T._FakeRoute("openrouter.ai", "orkey")})
    monkeypatch.setattr(routemaps.KeysRoutes, "load", lambda self: (T._head(1), {1: checked}, None))
    rows = T._rows(store, fetch=T._ok_fetch())
    assert "invoice ceiling (orkey)" in rows
    assert not any("anthropic" in n for n in rows), sorted(rows)


# --- D1: keys' own OpenRouter limit refresher ----------------------------------------------------------------------

OR = "openrouter.ai"


@pytest.fixture(scope="module")
def or_cert(tmp_path_factory):
    """A certificate for openrouter.ai, which the client context below trusts (the real host is never reached)."""
    import subprocess
    exe = shutil.which("openssl")
    if exe is None:
        pytest.skip("no openssl to make the fake OpenRouter's certificate")
    d = tmp_path_factory.mktemp("orcert")
    subprocess.run([exe, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(d / "k.pem"), "-out",
                    str(d / "c.pem"), "-days", "2", "-subj", "/CN=" + OR, "-addext", "subjectAltName=DNS:" + OR],
                   check=True, capture_output=True)
    return d / "c.pem", d / "k.pem"


def or_cred(lib: str, name: str, hosts=(OR,)) -> str:
    """A static credential `name` under `lib`, its secret `sk-<name>-SECRET` (the fake answers by it)."""
    import json
    secret = f"sk-{name}-SECRET"
    with open(os.path.join(lib, "keys", "cred", name + ".json"), "w") as f:
        json.dump({"format": "levain-keys-credential/1", "name": name, "rev": "0" * 32, "hosts": list(hosts),
                   "inject": {"header": "authorization", "scheme": "Bearer"}, "kind": "static", "secret": secret}, f)
    return secret


class PairRoutes:
    """keys' dispatch maps as `served_pairs` reads them: one generation, route `d<i>` -> (host, credential)."""

    def __init__(self, pairs) -> None:
        self._pol = {f"d{i}": SimpleNamespace(type="brokered", credential=c, upstream=SimpleNamespace(host=h))
                     for i, (h, c) in enumerate(pairs)}
        self.maps = {1: SimpleNamespace(routes=dict(self._pol), digests={r: "a" for r in self._pol})}

    def route_ids(self):
        return frozenset(self._pol)

    def policy(self, route, pin):
        return self._pol[route]


def key_body(limit, remaining, reset=None, byok=True) -> bytes:
    import json
    return json.dumps({"data": {"label": "sk-or-v1-abc...", "limit": limit, "limit_remaining": remaining,
                                "limit_reset": reset, "include_byok_in_limit": byok, "usage": 1.5}}).encode()


class FakeOpenRouter:
    """`GET /api/v1/key` over TLS on loopback: `answers[secret] = (delay_s, status, body)`, picked by the request's
    Bearer value; every request head is kept."""

    def __init__(self, answers) -> None:
        self.answers, self.heads, self.port, self.server = answers, [], 0, None

    async def handle(self, reader, writer) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            self.heads.append(head)
            auth = [ln.split(b":", 1)[1].strip() for ln in head.split(b"\r\n")
                    if ln.lower().startswith(b"authorization")]
            delay, status, body = self.answers[auth[0].split(b" ", 1)[1].decode()]
            if delay:
                await asyncio.sleep(delay)
            writer.write(b"HTTP/1.1 %d X\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: "
                         b"close\r\n\r\n" % (status, len(body)) + body)
            await writer.drain()
            writer.close()
        except Exception:
            pass


async def with_fake_openrouter(or_cert, monkeypatch, answers, body):
    """Run `body(fake, picks)` with keys' upstream pointed at a FakeOpenRouter: the client context trusts its
    certificate, `pick_address` answers loopback and records every host asked (the address check is not what this
    tests), and `LIMIT_PORT` is the fake's port."""
    import ssl
    from levain.broker import upstream as U

    sctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    sctx.load_cert_chain(str(or_cert[0]), str(or_cert[1]))
    fake = FakeOpenRouter(answers)
    fake.server = await asyncio.start_server(fake.handle, "127.0.0.1", 0, ssl=sctx)
    monkeypatch.setattr(U, "LIMIT_PORT", fake.server.sockets[0].getsockname()[1])
    ctx = ssl.create_default_context(cafile=str(or_cert[0]))
    monkeypatch.setattr(U, "_tls_context", lambda: ctx)
    picks = []

    async def pick(host, port):
        picks.append(host)
        return "127.0.0.1"
    monkeypatch.setattr(U, "pick_address", pick)
    try:
        return await body(fake, picks)
    finally:
        fake.server.close()
        await fake.server.wait_closed()


def _limit_lib(names, hosts=None) -> str:
    d = _lib()
    for n in names:
        or_cred(d, n, hosts=(hosts or {}).get(n, (OR,)))
    return d


def test_keys_limit_refresher_reads_each_openrouter_key_in_microdollars(or_cert, monkeypatch):
    names = ["or-lim", "or-none", "or-401", "or-inf", "or-neg", "or-huge", "or-shape", "or-period", "or-vast",
             "or-neglim", "or-overdrawn", "or-byokstr", "or-z"]
    d = _limit_lib(names)
    answers = {f"sk-{n}-SECRET": (0, 200, b) for n, b in (
        ("or-lim", key_body(12.5, 3.25)), ("or-none", key_body(None, None)), ("or-inf", b'{"data": {"limit": '
         b'Infinity, "limit_remaining": 1}}'), ("or-neg", key_body(5, -0.5)), ("or-huge", key_body(1e10, 1)),
        ("or-shape", b'{"limit": 5}'), ("or-period", key_body(20, 20, "daily", False)),
        ("or-vast", key_body(1e305, 1)),                  # finite, but * 10**6 overflows a float (L3 r1 E1)
        ("or-neglim", key_body(-1, 1)), ("or-overdrawn", key_body(5, -1e10)), ("or-byokstr", key_body(5, 5, None,
                                                                                                        "false")),
        ("or-z", key_body(7, 7)))}
    answers["sk-or-401-SECRET"] = (0, 401, b'{"error": "no"}')

    async def body(fake, picks):
        k = K.Keys(lib_root=d, routes=PairRoutes([(OR, n) for n in names]))
        k._in_loop()
        await k.limit_pass()
        return k.status(), fake.heads, picks

    try:
        st, heads, picks = asyncio.run(with_fake_openrouter(or_cert, monkeypatch, answers, body))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    got = {e["credential"]: e for e in st["limits"]}
    assert sorted(got) == sorted(names) and st["limits_truncated"] == 0
    lim = got["or-lim"]
    assert (lim["limit_microdollars"], lim["remaining_microdollars"], lim["reset"], lim["byok"], lim["failed"]) == \
        (12_500_000, 3_250_000, None, True, None) and type(lim["age_s"]) is int
    assert got["or-none"]["limit_microdollars"] is None and got["or-none"]["failed"] is None
    assert got["or-none"]["age_s"] is not None                         # read well: no limit is a reading, not a failure
    assert (got["or-period"]["reset"], got["or-period"]["byok"]) == ("daily", False)
    neg = got["or-neg"]                                                # C1: a negative remaining is a reading
    assert (neg["limit_microdollars"], neg["remaining_microdollars"], neg["failed"]) == (5_000_000, -500_000, None)
    assert got["or-z"]["limit_microdollars"] == 7_000_000              # read after or-vast: the pass went on
    for n, why in (("or-401", "http-4xx"), ("or-inf", "bad-value"), ("or-huge", "bad-value"),
                   ("or-shape", "bad-body"), ("or-vast", "bad-value"), ("or-neglim", "bad-value"),
                   ("or-overdrawn", "bad-value"), ("or-byokstr", "bad-value")):
        assert got[n]["failed"] == why and got[n]["age_s"] is None and got[n]["limit_microdollars"] is None, n
    assert picks == [OR] * len(names)                                  # c1 (api.example.com) opened nothing
    for h in heads:
        lines = h.split(b"\r\n")
        assert lines[0] == b"GET /api/v1/key HTTP/1.1" and b"host: openrouter.ai" in lines
        assert not any(ln.lower().startswith((b"content-length", b"transfer-encoding")) for ln in lines)
    assert {[ln for ln in h.split(b"\r\n") if ln.startswith(b"authorization")][0] for h in heads} == \
        {f"authorization: Bearer sk-{n}-SECRET".encode() for n in names}
    wire = K.encode_admin({"id": 1, "ok": True, **st})
    assert not any(f"sk-{n}-SECRET".encode() in wire for n in names)  # numbers only, never a credential's bytes
    assert C._keys_status_answer(st)["limits"] == st["limits"]         # CONTROL takes the shape keys sends


def test_keys_limit_read_is_bounded_and_keeps_the_last_good_read(or_cert, monkeypatch):
    import time as _t
    from levain.broker import upstream as U

    d = _limit_lib(["or-a", "or-b"], hosts={"or-b": ("api.example.com",)})
    answers = {"sk-or-a-SECRET": (0, 200, key_body(10, 4))}

    async def body(fake, picks):
        k = K.Keys(lib_root=d, routes=PairRoutes([(OR, "or-a"), (OR, "or-b"), (OR, "or-missing")]))
        k._in_loop()
        k.key_limit_s = 0.5
        await k.limit_pass()
        first = k.status()["limits"]
        answers["sk-or-a-SECRET"] = (3, 200, key_body(10, 4))         # answers after 3 s: past keys' deadline
        t0 = _t.monotonic()
        await k.limit_pass()
        slow = _t.monotonic() - t0
        late = k.status()["limits"]

        async def black_hole(addr, port):
            await asyncio.sleep(3600)
        monkeypatch.setattr(U, "_connect", black_hole)                 # the connect itself stalls
        t0 = _t.monotonic()
        await k.limit_pass()
        stalled = _t.monotonic() - t0
        return first, late, k.status()["limits"], slow, stalled, list(picks)

    try:
        first, late, stall, slow, stalled, picks = asyncio.run(with_fake_openrouter(or_cert, monkeypatch, answers,
                                                                                      body))
    finally:
        shutil.rmtree(d, ignore_errors=True)
    by = lambda rows: {e["credential"]: e for e in rows}   # noqa: E731
    assert by(first)["or-a"]["limit_microdollars"] == 10_000_000 and by(first)["or-a"]["failed"] is None
    assert by(first)["or-b"]["failed"] == "host-mismatch" and by(first)["or-missing"]["failed"] == "no-credential"
    for rows, took in ((late, slow), (stall, stalled)):
        a = by(rows)["or-a"]
        assert a["failed"] == "timeout" and took < 2.0                  # bounded by key_limit_s, not upstream's own
        assert a["limit_microdollars"] == 10_000_000 and type(a["age_s"]) is int   # the last good read, with its age
    assert picks == [OR] * 3                                            # or-b and or-missing never opened anything


def test_keys_status_opens_no_socket_while_the_limit_read_is_stalled(or_cert, monkeypatch):
    """D3's premise, made true by D1: no network inside a CONTROL answer. The refresher's connect is stalled; STATUS
    answers at once from memory and opens no socket; shutdown cancels the stalled read."""
    import socket as _s
    from levain.broker import upstream as U

    d = _limit_lib(["or-a"])

    async def run():
        entered = asyncio.Event()

        async def black_hole(addr, port):
            entered.set()
            await asyncio.sleep(3600)
        monkeypatch.setattr(U, "_connect", black_hole)

        async def pick(host, port):
            return "127.0.0.1"
        monkeypatch.setattr(U, "pick_address", pick)
        led = keys_ledger(os.path.join(d, "keys", "ledger.jsonl"))
        k = K.Keys(ledger=led, lib_root=d, routes=PairRoutes([(OR, "or-a")]))
        k._in_loop()
        k.start_limits()
        await asyncio.wait_for(entered.wait(), 5)
        opened = []
        real = _s.socket

        class Recording(real):
            def __init__(self, *a, **kw):
                opened.append(a)
                super().__init__(*a, **kw)
        monkeypatch.setattr(_s, "socket", Recording)
        ans = await asyncio.wait_for(k._admin_answer(k.admin_epoch, 3, "STATUS", {}), 1.0)
        monkeypatch.setattr(_s, "socket", real)
        task = k._limit_task
        try:
            await k.shutdown()
        finally:
            await led.aclose(2.0)
        return ans, opened, task.done()   # read inside the loop: asyncio.run cancels whatever is left at its end

    try:
        ans, opened, cancelled = asyncio.run(run())
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert ans["ok"] is True and opened == []
    assert [e["credential"] for e in ans["limits"]] == ["or-a"] and ans["limits"][0]["age_s"] is None
    assert cancelled                                                    # (2b) cancelled it


def test_keys_status_budget_gives_the_limits_the_room_first():
    names = [f"or{i:03d}" + "n" * 60 for i in range(256)]
    d = _lib()
    try:
        k = K.Keys(lib_root=d, routes=PairRoutes([(OR, n) for n in names]))
        st = k.status()
    finally:
        shutil.rmtree(d, ignore_errors=True)
    assert len(K.encode_admin({"id": 2 ** 53 - 1, "ok": True, **st})) - 4 <= K.STATUS_ANSWER_MAX
    kept = len(st["limits"])
    assert 0 < kept < 256 and st["limits_truncated"] == 256 - kept
    assert [e["credential"] for e in st["limits"]] == sorted(names)[:kept]
    assert st["routes"] == [] and st["routes_truncated"] == 257       # a cut list ends the room: routes come after


def test_control_refuses_a_limits_entry_off_its_shape(tmp_path):
    ok = {"credential": "or1", "age_s": 5, "limit_microdollars": 1, "remaining_microdollars": 0, "reset": None,
          "byok": True, "failed": None}
    assert _ask(_control(tmp_path / "ok", _Admin({**GOOD, "limits": [ok]})), OPERATOR)["limits"] == [ok]
    assert C._MICRODOLLARS_MAX == K.LIMIT_MICRODOLLARS_MAX
    over = {**ok, "remaining_microdollars": -K.LIMIT_MICRODOLLARS_MAX, "failed": "internal"}   # C1, E2's token
    assert _ask(_control(tmp_path / "over", _Admin({**GOOD, "limits": [over]})), OPERATOR)["limits"] == [over]
    bads = [{**ok, "age_s": 1.5}, {**ok, "age_s": -1}, {**ok, "limit_microdollars": True},
            {**ok, "remaining_microdollars": -K.LIMIT_MICRODOLLARS_MAX - 1}, {**ok, "limit_microdollars": -1},
            {**ok, "remaining_microdollars": K.LIMIT_MICRODOLLARS_MAX + 1}, {**ok, "byok": "yes"}, {**ok, "reset": 3}, {**ok, "failed": ""},
            {**ok, "credential": ""}, {**ok, "secret": "sk-x"}, {k: v for k, v in ok.items() if k != "failed"}]
    for i, bad in enumerate(bads):
        assert _ask(_control(tmp_path / f"b{i}", _Admin({**GOOD, "limits": [bad]})), OPERATOR)["refused"] == \
            "keys-answer", bad
    for i, bad in enumerate([{**GOOD, "limits": {}}, {**GOOD, "limits_truncated": -1},
                             {k: v for k, v in GOOD.items() if k != "limits"}]):
        assert _ask(_control(tmp_path / f"t{i}", _Admin(bad)), OPERATOR)["refused"] == "keys-answer"


def test_keys_limit_pass_one_credentials_defect_never_starves_the_rest(caplog):
    """L3 r1 E2: an unexpected exception reading one credential is that credential's `internal` failure (its last
    good read kept, with its age), a WARNING naming it and the exception's type, and the pass reads every other."""
    import logging
    d = _lib()
    try:
        k = K.Keys(lib_root=d, routes=PairRoutes([(OR, n) for n in ("or-a", "or-b", "or-c")]))
        broken: set = set()

        async def read(name):
            if name in broken:
                raise RuntimeError("a defect")
            return (1_000_000, 500_000, None, True)
        k._read_limit = read

        async def run():
            k._in_loop()
            await k.limit_pass()
            broken.update({"or-a", "or-b"})
            with caplog.at_level(logging.WARNING):
                await k.limit_pass()
            return k.status()["limits"]
        rows = {e["credential"]: e for e in asyncio.run(run())}
    finally:
        shutil.rmtree(d, ignore_errors=True)
    for n in ("or-a", "or-b"):
        assert rows[n]["failed"] == "internal" and rows[n]["limit_microdollars"] == 1_000_000, rows[n]
        assert type(rows[n]["age_s"]) is int
        assert any(n in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records), n
    assert rows["or-c"]["failed"] is None and "internal" in K.LIMIT_FAILURES


def test_keys_limit_read_a_kernel_connect_timeout_is_unreachable_and_keys_deadline_is_timeout(monkeypatch):
    """L3 r1 E4: on 3.11+ `asyncio.TimeoutError` is `TimeoutError`, an OSError: a kernel ETIMEDOUT from the connect
    is `unreachable`; only keys' own `key_limit_s` running out is `timeout`."""
    import errno
    from levain.broker import upstream as U
    d = _limit_lib(["or-k", "or-s"])

    async def get(header):
        if b"or-k" in header[1]:
            raise OSError(errno.ETIMEDOUT, "Operation timed out")
        await asyncio.sleep(3600)
    monkeypatch.setattr(U, "limit_get", get)

    async def run():
        k = K.Keys(lib_root=d, routes=PairRoutes([(OR, "or-k"), (OR, "or-s")]))
        k._in_loop()
        k.key_limit_s = 0.2
        return await k._read_limit("or-k"), await k._read_limit("or-s")
    try:
        assert isinstance(OSError(errno.ETIMEDOUT, "x"), TimeoutError)   # the premise: the kernel's is a TimeoutError
        assert asyncio.run(run()) == ("unreachable", "timeout")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_keys_limit_read_a_timeout_raised_inside_the_call_is_unreachable_not_keys_deadline(monkeypatch):
    """The 3.9 port of the bound (`wait_for`, not `asyncio.timeout`): an `asyncio.TimeoutError` the call itself raises
    (an inner `wait_for` running out) is `unreachable`, the same as the kernel's; only `key_limit_s` is `timeout`."""
    from levain.broker import upstream as U
    d = _limit_lib(["or-i"])

    async def get(header):
        raise asyncio.TimeoutError()
    monkeypatch.setattr(U, "limit_get", get)

    async def run():
        k = K.Keys(lib_root=d, routes=PairRoutes([(OR, "or-i")]))
        k._in_loop()
        k.key_limit_s = 5.0
        return await k._read_limit("or-i")
    try:
        assert asyncio.run(run()) == "unreachable"
    finally:
        shutil.rmtree(d, ignore_errors=True)
