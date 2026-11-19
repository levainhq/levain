"""The in-thread snapshot host: its plans, its derive cycle over a real writer and reader, discovery and the entity, a
derive that raises, a store that cannot be opened, and the threads' lifecycle. The cockpit and the store are real; the
providers are small recording ones, so a test can say which thread ran a derive."""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import pytest

from levain.cockpit.derive import File
from levain.cockpit.engine import Cockpit, Read, RowIn
from levain.cockpit.snapshot_host import (
    DERIVE_THREAD_PREFIX, DISCOVER_ID, ENTITY_ID, HEAVY_PANELS, PSEUDO_PREFIX, ServerProbe, SnapshotHost,
)
from levain.cockpit.snapshots import DB_FILENAME

from tests.test_cockpit_k1 import _simple

NONE_CRED = {"class": "none", "device_class": None}


def _row(n: int) -> RowIn:
    return RowIn(f"r{n}", f"row {n}", {"at": "2026-10-01T00:00:00+00:00"}, stored={"id": f"r{n}"})


class Calls:
    """The provider calls made for each panel id, and which thread asked the cockpit to derive. The engine runs a
    provider on a pool thread of its own on behalf of whoever asked, so the asking thread is what says who derived."""

    def __init__(self) -> None:
        self.by_panel: dict[str, list[str]] = {}
        self.askers: dict[str, list[str]] = {}
        self._lock = threading.Lock()

    def note(self, pid: str) -> None:
        with self._lock:
            self.by_panel.setdefault(pid, []).append(threading.current_thread().name)

    def spy_on(self, ck: Cockpit) -> None:
        real = ck.derive_panel

        def derive_panel(pid):
            with self._lock:
                self.askers.setdefault(pid, []).append(threading.current_thread().name)
            return real(pid)

        ck.derive_panel = derive_panel      # type: ignore[method-assign]

    def asking_threads(self) -> set[str]:
        with self._lock:
            return {name for names in self.askers.values() for name in names}

    def count(self, pid: str) -> int:
        with self._lock:
            return len(self.by_panel.get(pid, []))


def _recorded(pid: str, calls: Calls, **kw: Any):
    def read(ctx):
        calls.note(pid)
        return Read(rows=(_row(1),))

    return _simple(pid, read=read, **kw)


def _until(pred: Callable[[], Any], timeout: float = 10.0) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        got = pred()
        if got:
            return got
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.02)


@pytest.fixture
def hosts():
    made: list[SnapshotHost] = []
    yield made
    for h in made:
        h.stop(timeout=10)


@pytest.fixture
def make_host(tmp_path: Path, hosts: list[SnapshotHost]):
    def make(ck: Cockpit, **kw: Any) -> SnapshotHost:
        root = tmp_path / f"host{len(hosts)}"      # one store per host: a second one on a directory is refused
        root.mkdir()
        host = SnapshotHost(ck, root, **kw)
        hosts.append(host)
        return host

    return make


def _plan_map(host: SnapshotHost) -> dict[str, Any]:
    return {p.id: p for p in host._plans()}


def _derive_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(DERIVE_THREAD_PREFIX)]


# ---------------------------------------------------------------------------------------------- (a) the plans


