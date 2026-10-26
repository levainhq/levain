"""R15-14-1: the round-3 probe suite (B1, B4, B6, B7, B9 plain, B12), ported from the spike glue to the CH-D2 glue
(`fixtures.stack.ProdGlueRun`: the production spawn over the same `origin.py` rig and the same port stand-ins).

Each probe states the round-3 verdict (ROUND3.md, the patched column) and asserts it in what the production stack
exposes: the client's reply, `origin.py`'s byte log, the glue's `wall` events, the decider's `egress_attempt` rows (the
spike's decider frames have no production counterpart) and srt's lookups (the glue's test-seam trace, in place of the
spike's DNS responder log). Each wall family has a negative control on the same observers: a glue that admits the off
port (B1), a glue that trusts the bad CA (B6), a client that stays (B7), a decider that would key plain HTTP (B12).
B9's TLS rows run against the same spawn in `composed/test_tls_probes.py` and `composed/test_smuggling.py`.
"""

from __future__ import annotations

import threading
import time

import pytest

from ..conftest import build_rig
from ..fixtures import clients
from ..fixtures.stack import HELD_KEY, REBIND, ProdGlueRun
from ._util import client

pytestmark = pytest.mark.chat_egress_srt

CRED = ["-H", "Authorization: Bearer CLIENT-SECRET"]


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    r = build_rig(tmp_path_factory, "chd2", origin="bytes", dns=False)
    yield r
    r.stop()


def _start(rig, name: str, **kw) -> ProdGlueRun:
    g = ProdGlueRun(rig, name, **kw)
    rig.glues.append(g)                                  # Rig.stop() stops it whatever the test did
    return g.start()


@pytest.fixture(scope="module")
def glue(rig):
    """round 3's B2 shape: the CH-D2 glue, closed env (the production spawn's), upstream trust = the origin CA only."""
    g = _start(rig, "B2")
    yield g
    _stop_clean(g)


def _stop_clean(g: ProdGlueRun) -> None:
    """Stops `g` and holds its StopResult to test_launch.py's teardown bar (exit 0, clean decider and SOCKS, the final
    flush written)."""
    r = g.stop()
    assert r.glue_exit == 0 and r.decider_clean and r.socks_clean and not r.failures, r
    assert r.final_flush is not None and r.final_flush.get("final") is True, r


class Obs:
    """What a probe moved: origin bytes, walls, decider attempts and srt lookups, counted from a mark."""

    def __init__(self, rig, glue) -> None:
        self.rig, self.glue = rig, glue
        self.m = rig.mark(glue)
        self.n_att, self.n_look = len(glue.attempts()), len(glue.lookups())

    def done(self, settle: float = 0.9):
        seen = self.rig.since(self.m, self.glue, settle=settle)
        self.seen = seen
        self.walls = [e for e in seen.events if e.get("t") == "wall"]
        self.attempts = self.glue.attempts()[self.n_att:]
        self.lookups = self.glue.lookups()[self.n_look:]
        self.raw = " ".join(d["raw"] for d in seen.origin if d.get("bytes"))
        return self

    def facts(self) -> str:
        return (f"attempts={len(self.attempts)} lookups={self.lookups} origin={self.seen.origin_bytes()}B "
                f"walls={[(w['reason'], w['host']) for w in self.walls]}")

    def walled(self, reason: str = "port") -> None:
        assert self.seen.origin_bytes() == 0, "a walled request reached the origin"
        assert self.attempts == [] and self.lookups == [], "a walled request reached the decider or srt's dial"
        assert [w["reason"] for w in self.walls] == [reason], self.walls


# --- B1: the port walls -----------------------------------------------------------------------------------------------

def _connect_walled(rig, glue, verdict, pid, recorded, url, *args):
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, url, *args)
    o.done()
    verdict(pid, recorded, f"{client(res)} | {o.facts()}")
    assert res.rc == 56 and res.status == 0, res
    assert "CONNECT tunnel failed, response 403" in res.err and res.deny_reason == "host_not_allowed", res
    o.walled()


def test_b1_1_connect_to_an_off_tls_port_keyed_host(rig, glue, verdict):
    _connect_walled(rig, glue, verdict, "CHD2 R3 B1.1", "CONNECT 403 host_not_allowed; no decider, no lookup, origin 0",
                    f"https://api.rb.test:{rig.ports.off_tls}/b1-8443", *CRED)


