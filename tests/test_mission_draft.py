"""H1-5 lane 1: a mission-level draft (design page ``h15_design.md``, section 6, lane 1) and the invariants I1-I5.

A mission draft is one draft series whose proposal binds every child of one mission, signed once into one worded
envelope with N binds. Real keys, real scratch git repos, the store's clock a list the test moves (the fixtures are
H1-1's); the phone-port test at the end uses H1-3's world and its software passkey.

Each invariant test names, in a ``# guard:`` comment, the guard whose removal makes it fail; the mutation run and the
red line for each are in project_memory/1118-22_lanes/h15_lane1_return.md and h15_fix1_return.md. I4's two
FOREIGN-KEY tables have no revert of their own (see that test). What no instrument here holds: a person
reading the sheet that lists the threads, and signing it on the installed build."""
from __future__ import annotations

import inspect
import json
import random
import shutil
import sqlite3
from datetime import timedelta

import pytest

from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.desk import draft_sign as ds
from levain.desk import envelope as env
from levain.desk import envelope_draft as ed
from levain.desk import envelope_drafts as drafts
from levain.desk import missions
from levain.desk.envelope_drafts import DraftStale
from levain.desk.envelopes import EnvelopeStore
from levain.desk.legacy import ROLE_DESK
from levain.desk.store import CoordError, Part, Source
from tests.test_desk_envelope import BUDGET, HOUR, NOW
from tests.test_desk_envelope_draft import (  # noqa: F401  (fixtures)
    built, cites_for, ctx, fields_for, keys, make, repo, snapshot)
from tests.test_spec_card import card, counts, propose, spec_text  # noqa: F401  (fixture)
from tests.test_spec_card import tap as card_tap


def mission_of(ctx, n=3, words="land the readme change in three parts"):
    """One drop split into ``n`` work threads (each holds the drop), grouped into a mission. -> (drop, threads, mission)"""
    d = ctx.plain(words)
    ts = ctx.store.split(d.drop_id, [Part("work", f"part {i}") for i in range(n)])
    return d, sorted(ts, key=lambda t: t.thread_id), missions.create_mission(ctx.store, [t.thread_id for t in ts])


def mission_draft(ctx, m, d, **over):
    f = fields_for(ctx, **over)
    return drafts.create_mission_draft(ctx.store, m.mission_id, f, cites_for(d, f), by="operator")


def signed_mission(ctx, n=3):
    """A mission of ``n`` children, its draft signed once. -> (drop, threads, mission, draft, stored envelope)"""
    d, ts, m = mission_of(ctx, n)
    dr = mission_draft(ctx, m, d)
    signed, _since, _against = built(ctx, dr)
    stored, created = ctx.accept(signed)
    assert created
    return d, ts, m, dr, stored


def count(ctx, table):
    return ctx.rows(f"SELECT COUNT(*) FROM {table}")[0][0]


def late_drop(ctx, words="a drop that moves a thread"):
    return ctx.store.accept_drop(words, surface="cli", source=ctx.src)[0]


def new_work_thread(ctx, words="a thread nobody signed for"):
    d = ctx.plain(words)
    (t,) = ctx.store.split(d.drop_id, [Part("work", "u")])
    return t


def grant_of(body):
    """The part of a body a child inherits: where, effects, budget, stop."""
    return {k: body[k] for k in ("where", *env.EFFECT_LISTS, "budget", "stop_at")}


# --- the path that failed before this lane: a proposal binding more than one thread ------------------------------------
def test_a_proposal_binding_three_threads_passes_the_shape_check(ctx):
    d, ts, m = mission_of(ctx)
    _d, _t, one = make(ctx, "an ordinary single-thread draft")
    binds = [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256} for t in ts]
    assert ed.check_proposal({**one.proposal, "binds": binds})["binds"] == binds
    with pytest.raises(ed.DraftError, match="at least one"):
        ed.check_proposal({**one.proposal, "binds": []})
    with pytest.raises(ed.DraftError, match="sorted"):
        ed.check_proposal({**one.proposal, "binds": list(reversed(binds))})
    with pytest.raises(ed.DraftError, match="twice"):
        ed.check_proposal({**one.proposal, "binds": [binds[0], binds[0]]})


# --- the mission draft and its one signature -----------------------------------------------------------------------
def test_a_three_child_mission_signs_once_into_three_binds(ctx):
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    assert [b["thread_id"] for b in stored.body["binds"]] == [t.thread_id for t in ts]
    assert dr.thread_id == ts[0].thread_id                              # the series' anchor: the first child by id
    assert stored.body["drop_id"] == d.drop_id
    assert count(ctx, "envelopes") == 1
    assert ctx.rows("SELECT thread_id FROM envelope_binds ORDER BY thread_id") == [(t.thread_id,) for t in ts]
    assert ctx.rows("SELECT mission_id, mission_revision FROM mission_drafts") == [(m.mission_id, m.rev)]
    for t in ts:
        assert grant_of(ctx.store.effective(stored.envelope_id, t.thread_id)) == grant_of(stored.body)
        assert ctx.store.live_refusal(stored.envelope_id, t.thread_id, authority=ctx.snap) is None
    assert drafts.state_of(ctx.store, dr.draft_id).kind == "signed"


