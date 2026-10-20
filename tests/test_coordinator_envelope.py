"""Coordinator C2a: thread envelopes (body, digest, signature, acceptance, binding, narrowing, liveness, ceiling),
pinned to what the C2a residue run showed. Keys are real ssh-keygen keys enrolled in a real signed authority log (the
consent queue tests' ``Key`` and ``Log``); repos are real scratch git repos; the store clock is fixed. Synthetic words."""
from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator.envelopes import NOT_IN_FORCE, EnvelopeConflict, EnvelopeStore
from levain.coordinator.store import CoordError, Part, Source
from tests.test_consent_queue import Key, Log

NOW = datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
BUDGET = {"tool_calls": 200, "tokens": 2_000_000, "dollars_cents": 500, "hours": 2}


def _git(*a: str) -> str:
    cp = subprocess.run(["git", "-c", "user.email=r@x", "-c", "user.name=r", *a], capture_output=True, text=True,
                        check=True)
    return cp.stdout.strip()


def _sha(words: str) -> str:
    return "sha256:" + hashlib.sha256(words.encode()).hexdigest()


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
    d = tmp_path_factory.mktemp("envelope-keys")
    return {n: Key(d, n) for n in ("laptop", "phone", "presence", "revoked", "stranger")}


@pytest.fixture(scope="module")
def snapshot(keys, tmp_path_factory):
    """laptop (genesis, laptop-presence), phone (display-phone), presence (below T2), a revoked phone-class key."""
    lg = Log(tmp_path_factory.mktemp("envelope-auth"), keys)
    for name, cls in (("laptop", "laptop-presence"), ("phone", "display-phone"), ("presence", "presence"),
                      ("revoked", "display-phone")):
        lg.enrol(name, cls)
    lg.revoke("revoked")
    return lg.source()


@pytest.fixture(scope="module")
def repo(tmp_path_factory):
    r = tmp_path_factory.mktemp("envelope-repo") / "repo"
    r.mkdir()
    real = os.path.realpath(r)
    _git("init", "-q", "-b", "main", real)
    (r / "README").write_text("scratch\n")
    _git("-C", real, "add", "README")
    _git("-C", real, "commit", "-qm", "init")
    _git("-C", real, "branch", "other")
    _git("-C", real, "branch", "third")
    return {"path": real, "head": _git("-C", real, "rev-parse", "HEAD")}


class Ctx:
    def __init__(self, store, keys, snap, repo, tmp_path):
        self.store, self.keys, self.snap, self.repo, self.tmp = store, keys, snap, repo, tmp_path
        self.path = repo["path"]
        self.src = Source("cwd", self.path, "refs/heads/main", repo["head"])
        store.drops()  # create the file so the store id exists
        self.store_id = sqlite3.connect(store.path).execute("SELECT value FROM meta WHERE key='store_id'").fetchone()[0]

    def land(self, onto="main", **kw):
        d = {"repo": self.path, "onto": onto, "from": "named", "onto_from": "inferred", "criteria": []}
        d.update(kw)
        return d

    def body(self, drop_id, words, **over):
        b = {"format": env.BODY_FORMAT, "store_id": self.store_id, "envelope_id": "ev-" + secrets.token_hex(16),
             "origin": "structured", "drop_id": drop_id, "words_sha256": _sha(words), "binds": [],
             "supersedes": None, "where": [{"repo": self.path, "paths": ["README"]}],
             "lands": [self.land("main", **{"from": "source", "onto_from": "drop"})], "pushes": [], "publishes": [],
             "budget": dict(BUDGET), "stop_at": utc_stamp(NOW + 2 * HOUR), "issued_at": utc_stamp(NOW)}
        b.update(over)
        return b

    def sign(self, body, key="phone", signed_at=NOW):
        payload = env.sign_payload(body, signer_fp=self.keys[key].fp, signed_at=signed_at)
        return canonical(body), payload, self.keys[key].sign(env.sign_message(payload), env.SIGN_NAMESPACE)

    def plain(self, words):
        d, fresh = self.store.accept_drop(words, surface="cli", source=self.src)
        assert fresh
        return d

    def structured(self, words, key="phone", **over):
        drop_id = over.pop("drop_id", "dr-" + secrets.token_hex(16))
        b = self.body(drop_id, words, **over)
        text, payload, sig = self.sign(b, key)
        d, fresh = self.store.accept_drop(words, surface="cli", source=self.src, authority=self.snap,
                                          envelope=(text, payload, sig))
        return d, fresh, b, (text, payload, sig)

    def worded(self, drop, thread, key="phone", *, supersedes=None, signed_at=NOW, **over):
        b = self.body(drop.drop_id, drop.words, origin="worded",
                      binds=[{"thread_id": thread.thread_id, "rev": thread.rev,
                              "members_sha256": thread.members_sha256}], supersedes=supersedes, **over)
        return b, self.sign(b, key, signed_at)

    def accept(self, signed):
        text, payload, sig = signed
        return self.store.accept_envelope(text, payload, sig, authority=self.snap)

    def rows(self, sql):
        return sqlite3.connect(self.store.path).execute(sql).fetchall()

    def receipt(self, drop):
        return [r.body for r in self.store.drop_receipts(drop.drop_id)]

    def live(self, eid, thread, **kw):
        return self.store.live_refusal(eid, thread.thread_id, authority=self.snap, **kw)

    def plain_thread(self, words):
        d = self.plain(words)
        (t,) = self.store.split(d.drop_id, [Part("work", "w")])
        return d, t


