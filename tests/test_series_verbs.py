"""levain#34: on a repeating reminder every surface offers done-this-time, snooze-this-time and stop-repeating, and the List's
Resolve can no longer end a series by picking 'done'. One test group per surface:

* the List row (an ``intention:`` row of an open repeating agenda): the verbs it offers, and that Resolve / Keep are not among them;
* the band card (a ringing reminder's band row on the List): its done advances, its snooze keeps the series, its clear ends it, and it
  carries the one label table;
* the phone tap (a device credential's signed pending): the broker's effect for each verb, and a plain resolve of a series refused.

The incident: ``intention_close`` with kind 'done' on a repeating agenda composted it, ending the series with no warning. A plain
one-off agenda is pinned unchanged. Temp dirs only."""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import IntentionStore

from levain import germinate, recur
from levain.cockpit import effects
from levain.cockpit.effects import canonical_effect
from levain.cockpit.verbs import DEVICE_VERBS, dispatch
from levain import writes
from levain.writes import EditError, apply_edit
from tests.test_cockpit_list_panel import CRED, INSTALL_CLASS, NOW, TODAY, Rig

SERIES_ACTIONS = {"intention_done_once", "intention_stop_series"}


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path, band=False, board=False)


def _series(rig: Rig, text: str = "weekly review", **kw: Any) -> str:
    kw = {"disposition": "agenda", "target": "nudge", "next": "2026-11-08T21:00", "recur": "weekly SUN 21:00",
          "recur_anchor": "2026-11-08T21:00", **kw}
    return rig.sp.add(type="task", text=text, today=TODAY, **kw)["id"]


def _fire(rig: Rig, row: dict[str, Any], verb: str, params: dict[str, Any] | None = None, *, confirm: bool = False) -> dict:
    return dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                    install_class=INSTALL_CLASS,
                    req={"verb": verb, "panel_id": "tray", "row_id": row["id"], "row_version": row["version"],
                         "params": params or {}, "confirm": confirm})


def _open(rig: Rig, sid: str) -> dict[str, Any]:
    return next(i for i in rig.sp.list_open(today=TODAY) if i["id"] == sid)


def _verbs(row: dict[str, Any]) -> set[str]:
    return {a["verb"] for a in row["actions"]}


# --- the label table: one place -------------------------------------------------------------------------------------------------


def test_the_three_verb_names_live_in_one_table_and_the_rows_carry_it(rig: Rig) -> None:
    assert set(recur.SERIES_VERBS) == {"done", "snooze", "stop"} and all(isinstance(v, str) and v for v in recur.SERIES_VERBS.values())
    sid = _series(rig)
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    rows = rig.rows()
    assert rows[f"intention:{sid}"]["facets"]["series_labels"] == recur.SERIES_VERBS
    assert "series_labels" not in rows[f"intention:{plain}"]["facets"]


# --- surface 1: the List row ----------------------------------------------------------------------------------------------------


def test_the_list_row_of_a_repeating_agenda_offers_the_three_verbs_and_no_plain_resolve(rig: Rig) -> None:
    sid = _series(rig)
    row = rig.rows()[f"intention:{sid}"]
    verbs = _verbs(row)
    assert SERIES_ACTIONS | {"intention_surface_at"} <= verbs, verbs         # done this time, stop repeating, snooze this time
    assert not {"intention_close", "intention_keep"} & verbs, verbs          # Resolve and Keep would each end the series


def test_the_list_resolve_with_done_no_longer_ends_a_series(rig: Rig) -> None:
    """The incident, reproduced: Resolve (``intention_close``) with 'done' on a repeating agenda composted it."""
    sid = _series(rig)
    row = rig.rows()[f"intention:{sid}"]
    with pytest.raises(EditError):
        _fire(rig, row, "intention_close", {"intention_kind": "done"}, confirm=True)
    # and under the verb layer: the write itself refuses it, whatever a caller passes
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, {"kind": "intention_close", "intention_id": sid, "intention_kind": "done", "confirm": True})
    assert "repeat" in str(ei.value).lower()
    still = _open(rig, sid)
    assert still["recur"] == "weekly SUN 21:00" and still["next"] == "2026-11-08T21:00"


