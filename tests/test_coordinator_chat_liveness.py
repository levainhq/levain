"""CH-D1b-3: liveness. The activation columns (rev 7 §2.7), the verdict writer over an injected probe (§4.1-§4.2: the
session supplies raw evidence and never a verdict), ``probing`` at every boundary (§4.2 and R9's Claude boundary),
retry-then-latch, the first-send-while-``probing`` exception (H9, Q-R6-7) and the composer states, through the real
store on real sqlite files.

Several tests are "broken fakes" (§12): a check runs against the real service and against a subclass broken in one
named way, and must pass on the first and fail on the second."""
from __future__ import annotations

import dataclasses
import hashlib
import inspect
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from levain.coordinator import chat
from levain.coordinator.chat import (COMPOSER_STATES, ChatService, ChatStore, Verdict, canary_verdict_id,
                                     composer_state, liveness, record_check)
from levain.coordinator.driver import LivenessEvidence, ScriptedSession
from levain.coordinator.envelopes import EnvelopeStore
from levain.coordinator.store import CoordError, Part

T0 = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
ME = "cockpit:tab-a"
PHONE = "phone:iphone-1"
NONCE = "0123456789abcdef0123456789abcdef"
DIGEST = "ab" * 32


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> datetime:
        return self.now


@dataclasses.dataclass(frozen=True)
class Canary:                       # the shape of host_canary.CanaryVerdict on main (D:910)
    ok: bool
    checks: tuple
    claude_version: str
    claude_bin: str
    profile_digest: str


CANARY = Canary(True, ("network", "write"), "2.1.310", "/usr/local/bin/claude", "cd" * 32)


@pytest.fixture
def store(tmp_path):
    d = tmp_path / "consent"
    d.mkdir(mode=0o700)
    return EnvelopeStore(d, clock=Clock())


def _world(store: EnvelopeStore, harness: str) -> dict:
    store.coord_set_agent("none")
    token = store.coord_take()
    d = store.accept_drop("fix the build", surface="cli")[0]
    thread = store.split(d.drop_id, [Part("work", "fix the build")])[0]
    ln = store.launch_request(token, harness=harness, role="worker", threads=[thread.thread_id])
    got, why = store.launch_activate(ln.launch_id, harness_session_id=f"sess-{harness}-1")
    assert why is None and got.state == "active"
    return {"thread": thread.thread_id, "launch": ln.launch_id, "harness": harness}


@pytest.fixture(params=["claude", "codex"])
def world(store, request):
    return _world(store, request.param)


@pytest.fixture
def claude(store):
    return _world(store, "claude")


@pytest.fixture
def codex(store):
    return _world(store, "codex")


@pytest.fixture
def cs(store):
    s = ChatStore(store)
    s._connect().close()
    return s


class Probe:
    """A service-side verdict function: records the evidence it was handed and answers from a script."""

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers) or [Verdict(True, "hooks trusted")]
        self.seen: list[LivenessEvidence] = []

    def __call__(self, launch_id: str, evidence: LivenessEvidence) -> Verdict:
        self.seen.append(evidence)
        got = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(got, BaseException):
            raise got
        return got


def service(cs, probe=None, **kw):
    kw.setdefault("sleep", lambda _s: None)
    return ChatService(cs, probe=probe if probe is not None else Probe(), **kw)


def activate(svc, world, **kw):
    """The launch's activation verdict: Claude's live carries the nonce, the canary id and the containment digest."""
    if world["harness"] == "claude":
        kw.setdefault("spawn_nonce", NONCE)
        kw.setdefault("canary", CANARY)
        kw.setdefault("containment_digest", DIGEST)
    svc.activate(world["launch"], "live", "activation verified", **kw)


def ready(cs, world, session=None, probe=None, **kw):
    session = session or ScriptedSession()
    svc = service(cs, probe, **kw)
    activate(svc, world)
    svc.attach(world["launch"], session)
    svc.read_next(world["launch"])
    return svc, session


def rows(cs, launch):
    with cs._write() as (conn, _now):
        return conn.execute("SELECT kind, source, detail, evidence_sha256, spawn_nonce, canary_verdict_id, "
                            "containment_digest FROM chat_launch_checks WHERE launch_id = ? ORDER BY seq",
                            (launch,)).fetchall()


def kinds(cs, launch):
    return [(k, s) for k, s, *_ in rows(cs, launch)]


