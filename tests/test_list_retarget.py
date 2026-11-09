"""The List's "who does it" (board row 178, build 1, spore rows): who acts after a wake is DERIVED from ``target`` (the operator for
band and nudge, the machine for desk and board), never stored, and editing it sets ``target`` through ``spore_retarget``. Also pinned:
a timed ``next`` round-trips through the snooze verb and its tier, and only a target flow's germinate also reads is written."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from anneal_memory.spores import SporeStore

from levain.cockpit.effects import canonical_effect
from levain.cockpit.verbs import _date_tier
from levain.spores import executor_of, valid_retarget
from tests.test_cockpit_k2a import TOKEN, _get, _post, _serve
from tests.test_dashboard import _make_levain_install
from levain.dashboard import SubstrateSource

TODAY = date(2026, 11, 9)


def _install(tmp_path: Path) -> tuple[Path, SubstrateSource, dict[str, str]]:
    root = _make_levain_install(tmp_path)
    store = SporeStore(root / ".levain" / "memory.spores.json")
    ids = {
        "seed": store.add(type="task", text="a plain seed", disposition="seed", today=TODAY)["id"],
        "drop": store.add(type="task", text="a desk drop", disposition="agenda", target="desk",
                          next="2031-01-07T09:00", today=TODAY)["id"],
        "loop": store.add(type="task", text="a loop", tier="hot", today=TODAY)["id"],
    }
    return root, SubstrateSource.local(root), ids


def _row(base: str, sid: str) -> dict[str, Any]:
    rows = _get(f"{base}/cockpit/panel/tray.json", TOKEN)["rows"]
    return next(r for r in rows if r["id"] == f"spore:{sid}")


def _retarget(base: str, sid: str, params: dict[str, Any]) -> tuple[int, dict]:
    r = _row(base, sid)
    return _post(f"{base}/cockpit/verb", {"verb": "spore_retarget", "panel_id": "tray", "row_id": r["id"],
                                          "row_version": r["version"], "params": params, "intent": "reported", "tier_seen": "T2",
                                          "gesture_seen": "direct-reported"}, TOKEN)


def test_who_is_derived_from_the_target_and_only_a_target_flow_reads_is_written() -> None:
    assert [executor_of(t) for t in ("band", "nudge", "desk", "board:levain", None, "elsewhere", "board:")] == \
        ["operator", "operator", "machine", "machine", None, None, None]
    assert all(valid_retarget(t) for t in ("band", "nudge", "desk", "board:levain", "board:a.b_c-1"))
    assert not any(valid_retarget(t) for t in ("board:", "board:a b", "board:../x", "board:-x", "you", None, 3))


def test_the_effect_refuses_what_it_may_not_write() -> None:
    pre = {"id": "spore-1", "origin_key": "k", "status": "open", "disposition": "seed", "text": "t", "type": "task"}
    ver = lambda row: "v"                                      # noqa: E731
    ok = canonical_effect("spore_retarget", {"target": "band", "surface_at": "2031-02-03T09:00"}, pre, version_of=ver)
    assert ok.changes == {"target": "band", "disposition": "agenda", "next": "2031-02-03T09:00"}
    for bad, why in (({"target": "board:a b", "surface_at": "2031-02-03"}, "target must be"),
                     ({"target": "band"}, "needs a date"),                                 # a seed has no date to keep
                     ({"target": "band", "surface_at": "2031-02-30"}, "range"),
                     ({"target": "band", "surface_at": "2031-02-03", "text": "x"}, "sets target"),
                     ({"surface_at": "2031-02-03"}, "sets target")):
        with pytest.raises(ValueError, match=why):
            canonical_effect("spore_retarget", bad, pre, version_of=ver)
    with pytest.raises(ValueError, match="neither"):
        canonical_effect("spore_retarget", {"target": "band"}, {**pre, "disposition": None, "next": "2031-02-03"}, version_of=ver)
    held = {**pre, "disposition": "agenda", "next": "2031-02-03T09:00", "attention": "talk"}
    with pytest.raises(ValueError, match="attention"):                                    # steering's own rule: no ping on a desk drop
        canonical_effect("spore_retarget", {"target": "desk"}, held, version_of=ver)
    keep = canonical_effect("spore_retarget", {"target": "band"}, {**held, "attention": None}, version_of=ver)
    assert "next" not in keep.changes                                                      # the row keeps its date


def test_a_seed_given_who_and_when_becomes_a_dated_agenda_and_the_agenda_changes_hands(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}, install_class="reported") as (base, _h):
        assert "spore_retarget" in {a["verb"] for a in _row(base, ids["seed"])["actions"]}
        st, body = _retarget(base, ids["seed"], {"target": "band", "surface_at": "2031-02-03T09:30"})
        assert st == 200, body
        st, body = _retarget(base, ids["drop"], {"target": "board:levain"})
        assert st == 200, body
        st, body = _retarget(base, ids["drop"], {"target": "somebody"})
        assert st >= 400, body
    store = SporeStore(root / ".levain" / "memory.spores.json")
    seed, drop = store.get(ids["seed"]), store.get(ids["drop"])
    assert (seed["disposition"], seed["target"], seed["next"]) == ("agenda", "band", "2031-02-03T09:30")
    assert (drop["target"], drop["next"]) == ("board:levain", "2031-01-07T09:00")          # who changes, when does not


def test_the_legacy_write_never_takes_a_target(tmp_path: Path) -> None:
    from levain.writes import EditError, apply_edit

    root, src, ids = _install(tmp_path)
    with pytest.raises(EditError, match="signed") as ei:
        apply_edit(src.write_scope, {"kind": "spore_retarget", "spore_id": ids["seed"], "target": "desk", "surface_at": "2031-02-03"})
    assert ei.value.code == "bad_verb_arg"
    assert "target" not in SporeStore(root / ".levain" / "memory.spores.json").get(ids["seed"])
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}) as (base, _h):       # no broker: a bearer token is refused at its tier
        st, body = _post(f"{base}/edit", {"kind": "spore_retarget", "spore_id": ids["seed"], "target": "desk"}, TOKEN)
    assert st == 403 and body["error"] == "needs_broker", (st, body)


def test_a_timed_snooze_round_trips_and_keeps_its_tier(tmp_path: Path) -> None:
    root, src, ids = _install(tmp_path)
    with _serve(src, write_token=TOKEN, surface_tokens={"browser": TOKEN}) as (base, _h):
        r = _row(base, ids["seed"])
        st, body = _post(f"{base}/cockpit/verb", {"verb": "spore_surface_at", "panel_id": "tray", "row_id": r["id"],
                                                   "row_version": r["version"], "params": {"surface_at": "2031-02-03T21:05"}}, TOKEN)
        assert st == 200, body
        r = _row(base, ids["seed"])
        st, body = _post(f"{base}/cockpit/verb", {"verb": "spore_surface_at", "panel_id": "tray", "row_id": r["id"],
                                                   "row_version": r["version"], "params": {"surface_at": "2031-02-03T25:99"}}, TOKEN)
        assert st >= 400, body
    assert SporeStore(root / ".levain" / "memory.spores.json").get(ids["seed"])["next"] == "2031-02-03T21:05"
    held = {"facets": {"due": "2031-02-03T21:05"}}
    assert _date_tier("2031-02-03T21:05", held, "verified", TODAY) == "C1"
    assert _date_tier("2031-02-03T09:00", held, "unverified", TODAY) == "C1"               # a later minute on the same day is not sooner
    assert _date_tier("2031-02-02T09:00", held, "unverified", TODAY) == "T2"               # an earlier day on an unverified held row is
