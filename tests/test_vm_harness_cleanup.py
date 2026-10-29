"""3b-8a cleanup journal (S-A) and the ``_levain_gw<n>`` uid scan (S-B), against fakes. No VM, no root."""
from __future__ import annotations

import json

import pytest

from scripts.vm_harness.cleanup import (
    CleanupFailed, CleanupJournal, Survivor, parse_ps, run_with_cleanup, scan_survivors, start_session, walk_journal,
)

PS_HEADER = "  PID     ELAPSED  %CPU UCOMM                UID\n"


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines() if x.strip()]


class Crash(Exception):
    pass


class FakeTime:
    """Injectable clock + sleep: sleeping advances the clock, nothing really waits."""

    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.sleeps += 1
        self.t += s


def fast(ft):
    return {"clock": ft.clock, "sleep": ft.sleep}


# -- S-A: the journal ----------------------------------------------------------------------------------------

def test_intent_line_is_on_disk_before_alloc_runs(tmp_path):
    j = CleanupJournal(tmp_path / "cleanup.jsonl")
    seen = {}

    def alloc(slot, lease):
        seen["at_alloc"] = lines(tmp_path / "cleanup.jsonl")   # read from disk, inside the call
        raise Crash("died between journal line and alloc")

    with pytest.raises(Crash):
        start_session(j, 3, "L1", alloc=alloc, mint=lambda *a: "S", render=lambda s: None)
    assert seen["at_alloc"] == [{"slot": 3, "lease": "L1", "intent": "alloc"}]


