"""S1 3c-4: the dispatch route maps (3c design r3 §2.3 as R4 §2 amends it). Every signature here is a real `ssh-keygen`
ed25519 key's, verified by the production verifier (`levain.authority.verify_link_sig` through `/usr/bin/ssh-keygen`).

CONTROL's half (`ControlRoutes`, `ROUTE-SUBMIT` through `Control.handle`), keys' half (`Keys.reload` on its own loop
and `Keys.dispatch_policy(route, pin)`), and the three activation fault gates (a ROUTE-SUBMIT SIGKILLed at each point
in a child, then CONTROL through `control.serve` and keys as a child process: the start pass finishes the activation,
and keys acks the head through the attach-time RELOAD). The ticket-dependent rows (`test_mint_waits_for_keys_reload_ack`,
`test_pin_holds_for_session_life` through a real ticket) are 3c-5's."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from levain.broker import keys as K
from levain.broker import norm
from levain.contracts import canon
from levain.slot import Refused
from levain.slot import control as C
from levain.slot import routemaps as RM

KEYGEN = "/usr/bin/ssh-keygen"
ROOT = Path(__file__).resolve().parents[1]
STORE_ID = "ab" * 16
OTHER_STORE = "cd" * 16
OP_UID = 501
DISPATCH_UID = 7001
STAMP = "2026-10-23T12:00:00.000000Z"            # `utc_stamp`'s one format, as dispatch emits it
PRICE = {"in": 2_000_000, "out": 8_000_000, "cache_write": 2_000_000, "cache_read": 500_000,
         "default_output": 4096, "overhead_tokens": 64, "image_tokens": 1600, "source": "https://example.test/o"}

pytestmark = pytest.mark.skipif(not os.path.exists(KEYGEN), reason="no /usr/bin/ssh-keygen")


# ---- fixtures: a real key pair, a store, items ------------------------------------------------------------------

class Signer:
    """One ed25519 key pair made by ssh-keygen; `sign(raw)` is `ssh-keygen -Y sign` in the config namespace."""

    def __init__(self, d: Path, name: str) -> None:
        self.key = d / name
        subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.key)], check=True)
        self.pub = (d / (name + ".pub")).read_text().strip()
        out = subprocess.run([KEYGEN, "-l", "-E", "sha256", "-f", str(self.key) + ".pub"], check=True,
                             capture_output=True, text=True).stdout
        self.fp = out.split()[1]
        self.n = 0

    def sign(self, raw: bytes) -> str:
        self.n += 1
        f = self.key.parent / f"{self.key.name}.msg{self.n}"
        f.write_bytes(raw)
        subprocess.run([KEYGEN, "-q", "-Y", "sign", "-f", str(self.key), "-n", RM.CONFIG_NAMESPACE, str(f)],
                       check=True, capture_output=True)
        return Path(str(f) + ".sig").read_text()


def route(rid: str = "glm", *, host: str = "openrouter.ai", typ: str = "brokered", codec: str = "openai-chat",
          models=None) -> dict:
    if typ != "brokered":
        pol = {"route": rid, "type": typ, "hosts": [host]}
    else:
        pol = {"route": rid, "type": "brokered", "upstream": {"scheme": "https", "host": host, "base_path": "/api"},
               "requests": {"templates": [{"method": "POST", "path": "/v1/chat/completions"}], "headers": {},
                            "body_cap": 1048576, "body_codec": codec},
               "credential": "or-key"}
    return {"policy": pol, "provider": "openrouter",
            "models": models or [{"requested": "z-ai/glm-5.2", "backend": "zhipu/glm-5.2", "price": PRICE}],
            "body_policy": {"provider_only": ["z-ai"]} if host == "openrouter.ai" else {},
            "retention_claim": "recorded, never a guarantee"}


def item(gen: int, routes=None, *, store_id: str = STORE_ID, kind: str = "routemap") -> dict:
    return {"format": RM.CONFIG_FORMAT, "store_id": store_id, "generation": gen,
            "prev": "" if gen == 1 else "sha256:" + "0" * 64, "issued_at": STAMP, "kind": kind,
            "body": {"format": "levain-dispatch-routemap/3", "routes": [route()] if routes is None else routes}}


class Store:
    """A lib root `L`: `routes/` (trust, a base map with one egress id, `dispatch/`) and `keys/`."""

    def __init__(self, d: Path, signer: Signer, store_id: str = STORE_ID) -> None:
        self.L = d
        self.routes = d / "routes"
        self.dispatch = self.routes / "dispatch"
        self.dispatch.mkdir(parents=True)
        (d / "keys").mkdir(mode=0o700, exist_ok=True)
        (self.routes / "base.json").write_bytes(canon.canonical_bytes(
            {"format": "levain-routes-base/2", "routes": [{"route": "pypi", "type": "egress", "hosts": ["pypi.org"]}]}))
        (self.routes / "trust.pub").write_text(signer.pub + "\n")
        (self.routes / "trust.json").write_bytes(canon.canonical_bytes(
            {"format": RM.TRUST_FORMAT, "store_id": store_id, "fingerprint": signer.fp}))
        self.signer = signer

    def signed(self, it: dict, signer: Signer = None) -> tuple:
        return it, (signer or self.signer).sign(canon.canonical_bytes(it))

    def head(self) -> dict:
        return canon.parse((self.dispatch / "head").read_bytes())

    def current(self) -> int:
        return int((self.dispatch / "current").read_bytes())

    def keys_head(self) -> dict:
        return canon.parse((self.L / "keys" / "routemap.head").read_bytes())

    def gen_files(self) -> list:
        return sorted(p.name for p in self.dispatch.iterdir() if p.name.endswith((".json", ".sig")))


@pytest.fixture
def st():
    d = Path(tempfile.mkdtemp(prefix="c4", dir="/tmp"))
    try:
        yield Store(d, Signer(d, "op"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def refusal(fn, *a, **kw) -> str:
    try:
        fn(*a, **kw)
    except Refused as e:
        return e.name
    raise AssertionError("not refused")


def submit(cr: RM.ControlRoutes, it: dict, sig: str, reloads=None) -> int:
    return cr.submit(it, sig, (reloads.append if reloads is not None else (lambda h: None)))


def sha(it: dict) -> str:
    return RM.item_digest(canon.canonical_bytes(it))


def keys_of(st: Store) -> K.Keys:
    k = K.Keys(lib_root=str(st.L))
    k.routes.install(*k.routes.load())
    return k


#: a RELOAD `keep` naming every generation these tests make, so nothing is pruned (H5's pruning has its own tests)
KEEP_ALL = frozenset(range(1, 64))


def reload(k: K.Keys, gen: int, digest: str, keep=KEEP_ALL) -> dict:
    async def run():
        k._in_loop()
        return await k.reload(k.admin_epoch, gen, digest, keep=keep)
    return asyncio.run(run())


def acked(st: "Store", gen: int, digest: str) -> dict:
    """keys' RELOAD ack: the generation it now serves and its `served` list, which is what keys' durable head lists."""
    return {"generation": gen, "item_sha256": digest, "served": [e["generation"] for e in st.keys_head()["served"]]}


def dispatch_refusal(k: K.Keys, rid: str, pin) -> str:
    try:
        k.dispatch_policy(rid, pin)
    except norm.Refused as e:
        return f"{e.name}|{e.detail}"
    raise AssertionError("served")


# ---- CONTROL's accept ---------------------------------------------------------------------------------------------

def test_unsigned_map_refused(st):
    cr = RM.ControlRoutes(st.routes)
    it, sig = st.signed(item(1))
    other = Signer(st.L, "intruder")
    flipped = dict(it, issued_at="2026-10-23T12:00:01.000000Z")   # one byte changed after signing
    _, other_sig = st.signed(it, other)                             # a valid signature by a key that is not trust.pub
    assert refusal(submit, cr, flipped, sig) == "routemap-signature"
    assert refusal(submit, cr, it, other_sig) == "routemap-signature"
    assert refusal(submit, cr, it, sig.replace("A", "B", 1)) == "routemap-signature"
    assert refusal(submit, cr, it, "") == "routemap-signature"
    assert st.gen_files() == [] and not (st.dispatch / "head").exists()
    assert submit(cr, it, sig) == 1                                 # the untouched pair is accepted


def test_older_generation_refused(st):
    cr = RM.ControlRoutes(st.routes)
    one, two = st.signed(item(1)), st.signed(item(2))
    reloads: list = []
    assert submit(cr, *two, reloads) == 2
    assert refusal(submit, cr, *one) == "routemap-generation"
    # against CONTROL's own head file, even with the active generation's files deleted
    (st.dispatch / "2.json").unlink()
    (st.dispatch / "2.sig").unlink()
    assert refusal(submit, cr, *one) == "routemap-generation"
    assert st.head()["generation"] == 2 and st.current() == 2
    assert reloads == [{"generation": 2, "item_sha256": sha(two[0])}]


def test_same_generation_other_digest_refused(st):
    cr = RM.ControlRoutes(st.routes)
    a = st.signed(item(2))
    b = st.signed(item(2, [route(host="api.z.ai")]))
    reloads: list = []
    assert submit(cr, *a, reloads) == 2
    head_bytes = (st.dispatch / "head").read_bytes()
    assert refusal(submit, cr, *b) == "routemap-generation"
    # a resubmission is the convergent repair: the same bytes again, and the RELOAD re-sent unless keys serves the head
    assert cr.submit(*a, reloads.append, lambda: reloads[-1]) == 2
    assert (st.dispatch / "head").read_bytes() == head_bytes and len(reloads) == 1
    assert submit(cr, *a, reloads) == 2 and len(reloads) == 2
    # keys: the file swapped under it is refused at RELOAD, and at start
    k = keys_of(st)
    assert reload(k, 2, sha(a[0])) == acked(st, 2, sha(a[0]))
    (st.dispatch / "2.json").write_bytes(canon.canonical_bytes(b[0]))
    (st.dispatch / "2.sig").write_text(b[1])
    assert reload(k, 2, sha(a[0])) == {"refused": "routemap-invalid:item_sha256"}
    assert dispatch_refusal(k, "glm", RM.Pin(2, RM.policy_digest(route()["policy"]))).startswith("route-not-allowed|no ")
    assert reload(k, 2, sha(b[0])) == {"refused": "routemap-generation"}   # other digest at keys' own generation
    k2 = keys_of(st)
    assert k2.routes.problem == "routemap-head-unverifiable" and k2.routes.stated() == (None, None)


def test_other_store_refused(st):
    cr = RM.ControlRoutes(st.routes)
    assert submit(cr, *st.signed(item(1))) == 1
    foreign, fsig = st.signed(item(5, store_id=OTHER_STORE))       # validly signed, higher generation, another store
    assert refusal(submit, cr, foreign, fsig) == "routemap-store"
    # keys, handed the same files by hand
    k = keys_of(st)
    (st.dispatch / "5.json").write_bytes(canon.canonical_bytes(foreign))
    (st.dispatch / "5.sig").write_text(fsig)
    (st.dispatch / "current").write_bytes(b"5")
    assert reload(k, 5, sha(foreign)) == {"refused": "routemap-store"}


def test_generation_gap_accepted(st):
    cr = RM.ControlRoutes(st.routes)
    k = keys_of(st)
    digests = []
    for g in (1, 5):
        it, sig = st.signed(item(g))
        assert submit(cr, it, sig) == g
        assert reload(k, g, sha(it)) == acked(st, g, sha(it))
        digests.append(sha(it))
    assert st.keys_head()["served"] == [{"generation": 1, "item_sha256": digests[0]},
                                        {"generation": 5, "item_sha256": digests[1]}]
    assert k.routes.stated() == (5, digests[1]) and st.head()["generation"] == 5


def test_non_brokered_dispatch_route_refused(st):
    cr = RM.ControlRoutes(st.routes)
    for bad, name in (([route(typ="egress")], "routemap-invalid:body"),
                      ([route(codec="none")], "routemap-invalid:body"),
                      ([route(rid="pypi")], "routemap-invalid:route"),            # a base id
                      ([route(), route()], "routemap-invalid:body"),               # one id twice
                      ([route(models=[{"requested": "m", "backend": "m"}])], "routemap-invalid:body")):  # no price
        assert refusal(submit, cr, *st.signed(item(1, bad))) == name, bad
    assert refusal(submit, cr, *st.signed(item(1, kind="verifier"))) == "routemap-invalid:kind"
    pol_cap = route()
    pol_cap["policy"]["max_call_microdollars"] = 1                                   # R4 §1.2: no per-call cap
    assert refusal(submit, cr, *st.signed(item(1, [pol_cap]))) == "routemap-invalid:body"
    sess_cap = route()
    sess_cap["policy"]["max_session_microdollars"] = 1                               # a dispatch cap is the ticket's
    assert refusal(submit, cr, *st.signed(item(1, [sess_cap]))) == "routemap-invalid:body"
    assert st.gen_files() == []


def test_route_submit_through_control_handle(st):
    # the verb: dispatch's peer only, `{generation}` answered once CONTROL's head and `current` are durable
    cr = RM.ControlRoutes(st.routes)
    sdir = st.L / "sessions"
    sdir.mkdir(mode=0o700)
    control = C.Control(C.StateStore(sdir), OP_UID, dispatch_uid=DISPATCH_UID, routes=cr)
    it, sig = st.signed(item(3))
    req = {"v": 1, "op": "ROUTE-SUBMIT", "id": 9, "map": it, "sig": sig}
    assert control.handle(C.Conn(OP_UID), req) == {"id": 9, "ok": False, "refused": "verb-not-allowed-for-peer"}
    assert control.handle(C.Conn(DISPATCH_UID), req) == {"id": 9, "ok": True, "generation": 3}
    assert control.handle(C.Conn(DISPATCH_UID), dict(req, map=item(2))) == \
        {"id": 9, "ok": False, "refused": "routemap-signature"}
    assert C.Control(C.StateStore(sdir), OP_UID, dispatch_uid=DISPATCH_UID).handle(C.Conn(DISPATCH_UID), req) == \
        {"id": 9, "ok": False, "refused": "routemap-verifier"}                     # no route store
    assert cr.head() == {"generation": 3, "item_sha256": sha(it)}


# ---- keys' load ---------------------------------------------------------------------------------------------------

def _two_generations(st: Store) -> tuple:
    cr = RM.ControlRoutes(st.routes)
    k = keys_of(st)
    out = []
    for g in (1, 2):
        it, sig = st.signed(item(g))
        submit(cr, it, sig)
        assert reload(k, g, sha(it)) == acked(st, g, sha(it))
        out.append(it)
    return k, out


def test_keys_refuses_rolled_back_current(st):
    k, (one, two) = _two_generations(st)
    pin = RM.Pin(2, RM.policy_digest(route()["policy"]))
    assert k.dispatch_policy("glm", pin).route == "glm"
    (st.dispatch / "current").write_bytes(b"1")                       # current rewritten to N-1 by hand
    assert reload(k, 1, sha(one)) == {"refused": "routemap-generation"}
    assert dispatch_refusal(k, "glm", pin) == "route-not-allowed|no dispatch map is served"
    assert reload(k, 2, sha(two)) == {"refused": "routemap-invalid:current"}
    assert dispatch_refusal(k, "glm", pin) == "route-not-allowed|no dispatch map is served"
    assert k.routes.stated() == (None, None)                          # KEYS-ATTACH states none: CONTROL re-sends
    assert st.keys_head()["generation"] == 2                          # keys' head never moved back
    (st.dispatch / "current").write_bytes(b"2")
    assert reload(k, 2, sha(two)) == acked(st, 2, sha(two))
    assert k.dispatch_policy("glm", pin).route == "glm"


def test_unloadable_generation_refuses_dispatch_routes(st):
    k, (one, two) = _two_generations(st)
    raw = (st.dispatch / "2.json").read_bytes()
    (st.dispatch / "2.json").write_bytes(raw[: len(raw) // 2])        # a truncated <gen>.json
    k2 = keys_of(st)                                                   # keys' start
    assert k2.routes.problem == "routemap-head-unverifiable" and k2.routes.stated() == (None, None)
    for g in (1, 2):
        assert dispatch_refusal(k2, "glm", RM.Pin(g, RM.policy_digest(route()["policy"]))) == \
            "route-not-allowed|no dispatch map is served"
    assert reload(k, 2, sha(two)) == {"refused": "routemap-invalid:item_sha256"}
    assert dispatch_refusal(k, "glm", RM.Pin(1, RM.policy_digest(route()["policy"]))).startswith("route-not-allowed|")


def test_keys_head_written_before_ack(st):
    # keys' head is durable BEFORE the generation is served or acked: a failing head write serves nothing new
    k, (one, two) = _two_generations(st)
    cr = RM.ControlRoutes(st.routes)
    three, sig = st.signed(item(3))
    submit(cr, three, sig)

    def failing(fd):
        raise OSError(5, "EIO")
    k.routes._fsync = failing
    assert reload(k, 3, sha(three)) == {"refused": "keys-unavailable"}
    assert st.keys_head()["generation"] == 2 and k.routes.stated() == (None, None)
    k.routes._fsync = os.fsync
    assert reload(k, 3, sha(three)) == acked(st, 3, sha(three))


# ---- pins, through dispatch_policy with an explicit pin -------------------------------------------------------------

def test_pin_unserved_generation_refused(st):
    k, _ = _two_generations(st)
    d = RM.policy_digest(route()["policy"])
    assert dispatch_refusal(k, "glm", RM.Pin(7, d)) == "route-not-allowed|rule 1: generation 7 is not served"
    assert dispatch_refusal(k, "glm", None).startswith("route-not-allowed|no pin")
    assert k.dispatch_policy("glm", RM.Pin(1, d)).route == "glm"


def test_pin_digest_mismatch_refused(st):
    k, _ = _two_generations(st)
    other = RM.policy_digest(route(host="api.z.ai")["policy"])
    assert dispatch_refusal(k, "glm", RM.Pin(2, other)) == \
        "route-not-allowed|rule 3: generation 2's glm is not the pinned policy"


def test_pin_rule_strict_hold_removal_and_readd(st):
    # R4 §2 Pins through dispatch_policy (the ticket half is 3c-5's test_pin_holds_for_session_life): P is served for
    # the session's life across a head that CHANGES the route; the route's removal from the head stops it while absent;
    # re-adding resumes P (not the new entry)
    cr = RM.ControlRoutes(st.routes)
    k = keys_of(st)
    p_route, changed = route(), route(host="api.z.ai")
    seq = [(1, [p_route]), (2, [changed]), (3, [route(rid="other")]), (4, [changed])]
    pin = RM.Pin(1, RM.policy_digest(p_route["policy"]))
    seen = []
    for g, routes in seq:
        it, sig = st.signed(item(g, routes))
        submit(cr, it, sig)
        assert reload(k, g, sha(it))["generation"] == g
        try:
            seen.append((g, k.dispatch_policy("glm", pin).upstream.host))
        except norm.Refused as e:
            seen.append((g, e.detail.split(":")[0]))
    assert seen == [(1, "openrouter.ai"), (2, "openrouter.ai"), (3, "rule 2"), (4, "openrouter.ai")], seen
    assert dispatch_refusal(k, "glm", RM.Pin(4, pin.policy_digest)).startswith("route-not-allowed|rule 3")


# ---- the two locks ------------------------------------------------------------------------------------------------

def test_activation_serialized(st):
    # R4 §8 test 5. CONTROL: A reads head 9 and is held inside its accept; B submits 11; A then commits 10. Without the
    # activation lock B commits 11 while A is held, and A's 10 lands over it.
    cr0 = RM.ControlRoutes(st.routes)
    submit(cr0, *st.signed(item(9)))
    gate, entered = threading.Event(), threading.Event()
    calls = []

    def gated(*a, **kw):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            gate.wait(10)
        return RM.verify_link_sig(*a, **kw)

    cr = RM.ControlRoutes(st.routes, verify=gated)
    ten, eleven = st.signed(item(10)), st.signed(item(11))
    res: dict = {}

    def run(name, pair):
        try:
            res[name] = submit(cr, *pair)
        except Refused as e:
            res[name] = e.name
    ta = threading.Thread(target=run, args=("A", ten))
    ta.start()
    assert entered.wait(10)
    tb = threading.Thread(target=run, args=("B", eleven))
    tb.start()
    time.sleep(0.5)
    head_while_held = st.head()["generation"]
    gate.set()
    ta.join(20)
    tb.join(20)
    assert head_while_held == 9 and res == {"A": 10, "B": 11}, (head_while_held, res)
    assert st.head()["generation"] == 11 and st.current() == 11

    # keys: RELOAD N held inside its file work while RELOAD N+1 arrives; without the RELOAD lock N+1's head is written
    # first and N's (computed from the older head) lands over it
    k = keys_of(st)
    kgate, kentered = threading.Event(), threading.Event()
    kcalls = []

    def kgated(*a, **kw):
        kcalls.append(1)
        if len(kcalls) == 1:
            kentered.set()
            kgate.wait(10)
        return RM.verify_link_sig(*a, **kw)
    k.routes._verify = kgated
    twelve = st.signed(item(12))
    (st.dispatch / "current").write_bytes(b"11")

    async def interleave():
        k._in_loop()
        loop = asyncio.get_running_loop()
        first = asyncio.ensure_future(k.reload(k.admin_epoch, 11, sha(eleven[0]), keep=KEEP_ALL))
        assert await loop.run_in_executor(None, kentered.wait, 10)
        submit(cr0, *twelve)                                            # CONTROL moves on: current = 12
        second = asyncio.ensure_future(k.reload(k.admin_epoch, 12, sha(twelve[0]), keep=KEEP_ALL))
        await asyncio.sleep(0.3)
        kgate.set()
        return await first, await second
    a, b = asyncio.run(interleave())
    assert a["generation"] == 11 and b["generation"] == 12, (a, b)
    assert st.keys_head()["generation"] == 12 and [e["generation"] for e in st.keys_head()["served"]] == [11, 12]
    assert k.routes.stated() == (12, sha(twelve[0]))


# ---- the start pass and the three activation fault gates (process runs) ---------------------------------------------

def _crash(st: Store, it: dict, sig: str, point: str) -> int:
    req = st.L / "req.json"
    req.write_text(json.dumps({"v": 1, "op": "ROUTE-SUBMIT", "id": 1, "map": it, "sig": sig}))
    sdir = st.L / "crash-sessions"
    sdir.mkdir(mode=0o700, exist_ok=True)
    p = subprocess.run([sys.executable, str(ROOT / "tests" / "c4_activation_driver.py"), str(st.routes), str(sdir),
                        str(req), point], capture_output=True, timeout=60,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    return p.returncode


def _restart_and_ack(st: Store, keys_py: str) -> dict:
    """CONTROL through `control.serve` (its start pass) with keys as a child process (its own head loaded): keys
    attaches stating its generation, and CONTROL's attach-time RELOAD of its head is acked."""
    from tests import b2_keys_rig as B2
    from tests import c3_rig as R3
    out: dict = {}

    async def run():
        ct = R3.ControlThread(str(st.L), routes_dir=st.routes, dispatch_uid=DISPATCH_UID).start()
        keys = B2.KeysProc(str(st.L), keys_py, 1, driver=R3.DRIVER)
        try:
            await keys.start()
            want = ct.control.routes.head()
            await R3.aio_until(lambda: ct.control.keys_serves() == want, what="keys serves CONTROL's head")
            out["head"], out["serves"] = want, ct.control.keys_serves()
            out["loaded"] = [x for x in keys.err if "route map generation=" in x]
            out["problem"] = ct.control.routes.problem
        finally:
            assert keys.stop() == 0
            keys.cleanup()
            ct.stop()
    asyncio.run(run())
    return out


