"""Row 134 (BAND SNOOZE): the operator defers a band row to a later local day inside a tight window; the row stays open,
keeps its number and answers by it, folds into ONE ``LATER (n): <number> <Wkd>[ MM-DD]`` line (flow's
task_surfaces_1107.md SD-9 format and predicate names), the header counts only what is due, no held ping goes before its
day, an elapsed snooze is due again, and the record is append-only. Disposable stores only; synthetic words."""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import datetime, timedelta, timezone

import pytest

from levain import band_view
from levain.cockpit.consent import ConsentError
from levain.consent import band as consent_band
from levain.consent.questions import SNOOZE_MAX_DAYS, Question, QuestionStore, later_date, local_today

# Sat 2026-11-07 11:00 in New York (EST since 11-01): tomorrow is Sun 11-08, the window ends Tue 11-10
NOW = datetime(2026, 11, 7, 16, 0, tzinfo=timezone.utc)
OPTS = [("fix", "fixed 30 s"), ("exp", "exponential")]


@pytest.fixture(autouse=True)
def new_york(monkeypatch):
    """The operator's local day is the machine's: pin it, so the weekday labels are exact on any runner."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture(autouse=True)
def plain_terminal(monkeypatch):
    """Run as the operator at a plain terminal, whatever session runs the suite (test_band_answer.py's shape)."""
    for name in ("CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "CLAUDECODE", "CODEX_SANDBOX", "LEVAIN_TURN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(consent_band, "_ancestors",
                        lambda: iter([(1111, ["-zsh"]), (1000, ["/System/Applications/Utilities/Terminal.app"])]))


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def days(self, n: float) -> None:
        self.now = self.now + timedelta(days=n)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return d


@pytest.fixture
def store(qdir, clock) -> QuestionStore:
    return QuestionStore(qdir, clock=clock)


def ask(store: QuestionStore, what: str = "Which retry policy?"):
    return store.file(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
                      blocks="the feed PR", cwd="/tmp/proj")


def raw(store: QuestionStore) -> sqlite3.Connection:
    return sqlite3.connect(store.path)


def text(store: QuestionStore) -> str:
    return band_view.render_full(store).text


# --- the predicate: flow's semantics, one function ----------------------------------------------------------------
@pytest.mark.parametrize("row, want", [
    ({"snoozed_until": "2026-11-08"}, "2026-11-08"),
    ({"dated": "2026-11-10"}, "2026-11-10"),
    ({"snoozed_until": "2026-11-09", "dated": "2026-11-10"}, "2026-11-09"),     # the snooze end wins
    ({"snoozed_until": "2026-11-06", "dated": "2026-11-10"}, "2026-11-10"),     # an elapsed snooze: its dated
    ({"snoozed_until": "2026-11-07"}, None),                                     # today is due, not later
    ({"dated": "2026-11-01"}, None),
    ({"snoozed_until": "2026-11-31"}, None),                                     # not a calendar date: due
    ({"snoozed_until": "11-08"}, None),
    ({"snoozed_until": 20261108}, None),
    ({}, None),
])
def test_later_date_is_flows_predicate(row, want):
    assert band_view._later_date(row, "2026-11-07") == want == later_date(row, "2026-11-07")


def test_later_line_is_flows_exact_format():
    today = "2026-11-07"
    assert band_view._later_label(343, "2026-11-08", today) == "343 Sun"                # tomorrow: the weekday alone
    assert band_view._later_label(373, "2026-11-10", today) == "373 Tue 11-10"
    assert band_view._later_line([(343, "2026-11-08"), (373, "2026-11-10")], today) == \
        "LATER (2): 343 Sun · 373 Tue 11-10"


def test_local_today_is_the_operators_local_date_not_utc():
    late = datetime(2026, 11, 8, 3, 0, tzinfo=timezone.utc)          # 22:00 Sat in New York, already Sun in UTC
    assert local_today(late) == "2026-11-07"


# --- the store ----------------------------------------------------------------------------------------------------
def test_a_snoozed_row_stays_open_numbered_and_unversioned(store):
    q = ask(store)
    before = text(store)
    assert before.startswith("NEEDS YOU · 1 open · band@1")
    version = band_view.subject_version(store.get(q.id))
    s = store.snooze(q.id, until="2026-11-08", channel="cli", via="cli", note="after the call")
    assert (s.question_id, s.until, s.channel, s.via, s.note) == (q.id, "2026-11-08", "cli", "cli", "after the call")
    got = store.get(q.id)
    assert got.answer is None and got.snoozed_until == "2026-11-08" and got.n == q.n
    assert [x.id for x in store.open_questions()] == [q.id]                 # open: every reader still lists it
    assert band_view.subject_version(got) == version                      # a card signed before the snooze still answers
    assert raw(store).execute("SELECT n FROM band_items WHERE ref = ?", (q.id,)).fetchall() == [(1,)]


def test_the_band_folds_snoozed_rows_into_one_later_line_and_counts_only_due(store):
    a, b, c = ask(store, "first?"), ask(store, "second?"), ask(store, "third?")
    text(store)                                                           # numbered 1, 2, 3 on first show
    store.snooze(c.id, until="2026-11-08", channel="cli", via="cli")
    store.snooze(a.id, until="2026-11-10", channel="cli", via="cli")
    out = text(store).splitlines()
    assert out[0] == "NEEDS YOU · 1 due · band@3"
    assert "LATER (2): 3 Sun · 1 Tue 11-10" in out                      # sorted by day, then number
    assert sum("second?" in ln for ln in out) == 1 and not any("first?" in ln or "third?" in ln for ln in out)
    r = band_view.render_full(store)
    assert r.shown == [1, 2, 3]                                           # the view still binds every number


def test_later_ties_sort_by_number(store):
    qs = [ask(store, f"q{i}?") for i in range(3)]
    text(store)
    for q in reversed(qs):
        store.snooze(q.id, until="2026-11-09", channel="cli", via="cli")
    assert "LATER (3): 1 Mon 11-09 · 2 Mon 11-09 · 3 Mon 11-09" in text(store)
    assert text(store).startswith("NEEDS YOU · 0 due · band@3")


def test_an_elapsed_snooze_is_due_again_on_its_day(store, clock):
    q = ask(store)
    text(store)
    store.snooze(q.id, until="2026-11-09", channel="cli", via="cli")
    assert "LATER (1): 1 Mon 11-09" in text(store)
    clock.days(1)
    assert "LATER (1): 1 Mon" in text(store)                             # now tomorrow: the weekday alone
    clock.days(1)                                                         # its day: due, listed in full
    out = text(store)
    assert out.startswith("NEEDS YOU · 1 open · band@1") and "LATER" not in out and "Which retry policy?" in out


def test_the_newest_snooze_wins_and_unsnooze_brings_it_back(store):
    q = ask(store)
    text(store)
    store.snooze(q.id, until="2026-11-10", channel="cli", via="cli")
    store.snooze(q.id, until="2026-11-08", channel="cockpit", via="cockpit")
    assert store.get(q.id).snoozed_until == "2026-11-08"
    store.snooze(q.id, until=None, channel="cli", via="cli")
    assert store.get(q.id).snoozed_until is None and "LATER" not in text(store)
    assert [s.until for s in store.snoozes(q.id)] == ["2026-11-10", "2026-11-08", None]      # the history is kept
    with pytest.raises(ConsentError, match="is not snoozed"):
        store.snooze(q.id, until=None, channel="cli", via="cli")


@pytest.mark.parametrize("until, why", [
    ("2026-11-11", f"at most {SNOOZE_MAX_DAYS} days"),
    ("2026-12-01", "dated agenda"),
    ("2026-11-07", "not after today"),
    ("2026-11-01", "not after today"),
    ("2026-11-31", "YYYY-MM-DD"),
    ("11-10", "YYYY-MM-DD"),
    ("2026-11-10T00:00", "YYYY-MM-DD"),
    (20261110, "YYYY-MM-DD"),
])
def test_a_snooze_outside_the_tight_window_is_refused_with_its_reason(store, until, why):
    q = ask(store)
    with pytest.raises(ConsentError, match=why):
        store.snooze(q.id, until=until, channel="cli", via="cli")
    assert store.snoozes(q.id) == [] and store.get(q.id).snoozed_until is None


def test_the_window_edge_is_inclusive(store):
    q = ask(store)
    assert store.snooze(q.id, until="2026-11-10", channel="cli", via="cli").until == "2026-11-10"


def test_answered_and_unknown_questions_are_refused_and_nothing_is_written(store):
    q = ask(store)
    store.answer(q.id, option="fix", channel="cli")
    with pytest.raises(ConsentError, match="already answered"):
        store.snooze(q.id, until="2026-11-08", channel="cli", via="cli")
    with pytest.raises(ConsentError, match="no such question"):
        store.snooze("q-" + "0" * 24, until="2026-11-08", channel="cli", via="cli")
    for kw in ({"channel": "seat", "via": "cli"}, {"channel": "cli", "via": "agent"}):
        with pytest.raises(ConsentError):
            store.snooze(q.id, until="2026-11-08", **kw)
    assert raw(store).execute("SELECT COUNT(*) FROM question_snoozes").fetchone() == (0,)


def test_an_answer_closes_a_snoozed_row_and_it_leaves_the_later_line(store):
    q = ask(store)
    text(store)
    store.snooze(q.id, until="2026-11-08", channel="cli", via="cli")
    store.answer(q.id, option="fix", channel="cli")
    out = text(store)
    assert "LATER" not in out and "closed 1" in out


# --- append-only ---------------------------------------------------------------------------------------------------
def test_snoozes_are_append_only_and_never_touch_the_question(store):
    q = ask(store)
    before = raw(store).execute("SELECT * FROM questions").fetchall()
    store.snooze(q.id, until="2026-11-08", channel="cli", via="cli")
    conn = raw(store)
    for sql in ("UPDATE question_snoozes SET until = '2026-11-09'", "DELETE FROM question_snoozes",
                "INSERT OR REPLACE INTO question_snoozes (seq, question_id, until, channel, via, set_at) "
                "VALUES (1, 'x', NULL, 'cli', 'cli', 't')"):
        with pytest.raises(sqlite3.IntegrityError, match="append-only|never replaced"):
            conn.execute(sql)
    for sql in ("INSERT INTO question_snoozes (question_id, until, channel, via, set_at) "
                "VALUES ('x', '11-08', 'cli', 'cli', 't')",                                  # the date shape
                "INSERT INTO question_snoozes (question_id, until, channel, via, set_at) "
                "VALUES ('x', NULL, 'seat', 'cli', 't')"):                                     # the channel
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql)
    assert raw(store).execute("SELECT * FROM questions").fetchall() == before
    assert raw(store).execute("SELECT seq, until FROM question_snoozes").fetchall() == [(1, "2026-11-08")]


def test_a_store_set_up_before_snoozes_reads_unsnoozed_and_gets_the_table_on_its_first_snooze(qdir, clock):
    first = QuestionStore(qdir, clock=clock)
    q = ask(first)
    conn = raw(first)
    for t in ("question_snoozes_frozen", "question_snoozes_kept", "question_snoozes_no_replace"):
        conn.execute(f"DROP TRIGGER {t}")
    conn.execute("DROP TABLE question_snoozes")
    conn.commit()
    older = QuestionStore(qdir, clock=clock)
    assert older.get(q.id).snoozed_until is None and older.snoozes(q.id) == []        # a reader adds nothing
    assert "question_snoozes" not in {r[0] for r in raw(older).execute("SELECT name FROM sqlite_master")}
    older.snooze(q.id, until="2026-11-08", channel="cli", via="cli")                    # _peek made it ready: no setup
    assert older.get(q.id).snoozed_until == "2026-11-08"
    with pytest.raises(sqlite3.IntegrityError):
        raw(older).execute("DELETE FROM question_snoozes")


# --- answers by number unchanged -----------------------------------------------------------------------------------
def test_a_snoozed_row_answers_by_its_number(store, qdir, capsys, clock, monkeypatch):
    # run_band opens its own store: pin it to the test clock, or "tomorrow" follows the wall clock (it failed on 11-08)
    monkeypatch.setattr(band_view, "QuestionStore", lambda d: QuestionStore(d, clock=clock))
    ask(store, "first?")
    q2 = ask(store, "second?")
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    store.snooze(q2.id, until="2026-11-09", channel="cli", via="cli")
    capsys.readouterr()
    band_view.run_band(argparse.Namespace(queue_dir=qdir))                # the view he answers from shows it as LATER
    out = capsys.readouterr().out
    assert "LATER (1): 2 Mon 11-09" in out and "second?" not in out
    assert band_view.run_band_answer(argparse.Namespace(queue_dir=qdir, line="2a")) == 0
    got = store.get(q2.id)
    assert got.answer is not None and (got.answer.kind, got.answer.value) == ("option", "fix")


def test_the_view_does_not_read_a_snooze_as_a_close(store):
    a, b = ask(store, "first?"), ask(store, "second?")
    viewer = "claude_code:seat-1"
    r = band_view.show(store, viewer)
    store.snooze(b.id, until="2026-11-08", channel="cli", via="cli")
    prev = band_view.last_view(store, viewer)
    r2 = band_view.render_full(store)
    assert band_view._delta_line(store, prev, r2.shown) == "" and r.shown == r2.shown == [1, 2]


def test_the_stop_verifier_counts_a_later_number_as_reprinted(store):
    from levain.band_shorthand import View

    a, b = ask(store, "first?"), ask(store, "second?")
    text(store)
    store.snooze(b.id, until="2026-11-08", channel="cli", via="cli")
    r = band_view.render_full(store)
    view = View(r.hwm, tuple(r.shown), "s")
    assert band_view._reprint_check(view, r.text, "ok\n" + r.text) == ([], False)            # whole: verified
    partial = "\n".join(ln for ln in r.text.splitlines() if not ln.startswith("NEEDS YOU"))
    assert band_view._reprint_check(view, r.text, partial) == ([], True)                     # both numbers there
    no_later = "\n".join(ln for ln in r.text.splitlines() if not ln.startswith("LATER"))
    assert band_view._reprint_check(view, r.text, no_later) == ([2], True)                   # the LATER line dropped


# --- the CLI verbs ------------------------------------------------------------------------------------------------
def test_levain_band_snooze_and_unsnooze_by_band_number(store, qdir, capsys, monkeypatch):
    monkeypatch.setattr(band_view, "QuestionStore", lambda d: QuestionStore(d, clock=store._clock))
    q = ask(store)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    parser = argparse.ArgumentParser(prog="levain")
    band_view.add_parser(parser.add_subparsers(dest="cmd"))
    ns = parser.parse_args(["band", "--queue-dir", str(qdir), "snooze", "#1", "--until", "2026-11-09",
                            "--note", "after the call"])
    assert ns.func is band_view.run_band_snooze and ns.number == 1
    assert ns.func(ns) == 0
    assert "1 snoozed until Mon 2026-11-09" in capsys.readouterr().out
    s = store.snoozes(q.id)[-1]
    assert (s.until, s.channel, s.via, s.note) == ("2026-11-09", "cli", "cli", "after the call")
    ns = parser.parse_args(["band", "--queue-dir", str(qdir), "unsnooze", "1"])
    assert ns.until is None and ns.func(ns) == 0 and store.get(q.id).snoozed_until is None
    assert "is due again" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        parser.parse_args(["band", "snooze", "x1", "--until", "2026-11-09"])


def test_the_cli_refuses_with_the_reason_and_records_nothing(store, qdir, capsys, monkeypatch):
    monkeypatch.setattr(band_view, "QuestionStore", lambda d: QuestionStore(d, clock=store._clock))
    q = ask(store)
    band_view.run_band(argparse.Namespace(queue_dir=qdir))
    run = band_view.run_band_snooze
    assert run(argparse.Namespace(queue_dir=qdir, number=1, until="2026-11-12", note=None)) == 2
    assert f"at most {SNOOZE_MAX_DAYS} days" in capsys.readouterr().err
    assert run(argparse.Namespace(queue_dir=qdir, number=9, until="2026-11-08", note=None)) == 2
    assert "band number 9 was never shown" in capsys.readouterr().err
    band_view.allocate(store, "host", ["np-host1"])                       # number 2, a host's row
    assert run(argparse.Namespace(queue_dir=qdir, number=2, until="2026-11-08", note=None)) == 2
    assert "not a question" in capsys.readouterr().err
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "some-agent-session")
    assert run(argparse.Namespace(queue_dir=qdir, number=1, until="2026-11-08", note=None)) == 2
    assert "agent session" in capsys.readouterr().err
    assert store.snoozes(q.id) == []