@pytest.fixture
def ctx(tmp_path, keys, snapshot, repo):
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    return Ctx(EnvelopeStore(d, clock=lambda: NOW), keys, snapshot, repo, tmp_path)


@pytest.fixture
def structured_thread(ctx):
    d, _fresh, b, _signed = ctx.structured("make the readme say hello, and land it")
    (t,) = ctx.store.split(d.drop_id, [Part("work", "hello")])
    return d, b, t


# 1 -----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["laptop", "phone"])
def test_signed_digest_equals_parsed_stored_and_recomputed_digest(ctx, key):
    d, _fresh, b, (text, payload, _sig) = ctx.structured("one digest everywhere", key=key)
    stored_digest = ctx.rows(f"SELECT digest FROM envelopes WHERE envelope_id = '{b['envelope_id']}'")[0][0]
    stored_body = ctx.rows(f"SELECT body FROM envelopes WHERE envelope_id = '{b['envelope_id']}'")[0][0]
    assert d.envelope_id == b["envelope_id"]
    assert payload["digest"] == env.envelope_digest(env.parse_body(text)) == stored_digest
    assert stored_digest == env.envelope_digest(json.loads(stored_body))
    assert stored_body == text


# 2, 3 --------------------------------------------------------------------------------------------------------------
def test_structured_envelope_binds_to_its_drops_one_new_thread(ctx, structured_thread):
    d, b, t = structured_thread
    assert ctx.rows("SELECT envelope_id, thread_id, rev FROM envelope_binds") == [(b["envelope_id"], t.thread_id, 1)]
    assert ctx.live(b["envelope_id"], t) is None


def test_structured_envelope_split_into_two_threads_binds_nothing(ctx):
    d, _fresh, b, _s = ctx.structured("two things at once")
    t1, t2 = ctx.store.split(d.drop_id, [Part("work", "one"), Part("work", "two")])
    assert ctx.rows("SELECT * FROM envelope_binds") == []
    msg = ctx.live(b["envelope_id"], t1)
    assert msg is not None and "is not bound to thread" in msg


# 4 -----------------------------------------------------------------------------------------------------------------
def test_second_drop_attached_to_bound_thread_moves_the_revision_and_writes_no_attenuation(ctx, structured_thread):
    _d, b, t = structured_thread
    d2 = ctx.plain("also fix the typo")
    ctx.store.split(d2.drop_id, [Part("work", attach=t.thread_id)])
    msg = ctx.live(b["envelope_id"], t)
    assert msg is not None and "moved past the revision" in msg
    assert ctx.rows("SELECT * FROM envelope_attenuations") == []


