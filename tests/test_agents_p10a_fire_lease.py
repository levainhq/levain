"""P10a, the lease and the stop, through a REAL ``dream start`` fire (levain#121 L2, on the levain#127 step-2 rig).

``Install(tmp, stub=True)`` points the install fact ``claude_bin`` at a stub ``claude`` (``_p10a_stub.py``), so
``levain dream start`` (no spawn gate) runs the product's own chain: ``controller.spawn_fire`` -> the ``_fire`` process ->
claim -> ``DreamExecutor`` -> ``ClaudeMind.compose`` starts the stub as a real child that leads its own process group.
The stub sleeps, so the fire is alive and the child live while the ticks (``agents tick``, a faked ``P10A_NOW`` = real
now + an offset, the spawn gate ON so a tick starts no second fire) run against it.

Item (3), an expired heartbeat. The fire has no heartbeat of its own: the only renewer is the TICK's reaper. A claimed
row whose 900 s lease lapsed is not renewed and not taken over (the tick_driver docstring: left to the fire's own end);
at ``stop_at`` the reaper stops it. Item (4), the executor's stop at link expiry: the tick finds the row stale and stops
the fire through its identity record; the fire stops on SIGTERM and takes the stub's group down.

Observed on the real fire, each hand-run first (see levain's project_memory/1118-27_lanes/firelease_return.md):
a stop that lands inside ``compose`` settles a READABLE ZERO (``usage.json`` ``readable: true, actual_usd_micros: 0,
composes: []``; the reservation row ``settled/actual`` debiting 0). That contradicts the module's rule that a count that
could not be read is None, never 0. It is pinned below as a strict xfail; no product code is changed here.
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import signal
import subprocess
import time
from datetime import datetime, timedelta, timezone

import pytest

pytest.importorskip("cryptography")
from tests._p10a_rig import Install  # noqa: E402
from tests._p10a_stub import success_doc  # noqa: E402

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")

SLEEP_S = 40                 # how long the stub holds the compose child; every case ends it long before
NO_LINK_TEXT = ("levain dream start: no live signed link covers the dream's current spec (or the authority log could "
                "not be replayed), so nothing was claimed; the dream launches only under a live signed link\n")


def _at(minutes: float) -> str:
    """Real now + ``minutes``, as the ``P10A_NOW`` instant (the fire runs at the real clock; only a tick is faked)."""
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _say(tag: str, *what) -> None:
    print(f"[{tag}]", *what)


def _wait(pred, what: str, timeout: float = 90.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        got = pred()
        if got:
            return got
        time.sleep(0.25)
    raise AssertionError(f"timed out waiting for {what}")


class Fire:
    """One live dream fire on a stub-claude install, held by a sleeping stub. ``close`` always ends whatever is left."""

    def __init__(self, tmp_path, *, expires_in_min: float | None = None, sleep_s: float = SLEEP_S, reply=None) -> None:
        self.inst = inst = Install(tmp_path, stub=True)
        assert inst.enable("--time", "00:00").rc == 0
        inst.stub.sleep(sleep_s, 1)
        if reply is not None:
            inst.stub.reply(reply, 1)
        if expires_in_min is None:
            assert inst.sign(inst.link_doc()).rc == 0
            self.link = None
        else:                                         # signed into the log in the past, expiring in `expires_in_min`
            from levain.cockpit.consent import utc_stamp

            expires = utc_stamp(datetime.now(timezone.utc) + timedelta(minutes=expires_in_min))
            self.link = inst.sign_in_log_at(datetime(2026, 10, 26, 12, tzinfo=timezone.utc),
                                            inst.link_doc(expires_at=expires))
        started = inst.cli("dream", "start", spawn_gate=False)
        assert started.rc == 0 and "claimed and launched oc-" in started.out, started.text
        _wait(lambda: inst.stub.calls() >= 1 and inst.rows()[0]["pid"], "the fire to claim and run the stub")
        self.row = inst.rows()[0]
        self.oid = self.row["occurrence_id"]
        (pj,) = glob.glob(str(inst.anneal / "dream" / self.oid / "progress.json"))
        self.progress = _wait(lambda: self._composing(pj), "progress.json to name the compose child")

    @staticmethod
    def _composing(path):
        doc = json.loads(open(path, encoding="utf-8").read())
        return doc if doc.get("child_pid") else None

    @property
    def fire_pid(self) -> int:
        return self.row["pid"]

    @property
    def child(self) -> int:
        return self.progress["child_pid"]

    def now_row(self) -> dict:
        (row,) = self.inst.rows()
        return row

    def usage(self) -> dict:
        return json.loads((self.inst.anneal / "dream" / self.oid / "usage.json").read_text(encoding="utf-8"))

    def reservation(self) -> dict:
        (res,) = self.inst.rows(f"SELECT * FROM agent_reservations WHERE occurrence_id = '{self.oid}'")
        return res

    def slots(self) -> list[dict]:
        return self.inst.rows(f"SELECT * FROM agent_slots WHERE owner_id = '{self.oid}'")

    def group_alive(self) -> bool:
        try:
            os.killpg(self.child, 0)
        except ProcessLookupError:
            return False
        return True

    def close(self) -> None:
        for fn, pid in ((os.killpg, self.child), (os.kill, self.fire_pid)):
            try:
                fn(pid, signal.SIGCONT)
                fn(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def live(tmp_path):
    made: list[Fire] = []

    def make(**kw) -> Fire:
        made.append(Fire(tmp_path, **kw))
        return made[-1]

    yield make
    for f in made:
        f.close()


def _quiet_of_unconfirmed(err: str, oid: str) -> bool:
    """No stop-unconfirmed attention RAISED (the tick may print the 'attention resolved' line for it either way)."""
    return f"attention [agent-stop-unconfirmed:{oid}]" not in err


# ----------------------------------------------------------------------------------------------- (3) expired heartbeat
def test_a_lapsed_lease_is_not_renewed_nor_taken_over_and_stop_at_ends_the_fire(live):
    """The claimed row's lease (launched_at + 900 s) lapses; a tick 16 min on neither renews nor takes it over (the row is
    byte for byte as it was, the fire's slot still held). A tick 61 min on is past ``stop_at`` (3600 s): the reaper stops
    the fire, the stub's group goes, the row ends stopped/stop_at, the slot is freed and the reservation is settled."""
    f = live()
    held = f.row
    assert (held["state"], held["epoch"], held["launch_epoch"]) == ("launched", 0, 0) and held["pid"] == f.fire_pid
    assert len(f.slots()) == 1
    _say("start", {k: held[k] for k in ("state", "epoch", "pid", "launched_at", "lease_until")})

    lapse = f.inst.cli("agents", "tick", now=_at(16))
    _say("tick+16", lapse.rc, repr(lapse.err), {k: f.now_row()[k] for k in ("state", "epoch", "lease_until")})
    assert lapse.rc == 0 and "crash_recovered" not in lapse.err and _quiet_of_unconfirmed(lapse.err, f.oid)
    assert f.now_row() == held                       # not renewed (lease_until unchanged), not taken over, not fenced
    assert len(f.slots()) == 1 and f.group_alive()

    cut = f.inst.cli("agents", "tick", now=_at(61))
    ended = f.now_row()
    _say("tick+61", cut.rc, repr(cut.err), {k: ended.get(k) for k in ("state", "reason", "epoch", "ended_at")},
         f.reservation(), f.usage())
    assert ended["state"] == "stopped" and ended["reason"] == "stop_at" and ended["epoch"] == 0
    assert _quiet_of_unconfirmed(cut.err, f.oid)
    _wait(lambda: not f.group_alive(), "the stub's process group to end", timeout=15)
    assert f.slots() == []
    res = f.reservation()
    assert res["state"] == "settled" and res["reserved_usd_micros"] == 20_000_000
    assert f.inst.stub.calls() == 1                  # the stub was never run a second time