@pytest.fixture(params=[sys.executable, "/usr/bin/python3"], ids=["venv", "root-py"])
def keys_py(request):
    from tests import b2_keys_rig as B2
    if not os.path.exists(request.param):
        pytest.skip("no " + request.param)
    B2.require_h11(request.param)
    return request.param


@pytest.mark.parametrize("point", ["after_head", "before_head", "after_current"])
def test_activation_crash(st, keys_py, point):
    # r3 §2.3 "CONTROL's start pass FINISHES an interrupted activation": test_activation_crash_after_head_before_current,
    # ..._before_head, ..._after_current_before_reload, one SIGKILL each
    cr = RM.ControlRoutes(st.routes)
    one, sig1 = st.signed(item(1))
    submit(cr, one, sig1)
    k = keys_of(st)
    assert reload(k, 1, sha(one))["generation"] == 1                 # keys served generation 1 before the crash
    two, sig2 = st.signed(item(2, [route(host="api.z.ai")]))
    assert _crash(st, two, sig2, point) == -9
    files_after_crash, current_after_crash = st.gen_files(), st.current()
    out = _restart_and_ack(st, keys_py)
    if point == "before_head":
        # the old head stands; the orphan generation-2 files are deleted; keys keeps serving 1 (no RELOAD needed)
        assert files_after_crash == ["1.json", "1.sig", "2.json", "2.sig"] and current_after_crash == 1
        assert out["head"] == {"generation": 1, "item_sha256": sha(one)} and st.gen_files() == ["1.json", "1.sig"]
        assert st.current() == 1 and st.keys_head()["generation"] == 1
        assert submit(RM.ControlRoutes(st.routes), two, sig2) == 2   # the same map is accepted again, whole
    else:
        assert current_after_crash == (1 if point == "after_head" else 2)
        assert out["head"] == {"generation": 2, "item_sha256": sha(two)} == out["serves"]
        assert st.current() == 2 and st.keys_head()["generation"] == 2
    assert out["problem"] is None and out["loaded"] == ["levain gw-keys: route map generation=1"], out


