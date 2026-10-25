"""CH-D2 S6: the launch's route snapshot, the per-request liveness check and R-6's attachment (design §3.8.5(o)
items 2 and 4, R-6, R-10's `attach` field).

Rows: K2 (a key reaches only the snapshot's live host; the value in no artifact), R15-10-A6 (HTTPS only: both
schemes), R15-12-B12 (no key on port 80, ever; the strip still runs there), R15-18-L1 (one lookup per request; a
record made live between R-5 and R-6 changes nothing), R15-21-4 (revoke mid-launch: zero key bytes, still keyed; a
new record mid-launch attaches nothing until relaunch), R15-21-6 (made live mid-launch stays keyless; the next launch
is keyed; a redirect to an undeclared repo goes keyless), R15-23-4 (keyed = snapshot membership, liveness gates
attachment only), R15-24-1 (an unreadable liveness store: an error and, separately, a timeout), R15-25-4 (B supersedes
A mid-launch: zero key bytes, B's included, until relaunch), R15-26-2 (the five `attach` values), R15-28-3 (their
precedence). S6 L3 r1: the route bound to the record's incarnation (a revoked id created again, a same-id change
refused at append), single-flight liveness under a hung store, the keychain read by service AND account, and one
keychain read per record incarnation per launch.

Where a row's red ends at a card or a wall (A11 under `open network`), the card is S3a's verdict: this file asserts
what S6 owns, that the request is KEYED and carries zero key bytes. The credential store is a fake (keys_rig); the
real keychain is never read. Records enter the log through the rig's own links (`Cockpit.link`), so these rows hold
the log's and the decider's rules whatever `levain chat key setup` does in front of them (its own file covers it).
"""
from __future__ import annotations

import stat
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import levain.authority as auth
from levain.chat_egress import keys, strip, wire
from levain.chat_egress.parse import parse

from .keys_rig import ACCOUNT, KEY, SOURCE, Cockpit, FakeStore, artifact_hits, frame, record

NOW = datetime.now(timezone.utc)
INFO_REFS = "/o/r.git/info/refs"
SVC = "service=git-upload-pack"
INC = "1:" + "0" * 64


class Live:
    """A liveness check a test controls: `live` ids, every check counted, or an error / a hang."""

    def __init__(self, live=(), *, error=None, hang=None):
        self.live = set(live)
        self.error, self.hang = error, hang
        self.calls: list[str] = []

    def check(self, record_id: str, incarnation: str | None) -> bool:
        self.calls.append(record_id)
        if self.error is not None:
            raise self.error
        return record_id in self.live


def _route(rid="ek-000000000001", host="example.com", repos=("/o/r.git",), attach=True, inc=INC):
    return keys.RouteSnapshot(record_id=rid, canonical_host=host, credential_source=SOURCE,
                              declared_repos=frozenset(repos), attach=attach, credential_account=ACCOUNT,
                              incarnation=inc)


def _set(*routes):
    return keys.RouteSet.of(routes)


def _creds(store=None):
    """The launch's credentials over `store` (a FakeStore by default); a LaunchCredentials passes through."""
    return store if isinstance(store, keys.LaunchCredentials) else keys.LaunchCredentials(store or FakeStore())


def _decide(route, f, *, live=None, store=None):
    live = live if live is not None else Live([route.record_id] if route else [])
    return keys.decide_key(route, parse(f), liveness=live, credentials=_creds(store))


def _verdict(d, f):
    """The allow frame the decider would send for this decision (its credential iff attached)."""
    p = parse(f)
    s = strip.strip(p)
    return wire.allow_frame(p.srt_id, s.remove_headers, cred=d.cred, credential=d.credential)


def _create(ck, rid, host="example.com", **kw):
    """A setup record created by one `create_grant` link; returns the record."""
    rec = record(rid, host, **kw)
    ck.link({"op": "create_grant", "grant": rec})
    return rec


def _revoke(ck, rid):
    ck.link({"op": "revoke_grant", "grant_id": rid})