# --- no ping before its day -----------------------------------------------------------------------------------------
def test_a_held_ping_is_not_sent_before_the_snooze_ends_and_goes_on_its_day(tmp_path, monkeypatch):
    from tests.test_questions_held_ping import Desk

    desk = Desk(tmp_path, monkeypatch)                                    # Thu 2026-10-22 17:00 in New York
    desk.desk()
    q, _ = desk.ask()
    assert desk.sent == []                                                # held: a desk holds the live lease
    QuestionStore(desk.dir, clock=lambda: desk.now[0]).snooze(q.id, until="2026-10-24", channel="cli", via="cli")
    desk.later(minutes=10)                                                # unread past 10 minutes: would ping now
    assert consent_band.release_held_pings(desk.dir) == [] and desk.sent == []
    desk.later(days=1)                                                    # Fri 10-23: still before its day
    assert consent_band.release_held_pings(desk.dir) == [] and desk.sent == []
    desk.later(days=1)                                                    # Sat 10-24: its day
    assert consent_band.release_held_pings(desk.dir) == [f"question #{q.n} (unread): the operator was pinged"]
    assert len(desk.sent) == 1
    assert consent_band.release_held_pings(desk.dir) == [] and len(desk.sent) == 1      # once


def test_a_snooze_that_lands_after_the_release_reads_the_question_still_holds_the_ping(tmp_path, monkeypatch):
    """L3 (codex MED): the release worker read the question unsnoozed, the operator snoozed it, and the claim then
    released and pinged on that stale read. The decision is revalidated at the claim, under the question store's
    write lock, so the snooze (which takes that lock) lands wholly before it or wholly after."""
    from levain.desk import first_reader
    from tests.test_questions_held_ping import Desk

    desk = Desk(tmp_path, monkeypatch)
    desk.desk()
    q, _ = desk.ask()
    desk.later(minutes=10)                                                # unread past 10 minutes: would ping now
    real = first_reader._questions

    def racing(directory):
        qs = real(directory)
        get = qs.get

        def get_then_snooze(ref):                                         # the operator snoozes right after the read
            got = get(ref)
            QuestionStore(desk.dir, clock=lambda: desk.now[0]).snooze(q.id, until="2026-10-24", channel="cli",
                                                                      via="cli")
            return got
        qs.get = get_then_snooze
        return qs

    monkeypatch.setattr(first_reader, "_questions", racing)
    assert consent_band.release_held_pings(desk.dir) == [] and desk.sent == []
    monkeypatch.setattr(first_reader, "_questions", real)
    desk.later(days=2)                                                    # Sat 10-24: its day; the hold was not spent
    assert consent_band.release_held_pings(desk.dir) == [f"question #{q.n} (unread): the operator was pinged"]


