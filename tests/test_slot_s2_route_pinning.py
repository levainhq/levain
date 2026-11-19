"""S2 route pinning at render (CONTRACT §3.5 ROUTE PINNING, T:3c/test_route_pinned_across_new_generation).

A ticketed brokered session's front policy (``R/gw/slot<n>/session.json``) is written from the generation file its
ticket is pinned to (``L/routes/dispatch/<gen>.json``), never the base map and never a later generation: the entry's
``policy`` digest is recomputed first and must equal the ticket's, a route absent from the ACTIVE generation is refused
even when pinned, and a pinned generation root cannot load is refused. Every refusal is before render's first CAS.
Lane F2's world with the ticket store (test_pool's ``dispatch``); the signed path (CONTROL, keys) is test_tickets' and
test_routemaps'."""
from __future__ import annotations

import json
import os

import pytest

from levain.broker import front as FR
from levain.contracts import canon
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import routemaps as RM
from levain.slot import runtime_helper as H
from tests.test_pool import EXEC_FACTS, F2_DISPATCH, dispatch  # noqa: F401 - fixture
from tests.test_slot_3c7_fix_r1 import _mint, _render, write_pinned_generation
from tests.test_slot_s2f2_runtime_helper import BASE, BRIEF_DIGEST, _blob as _f2_blob

ROUTE = "glm-pin"


def _policy(rid: str = ROUTE, *, body_cap: int = 1048576) -> dict:
    """A dispatch-map route's `policy`: brokered, a codec, a per-call cap, no session cap."""
    return {"route": rid, "type": "brokered", "hosts": ["openrouter.ai"],
            "upstream": {"scheme": "https", "host": "openrouter.ai", "base_path": "/api"},
            "requests": {"templates": [{"method": "POST", "path": "/v1/chat/completions"}], "headers": {},
                         "body_cap": body_cap, "body_codec": "openai-chat"},
            "credential": "or-key", "max_call_microdollars": 2000000}


_write_generation = write_pinned_generation


def _facts(digest: str, gen: int = 1, route: str = ROUTE) -> dict:
    return dict(EXEC_FACTS, route=route, model_requested="m-1", brief_digest=BRIEF_DIGEST,
                routemap_generation=gen, route_policy_digest=digest)


def _blob(route: str = ROUTE) -> bytes:
    return _f2_blob(routes=[route, "github"], model="m-1")


def _ticketed(d, facts: dict) -> str:
    line = d.prepare(_mint(d, facts))
    assert line["ok"], (line, d.w.last_err)
    d.w.stage(line["session"])
    return line["session"]


def _front_file(w, s: str):
    return w.R / "gw" / f"slot{w.alloc(s).slot.index}" / "session.json"


def _refused_before_cas(d, s: str, name: str, route: str = ROUTE) -> None:
    w = d.w
    calls = len(w.calls)
    line = _render(d, s, _blob(route))
    assert line == {"ok": False, "refused": name}, (line, w.last_err)
    assert w.calls[calls:] == [] and w.state(s).state == R.ALLOCATED and w.alloc(s).policy is None
    assert not _front_file(w, s).exists() and w.fronts.procs == {}


def test_route_pinned_across_new_generation(dispatch):
    """A new generation re-points the route id at another policy: the ticket's session still gets the PINNED one."""
    d, w = dispatch, dispatch.w
    pinned, later = _policy(), _policy(body_cap=2097152)
    _write_generation(w, 1, [pinned])
    s = _ticketed(d, _facts(RM.policy_digest(pinned)))
    _write_generation(w, 2, [later])                                     # active now; the ticket is pinned to 1
    line = _render(d, s, _blob())
    assert line == {"ok": True}, (line, w.last_err)
    doc = json.loads(_front_file(w, s).read_bytes())
    (view,) = [r for r in doc["routes"] if r["route"] == ROUTE]
    assert view["requests"]["body_cap"] == 1048576                      # generation 1's, not generation 2's
    assert "credential" not in view and "max_session_microdollars" not in view
    assert [r["route"] for r in doc["routes"]] == [ROUTE, "github"]    # the blob's order, github from the base map
    assert doc["kind"] == "dispatch" and doc["session"] == s
    FR.load_policy(_front_file(w, s).read_bytes())                      # the front's own parser takes it
    assert (os.stat(_front_file(w, s)).st_mode & 0o777) == 0o440


def test_the_pinned_generation_alone_is_enough_when_it_is_the_active_one(dispatch):
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    assert _render(d, s, _blob()) == {"ok": True}, w.last_err


