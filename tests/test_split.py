"""Row 178 build 2: the split. Two linked intentions, the first part's completion waking the second (``on_event: done:<id>``).
Temp dirs only: no ~/.levain, no real questions.db, no live app. The ping seam is replaced; the band and the desk are the
real stores in a temp consent dir."""
from __future__ import annotations

import dataclasses
import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest
from anneal_memory import IntentionStore

from levain import germinate as g
from levain import split
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.desk.store import CoordStore
from levain.intentions import guarded_intention_store
from levain.writes import EditError, WriteScope, apply_edit

TODAY = date(2026, 11, 11)


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


def _install(tmp_path, name="a") -> Path:
    root = tmp_path / name
    (root / ".levain").mkdir(parents=True)
    return root


def _intentions(root) -> Path:
    return root / ".levain" / "memory.spores.json"


def _anchor(root, text="call Jim", **kw):
    """The first part: an operator task the operator marks done."""
    return guarded_intention_store(_intentions(root)).add(type="task", text=text, disposition="agenda", target="band", **kw)


def _follower(root, anchor_id, text="send the invoice", target="band", check_by="2026-11-18", **kw):
    return guarded_intention_store(_intentions(root)).add(
        type="task", text=text, disposition="agenda", target=target, on_event=f"done:{anchor_id}", check_by=check_by, **kw)


def _run(root, qdir, **kw):
    kw.setdefault("today", TODAY)
    return g.germinate(_intentions(root), qdir, install=root, **kw)


def _intention(root, sid):
    return IntentionStore(_intentions(root), id_prefix="intention").get(sid)


def _done(root, sid, kind="done"):
    guarded_intention_store(_intentions(root)).descend(sid, kind=kind)


def test_a_linked_part_wakes_when_its_first_part_is_done(tmp_path, qdir, pings):
    """The reproduced failure: a part linked on ``done:<id>`` never woke after the part it waits on was resolved."""
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    assert _run(root, qdir)["due"] == []                      # the first part is open: the second waits
    _done(root, a["id"])
    res = _run(root, qdir)
    assert [o["intention_id"] for o in res["due"]] == [f["id"]]
    stamp = _intention(root, f["id"])["germinated"]
    assert stamp["target"] == "band" and stamp["ref"].startswith("q-")
    assert [q.id for q in QuestionStore(qdir).open_questions()] == [stamp["ref"]]


def _rows(qdir):
    return QuestionStore(qdir).recent(100)


def _drops(qdir) -> int:
    with sqlite3.connect(qdir / "dispatch.db") as c:
        return c.execute("SELECT count(*) FROM drops").fetchone()[0]


# --- what wakes it, and what does not -----------------------------------------------------------------------------
@pytest.mark.parametrize("kind", ["dropped", "composted"])
def test_a_first_part_that_did_not_finish_never_wakes_the_second(tmp_path, qdir, pings, kind):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    _done(root, a["id"], kind)
    res = _run(root, qdir)
    assert res["due"] == [] and _intention(root, f["id"]).get("germinated") is None


def test_a_missing_first_part_wakes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    f = _follower(root, "intention-099")
    assert _run(root, qdir)["due"] == [] and _intention(root, f["id"]).get("germinated") is None