def test_session_id_journaled_when_mint_returns_before_render(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    seen = {}

    def render(sid):
        seen["at_render"] = lines(tmp_path / "c.jsonl")
        raise Crash("render failed after spawn")

    with pytest.raises(Crash):
        start_session(j, 1, "L", alloc=lambda s, l: None, mint=lambda s, l: "sess-9", render=render)
    assert seen["at_render"][-1] == {"slot": 1, "lease": "L", "session": "sess-9"}


def test_entries_are_fsynced(tmp_path, monkeypatch):
    import os
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    j = CleanupJournal(tmp_path / "c.jsonl")
    j.append({"slot": 1, "lease": "L", "intent": "alloc"})
    assert synced


def test_walk_revokes_every_session_even_when_one_revoke_raises(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    for s in ("a", "b", "c"):
        j.append({"slot": 1, "lease": "L", "session": s})
    called = []

    def revoke(sid):
        called.append(sid)
        if sid == "b":
            raise RuntimeError("control-down")

    failures = walk_journal(j.path, revoke)
    assert called == ["a", "b", "c"]
    assert [(f.session, "control-down" in f.error) for f in failures] == [("b", True)]


def test_walk_tolerates_a_torn_last_line_and_intent_only_entries(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"slot":1,"lease":"L","intent":"alloc"}\n{"slot":1,"lease":"L","session":"x"}\n{"slot":2,"le')
    called = []
    assert walk_journal(p, called.append) == []
    assert called == ["x"]


def test_walk_of_missing_journal_is_empty(tmp_path):
    assert walk_journal(tmp_path / "none.jsonl", lambda s: 1 / 0) == []


def test_torn_previous_line_does_not_swallow_the_next_entry(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"slot":2,"le')   # a crash left a torn tail with no newline
    CleanupJournal(p).append({"slot": 1, "lease": "L", "session": "y"})
    called = []
    walk_journal(p, called.append)
    assert called == ["y"]


def test_append_loops_over_short_writes(tmp_path, monkeypatch):
    import os
    real = os.write
    monkeypatch.setattr(os, "write", lambda fd, b: real(fd, bytes(b[:3])))   # every write is short
    j = CleanupJournal(tmp_path / "c.jsonl")
    j.append({"slot": 1, "lease": "L", "session": "whole-session-id"})
    assert lines(j.path) == [{"slot": 1, "lease": "L", "session": "whole-session-id"}]


def test_journal_directory_must_pre_exist(tmp_path):
    with pytest.raises(FileNotFoundError, match="journal directory"):
        CleanupJournal(tmp_path / "nope" / "c.jsonl")


def test_finally_walks_after_body_crash_and_notes_failures(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    revoked = []

    def body(jr):
        jr.append({"slot": 1, "lease": "L", "session": "s1"})
        raise Crash("boom")

    def revoke(sid):
        revoked.append(sid)
        raise RuntimeError("nope")

    with pytest.raises(Crash) as ei:
        run_with_cleanup(j, body, revoke, uids=[], lister=lambda: "")
    assert revoked == ["s1"]
    assert any("s1" in n for n in ei.value.__notes__)


def test_journal_walk_error_does_not_mask_the_body_exception(tmp_path):
    p = tmp_path / "c.jsonl"
    j = CleanupJournal(p)
    p.mkdir()   # reading the journal now raises IsADirectoryError

    with pytest.raises(Crash) as ei:
        run_with_cleanup(j, lambda jr: (_ for _ in ()).throw(Crash("boom")), lambda s: None, uids=[],
                         lister=lambda: "")
    assert any("journal walk failed" in n for n in ei.value.__notes__)


def test_journal_walk_error_still_runs_the_uid_scan(tmp_path):
    p = tmp_path / "c.jsonl"
    j = CleanupJournal(p)
    p.mkdir()
    ft = FakeTime()
    ps = PS_HEADER + "  77  00:09  0.0 front   4002\n"
    with pytest.raises(CleanupFailed) as ei:
        run_with_cleanup(j, lambda jr: None, lambda s: None, uids=[4002], lister=lambda: ps, **fast(ft))
    assert "journal walk failed" in str(ei.value) and "77" in str(ei.value)


def test_clean_body_with_revoke_failure_fails_the_run(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")

    def body(jr):
        jr.append({"slot": 1, "lease": "L", "session": "s1"})

    def revoke(sid):
        raise RuntimeError("nope")

    with pytest.raises(CleanupFailed) as ei:
        run_with_cleanup(j, body, revoke, uids=[], lister=lambda: "")
    assert "s1" in str(ei.value)


def test_clean_run_passes(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    run_with_cleanup(j, lambda jr: jr.append({"slot": 1, "lease": "L", "session": "s"}), lambda s: None,
                     uids=[], lister=lambda: "")


# -- S-B: the uid scan ---------------------------------------------------------------------------------------

def test_parse_ps_reads_the_five_columns():
    rows = parse_ps(PS_HEADER + "  101  00:05  0.0 levain-front        4001\n 1 10:00 0.1 launchd 0\n")
    assert [(r.pid, r.ucomm, r.uid) for r in rows] == [(101, "levain-front", 4001), (1, "launchd", 0)]


def test_survivor_found_fails_the_run(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    ps = PS_HEADER + "  77  00:09  0.0 front   4002\n  5  01:00  0.0 other 501\n"
    ft = FakeTime()
    with pytest.raises(CleanupFailed) as ei:
        run_with_cleanup(j, lambda jr: None, lambda s: None, uids=[4002], lister=lambda: ps, **fast(ft))
    assert "77" in str(ei.value)
    assert ft.t >= 60   # it waited out the default deadline before reporting


def test_survivor_that_exits_within_the_deadline_passes(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    ps = PS_HEADER + "  77  00:09  0.0 front   4002\n"
    ft = FakeTime()
    polls = {"n": 0}

    def lister():
        polls["n"] += 1
        return ps if polls["n"] <= 3 else PS_HEADER   # the reaper finishes after 3 polls

    run_with_cleanup(j, lambda jr: None, lambda s: None, uids=[4002], lister=lister, **fast(ft))
    assert polls["n"] == 4 and ft.t < 60


def test_empty_uids_is_an_explicit_opt_out_and_uids_is_required(tmp_path):
    j = CleanupJournal(tmp_path / "c.jsonl")
    with pytest.raises(TypeError):
        run_with_cleanup(j, lambda jr: None, lambda s: None)   # type: ignore[call-arg]
    run_with_cleanup(j, lambda jr: None, lambda s: None, uids=())


def test_scan_ignores_other_uids_and_no_argv_in_default_command():
    assert scan_survivors([4002], lambda: PS_HEADER + " 5 01:00 0.0 x 501\n") == []
    from scripts.vm_harness import cleanup
    assert cleanup.PS_COMMAND == ("ps", "-Ao", "pid,etime,pcpu,ucomm,uid")
    cols = set(cleanup.PS_COMMAND[2].split(","))
    assert cols == {"pid", "etime", "pcpu", "ucomm", "uid"}   # no args / command / comm-with-argv column


def test_scan_resolves_uid_names(tmp_path):
    rows = scan_survivors(["_levain_gw3"], lambda: PS_HEADER + " 9 00:01 0.0 f 4003\n",
                          resolve=lambda n: 4003 if n == "_levain_gw3" else None)
    assert rows == [Survivor(pid=9, etime="00:01", pcpu="0.0", ucomm="f", uid=4003)]


def test_unresolvable_uid_name_fails_closed():
    with pytest.raises(CleanupFailed):
        scan_survivors(["_levain_gw9"], lambda: PS_HEADER, resolve=lambda n: None)
