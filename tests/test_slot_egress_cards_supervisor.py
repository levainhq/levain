"""Host cards, slice S4b (board levain#66): the supervisor wiring of ``levain.slot.egress_cards``.

Three layers:
  - the lane (``supervisor._CardLane``) over a scripted run and a real sign queue: the one-open-card scan, the
    start bound, the probe exclusion, the delivery's priority over a read, the exception wall;
  - the real relay against a FAKE runtime helper script (the V13 / FX3 rig) that answers the three host-grant verbs and
    keeps root's session TRY lock for ``probe`` and ``egress-grant``, so an overlap shows as ``session-busy`` exactly as
    it would on root: T14 (the relay never blocks), T15 (no terminal write mid-relay), T16 (both lanes, darwin), T17
    (end of session), T22 (an exception), T24 (no phone: the notification center), T26 (probe and grant never overlap);
  - T20: a REAL ``ssh-keygen`` signature over the card the lane filed, through the REAL ``egress_grant`` handler.

What only his phone, his passkey and a real host show is not here.
"""
from __future__ import annotations

import hashlib
import json
import os
import signal
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

import pytest

from levain.consent import sign
from levain.consent.cockpit_origin import write_origin
from levain.slot import egress_cards as EC
from levain.slot import egress_grants as eg
from levain.slot import supervisor as SV
from tests.test_slot_s2fx3_supervisor import FX3_HELPER, gone, rig as fx3_rig
from tests.test_slot_s2s_supervisor import SESSION, bounded, isolated_git  # noqa: F401

ORIGIN = "https://cockpit.example.net"
FP = "SHA256:" + "A" * 43
HOST = "files.example.org"
STORE = "store-abc12345"

PRELUDE = r'''
import atexit, fcntl
ivl = os.environ.get("FAKE_IVL")
def _ivl(kind):
    if ivl:
        with open(ivl, "a") as f:
            f.write(json.dumps({"verb": verb, "pid": os.getpid(), kind: time.time()}) + "\n")
_ivl("t0")
atexit.register(lambda: _ivl("t1"))
def _count(name):
    p = os.path.join(store, name)
    n = int(open(p).read()) if os.path.exists(p) else 0
    with open(p, "w") as f:
        f.write(str(n + 1))
    return n
def _lock():
    fd = os.open(os.path.join(store, "session.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)      # root's session lock is a TRY lock
    except OSError:
        sys.stderr.write("levain runtime-helper: refused session-busy: the session lock is held\n")
        line({"ok": False, "refused": "session-busy"})
        sys.exit(1)
    return fd
_held = _lock() if verb == "probe" else None
if verb in ("egress-grants-list", "egress-refusals", "egress-grant"):
    with open(os.path.join(store, "pid-" + verb), "w") as f:
        f.write(str(os.getpid()))
    if verb == "egress-grants-list":
        if os.environ.get("FAKE_LIST_REFUSE"):
            line({"ok": False, "refused": os.environ["FAKE_LIST_REFUSE"]})
            sys.exit(1)
        line({"ok": True, "store": "store-abc12345", "entries": json.loads(os.environ.get("FAKE_LIST", "[]"))})
        sys.exit(0)
    if verb == "egress-refusals":
        n = _count("refusals")
        batches = json.loads(os.environ.get("FAKE_REFUSALS", "[]"))
        slow_from = int(os.environ.get("FAKE_REFUSALS_SLOW_FROM", "0"))
        if slow_from and n + 1 >= slow_from:
            open(os.path.join(store, "refusing"), "w").close()
            time.sleep(float(os.environ["FAKE_REFUSALS_SLOW"]))
        line({"ok": True, "hosts": batches[n] if n < len(batches) else [], "cursor": "0"})
        sys.exit(0)
    if verb == "egress-grant":
        _held = _lock()
        data = sys.stdin.buffer.read()
        with open(os.path.join(store, "grant-stdin"), "wb") as f:
            f.write(data)
        open(os.path.join(store, "granting"), "w").close()
        mode = os.environ.get("FAKE_GRANT", "ok")
        if mode.startswith("slow:"):
            time.sleep(float(mode[5:]))
            mode = "ok"
        while mode == "hang":
            time.sleep(0.05)
        if mode != "ok":
            line({"ok": False, "refused": mode})
            sys.exit(1)
        body = json.loads(data)
        fields = dict(ln.split(": ", 1) for ln in body["message"].splitlines()[1:])
        line({"ok": True, "grant": fields["grant"], "host": fields["host"], "expires": fields["expires-session"],
              "choice": body["choice"]})
        sys.exit(0)
'''
CARD_HELPER = FX3_HELPER.replace('hang = os.environ.get("FAKE_HANG", "")', PRELUDE + 'hang = os.environ.get("FAKE_HANG", "")', 1)
assert CARD_HELPER != FX3_HELPER


# --- helpers ---------------------------------------------------------------------------------------------------------

def wait_for(cond: Callable[[], Any], seconds: float = 20.0, step: float = 0.02) -> Any:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        got = cond()
        if got:
            return got
        time.sleep(step)
    return None


def card_files(queue: Path) -> List[Path]:
    d = queue / "sign"
    return sorted(p for p in d.glob("lv-*.json") if not p.name.endswith(".signed.json")) if d.is_dir() else []


