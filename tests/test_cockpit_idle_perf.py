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


def test_a_config_doc_edit_or_removal_rebuilds(counted) -> None:
    root, _s, ck, _clock, calls = counted
    origin = root / "seed" / "origin.md"
    assert origin.exists()
    ck.panel("health")
    origin.write_text(origin.read_text() + "\nmore words in place\n")
    ck.panel("health")
    assert len(calls) == 2
    import shutil
    shutil.rmtree(root / "seed")
    ck.panel("health")
    assert len(calls) == 3


# ---- L3 round 1 fixes (K3, K5, K6, K7, K8) ----------------------------------------------------------------

def _gate_build(monkeypatch, src, *, fail_first: bool = False):
    """Make ``source.build`` block on an event so concurrent readers pile up behind one miss."""
    import threading
    entered, release, calls = threading.Event(), threading.Event(), []
    real = type(src).build

    def build(self, **kw):
        calls.append(1)
        entered.set()
        release.wait(10)
        if fail_first and len(calls) == 1:
            raise RuntimeError("first build fails")
        return real(self, **kw)

    monkeypatch.setattr(type(src), "build", build)
    return entered, release, calls


def _readers(src, n: int):
    import threading
    from levain.cockpit.engine import ReadContext
    out: list = []

    def go():
        try:
            out.append(providers._view(src, ReadContext(datetime.now(timezone.utc))))
        except Exception as e:                      # noqa: BLE001
            out.append(e)

    ts = [threading.Thread(target=go) for _ in range(n)]
    for t in ts:
        t.start()
    return ts, out


def test_concurrent_misses_share_one_build(tmp_path: Path, monkeypatch) -> None:
    """K3: a miss is single-flight: readers that arrive while a build runs wait for it and share its view."""
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    entered, release, calls = _gate_build(monkeypatch, src)
    ts, out = _readers(src, 6)
    entered.wait(5)
    time.sleep(0.3)                                 # the other five are now at the miss too
    release.set()
    for t in ts:
        t.join(10)
    assert len(calls) == 1, len(calls)
    assert len(out) == 6 and all(v is out[0] for v in out)


def test_a_failed_build_is_not_cached_and_the_waiters_see_its_exception(tmp_path: Path, monkeypatch) -> None:
    """K3/K9c: a failing build raises to its caller, stores nothing, and the readers waiting on it get the same exception
    (no chain of serial rebuilds); the next read builds afresh."""
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    entered, release, calls = _gate_build(monkeypatch, src, fail_first=True)
    ts, out = _readers(src, 3)
    entered.wait(5)
    time.sleep(0.3)
    release.set()
    for t in ts:
        t.join(10)
    assert len(out) == 3 and all(isinstance(o, RuntimeError) for o in out), out
    assert len(calls) == 1, len(calls)
    from levain.cockpit.engine import ReadContext
    view = providers._view(src, ReadContext(datetime.now(timezone.utc)))      # nothing was cached: this builds
    assert len(calls) == 2 and not isinstance(view, Exception)


def test_a_slow_build_with_several_concurrent_readers_is_one_build(tmp_path: Path, monkeypatch) -> None:
    """C10: only the flight's builder builds. The wait slice (VIEW_FLIGHT_WAIT_S) expires several times while the one build is
    slow, and no waiter builds for itself: exactly one build call, every reader gets that view."""
    import threading
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    monkeypatch.setattr(providers, "VIEW_FLIGHT_WAIT_S", 0.1, raising=False)
    real = type(src).build
    entered, calls = threading.Event(), []

    def build(self, **kw):
        calls.append(1)
        entered.set()
        time.sleep(1.5)                              # several wait slices long
        return real(self, **kw)

    monkeypatch.setattr(type(src), "build", build)
    ts, out = _readers(src, 5)
    entered.wait(5)
    for t in ts:
        t.join(15)
    assert len(calls) == 1, len(calls)
    assert len(out) == 5 and all(v is out[0] for v in out) and not isinstance(out[0], Exception), out