# 5 -----------------------------------------------------------------------------------------------------------------
def test_identical_resend_returns_stored_drop_and_writes_no_second_envelope(ctx):
    words = "resend me"
    d, _f, b, signed = ctx.structured(words)
    d2, fresh2 = ctx.store.accept_drop(words, surface="cli", outbox_id=d.outbox_id, source=ctx.src,
                                       authority=ctx.snap, envelope=signed)
    assert (d2.drop_id, fresh2) == (d.drop_id, False)
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(1,)]


# 6 -----------------------------------------------------------------------------------------------------------------
def test_widened_body_under_original_signature_stores_drop_plain_with_refusal_receipt(ctx):
    words = "keep my words"
    drop_id = "dr-" + secrets.token_hex(16)
    b = ctx.body(drop_id, words)
    _text, payload, sig = ctx.sign(b)
    wide = dict(b, where=[{"repo": ctx.path, "paths": ["."]}])
    d, fresh = ctx.store.accept_drop(words, surface="cli", source=ctx.src, authority=ctx.snap,
                                     envelope=(canonical(wide), payload, sig))
    assert fresh and d.envelope_id is None and d.words == words
    (receipt,) = ctx.receipt(d)
    assert receipt["envelope_refused"] == "the signature is over another envelope digest"
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(0,)]


# 7 -----------------------------------------------------------------------------------------------------------------
def test_envelope_id_is_never_reused_by_a_resigned_different_body(ctx):
    d, _f, b, _s = ctx.structured("first use of the id")
    words2 = "second use of the id"
    b2 = ctx.body("dr-" + secrets.token_hex(16), words2, envelope_id=b["envelope_id"],
                  where=[{"repo": ctx.path, "paths": ["."]}])
    d2, _fresh = ctx.store.accept_drop(words2, surface="cli", source=ctx.src, authority=ctx.snap,
                                       envelope=ctx.sign(b2))
    (receipt,) = ctx.receipt(d2)
    assert "is never reused" in receipt["envelope_refused"]
    assert d2.words == words2 and d2.envelope_id is None
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(1,)]


def test_worded_envelope_reusing_a_stored_id_with_other_content_is_a_conflict(ctx, structured_thread):
    d, b, t = structured_thread
    wb, signed = ctx.worded(d, t, envelope_id=b["envelope_id"])
    with pytest.raises(EnvelopeConflict, match="already stored with a different"):
        ctx.accept(signed)
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(1,)]


# 8 -----------------------------------------------------------------------------------------------------------------
def test_kill_is_written_once_and_ends_liveness(ctx, structured_thread):
    _d, b, t = structured_thread
    assert ctx.store.attenuate(b["envelope_id"], "kill") is True
    assert ctx.store.attenuate(b["envelope_id"], "kill") is False
    msg = ctx.live(b["envelope_id"], t)
    assert msg is not None and "was killed" in msg


def test_thread_done_writes_a_service_attenuation_and_ends_liveness(ctx, structured_thread):
    _d, b, t = structured_thread
    ctx.store.set_state(t.thread_id, "working", by="coordinator")
    ctx.store.set_state(t.thread_id, "done", by="coordinator")
    assert ctx.rows("SELECT kind, by FROM envelope_attenuations") == [("thread_done", "service")]
    msg = ctx.live(b["envelope_id"], t)
    assert msg is not None and "is done, which ended envelope" in msg


def test_closing_a_thread_by_the_operator_writes_thread_done(ctx, structured_thread):
    _d, b, t = structured_thread
    ctx.store.set_state(t.thread_id, "closed", by="operator")
    assert ctx.rows("SELECT kind, by FROM envelope_attenuations") == [("thread_done", "service")]
    assert "is done" in ctx.live(b["envelope_id"], t)


# 9 -----------------------------------------------------------------------------------------------------------------
def test_stop_earlier_narrows_and_a_later_stop_is_refused(ctx, structured_thread):
    _d, b, t = structured_thread
    earlier = utc_stamp(NOW + HOUR)
    assert ctx.store.attenuate(b["envelope_id"], "stop_earlier", earlier) is True
    assert ctx.store.effective(b["envelope_id"], t.thread_id)["stop_at"] == earlier
    for later in (utc_stamp(NOW + timedelta(minutes=90)), utc_stamp(NOW + 3 * HOUR)):
        with pytest.raises(CoordError, match="widening"):
            ctx.store.attenuate(b["envelope_id"], "stop_earlier", later)


