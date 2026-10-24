"""CH-D2 S6: the launch's route snapshot, the per-request liveness check and R-6's attachment (design §3.8.5(o)
items 2 and 4, R-6, R-10's `attach` field).

Rows: K2 (a key reaches only the snapshot's live host; the value in no artifact), R15-10-A6 (HTTPS only: both
schemes), R15-12-B12 (no key on port 80, ever; the strip still runs there), R15-18-L1 (one lookup per request; a
record made live between R-5 and R-6 changes nothing), R15-21-4 (revoke mid-launch: zero key bytes, still keyed; a
new record mid-launch attaches nothing until relaunch), R15-21-6 (made live mid-launch stays keyless; the next launch
is keyed; a redirect to an undeclared repo goes keyless), R15-23-4 (keyed = snapshot membership, liveness gates
attachment only), R15-24-1 (an unreadable liveness store: an error and, separately, a timeout), R15-25-4 (B supersedes
A mid-launch: zero key bytes, B's included, until relaunch), R15-26-2 (the five `attach` values), R15-28-3 (their
precedence).

Where a row's red ends at a card or a wall (A11 under `open network`), the card is S3a's verdict: this file asserts
what S6 owns, that the request is KEYED and carries zero key bytes. The credential store is a fake (keys_rig); the
real keychain is never read.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
from levain.chat_egress import key_setup, keys, strip, wire
from levain.chat_egress.parse import parse

from .keys_rig import KEY, SOURCE, Cockpit, FakeStore, artifact_hits, frame, record

NOW = datetime.now(timezone.utc)
INFO_REFS = "/o/r.git/info/refs"
SVC = "service=git-upload-pack"


class Live:
    """A liveness check a test controls: `live` ids, every check counted, or an error / a hang."""

    def __init__(self, live=(), *, error=None, hang=None):
        self.live = set(live)
        self.error, self.hang = error, hang
        self.calls: list[str] = []

    def check(self, record_id: str) -> bool:
        self.calls.append(record_id)
        if self.error is not None:
            raise self.error
        return record_id in self.live


def _route(rid="ek-000000000001", host="example.com", repos=("/o/r.git",), attach=True):
    return keys.RouteSnapshot(record_id=rid, canonical_host=host, credential_source=SOURCE,
                              declared_repos=frozenset(repos), attach=attach)


def _set(*routes):
    return keys.RouteSet.of(routes)


def _decide(route, f, *, live=None, store=None):
    live = live if live is not None else Live([route.record_id] if route else [])
    return keys.decide_key(route, parse(f), liveness=live, store=store or FakeStore())


def _verdict(d, f):
    """The allow frame the decider would send for this decision (its credential iff attached)."""
    p = parse(f)
    s = strip.strip(p)
    return wire.allow_frame(p.srt_id, s.remove_headers, cred=d.cred, credential=d.credential)


# --- RouteSnapshot: the type the plan fixes (S3a imports it) ------------------------------------------------------

def test_route_snapshot_is_the_fixed_type():
    import dataclasses

    names = [f.name for f in dataclasses.fields(keys.RouteSnapshot)]
    assert names == ["record_id", "canonical_host", "credential_source", "declared_repos", "attach"]
    r = _route()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.attach = False  # type: ignore[misc]
    assert isinstance(r.declared_repos, frozenset)


def test_lookup_is_by_canonical_host_and_unknown_is_none():
    s = _set(_route())
    assert keys.lookup(s, "example.com") is s.routes["example.com"]
    assert keys.lookup(s, "other.example") is None and keys.lookup(s, None) is None


# --- K2 / R15-10-A6 / R15-12-B12: the key reaches only (https, snapshot host, 443) -------------------------------

def test_k2_a_keyed_host_gets_the_key_another_host_none():
    s = _set(_route())
    fa = frame(host="example.com", path="/api/v1/x")
    fb = frame(host="b.example", path="/api/v1/x")
    da = _decide(keys.lookup(s, "example.com"), fa)
    db = _decide(keys.lookup(s, "b.example"), fb)
    assert (da.attach, da.credential, da.keyed) == ("attached", KEY, True)
    assert (db.attach, db.credential, db.keyed) == ("none", None, False)
    va, vb = _verdict(da, fa), _verdict(db, fb)
    assert va["credential"] == KEY and va["cred"] == "ek-000000000001"
    assert "credential" not in vb and "cred" not in vb


def test_k2_a_redirect_from_a_to_b_carries_none():
    """A redirect is the client's next request: to B it is B's lookup, and B is not in the snapshot."""
    s = _set(_route())
    f = frame(host="b.example", path="/landing")
    d = _decide(keys.lookup(s, "b.example"), f)
    assert d.attach == "none" and d.credential is None


def test_k2_the_value_is_never_in_a_repr_or_str_of_the_decision():
    d = _decide(_route(), frame(path="/x"))
    assert d.credential == KEY
    assert KEY not in repr(d) and KEY not in str(d)


@pytest.mark.parametrize("port, sni", [(80, None), (8443, "default"), (443, None)])
def test_r15_10_a6_r15_12_b12_no_key_off_https_443(port, sni):
    """http:// to the setup host (port 80, and plain on 443) and https on 8443: keyed, zero key bytes, and R-5's
    strip still lists Authorization and Cookie for removal."""
    f = frame(host="example.com", port=port, sni=sni, path="/x",
              headers={"authorization": ["Bearer agent"], "cookie": ["s=1"]})
    d = _decide(_route(), f)
    assert d.keyed and d.credential is None and d.cred is None and d.attach == "none"
    v = _verdict(d, f)
    assert "credential" not in v
    assert {"authorization", "cookie"} <= set(v["removeHeaders"])


def test_r15_10_a6_https_443_carries_it():
    d = _decide(_route(), frame(host="example.com", port=443, path="/x"))
    assert d.attach == "attached" and d.credential == KEY


# --- the declared repo set (R15-17-1, R15-21-6's redirect) -------------------------------------------------------

def test_declared_repo_exchange_is_keyed_undeclared_keyless():
    r = _route(repos=("/o/r.git",))
    ok_get = _decide(r, frame(path=INFO_REFS, query=SVC))
    ok_post = _decide(r, frame(method="POST", path="/o/r.git/git-upload-pack"))
    other = _decide(r, frame(path="/o/other.git/info/refs", query=SVC))
    spelled = _decide(r, frame(path="/o/r/info/refs", query=SVC))          # o/r and o/r.git are two repos
    assert (ok_get.attach, ok_post.attach) == ("attached", "attached")
    assert (other.attach, other.credential, other.keyed) == ("none", None, True)
    assert (spelled.attach, spelled.credential) == ("none", None)


def test_r15_21_6_a_redirect_to_an_undeclared_repo_on_a_keyed_host_goes_keyless():
    r = _route(repos=("/o/r.git",))
    redirected = _decide(r, frame(path="/elsewhere/repo.git/info/refs", query=SVC))
    assert redirected.keyed and redirected.attach == "none" and redirected.credential is None


def test_upload_pack_prefix():
    assert keys.upload_pack_prefix(parse(frame(path=INFO_REFS, query=SVC))) == "/o/r.git"
    assert keys.upload_pack_prefix(parse(frame(method="POST", path="/o/r.git/git-upload-pack"))) == "/o/r.git"
    assert keys.upload_pack_prefix(parse(frame(path=INFO_REFS, query="service=git-receive-pack"))) is None
    assert keys.upload_pack_prefix(parse(frame(path=INFO_REFS))) is None
    assert keys.upload_pack_prefix(parse(frame(path="/api/x"))) is None


# --- R15-26-2 / R15-28-3: the attach enum and its precedence ------------------------------------------------------

def test_r15_26_2_each_value():
    r = _route()
    assert _decide(r, frame(path="/x", audit=False)).attach == "not-evaluated"
    assert _decide(None, frame(host="b.example", path="/x")).attach == "none"
    assert _decide(r, frame(path="/o/other.git/info/refs", query=SVC)).attach == "none"
    revoked = _decide(r, frame(path=INFO_REFS, query=SVC), live=Live([]))
    assert (revoked.attach, revoked.credential) == ("revoked", None)
    assert _decide(r, frame(path=INFO_REFS, query=SVC)).attach == "attached"
    assert keys.ATTACH_VALUES == ("not-evaluated", "liveness-unreadable", "revoked", "none", "attached")


def test_r15_28_3_precedence():
    r = _route()
    undeclared = frame(path="/o/other.git/info/refs", query=SVC)
    d = _decide(r, undeclared, live=Live([]))
    assert (d.attach, d.credential) == ("revoked", None)
    live = Live(error=OSError("store gone"))
    d = _decide(r, undeclared, live=live)
    assert d.attach == "liveness-unreadable" and live.calls == [r.record_id]   # the check ran for an undeclared repo
    no_audit = Live([])
    assert _decide(r, frame(path=INFO_REFS, query=SVC, audit=False), live=no_audit).attach == "not-evaluated"
    assert no_audit.calls == []                                                 # denied before the route lookup
    assert _decide(r, undeclared).attach == "none"


def test_r15_28_3_revoked_beats_off_https():
    d = _decide(_route(), frame(port=80, sni=None, path="/x"), live=Live([]))
    assert d.attach == "revoked"


def test_snapshot_attach_false_attaches_nothing():
    d = _decide(_route(attach=False), frame(path="/x"))
    assert d.keyed and d.attach == "none" and d.credential is None


def test_a_credential_store_that_cannot_answer_attaches_nothing():
    store = FakeStore(fail=True)
    d = _decide(_route(), frame(path="/x"), store=store)
    assert d.credential is None and d.attach == "none" and d.problem == "credential-unreadable"
    assert store.reads == [SOURCE]


def test_a_value_srt_would_refuse_attaches_nothing():
    d = _decide(_route(), frame(path="/x"), store=FakeStore({SOURCE: "has space"}))
    assert d.credential is None and d.problem == "credential-unusable"


def test_the_store_is_read_only_when_attaching():
    store = FakeStore()
    _decide(_route(), frame(path="/o/other.git/info/refs", query=SVC), store=store)
    _decide(_route(), frame(path="/x"), live=Live([]), store=store)
    assert store.reads == []


# --- R15-24-1: the liveness store unreadable ---------------------------------------------------------------------

def test_r15_24_1_an_error_attaches_nothing_and_stays_keyed(tmp_path):
    ck = Cockpit(tmp_path)
    rec, _ = key_setup.setup_key(ck.root, entity="ent", host="example.com", source=SOURCE,
                                 placement={"scheme": "bearer"}, repos=["/o/r.git"], expires=timedelta(days=7),
                                 **ck.kw())
    s = keys.build_route_set(ck.state(), entity="ent", now=NOW)
    reads = []

    def broken():
        reads.append(1)
        raise auth.AuthorityError("the authority log yields no head")

    live = keys.AuthorityLiveness(broken, clock=lambda: datetime.now(timezone.utc), timeout_s=2.0)
    d = keys.decide_key(keys.lookup(s, "example.com"), parse(frame(path=INFO_REFS, query=SVC)), liveness=live,
                        store=FakeStore())
    assert (d.attach, d.credential, d.keyed) == ("liveness-unreadable", None, True)
    assert reads == [1]                                             # one store read, no retry


def test_r15_24_1_a_timeout_attaches_nothing_within_its_bound():
    gate = threading.Event()
    reads = []

    def hung():
        reads.append(1)
        gate.wait(10)
        raise AssertionError("never answers in time")

    live = keys.AuthorityLiveness(hung, clock=lambda: datetime.now(timezone.utc), timeout_s=0.3)
    t0 = time.monotonic()
    try:
        d = keys.decide_key(_route(), parse(frame(path=INFO_REFS, query=SVC)), liveness=live, store=FakeStore())
    finally:
        gate.set()
    assert time.monotonic() - t0 < 2.0
    assert (d.attach, d.credential, d.keyed) == ("liveness-unreadable", None, True)
    assert reads == [1]


def test_r15_24_1_a_post_under_open_network_is_still_keyed():
    """A11: a keyed write is a card even under `open network` (S3a's verdict); S6's half is keyed + zero bytes."""
    d = _decide(_route(), frame(method="POST", path="/api/x"), live=Live(error=TimeoutError()))
    assert d.keyed and d.credential is None and d.attach == "liveness-unreadable"


# --- the launch snapshot against a real log: R15-21-4, R15-21-6, R15-23-4, R15-25-4, R15-18-L1 --------------------

@pytest.fixture
def launched(tmp_path):
    ck = Cockpit(tmp_path)
    rec, _ = key_setup.setup_key(ck.root, entity="ent", host="Example.COM.", source=SOURCE,
                                 placement={"scheme": "bearer"}, repos=["/o/r.git"], expires=timedelta(days=7),
                                 **ck.kw())
    s = keys.build_route_set(ck.state(), entity="ent", now=NOW)
    live = keys.AuthorityLiveness(ck.source(), clock=lambda: datetime.now(timezone.utc), timeout_s=10.0)
    return ck, rec, s, live


def _real(s, live, f, store=None):
    p = parse(f)
    route = keys.lookup(s, p.canonical_host)
    return keys.decide_key(route, p, liveness=live, store=store or FakeStore())


def test_r15_21_6_setup_host_Example_COM_dot_keys_example_com_on_https_443(launched):
    ck, rec, s, live = launched
    assert set(s.routes) == {"example.com"}
    r = s.routes["example.com"]
    assert (r.record_id, r.credential_source, r.declared_repos, r.attach) == (rec["id"], SOURCE,
                                                                              frozenset({"/o/r.git"}), True)
    d = _real(s, live, frame(host="example.com", path=INFO_REFS, query=SVC))
    assert d.attach == "attached" and d.credential == KEY


def test_r15_21_4_r15_23_4_revoke_mid_launch_zero_bytes_still_keyed(launched):
    ck, rec, s, live = launched
    assert _real(s, live, frame(path=INFO_REFS, query=SVC)).attach == "attached"
    key_setup.revoke_key(ck.root, rec["id"], **ck.kw())
    d = _real(s, live, frame(path=INFO_REFS, query=SVC))
    assert (d.keyed, d.attach, d.credential) == (True, "revoked", None)
    post = _real(s, live, frame(method="POST", path="/api/x"))           # open network: still a keyed write (A11)
    assert post.keyed and post.credential is None


def test_r15_21_4_r15_21_6_a_record_made_live_mid_launch_attaches_nothing_until_relaunch(launched):
    ck, rec, s, live = launched
    key_setup.setup_key(ck.root, entity="ent", host="git.example.org", source=SOURCE,
                        placement={"scheme": "bearer"}, repos=["/o/r.git"], expires=timedelta(days=7), **ck.kw())
    f = frame(host="git.example.org", path=INFO_REFS, query=SVC)
    d = _real(s, live, f)
    assert (d.keyed, d.attach, d.credential) == (False, "none", None)
    s2 = keys.build_route_set(ck.state(), entity="ent", now=datetime.now(timezone.utc))      # the next launch
    assert _real(s2, live, f).attach == "attached"


def test_r15_25_4_b_supersedes_a_mid_launch_zero_bytes_bs_included_until_relaunch(launched):
    ck, rec, s, live = launched
    new, _ = key_setup.setup_key(ck.root, entity="ent", host="example.com", source=SOURCE,
                                 placement={"scheme": "bearer"}, repos=["/o/r.git"], expires=timedelta(days=7),
                                 replace=True, **ck.kw())
    d = _real(s, live, frame(path=INFO_REFS, query=SVC))
    assert (d.keyed, d.attach, d.credential) == (True, "revoked", None)
    s2 = keys.build_route_set(ck.state(), entity="ent", now=datetime.now(timezone.utc))
    d2 = _real(s2, live, frame(path=INFO_REFS, query=SVC))
    assert d2.attach == "attached" and s2.routes["example.com"].record_id == new["id"]


def test_r15_18_l1_one_lookup_per_request_and_a_record_made_live_between_r5_and_r6_changes_nothing(launched,
                                                                                                    monkeypatch):
    """The composer's order: ONE lookup, then R-5 (strip; S3a's keyed-route rule reads the same route), then R-6.
    A record for the request's host made live between R-5 and R-6 (the seam) leaves it decided as one unkeyed state."""
    ck, rec, s, live = launched
    calls = []
    real_lookup = keys.lookup
    monkeypatch.setattr(keys, "lookup", lambda ss, h: calls.append(h) or real_lookup(ss, h))
    f = frame(host="git.example.org", path=INFO_REFS, query=SVC, headers={"authorization": ["Bearer agent"]})
    p = parse(f)
    route = keys.lookup(s, p.canonical_host)
    stripped = strip.strip(p)                                                      # R-5
    key_setup.setup_key(ck.root, entity="ent", host="git.example.org", source=SOURCE,  # the seam
                        placement={"scheme": "bearer"}, repos=["/o/r.git"], expires=timedelta(days=7), **ck.kw())
    d = keys.decide_key(route, p, liveness=live, store=FakeStore())                   # R-6
    assert calls == ["git.example.org"]
    assert (d.keyed, d.attach, d.credential) == (False, "none", None)
    assert "authorization" in stripped.stripped


