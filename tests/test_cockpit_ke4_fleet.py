"""KE4: the roster's health per kind lives in the kernel (a Levain operator's own agents get the same
semantics), the page draws it. Resident+stateful is judged by its writing, on-call by its last answer,
the roundtable's seats fold into one group, retired members are hidden with a count."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from levain.cockpit.engine import Cockpit, ProviderSpec, Read
from tests.test_cockpit_ck1 import NOW, ROW, _ok, _refused

RES = {**ROW, "digest": {"available": True, "episodes_24h": 4, "store_bytes": 10,
                         "last_wrap_at": datetime.now(timezone.utc).isoformat()}}
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


# --- L3 r1 fixes -----------------------------------------------------------------------------------
def _at(rows, now=None):
    """The rows' health read through a Cockpit whose clock is ``now`` (F3)."""
    ck = Cockpit(clock=(lambda: now) if now else None)
    ck.register(ProviderSpec(id="p", kind="roster", title="P", priority="feed", read=lambda c: Read(value={"rows": list(rows)})))
    p = ck.panel("p")
    assert p["status"] in ("ok", "empty"), p
    return {r["name"]: r["health"] for r in p["value"]["rows"]}


def test_f1_missing_or_unreadable_evidence_is_unknown_never_ok() -> None:
    dg = {"available": True, "episodes_24h": 4}
    h = _at([{**RES, "digest": dg},                                           # wrap_every set, no last_wrap_at
             {**RES, "name": "nullwrap", "digest": {**dg, "last_wrap_at": None}},
             {**RES, "name": "noavail", "digest": {"episodes_24h": 4, "last_wrap_at": NOW}},
             {**RES, "name": "empty", "memory": "stateless", "wrap_every": None, "digest": {}},
             {**RES, "name": "stateless_ok", "memory": "stateless", "wrap_every": None, "digest": {"available": True}}])
    assert h["flow"]["tone"] == "unknown" and "no wrap time reported" in h["flow"]["text"]
    assert h["nullwrap"]["tone"] == "unknown"
    assert h["noavail"]["tone"] == "unknown" and "no liveness data" in h["noavail"]["text"]
    assert h["empty"]["tone"] == "unknown" and "no liveness data" in h["empty"]["text"]
    assert h["stateless_ok"]["tone"] == "ok"
    # an alarm outranks unknown: silent AND no wrap time is still an alarm
    assert _at([{**RES, "digest": {"available": True, "episodes_24h": 0}}])["flow"]["tone"] == "alarm"


def test_f1_unreadable_or_zero_wrap_every_is_refused() -> None:
    for bad in ("1D", "1 day", "24h ", "0d", "d", "-1d"):
        _refused("roster", {"rows": [{**RES, "wrap_every": bad}]}, "wrap_every")


def test_f2_a_roundtable_member_must_be_on_call() -> None:
    _refused("roster", {"rows": [{**RES, "group": "roundtable"}]}, "roundtable", "on_call")
    _ok("roster", {"rows": [{**OC, "group": "roundtable"}]})


def test_f3_health_reads_the_cockpits_clock_not_the_hosts() -> None:
    lw = "2026-10-18T00:00:00+00:00"
    row = {**RES, "digest": {"available": True, "episodes_24h": 4, "last_wrap_at": lw}}
    assert _at([row], datetime(2026, 10, 18, 12, tzinfo=timezone.utc))["flow"]["tone"] == "ok"
    assert _at([row], datetime(2026, 10, 20, tzinfo=timezone.utc))["flow"]["tone"] == "alarm"   # host clock is long past: no matter
    # and one snapshot ages: the same cached read, a later clock, an overdue tone
    t = [datetime(2026, 10, 18, 12, tzinfo=timezone.utc)]
    ck = Cockpit(clock=lambda: t[0])
    ck.register(ProviderSpec(id="p", kind="roster", title="P", priority="feed", read=lambda c: Read(value={"rows": [row]})))
    first = ck.panel("p")
    t[0] = datetime(2026, 10, 20, tzinfo=timezone.utc)
    later = ck.panel("p")
    assert first["value"]["rows"][0]["health"]["tone"] == "ok" and later["value"]["rows"][0]["health"]["tone"] == "alarm"
    assert first["etag"] != later["etag"]


def test_f4_digest_numerics_never_crash_and_read_unknown() -> None:
    for bad in (float("inf"), float("nan"), -3, 0.5, "4", True):
        h = _at([{**RES, "digest": {"available": True, "episodes_24h": bad, "last_wrap_at": NOW}}], datetime.fromisoformat(NOW))["flow"]
        assert h["tone"] == "unknown" and "episodes_24h" in h["text"], (bad, h)
    assert _at([{**RES, "digest": {"available": True, "episodes_24h": 4.0, "last_wrap_at": NOW}}], datetime.fromisoformat(NOW))["flow"]["tone"] == "ok"
    # the store-search path: a hook's bad numeric is a search fault, not a raise
    ck = Cockpit()
    ck.register(ProviderSpec(id="p", kind="roster", title="P", priority="feed", search_scope="store", window=5,
                             read=lambda c: Read(value={"rows": [RES]}),
                             search=lambda q, n: [{**RES, "digest": {"available": True, "episodes_24h": float("inf")}}]))
    p = ck.panel("p", q="flow")
    assert p["value"]["rows"][0]["health"]["tone"] == "unknown" or "search_error" in p
