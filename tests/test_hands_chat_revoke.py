"""457 (c): a hands-MCP revoke ends a hands chat that is starting or running. The rig is the launch tests' one (a
sudo shim, a real scratch authority log); the vendor is a fake that never exits on its own, and the liveness read is
a scripted answer per tick. No hands account and no real vendor; the note store is a fake except where a test
files through a real one on a scratch directory."""
from __future__ import annotations

import subprocess
import sys

import pytest

from levain import hands_live
from levain.consent.notes import NoteStore as RealNoteStore
from levain.firing import hands_chat as hc, hands_mcp_login as hml
from tests.test_hands_chat_launch import assert_released, locks
from tests.test_hands_mcp_verbs import McpRig

pytestmark = pytest.mark.skipif(sys.platform not in ("darwin", "linux"), reason="POSIX shims")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = McpRig(tmp_path, monkeypatch)
    monkeypatch.setattr(hc, "host_refusal", lambda: None, raising=False)
    monkeypatch.setattr(hc, "vendor_missing", lambda vendor: None)      # a test host need not have the vendors
    monkeypatch.setattr(hc, "WATCH_S", 1.0)
    # a fake clock the fake vendor's timed waits advance: ticks are deterministic, the unreadable window is by time
    r.clock = [0.0]
    monkeypatch.setattr(hc, "_now", lambda: r.clock[0], raising=False)
    monkeypatch.setattr(hc, "UNREADABLE_S", 2.0, raising=False)
    monkeypatch.setattr(hc, "READ_DEADLINE_S", 5.0, raising=False)
    r.events = []
    real_sweep = hands_live.sweep_hands_user

    def sweep(user, *, uid, timeout=5.0):
        r.events.append(("sweep", locks(r)))
        return real_sweep(user, uid=uid, timeout=timeout)
    monkeypatch.setattr(hands_live, "sweep_hands_user", sweep)
    r.register("alpha", callback_port=49601)
    r.filed = []
    r.note_raises = False

    class Store:
        def file(self, **kw):
            if r.note_raises:
                raise OSError("the note store is down")
            r.filed.append(kw)

    monkeypatch.setattr(hc.notes_mod, "NoteStore", Store)
    yield r
    r.oauth.close()


class FakeChat:
    """A vendor that runs until stopped, or exits by itself after ``exit_after`` timed waits."""

    def __init__(self, rig, exit_after=None, rc=0):
        self.rig, self.exit_after, self.rc, self.waits, self.stopped = rig, exit_after, rc, 0, False

    def __call__(self, argv, cwd):
        return self

    def wait(self, timeout=None):
        if self.stopped:
            return -15
        self.waits += 1
        if self.exit_after is not None and self.waits > self.exit_after:
            return self.rc
        self.rig.clock[0] += timeout or 0.0         # the timed wait ran its length
        raise subprocess.TimeoutExpired("chat", timeout)

    def terminate(self):
        self.rig.events.append(("terminate", locks(self.rig)))
        self.stopped = True

    def kill(self):
        self.stopped = True


def script(monkeypatch, answers):
    """``hml.launch_check`` answers from ``answers`` in order, then live (None) for good."""
    calls = []
    seq = [None, *answers]      # the first read is the pre-start one

    def check(res, now=None):
        calls.append(1)
        answer = seq.pop(0) if seq else None
        if isinstance(answer, BaseException):
            raise answer
        return answer
    monkeypatch.setattr(hml, "launch_check", check)
    return calls


def run(rig, chat):
    lines = []
    rc = hc.launch_hands_chat(rig.entity, "claude", rig.ck.root, spawn=chat, emit=lines.append)
    return rc, lines


def revoked_lines(lines):
    return [x for x in lines if "hands revoked" in x]


