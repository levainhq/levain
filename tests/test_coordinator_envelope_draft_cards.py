"""H1-2: head state and the LAPSED / REVOKED / ENVELOPE read model, and the renew/1 and replace/1 rules (design r3 §8,
H1-2 tests 1-13). Real keys, real scratch repos, the store's clock a list the test moves; the fixtures are H1-1's.
Synthetic words.

Each test names the line it guards; the lane's return records the revert that made it fail."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.cockpit.consent import utc_stamp
from levain.coordinator import budget
from levain.coordinator import envelope as env
from levain.coordinator import envelope_draft as ed
from levain.coordinator import envelope_drafts as drafts
from levain.coordinator.envelope_drafts import DraftStale
from levain.coordinator.envelopes import NOT_IN_FORCE
from levain.coordinator.store import CoordError, Part
from tests import test_coordinator_envelope_draft as h11
from tests.test_coordinator_envelope import BUDGET, HOUR, NOW, _lease
from tests.test_coordinator_envelope_draft import _revoked_phone, built, cites_for, fields_for, head_thread

# H1-1's fixtures, registered here: its store with a clock the test moves, and the keys, authority and repo under it
ctx, spies, keys, snapshot, repo = h11.ctx, h11.spies, h11.keys, h11.snapshot, h11.repo


def qdir(ctx):
    d = ctx.tmp / "queue"
    d.mkdir(exist_ok=True)
    return d


def hstate(ctx, thread, authority=None):
    return drafts.head_state(ctx.store, thread.thread_id, authority=authority or ctx.snap, queue_dir=qdir(ctx))


def facet(ctx, thread, authority=None):
    return drafts.authority_facet(ctx.store, thread.thread_id, authority=authority or ctx.snap, queue_dir=qdir(ctx))


def cards(ctx, thread, authority=None):
    return drafts.cards(ctx.store, thread.thread_id, authority=authority or ctx.snap, queue_dir=qdir(ctx))


def head_with(ctx, words, **over):
    """A thread whose head is a signed structured envelope with the body keys in ``over`` replaced."""
    d, _fresh, b, _signed = ctx.structured(words, **over)
    (t,) = ctx.store.split(d.drop_id, [Part("work", "hello")])
    return d, b, t


def later(ctx, hours=3):
    ctx.clk[0] = NOW + hours * HOUR


def kill(ctx, head, thread=None):
    assert ctx.store.attenuate(head["envelope_id"], "kill", thread_id=None if thread is None else thread.thread_id)


def replies(ctx):
    return ctx.rows("SELECT COUNT(*) FROM replies")[0][0]


# 1 -----------------------------------------------------------------------------------------------------------------
def test_kill_then_replace_revives_no_pin(ctx):
    hd, head, t = head_thread(ctx)
    kill(ctx, head)
    hs = hstate(ctx, t)
    assert (hs.kind, hs.reason) == ("revoked", "killed")
    r = drafts.replace_rule(ctx.store, t.thread_id, hs, by="operator")
    signed, _since, _against = built(ctx, r, key="laptop")
    stored, created = ctx.accept(signed)
    assert created and stored.envelope_id != head["envelope_id"]
    # a launch pinned to the killed envelope stays refused: its own liveness first, then the chain's current head
    assert "was killed" in ctx.store.chain_live_refusal(head["envelope_id"], t.thread_id, authority=ctx.snap)
    assert "was killed" in ctx.store.live_refusal(head["envelope_id"], t.thread_id, authority=ctx.snap)
    assert ctx.store.ceiling_refusal(head["envelope_id"], t.thread_id, effect=("lands", head["lands"][0])) is not None
    assert ctx.rows("SELECT kind FROM envelope_attenuations") == [("kill",)]            # nothing un-killed it
    # a NEW pin on the replacement: its own body is the ceiling, and liveness reaches only NOT_IN_FORCE
    new = stored.body["lands"][0]
    assert ctx.store.ceiling_refusal(stored.envelope_id, t.thread_id, effect=("lands", new)) is None
    assert ctx.store.live_refusal(stored.envelope_id, t.thread_id, authority=ctx.snap, new_pin=True) is None
    assert ctx.store.chain_live_refusal(stored.envelope_id, t.thread_id, authority=ctx.snap) is None
    assert ctx.store.authority_refusal(stored.envelope_id, t.thread_id, authority=ctx.snap) == NOT_IN_FORCE


# 2 -----------------------------------------------------------------------------------------------------------------
def test_revoked_card_has_no_extend(ctx):
    hd, head, t = head_thread(ctx)
    kill(ctx, head)
    hs = hstate(ctx, t)
    (card,) = cards(ctx, t)
    assert card.kind == "REVOKED" and card.actions == ("replace", "close")
    assert "you revoked this envelope: sign a replacement or close" == card.line
    with pytest.raises(CoordError, match="no extend"):
        drafts.renew_rule(ctx.store, t.thread_id, hs, by="operator")
    # the same refusal under the lock, for a state read while the head was still live
    hd2, head2, t2 = head_thread(ctx, "land the other readme change")
    live = hstate(ctx, t2)
    kill(ctx, head2)
    with pytest.raises(CoordError, match="no extend"):
        drafts.renew_rule(ctx.store, t2.thread_id, live, by="operator")
    assert ctx.rows("SELECT COUNT(*) FROM envelope_drafts") == [(0,)]


# 3 -----------------------------------------------------------------------------------------------------------------
def test_replacement_diff_against_killed_is_all_new(ctx):
    hd, head, t = head_thread(ctx)
    kill(ctx, head)
    r = drafts.replace_rule(ctx.store, t.thread_id, hstate(ctx, t), by="operator")
    assert ed.is_complete(r.proposal)                                  # option a: prefilled from the signed body
    _signed, _since, against = built(ctx, r, key="laptop")
    assert against.base_kind == "revoked" and against.reason == "killed" and against.wider
    d = against.diff
    assert d["where"]["added"] == [f"{ctx.path}  README"] and d["where"]["removed"] == []
    assert d["effects"]["lands"]["added"] == [env.effect_identity("lands", e) for e in r.proposal["lands"]]
    assert d["effects"]["lands"]["added"] and d["effects"]["lands"]["removed"] == []
    assert d["stop_at"] == (r.proposal["issued_at"], r.proposal["stop_at"])
    assert d["budget"] == {k: (0, head["budget"][k]) for k in env.BUDGET_KEYS if head["budget"][k]}


# 4 -----------------------------------------------------------------------------------------------------------------
def test_precedence(ctx, tmp_path):
    ha, a, ta = head_thread(ctx, "head A, to be killed")
    hb, b, tb = head_thread(ctx, "head B, key revoked")
    hc, c, tc = head_thread(ctx, "head C, lapsed, then a draft opens over it")
    hd, d, td = head_thread(ctx, "head D, a draft, then a kill")
    he, e, te = head_thread(ctx, "head E, done")
    hf, f, tf = head_thread(ctx, "head F, live")
    fd = fields_for(ctx)
    d1 = drafts.create(ctx.store, td.thread_id, fd, cites_for(hd, fd), by="operator")
    live = facet(ctx, tf)
    assert (live.head.kind, drafts.label(live), live.head.note) == ("live", "LIVE", NOT_IN_FORCE)
    assert facet(ctx, td).draft is not None and drafts.label(facet(ctx, td)) == "ENVELOPE"      # open over a live head
    later(ctx)                                                         # every stop (NOW + 2h) has passed
    kill(ctx, a)                                                       # kill + stop passed -> revoked
    ha_ = hstate(ctx, ta)
    assert (ha_.kind, ha_.reason) == ("revoked", "killed") and drafts.label(facet(ctx, ta)) == "REVOKED"
    revoked = _revoked_phone(tmp_path, ctx.keys)
    hb_ = hstate(ctx, tb, revoked)                                     # the key outranks the passed stop
    assert (hb_.kind, hb_.reason) == ("revoked", "key") and drafts.label(facet(ctx, tb, revoked)) == "REVOKED"
    assert (hstate(ctx, tb).kind, hstate(ctx, tb).reason) == ("lapsed", "stop")      # same head, key not revoked
    fc = fields_for(ctx)                                               # an open draft over a LAPSED head -> ENVELOPE
    drafts.create(ctx.store, tc.thread_id, fc, cites_for(hc, fc), by="operator")
    assert facet(ctx, tc).head.kind == "lapsed" and drafts.label(facet(ctx, tc)) == "ENVELOPE"
    assert [x.kind for x in cards(ctx, tc)] == ["ENVELOPE"]
    kill(ctx, d)                                                       # a lapsed draft over a killed head -> REVOKED
    assert drafts.state_of(ctx.store, d1.draft_id).kind == "lapsed"
    assert facet(ctx, td).draft is None and drafts.label(facet(ctx, td)) == "REVOKED"
    assert [x.kind for x in cards(ctx, td)] == ["REVOKED"]
    ctx.store.set_state(te.thread_id, "done", by="coordinator", token=_lease(ctx.store))
    he_ = hstate(ctx, te)                                              # thread_done is never LAPSED, stop passed too
    assert (he_.kind, he_.reason) == ("ended", "thread_done") and drafts.label(facet(ctx, te)) == "ENDED"
    assert cards(ctx, te) == []


# 5 -----------------------------------------------------------------------------------------------------------------
def test_budget_lapse_is_the_existing_sign(ctx):
    hd, head, t = head_with(ctx, "make the readme say hello", budget={**BUDGET, "tool_calls": 0})
    before = replies(ctx)
    hs = hstate(ctx, t)
    assert (hs.kind, hs.reason, hs.crossed) == ("lapsed", "budget", ("tool_calls",))
    assert hs.ref == f"{t.thread_id}/{head['envelope_id']}" and hs.reply_id is None
    (card,) = cards(ctx, t)
    assert card.kind == "LAPSED" and card.actions == ("extend", "close") and "extend?" in card.line
    assert replies(ctx) == before                                      # reading files nothing
    with ctx.store._write("service") as (conn, now):                   # the SIGN record the spawn claim files
        g = budget.gauge(conn, qdir(ctx), t.thread_id)
        reply_id, created = budget.file_sign(ctx.store, conn, now, g)
    assert created and replies(ctx) == before + 1
    hs = hstate(ctx, t)
    assert hs.ref == ctx.rows(f"SELECT ref_id FROM replies WHERE reply_id = '{reply_id}'")[0][0]
    assert hs.reply_id == reply_id
    (card,) = cards(ctx, t)
    assert card.detail["reply_id"] == reply_id and replies(ctx) == before + 1


# 6 -----------------------------------------------------------------------------------------------------------------
def test_renew_repairs_every_reason(ctx):
    budget0 = {**BUDGET, "tool_calls": 0, "tokens": 0}                  # two crossed keys
    hd, head, t = head_with(ctx, "make the readme say hello", budget=budget0)
    late = ctx.plain("a drop that moves the thread")
    ctx.store.split(late.drop_id, [Part("work", attach=t.thread_id)])   # thread_changed
    later(ctx)                                                         # stop passed
    hs = hstate(ctx, t)
    assert hs.kind == "lapsed" and hs.lapses == ("thread_changed", "stop", "budget")
    assert hs.crossed == ("tool_calls", "tokens")
    r = drafts.renew_rule(ctx.store, t.thread_id, hs, by="operator")
    assert r.proposal["binds"][0]["rev"] == 2 and r.proposal["binds"][0]["thread_id"] == t.thread_id
    assert r.proposal["stop_at"] == utc_stamp(ctx.clk[0] + 2 * HOUR)    # the SIGNED length (2h), from now
    assert r.proposal["budget"] == {"tool_calls": None, "tokens": None, "dollars_cents": 500, "hours": 2}
    assert not ed.is_complete(r.proposal) and drafts.state_of(ctx.store, r.draft_id).complete is False
    assert {c["kind"] for c in r.cites} == {"envelope"} and {c["ref"] for c in r.cites} == {head["envelope_id"]}
    with pytest.raises(DraftStale, match="incomplete"):
        built(ctx, r, key="laptop")
    # nothing the rule did moved the head, and a narrowing still cannot widen the stop
    assert ctx.store.head(t.thread_id).envelope_id == head["envelope_id"]
    with pytest.raises(CoordError, match="widening"):
        ctx.store.attenuate(head["envelope_id"], "stop_earlier", utc_stamp(NOW + 5 * HOUR))
    # he fills the unset keys with a correction; only the signature moves the head
    f = fields_for(ctx)
    r2 = drafts.revise(ctx.store, r.draft_id, f, cites_for(hd, f), parent=r.digest, by="operator")
    signed, _since, _against = built(ctx, r2, key="laptop")
    stored, created = ctx.accept(signed)
    assert created and ctx.store.head(t.thread_id).envelope_id == stored.envelope_id


# 7 -----------------------------------------------------------------------------------------------------------------
def test_rule_targets_the_unclosed_series(ctx):
    hd, head, t = head_thread(ctx)
    f = fields_for(ctx)
    r1 = drafts.create(ctx.store, t.thread_id, f, cites_for(hd, f), by="operator")
    later(ctx)
    assert drafts.state_of(ctx.store, r1.draft_id).kind == "lapsed"
    hs = hstate(ctx, t)
    assert hs.kind == "lapsed"
    r2 = drafts.renew_rule(ctx.store, t.thread_id, hs, by="operator")
    assert (r2.draft_id, r2.revision) == (r1.draft_id, 2) and r2.parent == r1.digest
    assert ctx.rows("SELECT COUNT(DISTINCT draft_id) FROM envelope_drafts") == [(1,)]
    assert drafts.state_of(ctx.store, r2.draft_id).kind == "open"


# 8 -----------------------------------------------------------------------------------------------------------------
def test_head_state_reads_create_nothing(ctx):
    hd, head, t = head_thread(ctx)
    hd2, head2, t2 = head_thread(ctx, "land the other readme change")
    kill(ctx, head2)
    conn = sqlite3.connect(ctx.store.path)
    try:
        conn.execute("DROP TABLE envelope_draft_withdrawals")          # a store from before drafts
        conn.execute("DROP TABLE envelope_drafts")
        conn.commit()
        before = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()
    assert hstate(ctx, t).kind == "live" and facet(ctx, t).draft is None and drafts.label(facet(ctx, t)) == "LIVE"
    assert cards(ctx, t) == []
    assert hstate(ctx, t2).kind == "revoked" and [c.kind for c in cards(ctx, t2)] == ["REVOKED"]
    conn = sqlite3.connect(ctx.store.path)
    try:
        assert conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall() == before
    finally:
        conn.close()
    # a store that never held envelopes at all reads as "no head"
    from levain.coordinator.store import CoordStore

    plain = ctx.tmp / "plain"
    plain.mkdir(mode=0o700)
    cs = CoordStore(plain)
    drop, _ = cs.accept_drop("some plain work", surface="cli")
    (pt,) = cs.split(drop.drop_id, [Part("work", "w")])
    from levain.coordinator.envelopes import EnvelopeStore

    reader = EnvelopeStore(plain)
    assert drafts.head_state(reader, pt.thread_id, authority=ctx.snap, queue_dir=qdir(ctx)).kind == "none"
    assert drafts.cards(reader, pt.thread_id, authority=ctx.snap, queue_dir=qdir(ctx)) == []


# 9 -----------------------------------------------------------------------------------------------------------------
def test_replace_after_key_revocation_signs(ctx, tmp_path):
    hd, head, t = head_thread(ctx)                                     # signed by the phone key
    revoked = _revoked_phone(tmp_path, ctx.keys)
    hs = hstate(ctx, t, revoked)
    assert (hs.kind, hs.reason) == ("revoked", "key")
    (card,) = cards(ctx, t, revoked)
    assert card.kind == "REVOKED" and card.actions == ("replace", "close") and "key that signed this was revoked" in card.line
    r = drafts.replace_rule(ctx.store, t.thread_id, hs, by="operator")
    text, payload, since, against = drafts.draft_sign_bytes(
        ctx.store, r.draft_id, 1, r.digest, authority=revoked, signer_fp=ctx.keys["laptop"].fp, signed_at=ctx.clk[0])
    assert against.base_kind == "revoked" and "no longer holds" in against.reason
    assert against.diff["effects"]["lands"]["added"] and against.diff["where"]["added"]
    sig = ctx.keys["laptop"].sign(env.sign_message(payload), env.SIGN_NAMESPACE)
    stored, created = ctx.store.accept_envelope(text, payload, sig, authority=revoked)
    assert created and stored.signer_fp == ctx.keys["laptop"].fp
    assert ctx.store.head(t.thread_id).envelope_id == stored.envelope_id


# 10 ----------------------------------------------------------------------------------------------------------------
def test_renew_after_member_change_is_wider(ctx):
    hd, head, t = head_thread(ctx)
    late = ctx.plain("one more thing for the same thread")
    ctx.store.split(late.drop_id, [Part("work", attach=t.thread_id)])
    hs = hstate(ctx, t)
    assert (hs.kind, hs.reason) == ("lapsed", "thread_changed")
    (card,) = cards(ctx, t)
    assert card.detail["members_added"] == [{"drop_id": late.drop_id, "words": late.words}]
    r = drafts.renew_rule(ctx.store, t.thread_id, hs, by="operator")
    assert ed.is_complete(r.proposal)
    _signed, _since, against = built(ctx, r, key="laptop")
    assert against.base_kind == "effective"
    assert against.members_added == [late.drop_id] and against.members_removed == []
    assert against.wider                                               # the authority now covers different work
    d = against.diff
    assert d["where"] == {"added": [], "removed": []} and d["budget"] == {} and d["stop_at"] is None
    assert all(not v for kinds in d["effects"].values() for v in kinds.values())


# 11 ----------------------------------------------------------------------------------------------------------------
def test_ended_thread_holds_the_slot_and_reopen_rebases(ctx):
    hd, head, t = head_thread(ctx)
    f = fields_for(ctx)
    r1 = drafts.create(ctx.store, t.thread_id, f, cites_for(hd, f), by="operator")
    ctx.store.set_state(t.thread_id, "done", by="coordinator", token=_lease(ctx.store))
    with pytest.raises(CoordError):                                    # a second series: the slot is held
        drafts.create(ctx.store, t.thread_id, f, cites_for(hd, f), by="operator")
    with pytest.raises(CoordError, match="born open"):
        drafts.revise(ctx.store, r1.draft_id, f, cites_for(hd, f), parent=r1.digest, by="operator")
    assert hstate(ctx, t).kind == "ended" and facet(ctx, t).draft is None
    assert cards(ctx, t) == []                                         # no card, so no sign
    ctx.store.set_state(t.thread_id, "open", by="operator")
    (card,) = cards(ctx, t)
    assert card.kind == "REVOKED" and card.actions == ("replace", "close") and "reopened" in card.line
    hs = hstate(ctx, t)
    assert (hs.kind, hs.reason) == ("ended", "thread_done")
    r2 = drafts.replace_rule(ctx.store, t.thread_id, hs, by="operator")
    assert (r2.draft_id, r2.revision) == (r1.draft_id, 2)
    signed, _since, against = built(ctx, r2, key="laptop")
    assert against.base_kind == "revoked" and against.reason == "its thread ended"
    assert against.diff["effects"]["lands"]["added"] and against.diff["stop_at"][0] == r2.proposal["issued_at"]
    stored, created = ctx.accept(signed)
    assert created and ctx.store.head(t.thread_id).envelope_id == stored.envelope_id


# 12 ----------------------------------------------------------------------------------------------------------------
def test_rule_reads_no_disk_under_the_lock(ctx, monkeypatch, spies):
    hd, head, t = head_thread(ctx)
    calls = {"gauge": 0, "q13": 0}
    real_gauge, real_q13 = budget.gauge, budget._q13

    def gauge(*a, **k):
        calls["gauge"] += 1
        return real_gauge(*a, **k)

    def q13(*a, **k):
        calls["q13"] += 1
        return real_q13(*a, **k)

    monkeypatch.setattr(budget, "gauge", gauge)
    monkeypatch.setattr(budget, "_q13", q13)
    hs = hstate(ctx, t)                                                # the read model reads the gauge, once
    assert calls == {"gauge": 1, "q13": 1} and hs.kind == "live" and hs.crossed == ()
    calls.update(gauge=0, q13=0)
    spies.update(refusal=0)
    given = drafts.HeadState(hs.kind, hs.reason, hs.envelope_id, hs.since, hs.ref, hs.lapses, ("hours",))
    r = drafts.renew_rule(ctx.store, t.thread_id, given, by="operator")
    assert calls == {"gauge": 0, "q13": 0} and spies["refusal"] == 0   # no gauge, no file, no verifier under the lock
    assert r.proposal["budget"]["hours"] is None and head["budget"]["hours"] == 2   # the crossed set is the ARGUMENT


# 13 ----------------------------------------------------------------------------------------------------------------
def test_card_claims_no_author(ctx):
    d, t = ctx.plain_thread("make the readme say hello")
    f = fields_for(ctx)
    r = drafts.create(ctx.store, t.thread_id, f, cites_for(d, f), by="operator")
    assert r.by == "operator"
    (card,) = cards(ctx, t)
    assert card.kind == "ENVELOPE" and card.actions == ("sign", "withdraw")
    text = json.dumps(card.as_dict(), default=str).lower()
    assert "your words" not in text and "operator" not in text and "written by" not in text
    assert '"by"' not in text
    cite = card.detail["fields"][0]["cites"][0]
    assert cite["line"] == "in this thread" and "as reported by the sender" in cite["surface"]
    assert cite["words"] == d.words and "claim of relevance" in card.detail["cite_note"]
    assert card.detail["against"]["base_kind"] == "none" and card.detail["anchor"]["drop_id"] == d.drop_id