def test_the_binds_come_from_the_store_not_from_the_caller(ctx):
    d, ts, m = mission_of(ctx)
    dr = mission_draft(ctx, m, d)
    assert dr.proposal["binds"] == [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256}
                                    for t in ts]
    assert dr.proposal["supersedes"] is None and env.LAUNCH_LIST not in dr.proposal
    with pytest.raises(TypeError):                                       # no argument names the threads
        drafts.create_mission_draft(ctx.store, m.mission_id, {}, [], by="operator", threads=[ts[0].thread_id])


def test_a_per_thread_kill_stops_one_child_and_leaves_two_live(ctx):
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    assert ctx.store.attenuate(stored.envelope_id, "kill", thread_id=ts[1].thread_id)
    assert "was killed" in ctx.store.live_refusal(stored.envelope_id, ts[1].thread_id, authority=ctx.snap)
    for t in (ts[0], ts[2]):
        assert ctx.store.live_refusal(stored.envelope_id, t.thread_id, authority=ctx.snap) is None
        assert grant_of(ctx.store.effective(stored.envelope_id, t.thread_id)) == grant_of(stored.body)


def test_a_thread_attached_after_signing_is_not_bound(ctx):
    # guard: EnvelopeStore._store_refusal's "not bound" (I3)
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    u = new_work_thread(ctx)
    missions.attach(ctx.store, m.mission_id, u.thread_id)
    assert count(ctx, "envelope_binds") == 3
    refusal = ctx.store.live_refusal(stored.envelope_id, u.thread_id, authority=ctx.snap)
    assert refusal is not None and "not bound" in refusal
    with pytest.raises(CoordError, match="not bound"):                   # nor can a narrowing be scoped to it
        ctx.store.attenuate(stored.envelope_id, "kill", thread_id=u.thread_id)


def test_a_mission_draft_revises_as_a_mission_and_keeps_one_link(ctx):
    d, ts, m = mission_of(ctx)
    r1 = mission_draft(ctx, m, d)
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    r2 = drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r1.digest, by="operator")
    assert r2.revision == 2 and r2.thread_id == r1.thread_id and r2.proposal["binds"] == r1.proposal["binds"]
    assert ctx.rows("SELECT COUNT(*) FROM mission_drafts") == [(1,)]
    # the mission moves: the series can take no revision (it is born open or not at all)
    missions.attach(ctx.store, m.mission_id, new_work_thread(ctx).thread_id)
    with pytest.raises(CoordError, match="mission .* changed since this series was written"):
        drafts.revise(ctx.store, r1.draft_id, f, cites_for(d, f), parent=r2.digest, by="operator")
    assert ctx.rows("SELECT revision FROM envelope_drafts ORDER BY revision") == [(1,), (2,)]


def test_the_writer_refuses_what_a_mission_envelope_cannot_cover(ctx):
    f = fields_for(ctx)

    def refused(mission, match):
        with pytest.raises(CoordError, match=match):
            drafts.create_mission_draft(ctx.store, mission.mission_id, f, cites_for(d0, f), by="operator")
        assert (count(ctx, "envelope_drafts"), count(ctx, "mission_drafts")) == (0, 0)

    d0 = ctx.plain("the drop every refusal cites")
    # one child is a plain thread draft
    (solo,) = ctx.store.split(d0.drop_id, [Part("work", "only")])
    refused(missions.create_mission(ctx.store, [solo.thread_id]), "drafted on its own")
    # no shared drop
    a, b = new_work_thread(ctx, "separate a"), new_work_thread(ctx, "separate b")
    refused(missions.create_mission(ctx.store, [a.thread_id, b.thread_id]), "share no drop")
    # a child that already holds an envelope
    d1, ts, m = mission_of(ctx, 2, "two children, one of which is signed for first")
    _body, signed = ctx.worded(d1, ts[1])
    ctx.accept(signed)
    f1 = fields_for(ctx)
    with pytest.raises(CoordError, match="covers children that hold none yet"):
        drafts.create_mission_draft(ctx.store, m.mission_id, f1, cites_for(d1, f1), by="operator")
    # a child that is done
    d2, ts2, m2 = mission_of(ctx, 2, "two children, one of which is done")
    ctx.store.set_state(ts2[0].thread_id, "done", by=ROLE_DESK, token=_lease_token(ctx))
    with pytest.raises(CoordError, match="is done"):
        drafts.create_mission_draft(ctx.store, m2.mission_id, f1, cites_for(d2, f1), by="operator")
    # no such mission
    with pytest.raises(CoordError, match="no mission"):
        drafts.create_mission_draft(ctx.store, "ms-" + "0" * 32, f1, cites_for(d2, f1), by="operator")
    assert count(ctx, "mission_drafts") == 0


def _lease_token(ctx):
    from tests.test_desk_envelope import _lease

    return _lease(ctx.store)


@pytest.mark.parametrize("holder", [0, -1], ids=["only the anchor holds it", "only the last child holds it"])
def test_a_cite_held_by_one_child_only_is_refused(ctx, holder):
    """A drop in ONE child: a check that reads only the first bind passes it when the anchor holds it, one that reads
    only the last passes it when the last child does; the guard reads every bind."""
    # guard: envelope_drafts._check_cites_against loops over every bind (not binds[:1] / binds[-1:])
    d, ts, m = mission_of(ctx, 3)
    late = late_drop(ctx, "a drop attached to one child only")
    ctx.store.split(late.drop_id, [Part("work", attach=ts[holder].thread_id)])
    f = fields_for(ctx)
    bad = [{**c, "ref": late.drop_id, "digest": late.words_sha256} if c["field"] == "where" else c
           for c in cites_for(d, f)]
    with pytest.raises(CoordError, match="not in this thread and is not newer"):
        drafts.create_mission_draft(ctx.store, m.mission_id, f, bad, by="operator")
    assert count(ctx, "envelope_drafts") == 0