def fold(cs, launch):
    return cs._read(lambda conn: liveness(conn, launch))


def send(svc, cs, world, body="go", *, outbox="ob-00000001", owner=ME):
    epoch = cs.turn_view(world["launch"], owner).epoch or cs.turn_claim(world["launch"], owner)
    surface, client = owner.split(":", 1)
    return svc.send(world["launch"], author_kind="operator", author_surface=surface, client_id=client,
                    outbox_id=outbox, body=body, expected_epoch=epoch)


# --- the activation columns (rev 7 §2.7) ------------------------------------------------------------------------------
def test_activation_live_row_carries_verdict_id_and_containment_digest(cs, claude):
    svc = service(cs)
    activate(svc, claude)
    (kind, source, _d, _e, nonce, verdict_id, digest), = rows(cs, claude["launch"])
    assert (kind, source, nonce, digest) == ("live", "activation", NONCE, DIGEST)
    want = hashlib.sha256(json.dumps(dataclasses.asdict(CANARY), sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()
    assert verdict_id == want == canary_verdict_id(CANARY)


def test_claude_live_activation_without_canary_refused(cs, claude):
    svc = service(cs)
    for missing in ("canary", "containment_digest", "spawn_nonce"):
        with pytest.raises(CoordError):
            activate(svc, claude, **{missing: None})
    with pytest.raises(CoordError):
        activate(svc, claude, canary=dataclasses.replace(CANARY, ok=False))
    assert rows(cs, claude["launch"]) == []


def test_claude_activation_row_carries_spawn_nonce(cs, claude):
    svc = service(cs)
    svc.activate(claude["launch"], "probing", "spawned", spawn_nonce=NONCE)
    with pytest.raises(CoordError):
        svc.activate(claude["launch"], "probing", "spawned")           # a Claude activation always names its child
    assert [r[4] for r in rows(cs, claude["launch"])] == [NONCE]


def test_codex_activation_carries_no_nonce_or_canary(cs, codex):
    svc = service(cs)
    for kw in ({"spawn_nonce": NONCE}, {"canary": CANARY, "containment_digest": DIGEST}):
        with pytest.raises(CoordError):
            svc.activate(codex["launch"], "live", "hooks/list trusted", **kw)
    svc.activate(codex["launch"], "live", "hooks/list trusted")
    assert kinds(cs, codex["launch"]) == [("live", "activation")]


@pytest.mark.parametrize("cols", [
    {"source": "turn_start", "kind": "live", "nonce": None},         # not an activation row
    {"source": "activation", "kind": "probing", "nonce": NONCE},     # an activation row that is not live
    {"source": "activation", "kind": "live", "nonce": None},         # live without the child it judged
])
def test_verdict_id_refused_on_non_activation_row(cs, claude, cols):
    # the table's own rule, written raw past every Python writer: a released store holds it too (_ensure's trigger)
    with cs._write() as (conn, now):
        seq = 1
        with pytest.raises(sqlite3.IntegrityError, match="canary verdict id"):
            conn.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, spawn_nonce, detail, by, "
                         "at, canary_verdict_id, containment_digest) VALUES (?, ?, ?, ?, ?, ?, 'x', 'service', ?, ?, ?)",
                         (claude["launch"], seq, chat.next_gseq(conn, now), cols["kind"], cols["source"],
                          cols["nonce"], now, "ef" * 32, DIGEST))


def test_verdict_id_and_digest_come_together(cs, claude):
    with cs._write() as (conn, now):
        with pytest.raises(sqlite3.IntegrityError, match="canary verdict id"):
            conn.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, spawn_nonce, detail, by, "
                         "at, canary_verdict_id) VALUES (?, 1, ?, 'live', 'activation', ?, 'x', 'service', ?, ?)",
                         (claude["launch"], chat.next_gseq(conn, now), NONCE, now, "ef" * 32))
        with pytest.raises(sqlite3.IntegrityError, match="canary verdict id"):
            conn.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, spawn_nonce, detail, by, "
                         "at, canary_verdict_id, containment_digest) VALUES (?, 1, ?, 'live', 'activation', ?, 'x', "
                         "'service', ?, ?, ?)",
                         (claude["launch"], chat.next_gseq(conn, now), NONCE, now, "EF" * 32, DIGEST))


