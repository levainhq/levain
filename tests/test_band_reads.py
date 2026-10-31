"""W2-A: the band server contract in band_view (the three reads, one identity). Each test is built from a failure
reproduced first on the pre-W2 code (1030-34 w2a first spend): a version-less answer after the item moved elsewhere, two
writers on one row, a ``band_payload`` that named the newest view of the credential instead of its own, a dedupe decided
before the write lock, a coordinator SIGN that changed an item's identity."""
from __future__ import annotations

import contextlib
import sqlite3
import threading

import pytest

from levain import band_view
from levain.band_sources import Extra, Extras
from levain.consent.questions import Answer, Link, Question, QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]
VIEWER = "cockpit:browser"


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return QuestionStore(d)


def ask(store, what="Which?", **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp", blocks="the PR",
              cwd="/tmp/proj")
    kw.update(over)
    return store.file(**kw)


def counts(store):
    with sqlite3.connect(store.path) as c:
        return tuple(c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                     for t in ("answers", "answer_context", "band_views"))


def answer(store, subject, pick="a", text=None):
    return band_view.answer_item(store, subject, pick=pick if text is None else None, text=text, channel="cockpit",
                                 viewer=VIEWER)


def subject_of(store, q):
    return band_view.subject_for(store, "question", q.id)


class Host:
    """A foreign store (flow's NeedsQuestions shape) that offers the versioned answer."""

    def __init__(self, rows, versions, *, raises=False):
        self.rows, self.versions, self.raises = rows, versions, raises
        self.answered = []

    def open_questions(self, agent=None):
        return [q for q in self.rows if q.answer is None]

    def recent(self, limit=50):
        return list(self.rows)

    def get(self, ref):
        return next((q for q in self.rows if str(ref) in (str(q.n), q.id)), None)

    def answer(self, ref, *, option=None, text=None, channel):
        raise AssertionError("the unversioned answer is never called")

    def question_versions(self, qs):
        if self.raises:
            raise RuntimeError("sign log unreadable")
        return {q.id: self.versions.get(q.id) for q in qs}

    def answer_versioned(self, question_id, version, *, option=None, text=None, channel):
        self.answered.append((question_id, version, option, text, channel))
        return Answer(question_id, "option", option or "", channel, "2026-10-30T10:05:00Z")


def fq(n, qid, **over):
    kw = dict(n=n, id=qid, agent="desk", agent_session_id="-", what=f"repo: question {n}",
              options=(("a", "yes"), ("b", "no")), rec="a", blocks="the lane", cwd="/tmp/x",
              filed_at="2026-10-30T10:00:00Z", answer=None)
    kw.update(over)
    return Question(**kw)


def test_n3_a_wrong_or_moved_version_is_409_and_writes_nothing(store):
    moved, other = ask(store, "first?"), ask(store, "second?")
    s_moved, s_other = subject_of(store, moved), subject_of(store, other)
    store.answer(moved.id, option="fix", channel="cli")                  # answered elsewhere after the card drew it
    before = counts(store)
    with pytest.raises(band_view.VersionMoved):
        answer(store, s_moved)
    with pytest.raises(band_view.VersionMoved):
        answer(store, {**s_other, "version": "0" * 16})
    assert counts(store) == before and store.get(other.id).answer is None


def test_n5_an_authority_item_never_reaches_an_unsigned_answer(store, monkeypatch):
    linked = ask(store, "push the fix?", link=Link("fix", "push", "req-1", "qid-1"))
    consent = Extra(source="consent", ref="req-9", filed="2026-10-30T10:00:00Z", tag="t", what="Sign?",
                    options=("allow", "deny"), rec="none", rec_letter=None, blocks="b", origin="o")
    monkeypatch.setattr(band_view, "read_extras", lambda *a, **k: Extras(items=[consent], complete={"consent"}))
    p = band_view.band_payload(store, VIEWER)
    assert [i["kind"] for i in p["items"]] == ["authority", "authority"]
    assert all(a["do"] == "sign" and "verb" not in a for i in p["items"] for a in i["actions"])
    before = counts(store)
    s_linked = next(i["subject"] for i in p["items"] if i["question"] == linked.n)
    with pytest.raises(band_view.AuthorityItem):                         # its CURRENT subject: only the kind refuses it
        answer(store, s_linked)
    s_consent = next(i["subject"] for i in p["items"] if i["question"] is None)
    with pytest.raises(band_view.AuthorityItem):
        answer(store, s_consent)
    assert counts(store) == before and store.get(linked.id).answer is None
    # a foreign host that gives None for a row: no answer action, and its write is never called
    host = Host([fq(7, "np-1", sign_request="rq-1"), fq(8, "np-2")], {"np-2": None})
    fp = band_view.band_payload(host, VIEWER)
    assert [i["kind"] for i in fp["items"]] == ["authority", "information"]
    assert all(i["actions"] == [] or i["actions"][0]["do"] == "sign" for i in fp["items"])
    with pytest.raises(band_view.VersionMoved):
        answer(host, {"kind": "question", "id": "np-2", "version": "anything"})
    assert host.answered == []


