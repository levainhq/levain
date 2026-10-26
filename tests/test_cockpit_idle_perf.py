"""An idle store costs the cockpit nothing: nothing time-derived enters a panel's etag, and an unchanged store
serves one built view (the watcher ticks every few seconds; each tick and each panel GET used to rebuild the
whole SubstrateView and, through the health panel's moving age, announce a change that never happened)."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from anneal_memory import Store
from anneal_memory.spores import SporeStore

from levain.cockpit import providers
from levain.cockpit.providers import build_default_cockpit
from levain.dashboard import SubstrateSource
from tests.test_cockpit_k1 import NONE_CRED, _install


def _wrap(src: SubstrateSource) -> None:
    md = src.anneal.continuity_md
    md.write_text("## State\nfoc\n", encoding="utf-8")
    with Store(src.anneal.episodic_db) as store:
        e = store.record("episode A", "decision")
        store.wrap_started(token="w1", episode_ids=[e.id])
        store.wrap_completed(1, 20, wrap_token="w1", episode_ids=[e.id], pair_id="p1",
                             content_hash=hashlib.sha256(md.read_bytes()).hexdigest())


class _Clock:
    def __init__(self) -> None:
        self.t = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.t


@pytest.fixture
def counted(tmp_path: Path, monkeypatch):
    root, src = _install(tmp_path)
    _wrap(src)
    calls: list[int] = []
    real = type(src).build

    def build(self, **kw):
        calls.append(1)
        return real(self, **kw)

    monkeypatch.setattr(type(src), "build", build)
    clock = _Clock()
    return root, src, build_default_cockpit(src, clock=clock), clock, calls


def test_a_health_render_five_seconds_later_has_the_same_etag(counted) -> None:
    _r, _s, ck, clock, _calls = counted
    first = ck.panel("health")["etag"]
    clock.t += timedelta(seconds=5)
    assert ck.panel("health")["etag"] == first


def test_health_carries_the_save_stamp_not_a_moving_age(counted) -> None:
    _r, _s, ck, clock, _calls = counted
    m1 = {x["label"]: x["value"] for x in ck.panel("health")["value"]["metrics"]}
    clock.t += timedelta(hours=3)
    m2 = {x["label"]: x["value"] for x in ck.panel("health")["value"]["metrics"]}
    assert "last save (hours ago)" not in m1
    assert isinstance(m1["last save at"], str) and datetime.fromisoformat(m1["last save at"])
    assert m1["last save at"] == m2["last save at"]


def test_n_panel_reads_of_an_unchanged_store_build_once(counted) -> None:
    _r, _s, ck, clock, calls = counted
    for pid in ("health", "graph", "episodes", "wraps", "crystals") * 3:
        ck.panel(pid)
        clock.t += timedelta(seconds=5)
    ck.manifest(NONE_CRED)
    assert len(calls) == 1, len(calls)


def test_a_store_change_rebuilds_once_more(counted) -> None:
    root, src, ck, _clock, calls = counted
    ck.panel("episodes")
    assert len(calls) == 1
    SporeStore(root / ".levain" / "memory.spores.json").add(type="task", text="new loop", tier="hot", salience=2)
    ck.panel("episodes")
    ck.panel("health")
    assert len(calls) == 2, len(calls)
    with Store(src.anneal.episodic_db) as store:
        store.record("episode B", "decision")
    ck.panel("health")
    ck.panel("graph")
    assert len(calls) == 3, len(calls)


def test_a_state_line_edit_rebuilds(counted) -> None:
    root, _s, ck, _clock, calls = counted
    ck.panel("health")
    (root / ".levain" / "context.json").write_text(json.dumps({
        "state": "changed", "state_set_at": datetime.now(timezone.utc).isoformat(), "state_source": "cli"}))
    ck.panel("health")
    assert len(calls) == 2


def test_a_write_read_is_never_served_from_the_cache(counted) -> None:
    _r, _s, ck, clock, calls = counted
    ck.panel("health")
    from levain.cockpit.engine import ReadContext
    ctx = ReadContext(clock(), fresh=True)
    providers._view(_s, ctx)
    assert len(calls) == 2


def test_the_cache_is_bounded_in_age(counted, monkeypatch) -> None:
    _r, _s, ck, clock, calls = counted
    ck.panel("health")
    clock.t += timedelta(seconds=providers.VIEW_CACHE_MAX_AGE_S + 1)
    monkeypatch.setattr(providers.time, "monotonic", lambda real=providers.time.monotonic: real() + providers.VIEW_CACHE_MAX_AGE_S + 1)
    ck.panel("health")
    assert len(calls) == 2