def test_a_plain_agenda_is_resolved_exactly_as_before(rig: Rig) -> None:
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    row = rig.rows()[f"intention:{plain}"]
    assert {"intention_close", "intention_keep"} <= _verbs(row) and not SERIES_ACTIONS & _verbs(row)
    _fire(rig, row, "intention_close", {"intention_kind": "done"}, confirm=True)
    assert plain not in {i["id"] for i in rig.sp.list_open(today=TODAY)}


def test_done_this_time_moves_to_the_next_occurrence_even_before_this_one_is_due(rig: Rig, monkeypatch) -> None:
    monkeypatch.setattr(writes, "_local_now", lambda: datetime(2026, 11, 8, 12, 0))
    sid = _series(rig, next="2026-11-08T21:00")           # NOW is 12:00 that Sunday: this occurrence is 9 hours away
    row = rig.rows()[f"intention:{sid}"]
    _fire(rig, row, "intention_done_once")
    item = _open(rig, sid)
    assert item["next"] == "2026-11-15T21:00" and item["recur_anchor"] == "2026-11-15T21:00" and item["recur"] == "weekly SUN 21:00", item
    assert not item.get("germinated")


def test_snooze_this_time_moves_the_occurrence_and_keeps_the_series(rig: Rig) -> None:
    sid = _series(rig)
    row = rig.rows()[f"intention:{sid}"]
    _fire(rig, row, "intention_surface_at", {"surface_at": "2026-11-09T09:00"})
    item = _open(rig, sid)
    assert item["next"] == "2026-11-09T09:00" and item["recur"] == "weekly SUN 21:00" and item["recur_anchor"] == "2026-11-08T21:00", item


def test_stop_repeating_is_the_one_verb_that_ends_the_series_and_asks_first(rig: Rig) -> None:
    sid = _series(rig)
    row = rig.rows()[f"intention:{sid}"]
    with pytest.raises(EditError):
        _fire(rig, row, "intention_stop_series")                      # no confirm: refused, still open
    assert _open(rig, sid)["recur"]
    _fire(rig, row, "intention_stop_series", confirm=True)
    assert sid not in {i["id"] for i in rig.sp.list_open(today=TODAY)}


def test_a_series_verb_is_not_offered_on_a_row_that_does_not_repeat(rig: Rig) -> None:
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    row = rig.rows()[f"intention:{plain}"]
    for verb in SERIES_ACTIONS:
        with pytest.raises(EditError):
            _fire(rig, row, verb, confirm=True)


# --- surface 2: the band card (a ringing reminder's row) ------------------------------------------------------------------------


def _ring(rig: Rig, sid: str) -> dict[str, Any]:
    out = germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, ping=lambda *a, **k: True,
                              now=datetime(2026, 11, 8, 23, 0))
    assert not out["errors"] and out["due"], out
    rows = [r for r in rig.rows().values() if r["id"].startswith("band:") and r["facets"].get("intention") == sid]
    assert len(rows) == 1, list(rig.rows())
    return rows[0]


def test_the_band_card_of_a_repeating_reminder_carries_the_labels_and_its_verbs_keep_the_series_semantics(rig: Rig) -> None:
    sid = _series(rig, recur="daily 09:00", next="2026-11-08T09:00", recur_anchor="2026-11-08T09:00")   # due: NOW is 12:00
    row = _ring(rig, sid)
    assert row["facets"]["series_labels"] == recur.SERIES_VERBS
    assert {"intention_done", "intention_snooze", "intention_clear"} <= _verbs(row)
    _fire(rig, row, "intention_done")                                 # done this time: the next occurrence, the series goes on
    item = _open(rig, sid)
    assert item["next"] == "2026-11-09T09:00" and item["recur"] == "daily 09:00", item


