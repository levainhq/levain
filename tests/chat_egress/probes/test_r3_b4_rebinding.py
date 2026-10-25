"""ROUND 3 B4 (rebinding + the denied table) against srt's own resolve-once-dial-that-address path.

Recorded (ROUND3.md B4, B2 = the clean run with the levain-xlat table): `pin` rows PASS (ONE lookup by srt, the request
served from 127.0.0.1, no later query although every later answer would be 10.0.0.1); every `rebind` row PASS
(403 address_not_allowed, origin 0) incl. documentation 192.0.2.10 (the round-2 list dialed it); the table rows in
`table_check.txt` (the whole levain table refuses every IPv4 answer through `::ffff:0:0/96`; minus that range the public
addresses pass and the private/special ones do not).

Port note: round 3's decider resolved the name FIRST (`?dr=1`, answer 93.184.216.34) and srt second (the private answer).
The round-4 decider has no pre-resolve, so the probe plays that first resolve itself with one direct query to the local
responder (`clients.dns_query`), which consumes the public answer; srt's lookup then gets the private one. Without it srt
would be handed the PUBLIC answer and dial it."""

from __future__ import annotations

import subprocess

import pytest

from ..fixtures import clients, env as envmod
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt


def test_b4_pin_plain_one_lookup_dial_that_address(r3, r3_glue, verdict):
    p = r3.ports
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"http://pin-t.rb.test:{p.http}/pin")
    seen = r3.since(m, r3_glue)
    lookups = seen.event_types("srt-lookup")
    reqs = [d for d in seen.origin if d.get("bytes")]
    a_queries = [d for d in seen.dns if d["type"] == "A" and d["name"] == "pin-t.rb.test"]
    verdict("R3 B4 pin, plain", "PASS: ONE A+AAAA lookup by srt (['127.0.0.1']); origin: one 0-byte vetting conn + the request; no further query",
            f"status={res.status} srt-lookups={[x['answer'] for x in lookups]} a-queries={len(a_queries)} origin-conns-with-bytes={len(reqs)} "
            f"origin-conns-total={len(seen.origin)}")
    assert res.status == 200 and "origin-ok" in res.out
    assert [x["answer"] for x in lookups] == [["127.0.0.1"]] and len(a_queries) == 1
    assert len(reqs) == 1 and "GET /pin " in reqs[0]["raw"] and reqs[0]["port"] == p.http


def test_b4_pin_tls_one_lookup_dial_that_address(r3, r3_glue, verdict):
    p = r3.ports
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"https://pin2-t.rb.test:{p.tls}/pin2")
    seen = r3.since(m, r3_glue)
    lookups = seen.event_types("srt-lookup")
    a_queries = [d for d in seen.dns if d["type"] == "A" and d["name"] == "pin2-t.rb.test"]
    reqs = [d for d in seen.origin if d.get("bytes")]
    verdict("R3 B4 pin, TLS", "PASS: one lookup, request served from 127.0.0.1:9443",
            f"status={res.status} srt-lookups={[x['answer'] for x in lookups]} a-queries={len(a_queries)} origin-requests={len(reqs)}")
    assert res.status == 200 and [x["answer"] for x in lookups] == [["127.0.0.1"]] and len(a_queries) == 1
    assert len(reqs) == 1 and "GET /pin2 " in reqs[0]["raw"]