class TestPlans:
    def test_a_registered_panel_has_a_plan_from_its_spec(self, make_host, tmp_path: Path) -> None:
        ck = Cockpit()
        ck.register(_simple("plain"))
        ck.register(_simple("timed", refresh_every_s=5.0, stale_after_s=10.0))
        ck.register(_simple("watched", inputs=(File(str(tmp_path / "f")),)))
        ck.register(_simple("slow", timeout_s=7.0))
        plans = _plan_map(make_host(ck))
        plain, timed, watched, slow = (plans[k] for k in ("plain", "timed", "watched", "slow"))
        assert (plain.inputs, plain.every_s, plain.on_token, plain.heavy) == ((), None, True, False)
        assert (timed.every_s, timed.on_token) == (5.0, False)
        assert watched.inputs == (File(str(tmp_path / "f")),) and watched.on_token is False
        assert slow.timeout_s == 7.0 and plain.timeout_s == ck.spec("plain").timeout_s

    def test_the_heavy_panels_are_named_by_a_constant_ids_only(self, make_host) -> None:
        assert HEAVY_PANELS == {"board", "topology", "sky"}
        ck = Cockpit()
        for pid in ("board", "topology", "other"):
            ck.register(_simple(pid))
        plans = _plan_map(make_host(ck))
        assert [pid for pid in ("board", "topology", "other") if plans[pid].heavy] == ["board", "topology"]
        # a host is told which are heavy, so a cockpit that names others is not stuck with the default
        ck2 = Cockpit()
        ck2.register(_simple("other"))
        assert _plan_map(make_host(ck2, heavy={"other"}))["other"].heavy is True

    def test_serve_composed_panels_and_underscore_ids_have_no_plan(self, make_host) -> None:
        ck = Cockpit()
        for pid in ("edits", "mine", "_sneaky", "kept"):
            ck.register(_simple(pid))
        ck.mark_serve_composed(["mine"])
        plans = _plan_map(make_host(ck))
        assert "edits" not in plans and "mine" not in plans and "_sneaky" not in plans
        assert "kept" in plans

    def test_the_two_pseudo_plans_follow_the_global_token(self, make_host) -> None:
        assert DISCOVER_ID.startswith(PSEUDO_PREFIX) and ENTITY_ID.startswith(PSEUDO_PREFIX)
        assert DISCOVER_ID != ENTITY_ID
        plans = _plan_map(make_host(Cockpit()))
        assert set(plans) == {DISCOVER_ID, ENTITY_ID}
        assert plans[DISCOVER_ID].on_token and plans[ENTITY_ID].on_token


# ---------------------------------------------------------------------------------------------- (b) the cycle


class TestCycle:
    def test_one_cycle_derives_every_plan_into_rows_a_reader_can_read(self, make_host) -> None:
        calls = Calls()
        ck = Cockpit()
        ck.register(_recorded("a", calls))
        ck.register(_recorded("b", calls))
        calls.spy_on(ck)
        host = make_host(ck)
        assert host.reader.read("a") is None
        ran = host.run_once()
        assert sorted(ran) == sorted(["a", "b", DISCOVER_ID, ENTITY_ID])
        for pid in ("a", "b", DISCOVER_ID, ENTITY_ID):
            snap = host.reader.read(pid)
            assert snap is not None and snap.status == "ok" and snap.gen == 1
        body = host.reader.decoded(host.reader.read("a"))
        assert body["v"] == 1 and [r["id"] for r in body["rows"]] == ["r1"]

    def test_the_started_host_derives_on_its_own_threads_and_a_request_runs_no_provider(self, make_host) -> None:
        calls = Calls()
        ck = Cockpit()
        calls.spy_on(ck)
        for pid in ("a", "b", "c"):
            ck.register(_recorded(pid, calls))
        host = make_host(ck)
        host.attach()
        host.start()
        _until(lambda: all(host.reader.read(p) is not None for p in ("a", "b", "c", DISCOVER_ID, ENTITY_ID)))
        derived_on = calls.asking_threads()
        assert derived_on and all(n.startswith(DERIVE_THREAD_PREFIX) for n in derived_on), derived_on
        seen = {pid: calls.count(pid) for pid in ("a", "b", "c")}

        def request(i: int) -> None:
            assert ck.manifest(NONE_CRED)["panels"]
            for pid in ("a", "b", "c"):
                assert ck.panel(pid, credential_class="none")["rows"]

        with ThreadPoolExecutor(6, thread_name_prefix="req") as pool:
            for f in [pool.submit(request, i) for i in range(12)]:
                f.result(timeout=30)
        assert {pid: calls.count(pid) for pid in ("a", "b", "c")} == seen
        assert not any(n.startswith("req") for n in calls.asking_threads())

    def test_an_unchanged_derive_keeps_its_generation(self, make_host) -> None:
        calls = Calls()
        ck = Cockpit()
        ck.register(_recorded("a", calls))
        host = make_host(ck)
        t0 = time.time()
        host.run_once(t0)
        host.run_once(t0 + 31)      # past the backstop: derived again, the same body
        assert calls.count("a") == 2 and host.reader.read("a").gen == 1