def test_an_escalated_then_snoozed_question_pings_once_on_its_day(tmp_path, monkeypatch):
    """L3 (deepseek, not a defect): the escalation's release is the one release row, so a snooze defers it and the
    day's ping goes out once, never twice."""
    from levain.desk import first_reader as fr
    from tests.test_questions_held_ping import Desk

    desk = Desk(tmp_path, monkeypatch)
    tok = desk.desk()
    q, _ = desk.ask()
    fr.escalate(desk.store, q.id, "outside its envelope", token=tok)
    QuestionStore(desk.dir, clock=lambda: desk.now[0]).snooze(q.id, until="2026-10-24", channel="cli", via="cli")
    assert consent_band.release_held_pings(desk.dir) == [] and desk.sent == []
    desk.later(days=2)
    assert consent_band.release_held_pings(desk.dir) == [f"question #{q.n} (escalated): the operator was pinged"]
    desk.later(days=1)
    assert consent_band.release_held_pings(desk.dir) == [] and len(desk.sent) == 1


# --- a host's rows (flow's fields, passed through) and the cockpit payload ------------------------------------------
class Host:
    """A foreign store (flow's NeedsQuestions shape) whose rows carry flow's own ``snoozed_until`` / ``dated``."""

    def __init__(self) -> None:
        def q(n, qid, **kw):
            return Question(n=n, id=qid, agent="desk", agent_session_id="-", what=f"repo: question {n}",
                            options=(("a", "yes"), ("b", "no")), rec="a", blocks="the lane", cwd="/tmp/x",
                            filed_at="2026-11-07T10:00:00Z", answer=None, **kw)
        self.rows = [q(343, "np-a", snoozed_until="2026-11-08"), q(373, "np-b", dated="2026-11-10"),
                     q(380, "np-c", dated="2026-11-07"), q(381, "np-d", snoozed_until="garbage")]

    def open_questions(self, agent=None):
        return list(self.rows)

    def recent(self, limit=50):
        return list(self.rows)[:limit]

    def get(self, ref):
        return next((q for q in self.rows if str(ref).lstrip("#") in (str(q.n), q.id)), None)


