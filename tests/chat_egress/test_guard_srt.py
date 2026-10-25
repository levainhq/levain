"""CH-D2 S2: the generated table through srt 0.0.81's REAL `createResolvedAddressGuard` (per-address verdicts on the
dial path), and srt's own reading of the ambiguous request-targets.

Rows: R15-17-3 (asymmetric and complete: for every generated network, its first and last addresses and both adjacent
outside addresses, in native, IPv4-mapped and NAT64 forms, plus public controls in both families,
`refused_address(a) => srt_refuses(a)`; srt may refuse more), R15-14-2 (the same test over a table that KEEPS
`::ffff:0:0/96` fails on the public IPv4 control: measured ROUND3 B4 run B), R15-12-B4 (the equality test is per-address
verdicts, never a list compare; the rebinding classes refused at dial), R9-A6 (srt refuses the embedded-IPv4 forms too),
R15-10-A5 (srt's layer: `requestTargetAsSpelled: false` + `ambiguousRequestTarget: 'refuse'`).

The IPv4-mapped form is read through `guard.expected_dial_refusal`, the table's one translation (see that function).
"""

from __future__ import annotations

import copy
import ipaddress
import json
import subprocess
from pathlib import Path

import pytest

from levain.broker import front
from levain.chat_egress import guard

from .fixtures import env as envmod

pytestmark = pytest.mark.chat_egress_srt

HELPER = Path(__file__).resolve().parent / "srt_guard_verdicts.mjs"
PUBLIC_V4 = ["8.8.8.8", "1.1.1.1", "104.20.23.154", "93.184.215.14"]
PUBLIC_V6 = ["2606:4700:10::6814:179a", "2001:4860:4860::8888", "2a00:1450:4001:80b::200e"]


def srt_verdicts(tmp_path: Path, tables: dict, addresses=(), targets=()) -> dict:
    pkg = envmod.srt_package_dir()
    spec = tmp_path / "in.json"
    spec.write_text(json.dumps({"tables": tables, "addresses": list(addresses), "targets": list(targets)}))
    p = subprocess.run([envmod.node_bin(), str(HELPER), str(pkg / "dist/sandbox"), str(spec)],
                       capture_output=True, text=True, timeout=120, env={})
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def _forms(a: ipaddress._BaseAddress) -> list[str]:
    if a.version == 4:
        return [str(a), f"::ffff:{a}", str(ipaddress.IPv6Address(int(ipaddress.IPv6Address('64:ff9b::')) | int(a)))]
    return [str(a)]


def probe_addresses(ranges) -> list[str]:
    """First, last and both adjacent outside addresses of every network, in every form, plus public controls."""
    out: list[str] = []
    for r in ranges:
        n = ipaddress.ip_network(r)
        top = 2 ** n.max_prefixlen - 1
        cands = [int(n[0]), int(n[-1])]
        cands += [int(n[0]) - 1] if int(n[0]) > 0 else []
        cands += [int(n[-1]) + 1] if int(n[-1]) < top else []
        for c in cands:
            out += _forms(ipaddress.IPv6Address(c) if n.version == 6 else ipaddress.IPv4Address(c))
    for a in PUBLIC_V4:
        out += _forms(ipaddress.ip_address(a))
    out += PUBLIC_V6
    return sorted(set(out), key=lambda s: (":" in s, s))


def table_probes(table) -> list[str]:
    """The per-address probe set: the table's networks AND every network Levain requires covered (front's set and
    the running Python's non-global ranges, `guard.required_networks`), so a required network the table holds only
    inside a supernet (`::1/128` inside `::/96`) is probed at its own edges too."""
    return probe_addresses(list(table) + [str(n) for n in guard.required_networks()])


def violations(verdicts: dict) -> list[str]:
    """Addresses Levain refuses at dial that srt permits; and public controls srt refuses."""
    bad = [a for a, v in verdicts.items() if guard.expected_dial_refusal(a) and v != "refused"]
    for a in PUBLIC_V4 + PUBLIC_V6:
        if verdicts.get(a) != "permitted":
            bad.append(f"public control {a} {verdicts.get(a)}")
    return bad


@pytest.fixture(scope="module")
def table() -> list[str]:
    return list(guard.load_table())


def test_r15_17_3_asymmetric_complete_through_srt(tmp_path, table, verdict):
    addrs = table_probes(table)
    v = srt_verdicts(tmp_path, {"generated": table}, addrs)["verdicts"]["generated"]
    assert violations(v) == []
    more = sorted(a for a, x in v.items() if x == "refused" and not guard.expected_dial_refusal(a))
    verdict("S2 R15-17-3 per-address", "refused_address(a) => srt_refuses(a), srt may refuse more",
            f"{len(addrs)} addresses, 0 violations; srt refuses {len(more)} more: {more[:6]}")


