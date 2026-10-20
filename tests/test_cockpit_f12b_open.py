"""K10 S2 part B: a spore task nobody is working is OPEN with an age and a heat, never STUCK (Phill's K9 answer 1: the flow cockpit
and Levain's own Open loops panel show a spore as an open loop, "open 2 d", hot / warm / parked). The wire gains a state value and two
optional task fields; the renderer draws the quiet card and a loops-only value draws the view."""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from levain.cockpit import topology as topo
from tests.test_cockpit_f12b_topology_view import _paint, _run, _value, TV
from tests.test_cockpit_flip_parity import needs_browser
from tests.test_cockpit_k7_build import _sessions_fixture
from tests.test_cockpit_topology import _good, _panel

NOW = datetime(2026, 10, 20, 15, 0, tzinfo=timezone.utc)


def test_a_spore_task_carries_its_created_time_and_its_tier_as_heat():
    hot = SimpleNamespace(id="spore-1", text="hot loop", tier="hot", created="2026-10-18")
    stamped = SimpleNamespace(id="spore-2", text="stamped", tier="parked", created="2026-10-01T09:30:00+00:00")
    cold_unknown = SimpleNamespace(id="spore-3", text="no date", tier="lukewarm", created="")
    tasks = {t["id"]: t for t in topo._spore_tasks([hot, stamped, cold_unknown], NOW)}
    assert [t["state"] for t in tasks.values()] == ["open"] * 3
    assert tasks["spore:spore-1"]["heat"] == "hot" and tasks["spore:spore-1"]["opened"].startswith("2026-10-18")
    assert tasks["spore:spore-2"]["opened"] == "2026-10-01T09:30:00+00:00" and tasks["spore:spore-2"]["heat"] == "parked"
    assert tasks["spore:spore-3"]["opened"] is None and tasks["spore:spore-3"]["heat"] is None      # unknown is null, never a guess


def _with(task: dict) -> dict:
    v = copy.deepcopy(_good())
    v["tasks"].append({"id": "spore:x", "kind": "spore", "label": "a loop", "state": "open", "waits_on": [], "card": None, "outcomes": [],
                       "cost": {"usd": None, "tokens": None, "source": None}, "workers": [], **task})
    return v


def test_the_provider_value_accepts_open_with_its_age_and_heat_and_refuses_a_bad_one():
    assert _panel(_with({"opened": "2026-10-18T00:00:00+00:00", "heat": "warm"}))["status"] == "ok"
    assert _panel(_with({}))["status"] == "ok"                                                         # both fields are optional: flow may send neither
    assert _panel(_with({"opened": None, "heat": None}))["status"] == "ok"
    p = _panel(_with({"heat": "scorching"}))
    assert p["status"] == "error" and "value.tasks[" in p["error"] and "heat" in p["error"], p
    p = _panel(_with({"opened": "last tuesday"}))
    assert p["status"] == "error" and "opened" in p["error"], p


def _loops(v: dict) -> dict:
    v["nodes"], v["groups"], v["lost_if_closed"], v["conflicts"] = [], [], [], None
    t = lambda i, label, heat, opened: {"id": i, "kind": "spore", "label": label, "state": "open", "waits_on": [], "card": None,  # noqa: E731
                                        "outcomes": [], "cost": {"usd": None, "tokens": None, "source": None}, "workers": [], "heat": heat, "opened": opened}
    v["tasks"] = [t("spore:a", "parked one", "parked", "2026-10-10T12:00:00+00:00"), t("spore:b", "no heat", None, None),
                  t("spore:c", "hot one", "hot", "2026-10-18T12:00:00+00:00"), t("spore:d", "warm one", "warm", "2026-10-19T12:00:00+00:00")]
    v["as_of"] = "2026-10-20T12:00:00+00:00"
    return v


@needs_browser
def test_a_loops_only_value_draws_the_view_with_open_loops_hot_first_and_aged(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _loops(_value())
    probe = ("JSON.stringify({has: window.LevainTopologyView.hasWork(%s), "
             "tasks: [...%s.querySelectorAll('.tv-task')].map(t => [t.dataset.task, t.dataset.state, (t.querySelector('.tv-age') || {textContent: null}).textContent, "
             "(t.querySelector('.tv-heat') || {className: ''}).className, !!t.querySelector('.tv-tag'), t.querySelector('.tv-task-f').textContent])})") % (json.dumps(v), TV)
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    got = json.loads(out[-1])
    assert got["has"] is True, got
    ids = [t[0] for t in got["tasks"]]
    assert ids == ["spore:c", "spore:d", "spore:a", "spore:b"], got                                    # hot, warm, parked, then none
    by = {t[0]: t for t in got["tasks"]}
    assert by["spore:c"][2] == "open 2 d" and by["spore:d"][2] == "open 1 d" and by["spore:a"][2] == "open 10 d", got
    assert by["spore:b"][2] == "open" and by["spore:b"][3] == "", got                                  # no date: no number; no heat: no dot
    assert by["spore:c"][3] == "tv-heat hot" and by["spore:a"][3] == "tv-heat parked", got
    assert all(not t[4] for t in got["tasks"]), got                                                    # no STATE tag: it is quiet, not an event
    assert all("no agent attached" in t[5] and "cost not tracked" in t[5] for t in got["tasks"]), got


@needs_browser
def test_stuck_stays_a_live_worker_gone_silent_and_open_loops_sit_after_it(tmp_path, monkeypatch):
    _root, src = _sessions_fixture(tmp_path, monkeypatch)
    v = _value()
    v["tasks"] += _loops(copy.deepcopy(_value()))["tasks"][:2]
    probe = "JSON.stringify([...%s.querySelectorAll('.tv-task')].map(t => t.dataset.task))" % TV
    out = _run(tmp_path, src, [{"eval": _paint(v)}, {"eval": probe}])
    ids = json.loads(out[-1])
    assert ids.index("t-fail") < ids.index("t-stuck") < ids.index("spore:a") < ids.index("t-done"), ids   # loudest first: failed and stuck before an open loop
