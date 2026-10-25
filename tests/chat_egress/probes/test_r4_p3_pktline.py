"""ROUND 4 probe 3 (R15-17-2, the pkt-line dump): how large is an upload-pack request body really?

Run D: cap 33,554,432 (srt's maximum, a DUMP); every body's size, framing and pkt-line summary goes to `bodies.jsonl`.
Recorded (ROUND4.md probe 3): git gzips a request body between ~1 KiB and `http.postBuffer` (1 MiB); above that it sends a
4-byte `0000` probe POST and then the body CHUNKED and UNCOMPRESSED; the negotiation `have` list does not accumulate (each
round at most 1024 haves, ~52 KB decoded). **The largest body is the full-clone want list: ~50 bytes per wanted ref.**
  m  full clone, 8,000 branches (v2): 182 B; wire 198,419 -> **400,224 decoded**, Content-Length, gzip, 8,002 wants
  m0 same, v0: 198,413 -> 400,184, gzip, 8,001 wants
  a  --depth 1 of the 30,000-branch repo: 182, 251 (CL, identity)
  b  full clone of the 30,000-branch repo (v2): 182; 4; **1,500,224 decoded (wire 1,562,814)**, chunked (10,411 chunks),
     not gzipped, 30,002 wants;   b0 same (v0): 4; 1,500,184 (wire 1,500,931), chunked (93 chunks), 30,001 wants
  c  fetch after 50 new commits + 20 branches: 213; 660 -> 1,230 (gzip on the second), 21 wants, 1 have
  d  fetch where the client holds 3,000 commits on 300 branches the origin never saw: have rounds of 16 ... 1024, the
     largest 25,940 wire -> 51,380 decoded; each round carries only that round's haves
The exact byte counts include git's own `agent=git/<version>` line, so they are recorded next to the reproduced ones and
asserted by SHAPE (ref count exact, size = 50 B per ref + a version-dependent constant). The 8,000-branch case runs by
default; the 30,000-branch cases are `chat_egress_slow`."""

from __future__ import annotations

import subprocess

import pytest

from ..fixtures.repos import build_bare, git_env, git_through_proxy
from ._git import origin_reqs

pytestmark = pytest.mark.chat_egress_srt


@pytest.fixture(scope="module")
def d_glue(r4, r4_repos):
    return r4.start_glue("D", cap=33554432)


def _dump(seen):
    return [(b["decoded_len"], b["received"], b["decoded_as"], (b.get("pkt") or {}).get("want", 0)) for b in seen.bodies]


@pytest.fixture(scope="module")
def r4_mid(r4, r4_repos, git_streams):
    build_bare(r4.repo_root, "mid", git_streams / "mid.fi", r4_repos)


@pytest.fixture(scope="module")
def r4_big(r4, r4_repos, git_streams):
    build_bare(r4.repo_root, "big", git_streams / "big.fi", r4_repos)


@pytest.mark.parametrize("pid,v0,n_wants,rec", [
    ("R4 P3.m mid.git full clone v2", False, 8002, "182; wire 198,419 -> 400,224 decoded; CL, gzip; 8,002 wants"),
    ("R4 P3.m0 mid.git full clone v0", True, 8001, "wire 198,413 -> 400,184; CL, gzip; 8,001 wants"),
])
def test_p3_m_full_clone_of_8000_branches(r4, d_glue, r4_mid, r4_repos, tmp_path, verdict, pid, v0, n_wants, rec):
    m = r4.mark(d_glue)
    cp = git_through_proxy(d_glue.port, d_glue.ca, r4_repos, "clone", f"https://git.rb.test:{r4.ports.tls}/mid.git", str(tmp_path / "w"), version0=v0, timeout=300)
    seen = r4.since(m, d_glue, settle=1.5)
    dump = _dump(seen)
    big = max(dump)
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    verdict(pid, rec, f"clone rc={cp.returncode}; bodies (decoded, received, enc, wants)={dump}; origin framing={[(d['chunked'], d['body_wire_bytes']) for d in posts]}")
    assert cp.returncode == 0, cp.stderr[-300:]
    assert big[3] in (n_wants, n_wants - 1, n_wants + 1) and 400000 <= big[0] < 401000          # 50 B per wanted ref (+ a constant)
    assert big[2] == "gzip" and big[1] < big[0] / 1.5                                           # git gzipped it: wire < decoded
    assert not any(b["cut"] for b in seen.bodies) and not [v for v in seen.verdicts() if v["action"] == "deny"]