def tap_card(queue: Path, choice: str = "s", sig: str = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n",
             fp: str = FP) -> str:
    """The Signer's output for the newest card, written by hand in its shape (S4a's R3 ran the real Signer and found this
    record): ``<rid>.sig`` first, then ``<rid>.signed.json`` (the completion marker)."""
    path = card_files(queue)[-1]
    rid = path.stem
    message = json.loads(path.read_text())["message"]
    signed = eg.signed_bytes(message, choice)
    (queue / "sign" / f"{rid}.sig").write_text(sig)
    rec = {"choice": choice, "message_sha256": hashlib.sha256(signed).hexdigest(), "namespace": EC.NAMESPACE,
           "public_key": "ssh-ed25519 AAAA", "signed_at": "2026-11-16T12:00:00Z", "signed_message": signed.decode(),
           "signer_fp": fp}
    (queue / "sign" / f"{rid}.signed.json").write_text(json.dumps(rec))
    return rid


def intervals(path: Path) -> Dict[str, List[List[float]]]:
    """verb -> [[t0, t1], ...] from the fake helper's interval log (a run that was killed has no t1: its end is None)."""
    runs: Dict[int, Dict[str, Any]] = {}
    for ln in path.read_text().splitlines() if path.exists() else []:
        row = json.loads(ln)
        runs.setdefault(row["pid"], {"verb": row["verb"]}).update({k: v for k, v in row.items() if k in ("t0", "t1")})
    out: Dict[str, List[List[float]]] = {}
    for r in runs.values():
        out.setdefault(r["verb"], []).append([r["t0"], r.get("t1")])
    return out


def overlap(a: List[Optional[float]], b: List[Optional[float]]) -> bool:
    end_a, end_b = a[1] if a[1] is not None else float("inf"), b[1] if b[1] is not None else float("inf")
    return a[0] < end_b and b[0] < end_a


@pytest.fixture
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    """The flow's cadences made short so a real relay shows a whole card in seconds (the logic is unchanged: the
    bounds that matter, 480 / 570 / 3 attempts, are not touched)."""
    for name, val in (("POLL_S", 0.3), ("POLL_SLOW_S", 0.5), ("LIST_S", 5.0), ("ANSWER_POLL_S", 0.2),
                      ("SCAN_RETRY_S", 0.3), ("RETRY_AFTER_S", (0.3, 0.3))):
        monkeypatch.setattr(EC, name, val)
    monkeypatch.setattr(SV, "CARD_STEP_S", 0.05)
    monkeypatch.setattr(SV, "CARD_RP_S", 0.2)


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, darwin: bool = False, every: float = 0.4,
                 origin: bool = True, **env: str) -> None:
        self.queue = tmp_path / "queue"
        self.queue.mkdir()
        if origin:
            write_origin(self.queue, ORIGIN)
        self.ivl = tmp_path / "intervals.jsonl"
        self.r = fx3_rig(tmp_path, monkeypatch, FAKE_IVL=str(self.ivl), **env)
        self.r.helper.write_text(CARD_HELPER)
        self.r.seams.allow_dir = self.queue
        if darwin:
            self.r.seams.host = "darwin"
            self.r.seams.reprobe_s = every
        self.pushes: List[Dict[str, Any]] = []

        def spawn(queue_dir, **kw):                                   # type: ignore[no-untyped-def]
            self.pushes.append({"queue": str(queue_dir), **kw})
            return True
        monkeypatch.setattr("levain.webpush.sender.spawn", spawn)

    @property
    def store(self) -> Path:
        return self.r.store

    def verbs(self) -> List[str]:
        return [x["verb"] for x in self.r.verbs()]

    def stop_when(self, cond: Callable[[], Any], then: float = 0.3, seen: Optional[dict] = None) -> threading.Thread:
        def go() -> None:
            wait_for(cond, 40)
            time.sleep(then)
            if seen is not None:
                seen["err"] = self.r.stderr()
            os.kill(os.getpid(), signal.SIGTERM)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    def tap_when_filed(self, choice: str = "s", then: Optional[Callable[[], Any]] = None,
                       gate: Optional[Callable[[], Any]] = None) -> threading.Thread:
        def go() -> None:
            if wait_for(lambda: card_files(self.queue), 40):
                time.sleep(0.2)
                if gate is not None:
                    wait_for(gate, 40)               # e.g. a probe is running: the tap lands while it holds the lock
                tap_card(self.queue, choice)
                if then is not None:
                    then()
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t


def one_host(**env: str) -> Dict[str, str]:
    return {"FAKE_START": "wait", "FAKE_REFUSALS": json.dumps([[HOST]]), **env}


# --- the constants ---------------------------------------------------------------------------------------------------

def test_the_deadlines_and_the_wall_seam() -> None:
    assert SV.HELPER_DEADLINE_S["egress-grant"] == 30.0
    assert SV.HELPER_DEADLINE_S["egress-refusals"] == 15.0 and SV.HELPER_DEADLINE_S["egress-grants-list"] == 15.0
    assert SV.HELPER_DEADLINE_S["probe"] == 45.0
    s = SV.Seams(host="linux", helper=("x",))
    assert s.wall is time.time and s.clock is time.monotonic and s.cards is True
    # B2: a started delivery is ended by issued + 570 + 30 = 600 = root's FRESH_S
    assert EC.START_CUTOFF_S + SV.HELPER_DEADLINE_S["egress-grant"] == eg.FRESH_S


# --- the lane, over a scripted run and a real queue -------------------------------------------------------------------

