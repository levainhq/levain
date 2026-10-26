"""S1 3b-8a: the slot's front is the real gateway front (``levain.broker.front``), not the session-2 stub.

``render`` writes the front's policy file ``R/gw/slot<n>/session.json`` (CONTRACT §4.3 "the front's view", §5.1 render
(3)) from the base map: the session's routes, no credential, no price table, the lease's incarnation. The front is
started with that file, its log ``L/gw/slot<n>/front.log`` (3b-8a T1), keys' ``R/keys/front.sock`` and the GO fd. The front built
from what render wrote TUNNELS a CONNECT to a host on one of the session's routes and REFUSES a host on none, and logs
both. Unprivileged: the World of ``test_slot_s2f2_runtime_helper`` (owners recorded, the front a fake process table);
the front itself is the real ``Front`` over a unix socket, its resolver and upstream fixtures (never the internet).
The VM row (a slot started by ``levain session`` running this front) is the residue."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from levain.broker import front as FR
from levain.contracts import canon
from levain.slot import runtime_helper as H
from levain.slot import vendor_routes as VR
from levain.slot.ids import SlotId
from tests.test_broker_front import PUBLIC, client_hello
from tests.test_slot_s2f2_runtime_helper import World, _blob, _prepared

#: a base map setup would write: the shipped vendor routes plus one egress route.
GITHUB = {"route": "github", "type": "egress", "hosts": ["github.com"]}
BASE_DOC = VR.merge({"format": VR.BASE_FORMAT, "routes": [GITHUB]})[0]


def _world(tmp_path: Path, host: str = "linux") -> World:
    w = World(tmp_path, host=host)
    (w.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(BASE_DOC))
    return w


def _arg(argv, flag: str) -> str:
    argv = list(argv)
    return argv[argv.index(flag) + 1]


def test_slot_front_is_the_gateway_front():
    assert H.FRONT_MODULE == "levain.broker.front"
    assert H.Seams().front_module == "levain.broker.front"


@pytest.mark.parametrize("host", ["linux", "darwin"])
def test_render_starts_the_front_on_its_policy_log_keys_and_go_fd(tmp_path, host):
    w = _world(tmp_path, host)
    if host == "darwin":
        acc = {"_levain_s0": 3999, "_levain_gw0": 3998, "_levain_dispatch": 3799}
        w.seams.account_uid = lambda name: acc[name]
        w.seams.group_gid = lambda g: {"levain-gwfronts": 3789}[g]
    s = _prepared(w)
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": True}, w.last_err
    (p,) = w.fronts.procs.values()
    argv = p["spec"]["argv"]
    i = argv.index("levain.broker.front")
    args = argv[i + 1:]
    gw = w.R / "gw" / "slot0"
    assert _arg(args, "--policy") == str(gw / "session.json")
    assert _arg(args, "--log") == str(w.L / "gw" / "slot0" / "front.log")
    assert _arg(args, "--keys") == str(w.R / "keys" / "front.sock")
    assert _arg(args, "--go-fd") == str(H.GO_FD)
    if host == "darwin":
        assert _arg(args, "--port") == str(FR.GWPORT_BASE) and "--slot-uid" not in args
    else:
        assert _arg(args, "--unix") == str(gw / "gw.sock")
        assert _arg(args, "--slot-uid") == str(w.alloc(s).slot_uid)
    # the front's own argparse takes every argument root gives it
    FR_ap = FR.build_parser()
    a = FR_ap.parse_args(args)
    assert a.go_fd == H.GO_FD


@pytest.fixture
def rendered(tmp_path):
    w = _world(tmp_path)
    s = _prepared(w)
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": True}, w.last_err
    return w, s


def test_policy_file_is_the_front_view(rendered):
    w, s = rendered
    path = w.R / "gw" / "slot0" / "session.json"
    doc = json.loads(path.read_bytes())
    assert doc["format"] == FR.POLICY_FORMAT and doc["session"] == s and doc["kind"] == "interactive"
    assert doc["incarnation"] == w.alloc(s).policy.incarnation
    assert [r["route"] for r in doc["routes"]] == ["anthropic", "github"]          # the blob's routes, in order
    for r in doc["routes"]:
        assert "credential" not in r and "prices" not in r
    pol = FR.load_policy(path.read_bytes())                                         # the front's own parser takes it
    assert set(pol.connect) == {"github.com"} and set(pol.brokered) == {"anthropic"}
    assert (os.stat(path).st_mode & 0o777) == 0o440
    assert w.chown_of("gw/slot0/session.json") == (0, w.seams.user_gid(898))


def test_front_from_rendered_files_tunnels_a_route_and_refuses_no_route(rendered):
    """The DONE row in-process: the real Front, built from the argv and the policy file render produced, answers a
    CONNECT to a route's host 200 and carries a TLS hello end to end; a host on no route is 403 with no upstream."""
    w, s = rendered
    (p,) = w.fronts.procs.values()
    argv = p["spec"]["argv"]
    args = argv[argv.index("levain.broker.front") + 1:]
    log = Path(_arg(args, "--log"))
    # the listener on a short path (a tmp_path is past AF_UNIX's length); render's own path is the test above's
    sock = os.path.join(tempfile.mkdtemp(prefix="s3b8a", dir="/tmp"), "gw.sock")
    policy = FR.load_policy(Path(_arg(args, "--policy")).read_bytes())

    async def body() -> None:
        calls: list[str] = []

        async def echo(r, wr):
            while data := await r.read(65536):
                wr.write(data)
                await wr.drain()
            wr.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        port = upstream.sockets[0].getsockname()[1]

        async def resolve(host, _port):
            return [PUBLIC]

        async def connect(addr, _port):
            calls.append(addr)
            return await asyncio.open_connection("127.0.0.1", port)

        front = FR.Front(policy, log_path=log, slot_uid=os.getuid(), resolve=resolve, connect=connect)
        server = await front.serve_unix(sock)

        async def ask(host: str) -> tuple[bytes, bytes, bytes]:
            r, wr = await asyncio.open_unix_connection(sock)
            wr.write(f"CONNECT {host}:443 HTTP/1.1\r\nHost: {host}:443\r\n\r\n".encode())
            await wr.drain()
            head = await r.readuntil(b"\r\n\r\n")
            hello, back = client_hello(host), b""
            if head.startswith(b"HTTP/1.1 200"):
                wr.write(hello)
                await wr.drain()
                while len(back) < len(hello) and (chunk := await r.read(65536)):
                    back += chunk
            wr.close()
            return head.split(b" ", 2)[1], hello, back

        try:
            status, hello, back = await ask("github.com")
            assert status == b"200" and back == hello and calls == [PUBLIC]
            status, _, _ = await ask("evil.example")
            assert status == b"403" and calls == [PUBLIC]                            # no upstream for it
            for _ in range(200):
                if not front.live:
                    break
                await asyncio.sleep(0.01)
        finally:
            server.close()
            front.close_live()
            front.unbind()
            upstream.close()

    asyncio.run(body())
    recs = [json.loads(ln) for ln in log.read_text().splitlines()]
    assert {(r["event"], r.get("host")) for r in recs} >= {("tunnel", "github.com"), ("refused", "evil.example")}
    assert all(r["session"] == s for r in recs)