def test_a_hosts_rows_render_flows_example_line():
    out = band_view.render_full(Host(), now=NOW).text.splitlines()
    assert out[0] == "NEEDS YOU · 2 due · band@381"
    assert "LATER (2): 343 Sun · 373 Tue 11-10" in out
    assert any(ln.startswith("380.") for ln in out) and any(ln.startswith("381.") for ln in out)


def test_a_snoozed_row_is_projected_as_later_in_the_payload_and_the_summary(store):
    """L3 (codex MED): a client that lists every payload item must not get a full, answerable entry for a snoozed row;
    the number and subject stay (the view binds every number), and the summary counts only what is due."""
    a, b = ask(store, "first?"), ask(store, "second?")
    store.snooze(b.id, until="2026-11-09", channel="cli", via="cli")
    pay = band_view.band_payload(store, "cockpit:web")
    first, second = pay["items"]
    assert first["actions"] and first["options"] and "later" not in first
    assert second["later"] == "2026-11-09" and second["actions"] == [] and second["n"] == 2
    assert second["options"] == [] and second["rec"] is None and second["subject"]["id"] == b.id
    sm = band_view.summary(store)
    assert sm["count"] == 1 and [i["later"] for i in sm["items"]] == [None, "2026-11-09"]
    items = {i["n"]: i for i in band_view.band_payload(Host(), "cockpit:web")["items"]}
    assert items[380]["options"] and items[381]["options"]       # a host's due rows (a garbage snooze is due) pass through
    held = {e[1]["n"]: e[1] for e in band_view._foreign_snapshot(Host(), True, 0, NOW).entries}
    assert held[343]["later"] == "2026-11-08" and held[343]["options"] == []   # L3: a foreign future snooze is LATER here too