def test_the_band_card_snooze_keeps_the_series_and_its_clear_is_the_stop(rig: Rig) -> None:
    a, b = _series(rig, "weekly review"), _series(rig, "monthly bill", recur="monthly 15 09:00", recur_anchor="2026-11-08T21:00")
    germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, ping=lambda *a, **k: True,
                        now=datetime(2026, 11, 8, 23, 0))
    rows = {r["facets"].get("intention"): r for r in rig.rows().values() if r["id"].startswith("band:")}
    _fire(rig, rows[a], "intention_snooze", {"until": "2026-11-10"})
    assert _open(rig, a)["recur"] == "weekly SUN 21:00" and _open(rig, a)["next"] == "2026-11-10"
    _fire(rig, rows[b], "intention_clear", confirm=True)
    assert b not in {i["id"] for i in rig.sp.list_open(today=TODAY)}


# --- surface 3: the phone tap (a device pending's effect) ------------------------------------------------------------------------


def test_the_two_new_verbs_are_phone_verbs_and_a_plain_resolve_of_a_series_is_refused_in_the_effect(rig: Rig, monkeypatch) -> None:
    monkeypatch.setattr(effects, "_series_now", lambda: datetime(2026, 11, 8, 12, 0))
    assert SERIES_ACTIONS <= DEVICE_VERBS
    sid = _series(rig)
    pre = _open(rig, sid)
    with pytest.raises(ValueError, match="repeat"):
        canonical_effect("intention_close", {"intention_kind": "done"}, pre)
    once = canonical_effect("intention_done_once", {}, pre)
    assert once.changes == {"next": "2026-11-15T21:00", "recur_anchor": "2026-11-15T21:00", "germinated": {}}, once.changes
    stop = canonical_effect("intention_stop_series", {}, pre)
    assert stop.changes["status"] == "resolved" and stop.changes["resolution"]["direction"] == "descend", stop.changes


def test_the_phone_effect_refuses_a_series_verb_on_an_agenda_that_does_not_repeat(rig: Rig) -> None:
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)
    pre = next(i for i in rig.sp.list_open(today=TODAY) if i["id"] == plain["id"])
    for verb in ("intention_done_once", "intention_stop_series"):
        with pytest.raises(ValueError, match="repeat"):
            canonical_effect(verb, {}, pre)
    # a plain agenda resolves in the effect exactly as before
    assert canonical_effect("intention_close", {"intention_kind": "done"}, pre).changes["status"] == "resolved"


def test_the_phone_effects_apply_to_the_store_done_once_moves_the_series_and_stop_ends_it(rig: Rig, monkeypatch) -> None:
    """Not only built: the broker's applier writes each one (the postcondition holds for the stamp the move clears)."""
    monkeypatch.setattr(effects, "_series_now", lambda: datetime(2026, 11, 8, 12, 0))
    store = effects.IntentionEffectStore(rig.root / ".levain" / "memory.spores.json")
    keep, gone = _series(rig, "keep going"), _series(rig, "end this")
    for sid, verb in ((keep, "intention_done_once"), (gone, "intention_stop_series")):
        eff = canonical_effect(verb, {}, _open(rig, sid))
        res = store.apply_unit(store.units([(0, eff)])[0])
        assert res.outcome.name in ("APPLIED", "ALREADY"), (verb, res)
    assert _open(rig, keep)["next"] == "2026-11-15T21:00" and _open(rig, keep)["recur"] == "weekly SUN 21:00"
    assert gone not in {i["id"] for i in rig.sp.list_open(today=TODAY)}


# --- levain#34 r1 (L1 + L3): who may end a series, and the advance built from the fresh row ---------------------------------------
# Every test below failed on d1a8f9b06 except where its docstring says it pins behaviour that already held.


def _row_version(rig: Rig, sid: str) -> str:
    from levain.cockpit.providers import intention_row_version

    return intention_row_version(rig.sp.get(sid))


def test_an_ends_series_key_in_a_request_is_refused_and_ends_nothing(rig: Rig) -> None:
    """L1 HIGH / codex HIGH 1: ``{"kind": "intention_close", ..., "ends_series": true}`` ended a series (the incident path, one key
    away). Who may end a series is the server's own code: the key is refused, on a plain resolve and on the stop verb alike."""
    sid = _series(rig)
    for req in ({"kind": "intention_close", "intention_id": sid, "intention_kind": "done", "confirm": True, "ends_series": True},
                {"kind": "intention_stop_series", "intention_id": sid, "confirm": True, "ends_series": True}):
        with pytest.raises(EditError) as ei:
            apply_edit(rig.src.write_scope, req)
        assert ei.value.http_status == 400 and str(ei.value) == getattr(writes, "ENDS_SERIES_REFUSAL", "<missing>"), ei.value
    assert _open(rig, sid)["recur"] == "weekly SUN 21:00"


