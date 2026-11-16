"""H1-4 shape A, step 2: the store half of a room envelope card, with no door and no page. A card is a room proposal of a
second kind (``room.propose_spec``); his tap (``spec_card.tap``) is ONE transaction: the drop with the bytes he posted,
the proposal's ``committed`` event and room note, a ``noted`` receipt so the desk never files it, a draft revision (a
series opener first when the thread has none) and a link row. Each test names the failure it reproduces."""
from __future__ import annotations

import json
import sqlite3
from types import SimpleNamespace

import pytest

from levain.desk import envelope_drafts as drafts
from levain.desk import room, spec_card
from levain.desk.chat import ChatStore
from levain.desk.envelopes import EnvelopeStore
from levain.desk.store import CoordError, CoordStore, DropConflict, Part, words_sha256
from tests.test_desk_envelope import BUDGET, keys, repo, snapshot  # noqa: F401  (fixtures)
from tests.test_desk_envelope_draft import built, ctx, head_thread  # noqa: F401  (fixtures)
from tests.test_desk_room import make_launch, make_thread, raw, say

# every table a tap may touch: after a refused tap each count is what it was
_TABLES = ("drops", "replies", "threads", "thread_revisions", "proposal_events", "drop_notes", "envelope_drafts",
           "spec_card_taps", "envelopes")


@pytest.fixture
def card(ctx):
    store = ctx.store
    room_thread = make_thread(store)
    c = raw(store.directory)
    room.ensure(c)
    room.open_room_thread(c, room_thread)
    c.close()
    cs = ChatStore(store)
    cs._connect().close()
    launch = make_launch(store, store.directory, room_thread)
    # make_launch drops the launches_frozen trigger to make a room launch: bring the schema current again, so the
    # read-only doors (drafts.latest, cite_label) read this store as they read any other
    store._ready = False
    store._connect().close()
    return SimpleNamespace(ctx=ctx, store=store, cs=cs, room=room_thread, n=[0], launch=launch)


def spec_text(ctx, **over):
    s = {"where": [{"repo": ctx.path, "paths": ["README"]}], "lands": [{"repo": ctx.path, "onto": "main"}],
         "pushes": [], "publishes": [], "budget": dict(BUDGET), "stop": "2h"}
    s.update(over)
    return json.dumps(s)


def propose(card, thread_id, text, summary="two hours, README only"):
    card.n[0] += 1
    mid = say(card.cs, card.room, "give it two hours", n=card.n[0] + 100)
    with card.store._write() as (conn, now):
        return room.propose_spec(conn, card.launch, [mid], thread_id, text, summary)


def tap(card, p, words, *, thread=None, sha=None, outbox="tap-0001-aaaa", surface="cockpit"):
    return spec_card.tap(card.store, p.proposal_id, words, outbox, surface,
                         thread_echo=thread or room_spec(card, p).thread_id,
                         input_sha256=sha or words_sha256(words))


def room_spec(card, p):
    c = card.store._connect()
    try:
        return room.spec_of(c, p.proposal_id)
    finally:
        c.close()


def counts(store):
    c = sqlite3.connect(store.path)
    try:
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0] for t in _TABLES}
    finally:
        c.close()


def plain_work(ctx, words="fix the readme"):
    return ctx.plain_thread(words)


# --- propose_spec: information only --------------------------------------------------------------------------------
def test_propose_spec_writes_a_proposal_and_a_spec_row_and_no_drop(card):
    """A card is information: a proposal row and its spec row, never a drop or a draft (a door that commits)."""
    _d, t = plain_work(card.ctx)
    before = counts(card.store)
    p = propose(card, t.thread_id, spec_text(card.ctx))
    after = counts(card.store)
    assert p.state == "open" and p.about == t.thread_id
    assert {k: after[k] - before[k] for k in after} == {k: 0 for k in after}
    s = room_spec(card, p)
    assert (s.thread_id, s.spec_text) == (t.thread_id, spec_text(card.ctx)) and s.spec_sha256 == words_sha256(s.spec_text)


@pytest.mark.parametrize("bad,match", [
    ("th-" + "0" * 32, "names no thread"),
    ("not-a-thread", "names no thread"),
])
def test_propose_spec_refuses_a_thread_the_store_lacks(card, bad, match):
    with pytest.raises(CoordError, match=match):
        propose(card, bad, spec_text(card.ctx))