def test_gone_on_tick_two_ends_the_chat(rig, monkeypatch):
    script(monkeypatch, [None, (hml.GONE, "g-1")])
    chat = FakeChat(rig)
    rc, lines = run(rig, chat)
    assert rc == hc.HANDS_REVOKED_EXIT == 77
    assert chat.stopped
    kinds = [e[0] for e in rig.events]
    assert "terminate" in kinds
    # the hands account was swept FIRST (L1 MED 3: the old pin, terminate before the LAST sweep, held with the first
    # sweep deleted): a sweep after the pre-start one and right before the stop, then the final sweep after it, with
    # both locks still held
    stop = kinds.index("terminate")
    assert kinds[0] == "sweep" and stop >= 2 and kinds[stop - 1] == "sweep"
    assert rig.events[stop - 1] == ("sweep", (True, True))
    assert kinds.index("terminate") < max(i for i, k in enumerate(kinds) if k == "sweep")
    assert rig.events[-1] == ("sweep", (True, True))
    assert_released(rig)
    [line] = revoked_lines(lines)
    assert line == "hands chat: hands revoked: registry entry g-1 was revoked or ran out; this chat was ended"
    [note] = rig.filed
    assert note["what"] == line and note["agent"] == "claude_code"


def test_unreadable_below_the_limit_then_live_is_not_stopped(rig, monkeypatch):
    script(monkeypatch, [(hml.UNREADABLE, "slow")] * 2)
    chat = FakeChat(rig, exit_after=6, rc=3)
    rc, lines = run(rig, chat)
    assert rc == 3 and not chat.stopped
    assert not revoked_lines(lines) and rig.filed == []
    assert_released(rig)


def test_a_live_answer_resets_the_unreadable_count(rig, monkeypatch):
    u = (hml.UNREADABLE, "slow")
    script(monkeypatch, [u, u, None, u, u])
    chat = FakeChat(rig, exit_after=8, rc=0)
    rc, _ = run(rig, chat)
    assert rc == 0 and not chat.stopped


def test_unreadable_at_the_limit_ends_the_chat(rig, monkeypatch):
    script(monkeypatch, [(hml.UNREADABLE, "log gone")] * 3)
    chat = FakeChat(rig)
    rc, lines = run(rig, chat)
    assert rc == 77 and chat.stopped
    [line] = revoked_lines(lines)
    assert "could not be confirmed (log gone); this chat was ended" in line
    assert len(rig.filed) == 1
    assert_released(rig)


def test_the_chat_exiting_on_its_own_gives_its_own_code_no_line_no_note(rig, monkeypatch):
    script(monkeypatch, [])
    chat = FakeChat(rig, exit_after=2, rc=9)
    rc, lines = run(rig, chat)
    assert rc == 9 and not chat.stopped
    assert not revoked_lines(lines) and rig.filed == []
    assert_released(rig)


def test_a_note_store_that_raises_still_prints_and_exits_77(rig, monkeypatch):
    script(monkeypatch, [(hml.GONE, "g-1")])
    rig.note_raises = True
    rc, lines = run(rig, FakeChat(rig))
    assert rc == 77 and len(revoked_lines(lines)) == 1 and rig.filed == []
    assert_released(rig)


def test_the_pre_start_check_and_its_refusal_text_are_unchanged(rig, monkeypatch):
    """A revoke between the prepare and the start refuses with today's words and starts nothing."""
    res = hml.prepare_launch(rig.h, rig.entity, rig.ck.root)
    assert hml.launch_still_live(res) is None
    monkeypatch.setattr(hml, "load_registry", lambda *a, **k: ([], []))
    assert hml.launch_check(res) == (hml.GONE, res.ids[0], "was revoked")
    assert hml.launch_still_live(res) == (f"registry entry {res.ids[0]} was revoked after the launch files "
                                          "were written; launch again to write them from the registry")

    def boom(*a, **k):
        raise hml.HandsMcpError("the log is unreadable")
    monkeypatch.setattr(hml, "load_registry", boom)
    assert hml.launch_still_live(res) == ("the authority log could not be read again before the start "
                                          "(the log is unreadable)")