def test_a_waiter_on_a_hung_build_raises_the_timeout_at_the_deadline_and_does_not_build(tmp_path: Path, monkeypatch) -> None:
    """C10: a builder that never returns does not hang its waiters, and they do not build: at the panel deadline they raise
    TimeoutError (the fault the page already handles); a new flight starts only after the old one finished."""
    import threading
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    monkeypatch.setattr(providers, "VIEW_FLIGHT_WAIT_S", 0.1, raising=False)
    monkeypatch.setattr(providers, "PANEL_READ_DEADLINE_S", 0.6, raising=False)
    real = type(src).build
    hang, entered, calls = threading.Event(), threading.Event(), []

    def build(self, **kw):
        calls.append(1)
        if len(calls) == 1:
            entered.set()
            hang.wait(20)
        return real(self, **kw)

    monkeypatch.setattr(type(src), "build", build)
    ts, out = _readers(src, 1)
    entered.wait(5)
    t0 = time.monotonic()
    ts2, out2 = _readers(src, 3)
    for t in ts2:
        t.join(8)
    waited = time.monotonic() - t0
    assert len(out2) == 3 and all(isinstance(o, TimeoutError) for o in out2), out2
    assert 0.5 <= waited < 5, waited
    assert len(calls) == 1, "a waiter built for itself"
    hang.set()
    ts[0].join(10)
    from levain.cockpit.engine import ReadContext
    view = providers._view(src, ReadContext(datetime.now(timezone.utc)))      # the old flight finished: a new one may start
    assert not isinstance(view, Exception)


# ---- C12: a hung build's flight is replaced once it is older than the panel read budget (one rescue per budget window) ----

def _hung_builds(monkeypatch, src, deadline: float = 0.4):
    """Build call 1 hangs on ``hangs[0]``, call 2 on ``hangs[1]``; later calls build at once. ``entered[i]`` is set as call i+1 starts."""
    import threading
    monkeypatch.setattr(providers, "VIEW_FLIGHT_WAIT_S", 0.1, raising=False)
    monkeypatch.setattr(providers, "PANEL_READ_DEADLINE_S", deadline, raising=False)
    real = type(src).build
    entered, hangs, calls = [threading.Event(), threading.Event()], [threading.Event(), threading.Event()], []

    def build(self, **kw):
        calls.append(1)
        n = len(calls)
        if n <= 2:
            entered[n - 1].set()
            hangs[n - 1].wait(20)
        return real(self, **kw)

    monkeypatch.setattr(type(src), "build", build)
    return entered, hangs, calls


def test_a_read_after_a_hung_build_outlives_the_deadline_builds_and_the_late_build_does_not_overwrite_its_view(tmp_path: Path, monkeypatch) -> None:
    """C12: a builder that hangs past the panel budget no longer blocks the source: the next read replaces its flight and builds.
    When the hung build returns at last, its caller gets its own view, but the cache keeps the replacement's."""
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    entered, hangs, calls = _hung_builds(monkeypatch, src)
    hangs[1].set()                                  # only the first build hangs
    ts, out = _readers(src, 1)
    try:
        assert entered[0].wait(5)
        time.sleep(0.5)                             # the hung flight is now older than the budget
        ts2, out2 = _readers(src, 1)
        ts2[0].join(5)
        assert len(out2) == 1 and not isinstance(out2[0], Exception), out2
        assert len(calls) == 2, len(calls)
        key = id(src)
        assert providers._view_cache[key][3] is out2[0]
    finally:
        hangs[0].set()
        ts[0].join(10)
    assert len(out) == 1 and not isinstance(out[0], Exception) and out[0] is not out2[0], out
    assert providers._view_cache[key][3] is out2[0], "the abandoned build overwrote the newer view"
    from levain.cockpit.engine import ReadContext
    assert providers._view(src, ReadContext(datetime.now(timezone.utc))) is out2[0] and len(calls) == 2


