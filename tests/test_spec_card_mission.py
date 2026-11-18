"""H1-5 lane 2: the tap on a MISSION envelope card. A mission card names several work threads that hold one drop; his
tap is the one act that creates the mission (the room and the door never do), opens the mission's draft series over every
child, files the drop and writes the spec revision, all in ONE transaction. Nothing here signs: the draft is signed
afterward, once, through lane 1's flow. Each test names the failure it reproduces; where a failure could be reproduced on
the base tip it was (the base has no mission card, so a thread card's tap on the same set is the only base behaviour
and is pinned unchanged here)."""
from __future__ import annotations

import json
import sqlite3

import pytest

from levain.desk import envelope_drafts as drafts
from levain.desk import missions as ms
from levain.desk import room, room_mcp, spec_card
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import CoordError, Part, Source, words_sha256
from tests.test_desk_envelope import BUDGET, keys, repo, snapshot  # noqa: F401  (fixtures)
from tests.test_desk_envelope_draft import built, cites_for, ctx, fields_for  # noqa: F401  (fixtures)
from tests.test_spec_card import Boom, card, plain_work, spec_text  # noqa: F401  (fixture)
from tests.test_spec_card import counts as thread_counts
from tests.test_spec_card import propose as thread_propose
from tests.test_spec_card import tap as thread_tap

_TABLES = ("drops", "replies", "threads", "thread_revisions", "proposal_events", "drop_notes", "envelope_drafts",
           "spec_card_taps", "envelopes", "envelope_binds", "missions", "mission_revisions", "mission_drafts",
           "spec_proposals", "mission_spec_proposals")


def counts(store):
    c = sqlite3.connect(store.path)
    try:
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in _TABLES}
    finally:
        c.close()


def family(ctx, n=3, words="land the readme change in parts"):
    """One drop split into ``n`` work threads that each hold it, in no mission. -> (drop, threads sorted by id)"""
    d = ctx.plain(words)
    ts = ctx.store.split(d.drop_id, [Part("work", f"part {i}") for i in range(n)])
    return d, sorted(ts, key=lambda t: t.thread_id)


def propose(card, thread_ids, text, summary="one signature for the parts"):
    from tests.test_desk_room import say
    card.n[0] += 1
    mid = say(card.cs, card.room, "give them two hours", n=card.n[0] + 100)
    with card.store._write() as (conn, now):
        return room.propose_spec(conn, card.launch, [mid], None, text, summary, threads=thread_ids)


def tap(card, p, words, *, outbox="tap-0001-aaaa"):
    c = card.store._connect()
    try:
        anchor = room.spec_of(c, p.proposal_id).thread_id
    finally:
        c.close()
    return spec_card.tap(card.store, p.proposal_id, words, outbox, "cockpit", thread_echo=anchor,
                         input_sha256=words_sha256(words))


def rows(card, sql, *args):
    c = sqlite3.connect(card.store.path)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


# --- the card ----------------------------------------------------------------------------------------------------------
def test_a_mission_card_is_information_and_the_room_makes_no_mission(card):
    """Q1: his tap creates the mission, the room never does. Failure: a card (or the door) that groups the threads."""
    _d, ts = family(card.ctx)
    before = counts(card.store)
    p = propose(card, [t.thread_id for t in reversed(ts)], spec_text(card.ctx))
    after = counts(card.store)
    assert {k: after[k] - before[k] for k in after} == {
        **{k: 0 for k in after}, "spec_proposals": 1, "mission_spec_proposals": 1}
    c = card.store._connect()
    try:
        spec = room.spec_of(c, p.proposal_id)
        assert room.mission_threads(c, p.proposal_id) == tuple(t.thread_id for t in ts)      # sorted, whatever the order given
    finally:
        c.close()
    assert spec.thread_id == ts[0].thread_id and p.about == ts[0].thread_id                 # the anchor is the first sorted


@pytest.mark.parametrize("make,match", [
    (lambda ts: dict(thread=ts[0], threads=ts), "not both and not neither"),
    (lambda ts: dict(thread=None, threads=None), "not both and not neither"),
    (lambda ts: dict(thread=None, threads=[ts[0]]), "2 to"),
    (lambda ts: dict(thread=None, threads=[ts[0], ts[0]]), "each thread once"),
    (lambda ts: dict(thread=None, threads=[ts[0], "th-" + "0" * 32]), "names no thread"),
])
def test_propose_spec_refuses_a_malformed_thread_set_and_writes_nothing(card, make, match):
    _d, ts = family(card.ctx)
    kw = make([t.thread_id for t in ts])
    from tests.test_desk_room import say
    mid = say(card.cs, card.room, "give them two hours", n=301)
    before = counts(card.store)
    with pytest.raises(CoordError, match=match):
        with card.store._write() as (conn, now):
            room.propose_spec(conn, card.launch, [mid], kw["thread"], spec_text(card.ctx), "s", threads=kw["threads"])
    assert counts(card.store) == before