def test_a_cite_must_hold_for_every_bound_thread(ctx):
    d, ts, m = mission_of(ctx, 3)
    elsewhere = new_work_thread(ctx, "a drop in no child").thread_id
    own = ctx.store.thread(elsewhere).members[0]
    other = ctx.store.drop(own)
    f = fields_for(ctx)
    bad = [{**c, "ref": other.drop_id, "digest": other.words_sha256} if c["field"] == "where" else c
           for c in cites_for(d, f)]
    with pytest.raises(CoordError, match="not in this thread and is not newer"):
        drafts.create_mission_draft(ctx.store, m.mission_id, f, bad, by="operator")
    dr = mission_draft(ctx, m, d)
    conn = ctx.store.open_readonly()
    try:
        label = drafts.cite_label(conn, dr, next(c for c in dr.cites if c["field"] == "where"))
    finally:
        conn.close()
    assert label["in_thread"] is True and "every thread this draft binds" in label["line"]


def test_the_link_is_append_only_and_names_a_series(ctx):
    d, ts, m = mission_of(ctx)
    dr = mission_draft(ctx, m, d)
    conn = sqlite3.connect(ctx.store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE mission_drafts SET mission_revision = 9")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM mission_drafts")
        with pytest.raises(sqlite3.IntegrityError, match="never replaced"):
            conn.execute("INSERT INTO mission_drafts VALUES (?, ?, 1, 'x', 999999, 'x')", (dr.draft_id, m.mission_id))
        with pytest.raises(sqlite3.IntegrityError, match="names a draft series that exists"):
            conn.execute("INSERT INTO mission_drafts VALUES ('ed-none', ?, 1, 'x', 999998, 'x')", (m.mission_id,))
    finally:
        conn.close()


# --- a one-thread draft is what it was --------------------------------------------------------------------------------
GOLDEN_DIGEST = "sha256:d31266e9456ae750cd21a029ee894b41daf5cb9f5c7957e3e1a7a9ba33d2da4b"   # read from origin/main's code


def test_the_draft_digest_is_pinned_and_a_one_thread_draft_writes_one_bind_and_no_link(ctx):
    # the golden digest guards the NEXT lane's change to draft_digest; the second half is this lane's regression check
    proposal = {"format": env.BODY_FORMAT, "store_id": "0" * 32, "envelope_id": "ev-" + "1" * 32, "origin": "worded",
                "drop_id": "dr-" + "2" * 32, "words_sha256": "sha256:" + "3" * 64,
                "binds": [{"thread_id": "th-" + "4" * 32, "rev": 1, "members_sha256": "sha256:" + "5" * 64}],
                "supersedes": None, "issued_at": utc_stamp(NOW), "where": [{"repo": "/r", "paths": ["README"]}],
                "lands": [], "pushes": [], "publishes": [], "budget": dict(BUDGET),
                "stop_at": utc_stamp(NOW + 2 * HOUR)}
    cites = [{"field": "where", "kind": "drop", "ref": "dr-" + "2" * 32, "digest": "sha256:" + "3" * 64}]
    assert ed.draft_digest("0" * 32, "ed-" + "6" * 32, "th-" + "4" * 32, 1, None, proposal, cites) == GOLDEN_DIGEST
    # and the draft a thread writes now carries one bind, no link row, and recomputes to its stored digest
    _d, t, r1 = make(ctx)
    assert len(r1.proposal["binds"]) == 1 and count(ctx, "mission_drafts") == 0
    assert ed.draft_digest(r1.proposal["store_id"], r1.draft_id, t.thread_id, 1, None, r1.proposal,
                           r1.cites) == r1.digest
    assert set(r1.proposal) == ed._PROPOSAL_KEYS
    # the link is in neither the signed body nor the digest
    assert "mission_id" not in env.BODY_KEYS
    assert "mission_id" not in inspect.signature(ed.draft_digest).parameters


