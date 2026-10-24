"""ROUND 4 probe 1 (R15-17-1): an upload-pack POST is read-shaped only after an advertisement this session.

Recorded (ROUND4.md probe 1, run E): a POST with no advertisement is a card (`upload-pack POST with no advertisement this
session`, no need_body, origin 0, no DNS) on a keyed host, an unkeyed host and plain http (a, b, c); an `info/refs` answered
`text/plain` records nothing (d); an advertisement is keyed by prefix (e) and by scheme+port (f); the RESIDUE (g): a hostile
non-git origin can advertise and its POST is then let through (24 B reached the origin); REAL `git clone` (v2) of the declared
repo on the keyed host: GET advertisement, POST ls-refs, POST fetch, all allowed silently with `authorization: Bearer
SPIKE-KEY-GIT` on all 3 (h); v0: GET + one POST, key on both (i); an UNDECLARED repo on the same keyed host clones with ZERO
credential header bytes (j); `git fetch` in the declared clone: key on both (k)."""

from __future__ import annotations

import pytest

from ..fixtures import clients
from ..fixtures.repos import git_through_proxy
from ._git import ADV_Q, GH, PK, creds, origin_reqs
from ._util import client, facts

pytestmark = pytest.mark.chat_egress_srt
CARD = "card: upload-pack POST with no advertisement this session"


def _post(r4, g, url, *extra):
    return clients.curl(g.port, g.ca, url, "-X", "POST", *GH, "--data-binary", PK.decode(), *extra)


@pytest.mark.parametrize("pid,scheme,host,path_port", [
    ("R4 P1.a no advertisement, KEYED host", "https", "git.rb.test", "tls"),
    ("R4 P1.b no advertisement, unkeyed host", "https", "s.rb.test", "tls"),
    ("R4 P1.c no advertisement, plain http", "http", "s.rb.test", "http"),
])
def test_p1_abc_post_with_no_advertisement_is_a_card(r4, r4_glue, verdict, pid, scheme, host, path_port):
    port = getattr(r4.ports, path_port)
    m = r4.mark(r4_glue)
    res = _post(r4, r4_glue, f"{scheme}://{host}:{port}/anything/git-upload-pack")
    seen = r4.since(m, r4_glue)
    verdict(pid, f"PASS: decider `deny 403 {CARD}`; no need_body; origin 0, no DNS", f"{client(res)} card={CARD in res.out} | "
            f"verdicts={[(v['action'], v.get('reason')) for v in seen.verdicts()]} | {facts(seen)}")
    assert res.status == 403 and CARD in res.out
    assert [(v["action"], v["reason"]) for v in seen.verdicts()] == [("deny", CARD)] and not any(v["action"] == "need_body" for v in seen.verdicts())
    assert seen.origin_bytes() == 0 and seen.dns == []


def test_p1_d_non_advertisement_content_type_records_nothing(r4, r4_glue, verdict):
    p = r4.ports
    m = r4.mark(r4_glue)
    adv = clients.curl(r4_glue.port, r4_glue.ca, f"https://s.rb.test:{p.tls}/anything-d/{ADV_Q}", include=False)
    post = _post(r4, r4_glue, f"https://s.rb.test:{p.tls}/anything-d/git-upload-pack")
    seen = r4.since(m, r4_glue)
    verdict("R4 P1.d text/plain info/refs, then POST", "PASS: GET allowed, obs `ct text/plain` -> not recorded; POST card; GET only upstream",
            f"adv status={adv.status} obs={[(o['ct'], o['advertised']) for o in seen.obs]} post card={CARD in post.out} origin methods={[d['method'] for d in origin_reqs(seen)]}")
    assert adv.status == 200 and [(o["ct"], o["advertised"]) for o in seen.obs] == [("text/plain", None)]
    assert post.status == 403 and CARD in post.out and [d["method"] for d in origin_reqs(seen)] == ["GET"]


def test_p1_e_f_advertisement_is_keyed_by_prefix_scheme_and_port(r4, r4_glue, verdict):
    p = r4.ports
    adv = clients.curl(r4_glue.port, r4_glue.ca, f"https://git.rb.test:{p.tls}/declared.git/{ADV_Q}", "-o", "/dev/null", include=False)
    assert adv.status == 200
    m = r4.mark(r4_glue)
    e = _post(r4, r4_glue, f"https://git.rb.test:{p.tls}/undeclared.git/git-upload-pack")
    f = _post(r4, r4_glue, f"http://git.rb.test:{p.http}/declared.git/git-upload-pack")
    seen = r4.since(m, r4_glue)
    verdict("R4 P1.e / P1.f other prefix, other scheme+port", "PASS: card (a different prefix); card (different scheme+port); 0 upstream",
            f"e card={CARD in e.out} f card={CARD in f.out} origin={seen.origin_bytes()}B")
    assert e.status == 403 and CARD in e.out and f.status == 403 and CARD in f.out and seen.origin_bytes() == 0


