"""levain#66 S3a: the front admits an off-list CONNECT on a live entry of root's grants file, and only then.

Real sockets on both sides, the resolver and the upstream's address are fixtures (never the internet), the clock is a
fake the test moves, and every grants file is a real file (mode 0440) in a tmp dir. The harness shape is
``tests/test_broker_front.py``'s, copied rather than edited (that file stays unchanged)."""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import pytest

from levain.broker import front as FR
from levain.broker.front import Front, Refused, load_policy
from levain.broker.norm import STATUS
from levain.slot import egress_grants as eg
from tests.test_broker_front import PUBLIC, _connect, client_hello

SESSION = "s-0123456789abcdef"
OTHER_SESSION = "s-fedcba9876543210"
SLOT = 3
T0 = 1_800_000_000.0
HOST = "granted.example"


def _policy(kind: str = "interactive", brokered: bool = False) -> dict:
    routes = [{"route": "egress-test", "type": "egress", "hosts": ["listed.example"]}]
    if brokered:
        routes.append({"route": "vendor", "type": "brokered", "hosts": ["api.vendor.example"],
                       "upstream": {"scheme": "https", "host": "api.vendor.example", "base_path": ""},
                       "requests": {"templates": [{"method": "POST", "path": "/v1/messages?*"}], "headers": {},
                                    "body_cap": 1048576, "body_codec": "anthropic-messages"},
                       "max_call_microdollars": 2000000})
    return {"format": "levain-front-session/1", "kind": kind, "session": SESSION, "routes": routes}


def _when(offset: float) -> datetime:
    return datetime.fromtimestamp(T0 + offset, tz=timezone.utc)


def _entry(host: str = HOST, grant: str = "eh-" + "a" * 24, ttl: float = 100.0) -> eg.Entry:
    return eg.Entry(grant=grant, host=host, port=443, expires=_when(ttl), choice="s")


class Rig:
    """What a test reaches: the file writer, the fake clock, the spies and the front."""

    def __init__(self, tmp: Path) -> None:
        self.dir = tmp / f"slot{SLOT}"
        self.dir.mkdir()
        self.path = self.dir / "grants.json"
        self.now = [T0]
        self.reads = [0]
        self.front: Optional[Front] = None
        self.up_bytes = [0]
        self.log = tmp / "front.jsonl"
        self.on_resolve = None

    def write(self, entries, session: str = SESSION, slot: int = SLOT, mode: int = 0o440, raw: Optional[bytes] = None
              ) -> None:
        data = raw if raw is not None else eg.dump_grants(session, slot, entries)
        tmp = self.dir / "grants.json.new"
        tmp.write_bytes(data)
        os.chmod(tmp, mode)
        os.chown(tmp, -1, os.getgid())
        os.replace(tmp, self.path)

    def records(self) -> list:
        return [json.loads(ln) for ln in self.log.read_text().splitlines()] if self.log.exists() else []

    def refused(self) -> list:
        return [r for r in self.records() if r.get("event") == "refused"]


