"""Q17-ALL S1: the NEEDS YOU band over open questions: stable never-reused numbers, the render, the hooks' fail-open
and 2 s bound, the autonomous seat's silence, and `levain band`."""
from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from levain import band_view
from levain.consent.questions import QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir) -> QuestionStore:
    return QuestionStore(qdir)


def ask(store: QuestionStore, what: str = "Which retry policy?", **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
              blocks="the feed PR", cwd="/tmp/proj")
    kw.update(over)
    return store.file(**kw)


def env(qdir) -> dict[str, str]:
    return {"LEVAIN_HOME": str(qdir.parent)}          # queue_dir_for: <home>/consent


def raw(store: QuestionStore) -> sqlite3.Connection:
    return sqlite3.connect(store.path)


def shown(store: QuestionStore) -> str:
    """What a delivered hook render does: the text, then its closed notices recorded as shown."""
    text, closed = band_view.render_with_closed(store)
    band_view.mark_closed_shown(store, closed)
    return text


def hp(payload, environ) -> str:
    """hook_prompt, delivered: the text, then its commit."""
    text, commit = band_view.hook_prompt(payload, environ)
    commit()
    return text


def numbers_of(text: str) -> list[int]:
    return [int(line.split(".")[0]) for line in text.splitlines() if line.split(".")[0].isdigit()]


# --- the number space -------------------------------------------------------------------------------------
def test_numbers_are_never_reused_across_a_restart_or_an_empty_band(qdir):
    first = QuestionStore(qdir)
    q1 = ask(first)
    assert numbers_of(shown(first)) == [1]
    first.answer(q1.id, option="fix", channel="cli")
    assert "closed 1" in shown(first)
    assert shown(first) == ""            # the band emptied
    again = QuestionStore(qdir)                                   # a restart: a new object on the same file
    ask(again)
    assert numbers_of(shown(again)) == [2]


@pytest.mark.parametrize("sql", ["DELETE FROM band_items", "UPDATE band_items SET n = 99",
                                 "UPDATE band_items SET ref = 'x'",
                                 "INSERT OR REPLACE INTO band_items (n, source, ref, numbered_at) "
                                 "VALUES (1, 'question', 'other', 't')"])
def test_a_band_number_cannot_be_deleted_updated_or_replaced(store, sql):
    ask(store)
    shown(store)
    conn = raw(store)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute(sql)
    conn.close()


def test_a_closed_marker_cannot_be_deleted(store):
    q = ask(store)
    shown(store)
    store.answer(q.id, option="fix", channel="cli")
    shown(store)
    conn = raw(store)
    with pytest.raises(sqlite3.DatabaseError):
        conn.execute("DELETE FROM band_closed_shown")
    conn.close()


def test_two_concurrent_allocators_get_distinct_numbers(qdir):
    seed = QuestionStore(qdir)
    for i in range(6):
        ask(seed, what=f"question {i}")
    out: list[str] = []
    gate = threading.Barrier(2)

    def go() -> None:
        s = QuestionStore(qdir)
        gate.wait()
        out.append(shown(s))

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    a, b = (numbers_of(t) for t in out)
    assert a == b == [1, 2, 3, 4, 5, 6]                           # one writer numbered; the other read its numbers
    conn = raw(seed)
    assert [r[0] for r in conn.execute("SELECT n FROM band_items ORDER BY n")] == [1, 2, 3, 4, 5, 6]
    conn.close()


def test_a_busy_lock_shows_pending_never_a_provisional_number_within_the_bound(store):
    ask(store)
    band_view.render(store)                                       # sets the store's tables up: 1 is numbered
    ask(store, what="second")
    holder = raw(store)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        text = shown(QuestionStore(store.directory))
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert 1.5 <= took < 3.5                                      # waited for the 2 s bound, not longer
    assert "new (number pending): [proj · claude_code] second" in text
    assert numbers_of(text) == [1]                                # the numbered one keeps its number
    assert numbers_of(shown(store)) == [1, 2]   # and the pending one gets the next