# ---------------------------------------------------------------------------------------------- (c) discovery


class TestDiscovery:
    def _rig(self, make_host, retire: bool = False):
        calls = Calls()
        ck = Cockpit()
        calls.spy_on(ck)
        ck.register(_recorded("fixed", calls))
        answers: list[list[str]] = [[]]
        made: dict[str, Any] = {}

        def discoverer(ctx):
            out = []
            for pid in answers[0]:
                # a re-registered panel needs a new read callable (the engine matches a retired panel's state by it)
                made.setdefault(pid, _recorded(pid, calls))
                out.append(made[pid])
            return out

        ck.discover(discoverer, retire=retire)
        token = [0]
        host = make_host(ck, token_fn=lambda: token[0])
        return ck, host, calls, answers, token

    def test_a_panel_a_discoverer_adds_is_registered_planned_and_derived_by_the_next_cycles(self, make_host) -> None:
        ck, host, calls, answers, token = self._rig(make_host)
        t = time.time()
        host.run_once(t)
        assert "found" not in ck.panel_ids and "found" not in _plan_map(host)
        first = host.reader.read(DISCOVER_ID)
        assert host.reader.decoded(first)["panels"] == []
        answers[0] = ["found"]
        token[0] += 1       # the watch token moved: discovery is due again
        host.run_once(t + 1)
        assert "found" in ck.panel_ids and "found" in _plan_map(host)
        host.run_once(t + 2)
        row = host.reader.read("found")
        assert row is not None and row.status == "ok"
        moved = host.reader.read(DISCOVER_ID)
        assert moved.gen == first.gen + 1 and host.reader.decoded(moved)["panels"] == ["found"]
        assert calls.asking_threads() == {threading.current_thread().name}     # run_once derives on the calling thread

    def test_discovery_with_an_unchanged_answer_does_not_move_its_row(self, make_host) -> None:
        _ck, host, _calls, answers, token = self._rig(make_host)
        answers[0] = ["found"]
        t = time.time()
        host.run_once(t)
        gen = host.reader.read(DISCOVER_ID).gen
        for i in range(1, 4):
            token[0] += 1
            host.run_once(t + i)
        assert host.reader.read(DISCOVER_ID).gen == gen

    def test_a_retired_discovered_panel_loses_its_plan_and_nothing_is_published_for_it_after(self, make_host) -> None:
        ck, host, calls, answers, token = self._rig(make_host, retire=True)
        answers[0] = ["found"]
        t = time.time()
        host.run_once(t)
        host.run_once(t + 1)
        assert host.reader.read("found") is not None and "found" in _plan_map(host)
        answers[0] = []
        token[0] += 1
        host.run_once(t + 2)        # discovery drops the panel and the plan with it
        assert "found" not in ck.panel_ids and "found" not in _plan_map(host)
        assert host.reader.read("found") is None        # the row left with the panel
        seen = calls.count("found")
        for i in range(3, 8):
            token[0] += 1
            host.run_once(t + i)
        assert host.reader.read("found") is None and calls.count("found") == seen
        assert host.loop.pending("found") is False

    def test_a_discovered_panel_that_vanishes_and_returns_reads_not_derived_until_its_first_derive(self, make_host) -> None:
        calls = Calls()
        ck = Cockpit()
        answer: list[Any] = []
        ck.discover(lambda ctx: list(answer), retire=True)
        token = [0]
        host = make_host(ck, token_fn=lambda: token[0])
        host.attach()
        answer[:] = [_recorded("found", calls)]
        t = time.time()
        host.run_once(t)
        host.run_once(t + 1)
        assert host.reader.read("found") is not None
        answer[:] = []
        token[0] += 1
        host.run_once(t + 2)
        assert "found" not in ck.panel_ids
        answer[:] = [_recorded("found", calls)]     # a new read callable: the engine matches a retired panel's state by it
        token[0] += 1
        host.run_once(t + 3)                        # re-registered and planned; its first derive is the next poll's
        assert "found" in ck.panel_ids and "found" in _plan_map(host)
        assert host.reader.read("found") is None    # the previous life's row is not served for the new registration
        assert ck.freshness()["found"] == {"as_of": None, "status": "unread", "stale": True}
        host.run_once(t + 4)
        assert host.reader.read("found") is not None and ck.freshness()["found"]["status"] == "ok"

    def test_a_panel_unregistered_under_a_running_plan_is_retired_not_published_as_an_error(self, make_host, monkeypatch) -> None:
        ck = Cockpit()
        ck.register(_simple("gone", rows=[_row(1)]))
        host = make_host(ck)
        host.run_once()
        assert host.reader.read("gone") is not None
        ck.unregister("gone")           # the plan was made while it was registered
        retired: list[str] = []
        real = host.loop.retire
        monkeypatch.setattr(host.loop, "retire", lambda pid: (retired.append(pid), real(pid)))
        result = host._derive("gone")
        assert retired == ["gone"] and result.status == "error"
        assert host.reader.read("gone") is None         # the row left with the panel, and no error row replaced it