def test_drop_effect_removes_one_land_from_effective_and_the_ceiling(ctx):
    a, c = ctx.land("main"), ctx.land("other")
    d, _f, b, _s = ctx.structured("two lands", lands=[a, c])
    (t,) = ctx.store.split(d.drop_id, [Part("work", "w")])
    eid = b["envelope_id"]
    with pytest.raises(CoordError, match="identity the signed envelope holds"):
        ctx.store.attenuate(eid, "drop_effect", env.effect_identity("lands", ctx.land("third")))
    ident = env.effect_identity("lands", c)
    assert ctx.store.attenuate(eid, "drop_effect", ident) is True
    assert ctx.store.attenuate(eid, "drop_effect", ident) is False
    assert ctx.store.effective(eid, t.thread_id)["lands"] == [a]
    refused = ctx.store.ceiling_refusal(eid, t.thread_id, effect=("lands", c))
    assert refused is not None and "does not hold that land" in refused
    assert ctx.store.ceiling_refusal(eid, t.thread_id, effect=("lands", a)) is None


# 10 ----------------------------------------------------------------------------------------------------------------
def test_worded_envelope_binds_the_current_revision(ctx):
    d, t = ctx.plain_thread("a plain drop, echoed later")
    wb, signed = ctx.worded(d, t)
    stored, fresh = ctx.accept(signed)
    assert fresh and stored.origin == "worded"
    assert ctx.rows("SELECT envelope_id, thread_id, rev FROM envelope_binds") == [(wb["envelope_id"], t.thread_id, 1)]
    assert ctx.live(wb["envelope_id"], t) is None


def test_worded_envelope_for_a_stale_revision_is_refused(ctx):
    d, t = ctx.plain_thread("a plain drop that moves")
    d2 = ctx.plain("one more thing")
    ctx.store.split(d2.drop_id, [Part("work", attach=t.thread_id)])
    wb, signed = ctx.worded(d, t)  # t is the revision-1 view
    with pytest.raises(CoordError, match="moved since the echo"):
        ctx.accept(signed)
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(0,)]


def test_second_worded_envelope_must_supersede_the_head_by_id_and_digest(ctx):
    d, t = ctx.plain_thread("a plain drop with two echoes")
    w1, s1 = ctx.worded(d, t)
    ctx.accept(s1)
    _w, unsuperseding = ctx.worded(d, t)
    with pytest.raises(CoordError, match="already has envelope"):
        ctx.accept(unsuperseding)
    sup = {"envelope_id": w1["envelope_id"], "digest": env.envelope_digest(w1)}
    w2, s2 = ctx.worded(d, t, supersedes=sup)
    stored, fresh = ctx.accept(s2)
    assert fresh and stored.supersedes == w1["envelope_id"]
    assert ctx.live(w2["envelope_id"], t, new_pin=True) is None
    msg = ctx.live(w1["envelope_id"], t, new_pin=True)
    assert msg is not None and "is superseded for thread" in msg
    assert ctx.live(w1["envelope_id"], t) is None


# 11 ----------------------------------------------------------------------------------------------------------------
def test_cumulative_ceiling_never_widens_back(ctx):
    a, c = ctx.land("main"), ctx.land("other")
    d, t = ctx.plain_thread("narrowed and widened again")
    v, sv = ctx.worded(d, t, lands=[a, c])
    ctx.accept(sv)
    w, sw = ctx.worded(d, t, lands=[a], supersedes={"envelope_id": v["envelope_id"], "digest": env.envelope_digest(v)})
    ctx.accept(sw)
    x, sx = ctx.worded(d, t, lands=[a, c],
                       supersedes={"envelope_id": w["envelope_id"], "digest": env.envelope_digest(w)})
    ctx.accept(sx)
    msg = ctx.store.ceiling_refusal(v["envelope_id"], t.thread_id, effect=("lands", c))
    assert msg is not None and w["envelope_id"] in msg and "does not hold that land" in msg
    assert ctx.store.ceiling_refusal(v["envelope_id"], t.thread_id, effect=("lands", a)) is None