# --- I1: never wider than signed ------------------------------------------------------------------------------------
def test_i1_no_sequence_of_narrowings_is_wider_than_the_signed_body(ctx):
    """I1 and I2 over a seeded random sequence of ``attenuate`` calls (the three kinds, envelope-wide and per thread,
    refused ones included): every child's effective grant is never wider than the signed body, and never wider than
    it was before the call. A call the store refuses writes nothing."""
    # guard: the `at > _stop` refusal in EnvelopeStore._attenuate (red at "a later stop was taken"); I2 is below
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    eid, body = stored.envelope_id, stored.body
    stop = parse_stamp(body["stop_at"])
    identity = env.effect_identity("lands", body["lands"][0])
    rng = random.Random(1118)
    before = {t.thread_id: ctx.store.effective(eid, t.thread_id) for t in ts}
    refused = written = 0
    wide = stop                                      # the stop the envelope-wide narrowings have left
    own = {t.thread_id: stop for t in ts}            # the stop each child's own narrowings have left
    for _ in range(80):
        scope = rng.choice([None, *[t.thread_id for t in ts]])
        kind = rng.choice(["stop_earlier", "stop_earlier", "stop_earlier", "drop_effect", "drop_effect", "kill"]
                          if rng.random() < 0.9 else ["kill"])
        at = None
        if kind == "stop_earlier":
            at = stop + timedelta(minutes=rng.randint(-100, 90))
            value = utc_stamp(at)
        elif kind == "drop_effect":
            value = rng.choice([identity, "lands: an effect the body never held"])
        else:
            value = ""
        rows = count(ctx, "envelope_attenuations")
        current = wide if scope is None else min(wide, own[scope])
        try:
            wrote = ctx.store.attenuate(eid, kind, value, thread_id=scope)
        except CoordError:
            refused += 1
            assert at is not None or kind == "drop_effect"
            assert count(ctx, "envelope_attenuations") == rows          # a refusal writes nothing
        else:
            written += wrote
            assert at is None or at <= current, "a later stop was taken, not refused"
            if wrote and at is not None:
                if scope is None:
                    wide = at
                else:
                    own[scope] = at
        for t in ts:
            after = ctx.store.effective(eid, t.thread_id)
            assert not env.widens(body, after), (kind, value, scope)                       # I1
            assert not env.widens(before[t.thread_id], after), (kind, value, scope)        # I2
            before[t.thread_id] = after
    assert refused and written                                           # the sequence reached both outcomes


def test_i1_a_later_stop_is_refused_in_plain_words_and_writes_nothing(ctx):
    # guard: _attenuate's `at > _stop` refusal and its "identity the body holds" check for drop_effect
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    later = utc_stamp(parse_stamp(stored.body["stop_at"]) + timedelta(hours=1))
    for scope in (None, ts[1].thread_id):
        with pytest.raises(CoordError, match="a later one is a widening, which needs a new signed envelope"):
            ctx.store.attenuate(stored.envelope_id, "stop_earlier", later, thread_id=scope)
    with pytest.raises(CoordError, match="an effect identity the signed envelope holds"):
        ctx.store.attenuate(stored.envelope_id, "drop_effect", "lands: not held", thread_id=ts[0].thread_id)
    assert count(ctx, "envelope_attenuations") == 0
    for t in ts:
        assert ctx.store.effective(stored.envelope_id, t.thread_id)["stop_at"] == stored.body["stop_at"]


# --- I2: monotone over time ---------------------------------------------------------------------------------------
def test_i2_the_narrowing_record_only_grows_and_never_records_a_later_stop(ctx):
    """I2 as the store holds it: ``effective`` is computed from the signed body and the ``envelope_attenuations`` rows,
    so it can only widen if a row is changed or removed, or a row records a stop later than the one in force when it
    was written. Neither the MIN in ``_stop`` nor the I1 loop above can see either; this test reads the record."""
    # guards: ENVELOPE_SCHEMA's _append_only("envelope_attenuations"), and the `at > _stop` refusal in _attenuate
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    eid, stop = stored.envelope_id, parse_stamp(stored.body["stop_at"])
    for minutes, scope in ((-30, None), (+60, ts[1].thread_id), (-40, ts[1].thread_id), (+10, None), (-50, None)):
        try:
            ctx.store.attenuate(eid, "stop_earlier", utc_stamp(stop + timedelta(minutes=minutes)), thread_id=scope)
        except CoordError:
            pass
    # replay the record in its own order: every stop row is at or before the stop in force for its scope when written
    wide, own = stop, {}
    for tid, kind, value in ctx.rows(f"SELECT thread_id, kind, value FROM envelope_attenuations "
                                     f"WHERE envelope_id = '{eid}' ORDER BY gseq"):
        at = parse_stamp(value)
        current = wide if tid is None else min(wide, own.get(tid, stop))
        assert at <= current, f"the record holds a stop later than the one in force: {value} ({tid})"
        if tid is None:
            wide = at
        else:
            own[tid] = at
    assert count(ctx, "envelope_attenuations") == 3
    conn = sqlite3.connect(ctx.store.path)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE envelope_attenuations SET value = ?", (stored.body["stop_at"],))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM envelope_attenuations")
    finally:
        conn.close()
    for t in ts:
        assert ctx.store.effective(eid, t.thread_id)["stop_at"] == utc_stamp(stop + timedelta(minutes=-50))


# --- I3: no new heirs -----------------------------------------------------------------------------------------------
def test_i3_the_signed_envelopes_own_drop_cannot_be_split_again(ctx):
    """``bind_on_split`` is the one store path that binds an envelope on a split, and it runs only at a drop's FIRST
    split (``_split_done``). A worded envelope's drop is a member of every thread it binds, so it was split before it
    could be signed: a new split of it, the one input that path would read for this envelope, is refused."""
    # guard: CoordStore.split's "already split" refusal (bind_on_split's drop.envelope_id early return is behind it)
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    threads = count(ctx, "threads")
    with pytest.raises(CoordError, match="is already split"):
        ctx.store.split(d.drop_id, [Part("work", "the same drop again")])
    assert (count(ctx, "envelope_binds"), count(ctx, "threads")) == (3, threads)


