"""The List's "who does it" (board row 178, build 1, intention rows): who acts after a wake is DERIVED from ``target`` (the operator for
band and nudge, the machine for desk and board), never stored, and editing it sets ``target`` through ``intention_retarget``. Also pinned:
a timed ``next`` round-trips through the snooze verb and its tier, and only a target flow's germinate also reads is written."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import IntentionStore

from levain.cockpit.effects import canonical_effect
from levain.cockpit.verbs import _date_tier, _date_escalates
from levain.intentions import executor_of, valid_retarget
from tests.test_cockpit_k2a import TOKEN, _get, _post, _serve
from tests.test_dashboard import _make_levain_install
from levain.dashboard import SubstrateSource

TODAY = date(2026, 11, 9)


def _install(tmp_path: Path) -> tuple[Path, SubstrateSource, dict[str, str]]:
    root = _make_levain_install(tmp_path)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    ids = {
        "seed": store.add(type="task", text="a plain seed", disposition="seed", today=TODAY)["id"],
        "drop": store.add(type="task", text="a desk drop", disposition="agenda", target="desk",
                          next="2031-01-07T09:00", today=TODAY)["id"],
        "loop": store.add(type="task", text="a loop", tier="hot", today=TODAY)["id"],
    }
    return root, SubstrateSource.local(root), ids


def _row(base: str, sid: str) -> dict[str, Any]:
    rows = _get(f"{base}/cockpit/panel/tray.json", TOKEN)["rows"]
    return next(r for r in rows if r["id"] == f"intention:{sid}")


def _retarget(base: str, sid: str, params: dict[str, Any]) -> tuple[int, dict]:
    r = _row(base, sid)
    return _post(f"{base}/cockpit/verb", {"verb": "intention_retarget", "panel_id": "tray", "row_id": r["id"],
                                          "row_version": r["version"], "params": params, "intent": "reported", "tier_seen": "T2",
                                          "gesture_seen": "direct-reported"}, TOKEN)


def test_who_is_derived_from_the_target_and_only_a_target_flow_reads_is_written() -> None:
    assert [executor_of(t) for t in ("band", "nudge", "desk", "board:levain", None, "elsewhere", "board:")] == \
        ["operator", "operator", "machine", "machine", None, None, None]
    assert all(valid_retarget(t) for t in ("band", "nudge", "desk", "board:levain", "board:a.b_c-1"))
    assert not any(valid_retarget(t) for t in ("board:", "board:a b", "board:../x", "board:-x", "you", None, 3))


def test_the_effect_refuses_what_it_may_not_write() -> None:
    pre = {"id": "intention-1", "origin_key": "k", "status": "open", "disposition": "seed", "text": "t", "type": "task"}
    ver = lambda row: "v"                                      # noqa: E731
    ok = canonical_effect("intention_retarget", {"target": "band", "surface_at": "2031-02-03T09:00"}, pre, version_of=ver)
    assert ok.changes == {"target": "band", "disposition": "agenda", "next": "2031-02-03T09:00"}
    for bad, why in (({"target": "board:a b", "surface_at": "2031-02-03"}, "target must be"),
                     ({"target": "band"}, "needs a date"),                                 # a seed has no date to keep
                     ({"target": "band", "surface_at": "2031-02-30"}, "range"),
                     ({"target": "band", "surface_at": "2031-02-03", "text": "x"}, "sets target"),
                     ({"surface_at": "2031-02-03"}, "sets target")):
        with pytest.raises(ValueError, match=why):
            canonical_effect("intention_retarget", bad, pre, version_of=ver)
    with pytest.raises(ValueError, match="neither"):
        canonical_effect("intention_retarget", {"target": "band"}, {**pre, "disposition": None, "next": "2031-02-03"}, version_of=ver)
    held = {**pre, "disposition": "agenda", "next": "2031-02-03T09:00", "attention": "talk"}
    with pytest.raises(ValueError, match="attention"):                                    # steering's own rule: no ping on a desk drop
        canonical_effect("intention_retarget", {"target": "desk"}, held, version_of=ver)
    keep = canonical_effect("intention_retarget", {"target": "band"}, {**held, "attention": None}, version_of=ver)
    assert "next" not in keep.changes                                                      # the row keeps its date
    for empty in ("", None):                                                               # a target never clears a date
        with pytest.raises(ValueError, match="cannot clear"):
            canonical_effect("intention_retarget", {"target": "band", "surface_at": empty}, {**held, "attention": None},
                             version_of=ver)


def test_a_talk_seed_given_a_who_leaves_the_talk_queue_whole(tmp_path: Path) -> None:
    """``attention: talk`` steers a seed into the talk queue, which reads seeds only: the seed that becomes an agenda drops it,
    so no agenda carries a talk mark nobody reads. Steering itself would let an agenda keep talk on a band target."""
    from levain.intentions import check_steering

    assert check_steering("agenda", "band", "talk") is None
    pre = {"id": "intention-1", "origin_key": "k", "status": "open", "disposition": "seed", "text": "t", "type": "task",
           "attention": "talk"}
    eff = canonical_effect("intention_retarget", {"target": "desk", "surface_at": "2031-02-03"}, pre, version_of=lambda row: "v")
    assert eff.changes == {"target": "desk", "disposition": "agenda", "next": "2031-02-03", "attention": None}
    root, src, ids = _install(tmp_path)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    store.update(ids["seed"], attention="talk", today=TODAY)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        st, body = _retarget(base, ids["seed"], {"target": "band", "surface_at": "2031-02-03"})
    assert st == 200, body
    got = store.get(ids["seed"])
    assert (got["disposition"], got["target"], got.get("attention")) == ("agenda", "band", None)


def test_a_seed_given_who_and_when_becomes_a_dated_agenda_and_the_agenda_changes_hands(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        assert "intention_retarget" in {a["verb"] for a in _row(base, ids["seed"])["actions"]}
        st, body = _retarget(base, ids["seed"], {"target": "band", "surface_at": "2031-02-03T09:30"})
        assert st == 200, body
        st, body = _retarget(base, ids["drop"], {"target": "board:levain"})
        assert st == 200, body
        st, body = _retarget(base, ids["drop"], {"target": "somebody"})
        assert st >= 400, body
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    seed, drop = store.get(ids["seed"]), store.get(ids["drop"])
    assert (seed["disposition"], seed["target"], seed["next"]) == ("agenda", "band", "2031-02-03T09:30")
    assert (drop["target"], drop["next"]) == ("board:levain", "2031-01-07T09:00")          # who changes, when does not


def test_a_seed_stored_with_other_casing_is_still_offered_who(tmp_path: Path) -> None:
    """The store keeps a disposition as it was written ("Seed"); the verb reads it as the effect does, case and spacing aside.
    Today no panel renders such a row (the disposition facet is an enum, so the row is refused), so this pins the predicate."""
    from levain.cockpit.verbs import KERNEL_VERBS

    retarget = next(v for v in KERNEL_VERBS if v.name == "intention_retarget")
    for d in ("Seed", " seed ", "AGENDA"):
        assert retarget.applies_to({"id": "intention:intention-9", "facets": {"disposition": d}}), d
    assert not retarget.applies_to({"id": "intention:intention-9", "facets": {"disposition": "loop"}})
    root, src, _ids = _install(tmp_path)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    sid = store.add(type="task", text="a capital seed", disposition="Seed", today=TODAY)["id"]
    assert store.get(sid)["disposition"] == "Seed"
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        shown = {r["id"] for p in ("tray", "loops") for r in _get(f"{base}/cockpit/panel/{p}.json", TOKEN)["rows"]}
    assert f"intention:{sid}" not in shown


def test_an_agenda_that_already_woke_is_not_retargeted(tmp_path: Path) -> None:
    """A ``germinated`` stamp means the agenda woke (a band row rings for it, or it was handed on): germinate never reads its
    target again, so a new one would change nothing but the record. The retarget refuses it, as the other intention verbs refuse
    a ringing agenda."""
    root, src, ids = _install(tmp_path)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    stamp = {"on": "2031-01-07", "target": "desk", "ref": "q-0001"}
    store.update(ids["drop"], germinated=stamp, today=TODAY)
    with pytest.raises(ValueError, match="already woke"):
        canonical_effect("intention_retarget", {"target": "band"}, store.get(ids["drop"]), version_of=lambda row: "v")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        st, body = _retarget(base, ids["drop"], {"target": "band"})
    assert st >= 400, body
    assert store.get(ids["drop"])["target"] == "desk"


def test_the_legacy_write_never_takes_a_target(tmp_path: Path) -> None:
    from levain.writes import EditError, apply_edit

    root, src, ids = _install(tmp_path)
    with pytest.raises(EditError, match="signed") as ei:
        apply_edit(src.write_scope, {"kind": "intention_retarget", "intention_id": ids["seed"], "target": "desk", "surface_at": "2031-02-03"})
    assert ei.value.code == "bad_verb_arg"
    assert "target" not in IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").get(ids["seed"])
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}) as (base, _h):       # no broker: a bearer token is refused at its tier
        st, body = _post(f"{base}/edit", {"kind": "intention_retarget", "intention_id": ids["seed"], "target": "desk"}, TOKEN)
    assert st == 403 and body["error"] == "needs_broker", (st, body)


def test_a_timed_snooze_round_trips_and_keeps_its_tier(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}) as (base, _h):
        r = _row(base, ids["seed"])
        st, body = _post(f"{base}/cockpit/verb", {"verb": "intention_surface_at", "panel_id": "tray", "row_id": r["id"],
                                                   "row_version": r["version"], "params": {"surface_at": "2031-02-03T21:05"}}, TOKEN)
        assert st == 200, body
        r = _row(base, ids["seed"])
        st, body = _post(f"{base}/cockpit/verb", {"verb": "intention_surface_at", "panel_id": "tray", "row_id": r["id"],
                                                   "row_version": r["version"], "params": {"surface_at": "2031-02-03T25:99"}}, TOKEN)
        assert st >= 400, body
    assert IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").get(ids["seed"])["next"] == "2031-02-03T21:05"
    # the List row carries the stored minute in `fires`
    held = {"facets": {"due": "2031-02-03", "fires": "2031-02-03T21:05"}}
    assert _date_tier("2031-02-03T09:00", held, "verified", TODAY) == "C1"
    assert _date_tier("2031-02-02T09:00", held, "unverified", TODAY) == "T2"               # an earlier day on an unverified held row is


def test_an_earlier_minute_on_the_held_day_is_sooner() -> None:
    """A row held until a minute wakes sooner when moved to an earlier minute of the same day: that is T2 on an unverified
    row, as an earlier day is. A date-only value is the start of its day; a row whose minute is unknown (no ``fires``
    facet) is held to the end of its day."""
    timed = {"facets": {"due": "2031-02-03", "fires": "2031-02-03T21:05"}}
    assert _date_tier("2031-02-03T09:00", timed, "unverified", TODAY) == "T2"
    assert _date_tier("2031-02-03", timed, "unverified", TODAY) == "T2"
    assert _date_tier("2031-02-03T21:05", timed, "unverified", TODAY) == "C1"
    assert _date_tier("2031-02-03T22:00", timed, "unverified", TODAY) == "C1"
    dated = {"facets": {"due": "2031-02-03", "fires": None}}
    assert _date_tier("2031-02-03T09:00", dated, "unverified", TODAY) == "C1"
    assert _date_tier("2031-02-03", dated, "unverified", TODAY) == "C1"
    unknown = {"facets": {"due": "2031-02-03"}}
    assert _date_tier("2031-02-03T09:00", unknown, "unverified", TODAY) == "T2"
    assert _date_tier("2031-02-04", unknown, "unverified", TODAY) == "C1"
    assert _date_escalates(timed, "unverified", TODAY, "surface_at")[0]["value"] == "2031-02-03T21:05"


def test_a_timed_held_row_snoozed_earlier_the_same_day_is_signed(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    store.update(ids["seed"], next="2031-02-03T21:05", today=TODAY)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        r = _row(base, ids["seed"])
        snooze = next(a for a in r["actions"] if a["verb"] == "intention_surface_at")
        st, body = _post(f"{base}/cockpit/verb", {"verb": "intention_surface_at", "panel_id": "tray", "row_id": r["id"],
                                                   "row_version": r["version"], "params": {"surface_at": "2031-02-03T09:00"},
                                                   "intent": "reported", "tier_seen": "C1",
                                                   "gesture_seen": snooze["gesture"]}, TOKEN)
    assert st == 409 and body.get("tier") == "T2", body            # the page that saw C1 is told the tier moved
    assert store.get(ids["seed"])["next"] == "2031-02-03T21:05"


def test_a_timed_held_row_on_open_loops_reads_its_stored_minute(tmp_path: Path) -> None:
    """Open loops builds its rows with the provider's own intention row, not the List's: that row carries the stored minute too, so a
    later minute on the held day stays C1 there and an earlier one is T2."""
    root, src, ids = _install(tmp_path)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    store.update(ids["loop"], next="2031-02-03T21:05", today=TODAY)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        rows = _get(f"{base}/cockpit/panel/loops.json", TOKEN)["rows"]
        r = next(x for x in rows if x["id"] == f"intention:{ids['loop']}")
        later ={"verb": "intention_surface_at", "panel_id": "loops", "row_id": r["id"], "row_version": r["version"],
                 "params": {"surface_at": "2031-02-03T22:00"}, "dry_run": True}
        st, body = _post(f"{base}/cockpit/verb", later, TOKEN)
        assert st == 200 and body.get("tier") == "C1", (st, body)
        st, body = _post(f"{base}/cockpit/verb", {**later, "params": {"surface_at": "2031-02-03T09:00"}}, TOKEN)
        assert body.get("tier") == "T2", (st, body)


def test_intention_update_never_carries_a_target(tmp_path: Path) -> None:
    """``target`` is in the apply allow-list only for intention_retarget's effect: the edit verb, its effect and the legacy write
    each refuse it, so no edit sets a target at the edit's own tier."""
    from levain.writes import EditError, apply_edit

    pre = {"id": "intention-1", "origin_key": "k", "status": "open", "disposition": "seed", "text": "t", "type": "task"}
    with pytest.raises(ValueError, match="changes one or more"):
        canonical_effect("intention_update", {"target": "desk"}, pre, version_of=lambda row: "v")
    root, src, ids = _install(tmp_path)
    with pytest.raises(EditError):
        apply_edit(src.write_scope, {"kind": "intention_update", "intention_id": ids["seed"], "target": "desk"})
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        r = _row(base, ids["seed"])
        st, body = _post(f"{base}/cockpit/verb", {"verb": "intention_update", "panel_id": "tray", "row_id": r["id"],
                                                   "row_version": r["version"], "params": {"target": "not-a-target"}}, TOKEN)
    assert st == 400 and body["error"] == "field_not_allowed", (st, body)
    assert "target" not in IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention").get(ids["seed"])