def test_b1_2_tls_to_the_plain_port_standin(rig, glue, verdict):
    _connect_walled(rig, glue, verdict, "CHD2 R3 B1.2", "CONNECT 403; origin 0",
                    f"https://api.rb.test:{rig.ports.http}/b1-80-tls")


def test_b1_3_connect_to_the_plain_port_without_tls(rig, glue, verdict):
    _connect_walled(rig, glue, verdict, "CHD2 R3 B1.3", "CONNECT 403", f"http://api.rb.test:{rig.ports.http}/b1-80-notls",
                    "-p")


def test_b1_5_connect_to_an_off_tls_port_unkeyed(rig, glue, verdict):
    _connect_walled(rig, glue, verdict, "CHD2 R3 B1.5", "CONNECT 403, origin 0",
                    f"https://s.rb.test:{rig.ports.off_tls}/b1-8443-unkeyed")


@pytest.mark.parametrize("host", ["api.rb.test", "s.rb.test"])
def test_b1_4_absolute_form_http_to_an_off_port(rig, glue, verdict, host):
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, f"http://{host}:{rig.ports.off_http}/b1-8080", *CRED)
    o.done()
    verdict(f"CHD2 R3 B1.4 {host}", "403 host_not_allowed, origin 0", f"{client(res)} | {o.facts()}")
    assert res.status == 403 and res.deny_reason == "host_not_allowed", res
    o.walled()


@pytest.mark.parametrize("host", ["api.rb.test", "s.rb.test"])
def test_b1_6_7_absolute_form_https_without_connect(rig, glue, verdict, host):
    o = Obs(rig, glue)
    cred = b"Authorization: Bearer CLIENT-SECRET\r\n" if host.startswith("api") else b""
    reply = clients.Raw(glue.port, glue.ca).one_shot(
        f"GET https://{host}:{rig.ports.tls}/abs-https HTTP/1.1\r\nHost: {host}:{rig.ports.tls}\r\n".encode() + cred +
        b"Connection: close\r\n\r\n")
    o.done()
    verdict(f"CHD2 R3 B1.6/7 {host}", "403 host_not_allowed, origin 0, no lookup",
            f"{clients.status_line(reply)!r} | {o.facts()}")
    assert clients.status_line(reply) == "HTTP/1.1 403 Forbidden", reply
    assert clients.has_header(reply, "X-Deny-Reason", "host_not_allowed"), reply
    o.walled()


def test_b1_ctl_the_keyed_host_on_the_tls_standin_gets_the_held_key(rig, glue, verdict):
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, f"https://api.rb.test:{rig.ports.tls}/b1-ctl", *CRED)
    o.done()
    verdict("CHD2 R3 B1.ctl", "200, the held key at the origin (key on (https, host, 443-class) only)",
            f"status={res.status} key_at_origin={HELD_KEY in o.raw} client_secret_at_origin={'CLIENT-SECRET' in o.raw}")
    assert res.status == 200 and "origin-ok" in res.out
    assert f"Bearer {HELD_KEY}" in o.raw and "CLIENT-SECRET" not in o.raw
    assert len(o.attempts) == 1 and o.lookups == ["api.rb.test"] and o.walls == []


def test_b1_negative_control_an_admitted_off_port_is_seen_to_reach_the_origin(rig, verdict):
    """The same observers see a request a glue admits: a launch whose HTTP set includes the off port forwards it."""
    g = _start(rig, "B1-neg", extra_http_ports=(rig.ports.off_http,))
    try:
        o = Obs(rig, g)
        res = clients.curl(g.port, g.ca, f"http://s.rb.test:{rig.ports.off_http}/b1-8080-unkeyed")
        o.done()
        verdict("CHD2 R3 B1.4b negative control", "(the as-is column: 200, origin :8080 got the request)",
                f"{client(res)} | {o.facts()}")
        assert res.status == 200 and o.seen.origin_bytes() > 0 and len(o.attempts) == 1 and o.walls == []
    finally:
        _stop_clean(g)


# --- B4: one lookup, the address that passed is dialed; rebinding refused at dial -------------------------------------