def test_propose_spec_refuses_a_non_work_or_closed_thread(card):
    with pytest.raises(CoordError, match="open work thread"):
        propose(card, card.room, spec_text(card.ctx))               # the room's own note thread


def test_propose_spec_refuses_text_over_the_cap(card):
    _d, t = plain_work(card.ctx)
    with pytest.raises(CoordError, match="characters"):
        propose(card, t.thread_id, "x" * 100_000)


def test_spec_tables_are_append_only(card):
    _d, t = plain_work(card.ctx)
    p = propose(card, t.thread_id, spec_text(card.ctx))
    c = raw(card.store.directory)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("UPDATE spec_proposals SET spec_text = 'x' WHERE proposal_id = ?", (p.proposal_id,))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("DELETE FROM spec_proposals WHERE proposal_id = ?", (p.proposal_id,))
    c.close()


# --- the tap -------------------------------------------------------------------------------------------------------
def test_tap_writes_posted_bytes_not_stored_spec(card):
    """Stored A, posted B: the drop, the fields and the link are B's. Failure: the proposal row feeding the draft."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    a, b = spec_text(ctx), spec_text(ctx, budget={**BUDGET, "hours": 1}, stop="1h")
    p = propose(card, t.thread_id, a)
    r = tap(card, p, b)
    assert r.created and r.drop.words == b and r.drop.words_sha256 == words_sha256(b)
    d = drafts.latest(card.store, r.draft_id)
    assert d.proposal["budget"]["hours"] == 1 and d.revision == r.revision
    assert [c["ref"] for c in d.cites if c["field"] == "budget"] == [r.drop.drop_id]
    c = sqlite3.connect(card.store.path)
    row = c.execute("SELECT words_sha256, drop_id, draft_id, revision, thread_id FROM spec_card_taps WHERE proposal_id = ?",
                    (p.proposal_id,)).fetchone()
    assert row == (words_sha256(b), r.drop.drop_id, r.draft_id, r.revision, t.thread_id)


def test_tap_refuses_thread_mismatch_and_stale_preview_and_writes_nothing(card):
    """A tap adopting a target or bytes he did not see: refused with a named code, nothing written, card still open."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    _d2, other = plain_work(ctx, "another thing")
    words = spec_text(ctx)
    p = propose(card, t.thread_id, words)
    before = counts(card.store)
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, words, thread=other.thread_id)
    assert e.value.code == "thread_mismatch"
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, words, sha=words_sha256(words + " "))
    assert e.value.code == "stale_preview"
    assert counts(card.store) == before
    assert room.get_proposal(card.store._connect(), p.proposal_id).state == "open"


@pytest.mark.parametrize("words,match", [
    ("make it say hello", "not an envelope spec"),
    ('{"launches": []}', "never carries a launch grant"),
    ('{"budget": {}, "budget": {}}', "repeats the key"),
])
def test_tap_refuses_bytes_the_grammar_refuses(card, words, match):
    _d, t = plain_work(card.ctx)
    p = propose(card, t.thread_id, spec_text(card.ctx))
    before = counts(card.store)
    with pytest.raises(CoordError, match=match):
        tap(card, p, words)
    assert counts(card.store) == before


