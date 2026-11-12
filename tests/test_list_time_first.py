"""THE LIST GOES TIME-FIRST (plan 1109 "the List goes time-first", item 1).

The List's primary partition is time: NOW (due, overdue, live) / LATER (any row whose wake is in the future, from any source) /
SOMEDAY (undated seeds) / DONE (resolved or composted in the last 7 days). The type (band, intention, talk, drop, board) is a tag on
every row and a filter across the time-states. Pinned here, on the kernel's own panel: a future-dated row of ANY source lands
in Later and says where it lands when it wakes; Someday holds undated seeds only; Done shows the last seven days of resolutions
and offers no verb; every row kind carries its type tag."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import list_panel
from tests.test_cockpit_list_panel import NOW, TODAY, Rig, _band_with

@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def _rows(r: Rig) -> dict[str, dict[str, Any]]:
    return r.rows()


def _sp(r: Rig, key: str) -> str:
    return f"intention:{r.ids[key]}"


def test_a_future_dated_row_from_any_source_lands_in_later_and_says_where_it_wakes(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    add = rig.sp.add
    day3 = (TODAY + timedelta(days=3)).isoformat()
    board_drop = add(type="task", text="draft the BOBS note", disposition="agenda", target="board:levain",
                     next="2026-11-16T09:00", today=TODAY)["id"]
    talk = add(type="task", text="talk about the dryer", disposition="seed", attention="talk", next="2026-11-10", today=TODAY)["id"]
    seed = add(type="task", text="a dated seed", disposition="seed", next=day3, today=TODAY)["id"]
    _band_with(monkeypatch, rig, snoozed_until=day3)                       # a snoozed BAND row: the one that never reached the held view
    rows = _rows(rig)
    band = rows[f"band:{rig.q.id}"]
    got = {
        "band": band, "desk": rows[_sp(rig, "drop_later")], "board": rows[f"intention:{board_drop}"],
        "talk": rows[f"intention:{talk}"], "seed": rows[f"intention:{seed}"],
    }
    for name, row in got.items():
        assert row["group"] == "later", (name, row["group"])
        assert row["facets"]["time_state"] == "later", (name, row["facets"])
    lines = {name: row["facets"]["wake_line"] for name, row in got.items()}
    assert lines["band"].endswith("→ band") and lines["band"].startswith("wakes "), lines
    assert lines["desk"] == "wakes Tue 11-10 → desk", lines             # drop_later: 2026-11-10, target desk
    assert lines["board"] == "wakes Mon 11-16 → board · levain", lines
    assert lines["talk"] == "wakes Tue 11-10 → talk", lines
    assert lines["seed"].endswith("→ list"), lines
    assert got["talk"]["facets"]["surface"] == "talk" and got["talk"]["facets"]["wake_bucket"] == "week"
    # the rows come in wake order, so the page groups them by day without sorting
    later = [r for r in rig.panel()["rows"] if r["group"] == "later"]
    days = [r["facets"].get("wake_day") or r["facets"]["held_until"] for r in later]
    assert days == sorted(days), days
    assert "held" not in rig.panel() and not any("held" in r["facets"] for r in rig.panel()["rows"])   # the wire word is gone


def test_later_buckets_are_tomorrow_this_week_and_later_dates(rig: Rig) -> None:
    add = rig.sp.add
    ids = {n: add(type="task", text=f"a seed for {n}", disposition="seed",
                  next=(TODAY + timedelta(days=d)).isoformat(), today=TODAY)["id"]
           for n, d in (("tomorrow", 1), ("week", 6), ("far", 30))}
    rows = _rows(rig)
    assert rows[f"intention:{ids['tomorrow']}"]["facets"]["wake_bucket"] == "tomorrow"
    assert rows[f"intention:{ids['week']}"]["facets"]["wake_bucket"] == "week"
    assert rows[f"intention:{ids['far']}"]["facets"]["wake_bucket"] == "later"


def test_someday_holds_undated_seeds_only(rig: Rig) -> None:
    rows = _rows(rig)
    someday = [r for r in rows.values() if r["group"] == "someday"]
    assert someday, "the rig holds an undated seed"
    for r in someday:
        f = r["facets"]
        assert f["time_state"] == "someday" and f["surface"] == "intention" and f["disposition"] == "seed", f
        assert not f.get("due"), f                                         # undated
    assert _sp(rig, "plain") in {r["id"] for r in someday}
    for key in ("talk_old", "drop_late", "drop_soon", "agenda_old", "drop_later"):   # a talk, a drop, a dated row is never Someday
        assert rows[_sp(rig, key)]["group"] != "someday", key
    assert rows["board:levain:a"]["group"] == "board"
    assert [g["id"] for g in rig.panel()["groups"]][-3:] == ["someday", "done", "kept"]
    # an undated seed is counted as "Also pending" was (the card draws it); later and done are not
    assert rig.panel()["count"] == sum(1 for r in rig.panel()["rows"] if r["group"] not in ("desk", "later", "done"))


def test_done_shows_the_last_seven_days_of_resolutions_and_offers_no_verb(rig: Rig) -> None:
    recent = rig.sp.add(type="task", text="water the plants", disposition="seed", today=TODAY)["id"]
    older = rig.sp.add(type="task", text="renew the lease", disposition="seed", today=TODAY)["id"]
    talk = rig.sp.add(type="task", text="talk about the move", disposition="seed", attention="talk", today=TODAY)["id"]
    rig.sp.descend(recent, kind="done", today=TODAY, now=NOW - timedelta(days=2))
    rig.sp.descend(older, kind="dropped", today=TODAY, now=NOW - timedelta(days=9))     # outside the week
    rig.sp.descend(talk, kind="done", today=TODAY, now=NOW - timedelta(hours=1))
    p = rig.panel()
    done = [r for r in p["rows"] if r["group"] == "done"]
    assert [r["id"] for r in done] == [f"done:{talk}", f"done:{recent}"], [r["id"] for r in done]     # newest first
    assert f"done:{older}" not in {r["id"] for r in p["rows"]}
    for r in done:
        assert r["facets"]["time_state"] == "done" and r["facets"]["done_kind"] == "done", r["facets"]
        assert r["actions"] == [], r["actions"]                              # read-only: no verb on a closed row
    assert {r["id"]: r["facets"]["surface"] for r in done} == {f"done:{talk}": "talk", f"done:{recent}": "intention"}
    # a resolved intention is not an open row too
    assert f"intention:{recent}" not in {r["id"] for r in p["rows"]}


def test_every_row_kind_carries_its_type_tag(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    done = rig.sp.add(type="task", text="an old thing", disposition="seed", today=TODAY)["id"]
    rig.sp.descend(done, kind="done", today=TODAY, now=NOW - timedelta(days=1))
    _band_with(monkeypatch, rig, snoozed_until=(TODAY + timedelta(days=2)).isoformat())
    p = rig.panel()
    kinds = {r["facets"].get("surface") for r in p["rows"]}
    # band, board, intention, talk, drop: all present, none untagged; kept rows come only with a search query
    assert kinds == set(list_panel.SURFACES) - {list_panel.KEPT_SURFACE}, kinds
    assert {r["facets"]["time_state"] for r in p["rows"]} == {"now", "later", "someday", "done"}
    for r in p["rows"]:
        assert r["facets"].get("surface") in list_panel.SURFACES, r


def test_a_surface_date_moved_to_the_future_moves_the_row_to_later_on_the_next_read(rig: Rig) -> None:
    sid = rig.ids["drop_soon"]
    assert rig.rows()[f"intention:{sid}"]["group"] == "today"
    rig.sp.update(sid, next=(TODAY + timedelta(days=4)).isoformat())
    row = rig.rows()[f"intention:{sid}"]
    assert row["group"] == "later" and row["facets"]["time_state"] == "later", row


# --- the first fix round ---------------------------------------------------------------------------------------------


def _day(n: int) -> str:
    return (TODAY + timedelta(days=n)).isoformat()


def test_an_agenda_with_no_valid_target_wakes_into_the_band_never_the_list(rig: Rig) -> None:
    add = rig.sp.add
    bare = add(type="task", text="an agenda with no target", disposition="agenda", next=_day(3), today=TODAY)["id"]
    odd = add(type="task", text="an agenda with an unknown target", disposition="agenda", target="elsewhere",
              next=_day(3), today=TODAY)["id"]
    seed = add(type="task", text="a dated seed", disposition="seed", next=_day(3), today=TODAY)["id"]
    rows = _rows(rig)
    assert rows[f"intention:{bare}"]["facets"]["wake_line"].endswith("→ band"), rows[f"intention:{bare}"]["facets"]
    assert rows[f"intention:{odd}"]["facets"]["wake_line"].endswith("→ band"), rows[f"intention:{odd}"]["facets"]
    assert rows[f"intention:{seed}"]["facets"]["wake_line"].endswith("→ list")


def test_an_undated_agenda_is_someday_and_an_undated_talk_stays_now(rig: Rig) -> None:
    agenda = rig.sp.add(type="task", text="an agenda with no date", disposition="agenda", target="desk", today=TODAY)["id"]
    rows = _rows(rig)
    assert rows[f"intention:{agenda}"]["facets"]["time_state"] == "someday" and rows[f"intention:{agenda}"]["group"] == "someday"
    talk = rows[_sp(rig, "talk_new")]
    assert talk["facets"]["time_state"] == "now" and talk["group"] == "talk"


def test_a_band_rows_group_comes_from_its_time_state_and_held_until_only_when_future(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    _band_with(monkeypatch, rig, snoozed_until=_day(-2))
    past = _rows(rig)[f"band:{rig.q.id}"]
    assert past["group"] == "band" and past["facets"]["time_state"] == "now" and "held_until" not in past["facets"], past["facets"]
    _band_with(monkeypatch, rig, snoozed_until=_day(0))
    assert _rows(rig)[f"band:{rig.q.id}"]["group"] == "band"
    _band_with(monkeypatch, rig, snoozed_until=_day(2))
    future = _rows(rig)[f"band:{rig.q.id}"]
    assert future["group"] == "later" and future["facets"]["held_until"] == _day(2) and future["facets"]["time_state"] == "later"


def _resolved(rig: Rig) -> Path:
    return rig.root / ".levain" / "memory.spores.json"


def _edit_store(rig: Rig, edit: Any) -> None:
    import json
    p = _resolved(rig)
    data = json.loads(p.read_text("utf-8"))
    edit(data)
    p.write_text(json.dumps(data), "utf-8")


def _done_ids(rig: Rig) -> list[str]:
    return [r["id"] for r in rig.panel()["rows"] if r["group"] == "done"]


def test_done_is_a_window_with_an_edge_and_a_cap_that_says_what_it_hides(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    add, descend = rig.sp.add, rig.sp.descend
    inside = add(type="task", text="inside the window", disposition="seed", today=TODAY)["id"]
    outside = add(type="task", text="outside the window", disposition="seed", today=TODAY)["id"]
    descend(inside, kind="done", today=TODAY, now=NOW - timedelta(days=list_panel.DONE_DAYS) + timedelta(minutes=1))
    descend(outside, kind="done", today=TODAY, now=NOW - timedelta(days=list_panel.DONE_DAYS) - timedelta(minutes=1))
    assert _done_ids(rig) == [f"done:{inside}"]
    from levain.cockpit import providers
    monkeypatch.setattr(providers, "INTENTION_CAP", 1)
    more = add(type="task", text="a newer one", disposition="seed", today=TODAY)["id"]
    descend(more, kind="done", today=TODAY, now=NOW - timedelta(hours=1))
    p = rig.panel()
    assert _done_ids(rig) == [f"done:{more}"]
    assert any("1" in s["reason"] and "Done" in s["reason"] for s in p["skipped"]), p["skipped"]


def test_a_resolution_that_germinated_into_the_band_is_not_done(rig: Rig) -> None:
    done = rig.sp.add(type="task", text="really finished", disposition="seed", today=TODAY)["id"]
    sprouted = rig.sp.add(type="task", text="woke into the band", disposition="agenda", target="desk", next=_day(-1), today=TODAY)["id"]
    rig.sp.descend(done, kind="done", today=TODAY, now=NOW - timedelta(hours=2))
    rig.sp.descend(sprouted, kind="composted", today=TODAY, now=NOW - timedelta(hours=1))

    def stamp(data: dict[str, Any]) -> None:
        for r in data["resolved"]:
            if r["id"] == sprouted:
                r["resolution"]["ref"] = "germinated into q-0001"
    _edit_store(rig, stamp)
    assert _done_ids(rig) == [f"done:{done}"]


def test_a_malformed_resolution_is_skipped_and_the_rest_still_read(rig: Rig) -> None:
    ok = rig.sp.add(type="task", text="fine", disposition="seed", today=TODAY)["id"]
    rig.sp.descend(ok, kind="done", today=TODAY, now=NOW - timedelta(hours=1))

    def junk(data: dict[str, Any]) -> None:
        data["resolved"] += [
            {"id": "x1", "text": "bad at", "resolution": {"kind": "done", "at": "not a time"}},
            {"id": "x2", "text": "no at", "resolution": {"kind": "done"}},
            {"id": "x3", "text": "no resolution"},
            "not a dict",
        ]
    _edit_store(rig, junk)
    from levain.cockpit.engine import ReadContext
    part = list_panel._done_part(rig.src, ReadContext(NOW))      # the store's own view may refuse such a document; the reader must not
    assert [r.id for r in part.rows] == [f"done:{ok}"] and part.skipped == [], (part.rows, part.skipped)


def test_a_missing_or_non_list_resolved_is_no_done_rows_and_not_a_fault(rig: Rig) -> None:
    _edit_store(rig, lambda d: d.pop("resolved", None))
    assert _done_ids(rig) == [] and rig.panel()["status"] in ("ok", "partial")
    _edit_store(rig, lambda d: d.update(resolved={"a": 1}))
    assert _done_ids(rig) == []


def test_an_empty_store_has_no_done_rows(tmp_path: Path) -> None:
    empty = Rig(tmp_path, intentions=False, band=False, board=False)
    p = empty.panel()
    assert [r for r in p["rows"] if r["group"] == "done"] == []


def test_an_unreadable_store_names_the_done_rows_it_could_not_read(rig: Rig) -> None:
    from levain.cockpit.engine import ReadContext
    _resolved(rig).write_text("{ not json", "utf-8")
    part = list_panel._done_part(rig.src, ReadContext(NOW))
    assert part.rows == [] and part.skipped and "resolved rows could not be read" in part.skipped[0][1], part.skipped


def test_a_seed_with_a_malformed_next_is_read_as_undated(rig: Rig) -> None:
    sid = rig.sp.add(type="task", text="a seed with a bad date", disposition="seed", today=TODAY)["id"]
    _edit_store(rig, lambda d: [s.update(next="soonish") for s in d["intentions"] if s["id"] == sid])
    row = _rows(rig)[f"intention:{sid}"]
    assert row["facets"]["time_state"] == "someday" and not row["facets"].get("due"), row["facets"]


@pytest.mark.parametrize("days,bucket", [(1, "tomorrow"), (2, "week"), (7, "week"), (8, "later")])
def test_wake_bucket_edges(days: int, bucket: str) -> None:
    assert list_panel._wake(TODAY + timedelta(days=days), TODAY)[1] == bucket


def test_the_done_group_title_is_derived_from_the_window() -> None:
    from levain.cockpit import registry
    titles = dict(registry.ORDERINGS["intention.list"].groups)
    assert titles.get("done") == f"Done, last {list_panel.DONE_DAYS} days", titles