@pytest.mark.parametrize("scheme,name,path", [("http", "pin-t.rb.test", "/pin"), ("https", "pin2-t.rb.test", "/pin2")])
def test_b4_pin_one_lookup_and_that_address_dialed(rig, glue, verdict, scheme, name, path):
    port = rig.ports.http if scheme == "http" else rig.ports.tls
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, f"{scheme}://{name}:{port}{path}")
    o.done()
    reqs = [d for d in o.seen.origin if d.get("bytes")]
    verdict(f"CHD2 R3 B4 pin {scheme}", "ONE lookup by srt; the request served from 127.0.0.1",
            f"status={res.status} lookups={o.lookups} origin-requests={len(reqs)}")
    assert res.status == 200 and "origin-ok" in res.out
    assert o.lookups == [name] and len(reqs) == 1 and f"GET {path} " in reqs[0]["raw"]


@pytest.mark.parametrize("cls", sorted(REBIND))
def test_b4_rebinding_is_refused_at_dial(rig, glue, verdict, cls):
    """The decider's own lookup answers a public address (`Lookup()`); srt's answers the class: refused at dial."""
    name = f"rb-{cls}.rb.test"
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, f"http://{name}:{rig.ports.http}/", max_time=25)
    o.done(settle=1.5)
    verdict(f"CHD2 R3 B4 rebind {cls}", f"403 X-Deny-Reason address_not_allowed, origin 0 (srt gets {REBIND[cls]})",
            f"{client(res)} | {o.facts()}")
    assert res.status == 403 and res.deny_reason == "address_not_allowed", res.out[:300]
    assert len(o.attempts) == 1 and o.lookups == [name]            # the decider allowed it; srt resolved and refused
    assert o.seen.origin_bytes() == 0


# --- B6: upstream trust under a closed env ----------------------------------------------------------------------------

def _b6(rig, g):
    o = Obs(rig, g)
    res = clients.curl(g.port, g.ca, f"https://bad.rb.test:{rig.ports.tls}/b6")
    return res, o.done()


def test_b6_origin_ca_only_refuses_the_bad_cert_and_trusts_the_origin_leaf(rig, glue, verdict):
    res, o = _b6(rig, glue)
    verdict("CHD2 R3 B6 run B2", "502 (bad cert refused); the origin-CA leaf served", f"{client(res)} | {o.facts()}")
    assert res.status == 502 and o.seen.origin_bytes() == 0
    ctl = clients.curl(glue.port, glue.ca, f"https://s.rb.test:{rig.ports.tls}/b6-ctl")
    assert ctl.status == 200 and "origin-ok" in ctl.out          # the 502 above is the cert, not the rig


def test_b6_system_roots_refuse_the_bad_cert_with_a_hostile_parent_variable(rig, verdict):
    """C1 + C3 together, as production runs: no `--upstream-ca` (explicit system roots), env={} for node, while THIS
    process holds `NODE_EXTRA_CA_CERTS` naming the CA that signed the bad leaf."""
    g = _start(rig, "C1C3", upstream_ca="system", hostile_extra_ca=True)
    try:
        res, o = _b6(rig, g)
        verdict("CHD2 R3 B6 runs C1/C3", "502 PASS", f"{client(res)} | {o.facts()}")
        assert res.status == 502 and o.seen.origin_bytes() == 0
    finally:
        _stop_clean(g)


def test_b6_negative_control_a_glue_that_trusts_the_bad_ca_reaches_the_origin(rig, verdict):
    g = _start(rig, "B6-neg", upstream_ca="bad")
    try:
        res, o = _b6(rig, g)
        verdict("CHD2 R3 B6 negative control (upstream CA = the bad CA)", "(run C2's shape: 200, the origin got GET /b6)",
                f"{client(res)} | {o.facts()}")
        assert res.status == 200 and "GET /b6 " in o.raw
    finally:
        _stop_clean(g)


# --- B7: the client leaves while its card is held ---------------------------------------------------------------------