def test_an_abandoned_build_returning_late_leaves_the_replacement_flight_and_publishes_nothing(tmp_path: Path, monkeypatch) -> None:
    """C12: the abandoned builder returns while the replacement still builds: the replacement stays registered (a reader arriving
    then joins it, no third build) and the cache never holds the abandoned view."""
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    entered, hangs, calls = _hung_builds(monkeypatch, src, deadline=0.6)
    key = id(src)
    ts, out = _readers(src, 1)
    ts2: list = []
    ts3: list = []
    try:
        assert entered[0].wait(5)
        time.sleep(0.7)
        ts2, out2 = _readers(src, 1)
        assert entered[1].wait(2), "no reader replaced the hung flight"
        replacement = providers._view_flights.get(key)
        assert replacement is not None
        hangs[0].set()
        ts[0].join(5)
        assert not isinstance(out[0], Exception), out
        assert providers._view_flights.get(key) is replacement, "the abandoned build removed the replacement flight"
        hit = providers._view_cache.get(key)
        assert hit is None or hit[3] is not out[0], "the abandoned build published its view"
        ts3, out3 = _readers(src, 1)                # arrives while the replacement builds: it joins
        time.sleep(0.2)
        assert len(calls) == 2, len(calls)
        hangs[1].set()
        for t in ts2 + ts3:
            t.join(5)
    finally:
        for h in hangs:
            h.set()
        for t in ts + ts2 + ts3:
            t.join(10)
    assert not isinstance(out2[0], Exception) and out3 == [out2[0]], (out2, out3)
    assert providers._view_cache[key][3] is out2[0] and key not in providers._view_flights
    assert len(calls) == 2, len(calls)


def test_several_readers_arriving_after_a_hung_build_outlived_the_deadline_start_one_replacement(tmp_path: Path, monkeypatch) -> None:
    """C12 bound: at most one build per budget window per source. Readers that all find the stale flight together replace it once;
    the rest wait on the replacement and share its view (the C10 property, kept)."""
    import time
    _root, src = _install(tmp_path)
    _wrap(src)
    entered, hangs, calls = _hung_builds(monkeypatch, src, deadline=0.6)
    ts, out = _readers(src, 1)
    ts2: list = []
    try:
        assert entered[0].wait(5)
        time.sleep(0.7)
        ts2, out2 = _readers(src, 5)
        assert entered[1].wait(2), "no reader replaced the hung flight"
        time.sleep(0.2)                             # the other four are at the replacement now
        hangs[1].set()
        for t in ts2:
            t.join(5)
    finally:
        for h in hangs:
            h.set()
        for t in ts + ts2:
            t.join(10)
    assert len(calls) == 2, len(calls)
    assert len(out2) == 5 and not isinstance(out2[0], Exception) and all(v is out2[0] for v in out2), out2


def test_the_flight_wait_is_a_quarter_of_the_panel_read_budget() -> None:
    """K10b: the wait is an expression of the budget constant, so it moves with it and stays inside it."""
    from levain.cockpit.engine import PANEL_READ_DEADLINE_S
    assert providers.VIEW_FLIGHT_WAIT_S <= PANEL_READ_DEADLINE_S / 4


def test_the_signature_is_retaken_when_a_reader_looks_again(tmp_path: Path, monkeypatch) -> None:
    """K9d: a reader whose flight saw another store state loops, and the loop takes a fresh signature."""
    import time
    root, src = _install(tmp_path)
    _wrap(src)
    taken = []
    real_sig = providers._store_signature
    monkeypatch.setattr(providers, "_store_signature", lambda source: (taken.append(1), real_sig(source))[1])
    entered, release, calls = _gate_build(monkeypatch, src)
    spores = root / ".levain" / "memory.spores.json"
    ts, _o = _readers(src, 1)
    entered.wait(5)
    SporeStore(spores).add(type="task", text="one", tier="hot", salience=2)
    ts2, _o2 = _readers(src, 1)                     # its signature differs from the flight's: it waits, then looks again
    time.sleep(0.3)
    n_before = len(taken)
    SporeStore(spores).add(type="task", text="two", tier="hot", salience=2)
    release.set()
    for t in ts + ts2:
        t.join(10)
    assert len(taken) > n_before, (len(taken), n_before)


def test_a_permission_change_on_the_store_is_seen(counted) -> None:
    """K5: chmod moves ctime and mode but not inode, mtime or size."""
    import os
    root, src, ck, _clock, calls = counted
    ck.panel("health")
    db = src.anneal.episodic_db
    os.chmod(db, 0o000)
    try:
        try:
            ck.panel("health")
        except Exception:                           # noqa: BLE001
            pass
    finally:
        os.chmod(db, 0o600)
    assert len(calls) == 2, len(calls)