def test_the_mission_spec_row_is_append_only(card):
    _d, ts = family(card.ctx)
    p = propose(card, [t.thread_id for t in ts], spec_text(card.ctx))
    c = sqlite3.connect(card.store.path)
    for sql in ("UPDATE mission_spec_proposals SET threads_sha256 = 'x'", "DELETE FROM mission_spec_proposals"):
        with pytest.raises(sqlite3.IntegrityError):
            c.execute(sql)
    c.close()


# --- the tap -----------------------------------------------------------------------------------------------------------
def test_the_tap_writes_mission_series_drop_revision_and_links_and_no_envelope(card):
    """One tap: the mission over the card's threads, a 3-bind series (opener, then the spec), the drop, the receipt and
    both links; nothing signed."""
    ctx = card.ctx
    d, ts = family(ctx)
    ids = [t.thread_id for t in ts]
    words = spec_text(ctx)
    p = propose(card, ids, words)
    before = counts(card.store)
    r = tap(card, p, words)
    after = counts(card.store)
    assert r.created and r.mission_id and r.thread_id == ids[0] and r.revision == 2
    assert {k: after[k] - before[k] for k in after} == {
        **{k: 0 for k in after}, "missions": 1, "mission_revisions": 1, "mission_drafts": 1, "drops": 1,
        "envelope_drafts": 2, "spec_card_taps": 1, "proposal_events": 1, "drop_notes": 1, "replies": 2}
    assert after["envelopes"] == after["envelope_binds"] == 0 == before["envelopes"]
    m = ms.get_mission(card.store, r.mission_id)
    assert sorted(m.children) == ids
    dr = drafts.latest(card.store, r.draft_id)
    assert [b["thread_id"] for b in dr.proposal["binds"]] == ids
    assert rows(card, "SELECT mission_id FROM mission_drafts WHERE draft_id = ?", r.draft_id) == [(r.mission_id,)]
    assert [c["ref"] for c in dr.cites if c["field"] == "budget"] == [r.drop.drop_id]
    for t in ids:                                                       # the tap attached nothing to a child
        assert r.drop.drop_id not in card.store.thread(t).members


def test_the_tapped_mission_draft_signs_once_into_one_bind_per_child(card):
    """The tap's draft is lane 1's draft: one signature, one binds row per child."""
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    r = tap(card, propose(card, [t.thread_id for t in ts], words), words)
    signed, _since, _against = built(ctx, drafts.latest(card.store, r.draft_id))
    stored, created = ctx.accept(signed)
    assert created and [b["thread_id"] for b in stored.body["binds"]] == [t.thread_id for t in ts]
    assert counts(card.store)["envelopes"] == 1 and counts(card.store)["envelope_binds"] == len(ts)


@pytest.mark.parametrize("stage", ["mission", "opener", "commit_rows", "spec_revision", "note", "link"])
def test_a_fault_at_any_step_of_a_mission_tap_leaves_zero_rows(card, monkeypatch, stage):
    """Fault-inject each step (the mission row, the series opener, the drop, the revision, the note, the link): every
    table is what it was and the card is still open. Failure: a mission or series left behind by a tap that failed."""
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    before = counts(card.store)
    real_rev, real_create = drafts._write_mission_revision, ms._create
    calls = {"rev": 0}

    def revision(*a, **k):
        calls["rev"] += 1
        if (stage, calls["rev"]) in (("opener", 1), ("spec_revision", 2)):
            raise Boom(f"injected at {stage}")
        return real_rev(*a, **k)

    def create(*a, **k):
        made = real_create(*a, **k)                 # the mission row IS written, then the step after it fails
        if stage == "mission":
            raise Boom("injected after the mission")
        return made

    monkeypatch.setattr(drafts, "_write_mission_revision", revision)
    monkeypatch.setattr(ms, "_create", create)
    if stage == "commit_rows":
        monkeypatch.setattr(room, "_commit_rows", lambda *a, **k: (_ for _ in ()).throw(Boom("injected")))
    if stage == "note":
        monkeypatch.setattr(EnvelopeStore, "note_in", lambda *a, **k: (_ for _ in ()).throw(Boom("injected")))
    if stage == "link":
        monkeypatch.setattr(spec_card, "_link", lambda *a, **k: (_ for _ in ()).throw(Boom("injected")))
    with pytest.raises(Boom):
        tap(card, p, words)
    assert counts(card.store) == before
    assert room.get_proposal(card.store._connect(), p.proposal_id).state == "open"