def _supersede(ck, old_id, new_id, host="example.com", **kw):
    """The supersession: one `loosen` link dropping `old_id` and adding `new_id`."""
    policy = auth._thaw(ck.state().policy.as_dict())
    del policy["grants"][old_id]
    rec = record(new_id, host, **kw)
    policy["grants"][new_id] = rec
    ck.link({"op": "loosen", "policy": policy})
    return rec


def _launch(ck, entity="ent"):
    return keys.build_route_set(ck.state(), entity=entity, now=datetime.now(timezone.utc))


def _live(ck, timeout_s=10.0):
    return keys.AuthorityLiveness(ck.source(), clock=lambda: datetime.now(timezone.utc), timeout_s=timeout_s)


# --- RouteSnapshot: the type the plan fixes (S3a imports it) ------------------------------------------------------

def test_route_snapshot_is_the_fixed_type():
    import dataclasses

    names = [f.name for f in dataclasses.fields(keys.RouteSnapshot)]
    assert names == ["record_id", "canonical_host", "credential_source", "declared_repos", "attach",
                     "credential_account", "incarnation"]
    r = _route()
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.attach = False  # type: ignore[misc]
    assert isinstance(r.declared_repos, frozenset)


def test_a_route_must_name_its_account_and_incarnation():
    """Both fields are required (the integration merge): a route built the old way is a TypeError at construction.
    A None in either is still read fail closed: keyed, nothing read, nothing attached."""
    with pytest.raises(TypeError):
        keys.RouteSnapshot(record_id="ek-000000000001", canonical_host="example.com",  # type: ignore[call-arg]
                           credential_source=SOURCE, declared_repos=frozenset(), attach=True)
    bare = keys.RouteSnapshot(record_id="ek-000000000001", canonical_host="example.com", credential_source=SOURCE,
                              declared_repos=frozenset(), attach=True, credential_account=None, incarnation=None)
    store = FakeStore()
    d = _decide(bare, frame(path="/x"), store=store)
    assert d.keyed and d.credential is None and d.problem == "credential-unreadable" and store.reads == []


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


def test_decide_key_narrows_a_git_exchange_by_advert_key_allowed():
    """The declared-repo narrowing is `advert.key_allowed`'s (its one home, R15-17-1): every git exchange it reads,
    not only the two upload-pack shapes, goes keyless for an undeclared repo; a declared one still attaches."""
    r = _route(repos=("/o/r.git",))
    for f in (frame(path="/o/other.git/HEAD"),
              frame(path="/o/other.git/objects/ab/cdef"),
              frame(path="/o/other.git/info/packs"),
              frame(path="/o/other.git/info/refs/", query=SVC),
              frame(path="/o/other.git/INFO/REFS", query=SVC),
              frame(method="POST", path="/o/other.git/git-upload-pack;x"),
              frame(path=INFO_REFS, query=SVC + "&" + SVC),
              frame(path="/api/x", query="service=anything")):
        d = _decide(r, f)
        assert d.keyed and (d.attach, d.credential) == ("none", None), f
    for f in (frame(path=INFO_REFS, query=SVC), frame(method="POST", path="/o/r.git/git-upload-pack"),
              frame(path="/o/r.git/HEAD"), frame(path="/api/x")):
        assert _decide(r, f).attach == "attached", f


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
    assert store.reads == [SOURCE] and store.accounts == [ACCOUNT]


def test_a_value_srt_would_refuse_attaches_nothing():
    d = _decide(_route(), frame(path="/x"), store=FakeStore({SOURCE: "has space"}))
    assert d.credential is None and d.problem == "credential-unusable"


def test_the_store_is_read_only_when_attaching():
    store = FakeStore()
    _decide(_route(), frame(path="/o/other.git/info/refs", query=SVC), store=store)
    _decide(_route(), frame(path="/x"), live=Live([]), store=store)
    assert store.reads == []


# --- F8: one keychain read per record incarnation per launch -----------------------------------------------------

