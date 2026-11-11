"""Intention controls, S5: a native QuestionStore projects the intention a band row rings from its own intention store and acts on
it through the same ``intention_act`` a host offers. Temp dirs only."""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from anneal_memory import IntentionStore
from anneal_memory.spores import intention_version

from levain import germinate as g
from levain.cockpit.consent import ConsentError
from levain.cockpit.verbs import dispatch
from levain.consent import questions as questions_mod
from levain.consent.questions import ANSWER_CHANNELS, INTENTION_CLOSE, QuestionStore
from levain.intentions import guarded_intention_store
from levain.writes import EditError
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, NOW, Rig, TODAY

FOUR = {"intention_snooze", "intention_edit", "intention_done", "intention_clear"}
RUN_DAY = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)      # the day germinate runs in _ringing, as the store's clock


class _FarWall(datetime):
    """A wall clock long past the dates these tests name: a store left on the wall clock judges "after today" against it."""

    @classmethod
    def now(cls, tz=None):  # noqa: D102
        return datetime(2031, 6, 1, 12, 0, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2031, 6, 1, 12, 0)


@pytest.fixture(autouse=True)
def _wall_clock_far_away(monkeypatch):
    """Every store here runs on a pinned clock; the wall clock is moved years ahead so one that does not fails today."""
    monkeypatch.setattr(questions_mod, "datetime", _FarWall)


def _install(tmp_path: Path):
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    intentions = root / ".levain" / "memory.spores.json"
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    return root, intentions, qdir


def _ringing(tmp_path: Path, *, today: date = date(2026, 11, 7), text: str = "renew the thing", typ: str = "task"):
    """An install whose agenda has fired into a band row: germinate ran, so the row is open and the agenda is stamped."""
    root, intentions, qdir = _install(tmp_path)
    sp = guarded_intention_store(intentions)
    sid = sp.add(type=typ, text=text, disposition="agenda", next="2026-11-06")["id"]
    res = g.germinate(intentions, qdir, install=root, today=today)
    assert not res["errors"], res
    qs = QuestionStore(qdir, clock=lambda: datetime(today.year, today.month, today.day, 12, 0, tzinfo=timezone.utc))
    (q,) = qs.open_questions()
    return root, intentions, qdir, sp, sid, qs, q