def test_start_pass_unverifiable_head_rewrites_nothing(st):
    cr = RM.ControlRoutes(st.routes)
    two, sig = st.signed(item(2))
    submit(cr, two, sig)
    (st.dispatch / "current").write_bytes(b"1")
    (st.dispatch / "2.sig").write_text(Signer(st.L, "x").sign(canon.canonical_bytes(two)))   # a foreign signature
    cr2 = RM.ControlRoutes(st.routes)
    cr2.start_pass()
    assert cr2.problem == "routemap-head-unverifiable" and cr2.head() is None
    assert st.current() == 1                                          # a fault, not a crash artefact: not repaired


# ---- one checker, one set of bounds ----------------------------------------------------------------------------------

@pytest.mark.skipif(sys.version_info < (3, 10), reason="levain.dispatch.control is the operator's (3.10+)")
def test_bounds_and_envelope_match_dispatch():
    from levain.dispatch import control as dc
    assert RM.ITEM_KEYS == dc.ITEM_KEYS and RM.CONFIG_FORMAT == dc.FORMAT
    assert RM.CONFIG_NAMESPACE == dc.CONFIG_NAMESPACE
    assert RM.ITEM_MAX_BYTES == dc.ITEM_MAX_BYTES and RM.SIGNATURE_MAX == dc.SIGNATURE_MAX
    it = item(4)
    assert dc.item_bytes("routemap", 4, it["body"], it["issued_at"], prev=it["prev"], store_id=STORE_ID) == \
        canon.canonical_bytes(it)