def test_launch_credentials_read_the_store_once_per_incarnation():
    store = FakeStore()
    creds = keys.LaunchCredentials(store)
    r = _route()
    for _ in range(3):
        d = _decide(r, frame(path="/x"), store=creds)
        assert (d.attach, d.credential) == ("attached", KEY)
    assert store.reads == [SOURCE] and store.accounts == [ACCOUNT]
    other = _route(inc="2:" + "1" * 64)                       # the same id, another incarnation: read afresh
    assert _decide(other, frame(path="/x"), store=creds).credential == KEY
    assert store.reads == [SOURCE, SOURCE]
    assert KEY not in repr(creds)


def test_launch_credentials_hold_no_failed_or_unusable_read():
    store = FakeStore(fail=True)
    creds = keys.LaunchCredentials(store)
    for _ in range(2):
        assert _decide(_route(), frame(path="/x"), store=creds).problem == "credential-unreadable"
    assert store.reads == [SOURCE, SOURCE]
    bad = FakeStore({SOURCE: "has space"})
    creds = keys.LaunchCredentials(bad)
    for _ in range(2):
        assert _decide(_route(), frame(path="/x"), store=creds).problem == "credential-unusable"
    assert bad.reads == [SOURCE, SOURCE]


def test_h2_decide_key_takes_only_the_launchs_credentials():
    """S6 r2 H2: a bare store (a keychain spawn per attached request) is refused at the call, before any read; there
    is no raw-store path left to fall into."""
    store = FakeStore()
    for p in (parse(frame(path="/x")), parse(frame(path="/x", audit=False))):
        with pytest.raises(TypeError, match="LaunchCredentials"):
            keys.decide_key(_route(), p, liveness=Live([_route().record_id]), credentials=store)
    assert store.reads == []
    d = keys.decide_key(_route(), parse(frame(path="/x")), liveness=Live([_route().record_id]),
                        credentials=keys.LaunchCredentials(store))
    assert d.attach == "attached" and store.reads == [SOURCE]


def test_f8_a_held_value_is_zero_bytes_after_a_revoke(tmp_path):
    ck = Cockpit(tmp_path)
    rec = _create(ck, "ek-000000000001")
    s, live, store = _launch(ck), _live(ck), FakeStore()
    creds = keys.LaunchCredentials(store)
    f = frame(path=INFO_REFS, query=SVC)
    for _ in range(2):
        assert _real(s, live, f, creds).attach == "attached"
    assert store.reads == [SOURCE]
    _revoke(ck, rec["id"])
    d = _real(s, live, f, creds)
    assert (d.attach, d.credential, d.keyed) == ("revoked", None, True)


# --- F5: the keychain item is service AND account -----------------------------------------------------------------

def _fake_security(tmp_path, value="lvn-fake-value"):
    """A stand-in `security` binary that records its argv and prints a value (never the real keychain)."""
    fake = tmp_path / "security"
    argv = tmp_path / "argv"
    fake.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > '{argv}'\necho '{value}'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return str(fake), argv


def test_f5_the_keychain_read_names_service_and_account(tmp_path):
    fake, argv = _fake_security(tmp_path)
    assert keys.KeychainStore(fake).read(SOURCE, ACCOUNT) == "lvn-fake-value"
    assert argv.read_text().splitlines() == ["find-generic-password", "-s", SOURCE, "-a", ACCOUNT, "-w"]


@pytest.mark.parametrize("account", [None, ""])
def test_f5_a_keychain_read_without_an_account_is_refused_unspawned(tmp_path, account):
    fake, argv = _fake_security(tmp_path)
    with pytest.raises(keys.CredentialUnreadable):
        keys.KeychainStore(fake).read(SOURCE, account)  # type: ignore[arg-type]
    assert not argv.exists()


def test_f5_the_log_refuses_a_setup_record_without_an_account(tmp_path):
    ck = Cockpit(tmp_path)
    with pytest.raises(auth.AuthorityError, match="account"):
        ck.link({"op": "create_grant", "grant": record("ek-000000000001", "example.com", account=None)})
    with pytest.raises(auth.AuthorityError, match="account"):
        ck.link({"op": "create_grant", "grant": record("ek-000000000001", "example.com", account=" padded")})