# ---------------------------------------------------------------------------------------------- (d) the entity


class TestEntity:
    def test_the_attached_manifest_carries_the_entity_the_cycle_read_and_the_row_moves_with_it(self, make_host) -> None:
        name = {"n": "first"}
        ck = Cockpit(entity=lambda ctx: {"name": name["n"]})
        token = [0]
        host = make_host(ck, token_fn=lambda: token[0])
        host.attach()
        assert ck.manifest(NONE_CRED)["entity"]["name"] is None      # nothing read yet: the empty entity, every field present
        t = time.time()
        host.run_once(t)
        assert ck.manifest(NONE_CRED)["entity"]["name"] == "first"
        gen = host.reader.read(ENTITY_ID).gen
        token[0] += 1
        host.run_once(t + 1)
        assert host.reader.read(ENTITY_ID).gen == gen       # read again, the same entity: no move
        name["n"] = "second"
        token[0] += 1
        host.run_once(t + 2)
        assert ck.manifest(NONE_CRED)["entity"]["name"] == "second"
        assert host.reader.read(ENTITY_ID).gen == gen + 1

    def test_a_failing_entity_read_keeps_its_error_in_the_manifest(self, make_host) -> None:
        def entity(ctx):
            raise RuntimeError("entity is down")

        ck = Cockpit(entity=entity)
        host = make_host(ck)
        host.attach()
        host.run_once()
        m = ck.manifest(NONE_CRED)
        assert any(e.get("source") == "entity" for e in m["errors"])


# ---------------------------------------------------------------------------------------------- (e) a derive that raises


class TestRaising:
    def test_a_provider_that_fails_is_an_error_row_with_its_message_and_the_rest_still_derive(self, make_host) -> None:
        def read(ctx):
            raise RuntimeError("provider is down")

        ck = Cockpit()
        ck.register(_simple("bad", read=read))
        ck.register(_simple("good", rows=[_row(1)]))
        host = make_host(ck)
        host.run_once()
        bad = host.reader.read("bad")
        assert bad.status == "error" and "provider is down" in (bad.error or "")
        assert host.reader.read("good").status == "ok"

    def test_a_derive_that_raises_is_published_as_an_error_row_and_the_loop_lives(self, make_host, monkeypatch) -> None:
        ck = Cockpit()
        ck.register(_simple("flaky", rows=[_row(1)]))
        ck.register(_simple("good", rows=[_row(1)]))
        real = ck.derive_panel

        def derive_panel(pid):
            if pid == "flaky":
                raise RuntimeError("the engine broke")
            return real(pid)

        monkeypatch.setattr(ck, "derive_panel", derive_panel)
        host = make_host(ck)
        t = time.time()
        host.run_once(t)
        flaky = host.reader.read("flaky")
        assert flaky.status == "error" and "the engine broke" in flaky.error
        assert host.reader.read("good").status == "ok"
        monkeypatch.setattr(ck, "derive_panel", real)
        host.loop.dirty(["flaky"], 1)
        host.run_once(t + 1)
        assert host.reader.read("flaky").status == "ok"