# --- a first part cannot be handed to the machine while a second part waits on it (diogenes-20261113-020650) --------------
def _split_pair(root: Path) -> tuple[str, str]:
    """A first part (the operator's own, band) and a second part that waits on it finishing."""
    from levain.intentions import guarded_intention_store

    store = guarded_intention_store(root / ".levain" / "memory.spores.json")
    a = store.add(type="task", text="call Jim", disposition="agenda", target="band", next="2031-01-07", today=TODAY)["id"]
    f = store.add(type="task", text="send the invoice", disposition="agenda", target="band", on_event=f"done:{a}",
                  check_by="2031-01-14", today=TODAY)["id"]
    return a, f


@pytest.mark.parametrize("target", ["desk", "board:levain"])
def test_a_first_part_with_a_waiting_second_part_is_not_handed_to_the_machine(tmp_path: Path, target: str) -> None:
    """``create_then`` refuses a then-part whose first part is the machine's, because a machine part is closed when it is handed
    off and nothing could mark it done. The retarget reached the same state after the link existed: the first part went to the desk,
    germinate composted it, and the second part waited on an event that could no longer happen. Driven through the real dispatch."""
    root, src, _ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        st, body = _retarget(base, a, {"target": target})
    assert st == 409 and body["error"] == "refused", (st, body)                            # the neighbours' answer, not a 500
    assert f in str(body) and "waits on" in str(body), (st, body)                          # naming the waiting part
    assert store.get(a)["target"] == "band"