def test_the_pre_start_refusal_stops_the_launch_with_today_s_text(rig, monkeypatch):
    monkeypatch.setattr(hml, "launch_check", lambda res, now=None: (hml.UNREADABLE, "x"))
    started = []
    with pytest.raises(hc.HandsChatRefused) as ei:
        hc.launch_hands_chat(rig.entity, "claude", rig.ck.root, spawn=lambda a, c: started.append(1),
                             emit=lambda s: None)
    assert "the authority log could not be read again before the start (x); no chat was started" == str(ei.value) and started == []
    assert_released(rig)


def test_a_ctrl_c_during_the_check_does_not_end_the_chat(rig, monkeypatch):
    """Ctrl-C belongs to the vendor: one landing inside the liveness read skips that tick and the chat goes on."""
    calls = script(monkeypatch, [KeyboardInterrupt(), None])
    chat = FakeChat(rig, exit_after=4, rc=0)
    try:
        rc, lines = run(rig, chat)
    except KeyboardInterrupt:
        pytest.fail("a Ctrl-C inside the check escaped the watched wait")
    assert rc == 0 and not chat.stopped and len(calls) >= 3
    assert not revoked_lines(lines) and rig.filed == []
    assert_released(rig)


def test_any_exception_from_the_check_counts_as_unreadable_and_ends_at_the_limit(rig, monkeypatch):
    """A malformed grant can raise outside launch_check's own except: it is an unconfirmed read, never a live one,
    and never a traceback with no line, no note and no 77."""
    script(monkeypatch, [ValueError("bad stamp")] * 3)
    chat = FakeChat(rig)
    rc, lines = run(rig, chat)
    assert rc == 77 and chat.stopped
    [line] = revoked_lines(lines)
    assert line == ("hands chat: hands revoked: the hands grants could not be confirmed (ValueError: bad stamp); "
                    "this chat was ended")
    [note] = rig.filed
    assert note["what"] == line
    assert_released(rig)


def test_an_exception_below_the_limit_then_live_is_not_stopped(rig, monkeypatch):
    script(monkeypatch, [KeyError("id"), None])
    chat = FakeChat(rig, exit_after=4, rc=5)
    rc, lines = run(rig, chat)
    assert rc == 5 and not chat.stopped and not revoked_lines(lines)


def test_a_system_exit_from_the_check_still_propagates(rig, monkeypatch):
    """A stop signal's SystemExit raised inside the read keeps its orderly stop and its exit code."""
    script(monkeypatch, [SystemExit(143)])
    chat = FakeChat(rig)
    with pytest.raises(SystemExit) as ei:
        run(rig, chat)
    assert ei.value.code == 143 and chat.stopped and rig.filed == []
    assert_released(rig)


def test_survivors_after_a_revoke_are_named_with_their_pids_after_retried_sweeps_under_the_locks(rig, monkeypatch):
    """The revoked hands may still run: the line and the note must not say the chat simply ended. Leg 6 L3 r1
    (complement MED): a failed sweep after a revoke released both locks at once, leaving the revoked vendor running
    unwatched. Now the sweep is retried with both locks held, and a stub that refuses to die is named with its pids
    in a line that says it is still up; the exit is non-zero."""
    script(monkeypatch, [(hml.GONE, "g-1")])
    monkeypatch.setattr(hc, "_REVOKE_SWEEP_BACKOFF_S", 0.0)
    monkeypatch.setattr(hc, "_hands_pids", lambda uid: ["4242", "4243"])
    sweeps = []

    def sweep(user, *, uid, timeout=5.0):
        sweeps.append(locks(rig))
        return None if len(sweeps) == 1 else f"processes of {user} are still running"
    monkeypatch.setattr(hands_live, "sweep_hands_user", sweep)
    chat = FakeChat(rig)
    rc, lines = run(rig, chat)
    assert rc != 0 and rc == 77
    # pre-start, the revoke's sweep-first, the final one, then the retries: every one with both locks held
    assert len(sweeps) == 3 + hc._REVOKE_SWEEP_RETRIES and all(s == (True, True) for s in sweeps)
    user = rig.h.user
    want = (f"hands chat: hands revoked: registry entry g-1 was revoked or ran out; the revoked chat is STILL UP: "
            f"processes of {user} (pid 4242, 4243) survived {1 + hc._REVOKE_SWEEP_RETRIES} sweeps "
            f"(processes of {user} are still running); stop them by hand: sudo -u {user} /bin/kill -9 -- -1")
    assert [x for x in lines if "pid 4242" in x] == [want]
    assert "this chat was ended" not in "\n".join(lines)
    [note] = rig.filed
    assert note["what"] == want or note["detail"] == want
    assert_released(rig)