# --- the render -------------------------------------------------------------------------------------------
def test_two_filed_questions_then_answer_one_then_a_third(store):
    q1 = ask(store, what="first thing")
    ask(store, what="second thing")
    text = shown(store)
    assert text.splitlines()[0].startswith("NEEDS YOU · 2 open")
    assert numbers_of(text) == [1, 2]
    assert "1. [proj · claude_code] first thing" in text and "2. [proj · claude_code] second thing" in text
    assert "a) fixed 30 s   b) exponential   c) fail loud" in text   # one line, display letters
    assert "rec: b · blocks: the feed PR" in text
    store.answer(q1.id, option="fix", channel="cli")
    text = shown(store)
    assert "closed 1" in text.splitlines()[0]
    assert numbers_of(text) == [2]
    ask(store, what="third thing")
    text = shown(store)
    assert "closed" not in text                                   # shown once, then gone
    assert numbers_of(text) == [2, 3]
    assert "3. [proj · claude_code] third thing" in text


def test_levain_band_numbers_but_does_not_consume_the_closed_notice(store, capsys):
    q = ask(store)
    args = argparse.Namespace(queue_dir=store.directory)
    assert band_view.run_band(args) == 0
    assert "1. [proj" in capsys.readouterr().out
    store.answer(q.id, option="fix", channel="cli")
    assert band_view.run_band(args) == 0
    assert "closed 1" in capsys.readouterr().out
    assert band_view.run_band(args) == 0
    assert "closed 1" in capsys.readouterr().out                  # a look is not the seat's showing
    assert "closed 1" in shown(store)    # the seat still gets it
    assert shown(store) == ""


def test_past_twelve_items_the_rest_are_one_line_and_none_are_dropped(store):
    for i in range(15):
        ask(store, what=f"q{i} " + "w" * 150)
    text = shown(store)
    assert numbers_of(text) == list(range(1, 16))
    lines = text.splitlines()
    full = [ln for ln in lines if ln.startswith("12. ")]
    assert full and lines[lines.index(full[0]) + 1].startswith("    a) ")       # the 12th is still the full form
    tail = [ln for ln in lines if ln.split(".")[0] in ("13", "14", "15")]
    assert len(tail) == 3 and all("rec: b" in ln and "…" in ln and "blocks:" not in ln for ln in tail)
    assert not any(ln.startswith("    a) ") for ln in lines[lines.index(tail[0]):])


def test_an_unreadable_item_is_listed_never_dropped(store):
    ask(store, what="fine")
    bad = ask(store, what="broken")
    ask(store, what="also fine")
    conn = raw(store)
    conn.execute("DROP TRIGGER questions_frozen")                  # a raw writer's damage, to be read back
    conn.execute("UPDATE questions SET options = 'not json' WHERE id = ?", (bad.id,))
    conn.commit()
    conn.close()
    text = shown(store)
    assert numbers_of(text) == [1, 2, 3]
    assert f"2. UNREADABLE: question {bad.id} could not be read" in text
    assert "1. [proj" in text and "3. [proj" in text


def test_nothing_open_is_an_empty_render_and_levain_band_says_so(store, capsys):
    assert shown(store) == ""
    assert band_view.run_band(argparse.Namespace(queue_dir=store.directory)) == 0
    assert capsys.readouterr().out.strip() == "NEEDS YOU · nothing open"


# --- the hooks: fail open, the bound, the autonomous seat -------------------------------------------------
def test_hook_renders_for_a_seat_and_prints_nothing_when_the_store_is_missing_or_broken(qdir, store):
    payload = {"session_id": "s1", "prompt": "hi"}
    assert hp(payload, env(qdir)) == ""        # no questions.db: nothing, and none made
    assert not store.path.exists()
    ask(store)
    assert numbers_of(hp(payload, env(qdir))) == [1]
    store.path.write_bytes(b"this is not a database" * 100)
    assert hp(payload, env(qdir)) == ""        # unreadable store: silence, no exception
    assert hp(None, env(qdir)) == ""           # a malformed payload too
    assert hp({"session_id": "s1"}, {"LEVAIN_HOME": "/nonexistent/levain"}) == ""


