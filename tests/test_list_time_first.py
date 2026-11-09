"""THE LIST GOES TIME-FIRST (plan 1109 "the List goes time-first", item 1).

The List's primary partition is time: NOW (due, overdue, live) / LATER (any row whose wake is in the future, from any source) /
SOMEDAY (undated seeds) / DONE (resolved or composted in the last 7 days). The type (band, spore, talk, drop, board) is a tag on
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
    return f"spore:{r.ids[key]}"


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
        "band": band, "desk": rows[_sp(rig, "drop_later")], "board": rows[f"spore:{board_drop}"],
        "talk": rows[f"spore:{talk}"], "seed": rows[f"spore:{seed}"],
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
    assert rows[f"spore:{ids['tomorrow']}"]["facets"]["wake_bucket"] == "tomorrow"
    assert rows[f"spore:{ids['week']}"]["facets"]["wake_bucket"] == "week"
    assert rows[f"spore:{ids['far']}"]["facets"]["wake_bucket"] == "later"


def test_someday_holds_undated_seeds_only(rig: Rig) -> None:
    rows = _rows(rig)
    someday = [r for r in rows.values() if r["group"] == "someday"]
    assert someday, "the rig holds an undated seed"
    for r in someday:
        f = r["facets"]
        assert f["time_state"] == "someday" and f["surface"] == "spore" and f["disposition"] == "seed", f
        assert not f.get("due"), f                                         # undated
    assert _sp(rig, "plain") in {r["id"] for r in someday}
    for key in ("talk_old", "drop_late", "drop_soon", "agenda_old", "drop_later"):   # a talk, a drop, a dated row is never Someday
        assert rows[_sp(rig, key)]["group"] != "someday", key
    assert rows["board:levain:a"]["group"] == "board"
    assert [g["id"] for g in rig.panel()["groups"]][-2:] == ["someday", "done"]
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
    assert {r["id"]: r["facets"]["surface"] for r in done} == {f"done:{talk}": "talk", f"done:{recent}": "spore"}
    # a resolved spore is not an open row too
    assert f"spore:{recent}" not in {r["id"] for r in p["rows"]}


def test_every_row_kind_carries_its_type_tag(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    done = rig.sp.add(type="task", text="an old thing", disposition="seed", today=TODAY)["id"]
    rig.sp.descend(done, kind="done", today=TODAY, now=NOW - timedelta(days=1))
    _band_with(monkeypatch, rig, snoozed_until=(TODAY + timedelta(days=2)).isoformat())
    p = rig.panel()
    kinds = {r["facets"].get("surface") for r in p["rows"]}
    assert kinds == set(list_panel.SURFACES), kinds                          # band, board, spore, talk, drop: all present, none untagged
    assert {r["facets"]["time_state"] for r in p["rows"]} == {"now", "later", "someday", "done"}
    for r in p["rows"]:
        assert r["facets"].get("surface") in list_panel.SURFACES, r


def test_a_surface_date_moved_to_the_future_moves_the_row_to_later_on_the_next_read(rig: Rig) -> None:
    sid = rig.ids["drop_soon"]
    assert rig.rows()[f"spore:{sid}"]["group"] == "today"
    rig.sp.update(sid, next=(TODAY + timedelta(days=4)).isoformat())
    row = rig.rows()[f"spore:{sid}"]
    assert row["group"] == "later" and row["facets"]["time_state"] == "later", row