def test_a_first_part_can_still_change_hands_to_the_operator_or_when_nothing_waits(tmp_path: Path) -> None:
    """The refusal is for the machine only, and only while a part is still waiting: a band or nudge target, a part that waits
    on nothing, and a first part whose second part has ended all retarget as before."""
    root, src, ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        st, body = _retarget(base, a, {"target": "nudge"})
        assert st == 200, body                                                            # the operator still acts: it can be marked done
        st, body = _retarget(base, ids["drop"], {"target": "board:levain"})
        assert st == 200, body                                                            # nothing waits on it
        store.descend(f, kind="composted", today=TODAY)                                   # the second part ended
        st, body = _retarget(base, a, {"target": "desk"})
        assert st == 200, body
    assert store.get(a)["target"] == "desk"


def _set_disposition(base: str, sid: str, params: dict[str, Any]) -> tuple[int, dict]:
    r = _row(base, sid)
    return _post(f"{base}/cockpit/verb", {"verb": "intention_set_disposition", "panel_id": "tray", "row_id": r["id"],
                                          "row_version": r["version"], "params": params, "intent": "reported", "tier_seen": "T2",
                                          "gesture_seen": "direct-reported"}, TOKEN)


def test_a_second_part_moved_to_a_someday_no_longer_holds_its_first_part_and_moving_it_back_does(tmp_path: Path) -> None:
    """Row 1711. A second part moved to a someday keeps its ``on_event`` but the wake (``is_event_agenda``) never reaches it, so
    nothing waits: the first part may be handed to the desk, which the old guard refused in words naming a part that could not
    wake. Moved back to an agenda it waits on its event again and the refusal returns. Driven through the real dispatch."""
    root, src, _ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        st, body = _retarget(base, a, {"target": "desk"})
        assert st == 409 and f in str(body), (st, body)                                   # live: the guard holds
        st, body = _set_disposition(base, f, {"disposition": "someday"})
        assert st == 200, body
        assert store.get(f)["disposition"] == "someday" and store.get(f)["on_event"] == f"done:{a}"   # the event is kept, unreachable
        st, body = _set_disposition(base, f, {"disposition": "agenda"})
        assert st == 200, body
        st, body = _retarget(base, a, {"target": "desk"})
        assert st == 409 and f in str(body), (st, body)                                   # waiting again: the link was never cleared
        st, body = _set_disposition(base, f, {"disposition": "someday"})
        assert st == 200, body
        st, body = _retarget(base, a, {"target": "desk"})
        assert st == 200, body                                                            # dead follower: nothing is stranded
    assert store.get(a)["target"] == "desk"


