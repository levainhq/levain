"""CH-H2: the mission record, the acceptance snapshot compare-and-swap and the ACK rule, over a real coordinator store.

The verifier ``accept`` is handed stands in for the sign-request check CH-DS / TERMSIGN owns: it is a real callable that
records the message it was asked to verify, so what is signed is asserted, never assumed."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import missions, review
from levain.coordinator.missions import AcceptanceRefused
from levain.coordinator.store import CoordError, CoordStore, Part

T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self):
        self.now = T0

    def __call__(self):
        self.now += timedelta(seconds=1)
        return self.now


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return CoordStore(d, clock=Clock())


def _token(store):
    held = store.coord_status()["lease"]
    if held is not None and held["live"] and held["holder"] == "operator":
        return held["token"]
    return store.coord_take()


def _work(store, words="fix the build", kind="work"):
    d, _ = store.accept_drop(words, surface="cli")
    (t,) = store.split(d.drop_id, [Part(kind, "fix")])
    return t.thread_id


def _done(store, tid):
    store.set_state(tid, "done", by="coordinator", token=_token(store))


class Signer:
    """A verifier that signs whatever it is shown and remembers it."""

    def __init__(self, fp="SHA256:abc"):
        self.fp, self.seen = fp, []

    def __call__(self, message):
        self.seen.append(message)
        return self.fp


def _ready_mission(store, n=2):
    ids = [_work(store, f"fix {i}") for i in range(n)]
    for t in ids:
        _done(store, t)
    return missions.create_mission(store, ids), ids


def test_a_mission_groups_work_threads_and_each_regroup_is_a_revision(store):
    a, b, c = _work(store, "a"), _work(store, "b"), _work(store, "c")
    m = missions.create_mission(store, [b, a])
    assert m.rev == 1 and m.children == tuple(sorted([a, b])) and m.acceptance == "not_ready"
    m2 = missions.attach(store, m.mission_id, c)
    assert m2.rev == 2 and m2.children_sha256 != m.children_sha256
    m3 = missions.detach(store, m.mission_id, a)
    assert m3.rev == 3 and a not in m3.children
    assert [x.mission_id for x in missions.list_missions(store)] == [m.mission_id]


def test_only_work_threads_and_one_mission_per_thread(store):
    ask = _work(store, "what is x?", kind="ask")
    w = _work(store)
    with pytest.raises(CoordError, match="not a work thread"):
        missions.create_mission(store, [ask])
    m = missions.create_mission(store, [w])
    with pytest.raises(CoordError, match="already in mission"):
        missions.create_mission(store, [w])
    with pytest.raises(CoordError, match="never left with no thread"):
        missions.detach(store, m.mission_id, w)
    with pytest.raises(CoordError, match="no mission"):
        missions.get_mission(store, "ms-" + "0" * 32)


def test_a_coordinator_write_needs_its_lease_and_an_operator_write_none(store):
    w = _work(store)
    with pytest.raises(CoordError):
        missions.create_mission(store, [w], by="coordinator", token=999)
    assert missions.list_missions(store) == []
    assert missions.create_mission(store, [w], by="coordinator", token=_token(store)).rev == 1


def test_accepting_signs_the_frozen_snapshot_and_reads_back_signed(store):
    m, ids = _ready_mission(store)
    snap = missions.snapshot_of(store, m.mission_id)
    assert snap.ready and snap.unacked_item_ids == []
    assert [c["thread_id"] for c in snap.body["children"]] == sorted(ids)
    assert all(c["terminal_event_seq"] is not None for c in snap.body["children"])
    assert missions.get_mission(store, m.mission_id).acceptance == "ready"
    v = Signer()
    acc = missions.accept(store, m.mission_id, snapshot_sha256=snap.sha256, sign_request_id="sr-1", verify=v)
    assert v.seen == [missions.accept_message(m.mission_id, snap)] and snap.sha256 in v.seen[0]
    assert acc.signer_fp == "SHA256:abc" and acc.snapshot_sha256 == snap.sha256
    got = missions.get_mission(store, m.mission_id)
    assert got.acceptance == "signed" and got.accepted == acc


def test_a_child_not_done_is_not_ready(store):
    done, open_ = _work(store, "a"), _work(store, "b")
    _done(store, done)
    m = missions.create_mission(store, [done, open_])
    snap = missions.snapshot_of(store, m.mission_id)
    assert not snap.ready and m.acceptance == "not_ready"
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=snap.sha256, sign_request_id="sr-1", verify=Signer())
    assert e.value.why == "not_ready"


def test_an_unacked_know_blocks_acceptance_until_it_is_cleared(store):
    m, ids = _ready_mission(store)
    item, _ = review.file_know(store, ids[0], "the fix touches two files", token=_token(store))
    snap = missions.snapshot_of(store, m.mission_id)
    assert snap.unacked_item_ids == [item.item_id]
    assert missions.get_mission(store, m.mission_id).acceptance == "not_ready"
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=snap.sha256, sign_request_id="sr-1", verify=Signer())
    assert e.value.why == "unacked"
    review.clear_item(store, item.item_id)                      # the ACK is the review clear
    after = missions.snapshot_of(store, m.mission_id)
    assert after.unacked_item_ids == [] and after.sha256 != snap.sha256     # the clear moved the watermark
    missions.accept(store, m.mission_id, snapshot_sha256=after.sha256, sign_request_id="sr-1", verify=Signer())
    review.reopen_item(store, item.item_id)                     # a reopened KNOW un-gates nothing already signed...
    assert missions.get_mission(store, m.mission_id).acceptance == "stale"      # ...it makes the signature stale


def test_a_know_never_blocks_the_work_only_the_acceptance(store):
    w = _work(store)
    review.file_know(store, w, "fyi", token=_token(store))
    _done(store, w)                                              # the leg still completes with a KNOW open
    assert store.thread(w).state == "done"


def test_a_signature_over_a_render_before_a_know_accepts_nothing(store):
    m, ids = _ready_mission(store)
    rendered = missions.snapshot_of(store, m.mission_id)
    item, _ = review.file_know(store, ids[0], "arrived after render", token=_token(store))
    review.clear_item(store, item.item_id)                      # cleared, so nothing is un-ACKed: only the watermark moved
    now = missions.snapshot_of(store, m.mission_id)
    assert now.unacked_item_ids == [] and now.body["review_watermark_gseq"] > rendered.body["review_watermark_gseq"]
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=rendered.sha256, sign_request_id="sr-1", verify=Signer())
    assert e.value.why == "stale"
    assert missions.get_mission(store, m.mission_id).accepted is None


def test_a_child_attached_after_render_refuses_the_close(store):
    m, ids = _ready_mission(store)
    rendered = missions.snapshot_of(store, m.mission_id)
    extra = _work(store, "late")
    _done(store, extra)
    missions.attach(store, m.mission_id, extra)
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=rendered.sha256, sign_request_id="sr-1", verify=Signer())
    assert e.value.why == "stale"


def test_reopening_a_child_makes_the_signed_acceptance_stale_and_the_new_card_differs(store):
    m, ids = _ready_mission(store)
    s1 = missions.snapshot_of(store, m.mission_id)
    missions.accept(store, m.mission_id, snapshot_sha256=s1.sha256, sign_request_id="sr-1", verify=Signer())
    store.set_state(ids[0], "open", by="operator", cause="reopen")
    assert missions.get_mission(store, m.mission_id).acceptance == "stale"
    _done(store, ids[0])
    s2 = missions.snapshot_of(store, m.mission_id)
    assert s2.sha256 != s1.sha256                               # the terminal event moved though the state is done again
    missions.accept(store, m.mission_id, snapshot_sha256=s2.sha256, sign_request_id="sr-2", verify=Signer())
    assert missions.get_mission(store, m.mission_id).acceptance == "signed"


def test_one_signature_accepts_once_and_a_refused_verifier_writes_nothing(store):
    m, _ids = _ready_mission(store)
    s = missions.snapshot_of(store, m.mission_id)
    missions.accept(store, m.mission_id, snapshot_sha256=s.sha256, sign_request_id="sr-1", verify=Signer())
    m2, _ = _ready_mission(store)
    s2 = missions.snapshot_of(store, m2.mission_id)
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m2.mission_id, snapshot_sha256=s2.sha256, sign_request_id="sr-1", verify=Signer())
    assert e.value.why == "replay"

    def refuse(_message):
        raise AcceptanceRefused("signature", "denied")

    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m2.mission_id, snapshot_sha256=s2.sha256, sign_request_id="sr-9", verify=refuse)
    assert e.value.why == "signature"
    with pytest.raises(AcceptanceRefused):
        missions.accept(store, m2.mission_id, snapshot_sha256=s2.sha256, sign_request_id="sr-8", verify=lambda _m: "")
    n = sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM mission_acceptances").fetchone()[0]
    assert n == 1


def test_mission_rows_are_append_only_and_revisions_never_skip(store):
    m, _ = _ready_mission(store)
    c = sqlite3.connect(store.path)
    for sql in ("DELETE FROM missions", "UPDATE mission_revisions SET rev = 9",
                "INSERT INTO mission_revisions (mission_id, rev, children, children_sha256, cause, by, at) "
                f"VALUES ('{m.mission_id}', 5, '[]', 'x', 'attach', 'operator', 'now')"):
        with pytest.raises(sqlite3.DatabaseError):
            c.execute(sql)
    c.close()


def test_the_evidence_digest_moves_when_a_worker_writes_a_know(store):
    m, ids = _ready_mission(store)
    s1 = missions.snapshot_of(store, m.mission_id)
    review.file_know(store, ids[0], "tests pass", token=_token(store))
    s2 = missions.snapshot_of(store, m.mission_id)
    assert len(s2.body["evidence_digests"]) == len(s1.body["evidence_digests"]) + 1


# ---------------------------------------------------------------------------------------------- L3 round 1 fixes
def _pre_missions_store(directory, monkeypatch):
    """A coordinator store made before the mission tables existed: the store is made with MISSION_SCHEMA empty."""
    directory.mkdir(mode=0o700)
    with monkeypatch.context() as m:
        m.setattr(missions, "MISSION_SCHEMA", ())
        old = CoordStore(directory, clock=Clock())
        tid = _work(old)
        item, _ = review.file_know(old, tid, "made before missions", token=_token(old))
    c = sqlite3.connect(directory / "dispatch.db")
    names = {r[0] for r in c.execute("SELECT name FROM sqlite_master")}
    c.close()
    assert not names & {"missions", "mission_revisions", "mission_acceptances"} and "review_items" in names
    return tid, item.item_id


def test_a_store_made_before_missions_is_still_read_only_current_and_loses_nothing(tmp_path, monkeypatch):
    d = tmp_path / "dispatch"
    tid, item_id = _pre_missions_store(d, monkeypatch)
    store = CoordStore(d, clock=Clock())
    store.open_readonly().close()                                    # not StoreAbsent
    assert [i.item_id for i in review.list_items(store)] == [item_id]
    c = sqlite3.connect(d / "dispatch.db")
    assert "missions" not in {r[0] for r in c.execute("SELECT name FROM sqlite_master")}    # a reader made nothing
    c.close()
    m = missions.create_mission(store, [tid])                        # the first mission write migrates it
    other = CoordStore(d, clock=Clock())
    assert [x.mission_id for x in missions.list_missions(other)] == [m.mission_id]
    assert [i.item_id for i in review.list_items(other)] == [item_id] and other.thread(tid).state == "open"
    c = sqlite3.connect(d / "dispatch.db")
    assert {"missions", "mission_revisions", "mission_acceptances"} <= {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master")}
    c.close()


def _env_mission(tmp_path):
    """A mission over one thread bound to an envelope, in an EnvelopeStore (the bind row is written directly: the
    signed envelope path is not what is under test)."""
    from levain.coordinator.envelopes import EnvelopeStore
    from levain.coordinator.store import next_gseq

    d = tmp_path / "env"
    d.mkdir(mode=0o700)
    store = EnvelopeStore(d, clock=Clock())
    tid = _work(store)
    _done(store, tid)
    drop_id = store.thread(tid).members[0]
    with store._write("operator", None) as (conn, now):
        conn.execute("INSERT INTO envelopes (envelope_id, gseq, origin, drop_id, body, digest, payload, signature, "
                     "signer_fp, signer_class, supersedes, stop_at, accepted_at) VALUES ('env-1', ?, 'structured', ?, "
                     "'{}', 'd', '{}', 's', 'fp', 'operator', NULL, ?, ?)", (next_gseq(conn, now), drop_id, now, now))
        conn.execute("INSERT INTO envelope_binds (envelope_id, thread_id, rev, gseq, at) VALUES ('env-1', ?, 1, ?, ?)",
                     (tid, next_gseq(conn, now), now))
    return store, missions.create_mission(store, [tid])


def test_an_envelope_wide_attenuation_after_render_moves_the_snapshot(tmp_path):
    store, m = _env_mission(tmp_path)
    rendered = missions.snapshot_of(store, m.mission_id)
    assert rendered.body["children"][0]["envelope_chain_head"] == "env-1"
    with store._write("operator", None) as (conn, now):                 # thread_id NULL: it applies to every bound thread
        from levain.coordinator.store import next_gseq
        conn.execute("INSERT INTO envelope_attenuations (envelope_id, thread_id, kind, value, by, gseq, at) VALUES "
                     "('env-1', NULL, 'kill', '', 'operator', ?, ?)", (next_gseq(conn, now), now))
    now_snap = missions.snapshot_of(store, m.mission_id)
    assert now_snap.body["review_watermark_gseq"] > rendered.body["review_watermark_gseq"]
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=rendered.sha256, sign_request_id="sr-1", verify=Signer())
    assert e.value.why == "stale"


def test_a_stale_digest_is_refused_before_anything_is_asked_to_verify_it(store):
    m, ids = _ready_mission(store)
    rendered = missions.snapshot_of(store, m.mission_id)
    extra = _work(store, "late")
    _done(store, extra)
    missions.attach(store, m.mission_id, extra)                          # revision 2: the old digest was never signed
    v = Signer()
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=rendered.sha256, sign_request_id="sr-1", verify=v)
    assert e.value.why == "stale" and v.seen == []                      # no message nobody signed was ever built


def test_the_same_signature_cannot_accept_twice_under_a_new_sign_request_id(store):
    m, _ = _ready_mission(store)
    s = missions.snapshot_of(store, m.mission_id)
    missions.accept(store, m.mission_id, snapshot_sha256=s.sha256, sign_request_id="sr-1", verify=Signer())
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m.mission_id, snapshot_sha256=s.sha256, sign_request_id="sr-2", verify=Signer())
    assert e.value.why == "replay"
    # a verifier that hands back the signature bytes binds to them: the same bytes under a new id is a replay too
    m2, _ = _ready_mission(store)
    s2 = missions.snapshot_of(store, m2.mission_id)
    missions.accept(store, m2.mission_id, snapshot_sha256=s2.sha256, sign_request_id="sr-3",
                    verify=lambda _m: ("SHA256:abc", b"sig-bytes"))
    m3, _ = _ready_mission(store)
    s3 = missions.snapshot_of(store, m3.mission_id)
    with pytest.raises(AcceptanceRefused) as e:
        missions.accept(store, m3.mission_id, snapshot_sha256=s3.sha256, sign_request_id="sr-4",
                        verify=lambda _m: ("SHA256:abc", b"sig-bytes"))
    assert e.value.why == "replay"
    n = sqlite3.connect(store.path).execute("SELECT COUNT(*) FROM mission_acceptances").fetchone()[0]
    assert n == 2



# ---------------------------------------------------------------------------------------------- land-prep nits
def _mission_tables(directory):
    c = sqlite3.connect(directory / "dispatch.db")
    try:
        return {r[0] for r in c.execute("SELECT name FROM sqlite_master")} & {
            "missions", "mission_revisions", "mission_acceptances"}
    finally:
        c.close()


def test_a_mission_read_on_a_pre_mission_store_makes_no_tables_and_reads_empty(tmp_path, monkeypatch):
    d = tmp_path / "dispatch"
    tid, _ = _pre_missions_store(d, monkeypatch)
    store = CoordStore(d, clock=Clock())
    assert missions.list_missions(store) == []
    assert _mission_tables(d) == set()                                # a read creates nothing
    with pytest.raises(CoordError):
        missions.get_mission(store, "mission-none")
    with pytest.raises(CoordError):
        missions.snapshot_of(store, "mission-none")
    assert _mission_tables(d) == set()
    missions.create_mission(store, [tid])                             # the first write still migrates
    assert _mission_tables(d) == {"missions", "mission_revisions", "mission_acceptances"}


def test_the_watermark_survives_a_store_with_attenuations_but_no_binds(store):
    tid = _work(store)
    conn = sqlite3.connect(":memory:")
    for ddl in ("CREATE TABLE replies (gseq INT, thread_id TEXT)", "CREATE TABLE review_items (gseq INT, thread_id TEXT, "
                "item_id TEXT)", "CREATE TABLE review_events (gseq INT, item_id TEXT)",
                "CREATE TABLE envelope_attenuations (gseq INT, thread_id TEXT, envelope_id TEXT)"):
        conn.execute(ddl)
    assert missions._watermark(conn, [tid]) == 0