# ---------------------------------------------------------------------------------------------- (f) try_open


class TestTryOpen:
    def test_a_free_directory_opens_a_host(self, tmp_path: Path) -> None:
        host, reason = SnapshotHost.try_open(Cockpit(), tmp_path)
        try:
            assert host is not None and reason == ""
            assert (tmp_path / DB_FILENAME).exists()
        finally:
            host.stop()

    def test_a_second_host_on_one_directory_is_refused_with_a_reason(self, tmp_path: Path) -> None:
        first, _ = SnapshotHost.try_open(Cockpit(), tmp_path)
        try:
            second, reason = SnapshotHost.try_open(Cockpit(), tmp_path)
            assert second is None and reason and "\n" not in reason
            assert "lock" in reason.lower()
        finally:
            first.stop()
        third, again = SnapshotHost.try_open(Cockpit(), tmp_path)        # the first gave the lock back
        try:
            assert third is not None and again == ""
        finally:
            third.stop()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root writes into a read-only directory")
    def test_a_read_only_directory_is_refused_with_a_reason(self, tmp_path: Path) -> None:
        ro = tmp_path / "ro"
        ro.mkdir()
        ro.chmod(0o555)
        try:
            host, reason = SnapshotHost.try_open(Cockpit(), ro)
        finally:
            ro.chmod(0o755)
        assert host is None and reason and "\n" not in reason

    def test_a_missing_directory_is_refused_with_a_reason(self, tmp_path: Path) -> None:
        host, reason = SnapshotHost.try_open(Cockpit(), tmp_path / "nope")
        assert host is None and reason

    def test_a_database_that_is_not_a_snapshot_store_is_refused(self, tmp_path: Path) -> None:
        db = sqlite3.connect(tmp_path / DB_FILENAME)
        db.execute("CREATE TABLE unrelated (x)")
        db.commit()
        db.close()
        host, reason = SnapshotHost.try_open(Cockpit(), tmp_path)
        assert host is None and "contract" in reason.lower()

    def test_a_cockpit_already_attached_is_refused_and_leaves_the_store_free(self, tmp_path: Path) -> None:
        ck = Cockpit()
        (tmp_path / "one").mkdir()
        first, _ = SnapshotHost.try_open(ck, tmp_path / "one")
        try:
            first.attach()
            host, reason = SnapshotHost.try_open(ck, tmp_path)
            assert host is None and "attached" in reason
            free, _ = SnapshotHost.try_open(Cockpit(), tmp_path)        # nothing was left holding the writer lock
            assert free is not None
            free.stop()
        finally:
            first.stop()


# ---------------------------------------------------------------------------------------------- (g) lifecycle


