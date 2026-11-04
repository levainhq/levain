"""KE4: the roster's health per kind lives in the kernel (a Levain operator's own agents get the same
semantics), the page draws it. Resident+stateful is judged by its writing, on-call by its last answer,
the roundtable's seats fold into one group, retired members are hidden with a count."""
from __future__ import annotations

import json
import os
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
    assert h["stateless_ok"]["tone"] == "unknown" and "no run time reported" in h["stateless_ok"]["text"]
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


# ---- the stateless resident reading (a role run on a schedule): a time, its source, a status as text ----
ROLE = {**ROW, "name": "diogenes", "kind": "role", "memory": "stateless", "wrap_every": None,
        "digest": {"available": True, "episodes_24h": 33, "last_run_at": "2026-11-04T02:56:47-05:00",
                   "last_run_status": "skipped", "run_failed": False, "run_late": False, "last_run_source": "scheduler"}}


def _role(**dg):
    return _rows({**ROLE, "digest": {**ROLE["digest"], **dg}})["diogenes"]["health"]


def test_stateless_resident_never_reads_running_and_shows_its_last_run_time() -> None:
    h = _role()
    assert "running" not in h["text"]
    assert h["text"].startswith("last run 02:56") and "33 episodes in 24 h" in h["text"]
    assert "skipped" in h["text"] and h["tone"] == "ok"      # shown as text; the tone comes from the facts, not the word


def test_stateless_resident_names_the_source_when_no_run_is_recorded() -> None:
    h = _role(last_run_source="episode", last_run_status=None, run_failed=None, run_late=None)
    assert "newest episode 02:56" in h["text"] and "no run recorded" in h["text"]
    assert h["tone"] == "neutral"      # a time with its source, not a liveness verdict
    unk = _role(last_run_source="episode", last_run_status=None, run_failed=None, run_late=None, available=None)
    assert "newest episode 02:56" in unk["text"] and unk["tone"] == "neutral"     # an unknown `available` does not hide it


def test_a_scheduler_stop_reads_as_a_stop_never_as_a_run_time_or_as_health() -> None:
    h = _role(last_run_source="gate", last_run_at=None, last_run_status="skipped_low_energy",
              run_failed=None, run_late=None, available=None)
    assert h["text"] == "scheduler stop · status skipped_low_energy" and h["tone"] == "neutral"
    assert "last run" not in h["text"] and "newest episode" not in h["text"]


def test_run_flags_are_three_valued_null_is_unknown_never_on_time() -> None:
    # run_late / available null = UNKNOWN: a reader that tests `not run_late` would read it as on time
    h = _role(run_late=None, run_failed=None, available=None)
    assert h["tone"] == "unknown" and "no liveness data" in h["text"]
    assert _role(run_late=True, available=None)["tone"] == "alarm"


def test_failed_and_late_runs_are_alarms_with_their_own_words() -> None:
    f = _role(run_failed=True, available=False)
    assert f["tone"] == "alarm" and "last run failed" in f["text"] and "write path" not in f["text"]
    late = _role(run_late=True, available=False)
    assert late["tone"] == "alarm" and "run late" in late["text"] and "write path" not in late["text"] and "02:56" in late["text"]
    both = _role(run_failed=True, run_late=True, available=False)
    assert "failed" in both["text"] and "late" in both["text"]


def test_a_status_is_text_never_a_health_word() -> None:
    for s in ("skipped", "ok", "success", "weird"):
        h = _role(last_run_status=s)
        assert f"status {s}" in h["text"] and h["tone"] == "ok"
    assert "status" not in _role(last_run_status=None)["text"]


def test_old_roster_shape_degrades_without_error_and_never_reads_running() -> None:
    h = _rows({**ROLE, "digest": {"available": True, "episodes_24h": 33}})["diogenes"]["health"]
    assert h["tone"] == "unknown" and h["text"] == "no run time reported · 33 episodes in 24 h"     # absence of a run time is not health
    dead = _rows({**ROLE, "digest": {"available": False}})["diogenes"]["health"]
    assert dead["tone"] == "alarm" and dead["text"] == "write path unavailable"       # unchanged for the old shape
    junk = _role(last_run_at="not a time", run_failed="yes", run_late=1)
    assert "running" not in junk["text"] and "no run time reported" in junk["text"]


def test_a_stateful_resident_is_unchanged_by_the_run_fields() -> None:
    h = _rows({**RES, "digest": {**RES["digest"], "run_failed": True, "last_run_at": "2026-11-04T02:56:47-05:00"}})["flow"]["health"]
    assert h["tone"] == "ok" and "4 episodes in 24 h" in h["text"]


# ---- the page: headings per kind, and the role's reading drawn (real headless Chrome over a served cockpit) ----

from pathlib import Path

from tests.test_cockpit_flip_parity import _drive, needs_browser
from tests.test_cockpit_k2a import _install
from tests.test_cockpit_scroll_keep_page import _Served


