"""CH-D2 S3b: the real-clone reds of item 6, run against the S1 spike-glue fixture (srt 0.0.81 + `lib_proxy4.mjs` +
`decider_spike4.py`, ruling P-4) and REPLAYED through the build's predicate.

How: a real `git` runs through the spike stack (its decider must let it through, which is the round-4 reference
behaviour); the stack's raw-logged `req` frames, its observation lines and its body-spool results are then fed, in
order, through `parse.parse` -> `classify.decide(read_shaped=advert.read_shaped bound to the launch's set)` and
`AdvertisementSet.observe`, the order the CH-D2 decider will run them in (drain at decision). Every POST must come
out silent, and the key must follow the declared repo only. What the replay cannot show (the CH-D2 decider's own
frames: zero upstream bytes on a cap card, K1's count) is S9's: `xfail(strict=True)` at the bottom.

Rows: R9-A2 (a real fetch / ls-remote / clone, v0 and v2, silent), R15-15-Q1 (v2 ls-refs + fetch, v0, `--depth 1`),
R15-17-1 (a real clone silent after its advertisement; the key on a declared repo only), R15-19-3 (the 30,000-branch
full clone, v2 and v0, no card: `chat_egress_slow`), R15-10-A12 (a real clone with haves passes: the fetch).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from functools import partial

import pytest

from levain.chat_egress import advert, parse
from levain.chat_egress.advert import AdvertisementSet
from levain.chat_egress.classify import GATED, decide

from .conftest import build_rig
from .fixtures.repos import build_bare, git_env, git_through_proxy

pytestmark = pytest.mark.chat_egress_srt
HOST = "git.rb.test"


@dataclass(frozen=True)
class FakeRoute:
    record_id: str
    canonical_host: str
    credential_source: str
    declared_repos: frozenset
    attach: bool


ROUTE = FakeRoute("rec-1", HOST, "keychain:x", frozenset({"/declared.git"}), True)


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    r = build_rig(tmp_path_factory, "s3b", origin="git")
    yield r
    r.stop()


@pytest.fixture(scope="module")
def streams(tmp_path_factory):
    from .fixtures.repos import write_streams
    d = tmp_path_factory.mktemp("s3b-streams")
    write_streams(d)
    return d


@pytest.fixture(scope="module")
def home(rig, streams):
    h = rig.root / "home"
    h.mkdir(exist_ok=True)
    for name in ("declared", "undeclared"):
        build_bare(rig.repo_root, name, streams / "small.fi", h)
    return h


@pytest.fixture(scope="module")
def glue(rig, home):
    """Run E's shape (cap 2,097,152), with RAW frame logs so the replay sees each frame as srt sent it."""
    return rig.start_glue("s3b-E", cap=advert.UPLOAD_PACK_BODY_CAP, log="raw", upstream_ca="origin")


@dataclass
class Step:
    parsed: parse.ParsedRequest
    kind: str
    why: str | None
    disposition: str
    key: bool
    enum: str | None


def replay(seen, route=ROUTE, mode=GATED) -> list[Step]:
    """The CH-D2 decider's order over one recorded exchange: drain (observe the previous responses), then decide."""
    reqs = seen.req_frames()
    # The spike decider writes obs.jsonl when it DRAINS (at its next decision), so the log lags by one response and
    # can begin with the previous exchange's last line: each discovery GET is paired with the first unused
    # observation of its own (scheme, host, port, path), never by position.
    obs = list(seen.obs)
    used: set[int] = set()
    cut = {b["id"]: bool(b["cut"]) for b in seen.bodies}
    adverts = AdvertisementSet()
    steps = []
    for frame in reqs:
        p = parse.parse({k: v for k, v in frame.items() if k != "way"})
        body_cut = cut.get(frame["id"])
        rs = partial(advert.read_shaped, adverts=adverts, body_cut=body_cut)
        v = decide(p, route if route and route.canonical_host == p.canonical_host else None, mode, read_shaped=rs)
        disp = advert.upload_pack_disposition(p, adverts=adverts, body_cut=body_cut)
        enum = None
        for i, o in enumerate(obs):
            if i not in used and (o["scheme"], o["host"], o["port"], o["path"]) == (p.scheme, p.host, p.port, p.path):
                used.add(i)
                enum = adverts.observe(p, o["status"], o.get("ct"), verdict=v)
                break
        steps.append(Step(p, v.kind, v.why, disp, advert.key_allowed(p, route), enum))
    return steps