def test_a_someday_second_part_is_not_made_an_agenda_again_on_a_first_part_that_can_no_longer_be_marked_done(tmp_path: Path) -> None:
    """L3 round 1 of row 1711 (all three seats). F agenda -> someday -> A to the desk (allowed, nothing waits) -> F agenda again
    left F waiting on a ``done:A`` that nothing could mark: the state ``create_then`` refuses at link time. The move back is
    refused by the same check, with the first part named; a first part that is still the operator's own, or already done, is not."""
    root, src, _ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        st, body = _set_disposition(base, f, {"disposition": "someday"})
        assert st == 200, body
        st, body = _retarget(base, a, {"target": "desk"})
        assert st == 200, body                                                            # nothing waits on it: allowed
        st, body = _set_disposition(base, f, {"disposition": "agenda"})
        assert st == 409 and body["error"] == "refused", (st, body)                       # the neighbours' answer, not a 500
        assert a in str(body) and "handed to the desk" in str(body) and "someday" in str(body), (st, body)   # names the part, why, what to do
    assert store.get(f)["disposition"] == "someday" and store.get(f)["on_event"] == f"done:{a}"      # refused: nothing written


def test_the_other_door_to_an_agenda_asks_the_same_question(tmp_path: Path) -> None:
    """A someday second part given a who and a when through ``intention_retarget`` also becomes an agenda: the same refusal."""
    root, src, _ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        assert _set_disposition(base, f, {"disposition": "someday"})[0] == 200
        assert _retarget(base, a, {"target": "board:levain"})[0] == 200
        st, body = _retarget(base, f, {"target": "band", "surface_at": "2031-02-03"})
        assert st == 409 and a in str(body), (st, body)
    assert store.get(f)["disposition"] == "someday"