def test_stop_repeating_on_a_row_that_does_not_repeat_is_refused_at_the_write(rig: Rig) -> None:
    """L1 LOW / codex HIGH 1 / complement MED 3: the bearer write composted a plain agenda under "stop repeating"."""
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, {"kind": "intention_stop_series", "intention_id": plain, "confirm": True})
    assert ei.value.http_status == 409 and "does not repeat" in str(ei.value)
    assert _open(rig, plain)


def test_stop_repeating_is_recorded_as_its_own_edit(rig: Rig) -> None:
    """codex LOW: the stop was audited as ``intention_close``/``descend``; Recent edits could not tell it from a resolve."""
    sid = _series(rig)
    out = apply_edit(rig.src.write_scope, {"kind": "intention_stop_series", "intention_id": sid, "confirm": True})
    assert out["action"] == "stop_series"
    last = writes.recent_edits(rig.src.write_scope.ledger_root, 1)[0]
    assert (last["kind"], last["action"], last["source"]) == ("intention_stop_series", "stop_series", f"intention:{sid}"), last


def test_a_row_that_becomes_a_series_after_the_resolve_read_it_is_refused_in_the_store_transaction(rig: Rig, monkeypatch) -> None:
    """complement LOW 5: the series check was a read before the write. A rule landing in that window is now refused under the
    store's lock (``levain.intentions.series_gate``), and nothing is resolved."""
    plain = rig.sp.add(type="task", text="becomes weekly", disposition="agenda", target="nudge", next="2026-11-08T21:00",
                       today=TODAY)["id"]
    real = writes._client_snapshot

    def window(req: Any, current: Any, intention_id: str) -> Any:      # runs after every pre-check, before the store write
        out = real(req, current, intention_id)
        rig.sp.update(plain, recur="weekly SUN 21:00", recur_anchor="2026-11-08T21:00")
        return out

    monkeypatch.setattr(writes, "_client_snapshot", window)
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, {"kind": "intention_close", "intention_id": plain, "intention_kind": "done", "confirm": True,
                                         "expect_disposition": "agenda"})
    assert ei.value.http_status == 409 and "repeats" in str(ei.value)
    assert _open(rig, plain)["recur"] == "weekly SUN 21:00"


def test_the_store_every_levain_resolve_goes_through_refuses_to_end_a_series_without_the_callers_own_word(rig: Rig) -> None:
    """The agent-side path (a direct ``descend`` on the guarded store) is held by the same rule: only a caller that passes
    ``series="end"`` (germinate, the band card's Clear) or ``series="stop"`` (the verb) ends one, and stop needs a series."""
    from levain import intentions

    store = intentions.guarded_intention_store(rig.root / ".levain" / "memory.spores.json")
    sid = _series(rig)
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    refused = getattr(intentions, "SeriesRefused", None)
    assert refused is not None, "the store has no series rule"
    with pytest.raises(refused, match="repeats"):
        store.descend(sid, kind="done")
    with pytest.raises(refused, match="repeats"):
        store.ascend(sid, kind="project", ref="grew")
    assert _open(rig, sid)["recur"]
    with pytest.raises(refused, match="does not repeat"):
        store.descend(plain, kind="composted", series="stop")
    store.descend(sid, kind="composted", series="stop")
    store.descend(plain, kind="done")                                     # a plain resolve of a plain row: as before
    assert not {sid, plain} & {i["id"] for i in rig.sp.list_open(today=TODAY)}


def test_the_row_version_covers_the_rule_and_its_anchor(rig: Rig) -> None:
    sid = _series(rig)
    v = _row_version(rig, sid)
    rig.sp.update(sid, recur="daily 09:00")
    v2 = _row_version(rig, sid)
    rig.sp.update(sid, recur_anchor="2026-11-09T09:00")
    assert len({v, v2, _row_version(rig, sid)}) == 3