def test_n6_two_concurrent_answers_one_wins_and_the_stored_value_is_a_key(store):
    q = ask(store)
    subject = subject_of(store, q)
    barrier, out = threading.Barrier(2), {}

    def go(pick):
        barrier.wait()
        try:
            out[pick] = answer(store, subject, pick=pick)
        except band_view.VersionMoved as exc:
            out[pick] = exc

    ts = [threading.Thread(target=go, args=(p,)) for p in ("a", "c")]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(type(v).__name__ for v in out.values()) == ["Applied", "VersionMoved"]
    with sqlite3.connect(store.path) as c:
        rows = c.execute("SELECT value FROM answers WHERE question_id = ?", (q.id,)).fetchall()
    assert len(rows) == 1 and rows[0][0] in ("fix", "none")              # a stored key, never the letter


def test_n8_a_sign_that_enriches_a_consent_keeps_its_key_and_number(store, monkeypatch):
    base = dict(filed="2026-10-30T10:00:00Z", tag="t", what="Sign?", options=("allow", "deny"), rec="none",
                rec_letter=None, blocks="b", origin="o")
    undecided = Extra(source="consent", ref="req-9", **base)
    signed = Extra(source="sign", ref="rp-1", key=("consent", "req-9"), **base,
                   data={"request_id": "req-9", "menu": (("sign", None), ("deny", None)), "version": "f" * 64})
    seen = {}
    for name, e in (("undecided", undecided), ("signed", signed)):
        monkeypatch.setattr(band_view, "read_extras", lambda *a, e=e, **k: Extras(items=[e], complete={"consent", "sign"}))
        seen[name] = band_view.band_payload(store, VIEWER)
    a, b = (seen[k]["items"][0] for k in ("undecided", "signed"))
    assert (a["subject"]["kind"], a["subject"]["id"]) == (b["subject"]["kind"], b["subject"]["id"]) == ("consent", "req-9")
    assert a["n"] == b["n"] == 1
    assert a["subject"]["version"] != b["subject"]["version"] and seen["undecided"]["queue"] != seen["signed"]["queue"]


def test_n9_the_dedupe_is_decided_inside_the_write(store, monkeypatch):
    ask(store)
    for gen_b in (111, 222):                                           # equal and unequal generations
        viewer = f"v{gen_b}"
        a_done, real, got = threading.Event(), store._write_by, {}

        @contextlib.contextmanager
        def gated(deadline, real=real, a_done=a_done):
            if threading.current_thread().name == "B":
                assert a_done.wait(10)                                  # B read nothing yet that A's commit can change
            with real(deadline) as conn:
                yield conn
        monkeypatch.setattr(store, "_write_by", gated)

        def run(name, gen):
            got[name] = band_view.record_view(store, viewer, 1, [1], gen=gen, dedupe=True)
            if name == "A":
                a_done.set()
        tb = threading.Thread(target=run, args=("B", gen_b), name="B")
        tb.start()
        run("A", 111)
        tb.join()
        with sqlite3.connect(store.path) as c:
            rows = c.execute("SELECT seq FROM band_views WHERE viewer = ?", (viewer,)).fetchall()
        assert len(rows) == 1 and got["A"] == got["B"] == rows[0][0]
        monkeypatch.undo()
    assert isinstance(band_view.record_view(store, "empty", 0, []), int)    # an empty first view is an insert


def test_n10_the_items_and_the_queue_are_one_read(store, monkeypatch):
    ask(store, "one?")
    second = ask(store, "two?")
    real, state = band_view._read, {"hit": False}

    def read_then_answer(s):
        got = real(s)
        if not state["hit"]:
            state["hit"] = True
            s.answer(second.id, option="fix", channel="cli")             # answered after the snapshot read it open
        return got
    monkeypatch.setattr(band_view, "_read", read_then_answer)
    p = band_view.band_payload(store, VIEWER)
    assert [i["question"] for i in p["items"]] == [1, 2]                 # the one snapshot lists both
    assert p["queue"] == band_view._queue_of([({}, i) for i in p["items"]])   # and the queue names exactly those


def test_n13_a_response_carries_the_view_it_recorded_not_the_newest(store, monkeypatch):
    ask(store, "one?")
    real, in_a, go_on, rec = band_view.record_view, threading.Event(), threading.Event(), {}

    def held(*a, **k):
        r = real(*a, **k)
        if threading.current_thread().name == "A":
            rec["A"] = r
            in_a.set()
            assert go_on.wait(10)
        return r
    monkeypatch.setattr(band_view, "record_view", held)
    res = {}
    ta = threading.Thread(target=lambda: res.setdefault("A", band_view.band_payload(store, VIEWER)), name="A")
    ta.start()
    assert in_a.wait(10)
    ask(store, "two?")                                                   # tab B sees a newer queue and records it
    pb = band_view.band_payload(store, VIEWER)
    go_on.set()
    ta.join()
    assert pb["view_id"] != rec["A"] and res["A"]["view_id"] == rec["A"]
    assert [i["n"] for i in res["A"]["items"]] == [1] and [i["n"] for i in pb["items"]] == [1, 2]


