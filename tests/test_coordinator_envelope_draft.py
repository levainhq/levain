"""H1-1: the signed draft envelope (design r3 §8 tests 1-14 and 16). Real keys, real scratch git repos, the store's clock a
list the test moves; the fixtures are the envelope tests'. Synthetic words.

Each test names the line it guards; the lane's return records the revert that made it fail."""
from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

import pytest

from levain.cockpit.consent import utc_stamp
from levain.contracts.canon import canonical
from levain.coordinator import envelope as env
from levain.coordinator import envelope_draft as ed
from levain.coordinator import envelope_drafts as drafts
from levain.coordinator.envelope_drafts import DraftStale
from levain.coordinator.envelopes import EnvelopeConflict, EnvelopeStale, EnvelopeStore
from levain.coordinator.store import CoordError, Part
from tests.test_coordinator_envelope import BUDGET, HOUR, NOW, Ctx, _lease, keys, repo, snapshot  # noqa: F401  (fixtures)


@pytest.fixture
def ctx(tmp_path, keys, snapshot, repo):  # noqa: F811
    d = tmp_path / "dispatch"
    d.mkdir(mode=0o700)
    clk = [NOW]
    c = Ctx(EnvelopeStore(d, clock=lambda: clk[0]), keys, snapshot, repo, tmp_path)
    c.clk = clk
    return c


def fields_for(ctx, **over):
    f = {"where": [{"repo": ctx.path, "paths": ["README"]}], "lands": [ctx.land("main")], "pushes": [],
         "publishes": [], "budget": dict(BUDGET), "stop_at": utc_stamp(ctx.clk[0] + 2 * HOUR)}
    f.update(over)
    return f


def cites_for(drop, fields):
    return [{"field": k, "kind": "drop", "ref": drop.drop_id, "digest": drop.words_sha256} for k in sorted(fields)]


def make(ctx, words="make the readme say hello"):
    """A thread of one plain drop, and a complete first revision of its draft."""
    d, t = ctx.plain_thread(words)
    f = fields_for(ctx)
    return d, t, drafts.create(ctx.store, t.thread_id, f, cites_for(d, f), by="operator")


def built(ctx, dr, key="phone", signed_at=None):
    text, payload, since, against = drafts.draft_sign_bytes(
        ctx.store, dr.draft_id, dr.revision, dr.digest, authority=ctx.snap, signer_fp=ctx.keys[key].fp,
        signed_at=signed_at or ctx.clk[0])
    sig = ctx.keys[key].sign(env.sign_message(payload), env.SIGN_NAMESPACE)
    return (text, payload, sig), since, against


def envelopes(ctx):
    return ctx.rows("SELECT envelope_id FROM envelopes")


def raw(ctx):
    conn = sqlite3.connect(ctx.store.path)
    conn.execute("PRAGMA foreign_keys = OFF")
    return conn


@pytest.fixture
def spies(monkeypatch):
    """Counts of the verifier (``env.refusal``) and the payload builder (``env.sign_payload``)."""
    calls = {"refusal": 0, "sign_payload": 0}
    real_refusal, real_payload = env.refusal, env.sign_payload

    def refusal(*a, **k):
        calls["refusal"] += 1
        return real_refusal(*a, **k)

    def sign_payload(*a, **k):
        calls["sign_payload"] += 1
        return real_payload(*a, **k)

    monkeypatch.setattr(env, "refusal", refusal)
    monkeypatch.setattr(env, "sign_payload", sign_payload)
    return calls


def head_thread(ctx, words="make the readme say hello, and land it"):
    """A thread whose head is a signed structured envelope."""
    d, _fresh, b, _signed = ctx.structured(words)
    (t,) = ctx.store.split(d.drop_id, [Part("work", "hello")])
    return d, b, t


