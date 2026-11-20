"""D7 in the attached mode: a write bumps the serve's write generation and tells the derive host which panels it touched, and
a read of a touched panel waits, bounded, for the row that answers it.

Every table is built from the engine's own types over a real snapshot file (the harness is ``test_cockpit_attached_engine``'s)
and a real ``DeriveLoop`` as the host: driven in the test's own thread where a test needs the host to be silent or exact, on
its own threads where a test needs a host that answers a write by itself. The host's answer reaches the cockpit through
``Cockpit.note_publish``, the callable a loop is given as its ``on_publish``.

What the file pins, part by part: the write (the generation, what the host is told, outside which locks, what a host that
raises costs); the read (post-write data, the bounded fallback, one deadline for a manifest, nothing waited for when no write
touched the panel); and a bare cockpit, which does none of it."""
from __future__ import annotations

import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import engine
from levain.cockpit import snapshot_host as sh
from levain.cockpit.derive import DeriveLoop, PanelPlan
from levain.cockpit.engine import Cockpit, ReadContext
from levain.cockpit.snapshots import encode_body
from tests.test_cockpit_attached_engine import CRED, PANELS, Recorder, Rig, list_spec

LIVE = {"worker": "live", "heartbeat_age_s": 0.0}


class WriteRig(Rig):
    """An attached cockpit that hands the derive loop its writes. ``forward`` False makes the host deaf: it is told nothing
    (the calls are still recorded), so no row ever answers a write. ``fail`` makes the host's dirty hook raise."""

    def __init__(self, tmp_path: Path, panels: tuple[str, ...] = PANELS, **kw: Any) -> None:
        self.dirty_calls: list[tuple[tuple[str, ...], int]] = []
        self.forward = True
        self.fail: BaseException | None = None
        self.locks_free_in_dirty: list[bool] = []
        self.publishes: list[tuple[str, int, int]] = []
        self._pub = threading.Condition()
        super().__init__(tmp_path, panels, attach=False, **kw)
        self.ck.attach_snapshots(self.reader, dirty=self._dirty, host_state=lambda: LIVE)

    def make_loop(self, writer: Any, panels: list[str]) -> DeriveLoop:
        # the hook looks ``note_publish`` up when a publish lands, so a cockpit without it fails where it is asserted
        return DeriveLoop(writer, {pid: PanelPlan(pid, on_token=True) for pid in panels}, self.derive_fn,
                          clock=self.clock.epoch, on_publish=self._published)

    def _published(self, pid: str, gen: int, write_gen: int) -> None:
        with self._pub:
            self.publishes.append((pid, gen, write_gen))
            self._pub.notify_all()
        self.ck.note_publish(pid, gen, write_gen)

    def wait_published(self, pid: str, write_gen: int, timeout: float = 5.0) -> bool:
        with self._pub:
            return self._pub.wait_for(lambda: any(p == pid and w >= write_gen for p, _g, w in self.publishes), timeout)

    def _dirty(self, ids: Any, write_gen: int) -> None:
        ids = tuple(ids)
        free = []
        for lock in (self.ck._rc_lock, self.ck._lock, self.ck._attached_lock, self.ck._head_lock):
            got = lock.acquire(blocking=False)
            if got:
                lock.release()
            free.append(got)
        self.locks_free_in_dirty.append(all(free))
        self.dirty_calls.append((ids, write_gen))
        if self.fail is not None:
            raise self.fail
        if self.forward:
            self.loop.dirty(ids, write_gen)

    def publish_by_hand(self, pid: str, write_gen: int) -> None:
        """A row for ``pid`` stamped with ``write_gen``, published as the loop would, and the cockpit told (a late host)."""
        status, body = sh.encode_snap(self.ck.derive_panel(pid))
        now = self.clock.epoch()
        gen = self.writer.publish(pid, status=status, body=encode_body(body), started_at=now, derived_at=now, duration_ms=1,
                                  write_gen=write_gen)
        self.ck.note_publish(pid, gen, write_gen)

    def host_runs(self) -> None:
        """Start the loop's own threads: from here the host answers a write by itself."""
        self.loop.start()

    def close(self) -> None:
        if self.loop is not None:
            self.loop.stop(5.0)
        super().close()


