"""Q17-ALL S5a: the band on the human surfaces (server side). The shared answer path takes the channel; the phone
carries the band with its binding (`seen_through`, `store_id`) and answers on `POST /band/answer`; the cockpit answers
with `band_answer`; the ping carries the band number.

Q1 RULED (a): "Information answers stay unsigned and are accepted only from human channels; agent-typed or relayed
answers are refused and the item stays open." The phone and the cockpit ARE the human channels, gated by the
credentials they already require; these tests keep that gate (no credential: nothing recorded) and keep the answer
unsigned."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from levain import band_view
from levain.consent import band
from levain.consent.questions import QuestionStore

_TOK = "phone-band-token"
OPTS = [("fix", "fixed 30 s"), ("exp", "exponential"), ("none", "fail loud")]


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    h = tmp_path / "lh"
    (h / "consent").mkdir(parents=True, mode=0o700)
    monkeypatch.setenv("LEVAIN_HOME", str(h))
    return h


@pytest.fixture
def qdir(home: Path) -> Path:
    return home / "consent"


@pytest.fixture(autouse=True)
def _no_delivery(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(band, "deliver_answer", lambda store, ref, **kw: (
        calls.append(ref), type("D", (), {"route": "hook", "reason": "next hook delivers it"})())[1])
    return calls


def ask(store: QuestionStore, what: str = "Which retry policy?", **over):
    kw = dict(agent="claude_code", agent_session_id="sess-1", what=what, options=OPTS, rec="exp",
              blocks="the feed PR", cwd="/tmp/proj")
    kw.update(over)
    return store.file(**kw)


def views(store: QuestionStore, viewer: str | None = None) -> list:
    with sqlite3.connect(store.path) as c:
        if viewer is None:
            return c.execute("SELECT viewer, seen_through, open_list FROM band_views ORDER BY seq").fetchall()
        return c.execute("SELECT viewer, seen_through, open_list FROM band_views WHERE viewer = ? ORDER BY seq",
                         (viewer,)).fetchall()




def bind(b: dict) -> dict:
    """The binding a band listing hands an answer to carry back."""
    return {"store_id": b["store_id"], "view_id": b["view_id"]}




# --- apply_plan takes the channel -------------------------------------------------------------------------
@pytest.mark.parametrize("channel", ["cockpit", "phone"])
def test_apply_plan_records_the_channel_it_is_given(qdir, channel):
    store = QuestionStore(qdir)
    q = ask(store)
    r = band_view.render_full(store)
    band_view.record_view(store, f"{channel}:x", r.hwm, r.shown, gen=r.gen)
    plan, view, questions, extras = band_view.plan_for_full(store, f"{channel}:x", "1b")
    done = band_view.apply_plan(store, plan, questions, viewer=f"{channel}:x", via=channel,
                                seen_through=view.seen_through, extras=extras, channel=channel)
    assert done.recorded == ["1b"]
    a = store.get(q.n).answer
    assert (a.kind, a.value, a.channel) == ("option", "exp", channel)


def test_apply_plan_without_a_channel_is_still_cli(qdir):
    store = QuestionStore(qdir)
    q = ask(store)
    r = band_view.render_full(store)
    band_view.record_view(store, "cli", r.hwm, r.shown, gen=r.gen)
    plan, view, questions, extras = band_view.plan_for_full(store, "cli", "1a")
    band_view.apply_plan(store, plan, questions, viewer="cli", via="cli", seen_through=view.seen_through,
                         extras=extras)
    assert store.get(q.n).answer.channel == "cli"


# --- decision A (c): number on show, a view row only when the view changed ---------------------------------
def test_show_numbers_and_records_one_view_until_the_band_changes(qdir):
    store = QuestionStore(qdir)
    ask(store)
    for _ in range(4):                                    # a poll loop
        r = band_view.show(store, "phone:run")
    assert r.shown == [1] and r.hwm == 1
    assert len(views(store, "phone:run")) == 1
    ask(store, "A second one?")
    band_view.show(store, "phone:run")
    band_view.show(store, "phone:run")
    assert [v[1] for v in views(store, "phone:run")] == [1, 2]


def test_a_view_that_only_lost_an_item_is_a_change(qdir):
    store = QuestionStore(qdir)
    a, _b = ask(store), ask(store, "Second?")
    band_view.show(store, "phone:run")
    store.answer(a.id, option="fix", channel="cli")
    band_view.show(store, "phone:run")
    band_view.show(store, "phone:run")
    assert [json.loads(v[2]) for v in views(store, "phone:run")] == [[1, 2], [2]]


def test_the_seat_hook_still_records_a_view_per_prompt(qdir):
    """The dedupe is the surfaces' only: a seat's view row carries its Stop-verifier block per prompt."""
    store = QuestionStore(qdir)
    ask(store)
    env = {"LEVAIN_HOME": str(qdir.parent)}
    for _ in range(2):
        _text, commit = band_view.hook_prompt({"session_id": "s1"}, env)
        commit()
    assert len(views(store, "claude_code:s1")) == 2


# --- the phone: the band payload and POST /band/answer ------------------------------------------------------


def test_an_unchanged_poll_takes_the_lock_but_records_no_new_view_row(qdir):
    """DESIGN 2.4: an unchanged poll takes the write lock (the dedupe is one comparison inside the write) and records
    nothing: the band_views count and max seq are unchanged and the view_id answered is the existing one."""
    store = QuestionStore(qdir)
    ask(store)
    first = band_view.band_payload(store, "phone:run")
    with sqlite3.connect(store.path) as c:
        before = c.execute("SELECT COUNT(*), MAX(seq) FROM band_views").fetchone()
    again = band_view.band_payload(store, "phone:run")
    with sqlite3.connect(store.path) as c:
        after = c.execute("SELECT COUNT(*), MAX(seq) FROM band_views").fetchone()
    assert again["seen_through"] == 1
    assert before == after and before[0] == 1, (before, after)
    assert again["view_id"] == first["view_id"] == before[1]


def test_a_poll_against_a_busy_write_lock_records_nothing_and_still_answers(qdir):
    store = QuestionStore(qdir)
    ask(store)
    band_view.band_payload(store, "phone:run")
    ask(store, "Second?")                                 # the band changed: a poll now wants to record a new view
    with sqlite3.connect(store.path) as c:
        before = c.execute("SELECT COUNT(*), MAX(seq) FROM band_views").fetchone()
    holder = sqlite3.connect(store.path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        got = band_view.band_payload(store, "phone:run", deadline=time.monotonic() + 0.4)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    with sqlite3.connect(store.path) as c:
        after = c.execute("SELECT COUNT(*), MAX(seq) FROM band_views").fetchone()
    assert before == after, (before, after)               # nothing was recorded
    assert got["view_id"] is None and got["note"], got    # and it answered, saying so


# --- L3 r1 fix round: the view an answer names, checked inside the write ------------------------------------------
def test_two_views_recorded_between_plan_and_write_make_the_answer_stale(qdir, monkeypatch):
    """codex HIGH 2: a poll that records newer views after the answer's plan was made, but before its write, makes
    the answer stale (409), never bound to the view it no longer is."""
    store = QuestionStore(qdir)
    q = ask(store)
    b = band_view.band_payload(store, "phone:run")
    real = QuestionStore.answer_with_context

    def racing(self, *a, **k):
        ask(store, "Second?")
        band_view.show(store, "phone:run")
        ask(store, "Third?")
        band_view.show(store, "phone:run")
        return real(self, *a, **k)

    monkeypatch.setattr(QuestionStore, "answer_with_context", racing)
    with pytest.raises(band_view.StaleBand):
        band_view.answer_text(store, "1a", viewer="phone:run", channel="phone", **bind(b))
    assert store.get(q.n).answer is None


def test_an_unchanged_poll_between_plan_and_write_leaves_the_answer_good(qdir, monkeypatch):
    """The control: a poll that saw the same band records no view (dedupe), so the view is still the newest."""
    store = QuestionStore(qdir)
    q = ask(store)
    b = band_view.band_payload(store, "phone:run")
    real = QuestionStore.answer_with_context

    def polling(self, *a, **k):
        band_view.show(store, "phone:run")
        return real(self, *a, **k)

    monkeypatch.setattr(QuestionStore, "answer_with_context", polling)
    done = band_view.answer_text(store, "1a", viewer="phone:run", channel="phone", **bind(b))
    assert done.recorded == ["1a"] and store.get(q.n).answer.value == "fix"


def test_another_viewers_view_never_makes_this_answer_stale(qdir, monkeypatch):
    store = QuestionStore(qdir)
    q = ask(store)
    b = band_view.band_payload(store, "phone:run")
    real = QuestionStore.answer_with_context

    def other(self, *a, **k):
        ask(store, "Second?")
        band_view.show(store, "cockpit:web")
        return real(self, *a, **k)

    monkeypatch.setattr(QuestionStore, "answer_with_context", other)
    assert band_view.answer_text(store, "1b", viewer="phone:run", channel="phone", **bind(b)).recorded == ["1b"]
    assert store.get(q.n).answer.value == "exp"


def test_a_view_id_of_another_viewer_is_stale(qdir):
    store = QuestionStore(qdir)
    q = ask(store)
    theirs = band_view.band_payload(store, "cockpit:web")
    band_view.band_payload(store, "phone:run")
    with pytest.raises(band_view.StaleBand):
        band_view.answer_text(store, "1a", viewer="phone:run", channel="phone", **bind(theirs))
    assert store.get(q.n).answer is None


def test_an_answer_from_a_view_that_lost_an_item_with_seen_through_unchanged_is_stale(qdir):
    """codex HIGH 2: removing an item changes the open list and not seen_through or store_id; the older view is
    still a different view, and an answer naming it is stale."""
    store = QuestionStore(qdir)
    a, b2 = ask(store), ask(store, "Second?")
    v1 = band_view.band_payload(store, "phone:run")
    store.answer(a.id, option="fix", channel="cli")
    v2 = band_view.band_payload(store, "phone:run")
    assert (v1["seen_through"], v1["store_id"]) == (v2["seen_through"], v2["store_id"])
    assert v1["view_id"] != v2["view_id"]
    with pytest.raises(band_view.StaleBand):
        band_view.answer_text(store, "2a", viewer="phone:run", channel="phone", **bind(v1))
    assert store.get(b2.n).answer is None
    assert band_view.answer_text(store, "2a", viewer="phone:run", channel="phone", **bind(v2)).recorded == ["2a"]




def _band_tables(store: QuestionStore) -> dict[str, int]:
    with sqlite3.connect(store.path) as c:
        have = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        return {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                for t in ("band_items", "band_views") if t in have}




def test_the_real_lock_held_past_the_bound_is_raised_never_a_refusal(qdir):
    """A real held write lock: the surface path raises SQLite's busy error (the routes' 503), never refuses per item."""
    import time
    store = QuestionStore(qdir)
    q = ask(store)
    b = band_view.band_payload(store, "phone:run")
    hold = sqlite3.connect(store.path, isolation_level=None)
    hold.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(sqlite3.OperationalError):
            band_view.answer_text(store, "1a", viewer="phone:run", channel="phone", **bind(b),
                                  deadline=time.monotonic() + 0.2)
    finally:
        hold.execute("ROLLBACK")
        hold.close()
    assert store.get(q.n).answer is None


# --- the ping carries the band number (decision B (a)) ----------------------------------------------------------
def _capture_ping(monkeypatch) -> list[str]:
    sent: list[str] = []

    class Adapter:
        @staticmethod
        def load_settings(qdir):
            return None

        @staticmethod
        def notify(settings, text, **kw):
            sent.append(text)
            return True

    monkeypatch.setattr(band, "_agents", lambda: {"claude_code": Adapter, "codex": Adapter})
    return sent


def test_the_ping_names_the_band_number_and_allocates_it_once(qdir, monkeypatch):
    store = QuestionStore(qdir)
    sent = _capture_ping(monkeypatch)
    q = ask(store)
    assert band._send_ping("claude_code", q, qdir)
    assert "band 1, question #1" in sent[0].splitlines()[0]
    band._send_ping("claude_code", q, qdir)
    assert "band 1," in sent[1]                           # a re-ping never takes a second number
    assert band_view.render_full(store).shown == [1]


@pytest.mark.parametrize("failure", ["busy", "error"])
def test_a_busy_or_failing_number_write_falls_back_to_the_old_ping_text(qdir, monkeypatch, failure):
    store = QuestionStore(qdir)
    sent = _capture_ping(monkeypatch)
    q = ask(store)

    def boom(*a, **k):
        if failure == "error":
            raise RuntimeError("disk")
        return {}                                         # what _number returns when the lock stays busy

    monkeypatch.setattr(band_view, "_number", boom)
    assert band._send_ping("claude_code", q, qdir)
    assert sent[0].startswith(f"levain: claude_code asks (question #{q.n}):") and "band" not in sent[0].splitlines()[0]