# 12 ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key,why", [
    ("stranger", "the signer is not an enrolled key"),
    ("revoked", "the signer's key is revoked"),
    ("presence", "does not cover T2"),
])
def test_signer_below_the_floor_is_refused(ctx, key, why):
    d, t = ctx.plain_thread("who signs")
    _b, signed = ctx.worded(d, t, key=key)
    with pytest.raises(CoordError, match=why):
        ctx.accept(signed)
    assert ctx.rows("SELECT COUNT(*) FROM envelopes") == [(0,)]


def test_signature_three_minutes_off_the_store_clock_is_refused(ctx):
    d, t = ctx.plain_thread("skewed")
    for signed_at in (NOW - timedelta(minutes=3), NOW + timedelta(minutes=3)):
        _b, signed = ctx.worded(d, t, signed_at=signed_at)
        with pytest.raises(CoordError, match="more than 120 s from the store's clock"):
            ctx.accept(signed)


def test_stop_more_than_a_day_after_signing_is_refused(ctx):
    d, t = ctx.plain_thread("too long")
    _b, signed = ctx.worded(d, t, stop_at=utc_stamp(NOW + timedelta(hours=24, minutes=1)))
    with pytest.raises(CoordError, match="at most 24 hours"):
        ctx.accept(signed)


def test_stop_already_passed_is_refused(ctx):
    d, t = ctx.plain_thread("too late")
    _b, signed = ctx.worded(d, t, issued_at=utc_stamp(NOW - 2 * HOUR), stop_at=utc_stamp(NOW - timedelta(seconds=30)),
                            signed_at=NOW - timedelta(seconds=90))
    with pytest.raises(CoordError, match="has passed"):
        ctx.accept(signed)


# 13 ----------------------------------------------------------------------------------------------------------------
def _good(ctx):
    return ctx.body("dr-" + secrets.token_hex(16), "shape words")


def test_parse_body_accepts_the_canonical_good_body(ctx):
    b = _good(ctx)
    assert env.parse_body(canonical(b)) == b


def test_parse_body_refuses_a_non_canonical_encoding(ctx):
    with pytest.raises(env.EnvelopeError):
        env.parse_body(json.dumps(_good(ctx), indent=2))


def test_parse_body_refuses_binds_and_supersedes_on_a_structured_body(ctx):
    bind = {"thread_id": "th-" + "0" * 32, "rev": 1, "members_sha256": "sha256:" + "0" * 64}
    with pytest.raises(env.EnvelopeError, match="names no binds"):
        env.parse_body(canonical(dict(_good(ctx), binds=[bind])))
    sup = {"envelope_id": "ev-" + "1" * 32, "digest": "sha256:" + "0" * 64}
    with pytest.raises(env.EnvelopeError, match="supersedes nothing"):
        env.parse_body(canonical(dict(_good(ctx), supersedes=sup)))


@pytest.mark.parametrize("criteria,why", [
    (["free text is not a criterion"], "exactly one key"),
    ([{"note": "free text"}], "is not one of"),
    ([{"onto": "main"}], "is not one of"),
    ([{"paths": ["setup.py"]}], "outside its repo's where"),
])
def test_parse_body_refuses_bad_criteria(ctx, criteria, why):
    b = _good(ctx)
    b["lands"] = [ctx.land("main", criteria=criteria, **{"from": "source", "onto_from": "drop"})]
    with pytest.raises(env.EnvelopeError, match=why):
        env.parse_body(canonical(b))


def test_land_from_source_onto_another_branch_is_refused_at_acceptance(ctx):
    words = "land somewhere the drop did not come from"
    b = ctx.body("dr-" + secrets.token_hex(16), words,
                 lands=[ctx.land("other", **{"from": "source", "onto_from": "drop"})])
    d, _fresh = ctx.store.accept_drop(words, surface="cli", source=ctx.src, authority=ctx.snap, envelope=ctx.sign(b))
    (receipt,) = ctx.receipt(d)
    assert "names the branch the drop was made from" in receipt["envelope_refused"]
    assert d.envelope_id is None