async def _run(fn, *, kind: str = "interactive", brokered: bool = False, grants: bool = True,
               dns: Optional[dict] = None, **front_kw) -> None:
    calls: list = []
    dns = dns or {}

    with tempfile.TemporaryDirectory() as td:
        rig = Rig(Path(td))

        def counting_read(path, *, owner_uid=0, group_gid=None):
            rig.reads[0] += 1
            return eg.read_grants_file(path, owner_uid=owner_uid, group_gid=group_gid)

        async def resolve(host: str, port: int):
            if rig.on_resolve is not None:
                rig.on_resolve(host)
            return dns.get(host, [PUBLIC])

        async def echo(r, w):
            while data := await r.read(65536):
                rig.up_bytes[0] += len(data)
                w.write(data)
                await w.drain()
            w.close()

        upstream = await asyncio.start_server(echo, "127.0.0.1", 0)
        up_port = upstream.sockets[0].getsockname()[1]

        async def connect(addr: str, port: int):
            calls.append(addr)
            return await asyncio.open_connection("127.0.0.1", up_port)

        kw: dict = dict(log_path=rig.log, slot_uid=os.getuid(), resolve=resolve, connect=connect)
        if grants:
            kw.update(grants_path=str(rig.path), slot=SLOT, grants_owner_uid=os.getuid(), grants_gid=os.getgid(),
                      clock=lambda: rig.now[0], read_grants=counting_read)
        kw.update(front_kw)
        rig.front = Front(load_policy(json.dumps(_policy(kind, brokered)).encode()), **kw)
        sock = str(Path(td) / "gw.sock")
        server = await rig.front.serve_unix(sock)

        async def ask(target: str, sni: Optional[str] = "", settle: bool = True):
            """CONNECT `target`; on a 200 send a ClientHello and read the echo. (status, upstream opens, echoed)."""
            before = len(calls)
            r, w = await asyncio.open_unix_connection(sock)
            w.write(_connect(target))
            await w.drain()
            head = await r.readuntil(b"\r\n\r\n")
            back = b""
            if head.startswith(b"HTTP/1.1 200"):
                name = target.rsplit(":", 1)[0] if sni == "" else sni
                hello = client_hello(name)
                w.write(hello)
                await w.drain()
                while len(back) < len(hello) and (chunk := await r.read(65536)):
                    back += chunk
            w.close()
            if settle:
                for _ in range(200):   # a tunnel's record is written before its handler leaves `live`
                    if not rig.front.live:
                        break
                    await asyncio.sleep(0.01)
            return int(head.split(b" ", 2)[1]), len(calls) - before, back

        try:
            await fn(ask, rig)
        finally:
            server.close()
            rig.front.close_live()
            rig.front.unbind()
            upstream.close()


def _go(fn, **kw) -> None:
    asyncio.run(_run(fn, **kw))


# --- T1 admit, T2 binding, T3 expiry ----------------------------------------------------------------------------

def test_t1_live_entry_admits_and_the_tunnel_is_a_plain_one():
    """Fails if the off-list branch is not wired (403) or a granted tunnel is logged as passthrough."""
    async def body(ask, rig: Rig) -> None:
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)                      # no file: refused
        rig.write([_entry()])
        status, opens, back = await ask(f"{HOST}:443")
        assert (status, opens) == (200, 1) and back.startswith(b"\x16\x03")
        (t,) = [r for r in rig.records() if r.get("event") == "tunnel"]
        assert t["route"] == "egress-grant:" + "eh-" + "a" * 24 and t["passthrough"] is False and t["host"] == HOST
        assert "tunnel_id" not in t and "passthrough_total" not in t
        assert (await ask("LISTED.example:443"))[:2] == (200, 1)               # the listed host is unchanged

    _go(body)


@pytest.mark.parametrize("what", ["session", "slot"])
def test_t2_another_session_or_slot_admits_nothing(what):
    """Fails if live_entry is given the file's own session/slot instead of the policy's and the argv's."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()], **({"session": OTHER_SESSION} if what == "session" else {"slot": SLOT + 1}))
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)
        assert rig.refused()[-1]["detail"] == "off-list"
        rig.write([_entry()])
        assert (await ask(f"{HOST}:443"))[:2] == (200, 1)

    _go(body)


def test_t3_expiry_is_judged_at_the_decision():
    """Fails with `>=` for `>` or a naive-clock compare: expires - 1 s admits, expires + 0 s and later do not."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry(ttl=10)])
        rig.now[0] = T0 + 9
        assert (await ask(f"{HOST}:443"))[:2] == (200, 1)
        rig.now[0] = T0 + 10
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)
        rig.now[0] = T0 + 11
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)

    _go(body)


# --- T4, T5, T6, T7, T8 ---------------------------------------------------------------------------------------

def test_t4_port_and_authority_never_consult_grants():
    """Fails if grants are consulted on any host-not-allowed: the read spy stays at zero for these refusals."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        for target, detail in ((f"{HOST}:8443", "port"), (f"{HOST}:22", "port"), ("[::1]:443", "authority")):
            assert (await ask(target))[:2] == (403, 0), target
            assert rig.refused()[-1]["detail"] == detail
        assert rig.reads[0] == 0

    _go(body)


def test_t5_an_address_literal_or_junk_host_costs_no_read():
    """Reproduces F10: `1.2.3.4:443` matches the authority regex, so only the is_host_name gate keeps it off disk."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        for target in ("1.2.3.4:443", "granted.example.:443", "0x7f.1:443", "UPPER_.example:443"):
            assert (await ask(target))[:2] == (403, 0), target
            assert rig.refused()[-1]["detail"] == "off-list"
        assert rig.reads[0] == 0
        assert (await ask(f"{HOST}:443"))[:2] == (200, 1) and rig.reads[0] >= 1       # positive control

    _go(body)