class TestLifecycle:
    def test_the_threads_are_daemons_named_for_the_host_and_none_is_left_after_stop(self, tmp_path: Path) -> None:
        assert _derive_threads() == []
        ck = Cockpit()
        ck.register(_simple("a"))
        host = SnapshotHost(ck, tmp_path)
        host.start()
        try:
            names = {t.name for t in _derive_threads()}
            assert f"{DERIVE_THREAD_PREFIX}-poll" in names and len(names) > 1
            assert all(t.daemon for t in _derive_threads())
        finally:
            assert host.stop(timeout=10) == []
        assert _derive_threads() == []

    def test_stop_twice_is_safe_and_a_stopped_or_started_host_does_not_start_again(self, tmp_path: Path) -> None:
        host = SnapshotHost(Cockpit(), tmp_path)
        host.start()
        with pytest.raises(RuntimeError):
            host.start()
        assert host.stop(timeout=10) == []
        assert host.stop(timeout=10) == []
        with pytest.raises(RuntimeError):
            host.start()
        assert _derive_threads() == []

    def test_a_host_never_started_stops_and_gives_the_writer_lock_back(self, tmp_path: Path) -> None:
        host = SnapshotHost(Cockpit(), tmp_path)
        assert host.stop() == [] and host.stop() == []
        again, reason = SnapshotHost.try_open(Cockpit(), tmp_path)
        assert again is not None, reason
        again.stop()

    def test_the_started_host_heartbeats_and_keeps_deriving_until_stopped(self, tmp_path: Path) -> None:
        ck = Cockpit()
        ck.register(_simple("a"))
        host = SnapshotHost(ck, tmp_path, poll_s=0.05)
        host.attach()
        host.start()
        try:
            _until(lambda: host.reader.meta().get("heartbeat_at"))
            first = host.reader.meta()["heartbeat_at"]
            _until(lambda: host.reader.meta().get("heartbeat_at") != first)
            assert ck.worker_health()["worker"] == "live"
        finally:
            host.stop(timeout=10)


class TestAttachedWords:
    def test_the_busy_list_the_cockpit_reports_names_panels_never_the_host_s_pseudo_plans(self, tmp_path: Path) -> None:
        entered, gate = threading.Event(), threading.Event()

        def discoverer(ctx: Any) -> list:
            entered.set()
            gate.wait(10)
            return []

        ck = Cockpit()
        ck.register(_simple("a"))
        ck.discover(discoverer)
        host = SnapshotHost(ck, tmp_path)
        host.attach()
        host.start()
        try:
            assert entered.wait(10)
            assert DISCOVER_ID in host.loop.deriving()              # the loop is busy with a pseudo-plan
            assert not any(p.startswith(PSEUDO_PREFIX) for p in ck.worker_health()["deriving"])
            assert not any(p.startswith(PSEUDO_PREFIX) for p in ck._deriving_ids())
        finally:
            gate.set()
            host.stop(timeout=10)

    def test_the_default_poll_is_the_cadence_the_engine_expects_of_a_host(self, tmp_path: Path, monkeypatch) -> None:
        from levain.cockpit import engine
        monkeypatch.setattr(engine, "HEARTBEAT_CADENCE_S", 0.25)
        host = SnapshotHost(Cockpit(), tmp_path)
        try:
            assert host.loop._poll_s == 0.25
        finally:
            host.stop()


# ---------------------------------------------------------------------------------------------- the stream probe


class _EngineProbe:
    def __init__(self) -> None:
        self.seq = 0
        self.snapshots = 0

    def change_seq(self) -> int:
        return self.seq

    def snapshot(self):
        self.snapshots += 1
        return {"a": "1"}, "v"


