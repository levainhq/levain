"""The question band store: filing, answering once, claim-once delivery, append-only tables."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.cockpit.consent import ConsentError
from levain.consent.questions import DB_NAME, FORMAT, QuestionStore, QuestionStoreError

OPTS = [("a", "Do the first"), ("b", "Do the second")]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 18, 12, 0, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int = 1) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> QuestionStore:
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return QuestionStore(d, clock=clock)


def ask(store: QuestionStore, **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what="Which way?", options=OPTS, rec="a",
              blocks="the migration", cwd="/tmp/proj")
    kw.update(over)
    return store.file(**kw)


def raw(store: QuestionStore) -> sqlite3.Connection:
    return sqlite3.connect(store.path)


# --- validation ---------------------------------------------------------------------------------------
@pytest.mark.parametrize("over", [
    dict(agent="nobody"),
    dict(agent_session_id=""),
    dict(agent_session_id="x" * 257),
    dict(agent_session_id="a\nb"),
    dict(cwd=""),
    dict(cwd="relative/path"),
    dict(what=""),
    dict(what="   "),
    dict(what="two\nlines"),
    dict(what="tab\there"),
    dict(what="x" * 1001),
    dict(options=[("a", "only one")]),
    dict(options=[(f"k{i}", "l") for i in range(7)]),
    dict(options=[("a", "x"), ("a", "y")]),
    dict(options=[("A", "x"), ("b", "y")]),
    dict(options=[("-a", "x"), ("b", "y")]),
    dict(options=[("a" * 17, "x"), ("b", "y")]),
    dict(options=[("a", ""), ("b", "y")]),
    dict(options=[("a", "x\ny"), ("b", "y")]),
    dict(options=[("a", "x" * 601), ("b", "y")]),
    dict(options=[{"key": "a", "label": "x", "extra": 1}, ("b", "y")]),
    dict(options="ab"),
    dict(rec="zzz"),
    dict(rec=""),
    dict(rec=None),
    dict(blocks=""),
    dict(blocks=None),
    dict(blocks="x" * 6001),
    dict(blocks="a\nb"),
    dict(resume=["not", "an", "object"]),
    dict(resume={"x": object()}),
])
def test_file_refusals_write_nothing(store, over):
    with pytest.raises(ConsentError):
        ask(store, **over)
    assert store.open_questions() == []


def test_a_whole_analysis_files_whole(store):
    blocks, label = "b" * 6000, "l" * 600
    q = ask(store, blocks=blocks, options=[("a", label), ("b", "y")])
    assert q.blocks == blocks and q.options[0][1] == label


def test_missing_rec_and_blocks_refused(store):
    with pytest.raises(TypeError):
        store.file(agent="claude_code", agent_session_id="s", what="w", options=OPTS, cwd="/tmp")


def test_file_numbers_and_normalises(store):
    q1 = ask(store, options=[{"key": "a", "label": "A"}, ["b", "B"]], resume={"permission_mode": "default"})
    q2 = ask(store)
    assert (q1.n, q2.n) == (1, 2)
    assert q1.options == (("a", "A"), ("b", "B"))
    assert q1.id.startswith("q-") and len(q1.id) == 2 + 24
    assert q1.filed_at == "2026-10-18T12:00:00Z" or q1.filed_at.startswith("2026-10-18")
    assert q1.answer is None


def test_get_by_every_ref(store):
    q = ask(store)
    ask(store)
    for ref in (q.id, 1, "1", "#1", " #1 "):
        assert store.get(ref) == q
    assert store.get(2).n == 2
    for ref in (99, "q-nope", "x", "#", True, None):
        assert store.get(ref) is None


# --- answering ----------------------------------------------------------------------------------------
def test_answer_by_option(store, clock):
    q = ask(store)
    a = store.answer(q.id, option="b", channel="phone")
    assert (a.kind, a.value, a.channel, a.question_id) == ("option", "b", "phone", q.id)
    assert store.get(q.id).answer == a
    assert store.open_questions() == []


def test_answer_by_text_round_trips_verbatim(store):
    q = ask(store)
    text = '  "quote" and\nnewline\t\'x\'  '
    store.answer("#1", text=text, channel="cli")
    assert store.get(q.n).answer.value == text
    assert store.get(q.n).answer.kind == "text"


def test_second_answer_refused(store):
    q = ask(store)
    a = store.answer(q.id, option="a", channel="cli")
    with pytest.raises(ConsentError, match=rf"question #1 was already answered at {a.answered_at}"):
        store.answer(q.id, text="again", channel="cli")


def test_answer_refusals(store):
    q = ask(store)
    with pytest.raises(ConsentError):
        store.answer("q-unknown", option="a", channel="cli")
    with pytest.raises(ConsentError):
        store.answer(99, option="a", channel="cli")
    with pytest.raises(ConsentError):
        store.answer(q.id, option="zzz", channel="cli")
    with pytest.raises(ConsentError):
        store.answer(q.id, option="a", text="x", channel="cli")
    with pytest.raises(ConsentError):
        store.answer(q.id, channel="cli")
    with pytest.raises(ConsentError):
        store.answer(q.id, option="a", channel="email")
    with pytest.raises(ConsentError):
        store.answer(q.id, text="", channel="cli")
    with pytest.raises(ConsentError):
        store.answer(q.id, text="nul\x00", channel="cli")
    with pytest.raises(ConsentError):
        store.answer(q.id, text="x" * 4001, channel="cli")
    assert store.get(q.id).answer is None


def test_open_questions_and_recent(store):
    q1, q2, q3 = ask(store), ask(store, agent="codex"), ask(store)
    store.answer(q2.id, option="a", channel="cli")
    assert [q.n for q in store.open_questions()] == [1, 3]
    assert [q.n for q in store.open_questions("claude_code")] == [1, 3]
    assert store.open_questions("codex") == []
    assert [q.n for q in store.recent()] == [3, 2, 1]
    assert [q.n for q in store.recent(limit=2)] == [3, 2]
    assert q1.n == 1 and q3.n == 3


# --- delivery -----------------------------------------------------------------------------------------
def test_claim_returns_once(store):
    q = ask(store)
    assert store.claim_answers_for("claude_code", "sess-1") == []         # unanswered
    store.answer(q.id, text="go left", channel="cli")
    got = store.claim_answers_for("claude_code", "sess-1")
    assert [g.id for g in got] == [q.id] and got[0].answer.value == "go left"
    assert store.claim_answers_for("claude_code", "sess-1") == []
    state, when = store.delivery_state(q.id)
    assert state == "delivered" and when is not None


def test_claim_is_per_exact_agent_and_session(store):
    q = ask(store)
    store.answer(q.id, option="a", channel="cli")
    assert store.claim_answers_for("codex", "sess-1") == []
    assert store.claim_answers_for("claude_code", "sess-2") == []
    assert store.claim_answers_for("claude_code", "sess-1x") == []
    assert store.delivery_state(q.id) == ("undelivered", None)
    assert len(store.claim_answers_for("claude_code", "sess-1")) == 1


def test_claim_oldest_answer_first(store, clock):
    q1, q2 = ask(store), ask(store)
    store.answer(q2.id, option="a", channel="cli")
    clock.advance()
    store.answer(q1.id, option="b", channel="cli")
    assert [q.id for q in store.claim_answers_for("claude_code", "sess-1")] == [q2.id, q1.id]


def test_lease_is_unconfirmed_and_not_reclaimed(store):
    q = ask(store)
    store.answer(q.id, option="a", channel="cli")
    assert [x.id for x in store.lease_answers_for("claude_code", "sess-1")] == [q.id]
    assert store.delivery_state(q.id)[0] == "unconfirmed"
    assert store.claim_answers_for("claude_code", "sess-1") == []
    assert store.lease_answers_for("claude_code", "sess-1") == []
    assert [x.id for x in store.undelivered()] == [q.id]


def test_settle_started_is_delivered(store):
    q = ask(store)
    store.answer(q.id, option="a", channel="cli")
    store.lease_answers_for("claude_code", "sess-1")
    store.settle_resume([q.id], agent_session_id="sess-1", outcome="started", argv=["claude", "-r"], cwd="/tmp")
    assert store.delivery_state(q.id)[0] == "delivered"
    assert store.undelivered() == []
    store.settle_resume([q.id], agent_session_id="sess-1", outcome="failed", argv=[], cwd="/tmp")   # already settled
    assert store.delivery_state(q.id)[0] == "delivered"


def test_settle_failed_is_claimable_again_by_a_hook(store):
    q = ask(store)
    store.answer(q.id, option="a", channel="cli")
    store.lease_answers_for("claude_code", "sess-1")
    store.settle_resume([q.id], agent_session_id="sess-1", outcome="failed", argv=["x"], cwd="/tmp", detail="boom")
    assert store.delivery_state(q.id)[0] == "failed"
    assert [x.id for x in store.undelivered()] == [q.id]
    assert [x.id for x in store.claim_answers_for("claude_code", "sess-1")] == [q.id]
    assert store.delivery_state(q.id)[0] == "delivered"
    assert store.claim_answers_for("claude_code", "sess-1") == []
    with raw(store) as c:
        steps = [r[0] for r in c.execute("SELECT outcome FROM answer_resume_steps ORDER BY seq")]
    assert steps == ["launching", "failed", "hook"]


def test_settle_bad_outcome_refused(store):
    with pytest.raises(ConsentError):
        store.settle_resume(["q-x"], agent_session_id="s", outcome="hook", argv=[], cwd="/tmp")


def test_undelivered_lists_never_claimed_failed_and_unconfirmed(store):
    q1, q2, q3, q4 = (ask(store, agent_session_id=f"s{i}") for i in range(4))
    for q in (q1, q2, q3, q4):
        if q is not q4:
            store.answer(q.id, option="a", channel="cli")
    store.lease_answers_for("claude_code", "s1")          # q2: unconfirmed
    store.lease_answers_for("claude_code", "s2")          # q3: failed
    store.settle_resume([q3.id], agent_session_id="s2", outcome="failed", argv=[], cwd="/tmp")
    assert [q.id for q in store.undelivered()] == [q1.id, q2.id, q3.id]
    store.claim_answers_for("claude_code", "s0")
    assert [q.id for q in store.undelivered()] == [q2.id, q3.id]


# --- triggers -----------------------------------------------------------------------------------------
def _populate(store):
    q = ask(store)
    store.answer(q.id, option="a", channel="cli")
    store.lease_answers_for("claude_code", "sess-1")
    return q


@pytest.mark.parametrize("table,col", [("questions", "what"), ("answers", "value"),
                                       ("answer_deliveries", "delivered_at"), ("answer_resume_steps", "detail")])
def test_update_and_delete_refused(store, table, col):
    _populate(store)
    with raw(store) as c:
        with pytest.raises(sqlite3.DatabaseError, match="append-only|once|never"):
            c.execute(f"UPDATE {table} SET {col} = 'x'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only|once|never"):
            c.execute(f"DELETE FROM {table}")


def test_insert_or_replace_refused(store):
    q = _populate(store)
    with raw(store) as c:
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            c.execute("INSERT OR REPLACE INTO questions (n, id, agent, agent_session_id, what, options, rec, blocks, "
                      "cwd, filed_at) VALUES (1, 'q-other', 'codex', 's', 'w', '[]', 'a', 'b', '/', 't')")
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            c.execute("INSERT OR REPLACE INTO questions (id, agent, agent_session_id, what, options, rec, blocks, "
                      "cwd, filed_at) VALUES (?, 'codex', 's', 'w', '[]', 'a', 'b', '/', 't')", (q.id,))
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            c.execute("INSERT OR REPLACE INTO answers (question_id, kind, value, channel, answered_at) "
                      "VALUES (?, 'text', 'forged', 'cli', 't')", (q.id,))
        with pytest.raises(sqlite3.DatabaseError, match="never replaced"):
            c.execute("INSERT OR REPLACE INTO answer_deliveries (question_id, agent_session_id, delivered_at) "
                      "VALUES (?, 's', 't')", (q.id,))


# --- the store itself ---------------------------------------------------------------------------------
def test_meta_and_modes(store):
    ask(store)
    assert store.store_id.startswith("qs-") and len(store.store_id) == 3 + 32
    with raw(store) as c:
        assert c.execute("SELECT value FROM meta WHERE key = 'format'").fetchone()[0] == FORMAT
    assert store.path.name == DB_NAME
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_directory_created_0700(tmp_path):
    s = QuestionStore(tmp_path / "new" / "consent")
    ask(s)
    assert (tmp_path / "new" / "consent").stat().st_mode & 0o777 == 0o700


def test_loose_directory_refused(tmp_path):
    d = tmp_path / "loose"
    d.mkdir()
    d.chmod(0o755)
    with pytest.raises(QuestionStoreError, match="0700"):
        ask(QuestionStore(d))


def test_symlink_and_file_directory_refused(tmp_path):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    with pytest.raises(QuestionStoreError):
        ask(QuestionStore(link))
    f = tmp_path / "afile"
    f.write_text("x")
    with pytest.raises((QuestionStoreError, OSError)):
        ask(QuestionStore(f))


def test_missing_store_reads_create_nothing(tmp_path):
    d = tmp_path / "absent"
    s = QuestionStore(d)
    assert s.claim_answers_for("claude_code", "s") == []
    assert s.lease_answers_for("claude_code", "s") == []
    assert s.undelivered() == [] and s.open_questions() == [] and s.recent() == []
    assert s.get(1) is None and s.delivery_state(1) == ("undelivered", None)
    assert not d.exists()
    d.mkdir(mode=0o700)
    assert s.claim_answers_for("claude_code", "s") == []
    assert not (d / DB_NAME).exists()


def test_foreign_db_refused(tmp_path):
    d = tmp_path / "c"
    d.mkdir(mode=0o700)
    with sqlite3.connect(d / DB_NAME) as c:
        c.execute("CREATE TABLE stuff (x)")
    with pytest.raises(QuestionStoreError, match="no question-store format marker"):
        ask(QuestionStore(d))


def test_other_format_refused(tmp_path):
    d = tmp_path / "c"
    d.mkdir(mode=0o700)
    with sqlite3.connect(d / DB_NAME) as c:
        c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        c.execute("INSERT INTO meta VALUES ('format', 'levain-questions/9')")
    with pytest.raises(QuestionStoreError, match="levain-questions/9"):
        ask(QuestionStore(d))


def test_l1_whitespace_only_text_is_refused_and_text_is_kept_unstripped(tmp_path):
    from levain.cockpit.consent import ConsentError
    from levain.consent.questions import QuestionStore

    s = QuestionStore(tmp_path / "c")
    q = s.file(agent="codex", agent_session_id="s", what="w?", options=[("a", "x"), ("b", "y")], rec="a",
               blocks="z", cwd="/w")
    with pytest.raises(ConsentError):
        s.answer(q.id, text="   \n ", channel="cli")
    assert s.answer(q.id, text="  go  ", channel="cli").value == "  go  "


def test_l1_a_resume_step_is_never_replaced_and_settle_is_for_the_leasing_session(tmp_path):
    import sqlite3

    from levain.consent.questions import QuestionStore

    s = QuestionStore(tmp_path / "c")
    q = s.file(agent="codex", agent_session_id="s", what="w?", options=[("a", "x"), ("b", "y")], rec="a",
               blocks="z", cwd="/w")
    s.answer(q.id, option="a", channel="cli")
    assert [x.id for x in s.lease_answers_for("codex", "s")] == [q.id]
    s.settle_resume([q.id], agent_session_id="someone-else", outcome="started", argv=["x"], cwd="/w")
    assert s.delivery_state(q.id)[0] == "unconfirmed"
    conn = sqlite3.connect(s.path)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("INSERT OR REPLACE INTO answer_resume_steps (seq, question_id, agent_session_id, outcome, "
                     "created_at) VALUES (1, ?, 's', 'started', 'x')", (q.id,))
    conn.close()
    s.settle_resume([q.id], agent_session_id="s", outcome="started", argv=["x"], cwd="/w")
    assert s.delivery_state(q.id)[0] == "delivered"


def test_ready_by_one_deadline_covers_open_and_setup(tmp_path, monkeypatch):
    # A 0.5 s hook deadline: if the open burns 0.3 s, the setup's lock wait must be what is LEFT, not a fresh 0.5 s.
    import sqlite3
    import time

    from levain.consent import questions as Q

    seen = []
    real_open = Q.open_durable

    def slow_open(path, **kw):
        time.sleep(0.3)
        return real_open(path, **kw)

    def spy_schema(conn):
        seen.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        raise sqlite3.OperationalError("stop here")

    monkeypatch.setattr(Q, "open_durable", slow_open)
    monkeypatch.setattr(Q.QuestionStore, "_ensure_schema", staticmethod(spy_schema))
    store = Q.QuestionStore(tmp_path / "q")
    with pytest.raises(sqlite3.OperationalError):
        store.ready_by(time.monotonic() + 0.5)
    assert seen and seen[0] <= 250, seen


def test_ready_by_computes_the_open_budget_after_the_directory_check(tmp_path, monkeypatch):
    import time

    from levain.consent import questions as Q

    seen = []
    real_check = Q.QuestionStore._check_directory

    def slow_check(self):
        time.sleep(0.3)
        real_check(self)

    real_open = Q.open_durable

    def spy_open(path, **kw):
        seen.append(kw.get("busy_s"))
        return real_open(path, **kw)

    monkeypatch.setattr(Q.QuestionStore, "_check_directory", slow_check)
    monkeypatch.setattr(Q, "open_durable", spy_open)
    Q.QuestionStore(tmp_path / "q").ready_by(time.monotonic() + 0.5)
    assert seen and seen[0] <= 0.25, seen


def test_open_durable_returns_the_full_configured_busy_timeout(tmp_path, monkeypatch):
    import time

    from levain.autonomic import durable_sqlite as D

    real_set_wal = D._set_wal

    def slow_wal(conn, bound):
        time.sleep(0.3)
        real_set_wal(conn, bound)

    monkeypatch.setattr(D, "_set_wal", slow_wal)
    conn = D.open_durable(tmp_path / "x.db", busy_s=0.5)
    try:
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 500
    finally:
        conn.close()


def test_open_durable_wal_switch_and_first_read_share_one_budget(tmp_path, monkeypatch):
    import time

    from levain.autonomic import durable_sqlite as D

    seen = []
    real_set_wal = D._set_wal

    def slow_wal(conn, bound):
        time.sleep(0.3)
        real_set_wal(conn, bound)

    real_until = D._until_not_busy

    def spy(conn, step):
        seen.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
        return real_until(conn, step)

    monkeypatch.setattr(D, "_set_wal", slow_wal)
    monkeypatch.setattr(D, "_until_not_busy", spy)
    D.open_durable(tmp_path / "x.db", busy_s=0.5).close()
    assert seen and seen[0] <= 250, seen