@pytest.mark.parametrize("first_part", ["open", "done", "composted"])
def test_a_second_part_comes_back_to_an_agenda_only_while_its_first_part_can_still_be_done(tmp_path: Path, first_part: str) -> None:
    """Still open and the operator's own: it waits again (and holds the first part). Already done: it is an agenda the wake
    reaches at its next run. Ended without being done: refused, since nothing can mark it."""
    root, src, _ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        assert _set_disposition(base, f, {"disposition": "someday"})[0] == 200
        if first_part != "open":
            store.descend(a, kind="done" if first_part == "done" else "composted", today=TODAY)
        st, body = _set_disposition(base, f, {"disposition": "agenda"})
    assert (st == 200) == (first_part != "composted"), (first_part, st, body)
    assert store.get(f)["disposition"] == ("someday" if first_part == "composted" else "agenda")


def test_the_legacy_write_door_asks_the_same_question(tmp_path: Path) -> None:
    """``apply_edit``'s ``intention_set_disposition`` (the terminal UI and the CLI) refuses the same move, with the same words."""
    from levain.writes import EditError, apply_edit

    root, src, _ids = _install(tmp_path)
    a, f = _split_pair(root)
    store = IntentionStore(root / ".levain" / "memory.spores.json", id_prefix="intention")
    apply_edit(src.write_scope, {"kind": "intention_set_disposition", "intention_id": f, "disposition": "someday"})
    store.update(a, target="desk", today=TODAY)                                            # the first part went to the desk meanwhile
    with pytest.raises(EditError, match="handed to the desk") as exc:
        apply_edit(src.write_scope, {"kind": "intention_set_disposition", "intention_id": f, "disposition": "agenda"})
    assert a in str(exc.value) and "someday" in str(exc.value)
    assert store.get(f)["disposition"] == "someday"
    store.update(a, target="band", today=TODAY)                                            # the operator's own again: allowed
    apply_edit(src.write_scope, {"kind": "intention_set_disposition", "intention_id": f, "disposition": "agenda"})
    assert store.get(f)["disposition"] == "agenda"