def test_p1_g_residue_a_hostile_origin_can_advertise(r4, r4_glue, verdict):
    p = r4.ports
    m = r4.mark(r4_glue)
    adv = clients.curl(r4_glue.port, r4_glue.ca, f"https://s.rb.test:{p.tls}/x/fakeadv/{ADV_Q}", "-o", "/dev/null", include=False)
    post = _post(r4, r4_glue, f"https://s.rb.test:{p.tls}/x/fakeadv/git-upload-pack")
    seen = r4.since(m, r4_glue)
    posts = [d for d in origin_reqs(seen) if d["method"] == "POST"]
    verdict("R4 P1.g RESIDUE: non-git origin advertises", "as the ruling names it: recorded as advertised; POST need_body, ALLOWED silently; POST 24 B reached the origin",
            f"adv={adv.status} advertised-recorded={[o['advertised'] for o in seen.obs]} post status={post.status} origin POST body bytes={[d['body_decoded_len'] for d in posts]} "
            f"need_body={[v.get('max') for v in seen.verdicts() if v['action'] == 'need_body']}")
    assert adv.status == 200 and seen.obs[0]["advertised"] == ["https", "s.rb.test", p.tls, "/x/fakeadv"]
    assert [v["action"] for v in seen.verdicts() if v["action"] == "need_body"] and len(posts) == 1 and posts[0]["body_decoded_len"] == len(PK)


@pytest.fixture
def clone_home(r4_repos, tmp_path):
    return r4_repos, tmp_path


def _clone(r4, g, home, dest, repo, **kw):
    return git_through_proxy(g.port, g.ca, home, "clone", f"https://git.rb.test:{r4.ports.tls}/{repo}.git", str(dest), **kw)


def _log_count(home, dest):
    import subprocess
    from ..fixtures.repos import git_env
    return len(subprocess.run(["git", "-C", str(dest), "log", "--oneline"], capture_output=True, text=True, env=git_env(home)).stdout.splitlines())


KEY = "authorization: Bearer SPIKE-KEY-GIT"


@pytest.mark.parametrize("pid,repo,v0,n_req,keyed,recorded", [
    ("R4 P1.h real git clone v2, KEYED host, DECLARED repo", "declared", False, 3, True,
     "PASS: GET info/refs -> POST ls-refs (182 B) -> POST fetch; all allowed, no card; `authorization: Bearer SPIKE-KEY-GIT` on all 3; 5 commits"),
    ("R4 P1.i real git clone v0", "declared", True, 2, True, "PASS: GET advertisement -> one POST (want + done); key on both"),
    ("R4 P1.j real git clone v2, KEYED host, UNDECLARED repo", "undeclared", False, 3, False,
     "PASS: identical flow, all allowed; ZERO credential header bytes on all 3 requests; 5 commits"),
])
def test_p1_hij_real_git_clone_gets_the_key_only_for_a_declared_repo(r4, r4_glue, r4_repos, tmp_path, verdict, pid, repo, v0, n_req, keyed, recorded):
    m = r4.mark(r4_glue)
    cp = _clone(r4, r4_glue, r4_repos, tmp_path / "w", repo, version0=v0)
    seen = r4.since(m, r4_glue)
    reqs = origin_reqs(seen)
    n_commits = _log_count(r4_repos, tmp_path / "w") if cp.returncode == 0 else -1
    verdict(pid, recorded, f"clone rc={cp.returncode} commits={n_commits} origin requests={[(d['method'], d['target'].split('?')[0][-22:]) for d in reqs]} "
            f"key-on={[bool(KEY in ' '.join(creds(d)) or KEY.lower() in d['raw_head'].lower()) for d in reqs]} denies={[v for v in seen.verdicts() if v['action'] == 'deny']}")
    assert cp.returncode == 0 and n_commits == 5, cp.stderr[-500:]
    assert len(reqs) == n_req and [d["method"] for d in reqs] == ["GET"] + ["POST"] * (n_req - 1)
    assert not [v for v in seen.verdicts() if v["action"] == "deny"]
    for d in reqs:
        has_key = "authorization: bearer spike-key-git" in d["raw_head"].lower()
        assert has_key is keyed, (d["target"], d["raw_head"][:400])
        if not keyed:
            assert creds(d) == [], creds(d)


def test_p1_k_real_git_fetch_in_the_declared_clone(r4, r4_glue, r4_repos, tmp_path, verdict):
    cp = _clone(r4, r4_glue, r4_repos, tmp_path / "w", "declared")
    assert cp.returncode == 0
    m = r4.mark(r4_glue)
    fe = git_through_proxy(r4_glue.port, r4_glue.ca, r4_repos, "-C", str(tmp_path / "w"), "fetch", "origin")
    seen = r4.since(m, r4_glue)
    reqs = origin_reqs(seen)
    verdict("R4 P1.k real git fetch v2", "PASS: GET advertisement -> POST ls-refs; key on both",
            f"fetch rc={fe.returncode} requests={[d['method'] for d in reqs]} key-on={[KEY.lower() in d['raw_head'].lower() for d in reqs]}")
    assert fe.returncode == 0 and [d["method"] for d in reqs] == ["GET", "POST"]
    assert all(KEY.lower() in d["raw_head"].lower() for d in reqs)