# 1 -----------------------------------------------------------------------------------------------------------------
def test_stale_revision_signed_through_plain_accept_is_refused(ctx):
    d, t, r1 = make(ctx)
    signed, _s, _a = built(ctx, r1)
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    with pytest.raises(DraftStale):
        ctx.accept(signed)
    assert envelopes(ctx) == []


# 2 -----------------------------------------------------------------------------------------------------------------
def test_stale_card_builds_no_bytes_and_calls_no_verifier(ctx, spies):
    d, t, r1 = make(ctx)
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    with pytest.raises(DraftStale):
        built(ctx, r1)
    assert spies == {"refusal": 0, "sign_payload": 0}


# 3 -----------------------------------------------------------------------------------------------------------------
def test_body_naming_a_revision_with_other_fields_is_refused(ctx):
    _d, _t, r1 = make(ctx)
    (text, _payload, _sig), _s, _a = built(ctx, r1)
    body = json.loads(text)
    body["lands"][0]["onto"] = "other"                      # a real branch: only the bytes differ from the revision
    other = canonical(body)
    payload = env.sign_payload(body, signer_fp=ctx.keys["phone"].fp, signed_at=NOW)
    sig = ctx.keys["phone"].sign(env.sign_message(payload), env.SIGN_NAMESPACE)
    with pytest.raises(CoordError, match="is not that revision"):
        ctx.store.accept_envelope(other, payload, sig, authority=ctx.snap)
    assert envelopes(ctx) == []


# 4 -----------------------------------------------------------------------------------------------------------------
def test_concurrent_revise_on_one_parent(ctx):
    d, _t, r1 = make(ctx)
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    with pytest.raises(DraftStale):
        drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    assert ctx.rows("SELECT revision FROM envelope_drafts ORDER BY revision") == [(1,), (2,)]


def test_a_raw_insert_with_a_wrong_parent_aborts_in_the_trigger(ctx):
    d, t, r1 = make(ctx)
    conn = raw(ctx)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="the draft moved"):
            conn.execute("INSERT INTO envelope_drafts (draft_id, revision, thread_id, gseq, parent, proposal, cites, "
                         "digest, by, at) VALUES (?, 2, ?, 999999, ?, '{}', '[]', 'sha256:x', 'operator', 'x')",
                         (r1.draft_id, t.thread_id, "sha256:" + "0" * 64))
    finally:
        conn.close()
    assert ctx.rows("SELECT revision FROM envelope_drafts") == [(1,)]