def test_hook_with_a_locked_store_never_raises_and_stays_in_the_bound(qdir, store):
    ask(store)
    holder = raw(store)
    holder.isolation_level = None
    holder.execute("BEGIN IMMEDIATE")
    try:
        t0 = time.monotonic()
        text = hp({"session_id": "s1"}, env(qdir))
        took = time.monotonic() - t0
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    assert took < 3.5
    assert "new (number pending)" in text or text == ""           # pending, or silence; never a crash or a number


def test_an_autonomous_seat_renders_nothing_and_a_human_seat_gets_the_contract(qdir, store, monkeypatch):
    from levain.coordinator import lease
    from levain.coordinator.store import CoordStore

    ask(store)
    launches: dict[str, str] = {}
    monkeypatch.setattr(CoordStore, "open_readonly", lambda self, *_a, **_k: sqlite3.connect(":memory:"))
    monkeypatch.setattr(lease, "by_harness_session",
                        lambda conn, sid: SimpleNamespace(role=launches[sid]) if sid in launches else None)
    launches.update({"w1": "worker", "c1": "coordinator"})
    assert hp({"session_id": "w1"}, env(qdir)) == ""
    assert band_view.hook_contract({"session_id": "w1"}, env(qdir)) == ""
    assert hp({"session_id": "s1#agent:sub"}, env(qdir)) == ""        # a subagent
    for sid in ("c1", "no-launch-row"):                                                  # attended, or no fact
        assert numbers_of(hp({"session_id": sid}, env(qdir))) == [1]
        assert band_view.hook_contract({"session_id": sid}, env(qdir)) == band_view.CONTRACT
    assert not band_view.is_autonomous("c1", qdir) and band_view.is_autonomous("w1", qdir)


def test_no_coordinator_store_is_not_autonomous(qdir):
    assert band_view.is_autonomous("anything", qdir) is False


# --- S1 fix round 1 ---------------------------------------------------------------------------------------
def _closed_world(store):
    q = ask(store)
    shown(store)                                                  # numbered 1
    store.answer(q.id, option="fix", channel="cli")


def _shown_marks(store) -> list[int]:
    conn = raw(store)
    try:
        return [r[0] for r in conn.execute("SELECT n FROM band_closed_shown")]
    finally:
        conn.close()


