"""THE LIST contract in Levain (task_surfaces_1107.md, np-7c5c8752 (a)): a timed ``next``, the ``desk`` target,
``germinate --timed``, and talk seeds. Temp dirs only; the ping seam is replaced; the desk is the real store."""
from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pytest
from anneal_memory import IntentionStore

from levain import germinate as g
from levain import intentions as sp
from levain.cli import main
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.desk.store import WORDS_MAX, CoordStore
from levain.intentions import guarded_intention_store

TODAY = date(2026, 11, 7)
AT = "2026-11-07T09:30"


@pytest.fixture
def qdir(tmp_path):
    d = tmp_path / "home" / "consent"
    d.mkdir(parents=True, mode=0o700)
    return d


@pytest.fixture
def pings(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(band, "_send_ping", lambda agent, q, qdir, extra="", kind="question": sent.append(q.id) or True)
    return sent


def _install(tmp_path) -> Path:
    root = tmp_path / "a"
    (root / ".levain").mkdir(parents=True)
    return root


def _intentions(root) -> Path:
    return root / ".levain" / "memory.spores.json"


def _add(root, text, nxt="2026-11-05", target=None, disposition="agenda", **kw):
    extra = {"target": target} if target else {}
    return guarded_intention_store(_intentions(root)).add(type="thought", text=text, disposition=disposition, next=nxt,
                                                  **extra, **kw)


def _set_next(root, sid, value):
    """anneal 1.0.3 writes only a date-only ``next``; a timed one is placed in the file as THE LIST's writer will."""
    path = _intentions(root)
    data = json.loads(path.read_text())
    for row in data["spores"]:
        if row["id"] == sid:
            row["next"] = value
    path.write_text(json.dumps(data))


def _run(root, qdir, now, **kw):
    return g.germinate(_intentions(root), qdir, install=root, today=TODAY, now=now, **kw)


def _open_ids(root):
    return {s["id"] for s in IntentionStore(_intentions(root), id_prefix="intention").list_open()}


def _rows(qdir):
    return QuestionStore(qdir).recent(100)


def _drops(qdir) -> int:
    with sqlite3.connect(qdir / "dispatch.db") as c:
        return c.execute("SELECT count(*) FROM drops").fetchone()[0]


def _make_board(qdir, tmp_path):
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(tmp_path.resolve()))


def _at(h, m) -> datetime:
    return datetime(2026, 11, 7, h, m)


# --- (a) the timed next ---------------------------------------------------------------------------------------------
def test_parse_when_takes_exactly_two_shapes():
    assert sp._parse_when("2026-11-07") == datetime(2026, 11, 7, 0, 0)
    assert sp._parse_when(AT) == datetime(2026, 11, 7, 9, 30)
    for bad in (None, "", "2026-11-07T9:30", "2026-11-07T09:30:00", "2026-11-07 09:30", "2026-11-07T24:00",
                "2026-11-07T25:00", "2026-13-07", "2026-11-07 note", "2026-11-07T09:30Z", 5):
        assert sp._parse_when(bad) is None and not sp.valid_next(bad)
    assert sp.valid_next("2026-11-07") and sp.valid_next(AT)


def test_a_timed_agenda_is_not_due_a_minute_early_and_due_on_the_minute(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "call at half past")
    _set_next(root, s["id"], AT)
    assert _run(root, qdir, _at(9, 29), dry_run=True)["due"] == []
    assert _run(root, qdir, _at(9, 29))["due"] == [] and s["id"] in _open_ids(root)
    (o,) = _run(root, qdir, _at(9, 30))["due"]
    assert o["intention_id"] == s["id"] and o["done"] and s["id"] in _open_ids(root)


