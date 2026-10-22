"""S1 session 3b-1b: render's front start interlocked with the revoker (s3b DESIGN §5 row 3b-1b; r3 R12, r5 R26).

Render (4) takes the slot's ``front.lock``, reads the revoker's durable ``front-revoked`` marker under it, refuses
``front-revoked`` with nothing spawned when the marker names this lease, and otherwise spawns and releases. Both acts
are ``Seams`` fields (``front_lock``, ``front_revoked``) whose defaults reach the reaper's primitive only when called,
so the helper imports, and the fake-seam cases pass, with that half of the tree absent.

Every case runs on session 2f2's unprivileged World (a tmp store, a fake front process table, the real CONTROL). The
``real`` cases use the production defaults, whose lock is a real flock on ``slots/<tag>/front.lock``; the ``fake``
cases replace both seams.
"""
from __future__ import annotations

import fcntl
import os
import re
import stat
import subprocess
import sys
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from levain.slot import runtime_helper as H
from tests.test_slot_s2f2_runtime_helper import World, _blob, _prepared

ROOT = Path(__file__).resolve().parent.parent


class Interlock:
    """Both seams, recorded: ``events`` in order, each read and spawn with whether the front lock was held, and
    every pool / slot lock open or wait made while it was held (``inner_*`` is a fake, or the production default)."""

    def __init__(self, world: World, inner_lock, inner_revoked) -> None:
        self.inner_lock, self.inner_revoked = inner_lock, inner_revoked
        self.held = False
        self.events: list[tuple] = []
        self.locks_while_held: list[tuple] = []
        spawn = world.seams.spawn_front

        def spawn_front(argv, spec_json, go_rfd):
            self.events.append(("spawn", self.held))
            return spawn(argv, spec_json, go_rfd)

        world.seams.spawn_front = spawn_front
        world.seams.front_lock = self.lock
        world.seams.front_revoked = self.revoked

    @contextmanager
    def lock(self, ctx, slot):
        with self.inner_lock(ctx, slot):
            self.events.append(("lock", slot.tag))
            self.held = True
            try:
                yield
            finally:
                self.held = False
                self.events.append(("unlock", slot.tag))

    def revoked(self, ctx, a):
        self.events.append(("read", self.held))
        return self.inner_revoked(ctx, a)

    def watch(self, monkeypatch) -> None:
        """Record the helper's own pool / slot lock primitives (``_open_lock``, ``wait_flock``) used while the front
        lock is held. What this proves: render opens no pool or slot lock of the helper's while holding front.lock.
        It does not see a lock taken through any other path; the mutation that opens a slot lock under front.lock
        (``e1_mutation_slot_lock_under_front.txt``) is the proof the watch can fail."""
        for name in ("_open_lock", "wait_flock"):
            real = getattr(H, name)

            def rec(*args, _real=real, _name=name):
                if self.held:
                    self.locks_while_held.append((_name, args[1] if len(args) > 1 else None))
                return _real(*args)

            monkeypatch.setattr(H, name, rec)


@pytest.mark.parametrize("whose", ["this", "other"])
@pytest.mark.parametrize("mode", ["fake", "real"])
def test_render_front_interlock(tmp_path, monkeypatch, mode, whose):
    """Reproduced on main (``e0_repro_main.txt``): a render whose lease the revoker had marked spawned the front."""
    world = World(tmp_path)
    s = _prepared(world)
    a = world.alloc(s)
    marked = a if whose == "this" else SimpleNamespace(session="s-00000000000000ff", lease_seq=a.lease_seq + 1)
    if mode == "fake":
        rec = Interlock(world, lambda ctx, slot: nullcontext(),
                        lambda ctx, alloc: (alloc.session, alloc.lease_seq) == (marked.session, marked.lease_seq))
    else:
        from levain.slot import start as SS

        (a.slot.slot_dir(world.L) / SS.FRONT_REVOKED).write_bytes(SS._marker(marked))
        rec = Interlock(world, H.front_lock_default, H.front_revoked_default)
    rec.watch(monkeypatch)
    world.calls.clear()

    code, line = world.main(["render", s], _blob())

    ops = [op for op, _ in world.calls]
    if whose == "this":
        assert line == {"ok": False, "refused": "front-revoked"}
        assert world.fronts.procs == {} and "MINT" not in ops and "FRONT-ARM" not in ops
        assert rec.events == [("lock", "e0"), ("read", True), ("unlock", "e0")]
    else:
        assert line == {"ok": True}
        assert len(world.fronts.procs) == 1 and world.fronts.alive() == list(world.fronts.procs)
        assert rec.events == [("lock", "e0"), ("read", True), ("spawn", True), ("unlock", "e0")]
    assert rec.locks_while_held == []
    if mode == "real":
        path = a.slot.slot_dir(world.L) / "front.lock"
        assert stat.S_IMODE(os.stat(path, follow_symlinks=False).st_mode) == 0o600
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)     # released: nothing still holds it
        finally:
            os.close(fd)