def assert_silent_clone(steps, *, n_posts_min=1, key):
    assert steps and steps[0].parsed.method == "GET" and steps[0].enum == advert.CT_ADVERTISEMENT
    posts = [s for s in steps if s.parsed.method == "POST"]
    assert len(posts) >= n_posts_min
    assert [(s.kind, s.why) for s in steps] == [("silent", None)] * len(steps)
    assert all(s.disposition == "read_shaped" for s in posts)
    assert all(s.key is key for s in steps), [(s.parsed.method, s.key) for s in steps]


def _clone(rig, glue, home, dest, repo, *args, v0=False, timeout=300):
    return git_through_proxy(glue.port, glue.ca, home, "clone", *args, f"https://{HOST}:{rig.ports.tls}/{repo}.git",
                             str(dest), version0=v0, timeout=timeout)


@pytest.mark.parametrize("v0", [False, True], ids=["v2", "v0"])
def test_r15_17_1_real_clone_of_a_declared_repo_is_silent_with_the_key(rig, glue, home, tmp_path, v0):
    m = rig.mark(glue)
    cp = _clone(rig, glue, home, tmp_path / "w", "declared", v0=v0)
    seen = rig.since(m, glue)
    assert cp.returncode == 0, cp.stderr[-400:]
    steps = replay(seen)
    assert_silent_clone(steps, n_posts_min=1 if v0 else 2, key=True)


@pytest.mark.parametrize("v0", [False, True], ids=["v2", "v0"])
def test_r15_17_1_real_clone_of_an_undeclared_repo_is_silent_and_keyless(rig, glue, home, tmp_path, v0):
    m = rig.mark(glue)
    cp = _clone(rig, glue, home, tmp_path / "w", "undeclared", v0=v0)
    seen = rig.since(m, glue)
    assert cp.returncode == 0, cp.stderr[-400:]
    assert_silent_clone(replay(seen), n_posts_min=1 if v0 else 2, key=False)


def test_r15_15_q1_real_depth_1_clone_is_silent(rig, glue, home, tmp_path):
    m = rig.mark(glue)
    cp = _clone(rig, glue, home, tmp_path / "w", "declared", "--depth", "1")
    seen = rig.since(m, glue)
    assert cp.returncode == 0, cp.stderr[-400:]
    assert_silent_clone(replay(seen), n_posts_min=2, key=True)


@pytest.mark.parametrize("v0", [False, True], ids=["v2", "v0"])
def test_r9_a2_real_fetch_and_ls_remote_are_silent(rig, glue, home, tmp_path, v0):
    cp = _clone(rig, glue, home, tmp_path / "w", "declared", v0=v0)
    assert cp.returncode == 0, cp.stderr[-400:]
    cfg = ["-c", f"http.proxy=http://127.0.0.1:{glue.port}", "-c", f"http.sslCAInfo={glue.ca}"]
    if v0:
        cfg += ["-c", "protocol.version=0"]
    for args in (["fetch", "origin"], ["ls-remote", "origin"]):
        m = rig.mark(glue)
        r = subprocess.run(["git", *cfg, "-C", str(tmp_path / "w"), *args], env=git_env(home), capture_output=True,
                           text=True, timeout=120)
        seen = rig.since(m, glue)
        assert r.returncode == 0, r.stderr[-400:]
        steps = replay(seen)
        assert steps and steps[0].parsed.method == "GET"
        if any(s.parsed.method == "POST" for s in steps):   # v0 ls-remote is the discovery GET alone
            assert steps[0].enum == advert.CT_ADVERTISEMENT
        assert [(s.kind, s.why) for s in steps] == [("silent", None)] * len(steps)
        assert all(s.key for s in steps)