def test_an_event_agenda_is_never_due_by_a_date_even_with_a_next(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"], next="2026-11-01")
    assert g.is_due(_intention(root, f["id"]), TODAY) is False


def test_the_wake_is_idempotent_a_rerun_files_nothing_twice(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    _done(root, a["id"])
    first, second = _run(root, qdir), _run(root, qdir)
    assert len(first["due"]) == 1 and second["due"] == []
    assert len(_rows(qdir)) == 1


def test_a_crash_between_the_filing_and_the_stamp_lands_one_row(tmp_path, qdir, pings):
    """The state a crash leaves: the band row exists, the second part is still unstamped. The retry adopts that row."""
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    _done(root, a["id"])
    q, _ = QuestionStore(qdir).file_once(agent="claude_code", key=g.KEY_PREFIX + f["origin_key"], what="w", options=[("done", "Done")],
                                         rec="done", blocks="b", cwd=str(root.resolve()))
    res = _run(root, qdir)
    assert len(_rows(qdir)) == 1 and res["due"][0]["ref"] == q.id and res["due"][0]["filed"] is False
    assert _intention(root, f["id"])["germinated"]["ref"] == q.id


def test_a_dry_run_wakes_and_writes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    _follower(root, a["id"])
    _done(root, a["id"])
    before = hashlib.sha256(_intentions(root).read_bytes()).hexdigest()
    res = _run(root, qdir, dry_run=True)
    assert len(res["due"]) == 1 and res["due"][0]["done"] is False
    assert hashlib.sha256(_intentions(root).read_bytes()).hexdigest() == before and not (qdir / "questions.db").exists()


def test_an_event_run_wakes_only_that_events_followers_and_checks_the_store(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a1, a2 = _anchor(root, "one"), _anchor(root, "two")
    f1, f2 = _follower(root, a1["id"], "after one"), _follower(root, a2["id"], "after two")
    _done(root, a1["id"])
    _done(root, a2["id"])
    res = _run(root, qdir, event=f"done:{a1['id']}")
    assert [o["intention_id"] for o in res["due"]] == [f1["id"]] and _intention(root, f2["id"]).get("germinated") is None
    # a key whose first part is still open wakes nothing, whatever the caller asserts
    a3 = _anchor(root, "three")
    f3 = _follower(root, a3["id"], "after three")
    assert _run(root, qdir, event=f"done:{a3['id']}")["due"] == [] and _intention(root, f3["id"]).get("germinated") is None


def test_the_second_part_may_be_the_machines_operator_then_machine(tmp_path, qdir, pings):
    """The design's own example: call Jim (his), then the BOBS backfill (a board row)."""
    root = _install(tmp_path)
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(tmp_path.resolve()))
    a = _anchor(root, "call Jim")
    f = _follower(root, a["id"], "BOBS backfill", target="desk")
    _done(root, a["id"])
    res = _run(root, qdir)
    assert res["due"][0]["via"] == "desk" and res["due"][0]["event"] == f"done:{a['id']}"
    assert _drops(qdir) == 1 and _intention(root, f["id"])["status"] == "resolved"


def test_a_chain_wakes_one_link_per_finish(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root, "one")
    b = _follower(root, a["id"], "two")
    c = _follower(root, b["id"], "three")
    _done(root, a["id"])
    assert [o["intention_id"] for o in _run(root, qdir)["due"]] == [b["id"]]
    _done(root, b["id"])
    assert [o["intention_id"] for o in _run(root, qdir)["due"]] == [c["id"]]


def test_the_finish_pass_that_closes_an_answered_first_part_wakes_the_second_in_the_same_run(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="call Jim", disposition="agenda", target="band", next="2026-11-05")
    f = _follower(root, a["id"])
    first = _run(root, qdir)                       # the first part comes due and rings in the band
    assert [o["intention_id"] for o in first["due"]] == [a["id"]]
    QuestionStore(qdir).answer(_intention(root, a["id"])["germinated"]["ref"], option="done", channel="cli")
    second = _run(root, qdir)
    assert [o["intention_id"] for o in second["finish"]] == [a["id"]]
    assert [o["intention_id"] for o in second["due"]] == [f["id"]]


# --- the check-by: nothing waits silently --------------------------------------------------------------------------
def test_the_check_by_rings_once_unstamped_and_the_event_can_still_wake_it(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"], check_by="2026-11-11")
    early = _follower(root, a["id"], "not yet", check_by="2026-11-12")
    res = _run(root, qdir)
    assert [c["intention_id"] for c in res["checkby"]] == [f["id"]] and res["due"] == []
    assert _run(root, qdir)["checkby"][0]["via"] == "ringing" and len(_rows(qdir)) == 1     # one row, however often it runs
    row = _rows(qdir)[0]
    assert "Still waiting on done:" in row.what and f["id"] in row.what and early["id"] not in row.what
    assert _intention(root, f["id"]).get("germinated") is None                                   # the link stays live
    _done(root, a["id"])
    woke = _run(root, qdir)
    assert sorted(o["intention_id"] for o in woke["due"]) == sorted([f["id"], early["id"]])


def test_keeping_it_moves_the_check_by_a_week_and_dropping_it_ends_the_agenda(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    keep, drop = _follower(root, a["id"], "keep me", check_by="2026-11-10"), _follower(root, a["id"], "drop me", check_by="2026-11-10")
    _run(root, qdir)
    qs = QuestionStore(qdir)
    for sid, option in ((keep["id"], "keep"), (drop["id"], "drop")):
        ref = next(q.id for q in qs.open_questions() if sid in q.what)
        qs.answer(ref, option=option, channel="cli")
    res = _run(root, qdir)
    assert {c["intention_id"]: c["via"] for c in res["checkby"]} == {keep["id"]: "kept", drop["id"]: "dropped"}
    assert _intention(root, keep["id"])["check_by"] == "2026-11-18" and _intention(root, keep["id"])["status"] == "open"
    assert _intention(root, drop["id"])["status"] == "resolved"
    assert _run(root, qdir).get("checkby") is None                                           # kept: asked again on its new day


def test_an_event_agenda_made_with_no_check_by_rings_seven_days_after_it_was_made(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = guarded_intention_store(_intentions(root)).add(type="task", text="hand made", disposition="agenda", target="band",
                                               on_event=f"done:{a['id']}", today=date(2026, 11, 4))
    assert _run(root, qdir, today=date(2026, 11, 10)).get("checkby") is None
    assert _run(root, qdir)["checkby"][0]["intention_id"] == f["id"]


# --- the writers that resolve the first part wake the second now ----------------------------------------------------
def _scope(root, qdir):
    return dataclasses.replace(WriteScope.from_install_root(root), questions=QuestionStore(qdir))


def test_the_legacy_write_that_marks_it_done_wakes_the_second_part_now(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="call Jim", disposition="someday")
    f = _follower(root, a["id"])
    res = apply_edit(_scope(root, qdir), {"kind": "intention_close", "intention_id": a["id"], "intention_kind": "done", "confirm": True})
    assert res["woke"] == [f["id"]] and len(_rows(qdir)) == 1
    assert _intention(root, f["id"])["germinated"]["ref"].startswith("q-")


def test_the_legacy_write_that_composts_it_wakes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="call Jim", disposition="someday")
    _follower(root, a["id"])
    res = apply_edit(_scope(root, qdir), {"kind": "intention_close", "intention_id": a["id"], "intention_kind": "composted", "confirm": True})
    assert "woke" not in res and not (qdir / "questions.db").exists()


def test_a_wake_that_fails_never_fails_the_resolve(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="call Jim", disposition="someday")
    f = _follower(root, a["id"])
    monkeypatch.setattr(g, "germinate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("band store down")))
    with pytest.warns(RuntimeWarning, match="band store down"):
        res = apply_edit(_scope(root, qdir), {"kind": "intention_close", "intention_id": a["id"], "intention_kind": "done", "confirm": True})
    assert res["ok"] and "band store down" in res["wake_error"] and _intention(root, a["id"])["status"] == "resolved"
    monkeypatch.undo()
    assert [o["intention_id"] for o in _run(root, qdir)["due"]] == [f["id"]]        # the next ordinary run converges


def test_the_band_card_done_wakes_the_second_part_now(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="call Jim", disposition="agenda", target="band", next="2026-11-05")
    f = _follower(root, a["id"])
    _run(root, qdir)                                                  # the first part rings in the band
    said = QuestionStore(qdir).intention_act(a["id"], "done", channel="cockpit")
    assert f"woke {f['id']}" in said and _intention(root, f["id"])["germinated"]["ref"].startswith("q-")


def test_clearing_the_band_card_wakes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="call Jim", disposition="agenda", target="band", next="2026-11-05")
    f = _follower(root, a["id"])
    _run(root, qdir)
    assert "woke" not in QuestionStore(qdir).intention_act(a["id"], "clear", channel="cockpit")
    assert _intention(root, f["id"]).get("germinated") is None


# --- creating the second part ("then you") -------------------------------------------------------------------------
def test_then_you_files_a_band_agenda_linked_to_this_item_with_a_week_to_check(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    made = split.create_then(_intentions(root), a["id"], "  send the invoice  ", today=TODAY)
    row = _intention(root, made["intention_id"])
    assert (row["disposition"], row["target"], row["type"], row["text"]) == ("agenda", "band", "task", "send the invoice")
    assert row["on_event"] == f"done:{a['id']}" and row["check_by"] == "2026-11-18" and made["replay"] is False
    assert "note" not in made


def test_then_you_twice_lands_one_row_and_says_it_replayed(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    one = split.create_then(_intentions(root), a["id"], "send the invoice", today=TODAY)
    two = split.create_then(_intentions(root), a["id"], "send the invoice", today=TODAY)
    assert two["intention_id"] == one["intention_id"] and two["replay"] is True
    assert len([s for s in IntentionStore(_intentions(root), id_prefix="intention").list_open() if s.get("on_event")]) == 1
    assert split.create_then(_intentions(root), a["id"], "send a reminder", today=TODAY)["intention_id"] != one["intention_id"]


@pytest.mark.parametrize("words,check_by,why", [("", None, "needs words"), ("go", "soon", "YYYY-MM-DD"), ("go", "2026-11-10", "already past")])
def test_then_you_refuses_what_it_cannot_make(tmp_path, words, check_by, why):
    root = _install(tmp_path)
    a = _anchor(root)
    with pytest.raises(split.ThenRefused, match=why):
        split.create_then(_intentions(root), a["id"], words, check_by=check_by, today=TODAY)


def test_then_you_refuses_a_resolved_or_a_loop_first_part(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    loop = guarded_intention_store(_intentions(root)).add(type="task", text="a loop")
    _done(root, a["id"])
    with pytest.raises(split.ThenRefused) as gone:
        split.create_then(_intentions(root), a["id"], "next", today=TODAY)
    assert gone.value.status == 409
    with pytest.raises(split.ThenRefused, match="not a loop"):
        split.create_then(_intentions(root), loop["id"], "next", today=TODAY)


def test_then_you_on_a_machine_part_says_it_will_not_wake_on_done(tmp_path):
    root = _install(tmp_path)
    a = guarded_intention_store(_intentions(root)).add(type="task", text="draft it", disposition="agenda", target="desk")
    made = split.create_then(_intentions(root), a["id"], "send it", today=TODAY)
    assert "check-by day (2026-11-18)" in made["note"]


def test_the_verb_binds_the_row_version_and_audits(tmp_path, qdir):
    root = _install(tmp_path)
    a = _anchor(root)
    scope = _scope(root, qdir)
    from levain.cockpit.providers import intention_row_version

    ver = intention_row_version(_intention(root, a["id"]))
    res = apply_edit(scope, {"kind": "intention_then", "intention_id": a["id"], "text": "send the invoice",
                             "expect_disposition": "agenda"}, expected_version=ver)
    assert res["ok"] and res["action"] == "then" and res["event"] == f"done:{a['id']}"
    guarded_intention_store(_intentions(root)).update(a["id"], text="call Jim, then Sam")
    with pytest.raises(EditError) as stale:
        apply_edit(scope, {"kind": "intention_then", "intention_id": a["id"], "text": "another", "expect_disposition": "agenda"},
                   expected_version=ver)
    assert stale.value.http_status == 409
    assert len([s for s in IntentionStore(_intentions(root), id_prefix="intention").list_open() if s.get("on_event")]) == 1


# --- the List shows the two parts linked ----------------------------------------------------------------------------
def _list_rows(panel):
    return {r["id"]: r for r in panel["rows"]}


def test_the_list_shows_a_waiting_part_in_later_with_its_event_and_the_first_part_its_followers(tmp_path):
    from tests.test_cockpit_list_panel import Rig, TODAY as LIST_TODAY

    r = Rig(tmp_path, band=False, board=False)
    a = r.ids["agenda_old"]
    later = r.sp.add(type="task", text="send the invoice then", disposition="agenda", target="band", on_event=f"done:{a}",
                     check_by=(LIST_TODAY.replace(day=LIST_TODAY.day + 5)).isoformat(), today=LIST_TODAY)
    due = r.sp.add(type="task", text="chase the reply", disposition="agenda", target="band", on_event=f"done:{a}",
                   check_by=LIST_TODAY.isoformat(), today=LIST_TODAY)
    rows = _list_rows(r.panel())
    lf = rows[f"intention:{later['id']}"]["facets"]
    assert lf["time_state"] == "later" and lf["on_event"] == f"done:{a}" and lf["after"] == a
    assert lf["after_text"] == "an agenda from last week"
    assert lf["wake_line"].startswith(f"on done:{a}, check by ") and lf["wake_line"].endswith("→ band")
    assert lf["check_by"] == later["check_by"]
    df = rows[f"intention:{due['id']}"]["facets"]
    assert df["time_state"] == "now" and df["wake_line"] == f"still waiting on done:{a}"
    assert sorted(rows[f"intention:{a}"]["facets"]["then"]) == sorted([later["id"], due["id"]])
    assert "then" not in rows[f"intention:{r.ids['plain']}"]["facets"]


def test_then_you_is_offered_on_an_agenda_and_not_on_a_someday_or_a_loop(tmp_path):
    from tests.test_cockpit_list_panel import Rig

    r = Rig(tmp_path, band=False, board=False)
    rows = _list_rows(r.panel())
    offered = {rid: any(x["verb"] == "intention_then" for x in row["actions"]) for rid, row in rows.items()}
    assert offered[f"intention:{r.ids['agenda_old']}"] is True and offered[f"intention:{r.ids['plain']}"] is False


def test_a_desk_first_part_is_composted_at_germination_and_never_wakes_the_second_on_done(tmp_path, qdir, pings):
    root = _install(tmp_path)
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(tmp_path.resolve()))
    a = guarded_intention_store(_intentions(root)).add(type="task", text="draft it", disposition="agenda", target="desk", next="2026-11-05")
    f = _follower(root, a["id"], "send it")
    res = _run(root, qdir)
    assert [o["intention_id"] for o in res["due"]] == [a["id"]] and _intention(root, a["id"])["resolution"]["kind"] == "composted"
    assert _run(root, qdir)["due"] == [] and _intention(root, f["id"]).get("germinated") is None
