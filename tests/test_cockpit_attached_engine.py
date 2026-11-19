"""The attached mode of ``Cockpit``: panels read from the rows a derive host published, never from a provider.

Every table here is built from the engine's own types (a real ``Cockpit``, a real ``SnapshotWriter`` / ``SnapshotReader`` over a
file in ``tmp_path``, a real ``DeriveLoop`` driven with ``run_once`` in the test's own thread). The one assertion the rest
serves is the request-path one: a provider that records who called it is never called by a request, only by the derive side.
A bare ``Cockpit`` is the control: the same reads DO call its providers, which is what shows the recorder can see a request
call at all."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

import pytest

from levain.cockpit import engine
from levain.cockpit import snapshot_host as sh
from levain.cockpit.derive import DeriveLoop, DeriveResult, File, PanelPlan
from levain.cockpit.engine import Cockpit, ProviderSpec, ReadContext
from levain.cockpit.results import Read, RowIn
from levain.cockpit.snapshots import SnapshotReadError, SnapshotReader, SnapshotWriter, encode_body

T0 = datetime(2026, 11, 19, 15, 0, tzinfo=timezone.utc)
CRED = {"class": "none", "device_class": None}
PANELS = ("alpha", "beta")
THREADS = 8


class Clock:
    """One clock for the cockpit and the derive loop, so a row's ``derived_at`` and the reader's ``now`` agree."""

    def __init__(self, start: datetime = T0) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def epoch(self) -> float:
        return self.now.timestamp()

    def advance(self, seconds: float = 0.0, days: float = 0.0) -> None:
        self.now += timedelta(seconds=seconds, days=days)


class Recorder:
    """What the providers were called with. ``request_side`` is True for a read made under a request's authority (the
    manifest and panel routes build one); a derive-side read has none."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, bool]] = []       # (panel id, calling thread's name, made under an authority)
        self.rows: dict[str, list[str]] = {}
        self.failing: set[str] = set()                      # panels whose read raises
        self.discovered = 0
        self.entity_calls = 0
        self._mu = threading.Lock()

    def note(self, pid: str, ctx: ReadContext) -> None:
        with self._mu:
            self.calls.append((pid, threading.current_thread().name, ctx.authority is not None))

    def request_side(self) -> list[tuple[str, str, bool]]:
        return [c for c in self.calls if c[2]]

    def count(self, pid: str | None = None) -> int:
        return len([c for c in self.calls if pid is None or c[0] == pid])


def list_spec(pid: str, rec: Recorder, **kw: Any) -> ProviderSpec:
    def read(ctx: ReadContext) -> Read:
        rec.note(pid, ctx)
        if pid in rec.failing:
            raise RuntimeError("boom")
        names = rec.rows.get(pid, ["a", "b"])
        return Read(rows=tuple(RowIn(f"{pid}:{n}", n, {"at": "2026-11-19"}, stored={"id": f"{pid}:{n}"}) for n in names))

    return ProviderSpec(id=pid, kind="triage-list", title=pid, priority="feed", read=read, order="time.desc",
                        facets=frozenset({"at"}), version_fields=("id",), **kw)


class FakeVerbView:
    """The four things the engine asks a verb view for in a head and a manifest; ``rev`` is the revision hashed into etags."""
    install_class = "test"

    def __init__(self) -> None:
        self.rev = "r1"

    def revision(self, auth: Any) -> str:
        return self.rev

    def panel_actions(self, *a: Any, **k: Any) -> list:
        return []

    def verbs_map(self, *a: Any, **k: Any) -> dict:
        return {}

    def row_actions(self, *a: Any, **k: Any) -> list:
        return []