# ---- 3c-4 L3 r1 fixes -----------------------------------------------------------------------------------------------

def _keys_side(k: K.Keys):
    """CONTROL's `reload` and `served` callables over a real `Keys` (RELOAD on keys' own loop, its statement)."""
    def send(h):
        return reload(k, h["generation"], h["item_sha256"])

    def served():
        g, s = k.routes.stated()
        return None if g is None else {"generation": g, "item_sha256": s}
    return send, served


def _is_dir(fd: int) -> bool:
    import stat
    return stat.S_ISDIR(os.fstat(fd).st_mode)


def test_dispatch_dir_setgid_keys_group(st):
    # codex HIGH 1: L/routes/dispatch is setgid, group _levain_keys, so every file CONTROL creates there takes keys'
    # group (on Linux a new file takes its creator's primary group otherwise, and keys reads none of them)
    import grp
    import stat
    from levain.slot import layout
    for host in ("darwin", "linux"):
        row = next(r for r in layout.base_store_rows(host, "op") if r.path == layout.STORE[host] / "routes" / "dispatch")
        assert (row.owner, row.group, row.mode) == ("_levain_control", "_levain_keys", 0o2750), row
        steps = [s.argv for s in layout.row_steps(host, [row]) if s.argv]
        chown = [a for a in steps if a[0].endswith("chown")]
        chmod = [a for a in steps if a[0].endswith("chmod") and a[1] != "-N"]    # -N: the macOS ACL clear
        assert chown == [(chown[0][0], "_levain_control:_levain_keys", str(row.path))], steps
        assert chmod == [(chmod[0][0], "2750", str(row.path))], steps
        assert steps.index(chown[0]) < steps.index(chmod[0])   # the mode (setgid) set after the chown
    # the directory contract on a real directory: the row's mode applied, then a file CONTROL writes takes its gid
    os.chown(st.dispatch, -1, os.getgid())           # a group this writer is in (/tmp's may not be), so setgid holds
    os.chmod(st.dispatch, row.mode)
    assert stat.S_IMODE(os.stat(st.dispatch).st_mode) == 0o2750
    other = [g for g in os.getgroups() if g != os.getgid()]
    if sys.platform.startswith("linux") and other:
        os.chown(st.dispatch, -1, other[0])          # a group that is not this writer's primary one (as keys' is not
        os.chmod(st.dispatch, row.mode)              # CONTROL's); chown cleared nothing the mode then sets again
    cr = RM.ControlRoutes(st.routes)
    submit(cr, *st.signed(item(1)))
    want = os.stat(st.dispatch).st_gid
    for n in ("1.json", "1.sig", "head", "current"):
        assert os.stat(st.dispatch / n).st_gid == want, (n, want, grp.getgrgid(want).gr_name)