# 5 -----------------------------------------------------------------------------------------------------------------
def test_cite_must_match_its_record(ctx):
    old = ctx.plain("an old spec-grammar drop meant for another thread")          # sent BEFORE the series began
    d, t, r1 = make(ctx)
    f = fields_for(ctx)
    good = cites_for(d, f)

    def refused(cites, match):
        with pytest.raises(CoordError, match=match):
            drafts.revise(ctx.store, r1.draft_id, f, cites, parent=r1.digest, by="operator")

    wrong = [{**c, "digest": "sha256:" + "1" * 64} if c["field"] == "where" else c for c in good]
    refused(wrong, "digest that is not its words")
    for kind in ("message", "rule", "event"):
        refused([{**good[0], "kind": kind}, *good[1:]], "kind is one of")
    refused([{**good[0], "source": "x"}, *good[1:]], "exactly the keys")
    # an envelope bound to ANOTHER thread
    d2, other = ctx.plain_thread("some other work")
    eid = "ev-" + "a" * 32
    conn = raw(ctx)
    try:
        conn.execute("INSERT INTO gseq (at) VALUES ('x')")
        g = conn.execute("SELECT MAX(gseq) FROM gseq").fetchone()[0]
        conn.execute("INSERT INTO envelopes VALUES (?, ?, 'worded', ?, '{}', 'sha256:" + "2" * 64 + "', '{}', 's', "
                     "'fp', 'c', NULL, 'x', 'x')", (eid, g, d2.drop_id))
        conn.execute("INSERT INTO gseq (at) VALUES ('x')")
        conn.execute("INSERT INTO envelope_binds VALUES (?, ?, 1, ?, 'x')",
                     (eid, other.thread_id, conn.execute("SELECT MAX(gseq) FROM gseq").fetchone()[0]))
        conn.commit()
    finally:
        conn.close()
    refused([*(c for c in good if c["field"] != "stop_at"),
             {"field": "stop_at", "kind": "envelope", "ref": eid, "digest": "sha256:" + "2" * 64}],
            "not bound to this thread")
    # a non-member drop OLDER than the series' revision 1
    f2 = fields_for(ctx)
    later = ctx.plain("a correction sent after this draft began")
    swapped = [{**c, "ref": old.drop_id, "digest": old.words_sha256} if c["field"] == "where" else c for c in good]
    refused(swapped, "not in this thread and is not newer")
    # a non-member drop sent after revision 1, the right digest: accepted, and labelled as what it is
    newer = [{**c, "ref": later.drop_id, "digest": later.words_sha256} if c["field"] == "where" else c for c in good]
    r2 = drafts.revise(ctx.store, r1.draft_id, f2, newer, parent=r1.digest, by="operator")
    conn = ctx.store.open_readonly()
    try:
        label = drafts.cite_label(conn, r2, next(c for c in r2.cites if c["ref"] == later.drop_id))
    finally:
        conn.close()
    assert label["line"] == "not in this thread, sent after this draft began"
    assert "not authenticated before S1" in label["surface"] and "your words" not in json.dumps(label)


