"""H1-5 lane 1: a mission-level draft (design page ``h15_design.md``, section 6, lane 1) and the invariants I1-I5.

A mission draft is one draft series whose proposal binds every child of one mission, signed once into one worded
envelope with N binds. Real keys, real scratch git repos, the store's clock a list the test moves (the fixtures are
H1-1's); the phone-port test at the end uses H1-3's world and its software passkey.

Each invariant test names the revert that made it fail in the lane's return. What no instrument here holds: a person
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


def test_a_one_thread_draft_is_byte_identical_to_the_one_before_this_lane(ctx):
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


# --- I3: no new heirs -----------------------------------------------------------------------------------------------
def test_i3_no_regrouping_changes_who_the_envelope_binds(ctx):
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
def test_i4_a_mission_draft_writes_no_envelope_and_no_bind(ctx):
    """The writer, a revision, the rules, the card read, the sign builder and a withdrawal write zero rows to
    ``envelopes``, ``envelope_binds`` and ``envelope_attenuations``. (The room's card tap is not built in this lane.)"""
    d, ts, m = mission_of(ctx, 3)
    queue = ctx.tmp / "queue"
    queue.mkdir()
    dr = mission_draft(ctx, m, d)
    f = fields_for(ctx, budget={**BUDGET, "hours": 1})
    r2 = drafts.revise(ctx.store, dr.draft_id, f, cites_for(d, f), parent=dr.digest, by="operator")
    r3 = drafts.drop_rule(ctx.store, ts[0].thread_id, by="operator")
    assert r3.draft_id == dr.draft_id and r3.proposal["binds"] == dr.proposal["binds"]
    spec = ctx.plain(json.dumps({"stop": "1h"}))
    r4 = drafts.spec_rule(ctx.store, ts[0].thread_id, spec.drop_id, by="operator")
    assert r4.draft_id == dr.draft_id and len(r4.proposal["binds"]) == 3
    for t in ts:
        drafts.cards(ctx.store, t.thread_id, authority=ctx.snap, queue_dir=queue)
    built(ctx, r4)
    drafts.withdraw(ctx.store, dr.draft_id)
    assert r2.revision == 2 and r4.revision == r3.revision + 1
    for table in ("envelopes", "envelope_binds", "envelope_attenuations"):
        assert count(ctx, table) == 0, table


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
    d, ts, m = mission_of(ctx, 3)
    dr = mission_draft(ctx, m, d)
    u = new_work_thread(ctx)
    missions.attach(ctx.store, m.mission_id, u.thread_id)
    missions.detach(ctx.store, m.mission_id, u.thread_id)
    assert missions.get_mission(ctx.store, m.mission_id).children_sha256 == m.children_sha256
    assert drafts.state_of(ctx.store, dr.draft_id).kind == "lapsed"


def test_a_multi_bind_draft_that_supersedes_anything_is_lapsed(ctx):
    """In this build a mission envelope covers children that hold no envelope: a proposal that binds several threads and
    names a head to supersede is refused by the one lapse check every reader and the acceptance share."""
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