def test_build_route_set_reads_only_the_entitys_live_records(tmp_path):
    ck = Cockpit(tmp_path)
    key_setup.setup_key(ck.root, entity="other", host="example.com", source=SOURCE,
                        placement={"scheme": "bearer"}, repos=[], expires=timedelta(days=7), **ck.kw())
    assert keys.build_route_set(ck.state(), entity="ent", now=NOW).routes == {}


def test_setup_signs_the_workspaces_own_https_remotes_on_that_host_into_the_record(tmp_path):
    """K-8: the workspace's remotes are read at SETUP and signed in; the launch reads the record."""
    import subprocess

    ck = Cockpit(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True, timeout=30)
    remotes = ["https://Example.com/o/r.git", "https://user@example.com:443/o/s/", "http://example.com/o/plain.git",
               "https://example.com:8443/o/port.git", "git@example.com:o/ssh.git", "https://other.example/o/x.git"]
    for i, url in enumerate(remotes):
        subprocess.run(["git", "-C", str(ws), "remote", "add", f"r{i}", url], check=True, timeout=30)
    rec, _ = key_setup.setup_key(ck.root, entity="ent", host="example.com", source=SOURCE,
                                 placement={"scheme": "bearer"}, repos=["/added.git"], expires=timedelta(days=7),
                                 workspace=ws, **ck.kw())
    assert rec["repos"] == ["/added.git", "/o/r.git", "/o/s"]
    s = keys.build_route_set(ck.state(), entity="ent", now=NOW)
    assert s.routes["example.com"].declared_repos == frozenset({"/added.git", "/o/r.git", "/o/s"})