def test_a_pinned_file_whose_policy_digest_is_not_the_tickets_refuses_mismatch(dispatch):
    d, w = dispatch, dispatch.w
    s = _ticketed(d, _facts(RM.policy_digest(_policy())))
    _write_generation(w, 1, [_policy(body_cap=2097152)])                # the file is not what the ticket pinned
    _refused_before_cas(d, s, "route-policy-mismatch")


def test_a_digest_is_over_the_policy_member_only(dispatch):
    """A change to the entry's other members (models, body_policy) keeps the pin: the digest is the policy's alone."""
    d, w = dispatch, dispatch.w
    p = _policy()
    path = _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    obj = canon.parse(open(path, "rb").read())
    obj["body"]["routes"][0]["retention_claim"] = "another claim"
    open(path, "wb").write(canon.canonical_bytes(obj))
    assert _render(d, s, _blob()) == {"ok": True}, w.last_err


def test_a_route_removed_from_the_active_generation_is_refused_though_pinned(dispatch):
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    _write_generation(w, 2, [_policy("other-route")])                   # the route is gone from the active generation
    _refused_before_cas(d, s, "route-not-allowed")


@pytest.mark.parametrize("how", ["absent", "garbage", "not-canonical", "no-body", "link"])
def test_a_pinned_generation_root_cannot_load_is_refused_not_allowed(dispatch, how):
    d, w = dispatch, dispatch.w
    p = _policy()
    path = _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    good = open(path, "rb").read()
    os.unlink(path)
    if how == "garbage":
        open(path, "wb").write(b"\x00not json")
    elif how == "not-canonical":
        open(path, "wb").write(json.dumps(canon.parse(good), indent=1).encode())
    elif how == "no-body":
        open(path, "wb").write(canon.canonical_bytes({"generation": 1}))
    elif how == "link":
        (w.L / "routes" / "elsewhere.json").write_bytes(good)
        os.symlink(w.L / "routes" / "elsewhere.json", path)
    _refused_before_cas(d, s, "route-not-allowed")


@pytest.mark.parametrize("current", [None, b"", b"0", b"01", b"2", b"x"])
def test_an_active_generation_root_cannot_read_refuses_not_allowed(dispatch, current):
    """`current` absent or malformed, or naming a generation with no file: the active generation is unknown."""
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    cur = w.L / "routes" / "dispatch" / "current"
    os.unlink(cur)
    if current is not None:
        cur.write_bytes(current)
    _refused_before_cas(d, s, "route-not-allowed")


def test_a_route_that_is_also_a_base_route_is_refused_not_allowed(dispatch):
    """The pinned entry is never the base map's, and a base id colliding with the dispatch route fails closed."""
    d, w = dispatch, dispatch.w
    clash = next(r["route"] for r in BASE["routes"] if r["type"] == "brokered")
    p = _policy(clash)
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p), route=clash))
    _refused_before_cas(d, s, "route-not-allowed", route=clash)


def test_a_pinned_route_that_is_not_brokered_is_refused(dispatch):
    d, w = dispatch, dispatch.w
    p = {"route": ROUTE, "type": "egress", "hosts": ["openrouter.ai"]}
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    _refused_before_cas(d, s, "route-not-allowed")


def test_the_digest_here_is_routemaps_digest(dispatch):
    """root's digest of a policy is the one CONTROL and keys compute (levain.slot.routemaps.policy_digest)."""
    p = _policy()
    assert H.policy_digest(p) == RM.policy_digest(p)


def test_front_policy_bytes_of_a_ticketed_alloc_is_the_pinned_entrys(dispatch):
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    store = H.Store(w.seams)
    try:
        doc = json.loads(H.front_policy_bytes(store, w.alloc(s), [ROUTE, "github"]))
    finally:
        store.close()
    assert [r["route"] for r in doc["routes"]] == [ROUTE, "github"]


def test_front_policy_bytes_names_the_missing_pinned_route_first(dispatch):
    """A blob that omits the pinned route AND names an id the base map lacks is refused as ticket-routes-mismatch:
    the membership check runs before any lookup that could refuse for another reason (L3 r2, three lineages)."""
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    store = H.Store(w.seams)
    try:
        with pytest.raises(H.Refused) as ei:
            H.front_policy_bytes(store, w.alloc(s), ["github", "no-such-route"])
    finally:
        store.close()
    assert ei.value.name == "ticket-routes-mismatch"


def test_start_names_the_pinned_routes_secrets_not_route_unknown(dispatch):
    """`start`'s secret names are taken under the session's route types: the pinned route is brokered there too."""
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    store = H.Store(w.seams)
    try:
        types = H.session_route_types(store, w.alloc(s), H.base_route_types(store))
    finally:
        store.close()
    assert types[ROUTE] == "brokered" and types["github"] == "egress"


