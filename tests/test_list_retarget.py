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