def test_the_projection_names_the_intention_a_stamped_open_agenda_rings(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    assert qs.intention_for(q) == sid
    plain = qs.file(agent="claude_code", agent_session_id="s", what="other", options=[("a", "A"), ("b", "B")], rec="a",
                    blocks="b", cwd=str(root))
    assert qs.intention_for(plain) is None


def test_snooze_moves_the_intention_closes_the_row_and_the_wake_files_a_new_one(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    said = qs.intention_act(q.id, "snooze", until="2026-11-12", channel="cockpit")
    assert "snoozed to 2026-11-12" in said
    got = sp.get(sid)
    assert got["next"] == "2026-11-12" and not got.get("germinated") and got["status"] == "open"
    closed = qs.get(q.id)
    assert closed.answer is not None and closed.answer.value == INTENTION_CLOSE + "snoozed to 2026-11-12"
    assert closed.answer.channel == "cockpit" and qs.open_questions() == []
    assert g.germinate(intentions, qdir, install=root, today=date(2026, 11, 8))["due"] == []        # not yet
    res = g.germinate(intentions, qdir, install=root, today=date(2026, 11, 12))
    assert not res["errors"] and [o["intention_id"] for o in res["due"]] == [sid]
    (again,) = qs.open_questions()
    assert again.id != q.id and qs.intention_for(again) == sid


def test_an_ordinary_answer_still_holds_the_agendas_key(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    qs.answer(q.id, text="did it", channel="cli")
    res = g.germinate(intentions, qdir, install=root, today=date(2026, 11, 9))
    assert res["errors"] == [] and qs.open_questions() == []                # finished, not filed again
    assert sp.get(sid)["status"] != "open"


@pytest.mark.parametrize("action,kind,words", [("done", "done", "done"), ("clear", "dropped", "dropped")])
def test_done_and_clear_resolve_the_intention_and_close_the_row(tmp_path: Path, action, kind, words) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    qs.intention_act(q.id, action, channel="cockpit")
    got = sp.get(sid)
    assert got["status"] != "open" and got["resolution"]["kind"] == kind
    assert qs.get(q.id).answer.value == INTENTION_CLOSE + words


def test_a_question_typed_intention_is_answered_or_mooted(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path, typ="question", text="what about the thing?")
    qs.intention_act(q.id, "clear", channel="cockpit")
    assert sp.get(sid)["resolution"]["kind"] == "mooted"


def test_edit_changes_the_intention_text_and_leaves_the_row_open(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    qs.intention_act(sid, "edit", text="renew it on Friday", channel="cockpit")        # the intention id names it too
    assert sp.get(sid)["text"] == "renew it on Friday" and sp.get(sid)["germinated"]
    assert qs.get(q.id).answer is None


def test_refusals_change_nothing(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    plain = qs.file(agent="claude_code", agent_session_id="s", what="other", options=[("a", "A"), ("b", "B")], rec="a",
                    blocks="b", cwd=str(root))
    for call in (lambda: qs.intention_act(q.id, "snooze", until="2026-11-06", channel="cockpit"),
                 lambda: qs.intention_act(q.id, "snooze", until="soon", channel="cockpit"),
                 lambda: qs.intention_act(q.id, "snooze", until=None, channel="cockpit"),
                 lambda: qs.intention_act(q.id, "edit", text="  ", channel="cockpit"),
                 lambda: qs.intention_act(q.id, "teleport", channel="cockpit"),
                 lambda: qs.intention_act(q.id, "done", channel="agent"),
                 lambda: qs.intention_act(plain.id, "done", channel="cockpit"),
                 lambda: qs.intention_act("q-nope", "done", channel="cockpit")):
        with pytest.raises(ConsentError):
            call()
    assert sp.get(sid)["status"] == "open" and sp.get(sid)["germinated"] and qs.get(q.id).answer is None
    qs.intention_act(q.id, "done", channel="cockpit")
    with pytest.raises(ConsentError) as exc:                                    # a second act on a closed row
        qs.intention_act(q.id, "done", channel="cockpit")
    assert "already answered" in str(exc.value)


def test_an_agenda_edited_elsewhere_still_snoozes_from_a_fresh_read(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    sp.update(sid, text="someone edited it")
    qs.intention_act(q.id, "snooze", until="2026-11-12", channel="cockpit")       # a fresh read of the intention: lands
    assert sp.get(sid)["next"] == "2026-11-12"


def _rig_with_native(tmp_path: Path):
    rig = Rig(tmp_path, band=False, board=False)
    intentions = rig.root / ".levain" / "memory.spores.json"
    qs = QuestionStore(rig.qdir, clock=lambda: NOW, intentions_json=intentions)
    q, _ = qs.file_once(agent="claude_code", key="levain-germinate:k1", what="Due 11-08: renew the thing (sid)",
                        options=[("done", "Done")], rec="done", blocks="b", cwd=str(rig.root), held_by=ANSWER_CHANNELS)
    plain = qs.file(agent="claude_code", agent_session_id="s", what="Ship it?", options=[("a", "A"), ("b", "B")], rec="a",
                    blocks="b", cwd=str(rig.root))
    sid = rig.sp.add(type="task", text="renew the thing", disposition="agenda", next="2026-11-08", today=TODAY)["id"]
    rig.sp.update(sid, germinated={"on": "2026-11-08", "target": "band", "ref": q.id}, today=TODAY)
    rig.ck.attach_question_rows(qs)
    return rig, qs, q, plain, sid


def _fire(rig: Rig, panel: str, row: dict, verb: str, params: dict, confirm: bool = False) -> dict:
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                    install_class=INSTALL_CLASS,
                    req={"verb": verb, "panel_id": panel, "row_id": row["id"], "row_version": row["version"],
                         "params": params, "confirm": confirm})


def test_the_cockpit_offers_a_native_ringing_row_the_four_and_a_plain_row_its_answer(tmp_path: Path) -> None:
    rig, qs, q, plain, sid = _rig_with_native(tmp_path)
    dec = {r["id"]: r for r in rig.ck.panel("decisions", credential_class="token")["rows"]}
    assert {a["verb"] for a in dec[f"question:{q.n}"]["actions"]} == FOUR
    assert {a["verb"] for a in dec[f"question:{plain.n}"]["actions"]} == {"question_answer"}
    lst = rig.rows()
    assert {a["verb"] for a in lst[f"band:{q.id}"]["actions"]} == FOUR
    assert f"intention:{sid}" not in lst                                            # one row for the intention


def test_a_native_snooze_from_the_list_leaves_the_band_and_shows_the_agenda_with_its_wake(tmp_path: Path) -> None:
    rig, qs, q, plain, sid = _rig_with_native(tmp_path)
    out = _fire(rig, "tray", rig.rows()[f"band:{q.id}"], "intention_snooze", {"until": "2026-11-20"})
    assert out["intention"] == "snooze"
    rows = rig.rows()
    assert f"band:{q.id}" not in rows and f"band:{plain.id}" in rows
    assert rig.sp.get(sid)["next"] == "2026-11-20" and not rig.sp.get(sid).get("germinated")


def test_a_native_clear_from_decisions_needs_its_confirm(tmp_path: Path) -> None:
    rig, qs, q, plain, sid = _rig_with_native(tmp_path)
    row = next(r for r in rig.ck.panel("decisions", credential_class="token")["rows"] if r["id"] == f"question:{q.n}")
    with pytest.raises(EditError):
        _fire(rig, "decisions", row, "intention_clear", {})
    assert qs.get(q.id).answer is None
    _fire(rig, "decisions", row, "intention_clear", {}, confirm=True)
    assert qs.get(q.id).answer is not None and rig.sp.get(sid)["status"] != "open"


# --- fix round 1 ---------------------------------------------------------------------------------------------------------


def test_the_store_offers_the_intentions_own_words_for_the_edit_field(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path, text="renew the thing")
    assert q.what != "renew the thing"                            # the row reads as the band filed it ("Due ...: ...")
    assert questions_mod.intention_text_of(qs, q) == "renew the thing"


def test_an_unchanged_edit_writes_nothing(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path, text="renew the thing")
    before = intention_version(sp.get(sid))
    qs.intention_act(q.id, "edit", text="renew the thing", channel="cockpit")
    assert intention_version(sp.get(sid)) == before and sp.get(sid)["text"] == "renew the thing"


def test_an_edit_is_read_back_at_once_whatever_the_file_clock_says(tmp_path: Path) -> None:
    """A filesystem whose clock is coarser than two writes (G1, D3): the store's own write forgets what it had read."""
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path, text="renew the thing")
    assert questions_mod.intention_text_of(qs, q) == "renew the thing"     # read, and kept while the file looks unchanged
    st = intentions.stat()
    qs.intention_act(q.id, "edit", text="RENEW the thing", channel="cockpit")    # same length
    os.utime(intentions, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert intentions.stat().st_size == st.st_size
    assert questions_mod.intention_text_of(qs, q) == "RENEW the thing"


def test_a_snooze_to_today_or_a_malformed_time_reads_in_plain_words(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    with pytest.raises(ConsentError) as exc:
        qs.intention_act(q.id, "snooze", until="2026-11-07", channel="cockpit")
    assert str(exc.value) == "pick a later day"
    with pytest.raises(ConsentError) as exc2:
        qs.intention_act(q.id, "snooze", until="2026-11-20T25:99", channel="cockpit")
    assert "pick a day" in str(exc2.value) and "YYYY" not in str(exc2.value)
    assert sp.get(sid)["germinated"] and qs.get(q.id).answer is None


def test_a_moved_intention_is_refused_as_stale_and_a_bad_date_is_not(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    with pytest.raises(ConsentError) as bad:
        qs.intention_act(q.id, "snooze", until="2026-11-01", channel="cockpit")
    assert not getattr(bad.value, "stale", False)
    qs.intention_act(q.id, "done", channel="cockpit")
    with pytest.raises(ConsentError) as gone:
        qs.intention_act(q.id, "done", channel="cockpit")
    assert getattr(gone.value, "stale", False) is True
    with pytest.raises(ConsentError) as gone2:
        qs.intention_act(sid, "clear", channel="cockpit")             # by the intention id: it rings no open row now
    assert getattr(gone2.value, "stale", False) is True


@pytest.mark.parametrize("action", ["snooze", "done", "clear"])
def test_a_row_that_cannot_be_closed_after_the_reminder_moved_reports_the_act(tmp_path: Path, monkeypatch, action) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    real = QuestionStore._record

    def failing(conn, qq, kind, value, channel, stamp):
        if value.startswith(INTENTION_CLOSE):
            raise sqlite3.OperationalError("disk I/O error")
        return real(conn, qq, kind, value, channel, stamp)

    monkeypatch.setattr(QuestionStore, "_record", staticmethod(failing))
    said = qs.intention_act(q.id, action, until="2026-11-12" if action == "snooze" else None, channel="cockpit")
    assert "the row could not be closed" in said and "disk I/O error" in said
    got = sp.get(sid)
    assert (got["next"] == "2026-11-12") if action == "snooze" else (got["status"] != "open")


def test_an_answer_landing_first_leaves_the_reminder_untouched(tmp_path: Path) -> None:
    root, intentions, qdir, sp, sid, qs, q = _ringing(tmp_path)
    QuestionStore(qdir, clock=lambda: RUN_DAY).answer(q.id, text="did it", channel="phone")   # the phone, a moment before
    with pytest.raises(ConsentError) as exc:
        qs.intention_act(q.id, "clear", channel="cockpit")
    assert getattr(exc.value, "stale", False) is True
    assert sp.get(sid)["status"] == "open"


def test_a_re_rung_row_is_stale_for_the_page_that_saw_the_first_reminder(tmp_path: Path) -> None:
    """C1: the row version carries the reminder it rings, so Done on a row that now rings another reminder is a 409 and the
    other reminder is untouched."""
    rig, qs, q, plain, sid = _rig_with_native(tmp_path)
    for panel, row in (("tray", rig.rows()[f"band:{q.id}"]),
                       ("decisions", next(r for r in rig.ck.panel("decisions", credential_class="token")["rows"]
                                          if r["id"] == f"question:{q.n}"))):
        other = rig.sp.add(type="task", text="another reminder", disposition="agenda", next="2026-11-08", today=TODAY)["id"]
        rig.sp.update(sid, germinated={}, today=TODAY)
        rig.sp.update(other, germinated={"on": "2026-11-08", "target": "band", "ref": q.id}, today=TODAY)
        with pytest.raises(EditError) as exc:
            _fire(rig, panel, row, "intention_done", {})
        assert exc.value.http_status == 409, panel
        assert rig.sp.get(other)["status"] == "open" and qs.get(q.id).answer is None
        rig.sp.update(other, germinated={}, today=TODAY)
        rig.sp.update(sid, germinated={"on": "2026-11-08", "target": "band", "ref": q.id}, today=TODAY)


def test_the_list_row_draws_the_reminders_own_words(tmp_path: Path) -> None:
    rig, qs, q, plain, sid = _rig_with_native(tmp_path)
    row = rig.rows()[f"band:{q.id}"]
    assert row["title"] == "renew the thing" and row["facets"]["intention_text"] == "renew the thing"