def test_a_phone_done_once_built_for_one_rule_is_stale_on_a_row_whose_rule_changed(rig: Rig, monkeypatch) -> None:
    """codex HIGH 2: the effect carried a version without ``recur``: built for weekly SUN, it applied its weekly next to a row
    changed to daily meanwhile."""
    monkeypatch.setattr(effects, "_series_now", lambda: datetime(2026, 11, 8, 12, 0))
    store = effects.IntentionEffectStore(rig.root / ".levain" / "memory.spores.json")
    sid = _series(rig)
    eff = canonical_effect("intention_done_once", {}, _open(rig, sid))
    rig.sp.update(sid, recur="daily 09:00")
    res = store.apply_unit(store.units([(0, eff)])[0])
    assert res.outcome.name not in ("APPLIED", "ALREADY"), res
    item = _open(rig, sid)
    assert (item["recur"], item["next"], item["recur_anchor"]) == ("daily 09:00", "2026-11-08T21:00", "2026-11-08T21:00"), item


def test_a_write_done_once_is_compare_and_set_on_the_fresh_row_it_computed_from(rig: Rig, monkeypatch) -> None:
    """codex HIGH 2, the direct read -> update path: a rule changed between the write's read and its update is refused as stale,
    even though the caller's version matched the row it read."""
    sid = _series(rig)
    v = _row_version(rig, sid)

    def clock() -> datetime:          # read after the write's own read of the row, before its update
        rig.sp.update(sid, recur="daily 09:00")
        return datetime(2026, 11, 8, 12, 0)

    monkeypatch.setattr(writes, "_local_now", clock)
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, {"kind": "intention_done_once", "intention_id": sid}, expected_version=v)
    assert ei.value.http_status == 409, ei.value
    item = _open(rig, sid)
    assert (item["recur"], item["next"]) == ("daily 09:00", "2026-11-08T21:00"), item


def test_done_once_sent_twice_advances_once(rig: Rig, monkeypatch) -> None:
    """L1 MED 2: two sends moved next 11-08 -> 11-15 -> 11-22. A done names the row version it read (required); the resend finds
    the row moved and is refused as stale. Unbound, it is refused: a resend could not be told from a second done."""
    monkeypatch.setattr(writes, "_local_now", lambda: datetime(2026, 11, 8, 12, 0))
    sid = _series(rig)
    req = {"kind": "intention_done_once", "intention_id": sid}
    for _ in range(2):
        with pytest.raises(EditError) as ei:
            apply_edit(rig.src.write_scope, dict(req))
        assert ei.value.http_status == 400
    assert _open(rig, sid)["next"] == "2026-11-08T21:00"
    v = _row_version(rig, sid)
    apply_edit(rig.src.write_scope, dict(req), expected_version=v)
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, dict(req), expected_version=v)
    assert ei.value.http_status == 409
    assert _open(rig, sid)["next"] == "2026-11-15T21:00"


def test_a_phone_done_once_sent_twice_advances_once(rig: Rig, monkeypatch) -> None:
    """The broker half of L1 MED 2. Pins behaviour that already held on d1a8f9b06 (the effect's version names ``next``): the same
    effect applied again, or a second one built from the same read, is ``already`` (its postcondition holds): a no-op."""
    monkeypatch.setattr(effects, "_series_now", lambda: datetime(2026, 11, 8, 12, 0))
    store = effects.IntentionEffectStore(rig.root / ".levain" / "memory.spores.json")
    sid = _series(rig)
    pre = _open(rig, sid)
    one, two = canonical_effect("intention_done_once", {}, pre), canonical_effect("intention_done_once", {}, pre)
    assert store.apply_unit(store.units([(0, one)])[0]).outcome.name == "APPLIED"
    assert store.apply_unit(store.units([(0, one)])[0]).outcome.name == "ALREADY"
    assert store.apply_unit(store.units([(0, two)])[0]).outcome.name == "ALREADY"
    assert _open(rig, sid)["next"] == "2026-11-15T21:00"


# --- r1 fix 4: a promote is a resolve too, and the agents are told -----------------------------------------------------------------


