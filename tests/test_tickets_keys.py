"""S1 3c-5 session 2, keys' side of a ticketed run (CONTRACT §3.3 TOKEN-RESOLVE `run`; 3c design r3 §2.1 item 2, row
3c-5; R4 §1.2 scope and cap, §1.6 dispatch refusals, §1.7 step 4; head rulings H1 and H2, 1024+8).

The in-process cases drive the real `Keys._exchange` against the scripted upstream of `test_broker_keys_upstream.py`
on a dispatch-shaped route (no per-session cap, served through the `dispatch_policy` seam), with the session's claims
as keys caches them: CONTROL's `TOKEN-RESOLVE` answer through `keys.resolve_outcome`. The H1 case builds that answer
with a real CONTROL over a real ticket (`tests/test_tickets.py`'s rig) and reads every ledger and gw.jsonl line."""
from __future__ import annotations

import asyncio
import json
import os
from dataclasses import replace

import pytest

from levain.broker import creds as CR
from levain.broker import gwwire
from levain.broker import keys as K
from levain.broker.norm import digest_of_token
from levain.broker.policy import parse_base_map
from levain.contracts import canon
from levain.slot import control as C
from tests.ledger_support import file_lines
from tests.test_broker_keys_upstream import PRICE, SECRET, TOKEN, _q, _Rig, _run, _scripted, _types, _Up, base_route
from tests.test_routemaps import Store, st  # noqa: F401
from tests.test_tickets import (KEYS_UID, SESSION, Clock, Rig, bound_session, d_mint_claims, root,
                                to_rendering)

MODEL = "z-ai/glm-5.2"
RUN = {"run_id": "r-0123456789abcdef", "job_id": "job-1", "run_index": 0, "contract_digest": "sha256:" + "1" * 64,
       "route": "glm", "routemap_generation": 1, "route_policy_digest": "sha256:" + "2" * 64,
       "model_requested": MODEL, "reserved_microdollars": 10 ** 6}
FRONT_CLAIMS = {"front": {"uid": 70000, "pid": 1, "start": 1}, "incarnation": 1}


def dispatch_pol(host: str = "api.example.com", cred: str = "c1", codec: str = "anthropic-messages",
                 body_policy=None):
    """A routemap `/3`-shaped policy: brokered, priced, no per-session cap (keys' dispatch shape), on `host`."""
    pol = parse_base_map({"format": "levain-routes-base/2",
                          "routes": [base_route("glm", credential=cred, codec=codec, prices={MODEL: PRICE})]})["glm"]
    return replace(pol, max_session_microdollars=None, upstream=replace(pol.upstream, host=host),
                   hosts=(host,), body_policy=body_policy)


def _openrouter_cred(d: str) -> None:
    with open(os.path.join(d, "keys", "cred", "c2.json"), "w") as f:
        f.write(json.dumps({"format": CR.FORMAT, "name": "c2", "rev": "0" * 32, "hosts": ["openrouter.ai"],
                            "inject": {"header": "authorization", "scheme": "Bearer"}, "kind": "static",
                            "secret": SECRET}))


async def _exchange(d: str, monkeypatch, body: bytes, claims: dict, pol, *, route: str = "glm",
                    gw_log: bool = False) -> dict:
    up = _Up()
    st_ = _scripted(monkeypatch, up)
    kw = {"dispatch_policy": (lambda r, pin: pol)}
    if gw_log:
        kw["gw_log"] = K.GwLog(d)
    async with _Rig(d, **kw) as rig:
        rig.k.cache[digest_of_token(TOKEN)] = {"session": "s1", "claims": claims}
        rig.front.feed_data(gwwire.encode(b"B", body) + gwwire.encode(b"E"))
        up.feed(b"HTTP/1.1 200 OK\r\ncontent-length: 2\r\n\r\nok")
        up.feed(b"")
        await asyncio.wait_for(rig.run(dict(_q("length:%d" % len(body)), route=route)), 10)
        got = await rig.got()
        path = rig.ledger.path
    raw = b"".join(up.writes)
    return {"frames": got, "handshakes": st_["handshakes"], "sent": raw.partition(b"\r\n\r\n")[2],
            "lines": file_lines(path), "ledger_path": path}


def _f(name: str) -> tuple:
    return (b"F", gwwire.encode(b"F", {"refused": name})[5:])