def test_the_payload_carries_flows_two_fields_raw(store):
    q = ask(store)
    store.snooze(q.id, until="2026-11-08", channel="cli", via="cli")
    item = band_view.band_payload(store, "cockpit:web")["items"][0]
    assert (item["snoozed_until"], item["dated"]) == ("2026-11-08", None)
    hosted = band_view.band_payload(Host(), "cockpit:web")["items"]
    assert [(i["n"], i["snoozed_until"], i["dated"]) for i in hosted][:2] == [(343, "2026-11-08", None),
                                                                               (373, None, "2026-11-10")]


# --- L3 verify-round fixes ----------------------------------------------------------------------------------------
def test_a_later_item_carries_neutral_defaults_for_every_key_a_due_item_has(store):
    a, b = ask(store, "first?"), ask(store, "second?")
    text(store)
    store.snooze(a.id, until="2026-11-10", channel="cli", via="cli")
    items = {i["question"]: i for i in band_view.band_payload(store, "cockpit:web")["items"]}
    later, due = items[a.n], items[b.n]
    assert set(due) <= set(later)
    assert later["options"] == [] and later["blocks"] == [] and later["open_ended"] is False and later["rec"] is None
    assert later["actions"] == [] and later["later"] == "2026-11-10"


def test_the_later_label_weekday_ignores_the_locale(monkeypatch):
    # a label built from fixed English names: strftime('%a') is never consulted
    monkeypatch.setattr(band_view, "datetime", type("DT", (), {
        "fromisoformat": staticmethod(lambda s: _NoStrftime(datetime.fromisoformat(s)))}))
    assert band_view._later_label(1, "2026-11-10", "2026-11-07") == "1 Tue 11-10"
    assert band_view._later_label(3, "2026-11-08", "2026-11-07") == "3 Sun"