def test_a_promote_of_a_series_is_refused_on_every_layer_and_not_offered(rig: Rig) -> None:
    """L1 MED 3 / complement MED 1: ``intention_promote`` (signed) still ended a series and was offered on its row."""
    sid = _series(rig)
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    rows = rig.rows()
    assert "intention_promote" not in _verbs(rows[f"intention:{sid}"])
    assert "intention_promote" in _verbs(rows[f"intention:{plain}"])                 # a plain agenda: as before
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, {"kind": "intention_promote", "intention_id": sid, "intention_kind": "project",
                                         "ref": "grew into a project", "confirm": True})
    assert ei.value.http_status == 409 and "repeats" in str(ei.value)
    with pytest.raises(ValueError, match="repeats"):
        canonical_effect("intention_promote", {"intention_kind": "project", "ref": "grew"}, _open(rig, sid))
    assert _open(rig, sid)["recur"] == "weekly SUN 21:00"


def test_the_agents_instructions_name_the_three_verbs_for_a_repeating_reminder() -> None:
    """The seed told agents to close any row with ``intention_close``; the anneal tool an agent calls is not levain's store, so
    the rule reaches the agent as words (an observation for the head: that tool is not guarded here)."""
    from levain import packs

    seed = (Path(writes.__file__).parent / "templates" / "seed" / "intention_instructions.md").read_text(encoding="utf-8")
    assert "`recur`" in seed and all(v in seed for v in recur.SERIES_VERBS.values()), "the seed does not name the three verbs"
    assert "recur" in packs.ON_DEMAND_SUMMARY["intention_instructions.md"]


# --- r1 fix 5: a date-only occurrence covers its whole day ---------------------------------------------------------------------


@pytest.mark.parametrize("now", [datetime(2026, 11, 14, 12, 0), datetime(2026, 11, 15, 8, 0)])
def test_done_this_time_on_a_date_only_occurrence_moves_past_that_whole_day(now: datetime) -> None:
    """codex MED 3: snoozed to the day 2026-11-15 (date only), "done this time" on the 14th, or on the 15th before 21:00, gave
    2026-11-15T21:00: the occurrence just done rang again that evening."""
    item = {"disposition": "agenda", "recur": "weekly SUN 21:00", "next": "2026-11-15", "recur_anchor": "2026-11-08T21:00"}
    for before_due in (True, False):
        assert recur.advance_changes(item, now, before_due=before_due)["next"] == "2026-11-22T21:00", (now, before_due)


def test_a_date_only_occurrence_on_the_daylight_saving_day_moves_by_the_wall_clock() -> None:
    """2026-11-01 is the US fall-back Sunday (01:30 happens twice there): the next occurrence is the next calendar day's 01:30,
    a wall-clock time, never the day's second 01:30 nor 24 hours from an instant."""
    item = {"disposition": "agenda", "recur": "daily 01:30", "next": "2026-11-01", "recur_anchor": "2026-10-31T01:30"}
    assert recur.advance_changes(item, datetime(2026, 11, 1, 0, 10), before_due=True)["next"] == "2026-11-02T01:30"
    assert recur.advance_changes(item, datetime(2026, 10, 31, 9, 0), before_due=True)["next"] == "2026-11-02T01:30"
    timed = {**item, "next": "2026-11-01T01:30"}           # a timed pending occurrence: exactly as before
    assert recur.advance_changes(timed, datetime(2026, 11, 1, 0, 10), before_due=True)["next"] == "2026-11-02T01:30"
    assert recur.advance_changes(timed, datetime(2026, 11, 1, 0, 10))["next"] == "2026-11-01T01:30"


# --- r1 fixes 6-8: what a series row offers, decided by the one predicate, under the one table's names ---------------------------


def test_a_series_row_does_not_offer_then_you(rig: Rig) -> None:
    """codex MED 4: done this time advances the series and emits no done event, so a then-part filed on it never woke."""
    sid = _series(rig)
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T21:00", today=TODAY)["id"]
    rows = rig.rows()
    assert "intention_then" not in _verbs(rows[f"intention:{sid}"])
    assert "intention_then" in _verbs(rows[f"intention:{plain}"])