def test_k8_a_record_with_no_repos_keys_the_host_and_attaches_on_no_upload_pack_prefix(tmp_path):
    """A setup with no --repo and no repo for a workspace: the host is keyed, and every upload-pack exchange,
    discovery and POST alike, goes keyless (no store read)."""
    ck = Cockpit(tmp_path)
    rec, _ = key_setup.setup_key(ck.root, entity="ent", host="example.com", source=SOURCE,
                                 placement={"scheme": "bearer"}, repos=[], expires=timedelta(days=7),
                                 workspace=tmp_path / "not-a-repo", **ck.kw())
    assert rec["repos"] == []
    s = keys.build_route_set(ck.state(), entity="ent", now=NOW)
    store = FakeStore()
    live = Live([rec["id"]])
    for f in (frame(path=INFO_REFS, query=SVC), frame(method="POST", path="/o/r.git/git-upload-pack")):
        d = keys.decide_key(keys.lookup(s, "example.com"), parse(f), liveness=live, store=store)
        assert (d.keyed, d.attach, d.credential) == (True, "none", None)
    assert store.reads == []


def test_workspace_remote_urls_reads_the_repos_own_config(tmp_path):
    import subprocess

    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True, timeout=30)
    subprocess.run(["git", "-C", str(ws), "remote", "add", "origin", "https://example.com/o/r.git"], check=True,
                   timeout=30)
    subprocess.run(["git", "-C", str(ws), "remote", "add", "up", "https://example.com/o/s.git"], check=True,
                   timeout=30)
    assert sorted(keys.workspace_remote_urls(ws)) == ["https://example.com/o/r.git", "https://example.com/o/s.git"]
    assert keys.workspace_remote_urls(tmp_path / "not-a-repo") == ()