def test_f5_the_route_carries_the_records_account(tmp_path):
    ck = Cockpit(tmp_path)
    _create(ck, "ek-000000000001", account="me@example.com")
    r = _launch(ck).routes["example.com"]
    assert (r.credential_source, r.credential_account) == (SOURCE, "me@example.com")


# --- R15-24-1: the liveness store unreadable ---------------------------------------------------------------------

def test_r15_24_1_an_error_attaches_nothing_and_stays_keyed(tmp_path):
    ck = Cockpit(tmp_path)
    _create(ck, "ek-000000000001")
    s = _launch(ck)
    reads = []

    def broken():
        reads.append(1)
        raise auth.AuthorityError("the authority log yields no head")

    live = keys.AuthorityLiveness(broken, clock=lambda: datetime.now(timezone.utc), timeout_s=2.0)
    d = keys.decide_key(keys.lookup(s, "example.com"), parse(frame(path=INFO_REFS, query=SVC)), liveness=live,
                        credentials=_creds())
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
        d = keys.decide_key(_route(), parse(frame(path=INFO_REFS, query=SVC)), liveness=live, credentials=_creds())
    finally:
        gate.set()
    assert time.monotonic() - t0 < 2.0
    assert (d.attach, d.credential, d.keyed) == ("liveness-unreadable", None, True)
    assert reads == [1]


def test_r15_24_1_a_post_under_open_network_is_still_keyed():
    """A11: a keyed write is a card even under `open network` (S3a's verdict); S6's half is keyed + zero bytes."""
    d = _decide(_route(), frame(method="POST", path="/api/x"), live=Live(error=TimeoutError()))
    assert d.keyed and d.credential is None and d.attach == "liveness-unreadable"


# --- F3: single-flight liveness under a hung store ----------------------------------------------------------------

def _liveness_threads():
    return sum(1 for t in threading.enumerate() if t.name == "levain-key-liveness" and t.is_alive())