def test_keys_reload_transactions_never_overlap(st):
    # codex HIGH 2 (the synchronous half): two RELOAD transactions on keys' disk head, each on its own thread; the
    # first is held between its checks and its head write. The second's checks must not start until it commits.
    k, (one, two) = _two_generations(st)
    cr = RM.ControlRoutes(st.routes)
    three = st.signed(item(3))
    submit(cr, *three)
    rm = k.routes
    held, release, second_in = threading.Event(), threading.Event(), threading.Event()
    active: list = []
    overlaps: list = []
    orig_check, orig_commit = rm.check_reload, rm.commit

    def check(g, s):
        if active:
            overlaps.append((list(active), g))
        active.append(g)
        out = orig_check(g, s)
        if len(active) == 1 and not held.is_set():
            held.set()
            release.wait(10)
        else:
            second_in.set()
        return out

    def commit(h):
        try:
            orig_commit(h)
        finally:
            active.pop(0)
    rm.check_reload, rm.commit = check, commit
    res: dict = {}
    ta = threading.Thread(target=lambda: res.__setitem__("a", K.Keys._reload_files(rm, 3, sha(three[0]), KEEP_ALL)))
    ta.start()
    assert held.wait(10)
    tb = threading.Thread(target=lambda: res.__setitem__("b", K.Keys._reload_files(rm, 3, sha(three[0]), KEEP_ALL)))
    tb.start()
    entered_while_held = second_in.wait(1.0)        # bounded: with the transaction lock it never enters while held
    release.set()
    ta.join(20)
    tb.join(20)
    assert not entered_while_held and overlaps == [], (entered_while_held, overlaps)
    assert res["a"][1]["generation"] == res["b"][1]["generation"] == 3 == st.keys_head()["generation"]


def test_keys_reload_cancelled_waits_out_its_work(st):
    # codex HIGH 2 + complement MED 1 (the async half): RELOAD 3's file work is held past a preemption's cancel of its
    # task; RELOAD 4 arrives. RELOAD 3's task ends only once its work completed AND its outcome is in memory (keys'
    # in-memory head == the disk head it wrote), and RELOAD 4's work never overlaps it.
    k, _ = _two_generations(st)
    cr = RM.ControlRoutes(st.routes)
    three, four = st.signed(item(3)), st.signed(item(4))
    submit(cr, *three)
    rm = k.routes
    a_held, a_go, b_in, b_go = threading.Event(), threading.Event(), threading.Event(), threading.Event()
    active: list = []
    overlaps: list = []
    orig_check, orig_commit = rm.check_reload, rm.commit

    def check(g, s):
        if active:
            overlaps.append((list(active), g))
        active.append(g)
        if g == 4:
            b_in.set()
            b_go.wait(10)
        out = orig_check(g, s)
        if g == 3:
            a_held.set()
            a_go.wait(10)                          # held between its checks and its head write
        return out

    def commit(h):
        try:
            orig_commit(h)
        finally:
            active.remove(h["generation"])
    rm.check_reload, rm.commit = check, commit
    seen: dict = {}

    async def run():
        k.state_lock = None             # this loop's own locks: 3.9 binds a lock to the loop that made it, and the
        k._in_loop()                    # RELOAD lock is contended here (the earlier RELOADs ran on other loops)
        loop = asyncio.get_running_loop()
        first = asyncio.ensure_future(k.reload(k.admin_epoch, 3, sha(three[0]), keep=KEEP_ALL))
        assert await loop.run_in_executor(None, a_held.wait, 10)
        first.cancel()                                            # the preemption's cancel of the request task
        await asyncio.sleep(0)
        submit(cr, *four)                                         # CONTROL moves on: current = 4
        second = asyncio.ensure_future(k.reload(k.admin_epoch, 4, sha(four[0]), keep=KEEP_ALL))
        seen["b_in_while_held"] = await loop.run_in_executor(None, b_in.wait, 1.0)
        a_go.set()
        try:
            await first
            seen["first"] = "answered"
        except asyncio.CancelledError:
            seen["first"] = "cancelled"
        seen["after_first"] = (k.routes.head and k.routes.head["generation"], st.keys_head()["generation"],
                               k.routes.stated())
        b_go.set()
        seen["second"] = await second
    asyncio.run(run())
    assert seen["first"] == "cancelled" and not seen["b_in_while_held"] and overlaps == [], seen
    assert seen["after_first"] == (3, 3, (3, sha(three[0]))), seen
    assert seen["second"] == acked(st, 4, sha(four[0]))
    assert k.routes.stated() == (4, sha(four[0])) and st.keys_head()["generation"] == 4