def test_r15_17_3_dropped_range_is_red_through_srt(tmp_path, table):
    """A hand-edited copy with one range dropped: the per-address test goes red on that range's addresses."""
    drop = "198.51.100.0/24"
    edited = [r for r in table if r != drop]
    v = srt_verdicts(tmp_path, {"edited": edited}, probe_addresses([drop]))["verdicts"]["edited"]
    bad = violations({**v, **{a: "permitted" for a in PUBLIC_V4 + PUBLIC_V6}})
    assert "198.51.100.0" in bad and "198.51.100.255" in bad


def test_r15_14_2_kept_mapped_range_fails_the_public_ipv4_control(tmp_path, table):
    """ROUND3 B4 run B, as a red: the table with `::ffff:0:0/96` kept refuses every IPv4 answer."""
    kept = table + ["::ffff:0:0/96"]
    v = srt_verdicts(tmp_path, {"kept": kept}, PUBLIC_V4 + PUBLIC_V6)["verdicts"]["kept"]
    assert all(v[a] == "refused" for a in PUBLIC_V4)
    assert all(v[a] == "permitted" for a in PUBLIC_V6)
    assert any(x.startswith("public control 8.8.8.8") for x in violations(v))


def test_r15_17_3_rebinding_to_an_is_global_only_address_is_refused_at_dial(tmp_path, table):
    a = "2001:2::1"
    assert front.refused_address(a) == "not-global"
    v = srt_verdicts(tmp_path, {"generated": table, "front-only": [str(n) for n in front._REFUSED_NETS
                                                                     if n != ipaddress.ip_network("::ffff:0:0/96")]}, [a])
    assert v["verdicts"]["generated"][a] == "refused"
    assert v["verdicts"]["front-only"][a] == "permitted"       # without the generator's addition srt dials it


@pytest.mark.parametrize("cls,addr", [
    ("loopback", "127.0.0.2"), ("RFC 1918", "10.0.0.1"), ("link-local", "169.254.169.254"),
    ("documentation", "192.0.2.10"), ("NAT64", "64:ff9b::7f00:1"),
    ("R9-A6 6to4", "2002:7f00:1::1"), ("R9-A6 Teredo", "2001:0:7f00:1::1"),
])
def test_r15_12_b4_each_rebinding_class_refused_at_dial(tmp_path, table, cls, addr):
    assert srt_verdicts(tmp_path, {"t": table}, [addr])["verdicts"]["t"][addr] == "refused"


def test_scope_id_is_the_python_deciders_rule(tmp_path, table):
    """srt drops a zone id before it judges; a public scoped answer passes srt, so the decider refuses it first."""
    a = "2606:4700:10::6814:179a%en0"
    v = srt_verdicts(tmp_path, {"t": table}, [a])["verdicts"]["t"][a]
    assert v == "permitted"
    assert guard.address_wall([a]) == "non_public_address"


@pytest.mark.parametrize("raw,srt_refuses", [
    ("/a/%2e%2e/b", True), ("/a/../b", True), ("/a/./b", True), ("//a/b", True), ("/a\\b", True),
    ("/org/repo.git/git-%72eceive-pack", False),     # not ambiguous to srt: R-3 walls it on the canonical form
    ("/a//b", False),                                # an inner `//` is not ambiguous to srt: R-2's Python wall only
])
def test_r15_10_a5_srt_refuses_the_ambiguous_targets(tmp_path, raw, srt_refuses, verdict):
    t = srt_verdicts(tmp_path, {}, targets=[raw])["targets"][raw]
    assert (t["refusal"] is not None) is srt_refuses, t
    from levain.chat_egress import parse
    p = parse.parse({"t": "req", "id": 1, "method": "POST", "host": "example.com", "port": 443,
                     "sni": "example.com", "path": raw})
    assert parse.walls(p) in {"path", "use_w2_push"}          # the Python layer walls every one of them
    verdict(f"S2 R15-10-A5 {raw}", "walls path / use_w2_push",
            f"srt refusal={t['refusal'] is not None}; python wall={parse.walls(p)}")


def test_m4_v6_probes_stay_v6(table):
    """L3 r1 M4: a v6 network's probes are IPv6 even below 2^32 (`ip_address(int)` would read `::1` as 0.0.0.1)."""
    assert {"::", "::1", "::2"} <= set(table_probes(table))