def test_a_two_device_double_tap_writes_once(card):
    """A second device's tap (another outbox) is refused settled and writes nothing; a resend of the first answers the
    stored tap with the same mission. Failure: a second mission, drop or revision."""
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    first = tap(card, p, words)
    after = counts(card.store)
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, words, outbox="tap-0002-bbbb")
    assert e.value.code == "settled"
    again = tap(card, p, words)
    assert not again.created and (again.drop.drop_id, again.draft_id, again.revision, again.mission_id) == (
        first.drop.drop_id, first.draft_id, first.revision, first.mission_id)
    assert counts(card.store) == after and after["missions"] == 1


def _refused_tap(card, p, words, match):
    before = counts(card.store)
    with pytest.raises(CoordError, match=match):
        tap(card, p, words)
    assert counts(card.store) == before                         # no mission, series, drop or link either
    assert room.get_proposal(card.store._connect(), p.proposal_id).state == "open"


def test_a_tap_is_refused_when_a_child_opened_its_own_draft_after_the_card(card):
    """The set was clean when the card was made; a child then opened a draft of its own. The tap refuses in plain words
    and the mission it had already created is taken back with the rest."""
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    own = drafts.drop_rule(ctx.store, ts[2].thread_id, by="operator")
    _refused_tap(card, p, words, f"thread {ts[2].thread_id} is already bound by draft {own.draft_id}")


def test_a_tap_is_refused_when_a_child_gained_a_signed_head_after_the_card(card):
    ctx = card.ctx
    d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    f = fields_for(ctx)
    own = drafts.create(ctx.store, ts[1].thread_id, f, cites_for(d, f), by="operator")
    signed, _since, _against = built(ctx, own)
    stored, _ = ctx.accept(signed)
    _refused_tap(card, p, words, f"thread {ts[1].thread_id} already has envelope {stored.envelope_id}")


def test_a_tap_is_refused_when_a_child_joined_another_mission_after_the_card(card):
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    ms.create_mission(ctx.store, [ts[1].thread_id])
    _refused_tap(card, p, words, f"thread {ts[1].thread_id} is already in mission")


def test_a_tap_is_refused_when_a_child_closed_after_the_card(card):
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    ctx.store.set_state(ts[1].thread_id, "closed", by="operator")
    before = counts(card.store)
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, words)
    assert e.value.code == "thread_closed" and counts(card.store) == before


def test_a_tap_with_the_thread_echo_of_another_thread_is_refused(card):
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    p = propose(card, [t.thread_id for t in ts], words)
    before = counts(card.store)
    with pytest.raises(spec_card.TapRefused) as e:
        spec_card.tap(card.store, p.proposal_id, words, "tap-0001-aaaa", "cockpit", thread_echo=ts[1].thread_id,
                      input_sha256=words_sha256(words))
    assert e.value.code == "thread_mismatch" and counts(card.store) == before


# --- the door (a store the door reads through the environment) --------------------------------------------------------
@pytest.fixture
def door(card, monkeypatch):
    monkeypatch.setattr("levain.consent.queue.default_queue_dir", lambda: card.store.directory)
    from tests.test_desk_room import say
    mid = say(card.cs, card.room, "give them two hours", n=900)

    def call(**over):
        a = {"quote_ids": [mid], "spec": json.loads(spec_text(card.ctx)), "summary": "s", **over}
        return room_mcp.handle(card.launch, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "propose_envelope", "arguments": a}})
    return call


def _why(reply):
    return reply["error"]["message"] if "error" in reply else reply["result"]["content"][0]["text"]


def _refused(reply):
    return "error" in reply or reply["result"]["isError"] is True


def test_the_door_makes_a_mission_card_and_no_mission(card, door):
    _d, ts = family(card.ctx)
    before = counts(card.store)
    reply = door(threads=[t.thread_id for t in ts])
    assert not _refused(reply), reply
    assert "waiting for your tap" in _why(reply) and "nothing is grouped" in _why(reply)
    after = counts(card.store)
    assert {k: after[k] - before[k] for k in after} == {
        **{k: 0 for k in after}, "spec_proposals": 1, "mission_spec_proposals": 1}
    assert counts(card.store)["missions"] == 0 and counts(card.store)["drops"] == before["drops"]