def test_credential_placements_name_each_record_as_its_class(tmp_path):
    ck = Cockpit(tmp_path)
    a, _ = key_setup.setup_key(ck.root, entity="ent", host="example.com", source=SOURCE,
                               placement={"scheme": "bearer"}, repos=[], expires=timedelta(days=7), **ck.kw())
    b, _ = key_setup.setup_key(ck.root, entity="ent", host="git.example.org", source=SOURCE,
                               placement={"scheme": "basic", "user": "x-access-token"}, repos=[],
                               expires=timedelta(days=7), **ck.kw())
    s = keys.build_route_set(ck.state(), entity="ent", now=NOW)
    assert sorted(keys.credential_placements(s), key=lambda p: p["host"]) == [
        {"class": a["id"], "host": "example.com", "scheme": "bearer"},
        {"class": b["id"], "host": "git.example.org", "scheme": "basic", "user": "x-access-token"}]


# --- K2: the value appears in no artifact the run wrote ----------------------------------------------------------

def test_k2_the_key_value_is_in_no_artifact(launched, tmp_path):
    ck, rec, s, live = launched
    for f in (frame(path=INFO_REFS, query=SVC), frame(host="b.example", path="/x"), frame(port=80, sni=None)):
        d = _real(s, live, f)
        (tmp_path / "decisions.log").open("a").write(repr(d) + "\n" + str(d) + "\n")
    key_setup.revoke_key(ck.root, rec["id"], **ck.kw())
    _real(s, live, frame(path=INFO_REFS, query=SVC))
    assert artifact_hits(tmp_path) == []
    assert KEY not in repr(s) and KEY not in repr(s.routes)
