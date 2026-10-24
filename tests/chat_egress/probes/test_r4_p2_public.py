"""ROUND 4 probe 2 (R15-17-1): ONE real public read. Needs the internet (`chat_egress_network`); skipped in CI.

Recorded (ROUND4.md probe 2): `git clone --depth 1 https://github.com/octocat/Hello-World.git` through the proxy, unkeyed,
real 443, system roots. `GET .../info/refs?service=git-upload-pack` -> 200 `content-type:
application/x-git-upload-pack-advertisement` exactly, no Content-Length (chunked), then `POST .../git-upload-pack`
ls-refs (182 B, Content-Length, identity), then `POST` fetch (251 B). The clone succeeded, the gate recorded the
advertisement, and there was no card. No push and no receive-pack request."""

from __future__ import annotations

import pytest

from ..fixtures.repos import git_through_proxy

pytestmark = [pytest.mark.chat_egress_srt, pytest.mark.chat_egress_network]


def test_p2_public_shallow_clone_gets_its_advertisement_and_no_card(r4, r4_repos, tmp_path, verdict):
    g = r4.start_glue("D-p2", cap=33554432, upstream_ca="system-plus")
    try:
        m = r4.mark(g)
        cp = git_through_proxy(g.port, g.ca, r4_repos, "clone", "--depth", "1", "https://github.com/octocat/Hello-World.git", str(tmp_path / "gh"), timeout=120)
        seen = r4.since(m, g, settle=1.5)
        adv = [o for o in seen.obs if o["advertised"]]
        reqs = seen.req_frames()
        decoded = [(b["decoded_len"], b["decoded_as"]) for b in seen.bodies]
        verdict("R4 P2 public shallow clone (github.com)", "PASS: GET info/refs 200 advertisement (chunked) -> POST ls-refs 182 B -> POST fetch 251 B; clone ok, advertisement recorded, no card",
                f"clone rc={cp.returncode} requests={[(f['method'], f['path']) for f in reqs]} advertised={[o['advertised'] for o in adv]} "
                f"POST bodies (decoded,enc)={decoded} denies={[v for v in seen.verdicts() if v['action'] == 'deny']}")
        assert cp.returncode == 0, cp.stderr[-400:]
        assert [f["method"] for f in reqs] == ["GET", "POST", "POST"] and len(adv) == 1
        assert adv[0]["advertised"][1:] == ["github.com", 443, "/octocat/Hello-World.git"] and adv[0]["ct"] == "application/x-git-upload-pack-advertisement"
        assert not [v for v in seen.verdicts() if v["action"] == "deny"] and not any("receive-pack" in str(f) for f in reqs)
    finally:
        g.stop()
