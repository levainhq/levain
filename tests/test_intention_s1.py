"""Intention controls, S0 + S1: a fired agenda that filed a band row stays open (its stamp names the row), so the List must
draw ONE row for it, the generic spore verbs must not move it, and germinate / band snooze must not close or defer it
behind the stamp's back. Temp dirs only."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from anneal_memory import SporeStore

from levain import germinate as g
from levain.cockpit.verbs import dispatch
from levain.consent import band
from levain.consent.questions import ANSWER_CHANNELS, QuestionStore
from levain.spores import guarded_spore_store
from levain.writes import EditError
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, NOW, Rig, TODAY

GTODAY = date(2026, 11, 7)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _stamped_agenda(rig: Rig) -> str:
    """An open agenda carrying the stamp flow 8's germinate leaves: ``germinated.ref`` names the open band row."""
    sid = rig.sp.add(type="task", text="renew the thing", disposition="agenda", next="2026-11-08", today=TODAY)["id"]
    rig.sp.update(sid, germinated={"on": "2026-11-08", "target": "band", "ref": rig.q.id}, today=TODAY)
    return sid


def _fire(rig: Rig, rid: str, verb: str, params: dict) -> dict:
    row = rig.rows()[rid]
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                    install_class=INSTALL_CLASS,
                    req={"verb": verb, "panel_id": "tray", "row_id": rid, "row_version": row["version"],
                         "params": params, "confirm": True})


# --- S0 ---------------------------------------------------------------------------------------------------------------


def test_s0_i_one_intention_is_one_row(rig: Rig) -> None:
    sid = _stamped_agenda(rig)
    ids = set(rig.rows())
    assert f"band:{rig.q.id}" in ids
    assert f"spore:{sid}" not in ids, "the intention is drawn twice: its band row and its spore row"


def test_s0_i_the_spore_snooze_cannot_orphan_the_ring(rig: Rig) -> None:
    from levain.cockpit.providers import spore_row_version

    sid = _stamped_agenda(rig)
    with pytest.raises(EditError) as exc:                       # reachable by a stale page or a direct call
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS,
                 req={"verb": "spore_surface_at", "panel_id": "tray", "row_id": f"spore:{sid}",
                      "row_version": spore_row_version(rig.sp.get(sid)),
                      "params": {"surface_at": "2026-12-01"}, "confirm": True})
    assert exc.value.http_status == 409 and "band" in str(exc.value)
    assert rig.sp.get(sid)["germinated"]["ref"] == rig.q.id


def test_s0_ii_a_decisions_question_row_is_offered_only_question_answer(rig: Rig) -> None:
    rows = rig.ck.panel("decisions", credential_class="token")["rows"]
    qrows = [r for r in rows if r["id"].startswith("question:")]
    assert qrows
    assert all({a["verb"] for a in r["actions"]} == {"question_answer"} for r in qrows)


def test_s1_an_unreadable_band_keeps_the_spore_row_and_the_guard_still_refuses_it(rig: Rig, monkeypatch) -> None:
    from levain import band_view

    sid = _stamped_agenda(rig)

    def boom(store):
        raise RuntimeError("band down")

    monkeypatch.setattr(band_view, "_read", boom)
    assert f"spore:{sid}" in rig.rows()
    with pytest.raises(EditError) as exc:
        _fire(rig, f"spore:{sid}", "spore_surface_at", {"surface_at": "2026-12-01"})
    assert exc.value.http_status == 409


def test_s1_a_plain_agenda_and_a_seed_are_still_written_by_the_spore_verbs(rig: Rig) -> None:
    out = _fire(rig, f"spore:{rig.ids['plain']}", "spore_touch", {})
    assert out
    out = _fire(rig, f"spore:{rig.ids['agenda_old']}", "spore_surface_at", {"surface_at": "2026-12-01"})
    assert out and rig.sp.get(rig.ids["agenda_old"])["next"] == "2026-12-01"


def test_s1_a_band_snooze_of_a_question_an_open_agenda_rings_is_refused_naming_the_spore_verb(tmp_path: Path) -> None:
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    sp = guarded_spore_store(root / ".levain" / "memory.spores.json")
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    qs = QuestionStore(qdir)
    q, _ = qs.file_once(agent="claude_code", key="k-ring", what="Due: x", options=[("done", "Done")], rec="done",
                        blocks="b", cwd=str(root), held_by=ANSWER_CHANNELS)
    plain = qs.file(agent="claude_code", agent_session_id="s", what="other", options=[("a", "A"), ("b", "B")], rec="a",
                    blocks="b", cwd=str(root))
    sid = sp.add(type="task", text="x", disposition="agenda", next="2026-11-01")["id"]
    sp.update(sid, germinated={"on": "2026-11-07", "target": "band", "ref": q.id})
    from levain.cockpit.consent import ConsentError

    with pytest.raises(ConsentError) as exc:
        qs.snooze(q.id, until="2026-11-10", channel="cli", via="cli")
    assert sid in str(exc.value) and "spore_surface_at" in str(exc.value)
    assert qs.snoozes(q.id) == []
    qs.snooze(plain.id, until="2026-11-10", channel="cli", via="cli")           # a machine-filed row snoozes as before
    sp.descend(sid, kind="composted")                                         # the agenda closed: its row is plain again
    qs.snooze(q.id, until="2026-11-10", channel="cli", via="cli")