def test_r15_17_1_a_replay_without_the_drain_cards_the_clone(rig, glue, home, tmp_path):
    """Control: the same recorded clone decided against a set that never observed its advertisement -> a card (the
    replay can fail: a probe that cannot fail is not evidence)."""
    m = rig.mark(glue)
    cp = _clone(rig, glue, home, tmp_path / "w", "declared")
    seen = rig.since(m, glue)
    assert cp.returncode == 0
    empty = AdvertisementSet()
    for frame in seen.req_frames():
        p = parse.parse({k: v for k, v in frame.items() if k != "way"})
        if p.method == "POST":
            v = decide(p, ROUTE, GATED, read_shaped=partial(advert.read_shaped, adverts=empty, body_cut=False))
            assert v.kind == "card"


@pytest.fixture(scope="module")
def big(rig, home, streams):
    build_bare(rig.repo_root, "big", streams / "big.fi", home)


@pytest.mark.chat_egress_slow
@pytest.mark.parametrize("v0", [False, True], ids=["v2", "v0"])
def test_r15_19_3_full_clone_of_30000_branches_is_silent_under_the_cap(rig, glue, home, big, tmp_path, v0):
    m = rig.mark(glue)
    cp = _clone(rig, glue, home, tmp_path / "w", "big", v0=v0, timeout=900)
    seen = rig.since(m, glue, settle=2.0)
    assert cp.returncode == 0, cp.stderr[-400:]
    assert max(b["decoded_len"] for b in seen.bodies) > 1_400_000, "the want list of ~30,000 refs was the body"
    assert not any(b["cut"] for b in seen.bodies)
    steps = replay(seen, route=None)
    assert [(s.kind, s.why) for s in steps] == [("silent", None)] * len(steps)


@pytest.mark.chat_egress_network
def test_r15_15_q1_github_depth_1_public_clone_is_silent(rig, home, tmp_path):
    """github.com is read only: one `git clone --depth 1` of a public repo, unkeyed."""
    g = rig.start_glue("s3b-gh", cap=advert.UPLOAD_PACK_BODY_CAP, log="raw", upstream_ca="system-plus")
    try:
        m = rig.mark(g)
        cp = git_through_proxy(g.port, g.ca, home, "clone", "--depth", "1",
                               "https://github.com/octocat/Hello-World.git", str(tmp_path / "gh"), timeout=120)
        seen = rig.since(m, g, settle=1.5)
        assert cp.returncode == 0, cp.stderr[-400:]
        steps = replay(seen, route=None)
        assert steps[0].enum == advert.CT_ADVERTISEMENT
        assert [(s.kind, s.why) for s in steps] == [("silent", None)] * len(steps)
        assert [s.parsed.method for s in steps] == ["GET", "POST", "POST"]
    finally:
        g.stop()


# --- the composed halves: S9 ----------------------------------------------------------------------------------------

S9 = pytest.mark.xfail(strict=True, reason="S9 composed run")


@S9
def test_r15_19_3_composed_30000_branch_clone_v2_v0_through_the_chd2_decider():
    """The 30,000-branch full clone (v2 and v0) through the CH-D2 glue + decider: silent, no card."""
    raise NotImplementedError("needs the CH-D2 decider (S5b) composed with the glue (S4): S9")


@S9
def test_r15_19_3_composed_one_byte_over_the_cap_is_a_card_with_zero_upstream_bytes():
    """A body of UPLOAD_PACK_BODY_CAP + 1 decoded bytes -> a card, the origin receives zero bytes; K1 counts it."""
    raise NotImplementedError("needs the CH-D2 decider + K1's report: S9")


@S9
def test_r15_15_q1_composed_github_depth_1_through_the_chd2_decider():
    """`git clone --depth 1` of a public github.com repo through the CH-D2 path: every POST silent."""
    raise NotImplementedError("needs the CH-D2 decider composed with the glue: S9")


@S9
def test_r15_17_1_composed_declared_repo_key_bytes_upstream():
    """Through the CH-D2 path: the key reaches the origin on a declared repo's 3 requests, zero key bytes on an
    undeclared repo's (its discovery GET included)."""
    raise NotImplementedError("needs S6's key attach + the CH-D2 decider: S9")