def test_a_sweep_that_succeeds_on_a_retry_reports_the_chat_ended(rig, monkeypatch):
    script(monkeypatch, [(hml.GONE, "g-1")])
    monkeypatch.setattr(hc, "_REVOKE_SWEEP_BACKOFF_S", 0.0)
    answers = iter([None, "still running", "still running", None])
    monkeypatch.setattr(hands_live, "sweep_hands_user", lambda user, *, uid, timeout=5.0: next(answers))
    rc, lines = run(rig, FakeChat(rig))
    assert rc == 77
    [line] = revoked_lines(lines)
    assert line.endswith("; this chat was ended")
    assert_released(rig)


def _real_notes(monkeypatch, tmp_path):
    monkeypatch.setenv("LEVAIN_HOME", str(tmp_path / "lh"))
    monkeypatch.setattr(hc.notes_mod, "NoteStore", RealNoteStore)
    return RealNoteStore(tmp_path / "lh" / "consent")


@pytest.mark.parametrize("vendor,agent", [("claude", "claude_code"), ("codex", "codex")])
def test_the_note_files_through_a_real_note_store(rig, monkeypatch, tmp_path, vendor, agent):
    store = _real_notes(monkeypatch, tmp_path)
    script(monkeypatch, [(hml.GONE, "g-1")])
    lines = []
    rc = hc.launch_hands_chat(rig.entity, vendor, rig.ck.root, spawn=FakeChat(rig), emit=lines.append)
    assert rc == 77
    [line] = revoked_lines(lines)
    notes, total = store.open_page(10)
    assert total == 1
    [n] = notes
    assert n.what == line and n.agent == agent and n.agent_session_id == hc.notes_mod.NO_SESSION
    assert_released(rig)


def test_a_long_or_multi_line_survivor_report_still_files_its_note(monkeypatch, tmp_path):
    """The store refuses a ``what`` past WHAT_MAX or with a line break; the sweep's words can be both."""
    store = _real_notes(monkeypatch, tmp_path)
    left = "processes of levain-hands were left after the chat (" + "pid 1 claude\n" * 40 + ")"
    lines = []
    hc._report_revoked("registry entry g-1 was revoked or ran out", "claude", lines.append, left)
    [n], total = store.open_page(10)
    assert total == 1 and len(n.what) <= hc.notes_mod.WHAT_MAX and "\n" not in n.what
    assert n.what.startswith("hands chat: hands revoked: registry entry g-1 was revoked or ran out; "
                             "processes of levain-hands")
    assert n.detail == lines[0]


def test_a_vendor_with_no_note_agent_is_not_filed_as_codex(monkeypatch, tmp_path):
    """No guess: a vendor outside the map files nothing (the store refuses the name); the terminal line stands."""
    store = _real_notes(monkeypatch, tmp_path)
    lines = []
    hc._report_revoked("registry entry g-1 was revoked or ran out", "gemini", lines.append)
    assert len(lines) == 1 and store.open_page(10) == ([], 0)


# --- L3 r1 (1108-23) ----------------------------------------------------------------------------------------------

def _alpha(rig):
    """The rig's one registered entry, as signed."""
    from levain.authority import _thaw
    [g] = [_thaw(g) for g in rig.ck.state().policy.grants.values() if g.get("kind") == "hands-mcp"]
    return g