def test_a_fire_that_finishes_after_the_lapse_lands_done(live):
    """The stub finishes AFTER the lapse tick. Nothing raised the epoch, so the fire's own end is unfenced: it lands
    done/completed. (A lapsed lease not fencing a claimed fire is the designed behaviour: the tick_driver docstring.)"""
    from tests.test_dream_executor import _VALID

    f = live(sleep_s=8, reply=success_doc(_VALID, 0.02))
    held = f.row
    lapse = f.inst.cli("agents", "tick", now=_at(16))
    assert lapse.rc == 0 and f.now_row() == held, lapse.text
    row = _wait(lambda: (r := f.now_row())["state"] != "launched" and r, "the fire to end its own row", timeout=60)
    _say("fire end", {k: row.get(k) for k in ("state", "reason", "epoch")}, f.reservation(), f.usage())
    assert (row["state"], row["reason"], row["epoch"]) == ("done", "completed", 0)
    assert f.slots() == []
    res = f.reservation()
    assert res["state"] == "settled" and res["debited_usd_micros"] == 20_000
    assert f.usage()["readable"] is True and f.usage()["actual_usd_micros"] == 20_000


def test_a_stopped_fire_is_killed_but_its_unprovable_child_keeps_the_row_launched(live):
    """SIGSTOP the fire, then the stop_at tick. The SIGTERM stays pending; observed, ``stop()`` escalates inside its
    deadline: SIGKILL to the fire (identity and phase re-checked), so the FIRE ends, but the compose child is
    'not-confirmed' (the stub is an ``sh`` script: ``ps`` shows ``/bin/sh <stub> ...``, not the recorded argv, so no
    signal is sent to it). The row therefore stays launched, holding its slot, with an attention line naming how to end
    the child by hand. Once the child's group is ended, the next tick settles it. (Differs from the design text: the
    fire is not left alive pending SIGCONT; SIGCONT of a killed fire has nothing to resume.)"""
    f = live()
    held = f.row
    os.kill(f.fire_pid, signal.SIGSTOP)
    t0 = time.monotonic()
    cut = f.inst.cli("agents", "tick", now=_at(61))
    took = time.monotonic() - t0
    row = f.now_row()
    _say("stopped tick", round(took, 1), cut.rc, repr(cut.err), {k: row.get(k) for k in ("state", "reason", "epoch")})
    assert cut.rc == 0 and took < 60
    assert f"attention [agent-stop-unconfirmed:{f.oid}]: occurrence {f.oid}: asked to stop; not confirmed ended" in cut.err
    assert "SIGTERM to fire" in cut.err
    assert row == held and len(f.slots()) == 1       # still launched, same epoch, the slot held
    assert f.group_alive()                           # the child is not ended by the product here: we end it by hand

    os.killpg(f.child, signal.SIGKILL)               # what the attention line tells the operator to do
    _wait(lambda: not f.group_alive(), "the child's group to end", timeout=15)
    settle = f.inst.cli("agents", "tick", now=_at(62))
    row = f.now_row()
    _say("settle tick", settle.rc, repr(settle.err), {k: row.get(k) for k in ("state", "reason", "epoch")}, f.reservation())
    assert row["state"] == "stopped" and row["reason"] == "stop_at" and f.slots() == []
    res = f.reservation()                            # the SIGKILLed fire wrote no usage.json: unreadable, debited whole
    assert (res["state"], res["settled_reason"], res["debited_usd_micros"]) == ("settled", "unreadable", 20_000_000)
    assert _quiet_of_unconfirmed(settle.err, f.oid)


