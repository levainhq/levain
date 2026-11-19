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
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit import engine
from levain.cockpit.derive import DeriveLoop, PanelPlan
from levain.cockpit.engine import Cockpit
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
    rig.ck.invalidate_reads(["beta"], refresh=False)
    assert rig.dirty_calls == [(("alpha",), 1), (("alpha", "beta"), 2), (("beta",), 3)]


def test_a_write_naming_no_panels_touches_every_attached_panel_but_the_serve_composed_ones(tmp_path: Path) -> None:
    r = WriteRig(tmp_path, ("alpha", "beta", "gamma"))
    try:
        r.ck.mark_serve_composed(["gamma"])
        r.forward = False
        r.ck.invalidate_reads()
        assert r.dirty_calls == [(("alpha", "beta"), 1)]
    finally:
        r.close()


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
    rig.ck.invalidate_reads()
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