def _claims(run: dict, routes=("glm",)) -> dict:
    return {"routes": list(routes), **FRONT_CLAIMS, "run": dict(run)}


BODY = b'{"model":"%s","max_tokens":10,"messages":[]}' % MODEL.encode()


# ---- TOKEN-RESOLVE's run, as keys reads it -------------------------------------------------------------------------

def test_resolve_outcome_carries_run_and_refuses_a_malformed_one():
    ans = {"id": 1, "ok": True, "session": "s1", "claims": {"routes": ["glm"]}, "run": RUN}
    assert K.resolve_outcome(ans) == ("answer", {"session": "s1", "claims": {"routes": ["glm"], "run": RUN}})
    assert K.resolve_outcome({**ans, "run": {**RUN, "extra": 1}}) == ("failed",)
    assert K.resolve_outcome({**ans, "run": {**RUN, "reserved_microdollars": 0}}) == ("failed",)
    assert K.resolve_outcome({**ans, "run": {**RUN, "run_id": "attempt-1"}}) == ("failed",)
    # a `run` inside root's claims is never taken as the run
    assert K.resolve_outcome({**ans, "claims": {"routes": ["glm"], "run": RUN}}) == ("failed",)


def test_run_kind_without_run_refused():
    # 3c-5 fix 1 (L1 note 4): a session of a kind a ticket renders as, resolved with no `run`, is never cached as a
    # plain session (its `routes` could name a base route); it is refused, by name, and so is CONTROL's run-unknown
    plain = {"id": 1, "ok": True, "session": "s1", "claims": {"routes": ["claude-sub"], "kind": "interactive"}}
    assert K.resolve_outcome(plain)[0] == "answer"
    for kind in ("dispatch", "planner", "verifier"):
        ans = {**plain, "claims": {"routes": ["claude-sub"], "kind": kind}}
        assert K.resolve_outcome(ans) == ("refused", "run-unknown"), kind
        assert K.resolve_outcome({**ans, "run": RUN})[0] == "answer"
    assert K.resolve_outcome({"id": 1, "ok": False, "refused": "run-unknown"}) == ("refused", "run-unknown")
    assert K.RESOLVE_REFUSALS["run-unknown"] == "route-not-allowed"   # 403: no retry changes it


# ---- the run's own checks ----------------------------------------------------------------------------------------

def test_ticketed_q_wrong_model_refused(monkeypatch):
    # R4 §1.7 step 4: a ticketed run's `model` == its ticket's `model_requested`; else 403 model-not-allowed, with no
    # hold and no upstream. The right model is admitted on the run's scope at the run's cap (R4 §1.2).
    async def go(d: str) -> tuple:
        wrong = await _exchange(d, monkeypatch, BODY, _claims({**RUN, "model_requested": "z-ai/other"}),
                                dispatch_pol())
        right = await _exchange(d, monkeypatch, BODY, _claims(RUN), dispatch_pol())
        return wrong, right

    wrong, right = _run(go)
    assert wrong["frames"] == [(b"A", b"{}"), _f("model-not-allowed")], wrong["frames"]
    assert wrong["handshakes"] == 0 and not [x for x in wrong["lines"] if x["kind"] == "reserved"]
    assert _types(right["frames"]) == b"ARbe", right["frames"]
    held = [x for x in right["lines"] if x["kind"] == "reserved"]
    assert [(x["scope"], x["cap"]) for x in held] == [(["run", RUN["run_id"]], RUN["reserved_microdollars"])]


def test_keys_refuses_passthrough_for_dispatch(monkeypatch):
    # r3 §2.1 item 2, keys' half: a (forged) resolve answer whose run names a base-map route, a passthrough one or a
    # brokered interactive one, is 403 passthrough-for-dispatch with upstream 0; a run calling another route than its
    # own is route-not-allowed
    def base(d: str) -> None:
        with open(os.path.join(d, "routes", "base.json"), "wb") as f:
            f.write(canon.canonical_bytes({"format": "levain-routes-base/2", "routes": [
                base_route(), {"route": "claude-sub", "type": "subscription_passthrough",
                               "hosts": ["api.anthropic.com"]}]}))

    async def go(d: str) -> list:
        base(d)
        out = []
        for rid in ("claude-sub", "r"):
            out.append(await _exchange(d, monkeypatch, BODY, _claims({**RUN, "route": rid}, routes=(rid,)),
                                       dispatch_pol(), route=rid))
        out.append(await _exchange(d, monkeypatch, BODY, _claims(RUN, routes=("glm", "r")), dispatch_pol(),
                                   route="r"))
        return out

    passthrough, brokered, other = _run(go)
    for out in (passthrough, brokered):
        assert out["frames"][-1] == _f("passthrough-for-dispatch"), out["frames"]
        assert out["handshakes"] == 0 and not [x for x in out["lines"] if x["kind"] == "reserved"]
    assert other["frames"][-1] == _f("route-not-allowed") and other["handshakes"] == 0