def test_a_series_row_whose_repeat_line_does_not_draw_still_offers_the_series_verbs(rig: Rig, monkeypatch) -> None:
    """complement LOW-MED 4: verb availability hung on the display line being non-empty; a series whose line did not draw
    offered Resolve and Keep, each refused with 409 on press."""
    from levain.cockpit import list_panel

    monkeypatch.setattr(list_panel, "_repeat_line", lambda raw, nxt: None)
    sid = _series(rig)
    row = rig.rows()[f"intention:{sid}"]
    assert row["facets"].get("series_labels") == recur.SERIES_VERBS and "repeat_line" not in row["facets"]
    assert SERIES_ACTIONS <= _verbs(row) and not {"intention_close", "intention_keep"} & _verbs(row), _verbs(row)


def test_stop_repeating_takes_the_kind_the_rows_type_accepts(rig: Rig, monkeypatch) -> None:
    """L1 LOW 8: stop forced 'composted'; a type without it failed with "descend kind 'composted' is invalid"."""
    from anneal_memory import spores

    monkeypatch.setitem(spores.DESCEND_BY_TYPE, "task", frozenset({"done", "dropped"}))
    a, b = _series(rig, "write route"), _series(rig, "phone")
    apply_edit(rig.src.write_scope, {"kind": "intention_stop_series", "intention_id": a, "confirm": True})
    assert rig.sp.get(a)["resolution"]["kind"] == "dropped"
    assert canonical_effect("intention_stop_series", {}, _open(rig, b)).changes["resolution"]["kind"] == "dropped"


def test_the_decisions_card_of_a_repeating_reminder_carries_the_labels_from_one_cached_read(rig: Rig, monkeypatch) -> None:
    """L1 MED 5 / builder obs. 1: the Decisions question card kept Done / Snooze / Clear on a repeating reminder. The card reads
    whether it repeats from the question store's one cached read of the intention store (kept while the file is unchanged),
    so two cards over two panel reads cost at most one read of the store."""
    from levain import intentions

    a = _series(rig, "weekly review")
    b = _series(rig, "daily standup", recur="daily 09:00", next="2026-11-08T09:00", recur_anchor="2026-11-08T09:00")
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T09:00", today=TODAY)["id"]
    germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, ping=lambda *a, **k: True,
                        now=datetime(2026, 11, 8, 23, 0))
    real, reads = intentions.list_open_readonly, []

    def counted(*args: Any, **kw: Any) -> Any:
        reads.append(1)
        return real(*args, **kw)

    monkeypatch.setattr(intentions, "list_open_readonly", counted)
    cards = {}
    for _ in range(2):
        rows = rig.ck.panel("decisions", credential_class="token")["rows"]
        cards = {r["facets"].get("question_intention"): r for r in rows if r["facets"].get("question_intention")}
    assert {a, b, plain} <= set(cards), list(cards)
    assert cards[a]["facets"]["series_labels"] == recur.SERIES_VERBS == cards[b]["facets"]["series_labels"]
    assert "series_labels" not in cards[plain]["facets"]
    assert len(reads) <= 1, reads


# --- fix round 2 (L3 r1): a stale card cannot end, or advance, a reminder whose series state moved ----------------------------
# Every test below failed on f17f42c3b except where its docstring says it pins behaviour that already held.


def _card(rig: Rig, panel: str, sid: str) -> dict[str, Any]:
    rows = rig.ck.panel("decisions", credential_class="token")["rows"] if panel == "decisions" else list(rig.rows().values())
    key = "question_intention" if panel == "decisions" else "intention"
    return next(r for r in rows if r["facets"].get(key) == sid and r["id"].startswith(("question:", "band:")))


@pytest.mark.parametrize("panel", ["decisions", "tray"])
@pytest.mark.parametrize("moved", [{"recur": None, "recur_anchor": None}, {"recur": "weekly SUN 21:00"}],
                         ids=["rule-removed", "rule-changed"])