def test_spawn_nonce_refused_on_non_activation_row(cs, claude):
    with cs._write() as (conn, now):
        with pytest.raises(CoordError):
            record_check(conn, now, claude["launch"], "live", "turn_start", "x", spawn_nonce=NONCE)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO chat_launch_checks (launch_id, seq, gseq, kind, source, spawn_nonce, detail, by, "
                         "at) VALUES (?, 1, ?, 'live', 'turn_start', ?, 'x', 'service', ?)",
                         (claude["launch"], chat.next_gseq(conn, now), NONCE, now))


# --- the fold ---------------------------------------------------------------------------------------------------------
def test_live_after_not_live_stays_off(cs, world):
    probe = Probe(Verdict(True, "ok"), Verdict(False, "a hook changed"), Verdict(True, "ok again"))
    svc, s = ready(cs, world, probe=probe)
    for turn in ("t-1", "t-2", "t-3"):
        s.show("turn_started", turn, turn_id=turn)
        svc.read_next(world["launch"])
    assert [k for k, _s in kinds(cs, world["launch"])][-2:] == ["probing", "live"]
    f = fold(cs, world["launch"])
    assert f.latched and f.latest == "live" and not f.enabled
    assert send(svc, cs, world).state == "refused"


def test_reenabled_needs_latest_live(cs, world):
    probe = Probe(Verdict(False, "a hook changed"), Verdict(True, "ok again"))
    svc, s = ready(cs, world, probe=probe)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    with pytest.raises(CoordError):
        svc.reenable(world["launch"], ME)                       # the latest probe read not live
    s.show("turn_started", "t-2", turn_id="t-2")
    svc.read_next(world["launch"])
    svc.reenable(world["launch"], ME)
    assert fold(cs, world["launch"]).enabled
    assert kinds(cs, world["launch"])[-1] == ("reenabled", "operator")


def test_service_never_reenables_itself():
    # static: only the operator's reenable() writes a reenabled row; no other code path of chat.py names the kind
    src = inspect.getsource(chat.ChatService)
    assert src.count('"reenabled"') == 1 and "def reenable" in src


# --- probing at the boundary --------------------------------------------------------------------------------------
def test_probing_at_turn_start_blocks_send_and_accept(cs, world):
    seen = []

    def stuck(launch_id, evidence):
        # the turn's probing row committed before the probe ran: the fold is probing, nothing is enabled
        f = fold(cs, world["launch"])
        seen.append((f.latest, f.enabled))
        raise KeyboardInterrupt                      # the probe never returns a verdict

    svc, s = ready(cs, world)
    svc._probe = stuck
    s.show("turn_started", "t-1", turn_id="t-1")
    with pytest.raises(KeyboardInterrupt):
        svc.read_next(world["launch"])
    assert seen == [("probing", False)]
    assert kinds(cs, world["launch"])[-1] == ("probing", "turn_start")
    r = send(svc, cs, world)                         # an accept reads the same fold (D1c's attempt_answer)
    assert r.state == "refused" and "check is running" in r.cause


def test_probing_row_commits_with_the_turn_started_event(cs, world):
    svc, s = ready(cs, world)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    with cs._write() as (conn, _now):
        ev = conn.execute("SELECT gseq, at FROM chat_events WHERE launch_id = ? AND kind = 'turn_started'",
                          (world["launch"],)).fetchone()
        pr = conn.execute("SELECT gseq, at FROM chat_launch_checks WHERE launch_id = ? AND kind = 'probing'",
                          (world["launch"],)).fetchone()
    # one transaction: the same clock stamp, and nothing else's gseq between the event and its probing row
    assert pr[1] == ev[1] and pr[0] == ev[0] + 1


def test_backfill_turn_start_writes_no_probing(cs, world):
    s = ScriptedSession()
    s.show("turn_started", "t-0", turn_id="t-0")                 # history before the service's first read
    svc, _ = ready(cs, world, session=s)
    assert kinds(cs, world["launch"]) == [("live", "activation")]


def test_probe_read_failure_retries_then_latches(cs, world):
    probe = Probe(OSError("hooks/list timed out"))
    naps = []
    svc, s = ready(cs, world, probe=probe, sleep=naps.append)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    assert len(probe.seen) == chat.PROBE_TRIES and naps == [chat.PROBE_GAP] * (chat.PROBE_TRIES - 1)
    kind, source, detail, *_ = rows(cs, world["launch"])[-1]
    assert (kind, source) == ("not_live", "turn_start") and detail.startswith("could not check: ")
    assert "hooks/list timed out" in detail and fold(cs, world["launch"]).latched