def test_policy_the_front_would_refuse_refuses_render_before_any_front(tmp_path):
    """A base map whose egress route admits the brokered route's host: the base parser takes it, the front's does not
    (a brokered host is never a tunnel). render refuses by name before it spawns a front, never after."""
    w = _world(tmp_path)
    s = _prepared(w)
    doc = dict(BASE_DOC, routes=[r for r in BASE_DOC["routes"] if r["route"] != "github"]
               + [dict(GITHUB, hosts=["github.com", "api.anthropic.com"])])
    (w.L / "routes" / "base.json").write_bytes(canon.canonical_bytes(doc))
    code, line = w.main(["render", s], _blob())
    assert line == {"ok": False, "refused": "front-policy-refused"} and w.fronts.procs == {}
    assert not (w.R / "gw" / "slot0" / "session.json").exists()


def test_route_absent_from_base_map_refused_by_name(tmp_path):
    """A dispatch map's route has no base-map entry; its session file (the pinned generation's policy) is not built
    here, so render refuses by name rather than start a front that could not serve it."""
    with pytest.raises(H.Refused) as e:
        H.front_session_routes(BASE_DOC, ["github", "openrouter-glm"])
    assert e.value.name == "front-policy-unavailable"


def test_a_name_left_in_the_front_dir_is_replaced_never_followed(tmp_path):
    """R/gw/slot<n>/ is the front account's: a link it left at session.json is unlinked, not written through."""
    w = _world(tmp_path)
    target = tmp_path / "elsewhere"
    target.write_bytes(b"untouched")
    os.symlink(target, w.R / "gw" / "slot0" / "session.json")
    s = _prepared(w)
    assert w.main(["render", s], _blob())[1] == {"ok": True}, w.last_err
    path = w.R / "gw" / "slot0" / "session.json"
    assert not path.is_symlink() and json.loads(path.read_bytes())["session"] == s
    assert target.read_bytes() == b"untouched"


def test_front_dir_of_another_owner_refused(tmp_path):
    w = _world(tmp_path)
    w.seams.chowned_uid = lambda uid: os.getuid() + (uid == 898)   # gw0 (898) does not own R/gw/slot0
    s = _prepared(w)
    line = w.main(["render", s], _blob())[1]
    assert line == {"ok": False, "refused": "bad-root"} and w.fronts.procs == {}
    assert "gw/slot0 is not the front account" in w.last_err


def test_dispatch_session_view_drops_the_session_cap():
    """A dispatch or planner session's file carries no max_session_microdollars (the front's dispatch-session rule);
    the entry is otherwise the base map's (INTERIM: the pinned generation's entry is not read here)."""
    (anth,) = H.front_session_routes(BASE_DOC, ["anthropic"], dispatch=True)
    assert "max_session_microdollars" not in anth and "credential" not in anth and "prices" not in anth
    (inter,) = H.front_session_routes(BASE_DOC, ["anthropic"])
    assert inter["max_session_microdollars"] == VR.ANTHROPIC_SESSION_CAP
    doc = {"format": FR.POLICY_FORMAT, "kind": "dispatch", "session": "s-1", "incarnation": 1, "routes": [anth]}
    assert set(FR.load_policy(json.dumps(doc).encode()).brokered) == {"anthropic"}