# 6 -----------------------------------------------------------------------------------------------------------------
def test_digest_separates_drafts(ctx):
    d, t, r1 = make(ctx)
    base = dict(store_id=ctx.store_id, thread_id=t.thread_id, parent=None, proposal=r1.proposal, cites=r1.cites)
    same = ed.draft_digest(draft_id=r1.draft_id, revision=1, **base)
    assert same == r1.digest
    assert ed.draft_digest(draft_id="ed-" + "b" * 32, revision=1, **base) != same
    assert ed.draft_digest(draft_id=r1.draft_id, revision=2, **base) != same
    body = ed.draft_body(r1.proposal, r1.draft_id, 1, r1.digest)
    assert r1.digest != env.envelope_digest(body)
    # an unsorted stored cite list is refused on read
    swapped = canonical(list(reversed(r1.cites)))
    conn = raw(ctx)
    try:
        conn.execute("DROP TRIGGER envelope_drafts_frozen")
        conn.execute("UPDATE envelope_drafts SET cites = ?", (swapped,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(CoordError, match="sorted by"):
        drafts.latest(ctx.store, r1.draft_id)


# 7 -----------------------------------------------------------------------------------------------------------------
def test_v1_reads_and_verifies_after_the_bump(ctx):
    from levain.coordinator import budget

    d, t = ctx.plain_thread("an envelope signed before the upgrade")
    body = ctx.body(d.drop_id, d.words, origin="worded",
                    binds=[{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256}])
    del body["draft"]
    body["format"] = env.BODY_FORMAT_V1
    payload = env.sign_payload(body, signer_fp=ctx.keys["phone"].fp, signed_at=NOW)
    sig = ctx.keys["phone"].sign(env.sign_message(payload), env.SIGN_NAMESPACE)
    conn = raw(ctx)
    try:
        for _ in range(2):
            conn.execute("INSERT INTO gseq (at) VALUES ('x')")
        g = [r[0] for r in conn.execute("SELECT gseq FROM gseq ORDER BY gseq DESC LIMIT 2")]
        conn.execute("INSERT INTO envelopes VALUES (?, ?, 'worded', ?, ?, ?, ?, ?, ?, 'display-phone', NULL, ?, ?)",
                     (body["envelope_id"], g[0], d.drop_id, canonical(body), env.envelope_digest(body),
                      canonical(payload), sig, payload["signer_fp"], body["stop_at"], utc_stamp(NOW)))
        conn.execute("INSERT INTO envelope_binds VALUES (?, ?, ?, ?, ?)",
                     (body["envelope_id"], t.thread_id, t.rev, g[1], utc_stamp(NOW)))
        conn.commit()
    finally:
        conn.close()
    assert ctx.live(body["envelope_id"], t) is None
    assert ctx.store.effective(body["envelope_id"], t.thread_id)["where"] == body["where"]
    assert ctx.store.envelope(body["envelope_id"]).body["format"] == env.BODY_FORMAT_V1
    queue = ctx.tmp / "queue"
    queue.mkdir()
    rc = sqlite3.connect(ctx.store.path)
    try:
        assert budget.gauge(rc, queue, t.thread_id) is not None
    finally:
        rc.close()
    ctx.clk[0] = NOW + 3 * HOUR
    assert "stopped" in ctx.live(body["envelope_id"], t)            # only the clock now, never the format
    ctx.clk[0] = NOW
    before = ctx.rows("SELECT * FROM envelopes")
    other = {**body, "envelope_id": "ev-" + "c" * 32}
    p2 = env.sign_payload(other, signer_fp=ctx.keys["phone"].fp, signed_at=NOW)
    s2 = ctx.keys["phone"].sign(env.sign_message(p2), env.SIGN_NAMESPACE)
    for text, pl, sg in ((canonical(body), payload, sig), (canonical(other), p2, s2)):
        with pytest.raises(CoordError, match="no longer accepted.*levain envelope show"):
            ctx.store.accept_envelope(text, pl, sg, authority=ctx.snap)
    assert ctx.rows("SELECT * FROM envelopes") == before


# 9 -----------------------------------------------------------------------------------------------------------------
def test_closed_series_take_no_revision(ctx):
    d, t, r1 = make(ctx)
    signed, _s, _a = built(ctx, r1)
    stored, created = ctx.accept(signed)
    assert created and stored.body["draft"] == {"draft_id": r1.draft_id, "revision": 1, "digest": r1.digest}
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    with pytest.raises(CoordError, match="signed or withdrawn"):
        drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    with pytest.raises(CoordError, match="only the latest revision of an unsigned draft"):
        drafts.withdraw(ctx.store, r1.draft_id)
    with pytest.raises(DraftStale):
        built(ctx, r1)
    # a second signature over the same revision (another signed_at) is another record for the same id
    text, payload, _sig = signed
    again = env.sign_payload(json.loads(text), signer_fp=ctx.keys["phone"].fp, signed_at=NOW + timedelta(seconds=1))
    sig2 = ctx.keys["phone"].sign(env.sign_message(again), env.SIGN_NAMESPACE)
    with pytest.raises(EnvelopeConflict):
        ctx.store.accept_envelope(text, again, sig2, authority=ctx.snap)
    # a withdrawn series: no revision, no signature, no replacing its withdrawal row
    d2, t2, w1 = make(ctx, "a second thread's work")
    drafts.withdraw(ctx.store, w1.draft_id)
    with pytest.raises(CoordError, match="signed or withdrawn"):
        drafts.revise(ctx.store, w1.draft_id, f, cites_for(d2, f), parent=w1.digest, by="operator")
    with pytest.raises(DraftStale):
        built(ctx, w1)
    conn = raw(ctx)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
            conn.execute("INSERT OR REPLACE INTO envelope_draft_withdrawals (draft_id, revision, gseq, by, at) "
                         "VALUES (?, 1, 888888, 'operator', 'x')", (w1.draft_id,))
    finally:
        conn.close()


# 10 ----------------------------------------------------------------------------------------------------------------
def test_lapsed_draft_refused_before_verify(ctx, spies):
    d, t, r1 = make(ctx)
    d2 = ctx.plain("also fix the typo")
    ctx.store.split(d2.drop_id, [Part("work", attach=t.thread_id)])          # the thread's revision moves
    assert drafts.state_of(ctx.store, r1.draft_id, 1).kind == "lapsed"
    with pytest.raises(DraftStale, match="lapsed"):
        built(ctx, r1)
    assert spies == {"refusal": 0, "sign_payload": 0}
    f = fields_for(ctx)
    r2 = drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    assert r2.proposal["binds"][0]["rev"] == 2 and drafts.state_of(ctx.store, r1.draft_id).kind == "open"


# 11 ----------------------------------------------------------------------------------------------------------------
def test_incomplete_revision_is_not_signable(ctx):
    d, t = ctx.plain_thread("fix it, somehow")
    f = {"where": fields_for(ctx)["where"]}
    r1 = drafts.create(ctx.store, t.thread_id, f, cites_for(d, f), by="operator")
    st = drafts.state_of(ctx.store, r1.draft_id)
    assert st.kind == "open" and st.complete is False
    with pytest.raises(DraftStale, match="incomplete"):
        built(ctx, r1)
    shown = drafts.latest(ctx.store, r1.draft_id)
    assert shown.proposal["stop_at"] is None and shown.proposal["lands"] is None


# 12 ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize("kind", ["kill", "stop_earlier", "drop_effect"])
def test_narrowing_after_render_lapses_the_draft(ctx, spies, kind, scoped):
    hd, head, t = head_thread(ctx)
    f = fields_for(ctx)
    r1 = drafts.create(ctx.store, t.thread_id, f, cites_for(hd, f), by="operator")
    assert r1.proposal["supersedes"]["envelope_id"] == head["envelope_id"]
    signed, _s, _a = built(ctx, r1)
    spies["refusal"] = spies["sign_payload"] = 0
    value = {"kill": "", "stop_earlier": utc_stamp(NOW + HOUR),
             "drop_effect": env.effect_identity("lands", head["lands"][0])}[kind]
    assert ctx.store.attenuate(head["envelope_id"], kind, value, thread_id=t.thread_id if scoped else None)
    assert drafts.state_of(ctx.store, r1.draft_id, 1).kind == "lapsed"
    with pytest.raises(DraftStale, match="lapsed"):
        built(ctx, r1)
    assert spies == {"refusal": 0, "sign_payload": 0}
    with pytest.raises((DraftStale, EnvelopeStale)):                       # signed before the narrowing, sent after
        ctx.accept(signed)
    assert envelopes(ctx) == [(head["envelope_id"],)]
    r2 = drafts.revise(ctx.store, r1.draft_id, f, cites_for(hd, f), parent=r1.digest, by="operator")
    _signed, _since, against = built(ctx, r2)
    assert against.wider
    if kind == "kill":
        assert against.base_kind == "revoked" and against.diff["effects"]["lands"]["added"]
    elif kind == "drop_effect":
        assert against.base_kind == "effective"
        assert env.effect_identity("lands", r2.proposal["lands"][0]) in against.diff["effects"]["lands"]["added"]
    else:
        assert against.diff["stop_at"] == (value, r2.proposal["stop_at"])


# 13 ----------------------------------------------------------------------------------------------------------------
def test_one_unclosed_series_per_thread(ctx):
    d, t, a1 = make(ctx)
    d2 = ctx.plain("also fix the typo")
    ctx.store.split(d2.drop_id, [Part("work", attach=t.thread_id)])          # series A lapses
    f = fields_for(ctx)
    with pytest.raises(CoordError, match="one unclosed draft series"):
        drafts.create(ctx.store, t.thread_id, f, cites_for(d, f), by="operator")
    conn = raw(ctx)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="one unclosed draft series"):
            conn.execute("INSERT INTO envelope_drafts (draft_id, revision, thread_id, gseq, parent, proposal, cites, "
                         "digest, by, at) VALUES (?, 1, ?, 777777, NULL, '{}', '[]', 'sha256:y', 'operator', 'x')",
                         ("ed-" + "d" * 32, t.thread_id))
    finally:
        conn.close()
    a2 = drafts.revise(ctx.store, a1.draft_id, f, cites_for(d, f), parent=a1.digest, by="operator")
    drafts.withdraw(ctx.store, a1.draft_id)
    b1 = drafts.create(ctx.store, t.thread_id, f, cites_for(d, f), by="operator")
    assert a2.revision == 2 and b1.revision == 1 and b1.draft_id != a1.draft_id


# 14 ----------------------------------------------------------------------------------------------------------------
def test_a_revision_is_born_open(ctx):
    d, t = ctx.plain_thread("fix it")
    short = fields_for(ctx, stop_at=utc_stamp(NOW + timedelta(minutes=4)))          # 4 minutes
    with pytest.raises(CoordError, match="born open"):
        drafts.create(ctx.store, t.thread_id, short, cites_for(d, short), by="operator")
    assert ctx.rows("SELECT * FROM envelope_drafts") == []
    f = fields_for(ctx)
    r1 = drafts.create(ctx.store, t.thread_id, f, cites_for(d, f), by="operator")
    # the two other clauses cannot be written through the API (binds and seen are read under the lock); the shared
    # predicate that gates the write is held directly
    conn = ctx.store.open_readonly()
    try:
        now = utc_stamp(NOW)
        moved = {**r1.proposal, "binds": [{**r1.proposal["binds"][0], "rev": 7}]}
        assert "changed since" in drafts.lapse_reason(conn, now, t.thread_id, moved)
        assert drafts.lapse_reason(conn, now, t.thread_id, r1.proposal) is None
    finally:
        conn.close()
    hd, head, ht = head_thread(ctx, "land the readme change")
    g = fields_for(ctx)
    h1 = drafts.create(ctx.store, ht.thread_id, g, cites_for(hd, g), by="operator")
    ctx.store.attenuate(head["envelope_id"], "kill")
    conn = ctx.store.open_readonly()
    try:
        old = {**h1.proposal, "supersedes": {**h1.proposal["supersedes"], "seen": 0}}
        assert "narrowed after" in drafts.lapse_reason(conn, utc_stamp(NOW), ht.thread_id, old)
    finally:
        conn.close()
    ctx.store.set_state(t.thread_id, "done", by="coordinator", token=_lease(ctx.store))          # a done thread takes no first revision
    with pytest.raises(CoordError, match="born open"):
        drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")


# 16 ----------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["kill", "stop_earlier", "drop_effect"])
def test_direct_body_narrowed_after_render_is_refused(ctx, kind):
    hd, head, t = head_thread(ctx)
    sup = {"envelope_id": head["envelope_id"], "digest": env.envelope_digest(head)}
    body, signed = ctx.worded(hd, t, supersedes=sup)               # seen = the store's high-water now
    assert body["supersedes"]["seen"] == ctx.seen()
    value = {"kill": "", "stop_earlier": utc_stamp(NOW + HOUR),
             "drop_effect": env.effect_identity("lands", head["lands"][0])}[kind]
    assert ctx.store.attenuate(head["envelope_id"], kind, value)
    with pytest.raises(EnvelopeStale, match="narrowed after"):
        ctx.accept(signed)
    assert envelopes(ctx) == [(head["envelope_id"],)]


def test_a_superseding_body_without_seen_is_refused_by_parse_body(ctx):
    hd, head, t = head_thread(ctx)
    b = ctx.body(hd.drop_id, hd.words, origin="worded",
                 binds=[{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256}],
                 supersedes={"envelope_id": head["envelope_id"], "digest": env.envelope_digest(head)})
    with pytest.raises(CoordError, match="supersedes"):
        ctx.accept(ctx.sign(b))
    assert envelopes(ctx) == [(head["envelope_id"],)]