def test_t6_a_brokered_host_is_never_granted():
    """Fails if the grant path admits a vendor API host (tunnelling around the data plane); builds the policy through
    load_policy and asserts brokered_hosts holds both the route's hosts and its upstream host."""
    pol = load_policy(json.dumps(_policy(brokered=True)).encode())
    assert pol.brokered_hosts == frozenset({"api.vendor.example"})

    async def body(ask, rig: Rig) -> None:
        rig.write([_entry(), _entry(host="api.vendor.example", grant="eh-" + "b" * 24)])
        assert (await ask("api.vendor.example:443"))[:2] == (403, 0)
        assert rig.refused()[-1]["detail"] == "off-list" and rig.reads[0] == 0
        assert (await ask(f"{HOST}:443"))[:2] == (200, 1)

    _go(body, brokered=True)


def test_t7_a_granted_name_that_resolves_private_is_address_refused():
    """Fails if a grant route skips _open_upstream's address check."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)
        assert rig.refused()[-1]["refused"] == "address-refused"

    _go(body, dns={HOST: ["127.0.0.1"]})


def test_t8_the_sni_must_be_the_granted_host():
    """Fails if the grant route's host is not the CONNECT host: a hello for another name is closed, sni-mismatch."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        status, opens, back = await ask(f"{HOST}:443", sni="other.example")
        assert (status, opens, back) == (200, 1, b"")
        assert rig.refused()[-1]["detail"] == "sni-mismatch" and rig.up_bytes[0] == 0

    _go(body)


# --- T9 bad files, T10 the refusal line -----------------------------------------------------------------------------

def _bad_missing(rig):
    pass


def _bad_symlink(rig):
    rig.write([_entry()])
    real = rig.dir / "real.json"
    os.replace(rig.path, real)
    os.symlink(real, rig.path)


def _bad_mode(mode):
    def f(rig):
        rig.write([_entry()], mode=mode)
    return f


def _bad_fifo(rig):
    os.mkfifo(rig.path, 0o440)


def _bad_big(rig):
    rig.write([], raw=b" " * (eg.GRANTS_FILE_MAX + 1))


def _bad_never_grant_entry(rig):
    raw = eg.dump_grants(SESSION, SLOT, [_entry(), _entry(host="other.example", grant="eh-" + "c" * 24)])
    rig.write([], raw=raw.replace(b"other.example", b"localhost"))


def _bad_owner(rig):
    rig.write([_entry()])
    rig.front.grants.owner_uid = os.getuid() + 1


def _bad_gid(rig):
    rig.write([_entry()])
    rig.front.grants.gid = os.getgid() + 1


@pytest.mark.parametrize("make", [_bad_missing, _bad_symlink, _bad_mode(0o444), _bad_mode(0o460), _bad_fifo,
                                  _bad_big, _bad_never_grant_entry, _bad_owner, _bad_gid],
                         ids=["missing", "symlink", "world-readable", "group-writable", "fifo", "over-16KiB",
                              "one-bad-entry-voids-all", "owner-mismatch", "gid-mismatch"])
def test_t9_an_unreadable_or_corrupt_file_is_no_grants_and_the_front_serves_on(make):
    """Fails if an exception escapes the grant path (the front task dies) or the good entries of a bad file are
    trusted. A FIFO must not hang (O_NONBLOCK): the whole case has a 5 s timeout."""
    async def body(ask, rig: Rig) -> None:
        make(rig)
        assert (await asyncio.wait_for(ask(f"{HOST}:443"), 5))[:2] == (403, 0)
        assert rig.refused()[-1]["detail"] == "off-list"
        assert (await ask("listed.example:443"))[:2] == (200, 1)

    _go(body)


def test_t10_an_off_list_miss_is_logged_exactly_as_without_grants():
    """Fails if S2's egress-refusals reader or S4's card loses its trigger: the record is the no-grants front's."""
    seen = {}

    async def body(ask, rig: Rig) -> None:
        assert (await ask("evil.example:443"))[:2] == (403, 0)
        seen[bool(rig.front.grants)] = {k: v for k, v in rig.refused()[-1].items() if k != "at"}   # the time stamp

    _go(body, grants=False)
    _go(body, grants=True)
    assert seen[False] == seen[True] == {"event": "refused", "refused": "host-not-allowed", "detail": "off-list",
                                         "host": "evil.example", "remedy": FR.OFF_LIST_REMEDY, "session": SESSION}