@pytest.fixture()
def rig(tmp_path: Path):
    r = WriteRig(tmp_path)
    yield r
    r.close()


# --- the write: the generation and what the host is told -----------------------------------------------------------------
def test_a_write_tells_the_host_the_panels_it_touched_under_a_generation_that_only_grows(rig: WriteRig) -> None:
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    rig.ck.invalidate_reads(["alpha", "beta"])
    rig.ck.invalidate_reads(["beta"])
    assert rig.dirty_calls == [(("alpha",), 1), (("alpha", "beta"), 2), (("beta",), 3)]


def test_a_write_naming_no_panels_asks_nothing_of_the_host_and_holds_no_read(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)           # a read that entered the wait would show as time
    rig.derive()
    rig.forward = False
    waits = rig.ck.write_wait_counts()
    rig.ck.invalidate_reads()
    rig.ck.invalidate_reads(refresh=False)
    assert rig.dirty_calls == []
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    heads = rig.ck.manifest(CRED)["panels"]
    assert time.monotonic() - t0 < 5.0
    assert out["stale"] is False and out["pending"] is False
    assert all(h["stale"] is False and h["pending"] is False for h in heads.values())
    assert rig.ck.write_wait_counts() == waits
    rig.ck.invalidate_reads(["alpha"])                           # a write that names a panel still takes the path
    assert rig.dirty_calls == [(("alpha",), 1)]


def test_a_write_names_only_panels_that_are_registered_and_served_from_rows(tmp_path: Path) -> None:
    r = WriteRig(tmp_path, ("alpha", "beta", "gamma"))
    try:
        r.ck.mark_serve_composed(["gamma"])
        r.forward = False
        r.ck.invalidate_reads(["gamma", "nowhere", "beta", "beta"])
        assert r.dirty_calls == [(("beta",), 1)]
        r.ck.invalidate_reads(["nowhere"])            # nothing a host could answer: it is not told, and the generation still moves
        r.ck.invalidate_reads(["alpha"])
        assert r.dirty_calls == [(("beta",), 1), (("alpha",), 3)]
    finally:
        r.close()


def test_the_host_is_told_outside_every_cockpit_lock(rig: WriteRig) -> None:
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    rig.ck.invalidate_reads(["alpha", "beta"])
    assert rig.locks_free_in_dirty == [True, True]


def test_a_bare_cockpit_never_tells_a_host(tmp_path: Path) -> None:
    r = Rig(tmp_path, attach=False)
    told: list[Any] = []
    try:
        r.ck._host_dirty = lambda ids, w: told.append((ids, w))      # a hook no public call can give a bare cockpit
        r.ck.invalidate_reads(["alpha"])
        r.ck.invalidate_reads()
        assert told == []
    finally:
        r.close()


# --- the read: post-write data -------------------------------------------------------------------------------------------
def rows_of(payload: dict[str, Any]) -> list[str]:
    return sorted(r["id"] for r in payload["rows"])


def test_a_panel_read_after_a_write_returns_post_write_data_without_waiting_out_the_bound(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)          # a pass that needed the bound would be a pass of 30 s
    rig.derive()
    rig.host_runs()
    assert rows_of(rig.ck.panel("alpha")) == ["alpha:a", "alpha:b"]
    rig.rec.rows["alpha"] = ["a", "z"]
    rig.ck.invalidate_reads(["alpha"])
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    assert time.monotonic() - t0 < 10.0
    assert rows_of(out) == ["alpha:a", "alpha:z"] and out["stale"] is False and out["pending"] is False