class _NoStrftime:
    def __init__(self, dt):
        self._dt = dt

    def date(self):
        return _Date(self._dt.date())


class _Date:
    def __init__(self, d):
        self._d = d

    def weekday(self):
        return self._d.weekday()

    def strftime(self, fmt):
        assert fmt == "%m-%d", fmt          # %a would be locale-dependent
        return self._d.strftime(fmt)

    def __sub__(self, other):
        return self._d - other._d


def test_a_held_due_row_that_stays_held_takes_no_write_lock(tmp_path, monkeypatch):
    from levain.desk import first_reader
    from tests.test_questions_held_ping import Desk

    desk = Desk(tmp_path, monkeypatch)
    desk.desk()
    q, _ = desk.ask()
    QuestionStore(desk.dir, clock=lambda: desk.now[0]).snooze(q.id, until="2026-10-24", channel="cli", via="cli")
    desk.later(minutes=10)                                                # due in the table, held by the snooze
    monkeypatch.setattr(first_reader, "write_transaction", lambda *a, **k: pytest.fail("took the write lock"))
    assert first_reader.claim_held(desk.dir) == []


# --- the claim's pre-read: a missing db, a failed read, a purged question ----------------------------------------------
def test_the_due_pre_read_of_a_missing_db_is_nothing_due_not_a_crash(tmp_path):
    from levain.desk import first_reader

    assert first_reader._read_due(tmp_path / "no-such-dispatch.db") == []
    assert first_reader.claim_held(tmp_path) == []                       # a fresh directory with no db either