# --- the guard's own edges (L3 round 1 of the diogenes-20261113-020650 fix) ------------------------------------------------
import sqlite3  # noqa: E402

from levain.cockpit.broker import Broker, BrokerError  # noqa: E402
from levain.cockpit.effects import IntentionEffectStore, TransientApplyFault  # noqa: E402
from tests.test_cockpit_k2b_broker import BROWSER, _target, home, intentions  # noqa: E402,F401 - fixtures


def _pair_in(intentions: Path) -> tuple[str, str, str]:
    """In the broker tests' store: a first part (band, key ``k.a``), a second part waiting on it, and a lone agenda (key ``k.b``)."""
    from levain.intentions import guarded_intention_store

    store = guarded_intention_store(intentions)
    a = store.add(type="task", text="call Jim", disposition="agenda", target="band", next="2031-01-07", origin_key="k.a",
                  today=TODAY)["id"]
    f = store.add(type="task", text="send the invoice", disposition="agenda", target="band", on_event=f"done:{a}",
                  check_by="2031-01-14", origin_key="k.f", today=TODAY)["id"]
    store.add(type="task", text="a lone agenda", disposition="agenda", target="band", next="2031-01-07", origin_key="k.b", today=TODAY)
    return a, f, "k.b"


def _retarget_via(b: Broker, intentions: Path, key: str, params: dict[str, Any]) -> dict[str, Any]:
    return b.issue(verb="intention_retarget", params=params, target=_target(intentions, key), credential=BROWSER, tier="T2",
                   intent="reported")


def _target_of(intentions: Path, key: str) -> Any:
    from anneal_memory.spores import IntentionStore as _S

    return _S(intentions, id_prefix="intention").get_by_origin_key(key).get("target")


class _NoWaitingOn(IntentionEffectStore):
    """A store of the right protocol that cannot say what waits on a row: the guard must not read that as "nothing waits"."""

    @property
    def waiting_on(self) -> Any:                                     # hasattr() is False; a call is an AttributeError
        raise AttributeError("waiting_on")


class _Counting(IntentionEffectStore):
    calls = 0
    fault: Exception | None = None

    def waiting_on(self, anchor_id: str) -> list[str]:
        type(self).calls += 1
        if self.fault is not None:
            raise self.fault
        return super().waiting_on(anchor_id)


def test_a_store_that_cannot_say_what_waits_is_not_a_way_past_the_guard(home: Path, intentions: Path) -> None:
    from tests.test_cockpit_k2b_broker import _broker

    _pair_in(intentions)
    b = _broker(home, intentions, _NoWaitingOn(intentions))
    try:
        with pytest.raises(Exception):                                # fails closed (loudly), never "nothing waits"
            _retarget_via(b, intentions, "k.a", {"target": "desk"})
    finally:
        b.stop()
    assert _target_of(intentions, "k.a") == "band"