def test_n14_both_reads_give_one_queue_for_one_state_native_and_foreign(store):
    ask(store, "one?")
    ask(store, "two?", link=Link("fix", "push", "req-1", "qid-1"))
    assert band_view.summary(store)["queue"] == band_view.band_payload(store, VIEWER)["queue"]
    rows = [fq(7, "np-1"), fq(8, "np-2")]
    ok = Host(rows, {"np-1": "a" * 64, "np-2": None})
    s, p = band_view.summary(ok), band_view.band_payload(ok, VIEWER)
    assert s["queue"] == p["queue"] and s["note"] == "" and not s["read_only"]
    assert [i["actions"] for i in p["items"]] == [[{"do": "answer", "verb": "item_answer"}], []]
    bad = Host(rows, {}, raises=True)
    sb, pb = band_view.summary(bad), band_view.band_payload(bad, VIEWER)
    assert sb["queue"] == pb["queue"] and sb["note"] and pb["note"]
    assert all(i["subject"]["version"] is None and i["actions"] == [] for i in pb["items"])


def test_golden_one_row_has_one_version_on_every_path_answered_marker_included(store):
    q = ask(store)
    with sqlite3.connect(store.path) as c:
        c.row_factory = None
        via_conn = QuestionStore._by_ref(c, q.id)
    assert band_view.subject_version(via_conn) == band_view.subject_version(store.get(q.id)) == subject_of(store, q)["version"]
    store.answer(q.id, option="fix", channel="cli")
    with sqlite3.connect(store.path) as c:
        answered = QuestionStore._by_ref(c, q.id)
    assert answered.answer is not None
    assert band_view.subject_version(answered) == band_view.subject_version(store.get(q.id))
    assert band_view.subject_version(answered) != band_view.subject_version(via_conn)


# --- L3 r1 fixes (1030-34-w2a r1): each from the failing case, run failing first ------------------------------------
def test_r1_f1_a_foreign_authority_or_answered_row_never_reaches_the_host_write():
    signed, linked = fq(7, "np-1", sign_request="rq-1"), fq(8, "np-2", link=Link("a", "push", "rq-2", "np-2"))
    done = fq(9, "np-3", answer=Answer("np-3", "option", "a", "cli", "2026-10-30T10:01:00Z"))
    host = Host([signed, linked, done], {"np-1": "v1", "np-2": "v2", "np-3": "v3"})   # a host that versions them all
    for qid, v in (("np-1", "v1"), ("np-2", "v2")):
        with pytest.raises(band_view.AuthorityItem):
            answer(host, {"kind": "question", "id": qid, "version": v})
    with pytest.raises(band_view.VersionMoved):
        answer(host, {"kind": "question", "id": "np-3", "version": "v3"})
    # L3 r2: flow's host gives a signed row no version; the refusal still names the authority, not a move
    quiet = Host([signed], {})
    with pytest.raises(band_view.AuthorityItem):
        answer(quiet, {"kind": "question", "id": "np-1", "version": "v1"})
    assert host.answered == [] and quiet.answered == []


def test_r1_f2_both_or_neither_of_pick_and_text_is_refused_before_any_store_access(store):
    q = ask(store)
    s = subject_of(store, q)
    before = counts(store)
    for pick, text in (("a", "override"), (None, None)):
        with pytest.raises(ValueError):
            band_view.answer_item(store, s, pick=pick, text=text, channel="cockpit", viewer=VIEWER)
    assert counts(store) == before and store.get(q.id).answer is None


def test_r1_f3_the_summary_says_number_pending_as_the_payload_does(store, monkeypatch):
    ask(store)
    monkeypatch.setattr(band_view, "_number", lambda *a, **k: {})        # the lock was busy: nothing numbered
    assert band_view.summary(store)["note"] == band_view._PENDING_NOTE


def test_r1_f4_an_empty_view_skip_binds_a_row_with_the_calls_mark(store):
    store.answer(ask(store).id, option="fix", channel="cli")             # a store with an id and nothing open
    first = band_view.record_view(store, "v", 1, [])
    second = band_view.record_view(store, "v", 2, [])
    third = band_view.record_view(store, "v", 2, [])
    with sqlite3.connect(store.path) as c:
        marks = dict(c.execute("SELECT seq, seen_through FROM band_views WHERE viewer = 'v'").fetchall())
    assert marks[first] == 1 and marks[second] == 2 and third == second


def test_r1_f5_a_foreign_row_with_no_number_reads_without_a_crash():
    host = Host([fq(None, "np-1"), fq(4, "np-2")], {"np-1": "v1", "np-2": "v2"})
    assert band_view.summary(host)["count"] == 2
    p = band_view.band_payload(host, VIEWER)
    assert p["seen_through"] == 4 and len(p["items"]) == 2


def test_r1_f7_a_held_write_lock_refuses_the_answer_within_the_bound(store):
    import time
    q = ask(store)
    s = subject_of(store, q)
    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        with pytest.raises(sqlite3.OperationalError):
            answer(store, s)
        assert time.monotonic() - t0 < band_view.HOOK_BUSY_MS / 1000 + 3
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert store.get(q.id).answer is None