def test_a_timed_agenda_dated_in_the_past_is_due_even_late_in_the_day_and_a_future_day_is_not(tmp_path, qdir, pings):
    root = _install(tmp_path)
    old = _add(root, "yesterday 23:59")
    _set_next(root, old["id"], "2026-11-06T23:59")
    later = _add(root, "tomorrow 00:01")
    _set_next(root, later["id"], "2026-11-08T00:01")
    res = _run(root, qdir, _at(0, 5))
    assert [o["intention_id"] for o in res["due"]] == [old["id"]]


def test_a_malformed_timed_next_is_never_due_and_never_read_as_a_date(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "bad clock")
    _set_next(root, s["id"], "2026-11-01T25:00")
    assert _run(root, qdir, _at(23, 0))["due"] == [] and s["id"] in _open_ids(root)


def test_a_date_only_agenda_is_unchanged_due_all_day_on_its_date(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "today counts", nxt=TODAY.isoformat())
    assert [o["intention_id"] for o in _run(root, qdir, _at(0, 0), dry_run=True)["due"]] == [s["id"]]
    (o,) = _run(root, qdir, _at(0, 0))["due"]
    assert o["done"]


# --- (b) the desk target --------------------------------------------------------------------------------------------
def test_valid_target_accepts_band_nudge_desk_and_board_repo():
    for ok in ("band", "nudge", "desk", "board:levain"):
        assert sp.valid_target(ok)
    for bad in (None, "", "Desk ", "board:", "board:  ", "queue", 3):
        assert not sp.valid_target(bad)


def test_desk_with_a_desk_files_his_words_verbatim_stamps_and_composts(tmp_path, qdir, pings):
    root = _install(tmp_path)
    words = "ask Jess about the fellowship\n\n  second paragraph, indented\tand tabbed\n" + "x" * 5000
    s = _add(root, words, target="desk")
    _make_board(qdir, tmp_path)
    before = _drops(qdir)
    (o,) = _run(root, qdir, _at(9, 0))["due"]
    assert o["via"] == "desk" and o["filed"] and o["done"] and _drops(qdir) == before + 1
    drop = CoordStore(qdir).drop_by_outbox(g.outbox_id_for(s["origin_key"]))
    assert drop.drop_id == o["ref"] and drop.words == guarded_intention_store(_intentions(root)).get(s["id"])["text"]
    assert drop.words.endswith("x" * 5000) and "\n  second paragraph" in drop.words
    assert drop.source.how == "none" and drop.surface == "cli"
    got = IntentionStore(_intentions(root), id_prefix="intention").get(s["id"])
    assert got["status"] != "open" and got["germinated"] == {"on": "2026-11-07", "target": "desk", "ref": drop.drop_id}
    assert _rows(qdir) == [] and pings == []
    _run(root, qdir, _at(9, 5))
    assert _drops(qdir) == before + 1


def test_desk_without_a_desk_store_falls_back_to_a_band_row(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "ask Jess", target="desk")
    assert not (qdir / "dispatch.db").exists()
    (o,) = _run(root, qdir, _at(9, 0))["due"]
    assert o["via"] == "band-fallback" and o["done"] and s["id"] in _open_ids(root)
    (q,) = _rows(qdir)
    assert q.id == o["ref"] and "desk" in q.blocks and "no desk" in q.blocks
    assert not (qdir / "dispatch.db").exists() and pings == []
    assert IntentionStore(_intentions(root), id_prefix="intention").get(s["id"])["germinated"]["target"] == "desk"


def test_a_desk_intention_too_long_for_a_drop_is_an_error_not_a_truncation(tmp_path, qdir):
    root = _install(tmp_path)
    s = _add(root, "y" * (WORDS_MAX + 1), target="desk")
    _make_board(qdir, tmp_path)
    res = _run(root, qdir, _at(9, 0))
    assert res["due"] == [] and "at most" in res["errors"][0]["error"] and s["id"] in _open_ids(root)
    assert _drops(qdir) == 0