def test_a_revoke_between_ticks_ends_the_chat(rig):
    """L1 MED 3: the running chat's watch on the REAL replay (no scripted check): a revoke appended while the chat
    runs ends it at the next read."""
    e = _alpha(rig)

    class Revoke(FakeChat):
        def wait(self, timeout=None):
            if self.waits == 0 and not self.stopped:
                self.rig.ck.link({"op": "revoke_grant", "grant_id": e["id"]})
            return super().wait(timeout)
    chat = Revoke(rig, exit_after=6, rc=0)
    rc, lines = run(rig, chat)
    assert rc == 77 and chat.stopped
    [line] = revoked_lines(lines)
    assert line == f"hands chat: hands revoked: registry entry {e['id']} was revoked; this chat was ended"
    assert_released(rig)


def test_a_revoke_then_a_create_of_the_same_id_between_ticks_ends_the_chat(rig):
    """codex HIGH: the log allows a revoked id to be created again (a new incarnation, authority ``grant_born``).
    The running chat's servers came from the old one, so the watch must see the id's birth move, not only its
    presence. Real append API, real liveness read."""
    e = _alpha(rig)

    class Swap(FakeChat):
        def wait(self, timeout=None):
            if self.waits == 0 and not self.stopped:
                self.rig.ck.link({"op": "revoke_grant", "grant_id": e["id"]})
                self.rig.ck.link({"op": "create_grant", "grant": e})       # the same id, born again
            return super().wait(timeout)
    chat = Swap(rig, exit_after=6, rc=0)
    assert e["id"] in rig.ck.state().policy.grants
    rc, lines = run(rig, chat)
    assert rc == 77 and chat.stopped
    [line] = revoked_lines(lines)
    assert line == f"hands chat: hands revoked: registry entry {e['id']} was revoked and made again under the same id; this chat was ended"
    assert_released(rig)


def test_a_renew_between_ticks_does_not_end_the_chat(rig):
    """The incarnation is the id's birth, which a renew keeps: only a later expiry, the chat goes on."""
    from datetime import datetime, timedelta, timezone

    from levain import authority as auth
    e = _alpha(rig)

    class Renew(FakeChat):
        def wait(self, timeout=None):
            if self.waits == 0 and not self.stopped:
                now = datetime.now(timezone.utc)
                self.rig.ck.link({"op": "renew", "grant_id": e["id"], "signed_at": auth._stamp(now),
                                  "expires_at": auth._stamp(now + timedelta(days=30))}, at=now)
            return super().wait(timeout)
    chat = Renew(rig, exit_after=4, rc=0)
    rc, lines = run(rig, chat)
    assert rc == 0 and not chat.stopped and not revoked_lines(lines)
    assert_released(rig)


@pytest.mark.parametrize("answers,exit_after", [([None, (hml.GONE, "g-1")], 2),
                                                ([(hml.UNREADABLE, "slow")] * 3, 3)])
def test_a_chat_that_exited_on_its_own_during_the_read_keeps_its_code(rig, monkeypatch, answers, exit_after):
    """codex/deepseek MED: the vendor exits while the read runs and the read then says revoked (or the limit is
    reached). The revoke ended nothing: the vendor's own code, no line, no note."""
    script(monkeypatch, answers)
    chat = FakeChat(rig, exit_after=exit_after, rc=9)
    rc, lines = run(rig, chat)
    assert rc == 9 and not chat.stopped
    assert not revoked_lines(lines) and rig.filed == []
    assert_released(rig)


def test_ctrl_c_faster_than_the_watch_does_not_starve_the_read(rig, monkeypatch):
    """codex/gemini: a Ctrl-C inside every blocking wait restarted a full WATCH_S and the read never ran. The read
    is due by the clock; a poll (timeout 0) is not where a Ctrl-C lands in this fake, as it is near-instant."""
    import time as _time
    monkeypatch.setattr(hc, "WATCH_S", 0.05)
    monkeypatch.setattr(hc, "_now", _time.monotonic)        # this fake sleeps for real
    script(monkeypatch, [(hml.GONE, "g-1")])

    class Mash(FakeChat):
        def wait(self, timeout=None):
            if self.stopped:
                return -15
            self.waits += 1
            if self.waits > 200:
                raise AssertionError("the read was starved by Ctrl-C")
            if timeout:
                _time.sleep(0.01)
                raise KeyboardInterrupt
            raise subprocess.TimeoutExpired("chat", timeout)
    chat = Mash(rig)
    rc, lines = run(rig, chat)
    assert rc == 77 and chat.stopped and len(revoked_lines(lines)) == 1