def test_i3_no_regrouping_changes_who_the_envelope_binds(ctx):
    # guard: EnvelopeStore._store_refusal's "not bound" for a thread the envelope does not bind
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    eid = stored.envelope_id
    outsider = new_work_thread(ctx)

    def holds():
        assert ctx.rows(f"SELECT COUNT(*) FROM envelope_binds WHERE envelope_id = '{eid}'") == [
            (len(stored.body["binds"]),)]
        refusal = ctx.store.live_refusal(eid, outsider.thread_id, authority=ctx.snap)
        assert refusal is not None and "not bound" in refusal

    holds()
    missions.attach(ctx.store, m.mission_id, outsider.thread_id)
    holds()
    missions.detach(ctx.store, m.mission_id, ts[0].thread_id)
    holds()
    missions.attach(ctx.store, m.mission_id, ts[0].thread_id)
    holds()
    # a drop attached to a child moves that child's revision: its grant stops, the bind rows stay as signed
    ctx.store.split(late_drop(ctx).drop_id, [Part("work", attach=ts[1].thread_id)])
    holds()
    assert "moved past the revision" in ctx.store.live_refusal(eid, ts[1].thread_id, authority=ctx.snap)
    assert ctx.store.live_refusal(eid, ts[0].thread_id, authority=ctx.snap) is None
    # a merge folds one child into another: the closed one is ended by the service, nobody new is bound
    ctx.store.merge(ts[0].thread_id, ts[2].thread_id)
    holds()
    ctx.store.split(late_drop(ctx, "another").drop_id, [Part("work", attach=outsider.thread_id)])
    holds()


# --- I4: a draft grants nothing -------------------------------------------------------------------------------------
def test_i4_a_mission_draft_writes_no_envelope(ctx):
    """The writer, a revision, the card read, the sign builder and a withdrawal write zero rows to ``envelopes``. (The
    one-thread rules are refused on a mission's series: see the W2 test below.) ``envelope_binds`` and
    ``envelope_attenuations`` are NOT counted on their own: each has a FOREIGN KEY to ``envelopes``, so a zero there
    is implied by this one count and no revert can fail it separately; the test pins the keys instead."""
    # guard: the draft writers never call accept_envelope (the only writer of an envelopes row)
    d, ts, m = mission_of(ctx, 3)
    queue = ctx.tmp / "queue"
    queue.mkdir()
    dr = mission_draft(ctx, m, d)
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    r2 = drafts.revise(ctx.store, dr.draft_id, f, cites_for(d, f), parent=dr.digest, by="operator")
    for t in ts:
        drafts.cards(ctx.store, t.thread_id, authority=ctx.snap, queue_dir=queue)
    built(ctx, r2)
    drafts.withdraw(ctx.store, dr.draft_id)
    assert r2.revision == 2 and count(ctx, "envelopes") == 0
    for table in ("envelope_binds", "envelope_attenuations"):
        keys_to = {r[2] for r in ctx.rows(f"PRAGMA foreign_key_list({table})")}
        assert "envelopes" in keys_to, table


# --- W2: a one-thread change never revises a mission's series --------------------------------------------------------
def test_a_one_thread_tap_or_rule_on_a_mission_anchor_is_refused_and_writes_nothing(card):
    """Reproduced first (h15_fix1_return.md): before the guard, a spec card tap on the anchor thread, and drop_rule on
    it, each wrote revision 2 of the 3-bind series. The one-thread writer now refuses a series that binds several
    threads; revise() still revises it whole."""
    # guard: envelope_drafts._write_revision refuses a series with more than one bind (or a mission_drafts row)
    ctx = card.ctx
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    words = spec_text(ctx, stop="1h")
    p = propose(card, ts[0].thread_id, words)
    before = counts(card.store)
    with pytest.raises(CoordError, match="is a mission's draft and binds 3 threads"):
        card_tap(card, p, words)
    with pytest.raises(CoordError, match="is a mission's draft and binds 3 threads"):
        drafts.drop_rule(ctx.store, ts[0].thread_id, by="operator")
    assert counts(card.store) == before
    spec = ctx.plain(json.dumps({"stop": "1h"}))
    before = counts(card.store)                                          # with the spec drop this test sent itself
    with pytest.raises(CoordError, match="is a mission's draft and binds 3 threads"):
        drafts.spec_rule(ctx.store, ts[0].thread_id, spec.drop_id, by="operator")
    assert counts(card.store) == before
    assert drafts.latest(ctx.store, dr.draft_id).revision == 1
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    r2 = drafts.revise(ctx.store, dr.draft_id, f, cites_for(d, f), parent=dr.digest, by="operator")
    assert r2.revision == 2 and len(r2.proposal["binds"]) == 3


