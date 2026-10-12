"""KE4: the roster's health per kind lives in the kernel (a Levain operator's own agents get the same
semantics), the page draws it. Resident+stateful is judged by its writing, on-call by its last answer,
the roundtable's seats fold into one group, retired members are hidden with a count."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from tests.test_cockpit_ck1 import NOW, ROW, _ok

RES = {**ROW, "digest": {"available": True, "episodes_24h": 4, "store_bytes": 10}}
OC = {**ROW, "name": "codex", "cadence": "on_call", "memory": "stateless", "silence_is_signal": False,
      "wrap_every": None, "digest": None, "last_call": {"at": NOW, "outcome": "ok"}}


def _rows(*rows):
    return {r["name"]: r for r in _ok("roster", {"rows": list(rows)})["value"]["rows"]}


def test_resident_stateful_is_judged_by_its_writing() -> None:
    r = _rows(RES, {**RES, "name": "argus", "digest": {"available": True, "episodes_24h": 0}},
              {**RES, "name": "dead", "digest": {"available": False, "episodes_24h": 3}},
              {**RES, "name": "nodata", "digest": None})
    assert r["flow"]["health"]["tone"] == "ok" and "4 episodes in 24 h" in r["flow"]["health"]["text"]
    assert r["argus"]["health"]["tone"] == "alarm" and "should be writing" in r["argus"]["health"]["text"]
    assert r["dead"]["health"]["tone"] == "alarm"
    assert r["nodata"]["health"]["tone"] == "unknown" and r["nodata"]["health"]["text"] == "no data"


def test_wrap_age_alarms_only_against_wrap_every_for_resident_stateful() -> None:
    old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    dg = {"available": True, "episodes_24h": 4}
    r = _rows({**RES, "digest": {**dg, "last_wrap_at": old}},
              {**RES, "name": "b", "digest": {**dg, "last_wrap_at": fresh}},
              {**RES, "name": "c", "wrap_every": None, "digest": {**dg, "last_wrap_at": old}},
              {**OC, "wrap_every": "1d", "memory": "stateful", "digest": {"last_wrap_at": old}})
    assert r["flow"]["health"]["tone"] == "alarm" and "wrap" in r["flow"]["health"]["text"]
    assert r["b"]["health"]["tone"] == "ok"
    assert r["c"]["health"]["tone"] == "ok"                  # no wrap_every: wrap age is never an alarm
    assert r["codex"]["health"]["tone"] == "neutral"         # on call: never, even with an old wrap


def test_on_call_is_a_neutral_fact_never_an_alarm() -> None:
    failed = {**OC, "name": "glm", "last_call": {"at": NOW, "outcome": "failed"}}
    never = {**OC, "name": "gemini", "last_call": None}
    stale = {**OC, "name": "anansi", "memory": "stateful", "digest": {"store_bytes": 2_000_000},
             "last_call": {"at": "2026-08-01T00:00:00+00:00", "outcome": "ok"}}
    r = _rows(OC, failed, never, stale)
    for n in r:
        assert r[n]["health"]["tone"] in ("neutral", "unknown") and r[n]["fleet_group"] == "on_call"
    assert r["codex"]["health"]["text"] == "answered" and r["codex"]["health"]["at"] == NOW
    assert r["glm"]["health"]["text"] == "last call failed"
    assert r["gemini"]["health"]["text"] == "not called yet" and r["gemini"]["health"]["at"] is None
    assert r["anansi"]["health"]["memory"] == "memory 2.0 MB" and r["codex"]["health"]["memory"] == "stateless"


def test_resident_without_silence_signal_is_not_alarmed() -> None:
    r = _rows({**RES, "silence_is_signal": False, "digest": {"available": True, "episodes_24h": 0}})
    assert r["flow"]["health"]["tone"] == "neutral"


def test_roundtable_folds_and_groups() -> None:
    rt = lambda n, lc: {**OC, "name": n, "group": "roundtable", "last_call": lc}   # noqa: E731
    out = _ok("roster", {"rows": [RES, OC, rt("popper", {"at": NOW, "outcome": "ok"}), rt("coyote", None)]})["value"]
    g = {r["name"]: r["fleet_group"] for r in out["rows"]}
    assert g == {"flow": "resident", "codex": "on_call", "popper": "roundtable", "coyote": "roundtable"}
    assert all(r["health"]["tone"] == "neutral" for r in out["rows"] if r["fleet_group"] == "roundtable")


def test_retired_hidden_with_a_count() -> None:
    out = _ok("roster", {"rows": [RES, {**OC, "lifecycle": "retired"}, {**OC, "name": "z", "lifecycle": "retired"}]})
    assert [r["name"] for r in out["value"]["rows"]] == ["flow"] and out["value"]["retired_hidden"] == 2


def test_a_roster_is_not_cut_at_the_list_default_of_ten() -> None:
    from levain.cockpit.engine import roster_visible
    from tests.test_cockpit_ck1 import _value_panel
    ck = _value_panel("roster", {"rows": [{**RES, "name": f"a{i}"} for i in range(17)]})
    p = ck.panel("p")
    assert p["search"]["default_visible"] == 40 and p["count"] == 17
    assert roster_visible(type("S", (), {"kind": "list", "search_default_visible": 10})()) == 10
    assert roster_visible(type("S", (), {"kind": "roster", "search_default_visible": 5})()) == 5