def test_a_board_intention_too_long_for_a_drop_is_an_error_not_a_truncation(tmp_path, qdir):
    root = _install(tmp_path)
    s = _add(root, "y" * (WORDS_MAX + 1), target="board:levain")
    _make_board(qdir, tmp_path)
    res = _run(root, qdir, _at(9, 0))
    assert res["due"] == [] and "at most" in res["errors"][0]["error"] and s["id"] in _open_ids(root)
    assert _drops(qdir) == 0


def test_a_stamped_intention_edited_before_the_finish_compost_is_skipped_not_composted(tmp_path, qdir, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "write the notes", target="desk")
    _make_board(qdir, tmp_path)
    cls = type(guarded_intention_store(_intentions(root)))
    real = cls.descend
    monkeypatch.setattr(cls, "descend", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    _run(root, qdir, _at(9, 0))
    assert IntentionStore(_intentions(root), id_prefix="intention").get(s["id"])["germinated"]

    def edit_then_descend(self, *a, **k):
        guarded_intention_store(_intentions(root)).update(s["id"], text="publish the hotfix instead")
        return real(self, *a, **k)

    monkeypatch.setattr(cls, "descend", edit_then_descend)
    res = _run(root, qdir, _at(9, 0))
    assert res["finish"] == [] and res["errors"] == [] and [x["intention_id"] for x in res["skipped"]] == [s["id"]]
    assert s["id"] in _open_ids(root)


# --- (c) germinate --timed ------------------------------------------------------------------------------------------
def test_timed_files_only_timed_agendas_now_due_any_target(tmp_path, qdir, pings):
    root = _install(tmp_path)
    timed_band = _add(root, "timed band")
    _set_next(root, timed_band["id"], "2026-11-07T08:00")
    timed_nudge = _add(root, "timed nudge", target="nudge")
    _set_next(root, timed_nudge["id"], "2026-11-07T08:30")
    timed_future = _add(root, "timed later")
    _set_next(root, timed_future["id"], "2026-11-07T23:00")
    date_only = _add(root, "date only")
    res = _run(root, qdir, _at(9, 0), timed_only=True)
    assert {o["intention_id"] for o in res["due"]} == {timed_band["id"], timed_nudge["id"]}
    assert len(pings) == 1
    assert {timed_future["id"], date_only["id"]} <= _open_ids(root)


def test_timed_is_idempotent_with_the_plain_run_in_either_order(tmp_path, qdir, pings):
    root = _install(tmp_path)
    t = _add(root, "timed")
    _set_next(root, t["id"], "2026-11-07T08:00")
    d = _add(root, "date only")
    _run(root, qdir, _at(9, 0), timed_only=True)
    assert len(_rows(qdir)) == 1
    plain = _run(root, qdir, _at(9, 1))
    assert [o["intention_id"] for o in plain["due"]] == [d["id"]] and len(_rows(qdir)) == 2
    assert _run(root, qdir, _at(9, 2), timed_only=True)["due"] == [] and len(_rows(qdir)) == 2
    assert _run(root, qdir, _at(9, 3))["due"] == [] and len(_rows(qdir)) == 2


def test_the_cli_takes_timed(tmp_path, qdir, pings, capsys):
    root = _install(tmp_path)
    t = _add(root, "timed")
    _set_next(root, t["id"], "2020-01-01T08:00")
    d = _add(root, "date only", nxt="2020-01-01")
    rc = main(["intentions", "germinate", "--timed", "--json", "--path", str(root), "--queue-dir", str(qdir)])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and [o["intention_id"] for o in out["due"]] == [t["id"]] and d["id"] in _open_ids(root)


# --- (d) talk seeds -------------------------------------------------------------------------------------------------
def test_a_seed_may_carry_attention_talk_and_only_talk():
    assert sp.check_steering("seed", None, "talk") is None
    none_msg = sp.check_steering("seed", None, "none")
    assert none_msg and "AGENDA" in none_msg and "attention talk" in none_msg     # the refusal names the exception
    assert sp.check_steering("seed", "desk", None)
    assert sp.check_steering("seed", "desk", "talk")
    assert sp.check_steering("seed", None, "ping")
    assert sp.check_steering("loop", None, "talk") and sp.check_steering(None, None, "talk")
    assert sp.check_steering("seed", None, None) is None
    assert sp.check_steering("agenda", "desk", "talk") and sp.check_steering("agenda", "desk", "ping")
    assert sp.check_steering("agenda", "band", "talk") is None


def test_an_agenda_keeps_its_steering(tmp_path):
    assert sp.check_steering("agenda", "desk", "none") is None
    assert sp.check_steering("agenda", "nudge", "talk") is None
    assert sp.check_steering("agenda", "board:levain", None) is None
    assert sp.check_steering("agenda", "board:levain", "talk")
    assert sp.check_steering("agenda", "queue", None)


def test_the_store_carries_a_talk_seed_and_it_is_recognised_and_never_germinated(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = guarded_intention_store(_intentions(root)).add(type="thought", text="talk this through", disposition="someday",
                                               attention="talk")
    got = IntentionStore(_intentions(root), id_prefix="intention").get(s["id"])
    assert got["attention"] == "talk" and sp.is_talk_seed(got)
    assert not sp.is_talk_seed({"disposition": "agenda", "attention": "talk"})
    assert not sp.is_talk_seed({"disposition": "seed", "attention": "none"})
    assert _run(root, qdir, _at(9, 0))["due"] == [] and s["id"] in _open_ids(root)


# --- intention S1: a question-filing agenda stays live; a drop intention composts as before ---------------------------------
@pytest.mark.parametrize("target", ["band", "nudge", "desk", "board:levain"])
def test_s1_an_agenda_that_filed_or_adopted_a_question_stays_open_and_a_finish_run_leaves_it(tmp_path, qdir, pings, target):
    root = _install(tmp_path)
    s = _add(root, "ring me", target=target)           # no desk: desk / board fall back to the band
    (o,) = _run(root, qdir, _at(9, 0))["due"]
    assert o["ref"].startswith("q-") and o["done"] and s["id"] in _open_ids(root)
    again = _run(root, qdir, _at(9, 0))
    assert again["finish"] == [] and again["due"] == [] and s["id"] in _open_ids(root)
    assert IntentionStore(_intentions(root), id_prefix="intention").get(s["id"])["germinated"]["ref"] == o["ref"]


def test_s1_an_adopted_question_stays_live_too(tmp_path, qdir):
    root = _install(tmp_path)
    held, _ = QuestionStore(qdir).file_once(agent="claude_code", key="other", what="held", options=[("a", "A")],
                                            rec="a", blocks="b", cwd=str(tmp_path.resolve()))
    s = _add(root, f"follow up on {held.id}")
    (o,) = _run(root, qdir, _at(9, 0))["due"]
    assert o["via"] == "adopted" and s["id"] in _open_ids(root)
    assert _run(root, qdir, _at(9, 0))["finish"] == []


def test_s1_a_desk_drop_intention_still_composts(tmp_path, qdir, pings):
    root = _install(tmp_path)
    s = _add(root, "ask Jess", target="desk")
    _make_board(qdir, tmp_path)
    (o,) = _run(root, qdir, _at(9, 0))["due"]
    assert o["via"] == "desk" and o["ref"].startswith("dr-") and s["id"] not in _open_ids(root)


def test_s1_a_crash_stamped_drop_intention_is_still_finished(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    s = _add(root, "ask Jess", target="desk")
    _make_board(qdir, tmp_path)
    cls = type(guarded_intention_store(_intentions(root)))
    real = cls.descend
    monkeypatch.setattr(cls, "descend", lambda self, *a, **k: (_ for _ in ()).throw(RuntimeError("crash")))
    assert _run(root, qdir, _at(9, 0))["errors"]
    monkeypatch.setattr(cls, "descend", real)
    res = _run(root, qdir, _at(9, 0))
    assert [o["intention_id"] for o in res["finish"]] == [s["id"]] and s["id"] not in _open_ids(root)