# ---- H2: the body_policy rewrite ---------------------------------------------------------------------------------

def test_body_policy_replaces_client_provider(monkeypatch):
    # head ruling H2: on a dispatch route to OpenRouter, keys builds `provider` from the route's body_policy and
    # REPLACES the client's object (never a merge: the client's `order` / `ignore` and its `allow_fallbacks: true` are
    # gone); the forwarded body is that object, and the hold was estimated on it
    policy = {"provider_only": ["z-ai"], "allow_fallbacks": "no", "zdr": "yes", "data_collection": "deny"}
    body = (b'{"model":"%s","max_tokens":10,"messages":[],'
            b'"provider":{"order":["evil"],"ignore":["z-ai"],"allow_fallbacks":true}}' % MODEL.encode())

    async def go(d: str) -> tuple:
        _openrouter_cred(d)
        rewritten = await _exchange(d, monkeypatch, body, _claims(RUN),
                                    dispatch_pol("openrouter.ai", "c2", body_policy=policy))
        other_host = await _exchange(d, monkeypatch, body, _claims(RUN), dispatch_pol())
        return rewritten, other_host

    rewritten, other_host = _run(go)
    assert _types(rewritten["frames"]) == b"ARbe", rewritten["frames"]
    sent = json.loads(rewritten["sent"])
    assert sent["provider"] == {"only": ["z-ai"], "allow_fallbacks": False, "zdr": True, "data_collection": "deny"}
    # a dispatch route to another host is not an OpenRouter route: no rewrite there (its body_policy is refused at
    # ROUTE-SUBMIT when non-empty, tests/test_tickets.py)
    assert json.loads(other_host["sent"])["provider"]["order"] == ["evil"]


# ---- H1: the ticket string reaches no ledger or log line ---------------------------------------------------------

def test_ticket_string_in_no_ledger_or_log_line(st, monkeypatch):  # noqa: F811
    # a real CONTROL mints, binds and resolves a ticketed session; keys caches that answer and serves one exchange
    # with its gw.jsonl open. The ticket is in CONTROL's own journal only; the run is named by run_id everywhere else.
    rig = Rig(st, Clock())
    ticket, run_id, _att = bound_session(rig)
    rig.control._keys_front_register = lambda *a: 1
    assert root(rig, "MINT", session=SESSION, generation=to_rendering(rig),
                claims=d_mint_claims(front=dict(FRONT_CLAIMS["front"]), routes=["glm", "pypi"]))["ok"]
    res = rig.control.handle(C.Conn(KEYS_UID), {"v": 1, "op": "TOKEN-RESOLVE", "id": 1,
                                                 "token_digest": rig.control.handles(SESSION).token_digest})
    kind, answer = K.resolve_outcome(res)
    assert kind == "answer" and answer["claims"]["run"]["run_id"] == run_id
    assert ticket.encode() in (rig.sdir / "tickets.jsonl").read_bytes()   # CONTROL's journal holds it

    async def go(d: str) -> tuple:
        out = await _exchange(d, monkeypatch, BODY, answer["claims"], dispatch_pol(), gw_log=True)
        with open(os.path.join(d, *K.GW_LOG), "rb") as f:
            log = f.read()
        with open(out["ledger_path"], "rb") as f:
            ledger_bytes = f.read()
        return out, log, ledger_bytes

    out, log, ledger_bytes = _run(go)
    assert _types(out["frames"]) == b"ARbe", out["frames"]
    assert log and ledger_bytes
    assert run_id.encode() in ledger_bytes                                   # the line is about this run
    for name, data in (("ledger", ledger_bytes), ("gw.jsonl", log), ("resolve answer", json.dumps(res).encode())):
        assert ticket.encode() not in data, name