def test_the_shared_memory_file_is_not_part_of_the_signature(counted) -> None:
    """K6: -shm is touched by readers, so it must not invalidate the view; db and -wal move on writes."""
    root, src, ck, _clock, calls = counted
    ck.panel("health")
    shm = Path(f"{src.anneal.episodic_db}-shm")
    shm.write_bytes(b"x" * 64)
    ck.panel("health")
    shm.write_bytes(b"y" * 128)
    ck.panel("graph")
    assert len(calls) == 1, len(calls)


def test_a_signature_that_cannot_be_taken_means_no_caching_not_an_exception(tmp_path: Path, monkeypatch) -> None:
    """K7: a source missing an anneal path attribute builds normally every read."""
    from types import SimpleNamespace
    from levain.cockpit.engine import ReadContext
    built: list[int] = []
    anneal = SimpleNamespace(episodic_db=tmp_path / "m.db")            # no continuity_md, crystal_json, spores_json
    fake = SimpleNamespace(anneal=anneal, build=lambda **kw: built.append(1) or SimpleNamespace(errors={}, health=None))
    assert providers._store_signature(fake) is None
    for _ in range(2):
        providers._view(fake, ReadContext(datetime.now(timezone.utc)))
    assert len(built) == 2


def test_a_stat_that_raises_means_no_caching(counted, monkeypatch) -> None:
    root, src, ck, _clock, calls = counted
    monkeypatch.setattr(providers, "_stat_of", lambda p: (_ for _ in ()).throw(RuntimeError("stat blew up")))
    ck.panel("health")
    ck.panel("health")
    assert len(calls) == 2


def test_the_store_stamp_is_utc_whether_the_wrap_stamp_is_z_or_naive() -> None:
    """K8: anneal's wraps.wrapped_at is written as ...Z (UTC); a naive value is read as UTC, the same as the age
    oracle in dashboard.health_oracles, so the page and the oracle agree."""
    from types import SimpleNamespace
    h = SimpleNamespace(save_age_hours=1.0, last_wrap_at="2026-10-26T18:47:46Z")
    assert providers._save_stamp(h) == "2026-10-26T18:47:46+00:00"
    h = SimpleNamespace(save_age_hours=1.0, last_wrap_at="2026-10-26T18:47:46")
    assert providers._save_stamp(h) == "2026-10-26T18:47:46+00:00"
    h = SimpleNamespace(save_age_hours=None, last_wrap_at="2026-10-26T18:47:46Z")
    assert providers._save_stamp(h) is None


def test_a_hot_source_is_not_evicted_before_a_colder_one(tmp_path: Path) -> None:
    """The eviction drops the entry stored longest ago; re-storing a source (its store changed) moves it to the tail."""
    from types import SimpleNamespace
    from levain.cockpit.engine import ReadContext

    def fake(i: int):
        d = tmp_path / str(i)
        d.mkdir()
        files = {n: d / n for n in ("m.db", "c.md", "c.json", "s.json")}
        for f in files.values():
            f.write_text("x")
        built: list[int] = []
        anneal = SimpleNamespace(episodic_db=files["m.db"], continuity_md=files["c.md"], crystal_json=files["c.json"],
                                 spores_json=files["s.json"])
        return SimpleNamespace(anneal=anneal, built=built,
                               build=lambda **kw: built.append(1) or SimpleNamespace(errors={})), files["s.json"]

    srcs = [fake(i) for i in range(providers.VIEW_CACHE_SOURCES)]
    first = srcs[0][0]
    for s, _ in srcs:
        providers._view(s, ReadContext(datetime.now(timezone.utc)))
    srcs[0][1].write_text("changed in size")                   # the first source's store moves: it is rebuilt and re-stored
    providers._view(first, ReadContext(datetime.now(timezone.utc)))
    extra, _ = fake(99)
    providers._view(extra, ReadContext(datetime.now(timezone.utc)))      # one past the bound: the oldest-stored goes
    providers._view(first, ReadContext(datetime.now(timezone.utc)))
    assert len(first.built) == 2, "the just-rebuilt source was evicted by the store that rebuilt it"