# --- L1 fix round -----------------------------------------------------------------------------------------------------


def test_f2_one_predicate_a_drop_stamp_a_closed_row_and_a_missing_row_do_not_ring() -> None:
    from levain.spores import ringing_ref

    ag = {"disposition": "agenda", "status": "open", "germinated": {"ref": "q-1"}}
    assert ringing_ref(ag, {"q-1"}) == "q-1"
    assert ringing_ref(ag, {"q-2"}) is None                                   # its question is not open
    assert ringing_ref({**ag, "germinated": {"ref": "dr-9"}}, {"dr-9"}) is None   # a coordinator drop is not a band row
    assert ringing_ref({**ag, "status": "resolved"}, {"q-1"}) is None


def test_f1a_a_spore_verb_on_an_agenda_whose_question_is_answered_is_not_refused(rig: Rig) -> None:
    sid = _stamped_agenda(rig)
    rig.qs.answer(rig.q.id, option="ship", channel="cli")
    out = _fire(rig, f"spore:{sid}", "spore_surface_at", {"surface_at": "2026-12-01"})
    assert out and rig.sp.get(sid)["next"] == "2026-12-01"


def test_f1b_germinate_closes_an_agenda_whose_question_was_answered(tmp_path: Path) -> None:
    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    spores = root / ".levain" / "memory.spores.json"
    sp = guarded_spore_store(spores)
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    qs = QuestionStore(qdir)
    q, _ = qs.file_once(agent="claude_code", key="k-fin", what="Due: x", options=[("done", "Done")], rec="done",
                        blocks="b", cwd=str(root), held_by=ANSWER_CHANNELS)
    sid = sp.add(type="task", text="x", disposition="agenda", next="2026-11-01")["id"]
    sp.update(sid, germinated={"on": "2026-11-07", "target": "band", "ref": q.id})
    res = g.germinate(spores, qdir, install=root, today=GTODAY)
    assert res["finish"] == [] and sp.get(sid)["status"] == "open"          # still ringing: left alone
    qs.answer(q.id, option="done", channel="cli")
    res = g.germinate(spores, qdir, install=root, today=GTODAY)
    assert [o["spore_id"] for o in res["finish"]] == [sid] and res["errors"] == []
    got = sp.get(sid)
    assert got["status"] != "open" and got["resolution"]["kind"] == "done"


def test_f3_the_tui_write_path_is_guarded_too(rig: Rig, monkeypatch) -> None:
    from levain import writes

    monkeypatch.setattr("levain.consent.queue.default_queue_dir", lambda: rig.qdir)
    sid = _stamped_agenda(rig)
    scope = rig.src.write_scope
    for req in ({"kind": "spore_surface_at", "spore_id": sid, "surface_at": "2026-12-01"},
                {"kind": "spore_touch", "spore_id": sid},
                {"kind": "spore_update", "spore_id": sid, "text": "new words"}):
        with pytest.raises(EditError) as exc:
            writes.apply_edit(scope, req)
        assert exc.value.http_status == 409, req
    assert rig.sp.get(sid)["next"] == "2026-11-08"


def test_f4_the_snooze_guard_reads_the_spore_store_it_is_given_not_the_questions_cwd(tmp_path: Path) -> None:
    from levain.cockpit.consent import ConsentError

    root = tmp_path / "inst"
    (root / ".levain").mkdir(parents=True)
    spores = root / ".levain" / "memory.spores.json"
    sp = guarded_spore_store(spores)
    qdir = tmp_path / "q"
    qdir.mkdir(mode=0o700)
    qs = QuestionStore(qdir)
    q, _ = qs.file_once(agent="claude_code", key="k-f4", what="Due: x", options=[("done", "Done")], rec="done",
                        blocks="b", cwd=str(tmp_path / "elsewhere"), held_by=ANSWER_CHANNELS)
    sid = sp.add(type="task", text="x", disposition="agenda", next="2026-11-01")["id"]
    sp.update(sid, germinated={"on": "2026-11-07", "target": "band", "ref": q.id})
    with pytest.raises(ConsentError) as exc:
        qs.snooze(q.id, until="2026-11-10", channel="cli", via="cli", spores_json=spores)
    assert sid in str(exc.value)


def test_f6_a_stamp_landing_after_the_read_stales_the_spore_row_version(rig: Rig) -> None:
    from levain.cockpit.providers import spore_row_version

    sid = rig.ids["agenda_old"]
    before = spore_row_version(rig.sp.get(sid))
    rig.sp.update(sid, germinated={"on": "2026-11-08", "target": "band", "ref": "q-x"}, today=TODAY)
    assert spore_row_version(rig.sp.get(sid)) != before