class TestServerProbe:
    def _probe(self, engine, token, now=None, bare=None):
        box = {"token": token, "now": 0.0}
        probe = ServerProbe(lambda: engine, lambda: box["token"], bare or (lambda: ({"bare": "1"}, None)),
                            slow_s=5.0, clock=lambda: box["now"])
        return probe, box

    def test_the_counter_moves_with_the_engine_counter(self) -> None:
        eng = _EngineProbe()
        probe, _box = self._probe(eng, (("p", "1"),))
        a = probe.change_seq()
        assert probe.change_seq() == a
        eng.seq += 1
        assert probe.change_seq() != a and isinstance(probe.change_seq(), int)

    def test_the_counter_moves_with_the_serve_watch_token_alone(self) -> None:
        eng = _EngineProbe()
        probe, box = self._probe(eng, (("p", "1"),))
        a = probe.change_seq()
        box["token"] = (("p", "2"),)
        assert probe.change_seq() != a

    def test_without_a_token_the_counter_moves_once_per_slow_interval(self) -> None:
        eng = _EngineProbe()
        probe, box = self._probe(eng, None)
        a = probe.change_seq()
        box["now"] = 4.0
        assert probe.change_seq() == a
        box["now"] = 5.5
        assert probe.change_seq() != a

    def test_snapshot_is_the_engine_probes_and_a_cockpit_with_no_probe_gets_the_bare_one(self) -> None:
        eng = _EngineProbe()
        probe, _ = self._probe(eng, None)
        assert probe.snapshot() == ({"a": "1"}, "v") and eng.snapshots == 1
        bare = ServerProbe(lambda: None, lambda: None, lambda: ({"bare": "1"}, None), slow_s=5.0)
        assert bare.snapshot() == ({"bare": "1"}, None) and isinstance(bare.change_seq(), int)


# ---------------------------------------------------------------------------------------------- a construction that fails


class TestConstructionFailure:
    def _tracked_readers(self, monkeypatch) -> "list[Any]":
        from levain.cockpit import snapshot_host as sh

        made: list[Any] = []

        class Tracked(sh.SnapshotReader):
            def __init__(self, *a: Any, **k: Any) -> None:
                super().__init__(*a, **k)
                self.was_closed = False
                made.append(self)

            def close(self) -> None:
                self.was_closed = True
                super().close()

        monkeypatch.setattr(sh, "SnapshotReader", Tracked)
        return made

    def test_a_derive_loop_that_cannot_be_built_leaves_no_open_reader_and_frees_the_writer_lock(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        from levain.cockpit import snapshot_host as sh

        made = self._tracked_readers(monkeypatch)

        def refuse(*a: Any, **k: Any) -> None:
            raise RuntimeError("no loop")

        monkeypatch.setattr(sh, "DeriveLoop", refuse)
        with pytest.raises(RuntimeError, match="no loop"):
            SnapshotHost(Cockpit(), tmp_path)
        assert len(made) == 1 and made[0].was_closed
        monkeypatch.undo()
        again, reason = SnapshotHost.try_open(Cockpit(), tmp_path)
        assert again is not None, reason
        again.stop()

    def test_plans_that_cannot_be_made_leave_no_open_reader(self, tmp_path: Path, monkeypatch) -> None:
        made = self._tracked_readers(monkeypatch)

        def refuse(self: SnapshotHost) -> list:
            raise RuntimeError("no plans")

        monkeypatch.setattr(SnapshotHost, "_plans", refuse)
        with pytest.raises(RuntimeError, match="no plans"):
            SnapshotHost(Cockpit(), tmp_path)
        assert len(made) == 1 and made[0].was_closed


class TestServerProbeTokenFault:
    def test_a_watch_token_that_raises_does_not_stop_the_counter_following_the_engine(self) -> None:
        eng = _EngineProbe()

        def token() -> Any:
            raise OSError("the store is gone")

        probe = ServerProbe(lambda: eng, token, lambda: ({"bare": "1"}, None), slow_s=5.0, clock=lambda: 0.0)
        a = probe.change_seq()
        assert probe.change_seq() == a
        eng.seq += 1
        assert probe.change_seq() != a                  # publish_seq moved: the stream still sees it at once

    def test_a_token_that_raises_is_read_as_no_token_so_the_counter_also_moves_once_per_slow_interval(self) -> None:
        eng = _EngineProbe()
        box = {"now": 0.0}

        def token() -> Any:
            raise OSError("the store is gone")

        probe = ServerProbe(lambda: eng, token, lambda: ({"bare": "1"}, None), slow_s=5.0, clock=lambda: box["now"])
        a = probe.change_seq()
        box["now"] = 5.5
        assert probe.change_seq() != a