def _arm(fsync_calls: list, when):
    """An fsync that raises EIO on the call `when(fd)` picks while armed (`fsync_calls[0]` True), and records every
    call's (fd is a directory) after."""
    def fsync(fd):
        d = _is_dir(fd)
        if fsync_calls and fsync_calls[0] is True and when(d):
            fsync_calls[0] = False
            raise OSError(5, "EIO")
        fsync_calls.append(d)
        os.fsync(fd)
    return fsync


def test_resubmission_converges_after_current_write_failure(st):
    # complement MED 2: activation 2 wrote its head, then the `current` write failed (EIO before the rename). The same
    # map resubmitted finishes it: `current`, `_active`, keys' served head and keys' in-memory head, before the ack.
    k = keys_of(st)
    calls: list = [False]
    arm = {"on": False}
    cr = RM.ControlRoutes(st.routes, fsync=_arm(calls, lambda d: not d),
                          fault=lambda p: calls.__setitem__(0, True) if p == "after_head" and arm["on"] else None)
    send, served = _keys_side(k)
    one, two = st.signed(item(1)), st.signed(item(2, [route(host="api.z.ai")]))
    assert cr.submit(*one, send, served) == 1 and k.routes.stated() == (1, sha(one[0]))
    arm["on"] = True
    with pytest.raises(OSError):
        cr.submit(*two, send, served)
    arm["on"] = False
    assert st.head()["generation"] == 2 and st.current() == 1 and cr.head()["generation"] == 1   # part-done
    assert cr.submit(*two, send, served) == 2
    assert st.current() == 2 and cr.head() == {"generation": 2, "item_sha256": sha(two[0])} and cr.problem is None
    assert k.routes.stated() == (2, sha(two[0])) and k.routes.head["generation"] == 2 == st.keys_head()["generation"]


def test_resubmission_converges_after_head_dir_fsync_failure(st):
    # codex MED (routemaps 431): the head's rename succeeded but its directory fsync failed (NotDurable). The same map
    # resubmitted settles the files again (a directory fsync is seen), repairs `current`, publishes `_active`, RELOADs.
    from levain.slot.records import NotDurable
    k = keys_of(st)
    calls: list = [False]
    arm = {"on": False}
    cr = RM.ControlRoutes(st.routes, fsync=_arm(calls, lambda d: d),
                          fault=lambda p: calls.__setitem__(0, True) if p == "before_head" and arm["on"] else None)
    send, served = _keys_side(k)
    one, two = st.signed(item(1)), st.signed(item(2))
    cr.submit(*one, send, served)
    arm["on"] = True
    with pytest.raises(NotDurable):
        cr.submit(*two, send, served)
    arm["on"] = False
    assert st.head()["generation"] == 2 and st.current() == 1 and cr.head()["generation"] == 1
    del calls[1:]
    assert cr.submit(*two, send, served) == 2
    assert True in calls[1:]                                      # the directory fsynced again: the head settled
    assert st.current() == 2 and cr.head()["generation"] == 2
    assert k.routes.stated() == (2, sha(two[0])) == (st.keys_head()["generation"], st.keys_head()["item_sha256"])


def test_keys_equal_generation_reload_converges(st):
    # codex HIGH 3: keys' head rename succeeded, its directory fsync failed (refused keys-unavailable, the new head on
    # disk). The same RELOAD again settles that head and INSTALLS it: keys serves it, in memory as on disk.
    k, _ = _two_generations(st)
    cr = RM.ControlRoutes(st.routes)
    three = st.signed(item(3))
    submit(cr, *three)
    calls: list = [True]
    k.routes._fsync = _arm(calls, lambda d: d)
    assert reload(k, 3, sha(three[0])) == {"refused": "keys-unavailable"}
    assert st.keys_head()["generation"] == 3 and k.routes.stated() == (None, None)
    del calls[1:]
    assert reload(k, 3, sha(three[0])) == acked(st, 3, sha(three[0]))
    assert True in calls[1:]                                      # settled again
    assert k.routes.head["generation"] == 3 and k.routes.stated() == (3, sha(three[0]))
    pin = RM.Pin(3, RM.policy_digest(route()["policy"]))
    assert k.dispatch_policy("glm", pin).route == "glm"


def test_base_dispatch_collision_refused_at_use(st):
    # codex HIGH 4: the dispatch head serves `glm`; root then adds a brokered `glm` to the live base map. keys refuses
    # the id at resolution time (fails closed, named), never routes it by the base policy.
    k, _ = _two_generations(st)
    pin = {"route": "glm", "routemap_generation": 2, "route_policy_digest": RM.policy_digest(route()["policy"])}
    claims = {"routes": ["glm", "pypi"], "run": pin}
    assert k._route_policy(claims, "glm").upstream.host == "openrouter.ai"   # served from the dispatch map
    base_glm = dict(route(host="api.z.ai")["policy"], prices={"z-ai/glm-5.2": PRICE}, max_session_microdollars=10 ** 6)
    (st.routes / "base.json").write_bytes(canon.canonical_bytes(
        {"format": "levain-routes-base/2", "routes": [{"route": "pypi", "type": "egress", "hosts": ["pypi.org"]},
                                                      base_glm]}))
    try:
        k._route_policy(claims, "glm")
        raise AssertionError("served")
    except norm.Refused as e:
        assert (e.name, e.detail.split(":")[0]) == ("keys-unavailable", "route-collision"), (e.name, e.detail)
    # a base route the head does not name is untouched
    assert refusal_k(k, claims, "pypi") == "route-not-allowed"   # an egress base route is no `Q` route, as before


def refusal_k(k: K.Keys, claims: dict, rid: str) -> str:
    try:
        k._route_policy(claims, rid)
    except norm.Refused as e:
        return e.name
    return "served"


def test_envelope_checked_as_dispatch_does(st):
    # codex LOW: `prev` and `issued_at` are checked as `parse_item` checks them (one function, both sides)
    cr = RM.ControlRoutes(st.routes)
    for bad in (dict(item(1), prev="sha256:" + "0" * 64), dict(item(2), prev=""), dict(item(2), prev="sha256:x"),
                dict(item(1), issued_at="2026-10-23T12:00:00Z"), dict(item(1), issued_at="2026-02-30T12:00:00.000000Z"),
                dict(item(1), issued_at=7)):
        assert refusal(submit, cr, *st.signed(bad)) == "routemap-invalid:envelope", bad
    assert st.gen_files() == []
    assert submit(cr, *st.signed(item(1))) == 1


