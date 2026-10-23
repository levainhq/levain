"""The reservation ledger under `kill -9` (S1 3c-1 session B2, design `1023-1/3c/C1_DESIGN.md` r4 §5 and §7: T7, and
T23's subprocess case). A driver process (`tests/b2_ledger_driver.py`) opens the REAL ledger file, runs to one named
gate, and is SIGKILLed there; the test then reopens the file and asserts the §5 invariants, each in its own test
function so a failure names the invariant:

  I1  no credit without the open's flush (the reopen's flush covers every byte replay read)
  I2  a resolution line that was in the file before the reopen is in it, unchanged, after
  I3  exactly one resolution per `reserved` line after the reopen, and none for anything else
  I4  nothing is sent without a `reserved` line in the file, and nothing sent is ever `released`

`kill -9` keeps the page cache, so W2 here is "written, not flushed" and not a power cut (design §8: undischarged)."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import List, Optional

import pytest

from levain.broker import keys as K
from levain.broker import ledger as L

ROOT = Path(__file__).resolve().parents[1]
DRIVER = str(ROOT / "tests" / "b2_ledger_driver.py")
SYS_PY = "/usr/bin/python3"
INTERPRETERS = [sys.executable] + ([SYS_PY] if os.path.exists(SYS_PY) and sys.executable != SYS_PY else [])
GATES = ["prelude-only", "w1", "w1-torn", "w2", "w3", "row3", "row4"]
GATE_WAIT_S = 30.0


def _env() -> dict:
    return {**os.environ, "PYTHONPATH": str(ROOT)}


class Run:
    """One killed driver: the ledger path, the file's bytes just after the kill, and the events the driver wrote."""

    def __init__(self, d: str, py: str, mode: str, path: Optional[str] = None) -> None:
        self.d, self.py, self.mode = d, py, mode
        self.path = path or os.path.join(d, "keys", "ledger.jsonl")
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        self.events_path = os.path.join(d, "events-%s.jsonl" % mode)
        self.before = b""

    def events(self) -> List[dict]:
        if not os.path.exists(self.events_path):
            return []
        with open(self.events_path, "rb") as f:
            return [json.loads(x) for x in f.read().splitlines()]

    def kill_at_gate(self) -> None:
        p = subprocess.Popen([self.py, DRIVER, self.mode, self.path, self.events_path], env=_env(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            end = time.monotonic() + GATE_WAIT_S
            while not any(e.get("at") == "gate" for e in self.events()):
                assert p.poll() is None, "the driver exited before its gate: " + p.stderr.read().decode()
                assert time.monotonic() < end, "the driver never reached its gate: %r" % self.events()
                time.sleep(0.02)
            p.send_signal(signal.SIGKILL)
            p.wait(10)
            assert p.returncode == -signal.SIGKILL
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stderr.close()
        with open(self.path, "rb") as f:
            self.before = f.read()


def _lines(raw: bytes) -> List[dict]:
    return [json.loads(x) for x in raw.split(b"\n") if x]


def _complete(raw: bytes) -> bytes:
    """`raw` up to and including its last newline: replay's torn-tail rule."""
    i = raw.rfind(b"\n")
    return raw[: i + 1]


class Reopen:
    """The in-process reopen with `_flush` wrapped: every call's file size at the call and whether it returned."""

    def __init__(self, path: str, fail_call: int = 0) -> None:
        self.path, self.fail_call = path, fail_call
        self.calls: List[dict] = []
        self.error: Optional[L.LedgerOpenError] = None
        self.raw = b""
        self.ledger_id = ""

    def run(self) -> "Reopen":
        real = L._flush
        me = self

        def flush(fd: int) -> None:
            rec = {"size": os.fstat(fd).st_size, "returned": False}
            me.calls.append(rec)
            if me.fail_call and len(me.calls) == me.fail_call:
                raise OSError(5, "injected EIO")
            real(fd)
            rec["returned"] = True

        async def go() -> None:
            L._flush = flush
            try:
                led = L.Ledger.open(self.path)
            except L.LedgerOpenError as e:
                self.error = e
                return
            finally:
                L._flush = real
            self.open_returned_with = [c["returned"] for c in self.calls]
            self.ledger_id = led.ledger_id
            led.attach(asyncio.get_running_loop())
            await led.aclose(5.0)

        asyncio.run(go())
        with open(self.path, "rb") as f:
            self.raw = f.read()
        return self


@pytest.fixture(params=INTERPRETERS, ids=lambda p: "usr-bin-python3" if p == SYS_PY else "venv")
def interpreter(request):
    return request.param


def _killed_reopen(py: str, mode: str):
    """Kill a driver at `mode`'s gate, reopen its file in-process: `(bytes just after the kill, the driver's events,
    the Reopen)`. The temp directory is gone when it returns, so everything the assertions use is copied out."""
    d = tempfile.mkdtemp(prefix="b2k", dir="/tmp")
    try:
        run = Run(d, py, mode)
        run.kill_at_gate()
        events = run.events()
        re = Reopen(run.path).run()
        assert re.error is None, re.error.line
        return run.before, events, re
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- the per-invariant assertions -------------------------------------------------------------------------------

def check_i2(before: bytes, after: bytes) -> None:
    """Every complete line that was in the file before the reopen is in it, byte for byte, in place."""
    assert after.startswith(_complete(before)), "a line present before the reopen changed or went"


def check_i3(after: bytes) -> None:
    lines = _lines(after)
    reserved = [o["seq"] for o in lines if o["kind"] == "reserved"]
    res = [o["ref"] for o in lines if o["kind"] in ("released", "settled", "uncertain")]
    assert sorted(res) == sorted(set(res)), "a ref has two resolutions: %r" % res
    assert sorted(res) == sorted(reserved), "resolutions %r do not match reserved %r" % (res, reserved)


def check_i4(after: bytes, events: List[dict]) -> None:
    lines = _lines(after)
    reserved = {o["seq"] for o in lines if o["kind"] == "reserved"}
    released = {o["ref"] for o in lines if o["kind"] == "released"}
    sent = {e["ref"] for e in events if e["at"] == "sent"}
    assert sent <= reserved, "a hold was sent with no `reserved` line in the file: %r" % (sent - reserved)
    assert not (sent & released), "a sent hold was released: %r" % (sent & released)


def _res(after: bytes) -> dict:
    return {o["ref"]: (o["kind"], o.get("reason", o.get("settled"))) for o in _lines(after)
            if o["kind"] in ("released", "settled", "uncertain")}


@pytest.mark.parametrize("mode", GATES)
def test_i2_a_durable_resolution_is_never_rewritten(interpreter, mode):
    before, events, re = _killed_reopen(interpreter, mode)
    check_i2(before, re.raw)
    # the two resolutions the prelude waited on (RESOLVED, so flushed) survive as written
    done = [e["ref"] for e in events if e.get("at") == "resolved"]
    assert len(done) == 2
    assert all(r in _res(re.raw) for r in done)
    assert _res(re.raw)[done[0]] == ("settled", 1000) and _res(re.raw)[done[1]] == ("released", None)


@pytest.mark.parametrize("mode", GATES)
def test_i3_exactly_one_resolution_per_ref(interpreter, mode):
    before, events, re = _killed_reopen(interpreter, mode)
    check_i3(re.raw)


@pytest.mark.parametrize("mode", GATES)
def test_i4_nothing_unsent_is_sent_and_nothing_is_sent_without_a_durable_reserved(interpreter, mode):
    before, events, re = _killed_reopen(interpreter, mode)
    check_i4(re.raw, events)
    sent = {e["ref"] for e in events if e["at"] == "sent"}
    assert sent, "the driver sent nothing: the test would pass vacuously"
    # a `settled` line belongs to a sent hold only
    for ref, (kind, why) in _res(re.raw).items():
        if kind == "settled":
            assert ref in sent


# ---- what each gate leaves, named -------------------------------------------------------------------------------

@pytest.mark.parametrize("mode,want", [
    ("w1", ("uncertain", "restart")),        # the `released` line never reached the file: the hold is booked sent
    ("w1-torn", ("uncertain", "restart")),   # half a line: truncated, so the same
    ("row3", ("uncertain", "restart")),      # `reserved` written, its flush blocked, the cancel's `released` unwritten
    ("row4", ("uncertain", "restart")),      # sent, so booked at its full amount
    ("w2", ("released", None)),              # written, not flushed: kill -9 keeps the page cache, replay credits it
    ("w3", ("released", None)),              # flushed, the callback never ran: the file is ahead of memory
])
def test_the_gate_leaves_the_named_line(interpreter, mode, want):
    before, events, re = _killed_reopen(interpreter, mode)
    refs = [o["seq"] for o in _lines(re.raw) if o["kind"] == "reserved"]
    assert len(refs) == 3, refs                               # h0, h1, and the gated hold
    assert _res(re.raw)[refs[-1]] == want, (mode, _res(re.raw))
    if mode == "w1-torn":
        assert not before.endswith(b"\n"), "the kill did not tear the line"
        assert re.raw.endswith(b"\n")
    if mode == "row3":
        assert not any(e["at"] == "sent" and e["ref"] == refs[-1] for e in events)
        # the kill landed AFTER the driver cancelled the owner and saw its hold RESOLVING with the release queued
        ats = [e["at"] for e in events]
        assert "owner-cancelled" in ats, "the owner never saw the cancel: %r" % ats
        assert ats.index("blocked") < ats.index("cancelled") < ats.index("gate"), ats
        assert [e for e in events if e["at"] == "gate"][-1]["state"] == "RESOLVING", events


# ---- I1: the reopen's flush covers every byte replay read ----------------------------------------------------------

@pytest.mark.parametrize("mode", ["w2", "w3"])
def test_i1_the_reopen_credits_a_surviving_resolution_only_behind_its_flush(interpreter, mode):
    d = tempfile.mkdtemp(prefix="b2k", dir="/tmp")
    try:
        run = Run(d, interpreter, mode)
        run.kill_at_gate()
        copy_dir = os.path.join(d, "copy", "keys")
        os.makedirs(copy_dir, mode=0o700)
        copy = os.path.join(copy_dir, "ledger.jsonl")
        shutil.copy2(run.path, copy)
        read_bytes = len(_complete(run.before))
        re = Reopen(run.path).run()
        assert re.error is None, re.error.line
        # the last flush an open makes is `flush-opened`: made after the batch was written (the file had grown past
        # everything replay read) and returned OK before `open()` did
        last = re.calls[-1]
        assert last["returned"] is True
        assert last["size"] > read_bytes
        assert all(c["returned"] for c in re.calls)
        # a second reopen on the copy with `flush-opened` made to raise refuses the open: no credit is granted
        n_ok = len(re.calls)
        bad = Reopen(copy, fail_call=n_ok).run()
        assert bad.error is not None and "flush-opened" in bad.error.line, bad.error and bad.error.line
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- during replay ------------------------------------------------------------------------------------------------

def test_a_kill_during_replay_leaves_a_prefix_the_next_replay_finishes(interpreter):
    d = tempfile.mkdtemp(prefix="b2k", dir="/tmp")
    try:
        seed = Run(d, interpreter, "replay-seed")
        seed.kill_at_gate()
        assert [o["kind"] for o in _lines(seed.before)].count("reserved") == 5   # the prelude's two, the three left open
        gated = Run(d, interpreter, "replay-gate", path=seed.path)
        gated.kill_at_gate()
        # replay's batch was written and not flushed: the page cache keeps it
        mid = _lines(gated.before)
        assert [o["kind"] for o in mid].count("opened") == 2
        events = seed.events()
        re = Reopen(seed.path).run()
        assert re.error is None, re.error.line
        check_i2(gated.before, re.raw)
        check_i3(re.raw)
        check_i4(re.raw, events)
        epochs = [o["epoch"] for o in _lines(re.raw) if o["kind"] == "opened"]
        assert len(epochs) == len(set(epochs)) == 3, "a replay reused an epoch"
        assert sorted(_res(re.raw).values(), key=str) == sorted(
            [("settled", 1000), ("released", None)] + [("uncertain", "restart")] * 3, key=str)
        # no restart line twice: the third open resolved only what was still open
        assert [o["kind"] for o in _lines(re.raw)].count("uncertain") == 3
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ---- T23's subprocess case: the process exits though the writer's flush never returns -------------------------------

def test_t23_a_blocked_flush_does_not_keep_the_process_alive_and_the_lock_is_free_after(interpreter):
    d = tempfile.mkdtemp(prefix="b2k", dir="/tmp")
    try:
        run = Run(d, interpreter, "close")
        p = subprocess.Popen([interpreter, DRIVER, "close", run.path, run.events_path, str(K.SHUTDOWN_WAIT_S)], env=_env(),
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        try:
            # the parent's own `time.monotonic()` stamps every duration it asserts on: a monotonic clock is not
            # comparable across interpreters (the 3.9 macOS build's differs from 3.12's), so the driver's stamps
            # are used only as in-process differences (`took`)
            end_wait = time.monotonic() + 60
            while not any(e.get("at") == "aclose-start" for e in run.events()):
                assert p.poll() is None, "the driver exited before aclose: " + p.stderr.read().decode()
                assert time.monotonic() < end_wait, "the driver never reached aclose: %r" % run.events()
                time.sleep(0.01)
            t_start = time.monotonic()
            rc = p.wait(60)
            t_exit = time.monotonic()
            err = p.stderr.read().decode()
        finally:
            if p.poll() is None:
                p.kill()
                p.wait()
            p.stderr.close()
        assert rc == 0, err
        ev = run.events()
        end = [e for e in ev if e["at"] == "aclose-end"][0]
        # aclose returned by its drain deadline, with the writer still blocked in its flush
        assert end["took"] <= K.SHUTDOWN_WAIT_S + 1.0, end
        assert end["took"] >= K.SHUTDOWN_WAIT_S - 0.5, "aclose returned early: the writer was not blocked (%r)" % end
        # and the process left within SHUTDOWN_WAIT_S + 3 s of the parent seeing the `aclose` call start (the margin
        # holds interpreter exit with a blocked daemon thread on a loaded CI box)
        assert t_exit - t_start <= K.SHUTDOWN_WAIT_S + 3.0, (t_exit - t_start, end)
        # the hold was resolved by the close column even though its line could not be flushed
        assert end["states"] in (["RESOLVING"], ["RESOLVED"]), end
        # the dead process holds no lock: a reopen gets it, and replays what landed
        re = Reopen(run.path).run()
        assert re.error is None, re.error.line
        check_i3(re.raw)
    finally:
        shutil.rmtree(d, ignore_errors=True)