def test_a_manifest_after_a_write_carries_the_post_write_head(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.host_runs()
    before = rig.ck.manifest(CRED)["panels"]
    rig.rec.rows["alpha"] = ["a", "z", "y"]
    rig.ck.invalidate_reads(["alpha"])
    t0 = time.monotonic()
    after = rig.ck.manifest(CRED)["panels"]
    assert time.monotonic() - t0 < 10.0
    assert after["alpha"]["count"] == 3 and after["alpha"]["stale"] is False and after["alpha"]["etag"] != before["alpha"]["etag"]
    assert after["beta"]["etag"] == before["beta"]["etag"]


def test_the_read_is_woken_by_the_publish_and_not_by_the_bound(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    stamps: list[float] = []

    def answer() -> None:
        time.sleep(0.2)
        rig.publish_by_hand("alpha", 0)           # a row that is no answer to the write: the read stays where it is
        time.sleep(0.2)
        stamps.append(time.monotonic())
        rig.publish_by_hand("alpha", 1)

    t = threading.Thread(target=answer)
    t.start()
    out = rig.ck.panel("alpha")
    done = time.monotonic()
    t.join()
    assert out["stale"] is False and stamps and 0 <= done - stamps[0] < 2.0
    assert rig.ck.write_wait_counts() == {"waits": 1, "timeouts": 0}


# --- the bounded fallback -------------------------------------------------------------------------------------------------
def test_a_host_that_never_derives_is_served_stale_and_pending_within_the_bound(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.3)
    rig.derive()
    rig.forward = False
    rig.rec.rows["alpha"] = ["a", "z"]
    rig.ck.invalidate_reads(["alpha"])
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    took = time.monotonic() - t0
    assert 0.25 <= took < 1.5
    assert rows_of(out) == ["alpha:a", "alpha:b"]               # the older row
    assert out["stale"] is True and out["pending"] is True
    assert rig.ck.write_wait_counts() == {"waits": 1, "timeouts": 1}
    # a bound that has run out is not paid again by every read behind it
    t1 = time.monotonic()
    again = rig.ck.panel("alpha")
    assert time.monotonic() - t1 < 0.2 and again["stale"] is True and again["pending"] is True
    assert rig.ck.write_wait_counts()["waits"] == 1
    # the host answers late: the panel is current, and the flags go with it
    rig.forward = True
    rig.host_runs()
    rig.ck.invalidate_reads(["alpha"])
    assert rig.wait_published("alpha", 2)
    out = rig.ck.panel("alpha")
    assert rows_of(out) == ["alpha:a", "alpha:z"] and out["stale"] is False and out["pending"] is False


def test_a_late_row_clears_the_flags_without_another_write(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    assert rig.ck.panel("alpha")["stale"] is True
    rig.publish_by_hand("alpha", 1)
    out = rig.ck.panel("alpha")
    assert out["stale"] is False and out["pending"] is False


def test_a_new_write_after_a_lapsed_bound_waits_again(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    rig.ck.panel("alpha")
    rig.ck.invalidate_reads(["alpha"])
    rig.ck.panel("alpha")
    assert rig.ck.write_wait_counts() == {"waits": 2, "timeouts": 2}


def test_a_down_worker_is_not_waited_for(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    r = WriteRig(tmp_path)
    try:
        r.derive()
        r.ck._host_state = lambda: {"worker": "down", "heartbeat_age_s": 99.0}
        r.forward = False
        r.ck.invalidate_reads(["alpha"])
        t0 = time.monotonic()
        out = r.ck.panel("alpha")
        assert time.monotonic() - t0 < 5.0 and out["stale"] is True
        assert r.ck.write_wait_counts()["waits"] == 0
    finally:
        r.close()


# --- the double call ------------------------------------------------------------------------------------------------------
def test_the_repeat_call_of_one_write_tells_the_host_nothing_and_takes_no_generation(rig: WriteRig) -> None:
    """The server calls ``invalidate_reads`` for a write it admitted, and again after the response with ``refresh=False``: the
    first call is the write's, the second is its repeat. One write is one ``dirty`` and one generation."""
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    rig.ck.invalidate_reads(["alpha"], refresh=False)
    assert rig.dirty_calls == [(("alpha",), 1)]
    rig.ck.invalidate_reads(["alpha"])                          # the next write is the second generation, not the third
    assert rig.dirty_calls == [(("alpha",), 1), (("alpha",), 2)]


def test_the_repeat_call_leaves_the_requirement_of_the_first_and_what_a_derive_between_the_calls_stamped(
        rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.host_runs()
    rig.rec.rows["alpha"] = ["a", "z"]
    rig.ck.invalidate_reads(["alpha"])
    assert rig.wait_published("alpha", 1)                      # a derive began between the calls and stamped the first
    rig.ck.invalidate_reads(["alpha"], refresh=False)
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    assert time.monotonic() - t0 < 10.0
    assert rows_of(out) == ["alpha:a", "alpha:z"] and out["stale"] is False and out["pending"] is False
    assert rig.dirty_calls == [(("alpha",), 1)]
    assert rig.reader.read("alpha").write_gen == 1
    assert rig.ck.write_wait_counts()["timeouts"] == 0


def test_a_repeat_call_alone_records_no_requirement_and_holds_no_read(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"], refresh=False)
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    assert time.monotonic() - t0 < 5.0 and out["stale"] is False and out["pending"] is False
    assert rig.dirty_calls == [] and rig.ck.write_wait_counts()["waits"] == 0


def test_the_repeat_call_still_clears_the_caches_and_advances_the_epoch(rig: WriteRig) -> None:
    rig.derive()
    rig.forward = False
    epoch = rig.ck._state["alpha"].epoch
    rig.ck.invalidate_reads(["alpha"], refresh=False)
    assert rig.ck._state["alpha"].epoch == epoch + 1


# --- the manifest: one deadline -------------------------------------------------------------------------------------------
def test_a_manifest_takes_one_shared_deadline_for_every_touched_panel(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.4)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha", "beta"])
    t0 = time.monotonic()
    heads = rig.ck.manifest(CRED)["panels"]
    took = time.monotonic() - t0
    assert 0.35 <= took < 0.75                                 # one bound, not one per panel
    assert heads["alpha"]["stale"] is True and heads["alpha"]["pending"] is True
    assert heads["beta"]["stale"] is True and heads["beta"]["pending"] is True
    assert rig.ck.write_wait_counts() == {"waits": 1, "timeouts": 1}


def test_the_change_stream_does_not_wait_for_a_write(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.forward = False
    probe = rig.ck.change_probe()
    before, _v = probe.snapshot()
    rig.ck.invalidate_reads(["alpha"])
    t0 = time.monotonic()
    after, _v = probe.snapshot()
    rig.ck.manifest(CRED, read_after=time.monotonic())
    assert time.monotonic() - t0 < 5.0
    assert after["alpha"] != before["alpha"] and after["beta"] == before["beta"]    # announced: the panel is now said to be pending
    assert rig.ck.write_wait_counts()["waits"] == 0
    seq = probe.change_seq()
    rig.publish_by_hand("alpha", 1)                            # the row lands with the body it had: no panel change of its own
    assert probe.change_seq() != seq                           # and is announced, the stale and pending flags having flipped
    settled, _v = probe.snapshot()
    assert settled["alpha"] == before["alpha"]


# --- nothing waited for when nothing was written ------------------------------------------------------------------------
def test_a_read_with_no_write_waits_for_nothing(rig: WriteRig) -> None:
    rig.derive()
    rig.ck.panel("alpha")
    rig.ck.manifest(CRED)
    assert rig.ck.write_wait_counts() == {"waits": 0, "timeouts": 0}


def test_a_write_to_one_panel_makes_only_that_panels_reads_wait(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    assert rig.ck.panel("beta")["stale"] is False
    assert rig.ck.write_wait_counts()["waits"] == 0
    heads = rig.ck.manifest(CRED)["panels"]
    assert heads["alpha"]["stale"] is True and heads["beta"]["stale"] is False
    assert rig.ck.write_wait_counts()["waits"] == 1


def test_a_serve_composed_panel_is_never_waited_for(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    r = WriteRig(tmp_path, ("alpha", "gamma"))
    try:
        r.ck.mark_serve_composed(["gamma"])
        r.derive()
        r.forward = False
        r.ck.invalidate_reads(["gamma"])
        r.ck.invalidate_reads(["alpha", "gamma"])
        t0 = time.monotonic()
        r.ck.panel("gamma")
        assert time.monotonic() - t0 < 5.0 and r.ck.write_wait_counts()["waits"] == 0
    finally:
        r.close()


def test_an_attached_cockpit_with_no_dirty_hook_tells_no_one_and_waits_for_no_one(tmp_path: Path) -> None:
    r = Rig(tmp_path, attach=False)
    try:
        r.ck.attach_snapshots(r.reader, host_state=lambda: LIVE)
        r.derive()
        r.ck.invalidate_reads(["alpha"])
        t0 = time.monotonic()
        out = r.ck.panel("alpha")
        assert time.monotonic() - t0 < 1.0 and out["stale"] is False
        assert r.ck.write_wait_counts() == {"waits": 0, "timeouts": 0}
    finally:
        r.close()


def test_a_bare_cockpit_never_waits_and_reads_the_provider_after_a_write(tmp_path: Path) -> None:
    r = Rig(tmp_path, attach=False)
    try:
        r.ck.panel("alpha")
        r.rec.rows["alpha"] = ["a", "z"]
        r.ck.invalidate_reads(["alpha"])
        out = r.ck.panel("alpha")
        assert rows_of(out) == ["alpha:a", "alpha:z"] and "stale" not in out
        assert r.ck.write_wait_counts() == {"waits": 0, "timeouts": 0}
    finally:
        r.close()


# --- a host that raises ---------------------------------------------------------------------------------------------------
def test_a_host_that_raises_does_not_fail_the_write_and_its_panel_reads_stale_at_once(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)           # a wait for a host that cannot answer would show as time
    rig.derive()
    rig.fail = RuntimeError("the host is gone")
    rig.ck.invalidate_reads(["alpha"])                           # does not raise
    rig.ck.invalidate_reads(["alpha", "beta"])
    assert rig.ck.host_dirty_errors == 2 and rig.ck.last_host_dirty_error is rig.fail
    t0 = time.monotonic()
    head = rig.ck.manifest(CRED)["panels"]["alpha"]
    assert time.monotonic() - t0 < 5.0
    assert head["stale"] is True and head["pending"] is False   # the older row, said to be old; nothing is coming for it
    assert rig.ck.write_wait_counts()["waits"] == 0


def test_a_later_write_the_host_takes_clears_a_panel_marked_unsent(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.fail = RuntimeError("the host is gone")
    rig.ck.invalidate_reads(["alpha"])
    rig.fail = None
    rig.host_runs()
    rig.ck.invalidate_reads(["alpha"])
    out = rig.ck.panel("alpha")
    assert out["stale"] is False and out["pending"] is False
    assert rig.ck.host_dirty_errors == 1                         # the record of the failure stays


def test_a_panel_marked_unsent_is_current_again_once_a_derive_began_after_the_write(rig: WriteRig) -> None:
    rig.derive()
    rig.fail = RuntimeError("the host is gone")
    rig.ck.invalidate_reads(["alpha"])
    assert rig.ck.panel("alpha")["stale"] is True
    rig.clock.advance(31)                                        # the loop's own backstop re-derives it, with no write behind it
    rig.loop.run_once(rig.clock.epoch())
    assert rig.reader.read("alpha").write_gen == 0
    assert rig.ck.panel("alpha")["stale"] is False


# --- no lock is held while a read waits ---------------------------------------------------------------------------------
def test_a_waiting_read_holds_no_cockpit_lock(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 1.5)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    got: list[Any] = []
    t = threading.Thread(target=lambda: got.append(rig.ck.panel("alpha")))
    t.start()
    time.sleep(0.3)
    assert rig.ck.write_wait_counts()["waits"] == 1 and t.is_alive()       # it is inside the wait
    for lock in (rig.ck._rc_lock, rig.ck._lock, rig.ck._attached_lock, rig.ck._head_lock):
        assert lock.acquire(timeout=0.5)
        lock.release()
    rig.ck.invalidate_reads(["beta"])                            # a write goes through while it waits
    t.join()
    assert got[0]["stale"] is True


# --- two writers racing a reader ------------------------------------------------------------------------------------------
def test_two_writers_racing_a_reader_each_read_their_own_write(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 20.0)
    rig.derive()
    rig.host_runs()
    history: list[str] = []
    mu = threading.Lock()
    problems: list[Any] = []
    stop = threading.Event()

    def writer(tag: str) -> None:
        try:
            for i in range(6):
                marker = f"{tag}{i}"
                with mu:
                    history.append(marker)
                    rig.rec.rows["alpha"] = list(history)
                rig.ck.invalidate_reads(["alpha"])
                rig.ck.invalidate_reads(["alpha"], refresh=False)          # the server's second call of one write
                out = rig.ck.panel("alpha")
                # the other writer may have asked for a newer row since, so the flags are not this read's to assert
                if f"alpha:{marker}" not in rows_of(out):
                    problems.append((marker, rows_of(out)))
        except BaseException as exc:  # noqa: BLE001
            problems.append(exc)

    def reader() -> None:
        try:
            while not stop.is_set():
                rig.ck.panel("alpha")
                rig.ck.manifest(CRED)
        except BaseException as exc:  # noqa: BLE001
            problems.append(exc)

    rd = threading.Thread(target=reader)
    ws = [threading.Thread(target=writer, args=(tag,)) for tag in ("x", "y")]
    rd.start()
    for w in ws:
        w.start()
    for w in ws:
        w.join(60)
    stop.set()
    rd.join(30)
    assert not any(t.is_alive() for t in [rd, *ws])
    assert problems == []
    final = rig.ck.panel("alpha")
    assert rows_of(final) == sorted(f"alpha:{m}" for m in history) and final["stale"] is False


# --- a serve that outlives its worker's last run ------------------------------------------------------------------------
def test_a_row_stamped_by_an_earlier_serve_does_not_answer_this_serves_writes(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    rig.derive()
    rig.loop.dirty(("alpha",), 50)                               # the last run's generation, long past this one's first
    rig.loop.run_once(rig.clock.epoch())
    assert rig.reader.read("alpha").write_gen == 50
    ck = Cockpit(clock=rig.clock)
    ck.register(list_spec("alpha", rig.rec))
    ck.attach_snapshots(rig.reader, dirty=lambda ids, w: None, host_state=lambda: LIVE)
    ck.invalidate_reads(["alpha"])
    out = ck.panel("alpha")
    assert out["stale"] is True and out["pending"] is True




# --- a requirement outlives the first reader that sees its row ------------------------------------------------------------
def test_a_reader_holding_an_older_row_is_marked_owed_after_another_reader_saw_the_new_one(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.1)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    held = rig.reader.read_manifest()                   # one reader's read of the store, before the write's row lands
    rig.publish_by_hand("alpha", 1)
    ctx = ReadContext(rig.clock())
    seen_new = rig.ck._attached_snap(rig.spec("alpha"), ctx)
    assert seen_new.fresh_info.stale is False and seen_new.fresh_info.pending is False
    seen_old = rig.ck._attached_snap(rig.spec("alpha"), ctx, held)
    assert seen_old.fresh_info.stale is True and seen_old.fresh_info.pending is True
    assert rig.ck._write_owed("alpha", rig.reader.read("alpha")) == (False, False)
    assert rig.ck._write_owed("alpha", held[1]["alpha"]) == (True, False)       # asked again, by any reader, in any order


def test_a_manifest_that_read_the_store_before_the_row_landed_is_not_served_unmarked_by_another_readers_look(
        rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.1)
    rig.derive()
    rig.forward = False
    rig.rec.rows["alpha"] = ["a", "z", "y"]
    rig.ck.invalidate_reads(["alpha"])
    orig = rig.reader.read_manifest
    gate = threading.Event()
    other_done = threading.Event()

    def read_manifest_then_wait() -> Any:
        got = orig()                                    # the old row
        if not gate.is_set():
            gate.set()
            assert other_done.wait(10)                  # the row lands and another reader looks at it first
        return got

    rig.reader.read_manifest = read_manifest_then_wait
    out: list[Any] = []
    t = threading.Thread(target=lambda: out.append(rig.ck.manifest(CRED)["panels"]["alpha"]))
    t.start()
    assert gate.wait(10)
    rig.publish_by_hand("alpha", 1)
    assert rig.ck._attached_snap(rig.spec("alpha"), ReadContext(rig.clock())).fresh_info.stale is False
    other_done.set()
    t.join(10)
    assert out and (out[0]["count"] == 3 or out[0]["stale"] is True)


# --- a requirement the loop never stamps heals on the panel's next derive ---------------------------------------------------
def test_a_requirement_no_derive_stamps_is_answered_by_the_next_derive_that_began_after_the_write(
        rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    rig.derive()
    rig.forward = False                                          # the host accepted the call and never stamps it
    rig.ck.invalidate_reads(["alpha"])
    assert rig.ck.host_dirty_errors == 0
    out = rig.ck.panel("alpha")
    assert out["stale"] is True and out["pending"] is True
    rig.clock.advance(31)                                        # the loop's own backstop derives it, stamped with no write
    rig.loop.run_once(rig.clock.epoch())
    assert rig.reader.read("alpha").write_gen == 0
    out = rig.ck.panel("alpha")
    assert out["stale"] is False and out["pending"] is False


def test_a_read_is_not_held_for_a_row_a_derive_begun_after_the_write_already_is(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 1.5)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    rig.clock.advance(31)
    rig.loop.run_once(rig.clock.epoch())
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    assert time.monotonic() - t0 < 1.0 and out["stale"] is False
    assert rig.ck.write_wait_counts() == {"waits": 0, "timeouts": 0}


def test_a_panel_retired_and_registered_again_owes_nothing_for_a_write_that_named_its_earlier_life(
        rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 1.5)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    rig.ck.unregister("alpha")
    rig.ck.register(list_spec("alpha", rig.rec))
    rig.loop.dirty(("alpha",), 0)
    rig.loop.run_once(rig.clock.epoch())
    t0 = time.monotonic()
    out = rig.ck.panel("alpha")
    assert time.monotonic() - t0 < 1.0 and out["stale"] is False and out["pending"] is False
    assert rig.ck.write_wait_counts() == {"waits": 0, "timeouts": 0}


# --- attaching over rows an earlier life stamped ----------------------------------------------------------------------------
class AttachProbeReader:
    """The rig's reader, with a hook run inside the first ``read_manifest`` (the read an attach makes for the stamps)."""

    def __init__(self, inner: Any, during_first_read: Any) -> None:
        self._inner = inner
        self._hook = during_first_read
        self.reads = 0

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def read_manifest(self) -> Any:
        self.reads += 1
        if self.reads == 1:
            self._hook()
        return self._inner.read_manifest()


def earlier_life(tmp_path: Path) -> tuple[Rig, Cockpit, list[Any]]:
    """Rows stamped 50 by an earlier life of the serve, and a fresh cockpit that has not attached to them yet."""
    r = Rig(tmp_path, attach=False)
    r.derive()
    r.loop.dirty(("alpha",), 50)
    r.loop.run_once(r.clock.epoch())
    assert r.reader.read("alpha").write_gen == 50
    ck = Cockpit(clock=r.clock)
    ck.register(list_spec("alpha", r.rec))
    return r, ck, []


def test_a_write_made_while_the_stamps_are_being_read_at_attach_is_no_attached_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    r, ck, told = earlier_life(tmp_path)
    try:
        wrapped = AttachProbeReader(r.reader, lambda: ck.invalidate_reads(["alpha"]))
        ck.attach_snapshots(wrapped, dirty=lambda ids, w: told.append((tuple(ids), w)), host_state=lambda: LIVE)
        assert told == []                                       # nothing was told under a generation an old stamp passes
        ck.invalidate_reads(["alpha"])
        assert told == [(("alpha",), 51)]
        out = ck.panel("alpha")
        assert out["stale"] is True and out["pending"] is True
    finally:
        r.close()


class FailingFirstRead(AttachProbeReader):
    def __init__(self, inner: Any) -> None:
        super().__init__(inner, self._fail)

    @staticmethod
    def _fail() -> None:
        raise RuntimeError("the store cannot be read yet")

    def read_manifest(self) -> Any:
        self.reads += 1
        if self.reads == 1:
            self._fail()
        return self._inner.read_manifest()


def test_a_store_that_cannot_be_read_at_attach_leaves_an_earlier_lifes_stamps_no_answer(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    r, ck, told = earlier_life(tmp_path)
    try:
        ck.attach_snapshots(FailingFirstRead(r.reader), dirty=lambda ids, w: told.append((tuple(ids), w)),
                            host_state=lambda: LIVE)
        ck.invalidate_reads(["alpha"])
        assert told == [(("alpha",), 1)]
        out = ck.panel("alpha")                                  # the row is stamped 50: a stamp this life's generations pass
        assert out["stale"] is True and out["pending"] is True
        r.clock.advance(31)                                      # a derive that began after the write is the answer
        r.loop.run_once(r.clock.epoch())
        out = ck.panel("alpha")
        assert out["stale"] is False and out["pending"] is False
    finally:
        r.close()


# --- concurrent writes and what each one was told ---------------------------------------------------------------------------
def test_a_later_success_of_an_older_write_does_not_clear_what_a_newer_write_failed_to_tell(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    first_in = threading.Event()
    release = threading.Event()

    def dirty(ids: Any, write_gen: int) -> None:
        if write_gen == 1:
            first_in.set()
            assert release.wait(10)                      # the older write's call is still on its way to the host
            return
        raise RuntimeError("the host is gone")           # the newer write's call fails first

    rig.ck._host_dirty = dirty
    older = threading.Thread(target=lambda: rig.ck.invalidate_reads(["alpha"]))
    older.start()
    assert first_in.wait(10)
    rig.ck.invalidate_reads(["alpha"])
    release.set()
    older.join(10)
    t0 = time.monotonic()
    head = rig.ck.manifest(CRED)["panels"]["alpha"]
    assert time.monotonic() - t0 < 5.0
    assert head["stale"] is True and head["pending"] is False   # the host was never told of the newer write
    assert rig.ck.write_wait_counts()["waits"] == 0


def test_a_later_failure_of_an_older_write_does_not_mark_a_newer_write_the_host_took_as_unsent(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 0.2)
    rig.derive()
    first_in = threading.Event()
    release = threading.Event()

    def dirty(ids: Any, write_gen: int) -> None:
        if write_gen == 1:
            first_in.set()
            assert release.wait(10)
            raise RuntimeError("the host is gone")       # the older write's call fails after the newer one went through
        return                                           # the host takes the newer write and (deaf) never answers it

    rig.ck._host_dirty = dirty
    older = threading.Thread(target=lambda: rig.ck.invalidate_reads(["alpha"]))
    older.start()
    assert first_in.wait(10)
    rig.ck.invalidate_reads(["alpha"])
    release.set()
    older.join(10)
    head = rig.ck.manifest(CRED)["panels"]["alpha"]
    assert head["stale"] is True and head["pending"] is True     # a derive is owed for it: the host was told
    assert rig.ck.write_wait_counts() == {"waits": 1, "timeouts": 1}


def test_the_instant_of_a_write_is_taken_in_the_order_its_generation_is(rig: WriteRig) -> None:
    rig.forward = False
    base = rig.ck._clock
    mu = threading.Lock()
    taken: list[Any] = []
    first_in = threading.Event()

    def clock() -> Any:
        with mu:
            k = len(taken) + 1
            value = base() + timedelta(seconds=k)
            taken.append(value)
        if k == 1:
            first_in.set()
            time.sleep(0.3)                                # the first writer is slow between reading the clock and going on
        return value

    rig.ck._clock = clock
    older = threading.Thread(target=lambda: rig.ck.invalidate_reads(["alpha"]))
    older.start()
    assert first_in.wait(10)
    rig.ck.invalidate_reads(["alpha"])
    older.join(10)
    gen, at = rig.ck._write_required["alpha"]
    assert gen == 2 and at == max(v.timestamp() for v in taken)


# --- the request's clock is taken after the wait ----------------------------------------------------------------------------
def answer_after_the_clock_moved(rig: WriteRig) -> threading.Thread:
    def answer() -> None:
        time.sleep(0.3)
        rig.clock.advance(5)
        rig.publish_by_hand("alpha", 1)

    t = threading.Thread(target=answer)
    t.start()
    return t


def test_a_manifest_that_waited_is_composed_against_the_clock_after_the_wait(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    t = answer_after_the_clock_moved(rig)
    out = rig.ck.manifest(CRED)
    t.join(10)
    row = rig.reader.read("alpha")
    assert out["generated_at"] == engine._iso(rig.clock()) and engine._iso_epoch(row.derived_at) <= out["generated_at"]
    assert out["panels"]["alpha"]["stale"] is False


def test_a_panel_read_that_waited_is_composed_against_the_clock_after_the_wait(rig: WriteRig, monkeypatch) -> None:
    monkeypatch.setattr(engine, "WRITE_WAIT_S", 30.0)
    rig.derive()
    rig.forward = False
    rig.ck.invalidate_reads(["alpha"])
    seen: list[Any] = []
    orig = rig.ck._attached_panel

    def attached_panel(spec: Any, snap: Any, ctx: Any, *a: Any, **k: Any) -> Any:
        seen.append(ctx.now)
        return orig(spec, snap, ctx, *a, **k)

    rig.ck._attached_panel = attached_panel
    t = answer_after_the_clock_moved(rig)
    rig.ck.panel("alpha")
    t.join(10)
    assert seen == [rig.clock()]