def test_where_repo_that_is_not_a_git_top_is_refused(ctx):
    plain_dir = os.path.realpath(ctx.tmp / "not-a-repo")
    os.mkdir(plain_dir)
    words = "scope a directory git does not own"
    b = ctx.body("dr-" + secrets.token_hex(16), words, where=[{"repo": plain_dir, "paths": ["README"]}], lands=[])
    d, _fresh = ctx.store.accept_drop(words, surface="cli", source=ctx.src, authority=ctx.snap, envelope=ctx.sign(b))
    (receipt,) = ctx.receipt(d)
    assert "is not a git work tree's top" in receipt["envelope_refused"]
    assert d.envelope_id is None


# 14 ----------------------------------------------------------------------------------------------------------------
def test_authority_refusal_is_not_in_force_for_a_live_envelope(ctx, structured_thread):
    _d, b, t = structured_thread
    assert ctx.live(b["envelope_id"], t) is None
    assert ctx.store.authority_refusal(b["envelope_id"], t.thread_id) == NOT_IN_FORCE


# --- L1/L2 round 1 findings, each pinned (C2a fix commit) -------------------------------------------------------

def test_a_plain_coordstore_ending_the_thread_still_kills_its_envelope(ctx, structured_thread):
    # L1 HIGH 2: C1's CLI opens a plain CoordStore on the same file; T7 must hold whichever class ended the thread
    from levain.coordinator.store import CoordStore
    _d, b, t = structured_thread
    plain = CoordStore(ctx.store.directory, clock=lambda: NOW)
    plain.set_state(t.thread_id, "closed", by="operator")
    assert ctx.rows("SELECT kind, by FROM envelope_attenuations") == [("thread_done", "service")]
    assert "is done" in ctx.live(b["envelope_id"], t)


def test_a_plain_coordstore_split_binds_the_structured_envelope(ctx):
    from levain.coordinator.store import CoordStore
    d, _fresh, b, _signed = ctx.structured("rename the heading")
    plain = CoordStore(ctx.store.directory, clock=lambda: NOW)
    (t,) = plain.split(d.drop_id, [Part("work", "heading")])
    assert ctx.rows("SELECT envelope_id, thread_id, rev FROM envelope_binds") == [(b["envelope_id"], t.thread_id, 1)]


@pytest.mark.parametrize("state", ["done", "closed"])
def test_a_worded_envelope_never_binds_an_ended_thread(ctx, state):
    # L1 HIGH 1: a bind after the end would never get a thread_done row, and a reopen would revive it
    d, t = ctx.plain_thread("tidy the docs")
    if state == "done":
        ctx.store.set_state(t.thread_id, "working", by="coordinator")
    ctx.store.set_state(t.thread_id, state, by="coordinator" if state == "done" else "operator")
    t = ctx.store.thread(t.thread_id)
    _b, signed = ctx.worded(d, t, lands=[ctx.land("main")])
    with pytest.raises(CoordError, match="reopen it before signing"):
        ctx.accept(signed)


@pytest.mark.parametrize("candidate", ["src/../.git/hooks/pre-commit", "src//x", "./src/x", "src/./x",
                                       "src/.GIT/hooks/x", "src/café"])
def test_a_candidate_path_is_held_to_the_signed_paths_shape(candidate):
    # L1 MED 6 / L2 MED: path_within validates the candidate, not only the allowed side
    assert env.path_within(candidate, ("src/",)) is False


@pytest.mark.parametrize("path", [".git/hooks/x", ".GIT/config", "sub/.git/hooks/x"])
def test_a_whole_repo_scope_never_reaches_a_git_directory(path):
    assert env.path_within(path, (".",)) is False


def test_a_signed_where_path_into_dot_git_is_refused_at_any_depth_and_case(ctx):
    d = ctx.plain("x")
    for p in (".GIT/hooks/", "sub/.git/config"):
        b = ctx.body(d.drop_id, d.words, where=[{"repo": ctx.path, "paths": [p]}], lands=[])
        with pytest.raises(env.EnvelopeError, match=r"\.git"):
            env.parse_body(canonical(b))


