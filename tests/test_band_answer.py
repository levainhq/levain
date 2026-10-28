"""Q17-ALL S2: shorthand recorded through the seat hook and `levain band answer`; the Q1 fence; answer_context."""
from __future__ import annotations

import argparse
import sqlite3

import pytest

from levain import band_view
from levain.consent.band import run_question
from levain.consent.questions import Link, QuestionStore

OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]
SEAT = {"session_id": "seat-1"}


@pytest.fixture(autouse=True)
def human_terminal(monkeypatch):
    """Tests run as a human's own terminal unless one says otherwise."""
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "LEVAIN_TURN"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir):
    return QuestionStore(qdir)


@pytest.fixture
def env(qdir):
    return {"LEVAIN_HOME": str(qdir.parent)}


def ask(store, what="Which?", **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp", blocks="the PR",
              cwd="/tmp/proj")
    kw.update(over)
    return store.file(**kw)


def prompt(env, text, session="seat-1"):
    out, commit = band_view.hook_prompt({"session_id": session, "prompt": text}, env)
    commit()
    return out


def answers(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT q.n, a.kind, a.value, a.channel FROM answers a JOIN questions q ON q.id = a.question_id "
                         "ORDER BY q.n").fetchall()


def context(store):
    with sqlite3.connect(store.path) as c:
        return c.execute("SELECT q.n, x.via, x.viewer, x.seen_through, x.note FROM answer_context x JOIN questions q "
                         "ON q.id = x.question_id ORDER BY q.n").fetchall()


def two_seen(store, env):
    ask(store, "first")
    ask(store, "second")
    first = prompt(env, "hello")
    assert "band@2" in first
    return first


def test_seat_typed_1a_and_2_rec_records_both_and_the_echo_shows_them(store, env):
    two_seen(store, env)
    out = prompt(env, "1a, 2: rec")
    assert answers(store) == [(1, "option", "fix", "cli"), (2, "option", "exp", "cli")]
    assert "recorded: 1a (you, seat), 2 rec→b (you, seat)" in out
    assert "refused" not in out
    assert "closed 1, 2" in out                                 # the band's own closed notice
    assert "since your last message: closed 1 (answered via cli), 2 (answered via cli)" in out
    assert [(n, via, viewer) for n, via, viewer, _, _ in context(store)] == [(1, "seat", "claude_code:seat-1"),
                                                                               (2, "seat", "claude_code:seat-1")]


def test_the_same_text_through_levain_band_answer_inside_an_agent_session_is_refused(store, qdir, monkeypatch, capsys):
    ask(store)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "some-agent-session")
    rc = band_view.run_band_answer(argparse.Namespace(queue_dir=qdir, line="1a"))
    assert rc == 2 and answers(store) == [] and context(store) == []
    assert "agent session" in capsys.readouterr().err
    # a Codex session and the CLAUDECODE marker are fenced the same way
    for name, val in (("CODEX_THREAD_ID", "t-1"), ("CLAUDECODE", "1")):
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
        monkeypatch.setenv(name, val)
        assert band_view.run_band_answer(argparse.Namespace(queue_dir=qdir, line="1a")) == 2
    assert answers(store) == []


def test_levain_question_answer_is_refused_inside_an_agent_session_and_open_after(store, qdir, monkeypatch, capsys):
    q = ask(store)
    ns = argparse.Namespace(question_cmd="answer", ref=str(q.n), option="fix", text=None, deliver=False, queue_dir=qdir,
                            json=False)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "some-agent-session")
    assert _question_answer(ns) == 2
    assert "agent session" in capsys.readouterr().out and answers(store) == []
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    assert _question_answer(ns) == 0 and answers(store) == [(q.n, "option", "fix", "cli")]


def _question_answer(ns):
    import io
    out = io.StringIO()
    rc = run_question(ns, out)
    print(out.getvalue(), end="")
    return rc


def test_levain_band_answer_outside_an_agent_session_records_via_cli(store, qdir, capsys):
    ask(store)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    rc = band_view.run_band_answer(argparse.Namespace(queue_dir=qdir, line="1 b because it is cheaper"))
    assert rc == 0 and answers(store) == [(1, "option", "exp", "cli")]
    assert context(store) == [(1, "cli", "cli", 1, "it is cheaper")]
    assert "recorded: 1b (you, cli)" in capsys.readouterr().out


def test_band_answer_needs_the_whole_line_to_parse(store, qdir, capsys):
    ask(store)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    assert band_view.run_band_answer(argparse.Namespace(queue_dir=qdir, line="1a and also fix the bug")) == 2
    assert band_view.run_band_answer(argparse.Namespace(queue_dir=qdir, line="1a\nfix the bug")) == 2
    assert answers(store) == []


def test_all_recs_does_not_touch_an_item_filed_after_the_view(store, env):
    ask(store, "seen one")
    ask(store, "seen two")
    prompt(env, "hello")
    late = ask(store, "filed after the view")
    out = prompt(env, "all recs")
    assert [n for n, *_ in answers(store)] == [1, 2]
    assert store.get(late.n).answer is None
    assert "recorded: 1 rec→b (you, seat), 2 rec→b (you, seat)" in out
    assert "3." in out                                          # the late one is shown in this band, still open
    assert "new 3" in out


def test_a_number_above_the_view_is_refused_and_the_rest_proceeds(store, env):
    ask(store)
    prompt(env, "hello")
    late = ask(store, "late")
    out = prompt(env, f"1a, {late.n}b")
    assert answers(store) == [(1, "option", "fix", "cli")]
    assert f"refused: 2 was filed after the band you saw" in out