def test_probe_transient_failure_recovers_within_the_window(cs, world):
    probe = Probe(OSError("busy"), Verdict(True, "ok"))
    svc, s = ready(cs, world, probe=probe)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    assert kinds(cs, world["launch"])[-1] == ("live", "turn_start") and fold(cs, world["launch"]).enabled


def test_no_probe_configured_latches(cs, world):
    svc = ChatService(cs, sleep=lambda _s: None)
    activate(svc, world)
    s = ScriptedSession()
    svc.attach(world["launch"], s)
    svc.read_next(world["launch"])                               # the origin: what follows is no backfill
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    kind, _src, detail, *_ = rows(cs, world["launch"])[-1]
    assert kind == "not_live" and "no probe" in detail


def test_verdict_evidence_digest_recorded(cs, world):
    svc, s = ready(cs, world)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    *_, (kind, _s, _d, evidence, _n, _v, _c) = rows(cs, world["launch"])
    want = "sha256:" + hashlib.sha256(json.dumps(
        {"source": "scripted", "raw": {"hooks": "scripted"}, "read_at": ""}, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assert kind == "live" and evidence == want


# --- the session returns no verdict ------------------------------------------------------------------------------
class SaysLive(ScriptedSession):
    """A session that tries to hand the service a verdict: its evidence claims live, and it offers a Verdict too."""

    def liveness_evidence(self):
        return LivenessEvidence("scripted", {"live": True, "verdict": True}, "")


class GivesVerdict(ScriptedSession):
    def liveness_evidence(self):
        return Verdict(True, "trust me")


class GivesBool(ScriptedSession):
    def liveness_evidence(self):
        return True


@pytest.mark.parametrize("session_cls", [GivesVerdict, GivesBool])
def test_session_supplied_verdict_is_a_failed_probe(cs, world, session_cls):
    probe = Probe(Verdict(True, "ok"))
    svc, s = ready(cs, world, session=session_cls(), probe=probe)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    assert probe.seen == [] and kinds(cs, world["launch"])[-1] == ("not_live", "turn_start")


def test_session_evidence_claiming_live_is_only_evidence(cs, world):
    probe = Probe(Verdict(False, "PreToolUse entry untrusted"))
    svc, s = ready(cs, world, session=SaysLive(), probe=probe)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    assert kinds(cs, world["launch"])[-1] == ("not_live", "turn_start")
    assert probe.seen[0].raw == {"live": True, "verdict": True}


@pytest.mark.parametrize("bad", [True, ("live", True), {"live": True}, Verdict(1, "ok"), Verdict(True, 7)])
def test_malformed_probe_return_is_a_failed_try(cs, world, bad):
    probe = Probe(bad)
    svc, s = ready(cs, world, probe=probe)
    probe.answers = [bad]
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    assert kinds(cs, world["launch"])[-1] == ("not_live", "turn_start")


class _TakesSessionBool(ChatService):
    """BROKEN FAKE: takes the session's evidence as the verdict when it looks like one."""

    def _verdict(self, launch_id, evidence):
        if isinstance(evidence, LivenessEvidence) and isinstance(evidence.raw, dict) and evidence.raw.get("live"):
            return Verdict(True, "the session said so")
        return super()._verdict(launch_id, evidence)


def _no_session_verdict(svc_cls, cs, world) -> bool:
    probe = Probe(Verdict(False, "untrusted"))
    svc = svc_cls(cs, probe=probe, sleep=lambda _s: None)
    activate(svc, world)
    s = SaysLive()
    svc.attach(world["launch"], s)
    svc.read_next(world["launch"])
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(world["launch"])
    return kinds(cs, world["launch"])[-1] == ("not_live", "turn_start")


def test_broken_fake_takes_session_boolean(store, cs):
    w = _world(store, "codex")
    assert _no_session_verdict(ChatService, cs, w)
    w2 = _world(store, "codex")
    assert not _no_session_verdict(_TakesSessionBool, cs, w2)


class _ReenablesItself(ChatService):
    """BROKEN FAKE: a live verdict after a latch re-enables the launch."""

    def _write_verdict(self, launch_id, pending, kind, detail, evidence):
        super()._write_verdict(launch_id, pending, kind, detail, evidence)
        if kind == "live":
            with self._chat._write() as (conn, now):
                if liveness(conn, launch_id).latched:
                    record_check(conn, now, launch_id, "reenabled", "operator", "self", by="operator")


def _stays_off(svc_cls, cs, world) -> bool:
    probe = Probe(Verdict(True, "ok"), Verdict(False, "changed"), Verdict(True, "ok"))
    svc = svc_cls(cs, probe=probe, sleep=lambda _s: None)
    activate(svc, world)
    s = ScriptedSession()
    svc.attach(world["launch"], s)
    svc.read_next(world["launch"])
    for turn in ("t-1", "t-2", "t-3"):
        s.show("turn_started", turn, turn_id=turn)
        svc.read_next(world["launch"])
    return not fold(cs, world["launch"]).enabled


def test_broken_fake_live_after_not_live_reenables(store, cs):
    assert _stays_off(ChatService, cs, _world(store, "codex"))
    assert not _stays_off(_ReenablesItself, cs, _world(store, "codex"))


# --- Claude's boundary (R9) -----------------------------------------------------------------------------------------
class Watch(ScriptedSession):
    """Records the fold the service committed at the moment the bytes are written."""

    def __init__(self, cs, launch, **kw) -> None:
        super().__init__(**kw)
        self.cs, self.launch, self.at_write = cs, launch, []

    def send(self, command):
        self.at_write.append(kinds(self.cs, self.launch)[-1])
        return super().send(command)


def test_claude_send_writes_probing_before_bytes(cs, claude):
    s = Watch(cs, claude["launch"])
    svc, _ = ready(cs, claude, session=s)
    assert send(svc, cs, claude).state == "accepted"
    assert s.at_write == [("probing", "send")]
    assert kinds(cs, claude["launch"])[-2:] == [("probing", "send"), ("live", "send")]


def test_codex_send_writes_no_probing(cs, codex):
    s = Watch(cs, codex["launch"])
    svc, _ = ready(cs, codex, session=s)
    assert send(svc, cs, codex).state == "accepted"
    assert s.at_write == [("live", "activation")] and kinds(cs, codex["launch"]) == [("live", "activation")]


def test_claude_result_line_writes_probing(cs, claude):
    svc, s = ready(cs, claude)
    s.show("turn_ended", "t-1", turn_id="t-1")
    svc.read_next(claude["launch"])
    assert kinds(cs, claude["launch"])[-2:] == [("probing", "result"), ("live", "result")]


def test_codex_turn_end_is_no_boundary(cs, codex):
    svc, s = ready(cs, codex)
    s.show("turn_ended", "t-1", turn_id="t-1")
    svc.read_next(codex["launch"])
    assert kinds(cs, codex["launch"]) == [("live", "activation")]


def test_claude_queued_mid_turn_send_is_a_boundary(cs, claude):
    # a message queued into a running turn starts nothing visible; the send itself is the boundary
    svc, s = ready(cs, claude)
    s.show("turn_started", "t-1", turn_id="t-1")
    svc.read_next(claude["launch"])
    before = len(rows(cs, claude["launch"]))
    assert send(svc, cs, claude).state == "accepted"
    assert kinds(cs, claude["launch"])[before:] == [("probing", "send"), ("live", "send")]


def test_claude_second_send_refused_while_send_probe_pending(cs, claude):
    probe = Probe(Verdict(True, "ok"))
    svc, s = ready(cs, claude, probe=probe)
    svc._probe = None                                   # the send's probe cannot run: it latches
    assert send(svc, cs, claude).state == "accepted"
    assert kinds(cs, claude["launch"])[-1] == ("not_live", "send")
    assert send(svc, cs, claude, outbox="ob-00000002").state == "refused"


# --- H9: the first send under probing (Claude, stage 1 passed, first send of the child) ---------------------------
def stage1(svc, world):
    svc.activate(world["launch"], "probing", "spawned", spawn_nonce=NONCE)
    svc.activate(world["launch"], "probing", "stage 1: initialize verified", spawn_nonce=NONCE,
                 evidence=LivenessEvidence("claude init", {"pid": 4242, "current_permission_mode": "default"}, ""))


def test_first_send_waits_for_stage1(cs, claude):
    svc = service(cs)
    s = ScriptedSession()
    svc.attach(claude["launch"], s)
    svc.read_next(claude["launch"])
    svc.activate(claude["launch"], "probing", "spawned", spawn_nonce=NONCE)
    r = send(svc, cs, claude)
    assert r.state == "refused" and "not live" in r.cause and s.written == []
    stage1(svc, claude)
    assert send(svc, cs, claude, outbox="ob-00000002").state == "accepted"
    # the exception wrote no send probe: the launch waits for stage 2, which only the activation writer gives
    assert kinds(cs, claude["launch"])[-1] == ("probing", "activation")
    r3 = send(svc, cs, claude, outbox="ob-00000003")
    assert r3.state == "refused" and len(s.written) == 1          # only the FIRST send of the child


def test_first_send_exception_is_claude_only(cs, codex):
    svc = service(cs)
    s = ScriptedSession()
    svc.attach(codex["launch"], s)
    svc.read_next(codex["launch"])
    with cs._write() as (conn, now):
        record_check(conn, now, codex["launch"], "probing", "activation", "x",
                     evidence_sha256="sha256:" + "0" * 64)
    assert send(svc, cs, codex).state == "refused"


def test_allow_deferred_until_first_init_verified(cs, claude):
    # until stage 2 the launch is probing, so nothing reads enabled: every allow (D1c's attempt_answer) defers
    svc = service(cs)
    s = ScriptedSession()
    svc.attach(claude["launch"], s)
    svc.read_next(claude["launch"])
    stage1(svc, claude)
    send(svc, cs, claude)
    s.show("turn_started", "t-1", turn_id="t-1")                # a boundary before stage 2 writes no turn probe
    s.show("turn_ended", "t-1", turn_id="t-1")
    svc.read_next(claude["launch"])
    f = fold(cs, claude["launch"])
    assert not f.enabled and f.latest == "probing" and kinds(cs, claude["launch"])[-1] == ("probing", "activation")
    activate(svc, claude)
    assert fold(cs, claude["launch"]).enabled


def test_first_init_mismatch_latches(cs, claude):
    svc = service(cs)
    stage1(svc, claude)
    svc.activate(claude["launch"], "not_live", "stage 2: tools differ", spawn_nonce=NONCE)
    f = fold(cs, claude["launch"])
    assert f.latched and not f.enabled


# --- composer states ------------------------------------------------------------------------------------------------
def composer(cs, world, owner=ME, attached=True):
    with cs._write() as (conn, now):
        return composer_state(conn, now, world["launch"], owner, attached=attached)


def test_composer_state_set():
    assert COMPOSER_STATES == ("ended", "no_session", "not_live", "checking", "taken", "writable")


def test_composer_states_walk(cs, claude, store):
    svc = service(cs)
    assert composer(cs, claude).state == "checking"                  # no check has run
    assert composer(cs, claude, attached=False).state == "no_session"
    svc.activate(claude["launch"], "probing", "spawned", spawn_nonce=NONCE)
    assert composer(cs, claude).state == "checking"
    stage1(svc, claude)
    assert composer(cs, claude).state == "writable"                  # the first send of the child may go
    activate(svc, claude)
    c = composer(cs, claude)
    assert c.state == "writable" and c.why is None
    cs.turn_claim(claude["launch"], PHONE)
    c = composer(cs, claude)
    assert c.state == "taken" and c.holder
    assert composer(cs, claude, owner=PHONE).state == "writable"
    with cs._write() as (conn, now):
        record_check(conn, now, claude["launch"], "not_live", "tripwire:mode_changed", "plan mode")
    c = composer(cs, claude, owner=PHONE)
    assert c.state == "not_live" and not c.reenable
    with cs._write() as (conn, now):
        record_check(conn, now, claude["launch"], "live", "result", "ok again")
    c = composer(cs, claude, owner=PHONE)
    assert c.state == "not_live" and c.reenable


def test_composer_ended_launch(cs, codex, store):
    svc = service(cs)
    svc.activate(codex["launch"], "live", "ok")
    store.launch_end(codex["launch"], "ended", "done")
    assert composer(cs, codex).state == "ended"


def test_composer_carries_unresolved_sends(cs, codex):
    svc, s = ready(cs, codex, session=ScriptedSession(sends=("ambiguous",)))
    r = send(svc, cs, codex)
    assert r.state == "ambiguous"
    c = composer(cs, codex)
    assert c.unresolved == (r.message_id,) and c.state == "writable"


def test_composer_state_writes_nothing(cs, claude):
    with cs._write() as (conn, now):
        before = conn.total_changes
        composer_state(conn, now, claude["launch"], ME, attached=True)
        assert conn.total_changes == before