# --- fix 2 A: a mission's series is found from EVERY child it binds, not only from its anchor ------------------------
def test_a_child_thread_sees_the_mission_draft_and_opens_no_competing_series(card):
    """Reproduced first (h15_fix2_return.md): before the fix, ``_unclosed`` matched only the anchor thread, so on a
    non-anchor child a card tap, ``drop_rule`` and ``spec_rule`` each opened a second, single-thread series next to the
    open mission draft, and the child's card and facet showed no draft at all."""
    # guard: envelope_drafts._unclosed matches a series through any of its binds, and _write_series_revision refuses
    # a new series on a thread an unclosed series already binds
    ctx = card.ctx
    queue = ctx.tmp / "queue"
    queue.mkdir()
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    for t in ts:
        assert drafts.unclosed_series(ctx.store, t.thread_id).draft_id == dr.draft_id
        (c,) = drafts.cards(ctx.store, t.thread_id, authority=ctx.snap, queue_dir=queue)
        assert c.kind == "ENVELOPE" and c.detail["draft_id"] == dr.draft_id
        facet = drafts.authority_facet(ctx.store, t.thread_id, authority=ctx.snap, queue_dir=queue)
        assert drafts.label(facet) == "ENVELOPE" and facet.draft.draft_id == dr.draft_id
    child = ts[1].thread_id
    words = spec_text(ctx, stop="1h")
    p = propose(card, child, words)
    before = counts(card.store)
    with pytest.raises(CoordError, match="is a mission's draft and binds 3 threads"):
        card_tap(card, p, words)
    with pytest.raises(CoordError, match="is a mission's draft and binds 3 threads"):
        drafts.drop_rule(ctx.store, child, by="operator")
    f = fields_for(ctx)
    with pytest.raises(CoordError, match=f"thread {child} is already bound by draft {dr.draft_id}"):
        drafts.create(ctx.store, child, f, cites_for(d, f), by="operator")
    assert counts(card.store) == before
    spec = ctx.plain(json.dumps({"stop": "1h"}))
    before = counts(card.store)
    with pytest.raises(CoordError, match="is a mission's draft and binds 3 threads"):
        drafts.spec_rule(ctx.store, child, spec.drop_id, by="operator")
    assert counts(card.store) == before
    assert ctx.rows("SELECT DISTINCT draft_id FROM envelope_drafts") == [(dr.draft_id,)]
    # once the mission's series is withdrawn, the child drafts on its own again
    drafts.withdraw(ctx.store, dr.draft_id)
    one = drafts.create(ctx.store, child, f, cites_for(d, f), by="operator")
    assert drafts.unclosed_series(ctx.store, child).draft_id == one.draft_id
    assert drafts.unclosed_series(ctx.store, ts[0].thread_id) is None


def test_a_mission_draft_is_refused_while_a_child_holds_its_own_series(ctx):
    # guard: the same new-series check, the other way round: a child's own unclosed series blocks the mission's
    d, ts, m = mission_of(ctx, 3)
    f = fields_for(ctx)
    own = drafts.create(ctx.store, ts[2].thread_id, f, cites_for(d, f), by="operator")
    rows = count(ctx, "envelope_drafts")
    with pytest.raises(CoordError, match=f"thread {ts[2].thread_id} is already bound by draft {own.draft_id}"):
        mission_draft(ctx, m, d)
    assert (count(ctx, "envelope_drafts"), count(ctx, "mission_drafts")) == (rows, 0)


def test_a_child_card_reads_its_own_bind(ctx):
    """The card reads the bind of the thread it is shown on, not the anchor's (``_view_inputs`` through ``_bind_of``).
    No diff on screen can show the difference today (an open mission draft has no head on any child, and the member
    diff runs only beside a head), so the pick itself is pinned."""
    # guard: _bind_of, which _view_inputs reads the viewed thread's bind through
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    for t in ts:
        assert drafts._bind_of(dr, t.thread_id) == {"thread_id": t.thread_id, "rev": t.rev,
                                                    "members_sha256": t.members_sha256}
    with pytest.raises(CoordError, match="does not bind thread"):
        drafts._bind_of(dr, new_work_thread(ctx).thread_id)


# --- fix 2 B-E: shape and store refusals are typed and in plain words -------------------------------------------------
@pytest.mark.parametrize("binds, match", [
    (lambda b: [b[0], b[0]], "names thread .* twice"),
    (lambda b: [{k: v for k, v in b[0].items() if k != "thread_id"}, b[1]], "each bind is an object naming"),
    (lambda b: ["not a bind", b[1]], "each bind is an object naming"),
    (lambda b: [{**b[0], "thread_id": 7}, b[1]], "each bind is an object naming"),
    (lambda b: [b[1], b[0]], "sorted by thread id"),
], ids=["duplicate", "no thread id", "not an object", "thread id not a string", "unsorted"])
def test_check_proposal_refuses_malformed_binds_itself(ctx, binds, match):
    # guard: envelope_draft.check_proposal's own bind checks, before parse_body
    d, ts, m = mission_of(ctx)
    _d, _t, one = make(ctx, "an ordinary single-thread draft")
    good = [{"thread_id": t.thread_id, "rev": t.rev, "members_sha256": t.members_sha256} for t in ts]
    called = []
    real = env.parse_body
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(env, "parse_body", lambda text: called.append(1) or real(text))
        with pytest.raises(ed.DraftError, match=match):
            ed.check_proposal({**one.proposal, "binds": binds(good)})
    assert called == []                                                  # refused before the body parse


def test_a_mission_refusal_on_a_series_with_an_unreadable_proposal_is_typed(ctx, monkeypatch):
    # guard: _write_revision reads the series' binds through a typed check, never a raw subscript
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    broken = drafts.Draft(dr.draft_id, dr.revision, dr.thread_id, dr.gseq, dr.parent, {"no": "binds"}, dr.cites,
                          dr.digest, dr.by, dr.at)
    monkeypatch.setattr(drafts, "_row", lambda conn, draft_id, revision=None: broken)
    before = count(ctx, "envelope_drafts")
    with ctx.store._write("operator") as (conn, now):
        with pytest.raises(CoordError, match="cannot be read as a bound series"):
            drafts._write_revision(ctx.store, conn, now, thread_id=ts[0].thread_id, draft_id=dr.draft_id, fields={},
                                   cites=[], parent=dr.digest, by="operator")
    assert count(ctx, "envelope_drafts") == before