def test_on_a_revoke_the_hands_are_swept_before_the_graceful_stop(rig, monkeypatch):
    """complement MED: "end now". The hands account's processes go before _stop's terminate and its wait, so a vendor
    that traps SIGTERM does not keep the revoked servers for _STOP_TIMEOUT; the final sweep still runs, under both
    locks, and is the one reported."""
    script(monkeypatch, [(hml.GONE, "g-1")])
    chat = FakeChat(rig)
    rc, _ = run(rig, chat)
    assert rc == 77
    kinds = [e[0] for e in rig.events]
    assert kinds == ["sweep", "sweep", "terminate", "sweep"]    # pre-start, the revoke's, the stop, the final
    assert rig.events[1] == ("sweep", (True, True))
    assert_released(rig)


def test_a_stop_that_raises_on_a_revoke_still_reports(rig, monkeypatch):
    """deepseek MED, refuted on disk (_stop swallows every Exception, and calls no sweep): pinned so it stays so."""
    script(monkeypatch, [(hml.GONE, "g-1")])

    class Stubborn(FakeChat):
        def terminate(self):
            raise PermissionError("sudo runs as root")
    rc, lines = run(rig, Stubborn(rig))
    assert rc == 77 and len(revoked_lines(lines)) == 1 and len(rig.filed) == 1
    assert_released(rig)


# --- leg 6 fix 6 (L2 MED 6): the read has a deadline and the unreadable window is by the clock -----------------------

def test_a_read_that_hangs_counts_as_unreadable_and_ends_the_chat_within_the_window(rig, monkeypatch):
    """A replay that stalls (disk, a lock, a long key check) held the wait: nothing but a signal could end the chat.
    Now the read has READ_DEADLINE_S; while it still runs no second read starts and each tick is unreadable."""
    import threading
    monkeypatch.setattr(hc, "READ_DEADLINE_S", 0.05)
    release = threading.Event()
    calls = []

    def hang(res, now=None):
        calls.append(1)
        if len(calls) == 1:
            return None                     # the pre-start read
        release.wait(5)
        return None                         # a late "live" must never count
    monkeypatch.setattr(hml, "launch_check", hang)
    chat = FakeChat(rig, exit_after=10, rc=0)
    try:
        rc, lines = run(rig, chat)
    finally:
        release.set()
    assert rc == 77 and chat.stopped
    [line] = revoked_lines(lines)
    assert "could not be confirmed (the previous read has not finished after 0.05 s)" in line
    assert len(calls) == 2                  # the pre-start read and ONE watch read: a hung read is not piled on
    assert_released(rig)


def test_the_unreadable_window_is_judged_by_the_clock_not_by_a_tick_count(rig, monkeypatch):
    """The end comes UNREADABLE_S after the first unconfirmed read started, and that end is a read of its own (the
    watch is not left to the next WATCH_S tick past it)."""
    monkeypatch.setattr(hc, "UNREADABLE_S", 2.5)
    ended_at = []
    script(monkeypatch, [(hml.UNREADABLE, "slow")] * 50)

    class Clocked(FakeChat):
        def terminate(self):
            ended_at.append(self.rig.clock[0])
            super().terminate()
    rc, lines = run(rig, Clocked(rig))
    assert rc == 77
    # the first watch read starts at t=1 (WATCH_S); the end is the read at 1 + 2.5, not the tick at t=4
    assert ended_at == [3.5]