@needs_browser
def test_the_fleet_card_heads_agents_and_roles_apart_and_reads_a_roles_last_run(tmp_path: Path) -> None:
    """`LEVAIN_FLEET_SHOTS=<dir>` keeps the screenshots there and `LEVAIN_FLEET_PREFIX` names them (before | after)."""
    from levain.cockpit.providers import build_default_cockpit

    shots = Path(os.environ.get("LEVAIN_FLEET_SHOTS") or tmp_path)
    shots.mkdir(parents=True, exist_ok=True)
    prefix = os.environ.get("LEVAIN_FLEET_PREFIX") or "after"
    rows = [
        RES,
        {**ROLE, "kind": "role"},
        {**ROLE, "name": "ontime", "kind": "role", "digest": {**ROLE["digest"], "last_run_status": "ok"}},
        {**ROLE, "name": "late", "kind": "role", "digest": {**ROLE["digest"], "run_late": True, "available": False}},
        {**{k: v for k, v in ROLE.items() if k not in ("kind", "memory")}, "memory": "stateless", "name": "old", "digest": {"available": True, "episodes_24h": 3}},          # an older roster: no kind, no run fields
        {**OC, "last_call": {"at": datetime.now(timezone.utc).isoformat(), "outcome": "ok"}},
    ]
    _root, src, _ids = _install(tmp_path)
    ck = build_default_cockpit(src)
    ck.register(ProviderSpec(id="fleetcard", kind="roster", title="Fleet", priority="feed", region="operate",
                             read=lambda c: Read(value={"rows": rows})))
    panel = "document.querySelector('[data-ck-panel=fleetcard]')"
    facts = (f"JSON.stringify({{groups: [...{panel}.querySelectorAll('.fl-group')].map(g => [g.dataset.ckFleetGroup, "
             f"g.querySelector('.grp-h').textContent, [...g.querySelectorAll('.fl .n')].map(n => n.textContent)]), "
             f"d: [...{panel}.querySelectorAll('.fl')].map(f => f.querySelector('.n').textContent + '|' + f.querySelector('.d').textContent + '|' + f.querySelector('.d').className)}})")
    with _Served(src, cockpit=ck, surface_tokens={"browser": "T"}, install_class="reported") as url:
        out = _drive(tmp_path, f"{url}/", [
            {"emulate": {"w": 1440, "h": 1000, "dpr": 1}},
            {"until": f"{panel} && {panel}.querySelectorAll('.fl').length === {len(rows)}"},
            {"eval": f"{panel}.scrollIntoView(); 1"},
            {"shot": str(shots / f"fleet_{prefix}_desktop.png")},
            {"eval": facts},
            {"emulate": {"w": 390, "h": 844, "dpr": 2, "mobile": True}},
            {"eval": f"{panel}.scrollIntoView(); 1"},
            {"until": "document.documentElement.scrollWidth <= window.innerWidth + 1"},
            {"shot": str(shots / f"fleet_{prefix}_phone.png")},
        ])
    f = json.loads([v for v in out["values"] if isinstance(v, str) and v.startswith("{")][0])
    groups = {g[0]: g for g in f["groups"]}
    assert groups["resident"][1:] == ["Agents · judged by their writing", ["flow"]]
    assert groups["role"][1] == "Roles · run on a schedule" and groups["role"][2] == ["diogenes", "ontime", "late", "old"]
    d = {x.split("|")[0]: x.split("|")[1:] for x in f["d"]}
    assert d["diogenes"][0].startswith("last run 02:56") and "running" not in d["diogenes"][0] and "bad" not in d["diogenes"][1]
    assert d["late"][0].startswith("run late") and "bad" in d["late"][1]
    assert d["old"][0].startswith("no run time reported")


# ---- L3 r1 fixes: one classification, unreadable run flags, a huge count, a bounded status ----
def test_a_run_flag_that_is_present_but_not_a_bool_is_unknown_not_ok() -> None:
    for k in ("run_failed", "run_late"):
        for bad in ("true", 1, "yes"):
            h = _role(**{k: bad})
            assert h["tone"] == "unknown" and "run state unreadable" in h["text"]


def test_kind_decides_the_reading_and_memory_only_when_kind_is_absent() -> None:
    r = _rows({**ROLE, "name": "rs", "memory": "stateful", "digest": {**ROLE["digest"], "run_failed": True}},
              {**ROLE, "name": "as", "kind": "agent", "memory": "stateless", "digest": {"available": True, "episodes_24h": 4}})
    assert r["rs"]["health"]["tone"] == "alarm" and "last run failed" in r["rs"]["health"]["text"]
    assert r["as"]["health"]["tone"] == "ok" and "no run time reported" not in r["as"]["health"]["text"]


def test_a_huge_count_and_a_long_status_do_not_break_the_row() -> None:
    h = _role(episodes_24h=10 ** 1000, last_run_status="x\n" * 100)
    assert h["tone"] == "ok" and "health unreadable" not in h["text"] and "episodes in 24 h" not in h["text"] and len(h["text"]) < 100
