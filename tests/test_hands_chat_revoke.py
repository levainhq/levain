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
    monkeypatch.setattr(hc, "WATCH_S", 0.01)
    monkeypatch.setattr(hc, "UNREADABLE_LIMIT", 3)
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
    # the final sweep ran with both locks still held, after the stop
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
    assert hml.launch_check(res) == (hml.GONE, res.ids[0])
    assert hml.launch_still_live(res) == (f"registry entry {res.ids[0]} was revoked or ran out after the launch "
                                          "files were written; launch again to write them from the registry")

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


def test_survivors_after_a_revoke_are_named_and_the_exit_is_non_zero(rig, monkeypatch):
    """The revoked hands may still run: the line and the note must not say the chat simply ended."""
    script(monkeypatch, [(hml.GONE, "g-1")])
    sweeps = []

    def sweep(user, *, uid, timeout=5.0):
        sweeps.append(user)
        return None if len(sweeps) == 1 else "pid 4242 is still running"
    monkeypatch.setattr(hands_live, "sweep_hands_user", sweep)
    chat = FakeChat(rig)
    rc, lines = run(rig, chat)
    assert rc != 0 and rc == 77
    user = sweeps[-1]
    want = (f"hands chat: hands revoked: registry entry g-1 was revoked or ran out; the chat was stopped but "
            f"processes of {user} were left after the chat (pid 4242 is still running)")
    assert [x for x in lines if "pid 4242" in x] == [want]
    assert "this chat was ended" not in "\n".join(lines)
    [note] = rig.filed
    assert note["what"] == want
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
    assert n.what.startswith("hands chat: hands revoked: registry entry g-1 was revoked or ran out; the chat was "
                             "stopped but processes of levain-hands")
    assert n.detail == lines[0]


def test_a_vendor_with_no_note_agent_is_not_filed_as_codex(monkeypatch, tmp_path):
    """No guess: a vendor outside the map files nothing (the store refuses the name); the terminal line stands."""
    store = _real_notes(monkeypatch, tmp_path)
    lines = []
    hc._report_revoked("registry entry g-1 was revoked or ran out", "gemini", lines.append)
    assert len(lines) == 1 and store.open_page(10) == ([], 0)