# ------------------------------------------------------------------------ (4) the executor's stop at link expiry
def test_the_executor_stops_the_fire_when_the_link_expires_mid_run(live):
    """The link (signed into the log long ago) expires 10 min from the real start. A tick at +15 min (under ``stop_at``)
    finds the row stale: the fire gets SIGTERM, the stub's group is gone, the row ends stopped/link_not_live, the
    dream-link-expired attention is printed, the slot is freed, and a later ``dream start`` is refused."""
    f = live(expires_in_min=10)
    link = f.link
    assert f.now_row()["link_id"] == link
    tick = f.inst.cli("agents", "tick", now=_at(15))
    row = f.now_row()
    _say("tick+15", tick.rc, repr(tick.err), {k: row.get(k) for k in ("state", "reason", "epoch")})
    assert row["state"] == "stopped" and row["reason"] == "link_not_live" and row["epoch"] == 0
    assert (f"attention [dream-link-expired:{link}]: the dream's link {link} expired at ") in tick.err
    assert "; no run launches until it is renewed or a new link is signed\n" in tick.err
    assert _quiet_of_unconfirmed(tick.err, f.oid)
    _wait(lambda: not f.group_alive(), "the stub's process group to end", timeout=15)
    _wait(lambda: not _alive(f.fire_pid), "the fire process to exit", timeout=15)
    assert f.slots() == [] and f.reservation()["state"] == "settled"
    refused = f.inst.cli("dream", "start", now=_at(16))
    assert refused.rc == 1 and refused.out == "" and refused.err == NO_LINK_TEXT
    assert len(f.inst.rows()) == 1 and f.inst.stub.calls() == 1


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True).stdout.strip() != ""


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="PRODUCT BUG (levain/dream/executor.py, observed on the real fire): FireStopRequested "
                   "escapes ClaudeMind.compose before ctx.composes.append, so _write_usage sees composes == [] and writes "
                   "readable: true, actual_usd_micros: 0, and the reservation settles 'actual' with a 0 debit: a stop "
                   "mid-compose is a readable zero, against the module's rule that an unread count is None, never 0")
def test_a_stop_mid_compose_does_not_settle_a_readable_zero(live):
    """The invariant: a run stopped while its ``claude -p`` child was live spent an UNKNOWN amount, so its usage is not a
    readable zero and the reservation is not settled at a 0 debit."""
    f = live(expires_in_min=10)
    assert f.inst.cli("agents", "tick", now=_at(15)).rc == 0
    assert f.now_row()["state"] == "stopped"
    usage, res = f.usage(), f.reservation()
    _say("usage", usage, res)
    assert not (usage["readable"] is True and usage["actual_usd_micros"] == 0)
    assert res["debited_usd_micros"] == res["reserved_usd_micros"]