def test_the_stranding_read_is_for_a_machine_target_only(home: Path, intentions: Path) -> None:
    """A retarget to the band or a nudge never reads the store for followers, so a store fault cannot refuse it; a machine target does."""
    from tests.test_cockpit_k2b_broker import _broker

    _pair_in(intentions)
    _Counting.calls = 0
    store = _Counting(intentions)
    b = _broker(home, intentions, store)
    try:
        store.fault = TransientApplyFault("OSError", "store down")
        for tgt in ("nudge", "band"):
            assert _retarget_via(b, intentions, "k.a", {"target": tgt})["state"].upper() == "COMMITTED"
        assert _Counting.calls == 0                                    # no read for an operator target
        with pytest.raises(BrokerError) as ei:
            _retarget_via(b, intentions, "k.a", {"target": "desk"})
        assert (ei.value.code, ei.value.status) == ("source_unavailable", 503), (ei.value.code, ei.value.status)
        assert _Counting.calls == 1
        store.fault = None
        with pytest.raises(BrokerError) as ei:
            _retarget_via(b, intentions, "k.a", {"target": "board:levain"})
        assert (ei.value.code, ei.value.status) == ("refused", 409) and "waits on" in str(ei.value)
        assert _Counting.calls == 2
    finally:
        b.stop()


@pytest.mark.parametrize("params", [{"target": " Desk "}, {"target": "mars"}, {"target": 5}, {"target": "board:"}, {}])
def test_a_target_the_verb_does_not_allow_gets_its_own_answer_with_or_without_a_waiting_part(
        home: Path, intentions: Path, params: dict[str, Any]) -> None:
    """An invalid or missing target is answered by ``_ce_retarget`` (its own words), identically for a first part with a waiting
    second part and for a lone agenda, and costs no read of the store."""
    from tests.test_cockpit_k2b_broker import _broker

    _pair_in(intentions)
    _Counting.calls = 0
    b = _broker(home, intentions, _Counting(intentions))
    try:
        answers = []
        for key in ("k.a", "k.b"):
            with pytest.raises(BrokerError) as ei:
                _retarget_via(b, intentions, key, params)
            answers.append((ei.value.code, ei.value.status, str(ei.value)))
    finally:
        b.stop()
    assert answers[0] == answers[1], answers
    assert answers[0][:2] == ("refused", 409) and "waits on" not in answers[0][2], answers[0]
    assert _Counting.calls == 0


def _read_faults() -> list[Exception]:
    from anneal_memory.spores import IntentionError

    return [OSError("x"), ValueError("x"), TypeError("x"), RuntimeError("x"), KeyError("x"), AttributeError("x"), IntentionError("x")]


@pytest.mark.parametrize("exc", _read_faults(), ids=lambda e: type(e).__name__)
def test_waiting_on_reports_any_read_fault_as_a_transient_fault(intentions: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    import levain.intentions as li

    def boom(path: Any) -> Any:
        raise exc

    monkeypatch.setattr(li, "list_open_readonly", boom)
    with pytest.raises(TransientApplyFault):
        IntentionEffectStore(intentions).waiting_on("intention-001")


@pytest.mark.parametrize("exc", _read_faults(), ids=lambda e: type(e).__name__)
def test_snapshot_and_waiting_on_answer_the_same_read_faults(intentions: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """The two reads share one set of faults: a fault one wraps, the other wraps (they cannot drift apart)."""
    store = IntentionEffectStore(intentions)

    def boom(*a: Any, **k: Any) -> Any:
        raise exc

    monkeypatch.setattr(store._store, "get_by_origin_key", boom)
    with pytest.raises(TransientApplyFault):
        store.snapshot("intention:k.a")


def test_a_repeating_reminder_is_never_handed_to_a_machine_target() -> None:
    """Row 188 L3 r1 MED: retarget kept `recur` but took `desk`/`board:<repo>`, and germination delivers ONE drop and composts the
    intention, so every later occurrence vanished. The refusal says why; band and nudge still retarget, and a one-off still goes."""
    ver = lambda row: "v"                                      # noqa: E731
    rep = {"id": "intention-1", "origin_key": "k", "status": "open", "disposition": "agenda", "text": "t", "type": "task",
           "next": "2031-02-03T09:00", "target": "nudge", "recur": "weekly SUN 21:00", "recur_anchor": "2031-02-03T09:00"}
    for machine in ("desk", "board:levain"):
        with pytest.raises(ValueError, match="repeats weekly SUN 21:00"):
            canonical_effect("intention_retarget", {"target": machine}, rep, version_of=ver)
    assert canonical_effect("intention_retarget", {"target": "band"}, rep, version_of=ver).changes["target"] == "band"
    once = {k: v for k, v in rep.items() if k not in ("recur", "recur_anchor")}
    assert canonical_effect("intention_retarget", {"target": "desk"}, once, version_of=ver).changes["target"] == "desk"
