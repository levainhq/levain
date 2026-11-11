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
from anneal_memory import SporeStore

from levain import germinate as g
from levain import split
from levain.cockpit.verbs import dispatch
from levain.consent import band
from levain.consent.questions import QuestionStore
from levain.desk.store import CoordStore
from levain.spores import guarded_spore_store
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


def _spores(root) -> Path:
    return root / ".levain" / "memory.spores.json"


def _anchor(root, text="call Jim", **kw):
    """The first part: an operator task the operator marks done."""
    return guarded_spore_store(_spores(root)).add(type="task", text=text, disposition="agenda", target="band", **kw)


def _follower(root, anchor_id, text="send the invoice", target="band", check_by="2026-11-18", **kw):
    return guarded_spore_store(_spores(root)).add(
        type="task", text=text, disposition="agenda", target=target, on_event=f"done:{anchor_id}", check_by=check_by, **kw)


def _run(root, qdir, **kw):
    kw.setdefault("today", TODAY)
    return g.germinate(_spores(root), qdir, install=root, **kw)


def _spore(root, sid):
    return SporeStore(_spores(root)).get(sid)


def _done(root, sid, kind="done"):
    guarded_spore_store(_spores(root)).descend(sid, kind=kind)


def test_a_linked_part_wakes_when_its_first_part_is_done(tmp_path, qdir, pings):
    """The reproduced failure: a part linked on ``done:<id>`` never woke after the part it waits on was resolved."""
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    assert _run(root, qdir)["due"] == []                      # the first part is open: the second waits
    _done(root, a["id"])
    res = _run(root, qdir)
    assert [o["spore_id"] for o in res["due"]] == [f["id"]]
    stamp = _spore(root, f["id"])["germinated"]
    assert stamp["target"] == "band" and stamp["ref"].startswith("q-") and stamp["event"] == f"done:{a['id']}"
    assert _spore(root, f["id"]).get("on_event") is None                       # the wake consumed the link
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
    assert res["due"] == [] and _spore(root, f["id"]).get("germinated") is None


def test_a_missing_first_part_wakes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    f = _follower(root, "intention-099")
    assert _run(root, qdir)["due"] == [] and _spore(root, f["id"]).get("germinated") is None