def _wait(pred, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_b7_the_client_leaves_mid_hold_then_his_allow_forwards_nothing(rig, glue, verdict, scheme):
    port = rig.ports.http if scheme == "http" else rig.ports.tls
    n = len(glue.desk.filed)
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, f"{scheme}://s.rb.test:{port}/b7-{scheme}", "-X", "POST", "-d", "x=1",
                       max_time=1.5)
    assert res.rc == 28 and len(glue.desk.filed) == n + 1, (res, glue.desk.filed[n:])   # curl's own timeout, one card
    card = f"card-{n + 1}"
    ended = _wait(lambda: card in glue.desk.ended)
    glue.desk.answer[card] = "allowed"                   # his allow, after the client left
    o.done(settle=1.5)
    verdict(f"CHD2 R3 B7 {scheme}", "client gone at 1.5 s; the card ENDED from the close; the allow dials nothing",
            f"curl rc={res.rc} ended={ended} | {o.facts()}")
    assert ended, glue.desk.ended
    assert o.seen.origin_bytes() == 0 and o.lookups == []


def test_b7_control_the_client_stays_and_the_allow_forwards(rig, glue, verdict):
    n = len(glue.desk.filed)

    def allow():
        if _wait(lambda: len(glue.desk.filed) > n):
            glue.desk.answer[f"card-{n + 1}"] = "allowed"
    t = threading.Thread(target=allow, daemon=True)
    t.start()
    o = Obs(rig, glue)
    res = clients.curl(glue.port, glue.ca, f"http://s.rb.test:{rig.ports.http}/b7-ctl", "-X", "POST", "-d", "x=1")
    t.join(15)
    o.done()
    verdict("CHD2 R3 B7.3 control", "allowed, forwarded, 200", f"{client(res)} | {o.facts()}")
    assert res.status == 200 and "POST /b7-ctl " in o.raw and o.lookups == ["s.rb.test"]


# --- B9 (plain): Host against the absolute-form target ------------------------------------------------------------------

def test_b9_host_other_than_the_absolute_form_target_on_plain_http(rig, glue, verdict):
    o = Obs(rig, glue)
    p = rig.ports.http
    reply = clients.Raw(glue.port, glue.ca).one_shot(
        f"GET http://a.rb.test:{p}/h HTTP/1.1\r\nHost: b.rb.test:{p}\r\nConnection: close\r\n\r\n".encode())
    o.done()
    verdict("CHD2 R3 B9 Host vs absolute-form, plain", "421 Host header does not match", f"{clients.status_line(reply)!r}"
            f" | {o.facts()}")
    assert clients.status_line(reply).startswith("HTTP/1.1 421"), reply
    assert o.seen.origin_bytes() == 0 and o.attempts == [] and o.lookups == []


# --- B12: no key on plain HTTP, the strip applies there ---------------------------------------------------------------

HDRS = ["-H", "Authorization: Bearer CLIENT-SECRET", "-H", "Cookie: c=CLIENT-COOKIE", "-H", "X-Api-Key: CLIENT-APIKEY",
        "-H", "X_Api_Key: CLIENT-APIKEY2"]


def _b12(rig, g, url):
    o = Obs(rig, g)
    res = clients.curl(g.port, g.ca, url, *HDRS)
    return res, o.done()


@pytest.mark.parametrize("host,path", [("api.rb.test", "/b12-keyed"), ("s.rb.test", "/b12-strip?allow=1")])
def test_b12_no_key_and_no_client_credential_on_plain_http(rig, glue, verdict, host, path):
    res, o = _b12(rig, glue, f"http://{host}:{rig.ports.http}{path}")
    head = o.raw.lower()
    verdict(f"CHD2 R3 B12 {host}", "200; the origin got no key and no client credential", f"{client(res)} | {o.facts()}")
    assert res.status == 200 and "origin-ok" in res.out and head
    for needle in ("authorization", "cookie", "api-key", "api_key", "client-", HELD_KEY.lower()):
        assert needle not in head, needle


def test_b12_negative_control_a_decider_that_keys_plain_http_is_caught(rig, glue, verdict):
    """TEST-ONLY leaky variant: `decide_key` handed the plain stand-in as (https, 443). The B12 assertion must fail on
    what it then sees: the held key at the origin, or the request refused whole (srt's plaintext header set)."""
    glue.key_on_plain = True
    try:
        res, o = _b12(rig, glue, f"http://api.rb.test:{rig.ports.http}/b12-leaky")
    finally:
        glue.key_on_plain = False
    verdict("CHD2 R3 B12 negative control (key on plain)", "(the as-is column: allow WITH key)",
            f"{client(res)} key_at_origin={HELD_KEY in o.raw} | {o.facts()}")
    assert HELD_KEY in o.raw or res.status != 200, (res, o.raw)
