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
    assert ans == {"id": 7, "ok": True, "ledger_bytes": size,
                   "routes": [["api.example.com", "c1"], ["openrouter.ai", "or1"]], "routes_problem": None}
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


GOOD = {"ledger_bytes": 120, "routes": [["openrouter.ai", "or1"]], "routes_problem": None}


def test_control_relays_keys_status_to_the_operator_only(tmp_path):
    admin = _Admin(GOOD)
    ctl = _control(tmp_path, admin)
    assert _ask(ctl, OPERATOR) == {"id": 9, "ok": True, **GOOD}
    assert admin.calls == [("STATUS", {})]
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
    finally:
        (store / "keys").chmod(0o700)
    assert shown.ok and not shown.warn and "120 bytes" in shown.detail and "as keys reports" in shown.detail
    assert held.neutral and held.detail.startswith("held by keys")
    keys_said_none = D._check_spend_ledger(store, keys_status=lambda: {**GOOD, "ledger_bytes": None})
    assert "120" not in keys_said_none.detail                       # never a number keys did not give


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

    class _Client:
        def __init__(self, path, *, timeout):
            calls.append((str(path), timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def call(self, op, **f):
            assert op == "KEYS-STATUS" and not f
            return dict(GOOD)

    monkeypatch.setattr("levain.slot.control_client.ControlClient", _Client)
    ask = D._keys_status_asker(tmp_path / "run", 5)
    assert ask() == GOOD and ask() == GOOD and len(calls) == 1
    assert calls[0][0] == str(tmp_path / "run" / "control" / "control.sock") and 0 < calls[0][1] <= 5

    class _Down(_Client):
        def __init__(self, path, *, timeout):
            raise Refused("control-down", str(path))

    monkeypatch.setattr("levain.slot.control_client.ControlClient", _Down)
    assert D._keys_status_asker(tmp_path / "run", 5)() is None
    assert D._keys_status_asker(tmp_path / "run", 0)() is None      # no time to ask: not asked


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
    assert told == {"ledger_bytes": size, "routes": [["api.example.com", "c1"], ["openrouter.ai", "or1"]],
                    "routes_problem": None}
    assert gone is None