@pytest.mark.parametrize("cls,what,answer_note", [
    ("loop", "loopback", "srt gets 127.0.0.2"),
    ("1918", "RFC 1918", "srt gets 10.0.0.1"),
    ("ll", "link-local", "srt gets 169.254.169.254"),
    ("doc", "documentation", "srt gets 192.0.2.10 (the round-2 list dialed it, levain table refuses)"),
    ("nat64", "NAT64", "srt gets AAAA 64:ff9b::7f00:1, A empty"),
])
def test_b4_rebind_is_refused(r3, r3_glue, verdict, cls, what, answer_note):
    p = r3.ports
    name = f"rb-{cls}-t.rb.test"
    first = clients.dns_query(p.dns, name, "A")          # the decider's own resolve (round 3 `dr=1`)
    assert first == ["93.184.216.34"], first
    m = r3.mark(r3_glue)
    res = clients.curl(r3_glue.port, r3_glue.ca, f"http://{name}:{p.http}/?dr=1", max_time=25)
    seen = r3.since(m, r3_glue, settle=1.5)
    verdict(f"R3 B4 rebind {what}", f"PASS (B2): 403 X-Deny-Reason address_not_allowed, origin 0 ({answer_note})",
            f"{client(res)} | srt-lookups={[x['answer'] for x in seen.event_types('srt-lookup')]} origin={seen.origin_bytes()}B")
    assert res.status == 403 and res.deny_reason == "address_not_allowed", res.out[:300]
    assert seen.origin_bytes() == 0 and not [d for d in seen.origin if d.get("port") in (p.http, p.tls)]


def _table_check() -> list[str]:
    pkg = envmod.srt_package_dir()
    r = subprocess.run([envmod.node_bin(), str(envmod.TOOLS_DIR / "table_check.mjs"), str(pkg / "dist/sandbox")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    return r.stdout.splitlines()


def test_b4_denied_table_through_srts_guard(verdict):
    lines = _table_check()
    sect, rows = None, {}
    for ln in lines:
        if ln.startswith("---"):
            sect = ln
            rows[sect] = {}
        elif sect:
            if "REFUSED" in ln or "permitted" in ln:
                addr, _, rest = ln.partition(" ")
                rows[sect][addr] = "permitted" if rest.strip() == "permitted" else "REFUSED"
            elif ln.strip():
                rows[sect][ln.split()[0]] = ln
    whole = rows["--- whole levain table"]
    xlat = rows["--- levain table minus ::ffff:0:0/96 (levain-xlat)"]
    one = rows["--- one range at a time: ranges that refuse 104.20.23.154 or 2606:4700:10::6814:179a"]
    # recorded: the whole table refuses every IPv4 answer (public ones too) and permits public IPv6
    assert {a: whole[a] for a in ("104.20.23.154", "8.8.8.8", "1.1.1.1", "10.0.0.1", "192.0.2.10")} == dict.fromkeys(
        ("104.20.23.154", "8.8.8.8", "1.1.1.1", "10.0.0.1", "192.0.2.10"), "REFUSED")
    assert whole["2606:4700:10::6814:179a"] == whole["2001:4860:4860::8888"] == "permitted"
    # recorded: minus ::ffff:0:0/96 the public ones pass, the private / special ones do not
    for a in ("104.20.23.154", "8.8.8.8", "1.1.1.1", "2606:4700:10::6814:179a", "2001:4860:4860::8888", "::ffff:8.8.8.8", "64:ff9b::808:808"):
        assert xlat[a] == "permitted", (a, xlat[a])
    for a in ("10.0.0.1", "192.0.2.10", "198.51.100.7", "203.0.113.7", "100.64.0.1", "::ffff:127.0.0.1", "::ffff:10.0.0.1",
              "::127.0.0.1", "fec0::1", "2001:db8::1", "64:ff9b::a00:1"):
        assert xlat[a] == "REFUSED", (a, xlat[a])
    assert list(one) == ["::ffff:0:0/96"], one
    verdict("R3 B4 denied table (table_check.txt)",
            "whole levain table refuses EVERY IPv4 answer (single cause ::ffff:0:0/96); levain-xlat permits 8.8.8.8/1.1.1.1/104.20.23.154/::ffff:8.8.8.8/64:ff9b::808:808 "
            "and refuses 10.0.0.1/192.0.2.10/198.51.100.7/203.0.113.7/100.64.0.1/::ffff:127.0.0.1/::ffff:10.0.0.1/::127.0.0.1/fec0::1/2001:db8::1/64:ff9b::a00:1",
            f"identical on all {len(whole) + len(xlat)} rows; the one range that refuses a public address = {list(one)}")