@pytest.mark.chat_egress_slow
@pytest.mark.parametrize("pid,args,v0,rec", [
    ("R4 P3.a --depth 1 of big.git", ["--depth", "1"], False, "182, 251 (Content-Length, identity)"),
    ("R4 P3.b full clone of big.git v2", [], False, "182; 4; 1,500,224 decoded (wire 1,562,814), chunked, not gzipped, 30,002 wants"),
    ("R4 P3.b0 full clone of big.git v0", [], True, "4; 1,500,184 (wire 1,500,931), chunked (93 chunks), identity, 30,001 wants"),
])
def test_p3_big_30000_branches(r4, d_glue, r4_big, r4_repos, tmp_path, verdict, pid, args, v0, rec):
    m = r4.mark(d_glue)
    cp = git_through_proxy(d_glue.port, d_glue.ca, r4_repos, "clone", *args, f"https://git.rb.test:{r4.ports.tls}/big.git", str(tmp_path / "w"), version0=v0, timeout=900)
    seen = r4.since(m, d_glue, settle=2)
    dump = _dump(seen)
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    verdict(pid, rec, f"clone rc={cp.returncode}; bodies (decoded, received, enc, wants)={dump}; origin (chunked, chunks, wire)={[(d['chunked'], d['chunks'], d['body_wire_bytes']) for d in posts]}")
    assert cp.returncode == 0, cp.stderr[-300:]
    if args:
        assert [x[0] for x in dump][-1] < 400 and all(x[2] == "identity" for x in dump)
    else:
        big = max(dump)
        assert big[3] in (30001, 30002) and 1_500_000 <= big[0] < 1_501_000 and big[2] == "identity"
        assert any(d["chunked"] for d in posts) and 4 in [x[0] for x in dump]            # the `0000` probe, then the chunked body
        assert not any(b["cut"] for b in seen.bodies)


@pytest.mark.chat_egress_slow
def test_p3_c_d_fetch_bodies_stay_small_and_the_have_list_does_not_accumulate(r4, d_glue, r4_big, r4_repos, git_streams, tmp_path, verdict):
    clone = tmp_path / "w"
    cp = git_through_proxy(d_glue.port, d_glue.ca, r4_repos, "clone", f"https://git.rb.test:{r4.ports.tls}/big.git", str(clone), timeout=900)
    assert cp.returncode == 0
    env = git_env(r4_repos)
    with (git_streams / "more.fi").open("rb") as f:           # 50 new commits + 20 new branches at the origin
        subprocess.run(["git", "-C", str(r4.repo_root / "big.git"), "fast-import", "--quiet"], stdin=f, check=True, env=env, timeout=300)
    m = r4.mark(d_glue)
    fe = git_through_proxy(d_glue.port, d_glue.ca, r4_repos, "-C", str(clone), "fetch", "origin", timeout=300)
    seen_c = r4.since(m, d_glue, settle=1.5)
    dump_c = _dump(seen_c)
    verdict("R4 P3.c fetch after 50 commits + 20 branches", "213; 660 -> 1,230 (gzip on the second); 21 wants, 1 have", f"fetch rc={fe.returncode} bodies={dump_c}")
    assert fe.returncode == 0 and max(x[0] for x in dump_c) < 2000
    with (git_streams / "client.fi").open("rb") as f:         # 3,000 commits on 300 branches the origin never saw
        subprocess.run(["git", "-C", str(clone), "fast-import", "--quiet"], stdin=f, check=True, env=env, timeout=300)
    # git negotiates only when there is something to want: give the origin one new commit the client lacks (the spike's
    # fetch had one too; the 3,000 client-only commits then ride as `have` rounds)
    late = ("commit refs/heads/late\nmark :1\ncommitter Spike <spike@rb.test> 1792950000 +0000\ndata 5\nlate\n"
            "from refs/heads/main^0\nM 644 inline late.txt\ndata 5\nlate\n\n")
    subprocess.run(["git", "-C", str(r4.repo_root / "big.git"), "fast-import", "--quiet"], input=late.encode(), check=True, env=env, timeout=120)
    m = r4.mark(d_glue)
    fe = git_through_proxy(d_glue.port, d_glue.ca, r4_repos, "-C", str(clone), "fetch", "origin", timeout=300)
    seen_d = r4.since(m, d_glue, settle=1.5)
    dump_d = _dump(seen_d)
    verdict("R4 P3.d fetch with 3,000 client-only commits", "rounds of 16 ... 1024 haves; largest 25,940 wire -> 51,380 decoded; 980 ... 24,805",
            f"fetch rc={fe.returncode} bodies (decoded, received, enc, wants)={dump_d}")
    assert fe.returncode == 0 and 20_000 < max(x[0] for x in dump_d) < 80_000       # ~1024 haves x ~50 B
    assert len(dump_d) >= 6 and not any(b["cut"] for b in seen_d.bodies)