def test_a_broken_mission_store_is_not_read_as_a_mission_that_moved(ctx, monkeypatch):
    # guard: _mission_lapse_of reads absence by its own query and catches nothing, so a fault reading a mission that
    # is here propagates (before, any CoordError or OperationalError from missions._latest read as "not in this store")
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    with drafts._reading(ctx.store) as conn:
        link = drafts._mission_link(conn, dr.draft_id)
        assert drafts._mission_lapse_of(conn, ("mi-" + "0" * 32, link[1], link[2])) == \
            f"mission {'mi-' + '0' * 32} is not in this store"

        def broken(conn, mission_id):
            raise CoordError("a mission revision row this reader cannot read")
        monkeypatch.setattr(missions, "_latest", broken)
        with pytest.raises(CoordError, match="cannot read"):
            drafts._mission_lapse_of(conn, link)


def test_a_mission_link_insert_the_table_refuses_is_a_typed_refusal(ctx, monkeypatch):
    # guard: create_mission_draft wraps the mission_drafts INSERT as it wraps the envelope_drafts one
    d, ts, m = mission_of(ctx, 3)
    conn = sqlite3.connect(ctx.store.path)
    try:
        conn.execute("CREATE TRIGGER refuse_links BEFORE INSERT ON mission_drafts BEGIN "
                     "SELECT RAISE(ABORT, 'a link this test refuses'); END")
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(CoordError, match="a link this test refuses"):
        mission_draft(ctx, m, d)
    assert (count(ctx, "envelope_drafts"), count(ctx, "mission_drafts")) == (0, 0)


def test_the_series_writer_refuses_no_thread_and_no_mission_without_an_assert(ctx):
    # guard: _write_series_revision's explicit check (an assert is stripped under python -O)
    with ctx.store._write("operator") as (conn, now):
        with pytest.raises(CoordError, match="names neither a thread nor a mission"):
            drafts._write_series_revision(ctx.store, conn, now, thread_id=None, mission_id=None, draft_id=None,
                                          fields={}, cites=[], parent=None, by="operator", carry=False,
                                          stop_span=None)


# --- I5: stale is refused -------------------------------------------------------------------------------------------
def _attach(ctx, d, ts, m):
    missions.attach(ctx.store, m.mission_id, new_work_thread(ctx).thread_id)


def _detach(ctx, d, ts, m):
    missions.detach(ctx.store, m.mission_id, ts[-1].thread_id)


def _revision_moved(ctx, d, ts, m):
    ctx.store.split(late_drop(ctx).drop_id, [Part("work", attach=ts[-1].thread_id)])


def _gained_a_head(ctx, d, ts, m):
    _body, signed = ctx.worded(d, ts[-1])
    ctx.accept(signed)


@pytest.mark.parametrize("move", [_attach, _detach, _revision_moved, _gained_a_head],
                         ids=["child attached", "child detached", "child revision moved", "child gained a head"])
def test_i5_a_signature_over_a_stale_mission_draft_is_refused_and_writes_nothing(ctx, move):
    """The change is made to the LAST child by id, never the anchor, so a check that reads only the first bind passes it."""
    # guard: lapse_reason's L4 call (attached, detached) and its loop over EVERY bind (revision moved, gained a head)
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    signed, _since, _against = built(ctx, dr)                            # rendered and signed before the change
    move(ctx, d, ts, m)
    heads = count(ctx, "envelopes")
    binds = count(ctx, "envelope_binds")
    with pytest.raises(DraftStale, match="has lapsed"):
        ctx.accept(signed)
    with pytest.raises(DraftStale, match="has lapsed"):
        built(ctx, dr)                                                   # and no new bytes are built over it
    assert drafts.state_of(ctx.store, dr.draft_id).kind == "lapsed"
    assert (count(ctx, "envelopes"), count(ctx, "envelope_binds")) == (heads, binds)
    assert ctx.rows(f"SELECT COUNT(*) FROM envelopes WHERE json_extract(body, '$.draft.draft_id') = '{dr.draft_id}'") \
        == [(0,)]


def test_i5_a_mission_that_moved_and_came_back_still_lapses(ctx):
    """Attach then detach leaves the children as they were, but the mission is two revisions on: the link compares the
    revision as well as the children's hash."""
    # guard: the `now_rev != rev` compare in envelope_drafts._mission_lapse_of
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    u = new_work_thread(ctx)
    missions.attach(ctx.store, m.mission_id, u.thread_id)
    missions.detach(ctx.store, m.mission_id, u.thread_id)
    assert missions.get_mission(ctx.store, m.mission_id).children_sha256 == m.children_sha256
    assert drafts.state_of(ctx.store, dr.draft_id).kind == "lapsed"


def test_i5_a_mission_that_moves_after_preflight_is_refused_under_the_write_lock(ctx, monkeypatch):
    """The preflight reads the draft before the signature check; a child attached after it and before the write lock is
    caught only by the re-check under the lock (design T2)."""
    # guard: EnvelopeStore._draft_admit (admit_check under the write lock) in accept_envelope
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    signed, _since, _against = built(ctx, dr)
    real = EnvelopeStore._draft_rederive
    moved = []

    def rederive_then_move(self, body):
        real(self, body)                                   # runs after the preflight passed, outside the lock
        if not moved:
            moved.append(missions.attach(ctx.store, m.mission_id, new_work_thread(ctx).thread_id))

    monkeypatch.setattr(EnvelopeStore, "_draft_rederive", rederive_then_move)
    with pytest.raises(DraftStale, match="mission .* changed since this series was written"):
        ctx.accept(signed)
    assert moved and (count(ctx, "envelopes"), count(ctx, "envelope_binds")) == (0, 0)