def test_a_flapping_log_cannot_hide_a_revoke(rig, monkeypatch):
    """Five unreadable reads and one readable, again and again, never reach the window (each readable read starts it
    again), and the reason that is safe: the read that confirms re-reads the whole log, so a revoke made while the
    log flapped is seen by the first readable read after it. Real log, real append."""
    real_check = hml.launch_check
    e = _alpha(rig)
    n = [0]

    def flap(res, now=None):
        n[0] += 1
        if n[0] == 1:
            return real_check(res, now)                         # the pre-start read
        if n[0] == 9:
            rig.ck.link({"op": "revoke_grant", "grant_id": e["id"]})
        if (n[0] - 2) % 6 < 5:
            raise OSError("flap")
        return real_check(res, now)
    monkeypatch.setattr(hml, "launch_check", flap)
    monkeypatch.setattr(hc, "UNREADABLE_S", 10.0)               # five unreadable ticks stay inside the window
    chat = FakeChat(rig)
    rc, lines = run(rig, chat)
    assert rc == 77 and chat.stopped
    [line] = revoked_lines(lines)
    assert line == f"hands chat: hands revoked: registry entry {e['id']} was revoked; this chat was ended"
    assert n[0] == 13                                           # the readable read after the revoke, not later


def test_an_interrupt_during_a_read_keeps_its_answer_and_is_not_a_timeout(monkeypatch):
    """Leg 6 L3 r1 (deepseek LOW, gemini MED): a KeyboardInterrupt in the join left the worker running with its box
    local to the interrupted call; the retry said "the previous read has not finished after 5 s" (a read cut short at
    once is not one past its deadline) and the worker's answer, a revoke included, was dropped."""
    import threading
    release = threading.Event()

    def check():
        release.wait(5)
        return (hml.GONE, "g-1")
    reader = hc._BoundedRead(check)
    real_join = threading.Thread.join
    hit = []

    def join(self, timeout=None):
        if self.name == "hands-chat-watch" and not hit:
            hit.append(1)
            raise KeyboardInterrupt
        return real_join(self, timeout)
    monkeypatch.setattr(threading.Thread, "join", join)
    with pytest.raises(KeyboardInterrupt):
        reader.read()
    release.set()
    assert reader.read() == (hml.GONE, "g-1")
    assert reader.thread is None                    # consumed: the next read is a fresh one


def test_an_interrupt_then_a_retry_within_the_deadline_waits_for_the_same_read(monkeypatch):
    """The retry joins the interrupted read for what is left of its deadline, never starting a second read."""
    import threading
    calls = []
    release = threading.Event()

    def check():
        calls.append(1)
        release.wait(5)
        return None
    reader = hc._BoundedRead(check)
    real_join = threading.Thread.join
    hit = []

    def join(self, timeout=None):
        if self.name == "hands-chat-watch" and not hit:
            hit.append(1)
            raise KeyboardInterrupt
        return real_join(self, timeout)
    monkeypatch.setattr(threading.Thread, "join", join)
    with pytest.raises(KeyboardInterrupt):
        reader.read()
    threading.Timer(0.2, release.set).start()
    assert reader.read() is None and calls == [1]


def test_an_interrupted_read_that_finishes_past_its_deadline_is_not_taken_as_live(monkeypatch):
    """Leg 6 L3 r2 (codex MED): after an interrupted join, ``late`` was never set, so a worker that finished after
    its deadline but before the retry had its answer taken as live: a stale None reset the unreadable window. Its
    answer is dropped like any late one (unreadable), and the read after that is a fresh one."""
    import threading
    import time as time_mod
    monkeypatch.setattr(hc, "READ_DEADLINE_S", 0.2)
    calls = []
    release = threading.Event()

    def check():
        calls.append(1)
        if len(calls) == 1:
            release.wait(5)
        return None
    reader = hc._BoundedRead(check)
    real_join = threading.Thread.join
    hit = []

    def join(self, timeout=None):
        if self.name == "hands-chat-watch" and not hit:
            hit.append(1)
            raise KeyboardInterrupt
        return real_join(self, timeout)
    monkeypatch.setattr(threading.Thread, "join", join)
    with pytest.raises(KeyboardInterrupt):
        reader.read()
    time_mod.sleep(0.35)                # past the 0.2 s deadline
    release.set()
    time_mod.sleep(0.1)                 # the worker has answered, late
    got = reader.read()
    assert got is not None and got[0] == hml.UNREADABLE and "did not finish within" in got[1]
    assert reader.read() is None and calls == [1, 1]