def test_the_door_refuses_threads_that_share_no_drop(card, door):
    _d1, a = plain_work(card.ctx, "one thing")
    _d2, b = plain_work(card.ctx, "another thing")
    before = counts(card.store)
    reply = door(threads=[a.thread_id, b.thread_id])
    assert _refused(reply) and "share no drop" in _why(reply), _why(reply)
    assert counts(card.store) == before


def test_the_door_refuses_a_child_that_has_a_head(card, door):
    ctx = card.ctx
    d, ts = family(ctx)
    f = fields_for(ctx)
    own = drafts.create(ctx.store, ts[0].thread_id, f, cites_for(d, f), by="operator")
    signed, _since, _against = built(ctx, own)
    stored, _ = ctx.accept(signed)
    before = counts(card.store)
    reply = door(threads=[t.thread_id for t in ts])
    assert _refused(reply) and f"already has envelope {stored.envelope_id}" in _why(reply), _why(reply)
    assert counts(card.store) == before


def test_the_door_refuses_a_child_that_holds_its_own_unclosed_series_in_plain_words(card, door):
    ctx = card.ctx
    _d, ts = family(ctx)
    own = drafts.drop_rule(ctx.store, ts[1].thread_id, by="operator")
    before = counts(card.store)
    reply = door(threads=[t.thread_id for t in ts])
    assert _refused(reply), reply
    assert f"thread {ts[1].thread_id} already holds its own unclosed draft ({own.draft_id})" in _why(reply)
    assert "sign or withdraw that draft first" in _why(reply)
    assert counts(card.store) == before


@pytest.mark.parametrize("over,said", [
    ({"launches": []}, "launch grant"),
    ({"spec": {"launches": [], "stop": "2h"}}, "launch grant"),
])
def test_the_door_refuses_a_launches_key_on_a_mission_card(card, door, over, said):
    _d, ts = family(card.ctx)
    before = counts(card.store)
    reply = door(threads=[t.thread_id for t in ts], **over)
    assert _refused(reply) and said in _why(reply)
    assert counts(card.store) == before


def test_the_door_takes_one_of_thread_or_threads_and_a_real_set(card, door):
    _d, ts = family(card.ctx)
    ids = [t.thread_id for t in ts]
    before = counts(card.store)
    for over in ({"thread": ids[0], "threads": ids}, {}, {"threads": ids[:1]}, {"threads": [ids[0], ids[0]]},
                 {"threads": "th-x"}, {"threads": [*ids, 7]}):
        reply = door(**over)
        assert _refused(reply), over
    assert counts(card.store) == before


# --- the plain-words reading --------------------------------------------------------------------------------------------
def test_the_reading_of_a_mission_card_names_every_child_the_budget_and_what_is_not_covered(card):
    ctx = card.ctx
    _d, ts = family(ctx)
    words = spec_text(ctx)
    got = spec_card.preview(card.store, ts[0].thread_id, words, threads=[t.thread_id for t in ts])
    text = "\n".join(line["text"] for line in got["lines"])
    for t in ts:
        assert t.title in text and t.thread_id in text
    assert f"one signature covers these {len(ts)} threads" in text
    each = ", ".join(f"{k} {BUDGET[k]}" for k in ("tool_calls", "tokens", "dollars_cents", "hours") if k in BUDGET)
    assert f"each up to {each}, up to {len(ts)} x ({each}) together" in text
    assert "NOT covered" in text and "attached to this mission later" in text
    assert got["signed_head"] is False and got["input_sha256"] == words_sha256(words)


def test_a_thread_card_reads_and_taps_as_it_did(card):
    """A thread card is unchanged: its preview is exactly the spec's own lines under the card's keys, its tap makes no
    mission, no mission link and no mission row, and its result carries no mission."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    got = spec_card.preview(card.store, t.thread_id, words)
    fields, _named, span = drafts.parse_spec(words, Source(), card.store)
    assert got == {"input_sha256": words_sha256(words), "lines": drafts.render_spec(fields, span, None),
                   "signed_head": False}
    assert all(line["field"] not in ("mission", "budget_together", "mission_later") for line in got["lines"])
    p = thread_propose(card, t.thread_id, words)
    before = counts(card.store)
    r = thread_tap(card, p, words)
    after = counts(card.store)
    assert r.mission_id is None and r.revision == 2 and r.thread_id == t.thread_id
    assert (after["missions"], after["mission_drafts"], after["mission_spec_proposals"]) == (0, 0, 0)
    assert after["envelope_drafts"] - before["envelope_drafts"] == 2 and after["drops"] - before["drops"] == 1
    assert room.mission_threads(card.store._connect(), p.proposal_id) is None
    assert set(thread_counts(card.store)) <= set(after)