@pytest.mark.skipif(sys.version_info < (3, 10), reason="levain.dispatch.control is the operator's (3.10+)")
def test_envelope_is_the_producers():
    from levain.cockpit import consent as cc
    from levain.dispatch import control as dc
    from levain.dispatch import routemap_rules as rr
    assert rr.STAMP.pattern == cc._STAMP.pattern and rr.STAMP_FORMAT == cc._STAMP_FORMAT
    stamp = cc.utc_stamp(__import__("datetime").datetime(2026, 10, 23, tzinfo=__import__("datetime").timezone.utc))
    assert rr.envelope_problem(1, "", stamp) is None
    for gen, prev, at in ((1, "x", stamp), (2, "", stamp), (1, "", "2026-02-30T12:00:00.000000Z"), (1, "", "x")):
        raw = canon.canonical_bytes(dict(item(gen), prev=prev, issued_at=at))
        with pytest.raises(dc.ConfigRefused) as ei:
            dc.parse_item(raw)
        assert str(ei.value) == rr.envelope_problem(gen, prev, at)


def test_reload_keys_failure_never_raises(st):
    # complement LOW 5: any failure of the RELOAD call (not only a refusal) is recorded and never raised: the head is
    # already durable, so ROUTE-SUBMIT answers the activation
    class Boom:
        drop_session = front_register = front_arm = None

        def call(self, op, **kw):
            raise ConnectionResetError("keys went away")
    cr = RM.ControlRoutes(st.routes)
    sdir = st.L / "sessions"
    sdir.mkdir(mode=0o700)
    control = C.Control(C.StateStore(sdir), OP_UID, dispatch_uid=DISPATCH_UID, routes=cr, keys_admin=Boom())
    it, sig = st.signed(item(1))
    assert control.handle(C.Conn(DISPATCH_UID), {"v": 1, "op": "ROUTE-SUBMIT", "id": 1, "map": it, "sig": sig}) == \
        {"id": 1, "ok": True, "generation": 1}
    assert [(op, type(e).__name__) for op, _s, e in control.seam_errors] == [("reload", "ConnectionResetError")]


# ---- L3 r2 (1023+26 fix2) -----------------------------------------------------------------------------------------

def _base_with(st: Store, *rids: str) -> None:
    """Root's live base map gains a brokered route for each of `rids` (beside the egress `pypi`)."""
    extra = [dict(route(rid, host="api.z.ai")["policy"], prices={"z-ai/glm-5.2": PRICE},
                  max_session_microdollars=10 ** 6) for rid in rids]
    (st.routes / "base.json").write_bytes(canon.canonical_bytes(
        {"format": "levain-routes-base/2", "routes": [{"route": "pypi", "type": "egress", "hosts": ["pypi.org"]}]
         + extra}))


def _resolved(k: K.Keys, claims: dict, rid: str) -> str:
    """`_route_policy`'s outcome: the upstream host served, or `<name>|<detail's first word>`."""
    try:
        return k._route_policy(claims, rid).upstream.host
    except norm.Refused as e:
        return f"{e.name}|{e.detail.split(':')[0]}"


def test_collision_checks_every_served_generation(st):
    # complement N1: a session pinned to generation 1 (which has `glm`); generation 2 drops `glm`; root then adds a
    # brokered `glm` to the live base map. The collision set is the union across every served generation, so the
    # pinned id is refused, never routed by the base policy. A base id no served generation names is untouched.
    cr = RM.ControlRoutes(st.routes)
    k = keys_of(st)
    for g, routes in ((1, [route()]), (2, [route(rid="other")])):
        it, sig = st.signed(item(g, routes))
        submit(cr, it, sig)
        assert reload(k, g, sha(it))["generation"] == g
    _base_with(st, "glm", "zz")
    claims = {"routes": ["glm", "zz"],
              "run": {"route": "glm", "routemap_generation": 1,
                      "route_policy_digest": RM.policy_digest(route()["policy"])}}
    assert _resolved(k, claims, "glm") == "keys-unavailable|route-collision"
    assert _resolved(k, {"routes": ["zz"]}, "zz") == "api.z.ai"           # an interactive session's base route
    assert _resolved(k, claims, "zz") == "route-not-allowed|not the run's route"   # a run calls its own route only
    assert k.routes.route_ids() == frozenset({"glm", "other"})


def test_collision_fails_closed_when_ids_unavailable(st):
    # codex item 4: keys restarts after root added a colliding base `glm`; generation 1 (`glm`) no longer loads, so a
    # head exists whose served ids are unknown. Every base route resolution refuses (fail closed), `zz` included, until
    # the ids are known again.
    cr = RM.ControlRoutes(st.routes)
    k = keys_of(st)
    for g, routes in ((1, [route()]), (2, [route(rid="other")])):
        it, sig = st.signed(item(g, routes))
        submit(cr, it, sig)
        assert reload(k, g, sha(it))["generation"] == g
    _base_with(st, "glm", "zz")
    k2 = keys_of(st)                                                     # keys' start: generation 1 refused
    assert sorted(k2.routes.maps) == [2] and k2.routes.problem is None    # the head generation itself serves
    assert k2.routes.route_ids() is None
    claims = {"routes": ["glm", "zz"],
              "run": {"route": "glm", "routemap_generation": 1,
                      "route_policy_digest": RM.policy_digest(route()["policy"])}}
    assert _resolved(k2, claims, "glm") == "keys-unavailable|route-collision"
    assert _resolved(k2, claims, "zz") == "keys-unavailable|route-collision"
    # the head generation itself unloadable (codex's case): the same
    (st.dispatch / "2.json").write_bytes(b"{")
    k3 = keys_of(st)
    assert k3.routes.head is not None and k3.routes.maps == {} and k3.routes.route_ids() is None
    assert _resolved(k3, claims, "glm") == "keys-unavailable|route-collision"


def test_collision_absent_head_is_empty(st):
    # only a genuinely absent head is an empty collision set: base routes resolve as before. A head on disk that does
    # not parse is a head whose ids are unknown: fail closed.
    k = keys_of(st)
    _base_with(st, "glm")
    claims = {"routes": ["glm"]}
    assert k.routes.head is None and k.routes.route_ids() == frozenset()
    assert _resolved(k, claims, "glm") == "api.z.ai"
    (st.L / "keys" / "routemap.head").write_bytes(b"not a head")
    k2 = keys_of(st)
    assert k2.routes.head is None and k2.routes.route_ids() is None
    assert _resolved(k2, claims, "glm") == "keys-unavailable|route-collision"


