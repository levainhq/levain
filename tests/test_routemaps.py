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
            "body_policy": {"provider_only": ["z-ai"]}, "retention_claim": "recorded, never a guarantee"}


def item(gen: int, routes=None, *, store_id: str = STORE_ID, kind: str = "routemap") -> dict:
    return {"format": RM.CONFIG_FORMAT, "store_id": store_id, "generation": gen,
            "prev": "" if gen == 1 else "sha256:" + "0" * 64, "issued_at": "2026-10-23T12:00:00Z", "kind": kind,
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


def reload(k: K.Keys, gen: int, digest: str) -> dict:
    async def run():
        k._in_loop()
        return await k.reload(k.admin_epoch, gen, digest)
    return asyncio.run(run())


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
    flipped = dict(it, issued_at="2026-10-23T12:00:01Z")          # one byte changed after signing
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
    assert submit(cr, *a, reloads) == 2                             # a resubmission: acked, nothing written
    assert (st.dispatch / "head").read_bytes() == head_bytes and len(reloads) == 1
    # keys: the file swapped under it is refused at RELOAD, and at start
    k = keys_of(st)
    assert reload(k, 2, sha(a[0])) == {"generation": 2, "item_sha256": sha(a[0])}
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
        assert reload(k, g, sha(it)) == {"generation": g, "item_sha256": sha(it)}
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
        assert reload(k, g, sha(it)) == {"generation": g, "item_sha256": sha(it)}
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
    assert reload(k, 2, sha(two)) == {"generation": 2, "item_sha256": sha(two)}
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
    assert reload(k, 3, sha(three)) == {"generation": 3, "item_sha256": sha(three)}


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
        first = asyncio.ensure_future(k.reload(k.admin_epoch, 11, sha(eleven[0])))
        assert await loop.run_in_executor(None, kentered.wait, 10)
        submit(cr0, *twelve)                                            # CONTROL moves on: current = 12
        second = asyncio.ensure_future(k.reload(k.admin_epoch, 12, sha(twelve[0])))
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
    if not os.path.exists(request.param):
        pytest.skip("no " + request.param)
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