def test_a_failed_pre_read_falls_through_to_the_locked_path_and_drops_no_due_row(tmp_path, monkeypatch):
    from levain.desk import first_reader
    from tests.test_questions_held_ping import Desk

    desk = Desk(tmp_path, monkeypatch)
    desk.desk()
    q, _ = desk.ask()
    desk.later(minutes=10)                                                # unread past 10 minutes: due
    monkeypatch.setattr(first_reader, "_read_due", lambda db: None)       # BUSY / corruption: a distinct failure
    assert [(r.id, c) for r, c in first_reader.claim_held(desk.dir)] == [(q.id, "unread")]


def test_a_dispatch_row_whose_question_was_purged_is_released_answered_and_pings_nobody(tmp_path, monkeypatch):
    from levain.desk import first_reader
    from tests.test_questions_held_ping import Desk

    desk = Desk(tmp_path, monkeypatch)
    desk.desk()
    q, _ = desk.ask()
    desk.later(minutes=10)
    # the questions table is append-only by trigger, so the purge is modelled where it is observed: the read
    monkeypatch.setattr(QuestionStore, "get", lambda self, ref: None)
    assert first_reader.claim_held(desk.dir) == []                        # skipped, no crash, nobody pinged
    assert first_reader.claim_held(desk.dir) == []
    assert desk.rows("SELECT cause FROM held_ping_releases") == [("answered",)]