def test_tap_on_thread_without_series_opens_one_and_cites_without_attach(card):
    """Reproduces M2 (the cite refusal without a series): revision 2, the drop not a member, the thread rev unchanged."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    p = propose(card, t.thread_id, spec_text(ctx))
    r = tap(card, p, spec_text(ctx))
    assert r.revision == 2
    t2 = card.store.thread(t.thread_id)
    assert r.drop.drop_id not in t2.members and t2.rev == t.rev
    c = sqlite3.connect(card.store.path)
    assert [x[0] for x in c.execute("SELECT revision FROM envelope_drafts WHERE draft_id = ? ORDER BY revision",
                                    (r.draft_id,))] == [1, 2]            # one series: the opener, then the spec
    c.close()


def test_tap_on_an_existing_series_adds_the_next_revision(card):
    ctx = card.ctx
    _d, t = plain_work(ctx)
    opener = drafts.drop_rule(ctx.store, t.thread_id, by="operator")
    p = propose(card, t.thread_id, spec_text(ctx))
    r = tap(card, p, spec_text(ctx))
    assert (r.draft_id, r.revision) == (opener.draft_id, opener.revision + 1)


def test_tap_on_signed_head_leaves_grant_live_and_the_new_draft_signs(card):
    """Reproduces M1 (an attach lapsing a signed grant): live_refusal is None after the tap; the draft signs."""
    ctx = card.ctx
    _d, b, t = head_thread(ctx)
    p = propose(card, t.thread_id, spec_text(ctx))
    r = tap(card, p, spec_text(ctx))
    assert ctx.live(b["envelope_id"], t) is None
    signed, _since, _against = built(ctx, drafts.latest(card.store, r.draft_id))
    stored, created = ctx.accept(signed)
    assert created and stored.body["supersedes"]["envelope_id"] == b["envelope_id"]


def test_the_tap_never_signs(card):
    """No path from the tap to accept_envelope: the envelopes table is exactly what it was."""
    ctx = card.ctx
    _d, b, t = head_thread(ctx)
    p = propose(card, t.thread_id, spec_text(ctx))
    before = counts(card.store)["envelopes"]
    tap(card, p, spec_text(ctx))
    assert counts(card.store)["envelopes"] == before == 1


# 9: one transaction -------------------------------------------------------------------------------------------------
class Boom(CoordError):
    pass


@pytest.mark.parametrize("stage", ["opener", "commit_rows", "spec_revision", "note", "link"])
def test_tap_is_one_transaction(card, monkeypatch, stage):
    """Fault-inject each write after the first: zero new rows anywhere, the card still open. Failure: partial state
    (and a retry would write revision N+1, M4)."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    p = propose(card, t.thread_id, spec_text(ctx))
    before = counts(card.store)
    real_rev = drafts._write_revision
    calls = {"rev": 0}

    def revision(*a, **k):
        calls["rev"] += 1
        if (stage, calls["rev"]) in (("opener", 1), ("spec_revision", 2)):
            raise Boom(f"injected at {stage}")
        return real_rev(*a, **k)

    monkeypatch.setattr(drafts, "_write_revision", revision)
    if stage == "commit_rows":
        monkeypatch.setattr(room, "_commit_rows", lambda *a, **k: (_ for _ in ()).throw(Boom("injected")))
    if stage == "note":
        monkeypatch.setattr(EnvelopeStore, "note_in", lambda *a, **k: (_ for _ in ()).throw(Boom("injected")))
    if stage == "link":
        monkeypatch.setattr(spec_card, "_link", lambda *a, **k: (_ for _ in ()).throw(Boom("injected")))
    with pytest.raises(Boom):
        tap(card, p, spec_text(ctx))
    assert counts(card.store) == before
    assert room.get_proposal(card.store._connect(), p.proposal_id).state == "open"


# 10: exactly once ---------------------------------------------------------------------------------------------------
def test_tap_exactly_once_across_two_devices(card):
    """A second commit with another outbox is refused 'settled' and writes nothing; a resend with the same outbox
    answers the stored link. Failure: a second drop or revision."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    p = propose(card, t.thread_id, words)
    first = tap(card, p, words)
    after = counts(card.store)
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, words, outbox="tap-0002-bbbb")
    assert e.value.code == "settled"
    again = tap(card, p, words)
    assert not again.created and (again.drop.drop_id, again.draft_id, again.revision) == (
        first.drop.drop_id, first.draft_id, first.revision)
    assert counts(card.store) == after


def test_an_outbox_that_is_another_drops_is_a_conflict(card):
    ctx = card.ctx
    _d, t = plain_work(ctx)
    p = propose(card, t.thread_id, spec_text(ctx))
    other, _ = card.store.accept_drop("an unrelated drop", surface="cockpit", outbox_id="tap-0001-aaaa")
    with pytest.raises(DropConflict):
        tap(card, p, spec_text(ctx))


# 11-12: the desk and the worker never see the spec drop --------------------------------------------------------------
def test_spec_drop_is_settled_for_the_desk(card):
    """After the tap the drop has a 'noted' receipt, and the desk's unseen query does not offer it (codex HIGH 4)."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    p = propose(card, t.thread_id, words)
    r = tap(card, p, words)
    c = sqlite3.connect(card.store.path)
    stages = [json.loads(b)["stage"] for (b,) in c.execute(
        "SELECT body FROM replies WHERE kind = 'drop_receipt' AND ref_id = ? ORDER BY rowid", (r.drop.drop_id,))]
    assert stages == ["delivered", "noted"]
    why = json.loads(c.execute("SELECT body FROM replies WHERE ref_id = ? AND body LIKE '%noted%'",
                               (r.drop.drop_id,)).fetchone()[0])["why"]
    assert p.proposal_id in why and r.draft_id in why and f"#{r.revision}" in why
    seen = c.execute("SELECT 1 FROM replies s WHERE s.kind = 'drop_receipt' AND s.ref_id = ? AND "
                     "json_extract(s.body, '$.stage') IN ('split', 'noted')", (r.drop.drop_id,)).fetchone()
    assert seen is not None
    c.close()