class FakeRun:
    """What :class:`supervisor._CardRun` is to the lane: a verb, its args and stdin, and a scripted end."""

    made: List["FakeRun"] = []

    def __init__(self, sup: Any, verb: str, args: List[str], data: Optional[bytes], now: float) -> None:
        self.verb, self.args, self.data = verb, args, data
        self.open: List[int] = []
        self.wfd = -1
        self.ended = False
        FakeRun.made.append(self)

    def pump(self, ready: Any, writable: Any = ()) -> None:
        pass

    def outcome(self, now: float) -> Any:
        return SCRIPT[0](self)

    def end(self) -> None:
        self.ended = True


SCRIPT: List[Callable[[FakeRun], Any]] = [lambda run: None]


class FakeSup:
    def __init__(self, session: str = SESSION, slot: int = 0) -> None:
        self.session, self._slot_index = session, slot
        self.mono, self.wallv = 1000.0, 1_800_000_000.0
        self.s = SimpleNamespace(clock=lambda: self.mono, wall=lambda: self.wallv,
                                 helper_deadline_s=dict(SV.HELPER_DEADLINE_S), helper=("fake",), env=None)

    def tick(self, seconds: float = 0.5) -> None:
        self.mono += seconds
        self.wallv += seconds


@pytest.fixture
def lane_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    FakeRun.made = []
    SCRIPT[:] = [lambda run: None]
    monkeypatch.setattr(SV, "_CardRun", FakeRun)
    monkeypatch.setattr(SV, "CARD_STEP_S", 0.0)
    monkeypatch.setattr(SV, "CARD_RP_S", 0.0)
    monkeypatch.setattr("levain.webpush.sender.spawn", lambda *a, **k: True)
    queue = tmp_path / "q"
    queue.mkdir()
    write_origin(queue, ORIGIN)
    sup = FakeSup()
    lane = SV._CardLane(sup, EC.CardFlow(session=SESSION, slot=0), queue)
    lane._feed(EC.Listed(STORE, ()))
    return sup, lane, queue


def answer_script(refusals: List[List[str]], listed: Optional[Callable[[], tuple]] = None) -> None:
    """Every scripted run answers at once: ``egress-refusals`` from the batches (then none), the list empty."""
    batches = list(refusals)

    def go(run: FakeRun) -> Any:
        if run.verb == EC.REFUSALS:
            return None, {"hosts": batches.pop(0) if batches else [], "cursor": "0"}
        if run.verb == EC.GRANTS_LIST:
            return None, {"store": STORE, "entries": []}
        return "egress-grant-not-active", {}
    SCRIPT[:] = [go]


def drive(sup: FakeSup, lane: Any, seconds: float, probe_busy: bool = False, each: Optional[Callable[[], Any]] = None) -> None:
    end = sup.mono + seconds
    while sup.mono < end:
        sup.tick(0.5)
        lane.step(probe_busy)
        lane.pump([], [])
        if each:
            each()