def test_f3_a_hung_store_holds_one_read_and_refuses_the_rest_at_once():
    gate = threading.Event()
    reads = []

    def hung():
        reads.append(1)
        gate.wait(30)

    live = keys.AuthorityLiveness(hung, clock=lambda: datetime.now(timezone.utc), timeout_s=0.2)
    before = _liveness_threads()
    try:
        first = _decide(_route(), frame(path="/x"), live=live)            # waits its bound, then refuses
        assert first.attach == "liveness-unreadable"
        t0 = time.monotonic()
        sequential = [_decide(_route(), frame(path="/x"), live=live) for _ in range(50)]
        assert time.monotonic() - t0 < 0.5                                # refused at once, never 50 bounds
        out: list = []
        ts = [threading.Thread(target=lambda: out.append(_decide(_route(), frame(path="/x"), live=live)))
              for _ in range(20)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(5)
        assert {d.attach for d in sequential + out} == {"liveness-unreadable"}
        assert all(d.credential is None for d in sequential + out) and len(out) == 20
        assert reads == [1] and _liveness_threads() - before <= 1
    finally:
        gate.set()


def test_f3_after_a_stuck_read_ends_the_next_check_reads_afresh(tmp_path):
    ck = Cockpit(tmp_path)
    _create(ck, "ek-000000000001")
    s = _launch(ck)
    gate = threading.Event()
    real = ck.source()
    calls = []

    def source():
        calls.append(1)
        if len(calls) == 1:
            gate.wait(30)
        return real()

    live = keys.AuthorityLiveness(source, clock=lambda: datetime.now(timezone.utc), timeout_s=0.2)
    route = keys.lookup(s, "example.com")
    assert _decide(route, frame(path="/x"), live=live).attach == "liveness-unreadable"
    gate.set()
    deadline = time.monotonic() + 5
    while live._flight is not None and not live._flight.done.is_set() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert _decide(route, frame(path="/x"), live=live).attach == "attached"
    assert len(calls) == 2


def test_f3_a_check_never_answers_from_a_read_that_started_before_it(tmp_path):
    """Request B arrives while A's read is in flight (not stuck) and a revoke lands between: B waits for A's read
    to end and then reads afresh, so B sees the revoke (two reads, B revoked) and A's older answer is A's alone."""
    ck = Cockpit(tmp_path)
    rec = _create(ck, "ek-000000000001")
    s = _launch(ck)
    route = keys.lookup(s, "example.com")
    real = ck.source()
    first_started, release = threading.Event(), threading.Event()
    calls = []

    def source():
        calls.append(1)
        snap = real()
        if len(calls) == 1:
            first_started.set()
            release.wait(10)
        return snap

    live = keys.AuthorityLiveness(source, clock=lambda: datetime.now(timezone.utc), timeout_s=5.0)
    got: dict = {}
    a = threading.Thread(target=lambda: got.update(a=_decide(route, frame(path="/x"), live=live)))
    a.start()
    assert first_started.wait(5)
    _revoke(ck, rec["id"])
    b = threading.Thread(target=lambda: got.update(b=_decide(route, frame(path="/x"), live=live)))
    b.start()
    time.sleep(0.1)
    release.set()
    a.join(10)
    b.join(10)
    assert got["a"].attach == "attached" and (got["b"].attach, got["b"].credential) == ("revoked", None)
    assert len(calls) == 2


# --- the launch snapshot against a real log: R15-21-4, R15-21-6, R15-23-4, R15-25-4, R15-18-L1 --------------------

@pytest.fixture
def launched(tmp_path):
    ck = Cockpit(tmp_path)
    rec = _create(ck, "ek-000000000001")
    s = _launch(ck)
    return ck, rec, s, _live(ck)


def _real(s, live, f, store=None):
    p = parse(f)
    route = keys.lookup(s, p.canonical_host)
    return keys.decide_key(route, p, liveness=live, credentials=_creds(store))


def test_r15_21_6_the_records_canonical_host_keys_example_com_on_https_443(launched):
    ck, rec, s, live = launched
    assert set(s.routes) == {"example.com"}
    r = s.routes["example.com"]
    assert (r.record_id, r.credential_source, r.declared_repos, r.attach) == (rec["id"], SOURCE,
                                                                              frozenset({"/o/r.git"}), True)
    assert r.incarnation == auth.egress_key_incarnation(ck.state(), rec["id"]) and r.incarnation is not None
    d = _real(s, live, frame(host="example.com", path=INFO_REFS, query=SVC))
    assert d.attach == "attached" and d.credential == KEY


def test_r15_21_4_r15_23_4_revoke_mid_launch_zero_bytes_still_keyed(launched):
    ck, rec, s, live = launched
    assert _real(s, live, frame(path=INFO_REFS, query=SVC)).attach == "attached"
    _revoke(ck, rec["id"])
    d = _real(s, live, frame(path=INFO_REFS, query=SVC))
    assert (d.keyed, d.attach, d.credential) == (True, "revoked", None)
    post = _real(s, live, frame(method="POST", path="/api/x"))           # open network: still a keyed write (A11)
    assert post.keyed and post.credential is None


def test_r15_21_4_r15_21_6_a_record_made_live_mid_launch_attaches_nothing_until_relaunch(launched):
    ck, rec, s, live = launched
    _create(ck, "ek-000000000002", "git.example.org")
    f = frame(host="git.example.org", path=INFO_REFS, query=SVC)
    d = _real(s, live, f)
    assert (d.keyed, d.attach, d.credential) == (False, "none", None)
    s2 = _launch(ck)                                                      # the next launch
    assert _real(s2, live, f).attach == "attached"


def test_r15_25_4_b_supersedes_a_mid_launch_zero_bytes_bs_included_until_relaunch(launched):
    ck, rec, s, live = launched
    new = _supersede(ck, rec["id"], "ek-000000000002")
    d = _real(s, live, frame(path=INFO_REFS, query=SVC))
    assert (d.keyed, d.attach, d.credential) == (True, "revoked", None)
    s2 = _launch(ck)
    d2 = _real(s2, live, frame(path=INFO_REFS, query=SVC))
    assert d2.attach == "attached" and s2.routes["example.com"].record_id == new["id"]


# --- F2: a route is bound to the record's incarnation ------------------------------------------------------------

@pytest.mark.parametrize("change", [{}, {"source": "another-item"}, {"repos": ("/o/r.git", "/o/evil.git")},
                                    {"account": "someone-else"}])
def test_f2_revoke_then_recreate_the_same_id_leaves_the_stale_launch_keyless(launched, change):
    """The id revoked and created again, identical or not, is another incarnation: the running launch's route
    (the record it READ) attaches nothing from the next request, and stays keyed; the next launch attaches."""
    ck, rec, s, live = launched
    _revoke(ck, rec["id"])
    _create(ck, rec["id"], **change)
    store = FakeStore({SOURCE: KEY, "another-item": KEY})
    d = _real(s, live, frame(path=INFO_REFS, query=SVC), store)
    assert (d.keyed, d.attach, d.credential) == (True, "revoked", None) and store.reads == []
    assert _real(_launch(ck), live, frame(path=INFO_REFS, query=SVC), store).attach == "attached"


@pytest.mark.parametrize("change", [{"host": "evil.example"}, {"source": "another-item"}, {"account": "other"},
                                    {"placement": {"scheme": "basic", "user": "x"}},
                                    {"repos": ("/o/r.git", "/o/evil.git")}, {"repos": ()}, {"entity": "ent2"}])
def test_f2_a_same_id_change_but_its_expiry_is_refused_at_append(launched, change):
    ck, rec, s, live = launched
    policy = auth._thaw(ck.state().policy.as_dict())
    host = change.pop("host", "example.com")
    policy["grants"][rec["id"]] = record(rec["id"], host, **change)
    with pytest.raises(auth.AuthorityError, match="keeps its meaning"):
        ck.link({"op": "loosen", "policy": policy})
    assert _real(s, live, frame(path=INFO_REFS, query=SVC)).attach == "attached"


def test_f2_a_renew_keeps_the_incarnation_and_the_launch_keyed(launched):
    """Only the expiry moved later: the record the launch read, still; its liveness reads the new expiry."""
    ck, rec, s, live = launched
    before = auth.egress_key_incarnation(ck.state(), rec["id"])
    at = datetime.now(timezone.utc)
    ck.link({"op": "renew", "grant_id": rec["id"], "expires_at": auth._stamp(at + timedelta(days=20)),
             "signed_at": auth._stamp(at)}, at=at)
    assert ck.state().policy.grants[rec["id"]]["expires_at"] != rec["expires_at"]
    assert auth.egress_key_incarnation(ck.state(), rec["id"]) == before
    assert _real(s, live, frame(path=INFO_REFS, query=SVC)).attach == "attached"


def test_f2_the_incarnation_is_none_for_no_record(launched):
    ck, rec, s, live = launched
    assert auth.egress_key_incarnation(ck.state(), "ek-nothing") is None
    _revoke(ck, rec["id"])
    assert auth.egress_key_incarnation(ck.state(), rec["id"]) is None


def test_f2_the_liveness_check_compares_the_incarnation(launched):
    ck, rec, s, live = launched
    r = s.routes["example.com"]
    assert live.check(r.record_id, r.incarnation) is True
    assert live.check(r.record_id, "1:" + "f" * 64) is False and live.check(r.record_id, None) is False


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
    _create(ck, "ek-000000000002", "git.example.org")                              # the seam
    d = keys.decide_key(route, p, liveness=live, credentials=_creds())                # R-6
    assert calls == ["git.example.org"]
    assert (d.keyed, d.attach, d.credential) == (False, "none", None)
    assert "authorization" in stripped.stripped


def test_build_route_set_reads_only_the_entitys_live_records(tmp_path):
    ck = Cockpit(tmp_path)
    _create(ck, "ek-000000000001", entity="other", repos=())
    assert _launch(ck).routes == {}


def test_build_route_set_refuses_two_live_records_of_the_entity_for_one_host_only(tmp_path, monkeypatch):
    """The per-entity rule (F10): another entity's two live records for a host are not this launch's."""
    ck = Cockpit(tmp_path)
    monkeypatch.setattr(auth, "_egress_key_append_check", lambda state, rec, now: None)   # a foreign writer
    _create(ck, "ek-000000000001", entity="other")
    _create(ck, "ek-000000000002", entity="other")
    assert _launch(ck).routes == {}
    with pytest.raises(keys.LaunchRefused, match="ek-000000000001, ek-000000000002"):
        _launch(ck, entity="other")


def test_the_launch_reads_the_records_signed_repos_never_the_workspace_remotes(tmp_path):
    """K-8: the launch reads the record's signed repos only; remotes added to the workspace later change nothing."""
    import subprocess

    ck = Cockpit(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    subprocess.run(["git", "init", "-q", str(ws)], check=True, timeout=30)
    remotes = ["https://Example.com/o/r.git", "https://user@example.com:443/o/s/", "http://example.com/o/plain.git",
               "https://example.com:8443/o/port.git", "git@example.com:o/ssh.git", "https://other.example/o/x.git"]
    for i, url in enumerate(remotes):
        subprocess.run(["git", "-C", str(ws), "remote", "add", f"r{i}", url], check=True, timeout=30)
    _create(ck, "ek-000000000001", repos=("/added.git", "/o/r.git", "/o/s"))
    subprocess.run(["git", "-C", str(ws), "remote", "add", "late", "https://example.com/o/late.git"], check=True,
                   timeout=30)
    assert _launch(ck).routes["example.com"].declared_repos == frozenset({"/added.git", "/o/r.git", "/o/s"})


def test_k8_a_record_with_no_repos_keys_the_host_and_attaches_on_no_upload_pack_prefix(tmp_path):
    """A record with no declared repo: the host is keyed, and every upload-pack exchange, discovery and POST alike,
    goes keyless (no store read)."""
    ck = Cockpit(tmp_path)
    rec = _create(ck, "ek-000000000001", repos=())
    s = _launch(ck)
    store = FakeStore()
    live = Live([rec["id"]])
    for f in (frame(path=INFO_REFS, query=SVC), frame(method="POST", path="/o/r.git/git-upload-pack")):
        d = keys.decide_key(keys.lookup(s, "example.com"), parse(f), liveness=live, credentials=_creds(store))
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
    a = _create(ck, "ek-000000000001", repos=())
    b = _create(ck, "ek-000000000002", "git.example.org", repos=(),
                placement={"scheme": "basic", "user": "x-access-token"})
    s = _launch(ck)
    assert sorted(keys.credential_placements(s), key=lambda p: p["host"]) == [
        {"class": a["id"], "host": "example.com", "scheme": "bearer"},
        {"class": b["id"], "host": "git.example.org", "scheme": "basic", "user": "x-access-token"}]


# --- K2: the value appears in no artifact the run wrote ----------------------------------------------------------

def test_k2_the_key_value_is_in_no_artifact(launched, tmp_path):
    ck, rec, s, live = launched
    creds = keys.LaunchCredentials(FakeStore())
    for f in (frame(path=INFO_REFS, query=SVC), frame(host="b.example", path="/x"), frame(port=80, sni=None)):
        d = _real(s, live, f, creds)
        (tmp_path / "decisions.log").open("a").write(repr(d) + "\n" + str(d) + "\n" + repr(creds) + "\n")
    _revoke(ck, rec["id"])
    _real(s, live, frame(path=INFO_REFS, query=SVC), creds)
    assert artifact_hits(tmp_path) == []
    assert KEY not in repr(s) and KEY not in repr(s.routes)