def test_spec_drop_is_in_no_thread_so_no_worker_reads_it(card):
    """instantiate hands a worker every member drop; the tap attaches nothing, so the spec JSON is not one (F1)."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    r = tap(card, propose(card, t.thread_id, words), words)
    for th in card.store.threads():
        assert r.drop.drop_id not in th.members


def test_a_tap_asks_no_desk_to_wake(card):
    """The drop is noted in its own transaction, so a wake that fires first launches a desk to find nothing (measured:
    an ordinary drop writes a pending desk launch; the tap's drop writes none)."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    p = propose(card, t.thread_id, words)
    card.store.coord_release()
    card.store.coord_set_agent("claude", cwd=str(ctx.tmp))

    def pending():
        c = sqlite3.connect(card.store.path)
        try:
            return c.execute("SELECT count(*) FROM launches WHERE role = 'desk' OR role = 'coordinator'").fetchone()[0]
        finally:
            c.close()

    base = pending()
    tap(card, p, words)
    assert pending() == base                           # the tap's drop asks for no desk
    ctx.store.accept_drop("an ordinary drop", surface="cli")
    assert pending() == base + 1                       # while an ordinary drop does (so the count can move)


# 13: replaced cards -------------------------------------------------------------------------------------------------
def test_a_newer_card_for_the_same_thread_replaces_the_older(card):
    """A stale card would write revision N+1 over a newer one (_rule_write always revises the unclosed series)."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    _d2, other = plain_work(ctx, "another thing")
    old = propose(card, t.thread_id, spec_text(ctx))
    unrelated = propose(card, other.thread_id, spec_text(ctx))
    new = propose(card, t.thread_id, spec_text(ctx, stop="1h"))
    conn = card.store._connect()
    assert room.replaced_by(conn, old.proposal_id) == new.proposal_id
    assert room.replaced_by(conn, new.proposal_id) is None and room.replaced_by(conn, unrelated.proposal_id) is None
    conn.close()
    before = counts(card.store)
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, old, spec_text(ctx))
    assert e.value.code == "replaced" and counts(card.store) == before
    assert tap(card, new, spec_text(ctx, stop="1h")).created


def test_tap_refuses_a_plain_proposal(card):
    """A drop proposal (no spec row) is the old commit's: the spec writer never turns it into a draft."""
    ctx = card.ctx
    mid = say(card.cs, card.room, "just a note", n=7)
    with card.store._write() as (conn, now):
        p = room.propose(conn, card.launch, [mid], "a summary")
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, spec_text(ctx), thread="th-" + "0" * 32)
    assert e.value.code == "not_a_spec_card"


def test_tap_on_a_finished_thread_is_refused_and_rolled_back(card):
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    p = propose(card, t.thread_id, words)
    card.store.set_state(t.thread_id, "closed", by="operator")
    before = counts(card.store)
    with pytest.raises(CoordError):
        tap(card, p, words)
    assert counts(card.store) == before


# --- the cite line -------------------------------------------------------------------------------------------------
def test_cite_label_names_the_room_card(card):
    """The spec drop reads as a room card he tapped, never as his own speech (F3)."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    r = tap(card, propose(card, t.thread_id, words), words)
    d = drafts.latest(card.store, r.draft_id)
    c = card.store._connect()
    try:
        label = drafts.cite_label(c, d, {"kind": "drop", "ref": r.drop.drop_id})
        plain = drafts.cite_label(c, d, {"kind": "drop", "ref": _d.drop_id})
    finally:
        c.close()
    assert "from a room card you tapped (drafted by the room)" in label["line"]
    assert "room card" not in plain["line"]


def test_cite_label_on_a_store_without_room_tables(ctx):
    """cite_label reads through read-only doors: a store that never held the room's tables labels as before."""
    _d, t = plain_work(ctx)
    d = drafts.drop_rule(ctx.store, t.thread_id, by="operator")
    c = ctx.store._connect()
    try:
        assert "room card" not in drafts.cite_label(c, d, {"kind": "drop", "ref": _d.drop_id})["line"]
    finally:
        c.close()