def test_reload_executor_refusing_work_refuses(st):
    # codex MED: the executor refusing the work (shut down, cannot accept) is a failed RELOAD: refused
    # `keys-unavailable`, every dispatch route refused, never an escape that leaves the old head served
    import concurrent.futures

    class Shut(concurrent.futures.ThreadPoolExecutor):
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")
    k, _ = _two_generations(st)
    cr = RM.ControlRoutes(st.routes)
    three = st.signed(item(3))
    submit(cr, *three)
    pin = RM.Pin(2, RM.policy_digest(route()["policy"]))
    assert k.dispatch_policy("glm", pin).route == "glm"

    async def run():
        k.state_lock = None
        k._in_loop()
        ex = Shut(max_workers=1)
        asyncio.get_running_loop().set_default_executor(ex)
        try:
            return await k.reload(k.admin_epoch, 3, sha(three[0]), keep=KEEP_ALL)
        finally:
            ex.shutdown(wait=False)
    assert asyncio.run(run()) == {"refused": "keys-unavailable"}
    assert k.routes.stated() == (None, None) and k.routes.problem == "keys-unavailable"
    assert dispatch_refusal(k, "glm", pin) == "route-not-allowed|no dispatch map is served"


# ---- L3 r3 (1023+26 fix3) -----------------------------------------------------------------------------------------

def test_collision_unknown_ids_survive_a_refused_reload(st):
    # complement MED: a head on disk that does not parse leaves the ids UNKNOWN, and a later refused RELOAD (whatever it
    # names) must not make them known-empty: `glm`, pinned in the unreadable head, still refuses rather than resolving
    # by the base policy. Only a RELOAD keys acks (a verified head) makes the ids known again.
    import concurrent.futures

    class Shut(concurrent.futures.ThreadPoolExecutor):
        def submit(self, *a, **kw):
            raise RuntimeError("cannot schedule new futures after shutdown")
    cr = RM.ControlRoutes(st.routes)
    one = st.signed(item(1))
    submit(cr, *one)
    assert reload(keys_of(st), 1, sha(one[0]))["generation"] == 1
    (st.L / "keys" / "routemap.head").write_bytes(b"not a head")
    _base_with(st, "glm", "zz")
    k = keys_of(st)
    claims = {"routes": ["glm", "zz"]}
    assert k.routes.head is None and k.routes.route_ids() is None
    assert reload(k, 2, "0" * 64) == {"refused": "routemap-invalid:current"}
    assert k.routes.problem == "routemap-invalid:current" and k.routes.route_ids() is None
    assert _resolved(k, claims, "glm") == "keys-unavailable|route-collision"

    async def shut():
        k._in_loop()
        ex = Shut(max_workers=1)
        asyncio.get_running_loop().set_default_executor(ex)
        try:
            return await k.reload(k.admin_epoch, 1, sha(one[0]), keep=KEEP_ALL)
        finally:
            ex.shutdown(wait=False)
    assert asyncio.run(shut()) == {"refused": "keys-unavailable"}
    assert k.routes.problem == "keys-unavailable" and k.routes.route_ids() is None
    assert _resolved(k, claims, "glm") == "keys-unavailable|route-collision"
    assert _resolved(k, claims, "zz") == "keys-unavailable|route-collision"
    # the head repaired (here: removed), then a RELOAD keys acks (the base map clear of `glm` while it is checked, then
    # `glm` added back): the ids are known again
    (st.L / "keys" / "routemap.head").unlink()
    _base_with(st, "zz")
    assert reload(k, 1, sha(one[0]))["generation"] == 1
    _base_with(st, "glm", "zz")
    assert k.routes.ids_unknown is False and k.routes.route_ids() == frozenset({"glm"})
    assert _resolved(k, claims, "glm") == "keys-unavailable|route-collision"
    assert _resolved(k, claims, "zz") == "api.z.ai"


def test_collision_unknown_ids_from_a_reload_that_reads_an_unverifiable_head(st):
    # keys holds no head (none at its start), then a RELOAD reads keys' head on disk and it does not verify: the ids are
    # unknown from then on, as at a start with that head, until a RELOAD keys acks
    cr = RM.ControlRoutes(st.routes)
    k = keys_of(st)
    assert k.routes.head is None and k.routes.route_ids() == frozenset()
    one = st.signed(item(1))
    submit(cr, *one)
    (st.L / "keys").mkdir(mode=0o700, exist_ok=True)
    (st.L / "keys" / "routemap.head").write_bytes(b"not a head")
    assert reload(k, 1, sha(one[0])) == {"refused": "routemap-head-unverifiable"}
    assert k.routes.ids_unknown is True and k.routes.route_ids() is None
    _base_with(st, "zz")
    assert _resolved(k, {"routes": ["zz"]}, "zz") == "keys-unavailable|route-collision"


# --- the one base-map reader (3c-9 C fix r2 item 3): keys' read, shared with `levain doctor` ------------------------

def _base_lib(tmp_path, routes=None) -> str:
    from levain.slot import vendor_routes as VR
    lib = tmp_path / "L"
    (lib / "routes").mkdir(parents=True)
    doc = {"format": VR.BASE_FORMAT, "routes": [VR.anthropic_route()] if routes is None else routes}
    (lib / "routes" / "base.json").write_bytes(canon.canonical_bytes(doc))
    return str(lib)


def test_read_base_map_parses_whole_and_returns_the_opened_files_key(tmp_path):
    lib = _base_lib(tmp_path)
    key, routes = RM.read_base_map(lib)
    st = os.stat(os.path.join(lib, "routes", "base.json"))
    assert key == (st.st_dev, st.st_ino, st.st_mtime_ns, st.st_size) and list(routes) == ["anthropic"]
    known = (key, {"sentinel": None})
    assert RM.read_base_map(lib, known) is known          # the same file is not read again


def test_read_base_map_refusals(tmp_path):
    from levain.broker.policy import PolicyError
    from levain.slot import vendor_routes as VR
    with pytest.raises(FileNotFoundError):
        RM.read_base_map(str(tmp_path / "none"))
    lib = _base_lib(tmp_path, [VR.anthropic_route(), VR.anthropic_route()])
    with pytest.raises(PolicyError):
        RM.read_base_map(lib)                               # an id twice: the map refused whole
    path = os.path.join(lib, "routes", "base.json")
    with open(path, "wb") as f:
        f.write(b" " * (RM.BASE_MAP_MAX + 1))
    with pytest.raises(PolicyError):
        RM.read_base_map(lib)


def test_read_base_map_follows_a_link_as_keys_always_has(tmp_path):
    lib = _base_lib(tmp_path)
    path = os.path.join(lib, "routes", "base.json")
    os.replace(path, path + ".real")
    os.symlink(path + ".real", path)
    assert list(RM.read_base_map(lib)[1]) == ["anthropic"]


def test_keys_reads_the_base_map_through_the_one_reader(tmp_path, monkeypatch):
    lib = _base_lib(tmp_path)
    seen = []
    real = RM.read_base_map
    monkeypatch.setattr(RM, "read_base_map", lambda *a, **k: seen.append(a[0]) or real(*a, **k))
    assert K.Keys(lib_root=lib)._base_route("anthropic").route == "anthropic"
    assert seen == [lib] and not hasattr(K, "BASE_MAP_CAP")