def _run_prompt_hook(monkeypatch, qdir, emit):
    """The real user_prompt_submit.py main(), with the helper's outside world stubbed and ``emit`` as given."""
    import importlib.util
    import pathlib

    import levain

    path = pathlib.Path(levain.__file__).parent / "templates" / "activation" / "hooks" / "user_prompt_submit.py"
    spec = importlib.util.spec_from_file_location("ups_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    h = mod.hook
    monkeypatch.setenv("LEVAIN_HOME", str(qdir.parent))
    monkeypatch.setattr(h, "read_stdin", lambda: {"session_id": "s1", "prompt": "hi", "cwd": "/tmp"})
    monkeypatch.setattr(h, "should_fire", lambda: True)
    monkeypatch.setattr(h, "read_blocks", lambda *_a, **_k: [])
    monkeypatch.setattr(h, "consent_observe", lambda *_a, **_k: None, raising=False)
    monkeypatch.setattr(h, "state_line_pending", lambda _s: (None, None), raising=False)
    monkeypatch.setattr(h, "open_spores", lambda **_k: [])
    monkeypatch.setattr(h, "crystal_recall", lambda *_a, **_k: [])
    monkeypatch.setattr(h, "wrap_state", lambda **_k: None, raising=False)
    monkeypatch.setattr(h, "session_touch", lambda *_a, **_k: None)
    monkeypatch.setattr(h, "session_log", lambda *_a, **_k: None)
    seen: list[str] = []

    def fake_emit(text, event):
        seen.append(text)
        return emit(text)

    monkeypatch.setattr(h, "emit", fake_emit)
    assert mod.main() == 0
    return seen


def test_closed_survives_a_raise_after_render_and_is_marked_only_after_emit_true(qdir, store, monkeypatch):
    _closed_world(store)

    def boom(_t):
        raise RuntimeError("pipe died")

    assert "closed 1" in _run_prompt_hook(monkeypatch, qdir, boom)[0]
    assert _shown_marks(store) == []                              # a raise in emit: still owed
    assert "closed 1" in _run_prompt_hook(monkeypatch, qdir, lambda _t: False)[0]
    assert _shown_marks(store) == []                              # emit not True: still owed
    assert "closed 1" in _run_prompt_hook(monkeypatch, qdir, lambda _t: True)[0]
    assert _shown_marks(store) == [1]                             # delivered: now recorded
    assert _run_prompt_hook(monkeypatch, qdir, lambda _t: True) == []


def test_a_payload_with_no_session_id_renders_nothing_and_consumes_nothing(qdir, store):
    _closed_world(store)
    for payload in ({}, {"session_id": ""}, {"session_id": 7}, {"session_id": None}, {"prompt": "x"}, None):
        assert band_view.hook_prompt(payload, env(qdir))[0] == ""
        assert band_view.hook_contract(payload, env(qdir)) == ""
    assert _shown_marks(store) == []
    assert "closed 1" in band_view.render(store)                  # still owed to a real seat


def test_a_seven_option_question_is_unreadable_and_the_rest_of_the_band_renders(store):
    ask(store, what="fine one")
    seven = ask(store, what="too many")
    ask(store, what="fine two")
    conn = raw(store)                                             # the store refuses 7 at filing: a raw writer's damage
    conn.execute("DROP TRIGGER questions_frozen")
    conn.execute("UPDATE questions SET options = ?, rec = 'k6' WHERE id = ?",
                 (json.dumps([{"key": f"k{i}", "label": f"opt {i}"} for i in range(7)]), seven.id))
    conn.commit()
    conn.close()
    text = band_view.render(store)
    assert numbers_of(text) == [1, 2, 3]
    assert f"2. UNREADABLE: question {seven.id} could not be read" in text
    assert "1. [proj · claude_code] fine one" in text and "3. [proj · claude_code] fine two" in text


def test_a_non_busy_operational_error_is_not_number_pending(store, monkeypatch):
    ask(store)
    band_view.render(store)
    ask(store, what="second")

    def broken(deadline):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(QuestionStore, "_write_by", lambda self, deadline: broken(deadline))
    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        band_view.render(store)
    assert band_view.run_band(argparse.Namespace(queue_dir=store.directory)) == 1
    assert band_view.hook_prompt({"session_id": "s1"}, {"LEVAIN_HOME": str(store.directory.parent)})[0] == ""


def test_a_real_launch_row_in_the_real_coordinator_store_classes_a_worker_autonomous(qdir, tmp_path, monkeypatch):
    from levain.coordinator.store import CoordStore, Part

    ask(store := QuestionStore(qdir))
    monkeypatch.setenv("LEVAIN_HOME", str(qdir.parent))
    coord = CoordStore()                                          # no directory given: where the coordinator really lives
    assert coord.directory == qdir
    coord.coord_set_agent("none")
    token = coord.coord_take()
    drop = coord.accept_drop("fix the build", surface="cli")[0]
    thread = coord.split(drop.drop_id, [Part("work", "fix the build")])[0]
    ln = coord.launch_request(token, harness="claude", role="worker", threads=[thread.thread_id])
    got, why = coord.launch_activate(ln.launch_id, harness_session_id="real-worker-1")
    assert why is None and got.state == "active"
    assert band_view.is_autonomous("real-worker-1", qdir) is True
    assert band_view.is_autonomous("some-human-seat", qdir) is False
    assert band_view.hook_prompt({"session_id": "real-worker-1"}, env(qdir))[0] == ""
    assert band_view.hook_contract({"session_id": "real-worker-1"}, env(qdir)) == ""
    assert numbers_of(band_view.hook_prompt({"session_id": "some-human-seat"}, env(qdir))[0]) == [1]