def test_an_event_agenda_is_never_due_by_a_date_even_with_a_next(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"], next="2026-11-01")
    assert g.is_due(_spore(root, f["id"]), TODAY) is False


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
    assert _spore(root, f["id"])["germinated"]["ref"] == q.id


def test_a_dry_run_wakes_and_writes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    _follower(root, a["id"])
    _done(root, a["id"])
    before = hashlib.sha256(_spores(root).read_bytes()).hexdigest()
    res = _run(root, qdir, dry_run=True)
    assert len(res["due"]) == 1 and res["due"][0]["done"] is False
    assert hashlib.sha256(_spores(root).read_bytes()).hexdigest() == before and not (qdir / "questions.db").exists()


def test_an_event_run_wakes_only_that_events_followers_and_checks_the_store(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a1, a2 = _anchor(root, "one"), _anchor(root, "two")
    f1, f2 = _follower(root, a1["id"], "after one"), _follower(root, a2["id"], "after two")
    _done(root, a1["id"])
    _done(root, a2["id"])
    res = _run(root, qdir, event=f"done:{a1['id']}")
    assert [o["spore_id"] for o in res["due"]] == [f1["id"]] and _spore(root, f2["id"]).get("germinated") is None
    # a key whose first part is still open wakes nothing, whatever the caller asserts
    a3 = _anchor(root, "three")
    f3 = _follower(root, a3["id"], "after three")
    assert _run(root, qdir, event=f"done:{a3['id']}")["due"] == [] and _spore(root, f3["id"]).get("germinated") is None


def test_the_second_part_may_be_the_machines_operator_then_machine(tmp_path, qdir, pings):
    """The design's own example: call Jim (his), then the BOBS backfill (a board row)."""
    root = _install(tmp_path)
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(tmp_path.resolve()))
    a = _anchor(root, "call Jim")
    f = _follower(root, a["id"], "BOBS backfill", target="desk")
    _done(root, a["id"])
    res = _run(root, qdir)
    assert res["due"][0]["via"] == "desk" and res["due"][0]["event"] == f"done:{a['id']}"
    assert _drops(qdir) == 1 and _spore(root, f["id"])["status"] == "resolved"


def test_a_chain_wakes_one_link_per_finish(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root, "one")
    b = _follower(root, a["id"], "two")
    c = _follower(root, b["id"], "three")
    _done(root, a["id"])
    assert [o["spore_id"] for o in _run(root, qdir)["due"]] == [b["id"]]
    _done(root, b["id"])
    assert [o["spore_id"] for o in _run(root, qdir)["due"]] == [c["id"]]


def test_the_finish_pass_that_closes_an_answered_first_part_wakes_the_second_in_the_same_run(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="agenda", target="band", next="2026-11-05")
    f = _follower(root, a["id"])
    first = _run(root, qdir)                       # the first part comes due and rings in the band
    assert [o["spore_id"] for o in first["due"]] == [a["id"]]
    QuestionStore(qdir).answer(_spore(root, a["id"])["germinated"]["ref"], option="done", channel="cli")
    second = _run(root, qdir)
    assert [o["spore_id"] for o in second["finish"]] == [a["id"]]
    assert [o["spore_id"] for o in second["due"]] == [f["id"]]


# --- the check-by: nothing waits silently --------------------------------------------------------------------------
def test_the_check_by_rings_once_unstamped_and_the_event_can_still_wake_it(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"], check_by="2026-11-11")
    early = _follower(root, a["id"], "not yet", check_by="2026-11-12")
    res = _run(root, qdir)
    assert [c["spore_id"] for c in res["checkby"]] == [f["id"]] and res["due"] == []
    assert _run(root, qdir)["checkby"][0]["via"] == "ringing" and len(_rows(qdir)) == 1     # one row, however often it runs
    row = _rows(qdir)[0]
    assert "Still waiting on done:" in row.what and f["id"] in row.what and early["id"] not in row.what
    assert _spore(root, f["id"]).get("germinated") is None                                   # the link stays live
    ring = row
    _done(root, a["id"])
    woke = _run(root, qdir)
    assert sorted(o["spore_id"] for o in woke["due"]) == sorted([f["id"], early["id"]])
    # the ring filed before the wake is closed by it, in words, and the result says so
    closed = QuestionStore(qdir).get(ring.id)
    assert closed.answer is not None and closed.answer.kind == "text" and "happened" in closed.answer.value
    assert [(c["via"], c["ref"]) for c in woke["checkby"]] == [("superseded", ring.id)]
    assert ring.id not in [q.id for q in QuestionStore(qdir).open_questions()] and len(QuestionStore(qdir).open_questions()) == 2


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
    assert {c["spore_id"]: c["via"] for c in res["checkby"]} == {keep["id"]: "kept", drop["id"]: "dropped"}
    assert _spore(root, keep["id"])["check_by"] == "2026-11-18" and _spore(root, keep["id"])["status"] == "open"
    assert _spore(root, drop["id"])["status"] == "resolved"
    assert _run(root, qdir).get("checkby") is None                                           # kept: asked again on its new day


def test_an_event_agenda_made_with_no_check_by_rings_seven_days_after_it_was_made(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = guarded_spore_store(_spores(root)).add(type="task", text="hand made", disposition="agenda", target="band",
                                               on_event=f"done:{a['id']}", today=date(2026, 11, 4))
    assert _run(root, qdir, today=date(2026, 11, 10)).get("checkby") is None
    assert _run(root, qdir)["checkby"][0]["spore_id"] == f["id"]


# --- the writers that resolve the first part wake the second now ----------------------------------------------------
def _scope(root, qdir):
    return dataclasses.replace(WriteScope.from_install_root(root), questions=QuestionStore(qdir))


def test_the_legacy_write_that_marks_it_done_wakes_the_second_part_now(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="seed")
    f = _follower(root, a["id"])
    res = apply_edit(_scope(root, qdir), {"kind": "spore_descend", "spore_id": a["id"], "spore_kind": "done", "confirm": True})
    assert res["woke"] == [f["id"]] and len(_rows(qdir)) == 1
    assert _spore(root, f["id"])["germinated"]["ref"].startswith("q-")


def test_the_legacy_write_that_composts_it_wakes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="seed")
    _follower(root, a["id"])
    res = apply_edit(_scope(root, qdir), {"kind": "spore_descend", "spore_id": a["id"], "spore_kind": "composted", "confirm": True})
    assert "woke" not in res and not (qdir / "questions.db").exists()


def test_a_wake_that_fails_never_fails_the_resolve(tmp_path, qdir, pings, monkeypatch):
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="seed")
    f = _follower(root, a["id"])
    monkeypatch.setattr(g, "germinate", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("band store down")))
    with pytest.warns(RuntimeWarning, match="band store down"):
        res = apply_edit(_scope(root, qdir), {"kind": "spore_descend", "spore_id": a["id"], "spore_kind": "done", "confirm": True})
    assert res["ok"] and "band store down" in res["wake_error"] and _spore(root, a["id"])["status"] == "resolved"
    monkeypatch.undo()
    assert [o["spore_id"] for o in _run(root, qdir)["due"]] == [f["id"]]        # the next ordinary run converges