#: Run in a fresh interpreter with the reaper's module unimportable (a ``None`` entry in ``sys.modules`` makes its
#: import raise ``ImportError``): the helper imports, each production default refuses by name only when called,
#: and the fake-seam cases above pass.
_ABSENT = """
import sys
sys.modules["levain.slot.start"] = None
from levain.slot import Refused
from levain.slot import runtime_helper as H
for default in (H.front_lock_default, H.front_revoked_default):
    try:
        default(None, None)
    except Refused as e:
        assert e.name == "front-interlock-unavailable", e.name
    else:
        raise SystemExit("a default did not refuse with the reaper absent")
print("DEFAULTS-REFUSE")
import pytest
sys.exit(pytest.main([sys.argv[1], "-q", "-p", "no:cacheprovider", "--rootdir", sys.argv[2], "-k", "fake"]))
"""


def test_interlock_without_the_reaper_module(tmp_path):
    """Reproduced by a mutation: with the default's import of the reaper's module moved to module load, the
    helper does not import here (``e2_mutation_eager_import.txt``)."""
    inherited = os.environ.get("PYTHONPATH")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([str(ROOT)] + ([inherited] if inherited else []))}
    r = subprocess.run([sys.executable, "-c", _ABSENT, __file__, str(ROOT)], cwd=tmp_path, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DEFAULTS-REFUSE" in r.stdout, r.stdout
    counted = re.search(r"(\d+) passed", r.stdout)
    assert counted and int(counted.group(1)) >= 2 and "failed" not in r.stdout, r.stdout


@pytest.mark.parametrize("drop", ["drops", "never"])
def test_front_lock_held_until_the_front_is_the_gateway_uid(tmp_path, drop):
    """Group L3 r1 (codex MED 5), reproduced first (``1022-8/g1fix/item3_repro_before.txt``): ``front.lock`` was
    released as soon as the fork returned, while the child was still root and had not passed the wall, so a revoker
    taking the lock next swept a gateway uid the child was not yet in, wrote ``front-ended``, and the child entered
    the swept uid after it. Now render holds the lock until the child's real uid is the gateway uid; a child that
    stays root past the bound is refused ``front-start-unknown`` and killed."""
    world = World(tmp_path)
    s = _prepared(world)
    rec = Interlock(world, lambda ctx, slot: nullcontext(), lambda ctx, alloc: False)
    clock = [0.0]
    world.seams.monotonic = lambda: clock[0]
    world.seams.sleep = lambda dt: clock.__setitem__(0, clock[0] + dt)
    asked: list = []

    def identity(pid):
        uid, start = world.fronts.identity(pid) or (None, None)
        if uid is None:
            return None
        root = drop == "never" or len(asked) < 3          # the stage drops on the fourth look
        asked.append((rec.held, 0 if root else uid))
        return (0 if root else uid), start

    world.seams.proc_identity = identity
    code, line = world.main(["render", s], _blob())
    print("asked", asked, "events", rec.events, "line", line)
    if drop == "drops":
        assert line == {"ok": True}
        assert asked[:4] == [(True, 0), (True, 0), (True, 0), (True, asked[3][1])] and asked[3][1] != 0
        assert rec.events == [("lock", "e0"), ("read", True), ("spawn", True), ("unlock", "e0")]
    else:
        assert line == {"ok": False, "refused": "front-start-unknown"}
        assert all(held for held, _uid in asked[:-1]) and world.fronts.alive() == []   # killed (render's path)
        assert clock[0] >= H.FRONT_DROP_WAIT_S