def test_l4_a_revision_that_binds_other_threads_than_the_mission_children_is_lapsed(ctx):
    """No writer makes this (the binds are the children read under the lock), so it is built by hand: the same series
    and an unmoved mission, a proposal binding two of its three children."""
    # guard: the "binds equal the mission's children" half of envelope_drafts._mission_lapse
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    short = {**dr.proposal, "binds": dr.proposal["binds"][:2]}
    conn = ctx.store.open_readonly()
    try:
        assert drafts.lapse_reason(conn, utc_stamp(NOW), ts[0].thread_id, dr.proposal, draft_id=dr.draft_id) is None
        assert "does not bind exactly mission" in drafts.lapse_reason(conn, utc_stamp(NOW), ts[0].thread_id, short,
                                                                      draft_id=dr.draft_id)
    finally:
        conn.close()


def test_a_multi_bind_draft_that_supersedes_anything_is_lapsed(ctx):
    """In this build a mission envelope covers children that hold no envelope: a proposal that binds several threads and
    names a head to supersede is refused by the one lapse check every reader and the acceptance share."""
    # guard: lapse_reason's `len(binds) > 1 and sup is not None` refusal
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    forged = {**dr.proposal, "supersedes": {"envelope_id": "ev-" + "a" * 32, "digest": "sha256:" + "b" * 64, "seen": 0}}
    conn = ctx.store.open_readonly()
    try:
        assert "supersedes no envelope" in drafts.lapse_reason(conn, utc_stamp(NOW), ts[0].thread_id, forged)
        assert drafts.lapse_reason(conn, utc_stamp(NOW), ts[0].thread_id, dr.proposal, draft_id=dr.draft_id) is None
    finally:
        conn.close()


# --- the cards a reply to a signature carries ---------------------------------------------------------------------------
def test_a_reply_to_a_mission_signature_reads_every_bound_thread(ctx, monkeypatch):
    d, ts, m, dr, stored = signed_mission(ctx, 3)
    read = []
    monkeypatch.setattr(ds, "_cards", lambda store, tid: read.append(tid) or [{"thread": tid}])
    assert ds._bound_threads(stored) == [t.thread_id for t in ts]
    got = ds._cards_after(ctx.store, lambda: ds._bound_threads(stored))
    assert read == [t.thread_id for t in ts] and got == {"cards": [{"thread": t.thread_id} for t in ts]}
    read.clear()
    assert ds._cards_after(ctx.store, lambda: ts[0].thread_id) == {"cards": [{"thread": ts[0].thread_id}]}
    assert ds._cards_after(ctx.store, lambda: None) == {} and read == [ts[0].thread_id]


# --- the whole flow on the phone port, with a software passkey ----------------------------------------------------------
from tests.test_desk_draft_sign import Clock, Rig  # noqa: E402
from tests.test_desk_phone import coord_for  # noqa: E402
from tests.test_dispatch_contract import make_repo  # noqa: E402
from tests.test_dispatch_phone import world  # noqa: E402,F401  (fixture)


class MissionRig(Rig):
    """H1-3's phone port and clock, with no head: a fresh mission's children hold no envelope."""

    def __init__(self, world) -> None:
        self.world = world
        self.clock = Clock()
        self.cp = coord_for(world, clock=self.clock)
        self.repo = str(world["repo"][0])
        self.store = EnvelopeStore(world["qdir"], clock=self.clock)


@pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")
class TestPhonePort:
    @pytest.fixture
    def repo(self, tmp_path_factory):
        """The phone world's own repo, which this class gives it in place of the envelope tests' ``repo`` above."""
        return make_repo(tmp_path_factory.mktemp("mission-phone-repo"))

    def test_a_mission_draft_is_signed_once_through_the_phone_port(self, world, monkeypatch):
        rig = MissionRig(world)
        d, _ = rig.store.accept_drop("land the readme change in three parts", surface="cli", source=Source())
        ts = sorted(rig.store.split(d.drop_id, [Part("work", f"part {i}") for i in range(3)]),
                    key=lambda t: t.thread_id)
        m = missions.create_mission(rig.store, [t.thread_id for t in ts])
        f = {"where": [{"repo": rig.repo, "paths": ["."]}], "lands": [], "pushes": [], "publishes": [],
             "budget": dict(BUDGET), "stop_at": utc_stamp(rig.clock.t + timedelta(hours=2))}
        dr = drafts.create_mission_draft(rig.store, m.mission_id, f, cites_for(d, f), by="operator")
        r = rig.review({"draft_id": dr.draft_id, "revision": dr.revision, "digest": dr.digest})
        body = env.parse_body(r["body_text"])
        assert [b["thread_id"] for b in body["binds"]] == [t.thread_id for t in ts]
        assert r["against"]["base_kind"] == "none"
        read = []
        monkeypatch.setattr(ds, "_cards", lambda store, tid: read.append(tid) or [])
        _c, req = rig.sign(r)
        got = rig.accept(req)
        assert got["created"] is True and read == [t.thread_id for t in ts]
        assert rig.rows("SELECT COUNT(*) FROM envelopes") == [1]
        assert rig.rows("SELECT COUNT(*) FROM envelope_binds") == [3]