# --- schema upgrade ------------------------------------------------------------------------------------------------
def test_an_old_store_gains_the_spec_tables_and_keeps_its_proposals(card):
    """A store whose room tables predate the spec tables: ensure() adds them; an old proposal still reads."""
    _d, t = plain_work(card.ctx)
    mid = say(card.cs, card.room, "old card", n=5)
    with card.store._write() as (conn, now):
        old = room.propose(conn, card.launch, [mid], "an older proposal")
    c = raw(card.store.directory)
    c.execute("DROP TABLE spec_card_taps")
    c.execute("DROP TABLE spec_proposals")
    c.close()
    c = card.store._connect()
    try:
        room.ensure(c)       # not in a transaction: the connection autocommits
        names = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"spec_proposals", "spec_card_taps"} <= names
        assert room.get_proposal(c, old.proposal_id).summary == "an older proposal"
        assert room.spec_of(c, old.proposal_id) is None
    finally:
        c.close()
    p = propose(card, t.thread_id, spec_text(card.ctx))
    assert room_spec(card, p) is not None


# --- L3 round 1 (h14c) ---------------------------------------------------------------------------------------------
def test_standing_is_none_for_a_head_whose_thread_moved_past_its_binding(card):
    """codex L3 r1 MED: standing() read only kill / thread_done / stop, so a head no longer live (its thread moved past
    the revision it was signed for) still read as a running grant, and the card hid what the draft widens."""
    ctx = card.ctx
    _d, b, t = head_thread(ctx)
    c = card.store._connect()
    try:
        assert spec_card.standing(c, t.thread_id, card.store._stamp()) is not None
    finally:
        c.close()
    ctx.store.split(ctx.plain("one more thing").drop_id, [Part("work", attach=t.thread_id)])
    assert "moved past the revision" in ctx.live(b["envelope_id"], t)
    c = card.store._connect()
    try:
        assert spec_card.standing(c, t.thread_id, card.store._stamp()) is None
    finally:
        c.close()
    assert spec_card.preview(card.store, t.thread_id, spec_text(ctx))["signed_head"] is False


def test_a_dismissed_newer_card_replaces_nothing(card):
    """complement L3 r1 MED: a newer card he put away ("Not now") still replaced the older one, so both were dead."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    old = propose(card, t.thread_id, spec_text(ctx))
    new = propose(card, t.thread_id, spec_text(ctx, stop="1h"))
    with card.store._write() as (conn, now):
        room.dismiss(conn, new.proposal_id, now=now)
    conn = card.store._connect()
    try:
        assert room.replaced_by(conn, old.proposal_id) is None
    finally:
        conn.close()
    assert tap(card, old, spec_text(ctx)).created


def test_a_committed_newer_card_still_replaces_the_older(card):
    ctx = card.ctx
    _d, t = plain_work(ctx)
    old = propose(card, t.thread_id, spec_text(ctx))
    new = propose(card, t.thread_id, spec_text(ctx, stop="1h"))
    tap(card, new, spec_text(ctx, stop="1h"))
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, old, spec_text(ctx), outbox="tap-0002-bbbb")
    assert e.value.code == "replaced"


def test_a_resend_checks_the_thread_it_echoes_and_answers_the_stored_one(card):
    """codex L3 r1 LOW: a resend returned the stored tap before the thread check, so it answered with whatever thread the
    request named."""
    ctx = card.ctx
    _d, t = plain_work(ctx)
    words = spec_text(ctx)
    p = propose(card, t.thread_id, words)
    first = tap(card, p, words)
    assert first.thread_id == t.thread_id
    with pytest.raises(spec_card.TapRefused) as e:
        tap(card, p, words, thread="th-" + "1" * 32)
    assert e.value.code == "thread_mismatch"
    again = tap(card, p, words)
    assert not again.created and again.thread_id == t.thread_id