def test_a_done_this_time_card_is_stale_once_another_writer_removes_the_rule(rig: Rig, panel: str, moved: dict) -> None:
    """codex HIGH (r1 fix round): a card drew "Done this time"; another writer removed ``recur``; the card's version did not move,
    so the click found no advance and ENDED the reminder with no confirmation. The card's version now binds the intention's own
    version (the whole of it: a rule changed to another rule, still a series, stales the card too): the click is stale and the
    reminder is untouched."""
    sid = _series(rig, recur="daily 09:00", next="2026-11-08T09:00", recur_anchor="2026-11-08T09:00")
    germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, ping=lambda *a, **k: True,
                        now=datetime(2026, 11, 8, 23, 0))
    card = _card(rig, panel, sid)
    assert card["facets"]["series_labels"] == recur.SERIES_VERBS
    rig.sp.update(sid, **moved)                                          # another writer moved the reminder's rule
    with pytest.raises(EditError) as ei:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS, req={"verb": "intention_done", "panel_id": panel, "row_id": card["id"],
                                                   "row_version": card["version"], "params": {}, "confirm": False})
    assert ei.value.http_status == 409, ei.value
    assert _open(rig, sid)["next"] == "2026-11-08T09:00"                 # still open, not ended


@pytest.mark.parametrize("panel", ["decisions", "tray"])
def test_a_plain_done_card_is_stale_once_the_reminder_starts_repeating(rig: Rig, panel: str) -> None:
    """The reverse: a plain "Done" card, then a rule lands; the stale Done would have advanced a series the operator never saw."""
    plain = rig.sp.add(type="task", text="one-off", disposition="agenda", target="nudge", next="2026-11-08T09:00", today=TODAY)["id"]
    germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, ping=lambda *a, **k: True,
                        now=datetime(2026, 11, 8, 23, 0))
    card = _card(rig, panel, plain)
    assert "series_labels" not in card["facets"]
    rig.sp.update(plain, recur="daily 09:00", recur_anchor="2026-11-08T09:00")
    with pytest.raises(EditError) as ei:
        dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
                 install_class=INSTALL_CLASS, req={"verb": "intention_done", "panel_id": panel, "row_id": card["id"],
                                                   "row_version": card["version"], "params": {}, "confirm": False})
    assert ei.value.http_status == 409, ei.value
    assert _open(rig, plain)["next"] == "2026-11-08T09:00"


@pytest.mark.parametrize("panel", ["decisions", "tray"])
def test_a_card_version_is_stable_across_reads_of_an_unchanged_reminder(rig: Rig, panel: str) -> None:
    """Pins the binding does not stale a card on its own: two reads of an unchanged reminder give one version, and a fresh card's
    Done this time still moves the series (passed before the fix too)."""
    sid = _series(rig, recur="daily 09:00", next="2026-11-08T09:00", recur_anchor="2026-11-08T09:00")
    germinate.germinate(rig.root / ".levain" / "memory.spores.json", rig.qdir, install=rig.root, ping=lambda *a, **k: True,
                        now=datetime(2026, 11, 8, 23, 0))
    card = _card(rig, panel, sid)
    assert _card(rig, panel, sid)["version"] == card["version"]
    dispatch(registry=rig.reg, cockpit=rig.ck, scope=rig.src.write_scope, route="cockpit", credential=CRED,
             install_class=INSTALL_CLASS, req={"verb": "intention_done", "panel_id": panel, "row_id": card["id"],
                                               "row_version": card["version"], "params": {}, "confirm": False})
    assert _open(rig, sid)["recur"] == "daily 09:00" and _open(rig, sid)["next"] != "2026-11-08T09:00"


def test_stop_repeating_on_an_intention_that_does_not_exist_is_a_404(rig: Rig) -> None:
    """gemini MED (r1 fix round): the stop read the row for its kind and went on with no row; a missing id is now ``not_found``
    (404) before the kind is chosen, as every other intention write names it."""
    _series(rig)
    with pytest.raises(EditError) as ei:
        apply_edit(rig.src.write_scope, {"kind": "intention_stop_series", "intention_id": "intention-999", "confirm": True})
    assert (ei.value.code, ei.value.http_status) == ("not_found", 404), (ei.value.code, ei.value.http_status, str(ei.value))