def test_a_job_stops_when_the_chains_current_head_is_killed(ctx):
    # L1 MED 5 / L2 MED: the head's liveness is the third condition (C0 §4.5)
    d, t = ctx.plain_thread("refactor the readme")
    bv, sv = ctx.worded(d, t, lands=[ctx.land("main")])
    ctx.accept(sv)
    bw, sw = ctx.worded(d, t, lands=[ctx.land("main")],
                        supersedes={"envelope_id": bv["envelope_id"], "digest": env.envelope_digest(bv)})
    ctx.accept(sw)
    assert ctx.store.chain_live_refusal(bv["envelope_id"], t.thread_id, authority=ctx.snap) is None
    ctx.store.attenuate(bw["envelope_id"], "kill")
    assert ctx.live(bv["envelope_id"], t) is None
    assert "chain's current envelope is not live" in ctx.store.chain_live_refusal(
        bv["envelope_id"], t.thread_id, authority=ctx.snap)


def test_a_supersede_that_edits_only_criteria_does_not_strand_the_land(ctx):
    # L2 MED: effects match by target; criteria from every chain member hold together
    d, t = ctx.plain_thread("trim the readme")
    v_land = ctx.land("main", criteria=[{"max_files": 5}])
    bv, sv = ctx.worded(d, t, lands=[v_land])
    ctx.accept(sv)
    bx, sx = ctx.worded(d, t, lands=[ctx.land("main", criteria=[{"max_files": 10}])],
                        supersedes={"envelope_id": bv["envelope_id"], "digest": env.envelope_digest(bv)})
    ctx.accept(sx)
    assert ctx.store.ceiling_refusal(bv["envelope_id"], t.thread_id, effect=("lands", v_land)) is None
    assert ctx.store.ceiling_criteria(bv["envelope_id"], t.thread_id, "lands", v_land) == [
        [{"max_files": 5}], [{"max_files": 10}]]


def test_a_verifier_that_cannot_run_still_keeps_the_words(ctx, monkeypatch):
    # L1 MED 3 / L2: a fault on this machine is not a verdict, and never costs the drop
    from levain.authority import VerifierUnavailable

    def broken(*_a, **_k):
        raise VerifierUnavailable("ssh-keygen is missing")

    monkeypatch.setattr(env, "verify_link_sig", broken)
    d, fresh, _b, _s = ctx.structured("keep these words")
    assert fresh and d.envelope_id is None and d.words == "keep these words"
    assert "could not be checked on this machine" in ctx.store.envelope_refused(d.drop_id)


def test_a_drop_carrying_an_envelope_needs_an_authority_snapshot(ctx):
    b = ctx.body("dr-" + secrets.token_hex(16), "w")
    with pytest.raises(CoordError, match="authority snapshot"):
        ctx.store.accept_drop("w", surface="cli", source=ctx.src, envelope=ctx.sign(b))


@pytest.mark.parametrize("onto", ["a..b", "Main", "nope"])
def test_a_land_onto_must_pass_f11s_branch_check(ctx, onto):
    # L1 MED 4 / L2 LOW: C0 §4.3 "onto passes F11's branch_problem"
    d, t = ctx.plain_thread("land somewhere")
    _b, signed = ctx.worded(d, t, lands=[ctx.land(onto)])
    with pytest.raises(CoordError, match="a land onto"):
        ctx.accept(signed)


def test_stop_earlier_at_the_current_stop_writes_nothing(ctx, structured_thread):
    # L1 LOW 10: a repeated tap is a no-op, like a second kill
    _d, b, _t = structured_thread
    assert ctx.store.attenuate(b["envelope_id"], "stop_earlier", b["stop_at"]) is False


def test_an_attenuation_with_a_non_text_value_is_a_coord_error(ctx, structured_thread):
    _d, b, _t = structured_thread
    with pytest.raises(CoordError):
        ctx.store.attenuate(b["envelope_id"], "drop_effect", ["x"])


# --- L3 round 1 findings, each pinned ---------------------------------------------------------------------------

