"""Coordinator C1: the drop and thread store and its CLI, pinned to what the C1 residue run showed."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess

import pytest

from levain.cli import main
from levain.coordinator.store import CoordError, CoordStore, DropConflict, Part, Source
from levain.dispatch.jobs import FORMAT, JobStore

# synthetic words with the shapes real drops have (ellipses, an apostrophe, a newline, two intents in one)
COPY = ("app feedback: copying text ... sometimes crashes the app, also when it does work selection does not work "
        "at all ... also handle the build server's config drift tonight as well")
TWO = ("seeing two things from here, a stuck notification in the widget ... and also errors from the build "
       "server so please look into that and get them fixed. I'll be up a while, ping me if you need me")
NEWLINE = "let's move the review to Thursday\n- also please have the old session capture and close"
ASK = "please send a quick report on the status of the build fix"
NOTE = "note: stepping away for the night, I won't answer pings"


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return CoordStore(d)


def work(title):
    return Part(kind="work", title=title)


def count(store, table):
    with sqlite3.connect(store.path) as db:
        return db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def rows(store, sql, *args):
    with sqlite3.connect(store.path) as db:
        return db.execute(sql, args).fetchall()


def test_drop_is_stored_verbatim_with_a_delivered_receipt(store):
    for words in (COPY, NEWLINE, TWO):
        drop, created = store.accept_drop(words, surface="cli")
        assert created and drop.words == words
        assert drop.words_sha256 == "sha256:" + hashlib.sha256(words.encode("utf-8")).hexdigest()
        assert store.drop(drop.drop_id).words == words
        receipts = store.receipts(drop.drop_id)
        assert [r.body["stage"] for r in receipts] == ["delivered"]
        assert receipts[0].kind == "drop_receipt"


def test_outbox_resend_writes_nothing_and_changed_words_are_refused(store):
    first, created = store.accept_drop("hello from the outbox", surface="cli", outbox_id="phone-outbox-0001")
    assert created
    before = (count(store, "drops"), count(store, "replies"))
    again, created = store.accept_drop("hello from the outbox", surface="cli", outbox_id="phone-outbox-0001")
    assert not created and again == first
    with pytest.raises(DropConflict):
        store.accept_drop("different words, same id", surface="cli", outbox_id="phone-outbox-0001")
    assert (count(store, "drops"), count(store, "replies")) == before


def test_one_drop_split_into_two_work_threads(store):
    drop, _ = store.accept_drop(TWO, surface="cli")
    a, b = store.split(drop.drop_id, [work("stuck widget notification"), work("build server errors")])
    for t in (a, b):
        assert t.rev == 1 and t.members == (drop.drop_id,) and t.kind == "work" and t.state == "open"
    assert a.members_sha256 == b.members_sha256 and a.thread_id != b.thread_id
    stages = [r.body["stage"] for r in store.receipts(drop.drop_id)]
    assert stages == ["delivered", "split"]
    listed = store.receipts(drop.drop_id)[1].body["threads"]
    assert {t["thread_id"] for t in listed} == {a.thread_id, b.thread_id}
    with pytest.raises(CoordError, match="already split"):
        store.split(drop.drop_id, [work("again")])


def test_an_ask_about_a_work_thread_never_joins_it(store):
    two, _ = store.accept_drop(TWO, surface="cli")
    _, hub = store.split(two.drop_id, [work("stuck widget"), work("build server errors")])
    ask, _ = store.accept_drop(ASK, surface="cli")
    (ask_thread,) = store.split(ask.drop_id, [Part(kind="ask", title="status of the build fix",
                                                   about=hub.thread_id)])
    assert ask_thread.about == hub.thread_id and ask_thread.kind == "ask"
    revs = len(store.revisions(hub.thread_id))
    with pytest.raises(CoordError, match="never joins a work thread"):
        store.add(ask.drop_id, Part(kind="ask", attach=hub.thread_id))
    with pytest.raises(CoordError, match="asks about"):
        store.add(ask.drop_id, Part(kind="work", attach=hub.thread_id))
    with pytest.raises(CoordError, match="never joins a work thread"):
        store.merge(hub.thread_id, ask_thread.thread_id)
    with pytest.raises(CoordError, match="names the kind"):
        store.add(ask.drop_id, Part(attach=hub.thread_id))
    assert len(store.revisions(hub.thread_id)) == revs
    assert store.thread(hub.thread_id).members == (two.drop_id,)


def split_copy(store):
    drop, _ = store.accept_drop(COPY, surface="cli")
    parts = store.split(drop.drop_id, [work("copying text crashes the app"), work("text selection does not work"),
                                       work("config drift")])
    return drop, parts


def test_merge_folds_a_split_that_cut_one_intent_in_two(store):
    drop, (a, b, c) = split_copy(store)
    merged = store.merge(a.thread_id, b.thread_id)
    assert merged.rev == 2 and merged.members == (drop.drop_id,)
    other = store.thread(b.thread_id)
    assert other.rev == 2 and other.members == () and other.state == "closed"
    last = store.events(b.thread_id)[-1]
    assert last[1:3] == ("state", "closed") and last[4] == "service" and last[3].startswith("merged into")
    resplit = store.receipts(drop.drop_id)[-1].body
    assert resplit["stage"] == "resplit"
    assert {t["thread_id"] for t in resplit["threads"]} == {a.thread_id, c.thread_id}


def test_merge_refuses_threads_of_different_kinds(store):
    drop, (a, _b, _c) = split_copy(store)
    note, _ = store.accept_drop(NOTE, surface="cli")
    (n,) = store.split(note.drop_id, [Part(kind="note", title="stepping away")])
    with pytest.raises(CoordError, match="never joins"):
        store.merge(a.thread_id, n.thread_id)
    with pytest.raises(CoordError):
        store.merge(a.thread_id, a.thread_id)


def test_state_grammar(store):
    note, _ = store.accept_drop(NOTE, surface="cli")
    (n,) = store.split(note.drop_id, [Part(kind="note", title="stepping away")])
    done = store.set_state(n.thread_id, "done", by="coordinator")
    assert done.state == "closed" and store.events(n.thread_id)[-1][4] == "service"

    drop, (a, b, _c) = split_copy(store)
    assert store.set_state(a.thread_id, "done", by="coordinator").state == "done"
    with pytest.raises(CoordError):
        store.set_state(a.thread_id, "closed", by="service")
    with pytest.raises(CoordError):
        store.set_state(a.thread_id, "closed", by="coordinator")
    assert store.thread(a.thread_id).state == "done"

    assert store.set_state(done.thread_id, "open", by="operator").state == "open"
    store.merge(_c.thread_id, b.thread_id)
    with pytest.raises(CoordError, match="holds no drops"):
        store.set_state(b.thread_id, "open", by="operator")


def test_detach_never_orphans_a_drop(store):
    solo, _ = store.accept_drop(NOTE, surface="cli")
    (only,) = store.split(solo.drop_id, [work("stepping away")])
    with pytest.raises(CoordError, match="no other thread"):
        store.detach(only.thread_id, solo.drop_id)
    assert store.thread(only.thread_id).members == (solo.drop_id,)
    two, _ = store.accept_drop(TWO, surface="cli")
    x, y = store.split(two.drop_id, [work("widget"), work("server")])
    left = store.detach(x.thread_id, two.drop_id)
    assert left.members == () and left.state == "closed"
    assert store.events(x.thread_id)[-1][4] == "service"
    assert store.thread(y.thread_id).members == (two.drop_id,)
    with pytest.raises(CoordError, match="no other thread"):
        store.detach(y.thread_id, two.drop_id)


def test_set_field_guards(store):
    two, _ = store.accept_drop(TWO, surface="cli")
    _, hub = store.split(two.drop_id, [work("widget"), work("server")])
    ask, _ = store.accept_drop(ASK, surface="cli")
    (t,) = store.split(ask.drop_id, [Part(kind="ask", title="status", about=hub.thread_id)])
    with pytest.raises(CoordError, match="never becomes a work thread"):
        store.set_field(t.thread_id, "kind", "work")
    with pytest.raises(CoordError, match="one line"):
        store.set_field(t.thread_id, "title", "two\nlines")
    assert store.set_field(t.thread_id, "title", "one line").title == "one line"
    assert store.set_field(t.thread_id, "kind", "note").kind == "note"


def test_every_table_is_append_only(store):
    drop, (a, _b, _c) = split_copy(store)
    store.accept_drop("hello from the outbox", surface="cli", outbox_id="phone-outbox-0001")
    stmts = [
        "UPDATE drops SET words = 'x'", "DELETE FROM drops",
        "UPDATE thread_revisions SET members = '[]'", "DELETE FROM thread_revisions",
        "UPDATE replies SET body = '{}'", "DELETE FROM replies",
        "INSERT OR REPLACE INTO drops (drop_id, surface, outbox_id, words, words_sha256, sent_at, received_at, "
        f"source_how) VALUES ('{drop.drop_id}', 'cli', 'ob-replacing-1', 'x', 'sha256:0', 't', 't', 'none')",
    ]
    before = {t: count(store, t) for t in ("drops", "thread_revisions", "replies")}
    for sql in stmts:
        with sqlite3.connect(store.path) as db, pytest.raises(sqlite3.DatabaseError):
            db.execute(sql)
    assert {t: count(store, t) for t in before} == before
    assert store.drop(drop.drop_id).words == COPY and store.thread(a.thread_id).rev == 1


def test_format_marker_stays_the_dispatch_stores(store):
    split_copy(store)
    assert rows(store, "SELECT value FROM meta WHERE key='format'") == [(FORMAT,)]
    conn = JobStore(store.directory)._connect()
    conn.close()


def git(repo, *args):
    return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    git(r, "init", "-q", "-b", "fix/x")
    git(r, "commit", "-q", "--allow-empty", "-m", "base")
    return r


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "q"
    d.mkdir(mode=0o700)
    return str(d)


def cli_drop(capsys, qdir, *extra):
    assert main(["drop", "hello", *extra, "--queue-dir", qdir, "--json"]) == 0
    return json.loads(capsys.readouterr().out)["source"]


def test_cli_drop_records_the_checked_out_branch(repo, qdir, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    src = cli_drop(capsys, qdir)
    assert src["how"] == "cwd" and src["ref"] == "refs/heads/fix/x" and src["head"] == git(repo, "rev-parse", "HEAD")
    assert src["repo"] == str(repo.resolve())
    assert cli_drop(capsys, qdir, "--no-source")["how"] == "none"
    git(repo, "checkout", "-q", "--detach")
    assert cli_drop(capsys, qdir) == {"how": "none", "repo": None, "ref": None, "head": None}


def test_cli_split_and_thread_round_trip(qdir, capsys):
    assert main(["drop", COPY, "--queue-dir", qdir, "--json"]) == 0
    drop_id = json.loads(capsys.readouterr().out)["drop_id"]
    assert main(["drops", "split", drop_id, "--thread", "work:a", "--thread", "work:b", "--queue-dir", qdir,
                 "--json"]) == 0
    a, b = json.loads(capsys.readouterr().out)
    assert main(["thread", "merge", a["thread_id"], b["thread_id"], "--queue-dir", qdir, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["rev"] == 2
    assert main(["thread", "add", drop_id, "--attach", f"ask:{a['thread_id']}", "--queue-dir", qdir]) == 1