# --- T15 the race at the 200 ----------------------------------------------------------------------------------------

def test_t15_a_grant_revoked_while_the_upstream_opens_is_refused_grant_ended():
    """Reproduces a revoke landing inside _open_upstream: no 200, the upstream closed, a detail that is not
    `off-list` (so no second card), and no byte upstream."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        rig.on_resolve = lambda host: rig.write([])
        assert (await ask(f"{HOST}:443"))[:2] == (403, 1)
        last = rig.refused()[-1]
        assert (last["refused"], last["detail"]) == ("host-not-allowed", "grant-ended") and "tunnel" not in str(
            [r["event"] for r in rig.records()])
        assert rig.up_bytes[0] == 0
        # a different id for the same host is also an ended grant
        rig.on_resolve = lambda host: rig.write([_entry(grant="eh-" + "d" * 24)])
        rig.write([_entry()])
        assert (await ask(f"{HOST}:443"))[:2] == (403, 1)
        assert rig.refused()[-1]["detail"] == "grant-ended"

    _go(body)


# --- T16' the clock never latches, T17 a bad clock --------------------------------------------------------------------

def test_t16_a_bad_forward_reading_latches_nothing():
    """Reproduces the v1 wedge: one reading past the expiry must not outlive itself (no high-water mark)."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry(ttl=10)])
        rig.now[0] = T0 + 100
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)
        rig.now[0] = T0 + 5
        assert (await ask(f"{HOST}:443"))[:2] == (200, 1)

    _go(body)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1e20, "soon", None])
def test_t17_a_clock_that_gives_no_time_admits_nothing_and_never_kills_the_front(bad):
    """Fails if NaN, inf, an out-of-range value (OverflowError/OSError from fromtimestamp) or a non-number escapes."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        rig.now[0] = bad
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)
        assert (await ask("listed.example:443"))[:2] == (200, 1)
        rig.now[0] = T0
        assert (await ask(f"{HOST}:443"))[:2] == (200, 1)

    _go(body)


def test_t17_a_clock_that_raises_is_no_time_and_left_is_expired():
    def boom():
        raise RuntimeError("clock")

    g = FR._Grants("/nonexistent/slot3/grants.json", SLOT, 0, None, clock=boom)
    assert g.now() is None and g.left(_entry()) == 0.0
    ok = FR._Grants("/nonexistent/slot3/grants.json", SLOT, 0, None, clock=lambda: T0)
    assert ok.left(_entry(ttl=100)) == 100.0 and ok.left(_entry(ttl=-5)) == -5.0


# --- T18 no grants configured -----------------------------------------------------------------------------------------

def test_t18_a_front_with_no_grants_reads_nothing():
    """Fails if a default reads some path: every construction without the keywords is today's behaviour."""
    reads = []

    async def body(ask, rig: Rig) -> None:
        assert rig.front.grants is None
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)

    _go(body, grants=False, read_grants=lambda *a, **k: reads.append(a))
    assert reads == []
    f = Front(load_policy(json.dumps(_policy()).encode()), grants_path="/x/slot1/grants.json")   # path but no slot
    assert f.grants is None and Front(load_policy(json.dumps(_policy()).encode()), slot=1).grants is None


# --- T19 dispatch kinds -----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kind", ["dispatch", "planner"])
def test_t19_an_unattended_session_is_refused_unattended_with_no_read(kind):
    """Fails if grants admit in an unattended session. The line keeps the remedy (H2) and is not an `off-list` line,
    so S2's `_refused_host` never picks it for a card."""
    async def body(ask, rig: Rig) -> None:
        rig.write([_entry()])
        assert (await ask(f"{HOST}:443"))[:2] == (403, 0)
        last = rig.refused()[-1]
        assert (last["refused"], last["detail"]) == ("host-not-allowed", "unattended")
        assert last["remedy"] == FR.OFF_LIST_REMEDY and last["detail"] != "off-list" and rig.reads[0] == 0

    _go(body, kind=kind)


def test_t19_login_and_interactive_are_admitted():
    for kind in ("interactive", "login"):
        async def body(ask, rig: Rig) -> None:
            rig.write([_entry()])
            assert (await ask(f"{HOST}:443"))[:2] == (200, 1)

        _go(body, kind=kind)


