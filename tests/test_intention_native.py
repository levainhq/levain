"""Intention controls, S5: a native QuestionStore projects the intention a band row rings from its own spore store and acts on
it through the same ``intention_act`` a host offers. Temp dirs only."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from anneal_memory import SporeStore

from levain import germinate as g
from levain.cockpit.consent import ConsentError
from levain.cockpit.verbs import dispatch
from levain.consent.questions import ANSWER_CHANNELS, INTENTION_CLOSE, QuestionStore
from levain.spores import guarded_spore_store
from levain.writes import EditError
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, NOW, Rig, TODAY

FOUR = {"intention_snooze", "intention_edit", "intention_done", "intention_clear"}


def _install(tmp_path: Path):
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    spores = root / ".levain" / "memory.spores.json"
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    return root, spores, qdir


def _ringing(tmp_path: Path, *, today: date = date(2026, 11, 7), text: str = "renew the thing", typ: str = "task"):
    """An install whose agenda has fired into a band row: germinate ran, so the row is open and the agenda is stamped."""
    root, spores, qdir = _install(tmp_path)
    sp = guarded_spore_store(spores)
    sid = sp.add(type=typ, text=text, disposition="agenda", next="2026-11-06")["id"]
    res = g.germinate(spores, qdir, install=root, today=today)
    assert not res["errors"], res
    qs = QuestionStore(qdir)
    (q,) = qs.open_questions()
    return root, spores, qdir, sp, sid, qs, q


def test_the_projection_names_the_spore_a_stamped_open_agenda_rings(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    assert qs.intention_for(q) == sid
    plain = qs.file(agent="claude_code", agent_session_id="s", what="other", options=[("a", "A"), ("b", "B")], rec="a",
                    blocks="b", cwd=str(root))
    assert qs.intention_for(plain) is None


def test_snooze_moves_the_spore_closes_the_row_and_the_wake_files_a_new_one(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    said = qs.intention_act(q.id, "snooze", until="2026-11-12", channel="cockpit")
    assert "snoozed to 2026-11-12" in said
    got = sp.get(sid)
    assert got["next"] == "2026-11-12" and not got.get("germinated") and got["status"] == "open"
    closed = qs.get(q.id)
    assert closed.answer is not None and closed.answer.value == INTENTION_CLOSE + "snoozed to 2026-11-12"
    assert closed.answer.channel == "cockpit" and qs.open_questions() == []
    assert g.germinate(spores, qdir, install=root, today=date(2026, 11, 8))["due"] == []        # not yet
    res = g.germinate(spores, qdir, install=root, today=date(2026, 11, 12))
    assert not res["errors"] and [o["spore_id"] for o in res["due"]] == [sid]
    (again,) = qs.open_questions()
    assert again.id != q.id and qs.intention_for(again) == sid


def test_an_ordinary_answer_still_holds_the_agendas_key(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    qs.answer(q.id, text="did it", channel="cli")
    res = g.germinate(spores, qdir, install=root, today=date(2026, 11, 9))
    assert res["errors"] == [] and qs.open_questions() == []                # finished, not filed again
    assert sp.get(sid)["status"] != "open"


@pytest.mark.parametrize("action,kind,words", [("done", "done", "done"), ("clear", "dropped", "dropped")])
def test_done_and_clear_resolve_the_spore_and_close_the_row(tmp_path: Path, action, kind, words) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    qs.intention_act(q.id, action, channel="cockpit")
    got = sp.get(sid)
    assert got["status"] != "open" and got["resolution"]["kind"] == kind
    assert qs.get(q.id).answer.value == INTENTION_CLOSE + words


def test_a_question_typed_intention_is_answered_or_mooted(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path, typ="question", text="what about the thing?")
    qs.intention_act(q.id, "clear", channel="cockpit")
    assert sp.get(sid)["resolution"]["kind"] == "mooted"


def test_edit_changes_the_spore_text_and_leaves_the_row_open(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    qs.intention_act(sid, "edit", text="renew it on Friday", channel="cockpit")        # the spore id names it too
    assert sp.get(sid)["text"] == "renew it on Friday" and sp.get(sid)["germinated"]
    assert qs.get(q.id).answer is None


def test_refusals_change_nothing(tmp_path: Path) -> None:
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
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
    root, spores, qdir, sp, sid, qs, q = _ringing(tmp_path)
    sp.update(sid, text="someone edited it")
    qs.intention_act(q.id, "snooze", until="2026-11-12", channel="cockpit")       # a fresh read of the spore: lands
    assert sp.get(sid)["next"] == "2026-11-12"


def _rig_with_native(tmp_path: Path):
    rig = Rig(tmp_path, band=False, board=False)
    spores = rig.root / ".levain" / "memory.spores.json"
    qs = QuestionStore(rig.qdir, clock=lambda: NOW, spores_json=spores)
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
    assert f"spore:{sid}" not in lst                                            # one row for the intention


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