def test_the_band_card_done_wakes_the_second_part_now(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="agenda", target="band", next="2026-11-05")
    f = _follower(root, a["id"])
    _run(root, qdir)                                                  # the first part rings in the band
    said = QuestionStore(qdir).intention_act(a["id"], "done", channel="cockpit")
    assert f"woke {f['id']}" in said and _spore(root, f["id"])["germinated"]["ref"].startswith("q-")


def test_clearing_the_band_card_wakes_nothing(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="agenda", target="band", next="2026-11-05")
    f = _follower(root, a["id"])
    _run(root, qdir)
    assert "woke" not in QuestionStore(qdir).intention_act(a["id"], "clear", channel="cockpit")
    assert _spore(root, f["id"]).get("germinated") is None


# --- creating the second part ("then you") -------------------------------------------------------------------------
def test_then_you_files_a_band_agenda_linked_to_this_item_with_a_week_to_check(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    made = split.create_then(_spores(root), a["id"], "  send the invoice  ", today=TODAY)
    row = _spore(root, made["spore_id"])
    assert (row["disposition"], row["target"], row["type"], row["text"]) == ("agenda", "band", "task", "send the invoice")
    assert row["on_event"] == f"done:{a['id']}" and row["check_by"] == "2026-11-18" and made["replay"] is False
    assert made["note"] == f"filed {made['spore_id']}: it comes to your band when this is marked done; if it is not, you are asked on 2026-11-18"


def test_then_you_twice_lands_one_row_and_says_it_replayed(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    one = split.create_then(_spores(root), a["id"], "send the invoice", today=TODAY)
    two = split.create_then(_spores(root), a["id"], "send the invoice", today=TODAY)
    assert two["spore_id"] == one["spore_id"] and two["replay"] is True
    assert len([s for s in SporeStore(_spores(root)).list_open() if s.get("on_event")]) == 1
    assert split.create_then(_spores(root), a["id"], "send a reminder", today=TODAY)["spore_id"] != one["spore_id"]


@pytest.mark.parametrize("words,check_by,why", [("", None, "needs words"), ("go", "soon", "YYYY-MM-DD"), ("go", "2026-11-10", "already past")])
def test_then_you_refuses_what_it_cannot_make(tmp_path, words, check_by, why):
    root = _install(tmp_path)
    a = _anchor(root)
    with pytest.raises(split.ThenRefused, match=why):
        split.create_then(_spores(root), a["id"], words, check_by=check_by, today=TODAY)


def test_then_you_refuses_a_resolved_or_a_loop_first_part(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    loop = guarded_spore_store(_spores(root)).add(type="task", text="a loop")
    _done(root, a["id"])
    with pytest.raises(split.ThenRefused) as gone:
        split.create_then(_spores(root), a["id"], "next", today=TODAY)
    assert gone.value.status == 409
    with pytest.raises(split.ThenRefused, match="not a loop"):
        split.create_then(_spores(root), loop["id"], "next", today=TODAY)


@pytest.mark.parametrize("target", ["desk", "board:levain"])
def test_then_you_on_a_machine_part_is_refused_in_words(tmp_path, target):
    """A desk or board first part is composted when it germinates, so done: could never wake the link: refused, nothing filed."""
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="draft it", disposition="agenda", target=target)
    with pytest.raises(split.ThenRefused, match="handed to the desk or a board"):
        split.create_then(_spores(root), a["id"], "send it", today=TODAY)
    assert [s for s in SporeStore(_spores(root)).list_open() if s.get("on_event")] == []


def test_then_you_replayed_after_the_second_part_ended_is_refused_naming_it(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    one = split.create_then(_spores(root), a["id"], "send the invoice", today=TODAY)
    _done(root, one["spore_id"], "dropped")
    with pytest.raises(split.ThenRefused, match=one["spore_id"]) as why:
        split.create_then(_spores(root), a["id"], "send the invoice", today=TODAY)
    assert why.value.status == 409
    assert split.create_then(_spores(root), a["id"], "send a different invoice", today=TODAY)["replay"] is False


def test_then_you_keys_the_link_with_the_stored_id_and_followers_match_either_spelling(tmp_path):
    root = _install(tmp_path)
    a = _anchor(root)
    other = ("spore-" if a["id"].startswith("intention-") else "intention-") + a["id"].partition("-")[2]
    made = split.create_then(_spores(root), other, "send the invoice", today=TODAY)
    assert made["event"] == f"done:{a['id']}"
    items = SporeStore(_spores(root)).list_open()
    assert [f["id"] for f in split.followers_of(items, a["id"])] == [f["id"] for f in split.followers_of(items, other)] == [made["spore_id"]]


def test_the_verb_binds_the_row_version_and_audits(tmp_path, qdir):
    root = _install(tmp_path)
    a = _anchor(root)
    scope = _scope(root, qdir)
    from levain.cockpit.providers import spore_row_version

    ver = spore_row_version(_spore(root, a["id"]))
    res = apply_edit(scope, {"kind": "spore_then", "spore_id": a["id"], "text": "send the invoice",
                             "expect_disposition": "agenda"}, expected_version=ver)
    assert res["ok"] and res["action"] == "then" and res["event"] == f"done:{a['id']}"
    guarded_spore_store(_spores(root)).update(a["id"], text="call Jim, then Sam")
    with pytest.raises(EditError) as stale:
        apply_edit(scope, {"kind": "spore_then", "spore_id": a["id"], "text": "another", "expect_disposition": "agenda"},
                   expected_version=ver)
    assert stale.value.http_status == 409
    assert len([s for s in SporeStore(_spores(root)).list_open() if s.get("on_event")]) == 1


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
    lf = rows[f"spore:{later['id']}"]["facets"]
    assert lf["time_state"] == "later" and lf["on_event"] == f"done:{a}" and lf["after"] == a
    assert lf["after_text"] == "an agenda from last week"
    assert lf["wake_line"].startswith(f"on done:{a}, check by ") and lf["wake_line"].endswith("→ band")
    assert lf["check_by"] == later["check_by"]
    df = rows[f"spore:{due['id']}"]["facets"]
    assert df["time_state"] == "now" and df["wake_line"] == f"still waiting on done:{a}"
    assert sorted(rows[f"spore:{a}"]["facets"]["then"]) == sorted([later["id"], due["id"]])
    assert "then" not in rows[f"spore:{r.ids['plain']}"]["facets"]


def test_then_you_is_offered_on_an_agenda_and_not_on_a_seed_or_a_loop(tmp_path):
    from tests.test_cockpit_list_panel import Rig

    r = Rig(tmp_path, band=False, board=False)
    rows = _list_rows(r.panel())
    offered = {rid: any(x["verb"] == "spore_then" for x in row["actions"]) for rid, row in rows.items()}
    assert offered[f"spore:{r.ids['agenda_old']}"] is True and offered[f"spore:{r.ids['plain']}"] is False


def test_a_desk_first_part_is_composted_at_germination_and_never_wakes_the_second_on_done(tmp_path, qdir, pings):
    root = _install(tmp_path)
    CoordStore(qdir).coord_set_agent("claude", model="haiku", cwd=str(tmp_path.resolve()))
    a = guarded_spore_store(_spores(root)).add(type="task", text="draft it", disposition="agenda", target="desk", next="2026-11-05")
    f = _follower(root, a["id"], "send it")
    res = _run(root, qdir)
    assert [o["spore_id"] for o in res["due"]] == [a["id"]] and _spore(root, a["id"])["resolution"]["kind"] == "composted"
    assert _run(root, qdir)["due"] == [] and _spore(root, f["id"]).get("germinated") is None


# --- review fixes (L1 + L2, row 178 build 2) --------------------------------------------------------------------------
def test_a_snoozed_woken_part_is_not_woken_again_and_comes_back_as_a_dated_agenda(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    _done(root, a["id"])
    _run(root, qdir)
    QuestionStore(qdir).intention_act(f["id"], "snooze", until="2026-11-14", channel="cockpit")
    again = _run(root, qdir)
    assert again["due"] == [] and QuestionStore(qdir).open_questions() == []        # the snooze holds
    part = _spore(root, f["id"])
    assert part["next"] == "2026-11-14" and not part.get("on_event")
    back = _run(root, qdir, today=date(2026, 11, 14))
    assert [o["spore_id"] for o in back["due"]] == [f["id"]] and "event" not in back["due"][0]   # an ordinary dated wake now


def test_a_typed_drop_to_the_check_by_ring_drops_it_and_any_other_words_keep_it(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    drop = _follower(root, a["id"], "drop me", check_by="2026-11-10")
    keep = _follower(root, a["id"], "keep me", check_by="2026-11-10")
    _run(root, qdir)
    qs = QuestionStore(qdir)
    for sid, words in ((drop["id"], " Drop "), (keep["id"], "no, I did it already")):
        qs.answer(next(q.id for q in qs.open_questions() if sid in q.what), text=words, channel="cli")
    res = _run(root, qdir)
    assert {c["spore_id"]: c["via"] for c in res["checkby"]} == {drop["id"]: "dropped", keep["id"]: "kept"}
    assert _spore(root, drop["id"])["status"] == "resolved" and _spore(root, keep["id"])["status"] == "open"


def test_the_list_keeps_an_event_agenda_whose_key_is_not_done(tmp_path):
    from tests.test_cockpit_list_panel import Rig, TODAY as LIST_TODAY

    r = Rig(tmp_path, band=False, board=False)
    kinds = {}
    for key in ("landed:np-123", "answered:q-abc"):
        kinds[key] = r.sp.add(type="task", text=f"after {key}", disposition="agenda", target="band", on_event=key,
                              check_by=(LIST_TODAY.replace(day=LIST_TODAY.day + 5)).isoformat(), today=LIST_TODAY)["id"]
    panel = r.panel()
    rows = _list_rows(panel)
    for key, sid in kinds.items():
        facets = rows[f"spore:{sid}"]["facets"]
        assert facets["after"] is None and facets["time_state"] == "later" and facets["wake_line"].startswith(f"on {key}, check by ")
    assert not [w for w in panel.get("skipped", []) if "wrong type" in str(w)]


def test_when_on_an_event_agenda_is_refused_in_words_and_clearing_is_not(tmp_path, qdir):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    scope = _scope(root, qdir)
    with pytest.raises(EditError, match=f"waits on done:{a['id']}, not on a date") as why:
        apply_edit(scope, {"kind": "spore_surface_at", "spore_id": f["id"], "surface_at": "2026-11-20", "expect_disposition": "agenda"})
    assert why.value.http_status == 422 and _spore(root, f["id"]).get("next") is None
    assert apply_edit(scope, {"kind": "spore_surface_at", "spore_id": f["id"], "surface_at": None, "expect_disposition": "agenda"})["ok"]
    plain = _anchor(root, "a dated one")                       # an ordinary agenda still takes a date
    assert apply_edit(scope, {"kind": "spore_surface_at", "spore_id": plain["id"], "surface_at": "2026-11-20", "expect_disposition": "agenda"})["ok"]


def test_then_you_is_not_offered_on_a_desk_or_board_row_and_the_edit_kind_is_mediated(tmp_path):
    from levain import writes
    from tests.test_cockpit_list_panel import Rig

    r = Rig(tmp_path, band=False, board=False)
    rows = _list_rows(r.panel())
    assert not any(x["verb"] == "spore_then" for x in rows[f"spore:{r.ids['drop_late']}"]["actions"])
    assert any(x["verb"] == "spore_then" for x in rows[f"spore:{r.ids['agenda_old']}"]["actions"])
    assert "spore_then" in writes._VERB_KINDS


def test_an_event_run_matches_the_key_in_either_id_spelling(tmp_path, qdir, pings):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    _done(root, a["id"])
    other = ("spore-" if a["id"].startswith("intention-") else "intention-") + a["id"].partition("-")[2]
    assert [o["spore_id"] for o in _run(root, qdir, event=f"done:{other}")["due"]] == [f["id"]]


# --- L3 round 1 fixes (row 178 build 2, fix lane 2) --------------------------------------------------------------------
def _other_spelling(sid: str) -> str:
    return ("spore-" if sid.startswith("intention-") else "intention-") + sid.partition("-")[2]


@pytest.mark.parametrize("verb,params", [
    ("spore_surface_at", {"surface_at": "2026-11-20"}),
    ("spore_set_disposition", {"disposition": "agenda", "surface_at": "2026-11-20"}),
    ("spore_retarget", {"target": "band", "surface_at": "2026-11-20"}),
    ("spore_update", {"next": "2026-11-20"}),
])
def test_no_write_that_sets_a_date_puts_one_on_an_event_agenda_the_broker_effects_too(tmp_path, verb, params):
    """F-A: one rule, every write that sets ``next``. The planned effects (the broker path every T2 verb takes) held no guard."""
    from levain.cockpit.effects import canonical_effect

    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    with pytest.raises(ValueError, match=f"waits on done:{a['id']}, not on a date"):
        canonical_effect(verb, params, _spore(root, f["id"]))
    plain = _spore(root, _anchor(root, "an ordinary agenda", next="2026-11-15")["id"])
    canonical_effect(verb, params, plain)                                     # an ordinary agenda still takes the date


def test_the_legacy_set_disposition_refuses_a_date_on_an_event_agenda_and_a_clear_is_allowed(tmp_path, qdir):
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    scope = _scope(root, qdir)
    req = {"kind": "spore_set_disposition", "spore_id": f["id"], "disposition": "agenda", "expect_disposition": "agenda"}
    with pytest.raises(EditError, match=f"waits on done:{a['id']}, not on a date") as why:
        apply_edit(scope, {**req, "surface_at": "2026-11-20"})
    assert why.value.http_status == 422 and _spore(root, f["id"]).get("next") is None
    assert apply_edit(scope, {**req, "surface_at": None})["ok"]
    assert apply_edit(scope, req)["ok"]


def test_a_read_error_is_never_read_as_no_event_agenda(tmp_path, qdir, monkeypatch):
    """F-A (codex 5): the guard's read failing must refuse the write, not let a date land on what may be an event agenda."""
    from levain import writes

    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"])
    real = guarded_spore_store

    class Flaky:
        def __init__(self, store):
            self._s = store

        def get(self, *_a, **_k):
            raise OSError("disk went away")

        def __getattr__(self, name):
            return getattr(self._s, name)

    monkeypatch.setattr(writes, "guarded_spore_store", lambda p: Flaky(real(p)))
    with pytest.raises(EditError) as why:
        apply_edit(_scope(root, qdir), {"kind": "spore_surface_at", "spore_id": f["id"], "surface_at": "2026-11-20",
                                        "expect_disposition": "agenda"})
    assert why.value.http_status == 503
    monkeypatch.undo()
    assert _spore(root, f["id"]).get("next") is None


def test_then_you_fires_through_the_real_dispatch_with_the_browser_token(tmp_path):
    """F-B: ``device_ok`` governs a phone's signed credential only; the browser's token fires a C1 verb without it. Drive the
    real dispatch from the List row the page button is drawn on."""
    from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, Rig

    r = Rig(tmp_path, band=False, board=False)
    row = r.rows()[f"spore:{r.ids['agenda_old']}"]
    assert any(x["verb"] == "spore_then" for x in row["actions"])
    out = dispatch(registry=r.reg, cockpit=r.ck, scope=r.src.write_scope, route="cockpit", credential=CRED,
                   install_class=INSTALL_CLASS,
                   req={"verb": "spore_then", "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
                        "params": {"text": "send the invoice"}})
    assert out["ok"] and out["event"] == f"done:{r.ids['agenda_old']}" and out["note"].startswith("filed ")
    made = r.sp.get(out["spore_id"])
    assert made["on_event"] == out["event"] and made["target"] == "band"


def test_a_row_with_no_target_is_not_handed_off_and_can_be_followed(tmp_path):
    """F-C: ``executor_of(None)`` is None (not ``machine``), so a seed or agenda with no target is offered then-you and accepted."""
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="a bare agenda", disposition="agenda")
    assert "target" not in a or a["target"] is None
    assert split.create_then(_spores(root), a["id"], "after it", today=TODAY)["replay"] is False


@pytest.mark.parametrize("target", ["Desk", " desk ", "BOARD:levain", "Board: levain "])
def test_the_target_is_read_one_way_by_germination_and_by_then_you(tmp_path, qdir, pings, target):
    """F-D: a stored target spelled with a space or another case is the desk's to germination (it composts the part on handoff, so
    nothing could mark it done); then-you must read it the same way and refuse."""
    from levain.cockpit.verbs import _machine_part

    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="draft it", disposition="agenda", next="2026-11-05")
    path = _spores(root)
    import json
    data = json.loads(path.read_text())
    next(s for s in data["spores"] if s["id"] == a["id"])["target"] = target          # a hand-edited store
    path.write_text(json.dumps(data))
    assert _machine_part({"id": f"spore:{a['id']}", "facets": {"target": target}})
    with pytest.raises(split.ThenRefused, match="handed to the desk or a board"):
        split.create_then(path, a["id"], "send it", today=TODAY)
    assert g._target_of(_spore(root, a["id"]))[0] in ("desk", "board:levain")


def test_an_event_wake_finds_its_first_part_whichever_spelling_the_link_uses(tmp_path, qdir, pings):
    """F-E (refuted): anneal's id counter is shared across both prefixes and ``store.get`` resolves either spelling, so a link
    spelled the other way still finds its first part, in germinate and in doctor."""
    from levain import doctor

    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, _other_spelling(a["id"]))
    _done(root, a["id"])
    rep = doctor._check_overdue_agenda(root, today=TODAY)
    assert rep and f["id"] in rep[0].detail
    assert [o["spore_id"] for o in _run(root, qdir, event=f"done:{a['id']}")["due"]] == [f["id"]]


def test_both_spellings_of_one_number_cannot_be_minted(tmp_path):
    """F-E / F-G: the id counter counts both prefixes, so ``spore-N`` and ``intention-N`` never coexist from a store write."""
    from anneal_memory import SporeStore as Legacy      # mints ``spore-`` ids

    root = _install(tmp_path)
    old = Legacy(_spores(root)).add(type="task", text="old", disposition="agenda")
    new = guarded_spore_store(_spores(root)).add(type="task", text="new", disposition="agenda")
    assert old["id"].startswith("spore-") and new["id"].startswith("intention-")
    assert old["id"].partition("-")[2] != new["id"].partition("-")[2]


def test_then_you_takes_its_clock_from_the_writes_now(tmp_path, qdir):
    """F-F: the default check-by day and the already-past refusal both read the write's ``now``, never the host's clock."""
    from datetime import datetime, timedelta

    root = _install(tmp_path)
    a = _anchor(root)
    scope = _scope(root, qdir)
    now = "2030-01-05T17:00:00+00:00"
    today = datetime.fromisoformat(now).astimezone().date()
    res = apply_edit(scope, {"kind": "spore_then", "spore_id": a["id"], "text": "send it", "expect_disposition": "agenda"}, now=now)
    assert res["check_by"] == (today + timedelta(days=7)).isoformat()
    with pytest.raises(EditError, match="already past"):
        apply_edit(scope, {"kind": "spore_then", "spore_id": a["id"], "text": "and then", "check_by": "2029-12-01",
                           "expect_disposition": "agenda"}, now=now)


def test_a_wake_that_fails_part_way_still_reports_the_parts_it_woke(tmp_path, qdir, pings, monkeypatch):
    """F-H: one second part wakes and the next fails: the resolve reports who woke next to the error."""
    root = _install(tmp_path)
    a = guarded_spore_store(_spores(root)).add(type="task", text="call Jim", disposition="seed")
    f1, f2 = _follower(root, a["id"], "one"), _follower(root, a["id"], "two")
    calls = []
    real = QuestionStore.file_once

    def flaky(self, *args, **kw):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("band store hiccup")
        return real(self, *args, **kw)

    monkeypatch.setattr(QuestionStore, "file_once", flaky)
    with pytest.warns(RuntimeWarning):
        res = apply_edit(_scope(root, qdir), {"kind": "spore_descend", "spore_id": a["id"], "spore_kind": "done", "confirm": True})
    stamped = [s for s in (f1, f2) if _spore(root, s["id"]).get("germinated")]
    assert len(stamped) == 1 and res["woke"] == [stamped[0]["id"]] and "band store hiccup" in res["wake_error"]
    monkeypatch.undo()
    assert [o["spore_id"] for o in _run(root, qdir)["due"]] == [s["id"] for s in (f1, f2) if s is not stamped[0]]


def test_a_check_by_ring_that_cannot_be_closed_leaves_the_link_so_the_wake_retries(tmp_path, qdir, pings, monkeypatch):
    """F-I: the wake's one commit is the stamp, and the ring closes BEFORE it, so a close that fails (or a crash after it) leaves
    the part still waiting and the next run wakes it, once."""
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"], check_by="2026-11-11")
    _run(root, qdir)
    ring = _rows(qdir)[0]
    _done(root, a["id"])

    def boom(self, *a, **k):
        raise RuntimeError("questions store locked")

    monkeypatch.setattr(QuestionStore, "close_open", boom)
    res = _run(root, qdir)
    assert res["due"] == [] and "questions store locked" in res["errors"][0]["error"]
    part = _spore(root, f["id"])
    assert part.get("germinated") is None and part["on_event"] == f"done:{a['id']}"      # nothing committed
    assert [q.id for q in QuestionStore(qdir).open_questions()] == [ring.id]
    monkeypatch.undo()
    res = _run(root, qdir)
    assert [o["spore_id"] for o in res["due"]] == [f["id"]] and not res["errors"]
    assert QuestionStore(qdir).get(ring.id).answer is not None and len(_rows(qdir)) == 2
    assert _run(root, qdir)["due"] == []


def test_an_answered_check_by_ring_stays_closed_after_its_answer_is_applied(tmp_path, qdir, pings):
    """F-J (refuted): a keep or a drop answers the ring, which is what closes it; applying it leaves nothing open."""
    root = _install(tmp_path)
    a = _anchor(root)
    keep, drop = _follower(root, a["id"], "keep me", check_by="2026-11-10"), _follower(root, a["id"], "drop me", check_by="2026-11-10")
    _run(root, qdir)
    qs = QuestionStore(qdir)
    for sid, option in ((keep["id"], "keep"), (drop["id"], "drop")):
        qs.answer(next(q.id for q in qs.open_questions() if sid in q.what), option=option, channel="cli")
    _run(root, qdir)
    assert QuestionStore(qdir).open_questions() == []


def test_the_event_wake_closes_a_day_suffixed_check_by_ring_by_prefix(tmp_path, qdir, pings):
    """F-J (refuted): the ring's key is ``checkby:<okey>:<day>`` and ``close_open("checkby:<okey>")`` closes it (key or key:*)."""
    root = _install(tmp_path)
    a = _anchor(root)
    f = _follower(root, a["id"], check_by="2026-11-11")
    _run(root, qdir)
    q = QuestionStore(qdir).open_questions()[0]
    assert q.agent_session_id == f"{g.KEY_PREFIX}checkby:{f['origin_key']}:2026-11-11"
    closed = QuestionStore(qdir).close_open(f"{g.KEY_PREFIX}checkby:{f['origin_key']}", text="done happened", channel="cli")
    assert [c.id for c in closed] == [q.id]