def test_a_git_fault_checking_a_land_still_keeps_the_words(ctx, monkeypatch):
    # codex MED: branch_problem's GitFault is a fault, never a lost drop
    from levain.coordinator import envelopes
    from levain.dispatch.adopt import GitFault

    def broken(*_a, **_k):
        raise GitFault("git timed out")

    monkeypatch.setattr(envelopes, "branch_problem", broken)
    d, fresh, _b, _s = ctx.structured("still keep these words")
    assert fresh and d.envelope_id is None and d.words == "still keep these words"
    assert "could not be checked on this machine" in ctx.store.envelope_refused(d.drop_id)


def test_an_envelope_that_stops_before_it_was_signed_is_refused(ctx):
    # codex LOW: signed 12:01 within the skew, stop 12:00:30 ahead of the 12:00 clock
    d, t = ctx.plain_thread("sign late")
    _b, signed = ctx.worded(d, t, lands=[ctx.land("main")], signed_at=NOW + timedelta(minutes=1),
                            stop_at=utc_stamp(NOW + timedelta(seconds=30)))
    with pytest.raises(CoordError, match="stops before it was signed"):
        ctx.accept(signed)


def test_the_whole_repo_candidate_is_inside_only_a_whole_repo_scope():
    assert env.path_within(".", (".",)) is True
    assert env.path_within(".", ("src/",)) is False


def test_a_push_named_twice_is_refused(ctx):
    d = ctx.plain("push twice")
    push = {"repo": ctx.path, "remote": "origin", "url": "https://example.invalid/r.git", "branch": "main"}
    b = ctx.body(d.drop_id, d.words, pushes=[push, dict(push)])
    with pytest.raises(env.EnvelopeError, match="names one target twice"):
        env.parse_body(canonical(b))


@pytest.mark.parametrize("effect", [("pushs", {}), ("lands", {"repo": "/x"}), ("lands", None)])
def test_a_malformed_effect_is_a_refusal_line_never_a_crash(ctx, structured_thread, effect):
    _d, b, t = structured_thread
    assert "not a land, push or publish" in ctx.store.ceiling_refusal(b["envelope_id"], t.thread_id, effect=effect)


def test_an_inherited_git_dir_does_not_answer_for_the_where_repo(ctx, monkeypatch, tmp_path):
    # complement: a GIT_DIR from a hook or wrapper must not decide which repo a where names
    other = tmp_path / "other"
    _git("init", "-q", str(other))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    d, _fresh, _b, _s = ctx.structured("from inside a hook")
    assert d.envelope_id is not None, ctx.store.envelope_refused(d.drop_id)


# --- L3 round 2: the unhashable-value class (all three seats) -------------------------------------------------

def test_an_unhashable_push_field_is_a_shape_refusal_and_keeps_the_words(ctx):
    bad = {"repo": [1], "remote": "o", "url": "u", "branch": "b"}
    d = ctx.plain("x")
    with pytest.raises(env.EnvelopeError):
        env.parse_body(canonical(ctx.body(d.drop_id, d.words, pushes=[bad])))
    dropped, fresh, _b, _s = ctx.structured("keep me", pushes=[bad])
    assert fresh and dropped.envelope_id is None and dropped.words == "keep me"
    assert ctx.store.envelope_refused(dropped.drop_id)


@pytest.mark.parametrize("effect", [("lands", {"repo": [], "onto": "main"}), ("pushes", {"repo": {}, "remote": "o",
                                                                                         "url": "u", "branch": "b"})])
def test_an_unhashable_effect_asked_for_is_a_refusal_line(ctx, structured_thread, effect):
    _d, b, t = structured_thread
    assert "not a land, push or publish" in ctx.store.ceiling_refusal(b["envelope_id"], t.thread_id, effect=effect)


@pytest.mark.parametrize("scope", [("/r", "README"), ([], ["README"]), ("/r",), ("/r", [["README"]])])
def test_a_malformed_scope_is_a_refusal_line(ctx, structured_thread, scope):
    _d, b, t = structured_thread
    assert "not (repo, [paths])" in ctx.store.ceiling_refusal(b["envelope_id"], t.thread_id, scope=scope)