def test_t2_the_scan_sees_an_open_card_anywhere_and_ignores_the_rest(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    now = datetime.now(timezone.utc)
    sup.wallv = now.timestamp()
    assert lane._scan() == EC.Scanned(False)                       # no sign dir yet
    msg = eg.build_message(STORE, eg.new_grant_id(), "s-" + "ab" * 8, 1, HOST, now, now.replace(year=now.year + 1), "x.example")
    rid = sign.file_levain_request(queue, EC.NAMESPACE, msg, title="t", ttl_s=480)
    assert lane._scan() == EC.Scanned(True)                        # another session's open card counts
    (queue / "sign" / f"{rid}.denied").write_text("")
    assert lane._scan() == EC.Scanned(False)                       # denied: nothing is asked of him
    (queue / "sign" / f"{rid}.denied").unlink()
    sup.wallv += 481                                               # past its ttl and out of the mtime window
    assert lane._scan() == EC.Scanned(False)
    # something that is not a card file name is never read
    (queue / "sign" / "notes.json").write_text("{}")
    assert lane._scan() == EC.Scanned(False)


def test_t2_a_second_card_is_never_filed_while_one_is_open(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    now = datetime.now(timezone.utc)
    sup.wallv = now.timestamp()
    msg = eg.build_message(STORE, eg.new_grant_id(), "s-" + "ab" * 8, 1, "other.example.org", now,
                           now.replace(year=now.year + 1), "x.example")
    sign.file_levain_request(queue, EC.NAMESPACE, msg, title="t", ttl_s=480)
    answer_script([[HOST]])
    drive(sup, lane, 20)
    assert len(card_files(queue)) == 1                             # only the other session's card
    assert lane.flow.hosts[HOST].state == EC.WAITING               # it waits; nothing was filed for it


def test_a_card_is_filed_when_none_is_open_and_the_push_is_recorded_not_only_sent(lane_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    sent: List[Dict[str, Any]] = []
    monkeypatch.setattr("levain.webpush.sender.spawn", lambda q, **kw: sent.append(kw) or True)
    answer_script([[HOST]])
    drive(sup, lane, 10)
    assert len(card_files(queue)) == 1 and len(sent) == 1
    p = sent[0]
    rid = card_files(queue)[0].stem
    assert p["kind"] == "attention" and p["ref"] == rid and p["push"] is False      # no phone is live: recorded only
    assert p["data"]["sign_id"] == rid and p["url"] == f"{ORIGIN}/sign/{rid}"
    assert HOST in p["title"] and "send anything it can read to any account" in p["body"]
    assert "did not write" not in p["body"]                                          # CA2: no authorship claim


def test_t4_no_delivery_starts_after_issued_plus_570(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    answer_script([[HOST]])
    drive(sup, lane, 6)
    card = lane.flow.card
    assert card is not None and card.stage == "open"
    tap_card(queue)
    drive(sup, lane, 1.0)                                                           # the answer is read, a delivery starts
    assert [r.verb for r in FakeRun.made if r.verb == EC.GRANT] == [EC.GRANT]
    # a delivery waiting on the probe past its start bound is not started: the flow is told, no process is made
    FakeRun.made.clear()
    lane.run = None
    lane.pending = (EC.GRANT, [SESSION], b"{}", sup.wallv + 1.0)
    sup.wallv += 5.0
    lane.step(False)
    assert lane.pending is None and FakeRun.made == []


def test_a_delivery_never_starts_while_the_probe_is_in_flight_and_goes_first_when_it_ends(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    lane.pending = (EC.GRANT, [SESSION], b"{}", sup.wallv + 500)
    assert lane.delivering
    for _ in range(5):
        lane.step(True)
    assert FakeRun.made == [] and lane.pending is not None
    lane.step(False)
    assert [r.verb for r in FakeRun.made] == [EC.GRANT] and lane.pending is None and lane.delivering


def test_a_signed_answer_ends_a_read_in_flight_and_its_delivery_is_next(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    answer_script([[HOST]])
    drive(sup, lane, 6)                                                             # the card is filed and open
    assert lane.flow.card is not None and lane.flow.card.stage == "open"
    SCRIPT[:] = [lambda run: None]                                                  # every read now hangs
    for _ in range(20):
        sup.tick(0.5)
        lane.step(False)
        lane.pump([], [])
        if lane.run is not None and lane.run.verb != EC.GRANT:
            break
    read = lane.run
    assert read is not None and read.verb in (EC.REFUSALS, EC.GRANTS_LIST) and not read.ended
    tap_card(queue)                                                                 # he taps while the read is in flight
    for _ in range(20):                                                           # the answer is read on the next ticks
        sup.tick(0.5)
        lane.step(False)
        if lane.run is not None and lane.run.verb == EC.GRANT:
            break
    assert read.ended and lane.run is not None and lane.run.verb == EC.GRANT


def test_the_lane_never_raises_into_the_relay(lane_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    sup, lane, queue = lane_env

    def boom(self: Any, event: object, wall: float, mono: float) -> Any:
        raise RuntimeError("boom")
    monkeypatch.setattr(EC.CardFlow, "advance", boom)
    lane.step(False)
    lane.step(False)
    assert lane.dead and lane.run is None
    notes = lane.notes()
    assert sum("host cards stopped for this session" in n for n in notes) == 1 and "RuntimeError" in " ".join(notes)


def test_a_helper_name_that_is_not_root_is_not_taken_for_a_refusal(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    answer_script([[HOST]])
    drive(sup, lane, 6)
    tap_card(queue)
    SCRIPT[:] = [lambda run: ("helper-timeout", {}) if run.verb == EC.GRANT else (None, {"hosts": [], "cursor": "0"})]
    drive(sup, lane, 3)
    h = lane.flow.hosts[HOST]
    assert lane.flow.card is not None and lane.flow.card.unknown       # an ended delivery: the outcome is unknown, retried
    assert "helper-timeout" not in " ".join(lane.notes())


def _lock_free(queue: Path) -> bool:
    import fcntl

    fd = os.open(queue / "sign" / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except BlockingIOError:
        return False
    finally:
        os.close(fd)


def test_l3_two_sessions_on_one_queue_cannot_both_file_a_card(lane_env: Any) -> None:
    """codex HIGH 178 / complement LOW 42: the ONE RULE is check-then-act. Session A's scan found nothing and is about
    to file; session B scans in that window. B must not read "nothing open"; once A has filed, B sees A's card."""
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    other = SV._CardLane(FakeSup(session="s-" + "cd" * 8, slot=1), EC.CardFlow(session="s-" + "cd" * 8, slot=1), queue)
    other.sup.wallv = sup.wallv
    assert lane._scan() == EC.Scanned(False)                       # A: nothing open; A holds the queue until it files
    assert other._scan() == EC.Scanned(True)                       # B in the window: a card may be open, it waits
    now = datetime.now(timezone.utc)
    msg = eg.build_message(STORE, eg.new_grant_id(), SESSION, 0, HOST, now, now.replace(year=now.year + 1), "x.example")
    assert isinstance(lane._file(EC.File(msg, "t", 480.0)), EC.Filed)
    assert _lock_free(queue)                                       # the filing let the lock go
    assert other._scan() == EC.Scanned(True)                       # and B now sees A's card
    assert _lock_free(queue)


def test_l3_the_queue_lock_never_outlives_a_feed(lane_env: Any) -> None:
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    answer_script([[HOST]])
    drive(sup, lane, 6, each=lambda: None if _lock_free(queue) else pytest.fail("the queue lock was left held"))
    [card] = card_files(queue)
    (queue / "sign" / f"{card.stem}.denied").write_text("")      # nothing open: a scan now holds the lock
    lane.flow = EC.CardFlow(session=SESSION, slot=0)                # rp unknown: a feed that files nothing
    assert lane._scan() == EC.Scanned(False) and not _lock_free(queue)
    lane._feed(EC.Tick())
    assert _lock_free(queue)
    assert lane._scan() == EC.Scanned(False) and not _lock_free(queue)
    lane.end()
    assert _lock_free(queue)


def test_l3_a_damaged_queue_file_does_not_end_the_lane(lane_env: Any) -> None:
    """deepseek MED 63 / 81: a malformed ``lv-*.json`` in the scan, and a damaged answer to the lane's own card. Neither
    ``read_request`` nor ``state_of`` raises for a file's contents (the first returns a refused request, the second
    only asks whether the answer files exist), so the lane goes on: the scan counts nothing for the junk, the card is
    filed, and a damaged answer is the host's 'unreadable' note."""
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    d = queue / "sign"
    d.mkdir()
    (d / ("lv-" + "1" * 32 + ".json")).write_text("{not json")
    (d / ("lv-" + "2" * 32 + ".json")).mkdir()
    (d / ("lv-" + "3" * 32 + ".json")).write_bytes(b"\xff\xfe" * 10)
    (d / ("lv-" + "1" * 32 + ".signed.json")).write_text("junk")
    answer_script([[HOST]])
    drive(sup, lane, 6)
    assert not lane.dead and lane.flow.card is not None and lane.flow.card.stage == "open"
    rid = lane.flow.card.rid
    (d / f"{rid}.sig").mkdir()                                      # not a regular file
    (d / f"{rid}.signed.json").write_text("{damaged")
    drive(sup, lane, 3)
    assert not lane.dead and lane.flow.hosts[HOST].outcome == "unreadable"


def test_l3_a_transient_filing_failure_files_the_card_once_the_queue_recovers(lane_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex MED 184: one OSError at filing (ENOSPC) is not a verdict on the host."""
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    real = sign.file_levain_request
    fails = [OSError(28, "No space left on device")]

    def flaky(*a: Any, **k: Any) -> str:
        if fails:
            raise fails.pop()
        return real(*a, **k)
    monkeypatch.setattr(sign, "file_levain_request", flaky)
    answer_script([[HOST]])
    drive(sup, lane, 3)
    assert not fails and card_files(queue) == [] and lane.flow.hosts[HOST].state == EC.WAITING
    drive(sup, lane, EC.SCAN_RETRY_S + 2)
    assert len(card_files(queue)) == 1 and lane.flow.hosts[HOST].state == EC.CARDED


def test_l3_a_ping_that_could_not_start_is_not_said_to_have_reached_anyone(lane_env: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """codex MED 186: the phone was live, ``sender.spawn`` answered False."""
    sup, lane, queue = lane_env
    sup.wallv = datetime.now(timezone.utc).timestamp()
    monkeypatch.setattr("levain.webpush.live", lambda q: True)
    monkeypatch.setattr("levain.webpush.sender.spawn", lambda *a, **k: False)
    answer_script([[HOST]])
    drive(sup, lane, 6)
    assert len(card_files(queue)) == 1
    notes = lane.notes()
    [failed] = [n for n in notes if "could not be started" in n]
    assert "signing queue" in failed
    assert not any("pushed to your phone" in n or "waited in the cockpit's notification center" in n for n in notes)


# --- the real relay against the fake helper ---------------------------------------------------------------------------

@bounded(90)
def test_t15_a_whole_card_to_a_grant_writes_nothing_to_the_terminal_mid_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, **one_host())
    seen: dict = {}
    rig.tap_when_filed()
    rig.stop_when(lambda: (rig.store / "granting").exists() and any(v for v in intervals(rig.ivl).get("egress-grant", []) if v[1]), 0.8, seen)
    assert rig.r.run() == 1
    err = rig.r.stderr()
    assert HOST not in seen["err"] and "host card" not in seen["err"]        # N5: nothing mid-relay
    assert f"host {HOST} was allowed until" in err and "(card tapped, root answered ok)" in err
    assert "host cards were pushed" not in err and "no phone was subscribed" in err
    assert HOST not in rig.r.out.read_text("utf-8", "replace")             # and none of it reached his terminal
    v = rig.verbs()
    assert v[:4] == ["prepare", "render", "probe", "egress-grants-list"] and v.index("start") > v.index("egress-grants-list")
    assert v.count("egress-grant") == 1 and v.count("teardown") == 1
    stdin = json.loads((rig.store / "grant-stdin").read_bytes())
    assert set(stdin) == {"message", "sig", "signer_fp", "choice"} and stdin["choice"] == "s"
    assert (rig.store / "grant-stdin").read_bytes() == EC.stdin_bytes(stdin)   # the canonical bytes, nothing re-typed
    assert len(rig.pushes) == 1 and rig.pushes[0]["push"] is False and rig.pushes[0]["kind"] == "attention"


@bounded(90)
def test_t14_the_relay_keeps_reading_while_a_slow_read_runs_and_a_stop_ends_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_START="forever", FAKE_REFUSALS_SLOW_FROM="1", FAKE_REFUSALS_SLOW="30")
    sizes: dict = {}

    def go() -> None:
        wait_for(lambda: (rig.store / "refusing").exists(), 30)
        sizes["a"] = rig.r.out.stat().st_size
        time.sleep(1.0)
        sizes["b"] = rig.r.out.stat().st_size
        sizes["pid"] = int((rig.store / "pid-egress-refusals").read_text())
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=go, daemon=True).start()
    assert rig.r.run() == 1
    assert sizes["b"] > sizes["a"] + 65536                                     # the vendor's bytes kept reaching the terminal
    assert wait_for(lambda: gone(sizes["pid"]), 5)                              # TERM, reaped
    v = rig.verbs()
    assert v.count("egress-refusals") == 1 and v.count("teardown") == 1
    assert "SIGTERM: ending session" in rig.r.stderr()


@bounded(90)
def test_t17_the_end_says_a_card_left_open_and_a_run_in_flight_is_ended_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, **one_host(FAKE_REFUSALS_SLOW_FROM="2", FAKE_REFUSALS_SLOW="30"))
    rig.stop_when(lambda: card_files(rig.queue) and (rig.store / "refusing").exists(), 0.5)
    assert rig.r.run() == 1
    err = rig.r.stderr()
    assert f"host card for {HOST} was left open; it expires on its own and tapping it now does nothing" in err
    pid = int((rig.store / "pid-egress-refusals").read_text())
    assert gone(pid)
    assert rig.verbs().count("teardown") == 1


@bounded(90)
@pytest.mark.parametrize("where", ["advance", "_scan", "_file", "_push", "_read"])
def test_t22_an_exception_in_any_card_action_ends_the_lane_with_one_note_and_not_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None, where: str) -> None:
    rig = Rig(tmp_path, monkeypatch, **one_host())
    calls = {"n": 0}
    if where == "advance":
        real = EC.CardFlow.advance

        def advance(self: Any, event: object, wall: float, mono: float) -> Any:
            if isinstance(event, EC.Polled):
                raise RuntimeError("boom")
            return real(self, event, wall, mono)
        monkeypatch.setattr(EC.CardFlow, "advance", advance)
    else:
        def boom(self: Any, *a: Any) -> Any:
            calls["n"] += 1
            raise RuntimeError("boom")
        monkeypatch.setattr(SV._CardLane, where, boom)
        rig.tap_when_filed()
    sizes: dict = {}

    def go() -> None:
        wait_for(lambda: calls["n"] or where == "advance", 30)
        time.sleep(1.5)
        sizes["a"] = rig.r.out.stat().st_size
        time.sleep(0.8)
        sizes["b"] = rig.r.out.stat().st_size
        sizes["err"] = rig.r.stderr()
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=go, daemon=True).start()
    assert rig.r.run() == 1
    err = rig.r.stderr()
    assert err.count("host cards stopped for this session: an unexpected error (RuntimeError)") == 1
    assert "ending session" in err and "SIGTERM: ending session" in err           # his stop, not the lane's
    assert sizes["b"] >= sizes["a"] and "stopped for this session" not in sizes["err"]   # held until the end
    assert rig.verbs().count("teardown") == 1


@bounded(90)
def test_the_preflight_says_cards_are_off_once_and_the_session_goes_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_LIST_REFUSE="egress-grant-not-ready", FAKE_START="wait")
    rig.stop_when(lambda: (rig.store / "started").exists(), 0.8)
    assert rig.r.run() == 1
    err = rig.r.stderr()
    assert err.count("host cards are off for this session: host grants are not set up for this install") == 1
    assert rig.verbs().count("egress-grants-list") == 1 and "egress-refusals" not in rig.verbs()
    assert rig.verbs().index("egress-grants-list") < rig.verbs().index("start")    # said before the relay: terminal free
    assert "SIGTERM: ending session" in err


@bounded(60)
def test_cards_are_off_with_no_queue_and_make_no_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch)
    rig.r.seams.allow_dir = None
    assert rig.r.run() == 0
    assert not [v for v in rig.verbs() if v.startswith("egress-")]


@bounded(90)
def test_no_cockpit_means_no_card_and_the_end_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, origin=False, **one_host())
    rig.stop_when(lambda: (rig.store / "refusals").exists() and int((rig.store / "refusals").read_text()) >= 3, 0.5)
    assert rig.r.run() == 1
    assert card_files(rig.queue) == [] and rig.pushes == []
    assert f"the cockpit was not running: no card was filed for {HOST}" in rig.r.stderr()


@bounded(90)
def test_t24_with_no_phone_live_the_card_waits_in_the_notification_center(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    from levain import webpush
    from levain.webpush import center, sender
    from levain.webpush.store import PushStore

    rig = Rig(tmp_path, monkeypatch, **one_host(FAKE_REFUSALS_SLOW_FROM="2", FAKE_REFUSALS_SLOW="30"))
    pushed: List[Any] = []

    def spawn(queue_dir: Any, **kw: Any) -> bool:
        pushed.append(kw["push"])
        sender.send(queue_dir, **kw)                  # the real recording, in-process (the production child does the same)
        return True
    monkeypatch.setattr("levain.webpush.sender.spawn", spawn)
    assert webpush.live(rig.queue) is False
    rig.stop_when(lambda: pushed and (rig.store / "refusing").exists(), 0.3)
    assert rig.r.run() == 1
    assert pushed == [False]                          # recorded, pushed to no subscription
    rid = card_files(rig.queue)[0].stem
    items = center.list_items(PushStore(rig.queue))["items"]
    mine = [i for i in items if rid in json.dumps(i)]
    assert len(mine) == 1
    needs = mine[0]["needs"]                          # the row the center draws its Sign button from
    assert needs["sign_state"] == "open" and needs["sign_id"] == rid, mine[0]
    assert "no phone was subscribed: the host cards waited in the cockpit's notification center" in rig.r.stderr()


@bounded(90)
def test_t16_a_slow_read_in_flight_when_a_reprobe_is_due_overlaps_it_and_the_probe_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, darwin=True, every=0.4, FAKE_START="wait", FAKE_REFUSALS_SLOW_FROM="1",
              FAKE_REFUSALS_SLOW="3", FAKE_PROBES="closed")
    rig.stop_when(lambda: len(intervals(rig.ivl).get("probe", [])) >= 4, 0.3)
    assert rig.r.run() == 1
    iv = intervals(rig.ivl)
    read = iv["egress-refusals"][0]
    probes = [p for p in iv["probe"][1:] if p[1] is not None]
    assert any(overlap(read, p) for p in probes)                                  # both lanes ran together
    err = rig.r.stderr()
    assert "firewall self-probe skipped" not in err and "unknown" not in err      # no session-busy reached the re-probe
    assert "SIGTERM: ending session" in err


@bounded(90)
def test_t26_a_delivery_and_a_darwin_reprobe_never_overlap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, darwin=True, every=0.3, FAKE_GRANT="slow:0.8", **one_host(FAKE_PROBES="slow:1.2"))
    rig.tap_when_filed(gate=lambda: (rig.store / "probing").exists())
    rig.stop_when(lambda: any(g[1] for g in intervals(rig.ivl).get("egress-grant", [])) and
                  len([p for p in intervals(rig.ivl).get("probe", []) if p[0] > intervals(rig.ivl)["egress-grant"][0][0]]) >= 1, 0.3)
    assert rig.r.run() == 1
    iv = intervals(rig.ivl)
    grants = iv["egress-grant"]
    assert len(grants) == 1 and grants[0][1] is not None
    assert not any(overlap(grants[0], p) for p in iv["probe"])                    # N2: the session lock is never contended
    assert any(p[0] > grants[0][1] - 0.05 for p in iv["probe"][1:])               # the probe came after the delivery
    err = rig.r.stderr()
    assert "session-busy" not in err and "firewall self-probe skipped" not in err
    assert f"host {HOST} was allowed" in err


@bounded(90)
def test_t16_a_tap_during_a_slow_read_ends_it_and_the_delivery_starts_within_a_tick_and_the_kill_grace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, **one_host(FAKE_REFUSALS_SLOW_FROM="3", FAKE_REFUSALS_SLOW="20"))
    mark: dict = {}

    def tap() -> None:
        wait_for(lambda: (rig.store / "refusing").exists(), 30)
        mark["pid"] = int((rig.store / "pid-egress-refusals").read_text())
        mark["tapped"] = time.time()
        tap_card(rig.queue)
    threading.Thread(target=lambda: (wait_for(lambda: card_files(rig.queue), 30), tap()), daemon=True).start()
    rig.stop_when(lambda: (rig.store / "granting").exists(), 0.8)
    assert rig.r.run() == 1
    grant = intervals(rig.ivl)["egress-grant"][0]
    assert grant[0] - mark["tapped"] < 0.2 + 0.05 + SV.KILL_GRACE_S + 1.0          # the answer poll, a tick, the kill grace
    assert gone(mark["pid"])                                                        # the read was ended, not waited out
    assert f"host {HOST} was allowed" in rig.r.stderr()


@bounded(90)
def test_a_delivery_refused_retryable_is_retried_then_stops_and_the_note_says_no_grant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_GRANT="egress-grant-not-active", **one_host())
    rig.tap_when_filed()
    rig.stop_when(lambda: len(intervals(rig.ivl).get("egress-grant", [])) >= 3, 1.5)
    assert rig.r.run() == 1
    assert rig.verbs().count("egress-grant") == 3                                   # at most 3 attempts per card
    assert "the session was paused when you tapped" in rig.r.stderr() and "no grant was made" in rig.r.stderr()


@bounded(90)
def test_a_delivery_that_outlives_its_30_s_deadline_is_ended_and_said_unknown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    rig = Rig(tmp_path, monkeypatch, FAKE_GRANT="hang", **one_host())
    rig.r.seams.helper_deadline_s = {**SV.HELPER_DEADLINE_S, "egress-grant": 0.6}
    rig.tap_when_filed()
    rig.stop_when(lambda: len(intervals(rig.ivl).get("egress-grant", [])) >= 3, 2.5)
    assert rig.r.run() == 1
    err = rig.r.stderr()
    assert rig.verbs().count("egress-grant") == 3
    assert "the host may be allowed, check the host grants list" in err and "helper-timeout" not in err


# --- T20: a real signature, the real handler --------------------------------------------------------------------------

from levain.slot import install_helper as IH  # noqa: E402
from tests.test_slot_egress_grant_real_sig import NS, real  # noqa: E402,F401
from tests.test_slot_egress_grant_verbs import NOW, S as ROOT_SESSION, entries, world  # noqa: E402,F401


class InProcessRun:
    """What the helper subprocess is to the lane, replaced by the REAL verb handlers (``runtime_helper.main`` over the
    verb tests' scratch world), so the card the lane filed and the signature the test made reach the real
    ``egress_grant``. ``world`` is set per test."""
    world: Any = None
    made: List[str] = []

    def __init__(self, sup: Any, verb: str, args: List[str], data: Optional[bytes], now: float) -> None:
        self.verb, self.open, self.wfd = verb, [], -1
        InProcessRun.made.append(verb)
        self.line = InProcessRun.world.main([verb, *args], data or b"")

    def pump(self, ready: Any, writable: Any = ()) -> None:
        pass

    def outcome(self, now: float) -> Any:
        rc, obj = self.line
        if obj.get("ok") is True:
            return None, {k: v for k, v in obj.items() if k != "ok"}
        return obj["refused"], {}

    def end(self) -> None:
        pass


def test_t20_the_lane_files_the_card_a_real_key_signs_it_and_root_admits_it(real: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    world, key, _other, _auth = real
    InProcessRun.world, InProcessRun.made = world, []
    monkeypatch.setattr(SV, "_CardRun", InProcessRun)
    monkeypatch.setattr(SV, "CARD_STEP_S", 0.0)
    monkeypatch.setattr(SV, "CARD_RP_S", 0.0)
    monkeypatch.setattr(EC, "POLL_S", 0.0)
    monkeypatch.setattr("levain.webpush.sender.spawn", lambda *a, **k: True)
    queue = tmp_path / "queue"
    queue.mkdir()
    write_origin(queue, ORIGIN)
    # root's clock in the world is NOW; the supervisor's wall clock is the same instant, so the card's ``issued`` is fresh to it
    sup = FakeSup(session=ROOT_SESSION, slot=3)
    sup.wallv = NOW.timestamp()
    monkeypatch.setattr(sign, "_now", lambda: NOW)
    lane = SV._CardLane(sup, EC.CardFlow(session=ROOT_SESSION, slot=3), queue)
    # root's real store id, through the real list verb (the preflight's read)
    rc, listed = world.main(["egress-grants-list", ROOT_SESSION])
    ev = SV.card_read_event(EC, EC.GRANTS_LIST, {k: v for k, v in listed.items() if k != "ok"})
    assert ev is not None
    lane._feed(ev)
    # the front refused one host: a real refusals answer is a log scan; the lane's own poll is replaced by the event
    lane._feed(EC.Polled((HOST,), "0"))
    for _ in range(12):
        sup.tick(0.5)
        lane.step(False)
        lane.pump([], [])
        if card_files(queue):
            break
    assert len(card_files(queue)) == 1
    path = card_files(queue)[0]
    message = json.loads(path.read_text())["message"]
    sig = key.sign(eg.signed_bytes(message, "s"), NS)                                # a REAL ssh-keygen signature
    rid = path.stem
    (queue / "sign" / f"{rid}.sig").write_text(sig)
    signed = eg.signed_bytes(message, "s")
    (queue / "sign" / f"{rid}.signed.json").write_text(json.dumps({
        "choice": "s", "message_sha256": hashlib.sha256(signed).hexdigest(), "namespace": NS, "public_key": key.public,
        "signed_at": "2026-11-16T11:59:00Z", "signed_message": signed.decode(), "signer_fp": IH.blob_fingerprint(key.public)}))
    for _ in range(40):
        sup.tick(0.5)
        lane.step(False)
        lane.pump([], [])
        if lane.flow.hosts[HOST].outcome == "granted":
            break
    assert lane.flow.hosts[HOST].outcome == "granted", (lane.flow.hosts[HOST], InProcessRun.made)
    assert HOST in (entries(world) or {})                                            # root wrote the grant
    assert any(f"host {HOST} was allowed until" in n for n in lane.notes())


# --- L4 review 1116-19 M1: no card for a host root would refuse as routed ---------------------------------------------

@bounded(90)
def test_m1_a_claude_session_refused_a_codex_host_files_no_card_and_the_end_says_no_grant_is_possible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quick: None) -> None:
    """M1, reproduced on the real relay: a claude session's agent CONNECTs api.openai.com (the base map's codex route
    and a codex vendor host). Before the fix a card was filed and pushed, and the tap could only end in root's
    ``egress-grant-host-routed``. Mutation: pass no routed set to the flow in ``_cards_preflight`` -> a card is filed."""
    routed = "api.openai.com"
    rig = Rig(tmp_path, monkeypatch, FAKE_START="wait", FAKE_REFUSALS=json.dumps([[routed]]))
    rig.stop_when(lambda: (rig.store / "refusals").exists() and int((rig.store / "refusals").read_text()) >= 3, 0.5)
    assert rig.r.run() == 1
    assert card_files(rig.queue) == [] and rig.pushes == []
    err = rig.r.stderr()
    assert f"host {routed} was refused" in err and "a host grant cannot allow it, so no card was filed" in err


_TABLE_BASE = {"format": "levain-routes-base/1", "routes": [
    {"route": "anthropic", "type": "brokered", "hosts": ["api.anthropic.com"],
     "upstream": {"scheme": "https", "host": "up.example.net", "base_path": "/"}},
    {"route": "github", "type": "egress", "hosts": ["github.com", "codeload.github.com"]},
    {"route": "mirror", "type": "egress", "hosts": ["pypi.mirror.example.org"]},
    {"route": "pt", "type": "subscription_passthrough", "hosts": ["claude.ai"]}]}


@pytest.mark.parametrize("host", ["api.anthropic.com", "github.com", "codeload.github.com", "pypi.mirror.example.org",
                                  "claude.ai", "api.openai.com", "auth.openai.com", "chatgpt.com", "console.anthropic.com",
                                  "platform.claude.com", "up.example.net", "files.example.org", "github.com.example.org",
                                  "example.org"])
def test_m1_the_lanes_routed_set_is_roots_host_class_over_a_table(host: str) -> None:
    """The lane's filter and root's refusal cannot drift: ``routed_hosts`` over the base map the session read says a
    host is routed exactly when ``egress_verify.host_class`` over the same map refuses it ``egress-grant-host-routed``
    (an ``upstream`` host that no route's ``hosts`` names is not routed for either)."""
    from levain.contracts import canon
    from levain.slot import Refused
    from levain.slot import egress_verify as EVY

    raw = canon.canonical_bytes(_TABLE_BASE)
    ctx = SimpleNamespace(store=SimpleNamespace(read=lambda parts, name: raw if (tuple(parts), name) == (("routes",), "base.json") else None))
    try:
        EVY.host_class(ctx, host)
        root_refuses = False
    except Refused as e:
        assert e.name == EVY.ERR_ROUTED
        root_refuses = True
    base = SV.read_base_map(Path("unused"), read=lambda p: raw)
    assert (host in SV.routed_hosts(base)) is root_refuses