# --- leg 6 fix2 6 (L3 r1): launch_check fails closed on an unknown incarnation and reads the log with no entries ---

def _files(rig, ids, born):
    return hml.LaunchFiles("c", "x", [], [], {}, ids=ids, born=born, cockpit_root=rig.ck.root)


def test_a_missing_born_on_either_side_is_gone_not_the_same_incarnation(rig, monkeypatch):
    """complement LOW: None == None read as the same incarnation, so a revoke-then-recreate was invisible when
    grant_born lacked the id. An unknown incarnation ends the chat as revoked."""
    gid = next(iter(hml.load_registry(rig.ck.root)[0]))["id"]
    born: dict = {}
    hml.load_registry(rig.ck.root, born=born)
    assert born[gid] is not None
    assert hml.launch_check(_files(rig, [gid], {gid: born[gid]})) is None           # the same incarnation: live
    assert hml.launch_check(_files(rig, [gid], {})) == (hml.GONE, gid, hml.UNCONFIRMED)            # unknown when written
    real = hml.load_registry

    def no_born(root, now=None, *, born=None):
        out = real(root, now, born=born)
        if born is not None:
            born[gid] = None
        return out
    monkeypatch.setattr(hml, "load_registry", no_born)
    assert hml.launch_check(_files(rig, [gid], {gid: None})) == (hml.GONE, gid, hml.UNCONFIRMED)   # unknown on both sides


def test_with_no_entries_an_unreadable_log_is_still_unreadable(rig, monkeypatch):
    """codex MED: zero rendered entries returned None before reading the log, so an unreadable log never ended the
    chat and the 30 s deadline was bypassed."""
    assert hml.launch_check(_files(rig, [], {})) is None

    def boom(*a, **k):
        raise hml.HandsMcpError("the log is unreadable")
    monkeypatch.setattr(hml, "load_registry", boom)
    assert hml.launch_check(_files(rig, [], {})) == (hml.UNREADABLE, "the log is unreadable")


# --- leg 6 fix2 8 (L3 r1 smalls): the gone line names the real cause; any read error is a clean refusal -------------

def test_the_gone_cause_is_named_revoked_expired_or_remade(rig, monkeypatch):
    """deepseek LOW: every gone entry was "revoked or ran out", a rule tightened at render time included."""
    res = hml.prepare_launch(rig.h, rig.entity, rig.ck.root)
    gid = res.ids[0]
    monkeypatch.setattr(hml, "load_registry", lambda *a, **k: ([], []))
    assert hml.launch_check(res) == (hml.GONE, gid, "was revoked")
    monkeypatch.setattr(hml, "load_registry", lambda *a, **k: ([], [(gid, "rules")]))
    assert hml.launch_check(res) == (hml.GONE, gid, "is no longer live (rules)")
    assert hml.launch_still_live(res) == (f"registry entry {gid} is no longer live (rules) after the launch files "
                                          "were written; launch again to write them from the registry")


def test_the_watch_line_carries_the_cause(rig, monkeypatch):
    script(monkeypatch, [(hml.GONE, "g-1", "is no longer live (expired)")])
    rc, lines = run(rig, FakeChat(rig))
    assert rc == 77
    [line] = revoked_lines(lines)
    assert line == "hands chat: hands revoked: registry entry g-1 is no longer live (expired); this chat was ended"


@pytest.mark.parametrize("exc", [__import__("sqlite3").OperationalError("database is locked"), ValueError("bad stamp")])
def test_any_read_error_is_unreadable_so_the_pre_start_check_refuses_cleanly(rig, monkeypatch, exc):
    """deepseek LOW: launch_check caught only HandsMcpError and OSError, so launch_still_live raised a traceback."""
    res = hml.prepare_launch(rig.h, rig.entity, rig.ck.root)

    def boom(*a, **k):
        raise exc
    monkeypatch.setattr(hml, "load_registry", boom)
    kind, why = hml.launch_check(res)
    assert kind == hml.UNREADABLE and type(exc).__name__ in why
    assert hml.launch_still_live(res).startswith("the authority log could not be read again before the start")