# --- T25 the names ----------------------------------------------------------------------------------------------------

def test_t25_the_new_details_are_old_names_so_refuse_writes_them_without_raising():
    """Fails if a new refusal name were added: STATUS is keyed by name, so a new name raises KeyError."""
    for detail in ("grant-ended", "unattended"):
        r = Refused("host-not-allowed", detail)
        assert r.status == 403 == STATUS["host-not-allowed"]

        class W:
            def __init__(self) -> None:
                self.out = b""

            def write(self, b): self.out += b

            async def drain(self): pass

            def can_write_eof(self): return False

        class R:
            async def read(self, n): return b""

        front = Front(load_policy(json.dumps(_policy()).encode()))
        w = W()
        asyncio.run(front._refuse(R(), w, r, b"x.example:443"))
        assert w.out.startswith(b"HTTP/1.1 403 ") and b"X-Levain-Refused: host-not-allowed" in w.out


# --- T14 / T23 main's argv ----------------------------------------------------------------------------------------------

def _main_args(tmp_path: Path, **over) -> list:
    pol = tmp_path / "session.json"
    pol.write_text(json.dumps(dict(_policy(), incarnation=1)))
    d = dict(slot="3", grants=str(tmp_path / "run" / "grants" / "slot3" / "grants.json"),
             port=str(FR.GWPORT_BASE + 3))
    d.update(over)
    argv = ["--policy", str(pol), "--keys", str(tmp_path / "front.sock"), "--go-fd", "9"]
    if d.get("port") is not None:
        argv += ["--port", d["port"]]
    if d.get("grants") is not None:
        argv += ["--grants", d["grants"]]
    if d.get("slot") is not None:
        argv += ["--slot", d["slot"]]
    return argv


@pytest.mark.parametrize("over", [
    dict(slot=None),                                                          # --grants without --slot
    dict(grants=None),                                                        # --slot without --grants
    dict(slot="64", port=None, grants="/r/grants/slot64/grants.json"),        # past the slot space
    dict(slot="-1", port=None, grants="/r/grants/slot-1/grants.json"),
    dict(grants="relative/slot3/grants.json"),                                # not absolute
    dict(grants="/r/grants/slot3/other.json"),                                # the basename is not grants.json
    dict(grants="/r/grants/slot4/grants.json"),                               # the parent is another slot
    dict(grants="/r/grants/gw/grants.json"),
    dict(port=str(FR.GWPORT_BASE + 4)),                                       # TCP: the port is not GWPORT_BASE + slot
], ids=["no-slot", "no-grants", "slot-64", "slot-neg", "relative", "basename", "parent-slot", "parent-name",
        "port-mismatch"])
def test_t14_t23_main_refuses_a_bad_grants_argv_before_go(tmp_path, monkeypatch, over):
    """Fails if the front starts with a half-configured or self-contradicting grants path: exit 2 before the GO fd."""
    served: list = []

    async def fake_run_front(front, *, unix=None, port=None, keys=None):
        served.append(front)
        return 0

    monkeypatch.setattr(FR, "run_front", fake_run_front)
    monkeypatch.setattr(FR.os, "read", lambda *a: pytest.fail("the GO fd was read"))
    with pytest.raises(SystemExit) as e:
        FR.main(_main_args(tmp_path, **over))
    assert e.value.code == 2 and served == []


def test_t14_main_builds_a_front_with_grants_and_the_effective_gid(tmp_path, monkeypatch):
    """The positive control for the cases above, and getegid (the process credential) is the group checked."""
    served: list = []

    async def fake_run_front(front, *, unix=None, port=None, keys=None):
        served.append(front)
        return 0

    rfd, wfd = os.pipe()
    os.write(wfd, b"g")
    os.close(wfd)
    monkeypatch.setattr(FR, "run_front", fake_run_front)
    monkeypatch.setattr(FR.os, "getegid", lambda: 4242)
    args = _main_args(tmp_path)
    args[args.index("--go-fd") + 1] = str(rfd)
    assert FR.main(args) == 0
    (front,) = served
    assert front.grants.slot == 3 and front.grants.gid == 4242 and front.grants.owner_uid == 0
    assert front.grants.path == str(tmp_path / "run" / "grants" / "slot3" / "grants.json")