def test_store_id_mismatch_is_refused_and_records_nothing(store, env):
    ask(store)
    prompt(env, "hello")
    with sqlite3.connect(store.path) as c:                      # a view from another store (a deleted-and-rebuilt db)
        c.execute("INSERT INTO band_views (viewer, seen_through, open_list, store_id, at) VALUES "
                  "('claude_code:seat-1', 1, '[1]', 'qs-someone-else', '2026-10-28T00:00:00Z')")
    out = prompt(env, "1a")
    assert answers(store) == [] and "another question store" in out


def test_note_is_kept_verbatim_in_answer_context_and_answers_table_is_unchanged(store, env):
    ask(store)
    ask(store)
    ask(store)
    prompt(env, "hello")
    note = "the API is down,  really; — \"quoted\" ü  spaces"
    prompt(env, f"3 b because {note}")
    assert answers(store) == [(3, "option", "exp", "cli")]
    assert context(store) == [(3, "seat", "claude_code:seat-1", 3, note)]
    with sqlite3.connect(store.path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(answers)")]
        sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'answers'").fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("UPDATE answer_context SET note = 'x'")
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM answer_context")
    assert cols == ["question_id", "kind", "value", "channel", "answered_at"]
    assert "channel IN ('cli','phone','cockpit')" in sql


def test_free_text_answer_is_recorded_as_text(store, env):
    ask(store)
    prompt(env, "hello")
    prompt(env, "1: no longer needed")
    assert answers(store) == [(1, "text", "no longer needed", "cli")]


def test_a_relay_turn_is_never_parsed(store, env):
    ask(store)
    prompt(env, "hello")
    out = prompt({**env, "LEVAIN_TURN": "relay"}, "1a")
    assert answers(store) == [] and "recorded" not in out
    out = prompt(env, "levain: an answer was recorded for question #9 (via cli): option fix.\n1a")
    assert answers(store) == []


def test_partial_and_ambiguous_lines_record_nothing_and_say_what_was_refused(store, env):
    ask(store)
    ask(store)
    prompt(env, "hello")
    out = prompt(env, "(a) all recs")
    assert answers(store) == [] and "number in front" in out and "nothing was recorded" in out
    out = prompt(env, "1a, 1b, 2c")
    assert answers(store) == [(2, "option", "none", "cli")]
    assert "refused: conflict: 1a and 1b" in out
    out = prompt(env, "1a, what")                                # a line that does not fully parse: nothing at all
    assert answers(store) == [(2, "option", "none", "cli")] and "recorded:" not in out


def test_an_authority_item_gets_no_answer_from_shorthand(store, env):
    q1 = ask(store, "plain")
    q2 = ask(store, "linked", link=Link("fix", "push", "req-1", "qid-1"))
    prompt(env, "hello")
    out = prompt(env, "all recs, 2a")
    assert answers(store) == [(1, "option", "exp", "cli")]
    assert store.get(q2.n).answer is None
    assert "2 needs your signature; nothing recorded" in out


def test_fail_open_on_a_broken_store_the_prompt_passes_and_nothing_is_recorded(store, env, qdir):
    ask(store)
    prompt(env, "hello")
    store.path.write_bytes(b"this is not a sqlite database at all" * 40)
    out, commit = band_view.hook_prompt({"session_id": "seat-1", "prompt": "1a"}, env)
    commit()
    assert out == ""


def test_fail_open_when_the_parse_raises_nothing_is_recorded(store, env, monkeypatch):
    ask(store)
    prompt(env, "hello")
    monkeypatch.setattr(band_view, "parse", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    out = prompt(env, "1a")
    assert answers(store) == []
    assert "recorded" not in out and "1." in out                # the band still renders


def test_the_hook_is_idempotent_a_replayed_prompt_records_nothing_twice(store, env):
    ask(store)
    ask(store)
    prompt(env, "hello")
    prompt(env, "1a, 2: rec")
    before = (answers(store), context(store))
    out = prompt(env, "1a, 2: rec")                              # the same prompt delivered again
    assert (answers(store), context(store)) == before
    assert "recorded: nothing" in out and "was not open in the band you saw" in out


def test_a_closed_item_is_refused_with_how_it_closed(store, env):
    q = ask(store)
    prompt(env, "hello")
    store.answer(q.id, option="none", channel="phone")
    out = prompt(env, "1a")
    assert answers(store) == [(1, "option", "none", "phone")]
    assert "1 was already answered (c, via phone," in out


def test_an_autonomous_or_idless_seat_parses_nothing(store, env):
    ask(store)
    out, commit = band_view.hook_prompt({"prompt": "1a"}, env)
    assert out == "" and answers(store) == []


def test_each_render_stores_a_view_row_with_its_high_water_mark(store, env):
    ask(store)
    ask(store)
    prompt(env, "hello")
    with sqlite3.connect(store.path) as c:
        rows = c.execute("SELECT viewer, seen_through, open_list FROM band_views").fetchall()
    assert rows == [("claude_code:seat-1", 2, "[1, 2]")]
    with sqlite3.connect(store.path) as c:
        with pytest.raises(sqlite3.DatabaseError):
            c.execute("DELETE FROM band_views")


def test_first_band_on_a_seat_says_so(store, env):
    ask(store)
    assert "first band on this seat" in prompt(env, "hello")
    ask(store)
    assert "since your last message: new 2" in prompt(env, "again")


def test_levain_band_answer_is_registered_in_the_cli(qdir, store, monkeypatch, capsys):
    ask(store)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    parser = argparse.ArgumentParser(prog="levain")
    band_view.add_parser(parser.add_subparsers(dest="cmd"))
    ns = parser.parse_args(["band", "--queue-dir", str(qdir), "answer", "1a"])
    assert ns.func is band_view.run_band_answer and ns.line == "1a" and ns.queue_dir == qdir
    assert parser.parse_args(["band", "--queue-dir", str(qdir)]).func is band_view.run_band