def test_render_facts_takes_types_and_hosts_from_one_base_map_read(dispatch, monkeypatch):
    """L3 r1 (codex, complement LOW): the route types, the hosts and the pin's base-id check are one read of
    `L/routes/base.json`, so a map replaced between reads cannot pair one map's types with another's hosts."""
    d, w = dispatch, dispatch.w
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    reads = []
    real = H.Store.read

    def counted(self, parts, name, *a, **kw):
        if tuple(parts) == ("routes",) and name == "base.json":
            reads.append(name)
        return real(self, parts, name, *a, **kw)
    monkeypatch.setattr(H.Store, "read", counted)
    store = H.Store(w.seams)
    try:
        from levain.slot.runtime_grammar import RenderBlob

        ctx = H.Ctx(w.seams, store, 0, "dispatch", 0, {})
        facts = H.render_facts(ctx, w.alloc(s), RenderBlob(vendor="claude", routes=(ROUTE, "github"), model="m-1"))
    finally:
        store.close()
    assert facts.route_types[ROUTE] == "brokered" and facts.route_hosts["github"]
    assert reads == ["base.json"]


def _start_world(w):
    """test_slot_s2f3_start's fake launch table on the dispatch world (Linux: the clone, the forwarder, the vendor)."""
    from tests.test_slot_s2c_end import mine, thread_run_as
    from tests.test_slot_s2f3_start import Launches, Table

    table = Table()
    la = Launches(w, table)
    s = w.seams
    s.spawn = lambda argv, spec_json, cgroup, exec_w, detach, **_capture: la.spawn(argv, spec_json, cgroup, exec_w,
                                                                                    detach)
    s.wait, s.wait_within, s.kill = la.wait, la.wait_within, la.kill
    s.capture = la.capture
    s.netns_names = lambda: {"levain-e0", "levain-e1", "levain-e2", "levain-v0"}
    s.host_links = lambda: {"/bin": "usr/bin", "/sbin": "usr/sbin", "/lib": "usr/lib", "/lib64": None}

    def make_cgroup(path):
        table.cgroup_made = True
    s.make_cgroup = make_cgroup
    s.egress_ready = lambda slot, ids: None
    s.proc_seams = table.seams()
    s.end_overrides = {"owner_ok": mine, "run_as": thread_run_as(), "root_uid": os.getuid(),
                       "root_gid": os.getgid(), "sleep": lambda x: None}
    s.clock = lambda: 1_790_000_000.0
    return la


def _rendered_ticketed(d) -> tuple:
    w = d.w
    (w.L / "retained").mkdir(mode=0o700, exist_ok=True)
    la = _start_world(w)
    p = _policy()
    _write_generation(w, 1, [p])
    s = _ticketed(d, _facts(RM.policy_digest(p)))
    w.session = s
    assert _render(d, s, _blob()) == {"ok": True}, w.last_err
    return s, la


def test_a_route_withdrawn_after_render_stops_start_before_the_clone(dispatch):
    """L3 r1 (codex MED): a route withdrawn between render and start refuses `start` before anything permanent: no
    clone marker, no forwarder, no spawn, the record still RENDERED, so the refusal is the route's and not, on a
    retry, `already-started`."""
    d, w = dispatch, dispatch.w
    s, la = _rendered_ticketed(d)
    _write_generation(w, 2, [_policy("other-route")])
    line = w.main(["start", s], uid=F2_DISPATCH)[1]
    assert line == {"ok": False, "refused": "route-not-allowed"}, (line, w.last_err)
    assert la.spawns == [] and not (w.L / "alloc" / f"{s}.cloned").exists()
    assert w.state(s).state == R.RENDERED
    assert w.main(["start", s], uid=F2_DISPATCH)[1]["refused"] == "route-not-allowed"


def test_a_ticketed_session_starts_with_its_pinned_routes_token(dispatch, monkeypatch):
    """The pinned route is the session's brokered route at start: the vendor is given the token's carrier. (The
    read-set clone and its check are test_slot_s2f3_start's; here the clone step is a stand-in.)"""
    from levain.slot import start as SS

    d, w = dispatch, dispatch.w
    s, la = _rendered_ticketed(d)
    monkeypatch.setattr(SS, "_clone_repo", lambda ctx, a: None)
    w.main(["start", s], uid=F2_DISPATCH)           # the drain of a fake vendor's capture is not this test's
    (v,) = la.of("vendor")
    assert set(v["spec"]["extra_env"]) == {RD.TOKEN_ENV["claude"]}