class Rig:
    """An attached cockpit over a real snapshot file, with the derive loop in the test's thread."""

    def __init__(self, tmp_path: Path, panels: tuple[str, ...] = PANELS, *, attach: bool = True,
                 specs: dict[str, ProviderSpec] | None = None, start: datetime = T0, worker: bool = True,
                 **attach_kw: Any) -> None:
        self.clock = Clock(start)
        self.rec = Recorder()
        self.ck = Cockpit(clock=self.clock)
        self.panels = list(panels)
        for pid in panels:
            self.ck.register((specs or {}).get(pid) or list_spec(pid, self.rec))
        self.path = tmp_path / "snapshots.db"
        # ``worker=False``: a serve that opened a file some earlier run made, with no worker of its own yet
        self.writer = SnapshotWriter(self.path, now=self.clock.epoch()) if worker else None
        self.reader = SnapshotReader(self.path)
        self.loop = self.make_loop(self.writer, self.panels) if worker else None
        if attach:
            self.ck.attach_snapshots(self.reader, **attach_kw)

    def make_loop(self, writer: SnapshotWriter, panels: list[str]) -> DeriveLoop:
        return DeriveLoop(writer, {pid: PanelPlan(pid, on_token=True) for pid in panels}, self.derive_fn,
                          clock=self.clock.epoch)

    def derive_fn(self, pid: str) -> DeriveResult:
        snap = self.ck.derive_panel(pid)
        status, body = sh.encode_snap(snap)
        return DeriveResult(status, body, error=snap.error)

    def derive(self, *ids: str, loop: DeriveLoop | None = None) -> list[str]:
        """Derive the named panels (all of them when none are named) on the calling thread."""
        use = loop or self.loop
        if ids:
            use.dirty(ids, 1)
        return use.run_once(self.clock.epoch())

    def spec(self, pid: str) -> ProviderSpec:
        return self.ck._specs[pid]

    def snap(self, pid: str) -> engine._Snap:
        return self.ck._snap_for(self.spec(pid), ReadContext(self.clock()))

    def close(self) -> None:
        self.reader.close()
        if self.writer is not None:
            self.writer.close()


@pytest.fixture()
def rig(tmp_path: Path):
    r = Rig(tmp_path)
    yield r
    r.close()


# --- ProviderSpec.inputs ---------------------------------------------------------------------------------------------
def test_provider_spec_inputs_default_empty_and_ride_through_replaced_and_with_verbs() -> None:
    rec = Recorder()
    plain = list_spec("p", rec)
    assert plain.inputs == ()
    declared = list_spec("p", rec, inputs=(File("/x/y"),))
    assert declared.inputs == (File("/x/y"),)
    assert declared.replaced(title="t").inputs == declared.inputs
    assert declared.with_verbs(("v",)).inputs == declared.inputs


# --- (a) a bare cockpit is today's ---------------------------------------------------------------------------------------
def test_a_bare_cockpit_runs_its_provider_on_the_request_path_and_has_no_probe(tmp_path: Path) -> None:
    r = Rig(tmp_path, attach=False)
    try:
        assert not r.ck.attached and r.ck.change_probe() is None
        r.ck.manifest(CRED)
        r.ck.panel("alpha")
        # the control for the attached tests: a request's read made the call, under the request's authority
        assert len(r.rec.request_side()) >= len(PANELS)
        assert r.ck.freshness()["alpha"]["status"] in ("ok", "empty")
    finally:
        r.close()


def test_a_bare_cockpit_start_runs_refreshers_and_stop_halts_them(tmp_path: Path) -> None:
    rec = Recorder()
    ck = Cockpit(clock=Clock())
    ck.register(list_spec("alpha", rec, refresh_every_s=60.0, stale_after_s=60.0))
    ck.start()
    try:
        assert ck.refresher("alpha") is not None and rec.count("alpha") == 1
    finally:
        assert ck.stop() == []


def test_derive_panel_serves_a_bare_cockpit_too_and_returns_the_snapshot_it_made(tmp_path: Path) -> None:
    rec = Recorder()
    ck = Cockpit(clock=Clock())
    ck.register(list_spec("alpha", rec))
    snap = ck.derive_panel("alpha")
    assert snap.status == "ok" and [r["id"] for r in snap.rows] == ["alpha:a", "alpha:b"]
    assert snap is ck._state["alpha"].snap                     # the same commit a refresher cycle makes
    assert rec.calls == [("alpha", "cockpit-read", False)]     # no authority: nothing a credential decides
    with pytest.raises(KeyError):
        ck.derive_panel("nope")


# --- (b) zero provider calls on a request thread ----------------------------------------------------------------------------
def test_attached_reads_on_request_threads_never_call_a_provider(rig: Rig) -> None:
    rig.derive()
    derived = rig.rec.count()
    assert derived == len(PANELS) and not rig.rec.request_side()
    errors: list[BaseException] = []

    def request() -> None:
        try:
            rig.ck.manifest(CRED)
            for pid in PANELS:
                rig.ck.panel(pid)
                rig.ck.panel(pid, profile="compact")
                rig.ck.panel(pid, row=f"{pid}:a")
                rig.ck.panel(pid, q="a")
            rig.ck.freshness()
        except BaseException as exc:  # noqa: BLE001 - reported below, from the main thread
            errors.append(exc)

    threads = [threading.Thread(target=request, name=f"request-{i}") for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert not errors, errors
    assert rig.rec.count() == derived and not rig.rec.request_side()


def test_attached_panel_and_manifest_carry_the_rows_the_derive_read(rig: Rig) -> None:
    rig.derive()
    got = rig.ck.panel("alpha")
    assert [r["id"] for r in got["rows"]] == ["alpha:a", "alpha:b"] and got["status"] == "ok"
    assert set(rig.ck.manifest(CRED)["panels"]) == set(PANELS)


# --- (c) an empty db ----------------------------------------------------------------------------------------------------------
def test_attached_over_an_empty_db_every_panel_reads_not_derived_yet_and_no_provider_runs(rig: Rig) -> None:
    for pid in PANELS:
        snap = rig.snap(pid)
        assert (snap.status, snap.error, snap.stale_hint, snap.rows) == ("error", engine.NOT_DERIVED_YET, True, None)
        assert snap.fresh_info == engine._Fresh(None, None, True, False)
    m = rig.ck.manifest(CRED)
    assert all(h["status"] == "error" and h["error"] == "not derived yet" for h in m["panels"].values())
    assert rig.ck.panel("alpha")["status"] == "error"
    assert rig.ck.freshness()["alpha"] == {"as_of": None, "status": "unread", "stale": True}
    assert rig.rec.count() == 0


def test_attached_over_a_missing_file_reads_the_same(tmp_path: Path) -> None:
    rec = Recorder()
    ck = Cockpit(clock=Clock())
    ck.register(list_spec("alpha", rec))
    ck.attach_snapshots(SnapshotReader(tmp_path / "never.db"))
    assert ck.panel("alpha")["error"] == "not derived yet" and rec.count() == 0
    assert ck.worker_health() == {"worker": "down", "heartbeat_age_s": None, "deriving": []}


# --- (d) a persisted db, no live worker, then a worker comes alive ---------------------------------------------------------------
def test_a_persisted_db_with_no_live_heartbeat_serves_every_row_stale_with_its_age(tmp_path: Path) -> None:
    first = Rig(tmp_path)
    first.derive()
    first.close()                                    # the run that made the rows ends; nothing beats any more
    # a relaunched serve: the same file, five minutes on, no worker of its own yet
    cold = Rig(tmp_path, start=T0 + timedelta(minutes=5), worker=False)
    worker = None
    try:
        for pid in PANELS:
            snap = cold.snap(pid)
            assert snap.status == "ok" and snap.fresh_info.stale is True
            assert snap.as_of == T0.isoformat() == snap.fresh_info.derived_at
            age = (cold.clock() - datetime.fromisoformat(snap.fresh_info.derived_at)).total_seconds()
            assert age == 300.0
        assert cold.ck.worker_health()["worker"] == "down"
        assert cold.ck.manifest(CRED)["panels"]["alpha"]["as_of"] == T0.isoformat()
        assert cold.rec.count() == 0
        # a new worker opens the file: until it re-derives, a beat alone is not enough (D10)
        worker = SnapshotWriter(cold.path, now=cold.clock.epoch())
        worker.heartbeat(cold.clock.epoch())
        assert cold.ck.worker_health()["worker"] == "live"
        assert all(cold.snap(pid).fresh_info.stale for pid in PANELS)
        cold.derive(loop=cold.make_loop(worker, cold.panels))
        assert not any(cold.snap(pid).fresh_info.stale for pid in PANELS)
        assert cold.snap("alpha").as_of == (T0 + timedelta(minutes=5)).isoformat()
    finally:
        if worker is not None:
            worker.close()
        cold.close()


# --- the freshness rule, one clause at a time ---------------------------------------------------------------------------
def _publish(r: Rig, pid: str, *, derived_at: float | None = None, valid_until: float | None = None,
             status: str = "ok", error: str | None = None, body: Any = None) -> None:
    """One row written straight to the file, so a clause can be set without a derive."""
    if body is None:
        status_, body = sh.encode_snap(r.ck.derive_panel(pid))
        status = status if status != "ok" else status_
    at = r.clock.epoch() if derived_at is None else derived_at
    r.writer.publish(pid, status=status, body=encode_body(body) if body is not None else None, started_at=at,
                     derived_at=at, duration_ms=1, valid_until=valid_until, error=error)


def test_the_worker_is_live_within_three_beats_stale_to_the_bound_then_down(rig: Rig) -> None:
    rig.derive()
    live, down_after = engine.WORKER_LIVE_FACTOR * engine.HEARTBEAT_CADENCE_S, engine.WORKER_DOWN_AFTER_S
    for gap, worker in ((0, "live"), (live, "live"), (live + 1, "stale"), (down_after, "stale"), (down_after + 1, "down")):
        rig.clock.now = T0 + timedelta(seconds=gap)
        health = rig.ck.worker_health()
        assert (health["worker"], health["heartbeat_age_s"]) == (worker, float(gap)), gap
        assert rig.snap("alpha").fresh_info.stale == (worker != "live")


def test_a_host_that_states_its_health_is_believed_and_a_garbled_answer_falls_back_to_the_file(tmp_path: Path) -> None:
    told: dict[str, Any] = {"worker": "down", "heartbeat_age_s": 99.0}
    r = Rig(tmp_path, host_state=lambda: told)
    try:
        r.derive()
        assert r.ck.worker_health() == {"worker": "down", "heartbeat_age_s": 99.0, "deriving": []}
        assert r.snap("alpha").fresh_info.stale
        told.update(worker="sideways")
        assert r.ck.worker_health()["worker"] == "live"
        r.ck._host_state = lambda: 1 / 0               # a host that raises is read through the file
        assert r.ck.worker_health()["worker"] == "live"
    finally:
        r.close()


def test_a_row_is_stale_once_its_valid_until_passes(rig: Rig) -> None:
    rig.derive()
    _publish(rig, "alpha", valid_until=rig.clock.epoch() + 60)
    for gap, stale in ((0, False), (59, False), (60, True), (120, True)):
        rig.clock.now = T0 + timedelta(seconds=gap)
        rig.writer.heartbeat(rig.clock.epoch())
        snap = rig.snap("alpha")
        assert snap.fresh_info.stale is stale, gap
        assert snap.fresh_info.valid_until == (T0 + timedelta(seconds=60)).isoformat()
    assert not rig.snap("beta").fresh_info.stale and rig.snap("beta").fresh_info.valid_until is None


def test_a_row_older_than_twice_its_refresh_cadence_is_stale_under_a_live_worker(tmp_path: Path) -> None:
    rec = Recorder()
    r = Rig(tmp_path, specs={"alpha": list_spec("alpha", rec, refresh_every_s=10.0, stale_after_s=10.0), "beta": list_spec("beta", rec)})
    try:
        r.derive()
        for gap, stale in ((19, False), (20, False), (21, True)):
            r.clock.now = T0 + timedelta(seconds=gap)
            r.writer.heartbeat(r.clock.epoch())
            assert r.snap("alpha").fresh_info.stale is stale, gap
        # a panel with no cadence of its own is held to twice its ``stale_after_s``
        limit = 2 * r.spec("beta").stale_after_s
        for gap, stale in ((limit, False), (limit + 1, True)):
            r.clock.now = T0 + timedelta(seconds=gap)
            r.writer.heartbeat(r.clock.epoch())
            assert r.snap("beta").fresh_info.stale is stale, gap
    finally:
        r.close()


def test_pending_and_the_deriving_list_come_from_the_host(tmp_path: Path) -> None:
    r = Rig(tmp_path, pending=lambda pid: pid == "alpha", deriving=lambda: ["beta", "alpha"])
    try:
        r.derive()
        assert r.snap("alpha").fresh_info.pending is True and r.snap("beta").fresh_info.pending is False
        assert r.ck.worker_health()["deriving"] == ["alpha", "beta"]
    finally:
        r.close()


# --- error rows, an unreadable store, an unreadable body --------------------------------------------------------------------
def test_a_failed_derive_reads_as_an_error_with_its_message_and_serves_no_stale_rows(rig: Rig) -> None:
    rig.derive()
    rig.rec.failing.add("alpha")
    rig.derive("alpha")
    snap = rig.snap("alpha")
    assert snap.status == "error" and "boom" in snap.error and snap.rows is None
    assert rig.ck.panel("alpha")["rows"] is None
    assert rig.snap("beta").status == "ok"


def test_a_timeout_row_reads_as_an_error(rig: Rig) -> None:
    rig.derive()
    rig.writer.publish("alpha", status="timeout", body=None, started_at=rig.clock.epoch(), derived_at=rig.clock.epoch(),
                       duration_ms=1, error="derive still running after 120 s")
    snap = rig.snap("alpha")
    assert (snap.status, snap.error) == ("error", "derive still running after 120 s")


def test_a_store_that_cannot_be_read_is_an_error_answer_and_runs_no_provider(tmp_path: Path) -> None:
    class Broken:
        def read(self, pid: str) -> Any:
            raise SnapshotReadError("OperationalError: database is locked")

        def meta(self) -> dict:
            raise SnapshotReadError("OperationalError: database is locked")

    rec = Recorder()
    ck = Cockpit(clock=Clock())
    ck.register(list_spec("alpha", rec))
    ck.attach_snapshots(Broken())
    got = ck.panel("alpha")
    assert got["status"] == "error" and "could not be read" in got["error"] and "locked" in got["error"]
    assert rec.count() == 0


def test_a_body_of_a_schema_this_code_does_not_know_is_an_error_answer(rig: Rig) -> None:
    rig.derive()
    rig.writer.publish("alpha", status="ok", body=encode_body({"v": 99}), started_at=1.0, derived_at=rig.clock.epoch(),
                       duration_ms=1)
    snap = rig.snap("alpha")
    assert snap.status == "error" and "could not be read" in snap.error
    assert rig.snap("beta").status == "ok"


# --- the decoded snapshot is shared by generation ------------------------------------------------------------------------
def test_one_decoded_snapshot_serves_a_generation_and_a_new_derive_of_the_same_data_moves_only_its_age(rig: Rig) -> None:
    rig.derive()
    first = rig.snap("alpha")
    again = rig.snap("alpha")
    assert first.rows is again.rows and first is not again          # one decode, a copy per read for the facts that move
    head = rig.ck._head(rig.spec("alpha"), first, "none", T0.date(), rig.ck.authority())
    rig.clock.advance(seconds=1)
    rig.derive("alpha")                                              # the same rows: the store keeps the generation
    later = rig.snap("alpha")
    assert later.rows is first.rows
    assert later.as_of > first.as_of
    assert rig.ck._head(rig.spec("alpha"), later, "none", T0.date(), rig.ck.authority())["etag"] == head["etag"]
    rig.rec.rows["alpha"] = ["a", "c"]
    rig.clock.advance(seconds=1)
    rig.derive("alpha")
    changed = rig.snap("alpha")
    assert changed.rows is not first.rows and [r["id"] for r in changed.rows] == ["alpha:a", "alpha:c"]


# --- (f) a retired panel is refused ------------------------------------------------------------------------------------------
def test_a_retired_panel_is_refused_and_never_served(rig: Rig) -> None:
    rig.derive()
    old = rig.spec("alpha")
    rig.ck.unregister("alpha")
    refused = rig.ck._snap_for(old, ReadContext(rig.clock()))
    assert refused.status == "error" and "retired" in refused.error
    assert rig.ck.panel("alpha") is None and "alpha" not in rig.ck.manifest(CRED)["panels"]
    with pytest.raises(KeyError):
        rig.ck.derive_panel("alpha")
    assert rig.ck.change_probe().snapshot()[0].keys() == {"beta"}


# --- (g) the serve-composed panels still run on the request path -------------------------------------------------------------
def test_serve_composed_panels_run_their_provider_on_the_request_path_and_the_rest_do_not(tmp_path: Path) -> None:
    rec = Recorder()
    r = Rig(tmp_path, panels=("alpha", "edits", "mine"),
            specs={"alpha": list_spec("alpha", rec), "edits": list_spec("edits", rec), "mine": list_spec("mine", rec)})
    try:
        r.ck.mark_serve_composed(["mine"])
        assert [r.ck.serve_composed(p) for p in ("decisions", "edits", "mine", "alpha")] == [True, True, True, False]
        assert engine.SERVE_COMPOSED_IDS == frozenset({"decisions", "edits"})
        r.derive("alpha")
        before = rec.count()
        r.ck.manifest(CRED)
        r.ck.panel("edits")
        r.ck.panel("mine")
        r.ck.panel("alpha")
        made = rec.calls[before:]
        assert {c[0] for c in made} == {"edits", "mine"} and all(c[2] for c in made)
        assert rec.count("alpha") == 1                               # the derive's, and no request's
        assert r.ck.freshness()["alpha"]["status"] == "ok"
    finally:
        r.close()


def test_mark_serve_composed_refuses_a_non_string_id() -> None:
    with pytest.raises(TypeError):
        Cockpit().mark_serve_composed([1])


# --- start and stop ---------------------------------------------------------------------------------------------------------
def test_an_attached_cockpit_starts_no_refresher_and_stop_is_safe(tmp_path: Path) -> None:
    rec = Recorder()
    r = Rig(tmp_path, specs={"alpha": list_spec("alpha", rec, refresh_every_s=5.0, stale_after_s=5.0)})
    try:
        r.ck.start()
        assert r.ck.refresher("alpha") is None and rec.count() == 0
        assert r.ck.stop() == []
        r.ck.start()
        assert r.ck.stop() == [] and rec.count() == 0
        r.derive()
        assert r.snap("alpha").status == "ok" and rec.count("alpha") == 1     # a refresher-declared panel reads its row too
    finally:
        r.close()


# --- (i) once only ----------------------------------------------------------------------------------------------------------
def test_attach_snapshots_twice_with_another_reader_raises_and_with_the_same_one_is_a_no_op(rig: Rig, tmp_path: Path) -> None:
    rig.ck.attach_snapshots(rig.reader)
    other = SnapshotReader(tmp_path / "other.db")
    with pytest.raises(RuntimeError):
        rig.ck.attach_snapshots(other)
    assert rig.ck._reader is rig.reader


# --- the manifest's entity and discovery come from the host cycle ----------------------------------------------------------------
def test_an_attached_manifest_runs_no_discoverer_and_no_entity_function(tmp_path: Path) -> None:
    rec = Recorder()

    def entity(ctx: ReadContext) -> dict:
        rec.entity_calls += 1
        return {"name": "the install"}

    def discoverer(ctx: ReadContext) -> list:
        rec.discovered += 1
        return [list_spec("found", rec)]

    clock = Clock()
    ck = Cockpit(entity=entity, clock=clock)
    ck.register(list_spec("alpha", rec))
    ck.discover(discoverer)
    path = tmp_path / "snapshots.db"
    writer, reader = SnapshotWriter(path, now=clock.epoch()), SnapshotReader(path)
    try:
        ck.attach_snapshots(reader)
        m = ck.manifest(CRED)
        assert (rec.entity_calls, rec.discovered) == (0, 0)
        assert m["entity"] == {"name": None, "governance": None, "brand": {"wordmark": None, "model": None}}
        assert set(m["panels"]) == {"alpha"} and ck.panel("found") is None and rec.discovered == 0
        ck.start()
        assert (rec.entity_calls, rec.discovered) == (0, 0)
    finally:
        reader.close()
        writer.close()


# --- (j) the derive-side calls ----------------------------------------------------------------------------------------------
def test_run_discovery_derive_panel_and_read_entity_each_work_against_a_discovered_panel(tmp_path: Path) -> None:
    rec = Recorder()
    clock = Clock()

    def entity(ctx: ReadContext) -> dict:
        rec.entity_calls += 1
        return {"name": "the install"}

    def discoverer(ctx: ReadContext) -> list:
        rec.discovered += 1
        return [list_spec("found", rec)]

    ck = Cockpit(entity=entity, clock=clock)
    ck.register(list_spec("alpha", rec))
    ck.discover(discoverer)
    path = tmp_path / "snapshots.db"
    writer, reader = SnapshotWriter(path, now=clock.epoch()), SnapshotReader(path)
    try:
        ck.attach_snapshots(reader)
        assert ck.run_discovery() == ["found"] and rec.discovered == 1
        assert "found" in ck.panel_ids and ck.run_discovery() == ["found"]
        snap = ck.derive_panel("found")                                  # registered by discovery, derived like any other
        assert snap.status == "ok" and rec.calls[-1] == ("found", "cockpit-read", False)
        entity_value = ck.read_entity()
        assert entity_value[0]["name"] == "the install" and entity_value[1] == [] and rec.entity_calls == 1
        assert ck.manifest(CRED)["entity"]["name"] is None             # not stored yet: the empty entity
        ck.set_entity_value(entity_value)
        assert ck.manifest(CRED)["entity"]["name"] == "the install" and rec.entity_calls == 1
        # the manifest's errors are what the host last saw: a discoverer fault and an entity fault
        ck.discover(lambda ctx: 1 / 0)
        ck.run_discovery()
        ck._entity_fn = lambda ctx: 1 / 0
        ck.set_entity_value(ck.read_entity())
        sources = {e["source"] for e in ck.manifest(CRED)["errors"]}
        assert sources == {"discovery:1", "entity"}
    finally:
        reader.close()
        writer.close()


def test_a_retiring_discoverers_panel_leaves_the_panel_set_the_host_reads(tmp_path: Path) -> None:
    rec = Recorder()
    live = [True]
    ck = Cockpit(clock=Clock())
    ck.discover(lambda ctx: [list_spec("team", rec)] if live[0] else [], retire=True)
    assert ck.run_discovery() == ["team"]
    live[0] = False
    assert ck.run_discovery() == [] and "team" not in ck.panel_ids


# --- (h) the change probe --------------------------------------------------------------------------------------------------
def test_change_seq_moves_on_a_published_change_only_never_on_a_heartbeat(rig: Rig) -> None:
    rig.derive()
    probe = rig.ck.change_probe()
    assert probe is rig.ck.change_probe()
    seq = probe.change_seq()
    assert seq > 0
    rig.clock.advance(seconds=1)
    rig.writer.heartbeat(rig.clock.epoch())
    assert probe.change_seq() == seq
    rig.derive("alpha")                                              # the same rows: no new generation
    assert probe.change_seq() == seq
    rig.rec.rows["alpha"] = ["a", "z"]
    rig.derive("alpha")
    assert probe.change_seq() == seq + 1


def test_the_probe_snapshot_etags_are_the_class_none_manifest_etags_and_a_provider_never_runs(rig: Rig) -> None:
    rig.derive()
    calls = rig.rec.count()
    probe = rig.ck.change_probe()
    etags, version = probe.snapshot()
    heads = rig.ck.manifest(CRED)["panels"]
    assert etags == {pid: heads[pid]["etag"] for pid in PANELS}
    assert probe.snapshot() == (etags, version)
    rig.rec.rows["alpha"] = ["a", "z"]
    rig.derive("alpha")
    moved, same_version = probe.snapshot()
    assert moved["alpha"] != etags["alpha"] and moved["beta"] == etags["beta"] and same_version == version
    assert rig.rec.count() == calls + 1                              # the one derive; the probe made none


def test_set_version_moves_with_the_day_the_verb_revision_and_the_panel_set(rig: Rig) -> None:
    view = FakeVerbView()
    rig.ck.attach_verbs(view)
    rig.derive()
    probe = rig.ck.change_probe()
    version = probe.snapshot()[1]
    assert probe.snapshot()[1] == version
    seen = {version}
    view.rev = "r2"
    seen.add(probe.snapshot()[1])
    rig.clock.advance(days=1)
    seen.add(probe.snapshot()[1])
    rig.ck.register(list_spec("gamma", rig.rec))
    seen.add(probe.snapshot()[1])
    assert len(seen) == 4


def test_an_unattached_cockpit_has_no_probe() -> None:
    assert Cockpit().change_probe() is None
    with pytest.raises(RuntimeError):
        Cockpit().worker_health()


# --- the route's freshness map ----------------------------------------------------------------------------------------------
def test_freshness_answers_from_the_rows(rig: Rig) -> None:
    assert rig.ck.freshness()["alpha"] == {"as_of": None, "status": "unread", "stale": True}
    rig.derive()
    fresh = rig.ck.freshness()
    assert fresh["alpha"] == {"as_of": T0.isoformat(), "status": "ok", "stale": False}
    rig.clock.advance(seconds=60)
    assert rig.ck.freshness()["alpha"]["stale"] is True
    assert rig.rec.request_side() == []


# --- a provider's own stale word and its own source instant survive the move to attached mode -------------------------------
def _declared_spec(pid: str, rows: tuple[str, ...], *, stale: bool, as_of: str | None) -> ProviderSpec:
    def read(ctx: ReadContext) -> Read:
        return Read(rows=tuple(RowIn(f"{pid}:{n}", n, {"at": "2026-11-19"}, stored={"id": f"{pid}:{n}"}) for n in rows),
                    stale=stale, as_of=as_of)

    return ProviderSpec(id=pid, kind="triage-list", title=pid, priority="feed", read=read, order="time.desc",
                        facets=frozenset({"at"}), version_fields=("id",))


EARLIER = (T0 - timedelta(hours=5)).isoformat()


def test_a_provider_declared_stale_read_with_its_own_as_of_reads_the_same_attached_as_on_a_bare_cockpit(tmp_path: Path) -> None:
    specs = {"alpha": _declared_spec("alpha", ("a", "b"), stale=True, as_of=EARLIER)}
    bare = Cockpit(clock=Clock())
    bare.register(specs["alpha"])
    want = bare._snap_for(bare._specs["alpha"], ReadContext(T0))
    assert (want.status, want.as_of, want.stale_hint) == ("stale", EARLIER, True)
    r = Rig(tmp_path, panels=("alpha",), specs=specs)
    try:
        r.derive()
        got = r.snap("alpha")
        assert got.fresh_info.stale is True
        assert got.as_of == want.as_of == EARLIER
        assert r.ck.freshness()["alpha"] == {"as_of": EARLIER, "status": got.status, "stale": True}
    finally:
        r.close()


def test_a_read_with_no_declared_as_of_serves_the_row_s_derive_time_and_is_not_stale(tmp_path: Path) -> None:
    r = Rig(tmp_path, panels=("alpha",), specs={"alpha": _declared_spec("alpha", ("a",), stale=False, as_of=None)})
    try:
        r.derive()
        got = r.snap("alpha")
        assert (got.as_of, got.fresh_info.stale) == (T0.isoformat(), False)
    finally:
        r.close()


def test_a_declared_as_of_is_part_of_the_content_so_a_new_one_is_a_new_generation(tmp_path: Path) -> None:
    box = {"as_of": EARLIER}

    def read(ctx: ReadContext) -> Read:
        return Read(rows=(RowIn("p:a", "a", {"at": "2026-11-19"}, stored={"id": "p:a"}),), as_of=box["as_of"])

    spec = ProviderSpec(id="alpha", kind="triage-list", title="alpha", priority="feed", read=read, order="time.desc",
                        facets=frozenset({"at"}), version_fields=("id",))
    r = Rig(tmp_path, panels=("alpha",), specs={"alpha": spec})
    try:
        r.derive()
        first = r.reader.read("alpha")
        box["as_of"] = (T0 - timedelta(hours=1)).isoformat()
        r.clock.advance(seconds=1)
        r.derive("alpha")
        second = r.reader.read("alpha")
        assert second.body_sha != first.body_sha and second.gen > first.gen
        assert r.snap("alpha").as_of == box["as_of"]
    finally:
        r.close()
