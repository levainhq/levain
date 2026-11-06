"""3b-8a S-C: the VM-harness driver around the cleanup journal (S-A) and the uid scan (S-B), against fakes.
No VM, no root, no tart, no socket."""
from __future__ import annotations

import json

import pytest

from scripts.vm_harness import driver
from scripts.vm_harness.driver import RESULT_HEADER, SlotPlan, journaled_slots, run_vm_lifecycle

PS_HEADER = "  PID     ELAPSED  %CPU UCOMM                UID\n"


class Crash(Exception):
    pass


class FakeTime:
    def __init__(self):
        self.t = 0.0

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Rig:
    """Records every call; each seam can be told to fail."""

    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.calls: list[tuple] = []
        self.ft = FakeTime()
        self.ps = PS_HEADER
        self.slot_ok = True
        self.alloc_exc = None
        self.mint_exc = None
        self.render_exc = None
        self.revoke_exc = None

    def alloc(self, slot, lease):
        self.calls.append(("alloc", slot, lease))
        if self.alloc_exc:
            raise self.alloc_exc

    def mint(self, slot, lease):
        self.calls.append(("mint", slot, lease))
        if self.mint_exc:
            raise self.mint_exc
        return f"sess-{slot}"

    def render(self, session):
        self.calls.append(("render", session))
        if self.render_exc:
            raise self.render_exc

    def revoke(self, session):
        self.calls.append(("revoke", session))
        if self.revoke_exc:
            raise self.revoke_exc

    def slot_check(self, slot):
        self.calls.append(("slot_check", slot))
        return self.slot_ok

    def run(self, plan=(SlotPlan(3, "L3"),), **kw):
        return run_vm_lifecycle(
            self.dir, plan, alloc=self.alloc, mint=self.mint, render=self.render, revoke=self.revoke,
            slot_check=self.slot_check, lister=lambda: self.ps, resolve=lambda n: 4000 + int(n[len("_levain_gw"):]),
            clock=self.ft.clock, sleep=self.ft.sleep, **kw)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def result_lines(rig):
    return (rig.dir / "RESULT.txt").read_text().splitlines()


def journal(rig):
    return [json.loads(x) for x in (rig.dir / "cleanup.jsonl").read_text().splitlines() if x.strip()]


# -- the RESULT header -------------------------------------------------------------------------------------

def test_header_says_incomplete_lifecycle_and_never_reads_active_as_health():
    assert "incomplete lifecycle" in RESULT_HEADER
    assert "FRONT-LOST" in RESULT_HEADER and "3c" in RESULT_HEADER
    assert "ACTIVE" in RESULT_HEADER and "not health" in RESULT_HEADER


def test_header_is_on_disk_before_the_first_alloc_and_survives_a_crash(rig):
    seen = {}

    def alloc(slot, lease):
        seen["at_alloc"] = result_lines(rig)
        raise Crash("died at alloc")

    rig.alloc = alloc
    res = rig.run()
    assert seen["at_alloc"] == [RESULT_HEADER]
    assert not res.ok
    assert result_lines(rig)[0] == RESULT_HEADER


# -- the happy path ----------------------------------------------------------------------------------------

def test_clean_run_passes_in_order_and_writes_the_verdict(rig):
    res = rig.run((SlotPlan(1, "La"), SlotPlan(2, "Lb")))
    assert res.ok and res.problems == []
    assert [c[0] for c in rig.calls if c[0] != "slot_check"] == [
        "alloc", "mint", "render", "alloc", "mint", "render", "revoke", "revoke"]
    assert [c for c in rig.calls if c[0] == "slot_check"] == [("slot_check", 1), ("slot_check", 2)]
    assert result_lines(rig)[0] == RESULT_HEADER
    assert result_lines(rig)[1:] == ["RESULT: PASS"]


def test_uid_scan_covers_each_slots_gw_account(rig):
    rig.ps = PS_HEADER + "  77  00:09  0.0 front   4002\n"   # slot 2's uid; the plan only used slot 1
    assert rig.run((SlotPlan(1, "La"),)).ok
    second = rig.dir / "second"   # a run is scoped to a fresh evidence dir
    second.mkdir()
    rig.dir = second
    res = rig.run((SlotPlan(1, "La"), SlotPlan(2, "Lb")))
    assert not res.ok and any("pid=77" in p for p in res.problems)


# -- the four reproduced shapes ----------------------------------------------------------------------------

def test_crash_between_journal_and_alloc_leaves_an_intent_and_the_slot_check_judges_it(rig):
    rig.alloc_exc = Crash("died between journal line and alloc")
    rig.slot_ok = False   # alloc may have committed: the id never reached the journal (cleanup.py known edge)
    res = rig.run()
    assert journal(rig) == [{"slot": 3, "lease": "L3", "intent": "alloc"}]
    assert ("revoke",) not in [c[:1] for c in rig.calls]   # no id, so nothing to REVOKE
    assert ("slot_check", 3) in rig.calls
    assert not res.ok
    assert any("Crash" in p and "died between" in p for p in res.problems)
    assert any("slot 3" in p for p in res.problems)
    assert result_lines(rig)[1].startswith("RESULT: FAIL")


def test_mint_never_returned_fails_even_though_nothing_was_revocable(rig):
    rig.mint_exc = Crash("mint reply lost")
    rig.slot_ok = False
    res = rig.run()
    kinds = [c[0] for c in rig.calls]
    assert kinds[:2] == ["alloc", "mint"] and set(kinds[2:]) == {"slot_check"}   # no render, no REVOKE; the check polls
    assert journal(rig) == [{"slot": 3, "lease": "L3", "intent": "alloc"}]
    assert not res.ok and any("slot 3" in p for p in res.problems)


def test_render_raising_after_spawn_still_revokes_and_scans(rig):
    rig.render_exc = Crash("render failed after the front spawned")
    rig.ps = PS_HEADER + "  88  00:03  0.0 front   4003\n"   # the orphan front blocked on its GO fd
    res = rig.run()
    assert ("revoke", "sess-3") in rig.calls
    assert not res.ok
    assert any("render failed after the front spawned" in p for p in res.problems)
    assert any("pid=88" in p for p in res.problems)
    assert rig.ft.t >= 60   # the scan waited out its deadline before reporting


def test_survivor_found_after_a_clean_body_fails_the_run(rig):
    rig.ps = PS_HEADER + "  77  00:09  0.0 front   4003\n"
    res = rig.run()
    assert not res.ok and any("pid=77" in p for p in res.problems)
    assert result_lines(rig)[1].startswith("RESULT: FAIL")
    assert any("pid=77" in line for line in result_lines(rig)[2:])   # the reasons are on disk too


# -- W12 slot-reuse check ----------------------------------------------------------------------------------

def test_slot_not_reusable_after_a_clean_body_fails(rig):
    rig.slot_ok = False
    res = rig.run()
    assert not res.ok and any("slot 3 is not reusable" in p for p in res.problems)


def test_slot_check_that_raises_is_a_problem_not_a_pass(rig):
    def boom(slot):
        raise RuntimeError("control-down")

    rig.slot_check = boom
    res = rig.run()
    assert not res.ok and any("slot 3" in p and "control-down" in p for p in res.problems)


def test_revoke_failure_fails_and_slot_check_still_runs(rig):
    rig.revoke_exc = RuntimeError("revoke refused")
    res = rig.run()
    assert not res.ok
    assert any("revoke refused" in p for p in res.problems)
    assert ("slot_check", 3) in rig.calls


# -- plumbing ----------------------------------------------------------------------------------------------

def test_keyboard_interrupt_still_writes_a_fail_line_and_propagates(rig):
    rig.render_exc = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        rig.run()
    assert ("revoke", "sess-3") in rig.calls
    assert result_lines(rig)[1].startswith("RESULT: FAIL")


def test_journaled_slots_reads_intent_and_session_lines_and_skips_torn_ones(tmp_path):
    p = tmp_path / "c.jsonl"
    p.write_text('{"slot":2,"lease":"L","intent":"alloc"}\n{"slot":2,"lease":"L","session":"x"}\n'
                 '{"slot":5,"lease":"L","intent":"alloc"}\n{"slot":7,"le')
    assert journaled_slots(p) == [2, 5]
    assert journaled_slots(tmp_path / "none.jsonl") == []


def test_empty_plan_is_refused(rig):
    with pytest.raises(ValueError, match="empty plan"):
        rig.run(())


# -- L3 r1 fix round: the harness must not report PASS falsely ---------------------------------------------

@pytest.mark.parametrize("answer", [{"reusable": False}, "leased", 1, None, [], {}])
def test_slot_check_passes_only_on_true_anything_else_is_malformed(rig, answer):
    rig.slot_ok = answer
    res = rig.run()
    assert not res.ok
    assert any("slot 3" in p and "malformed" in p for p in res.problems)
    assert result_lines(rig)[1].startswith("RESULT: FAIL")


def test_slot_check_is_polled_until_the_async_reaper_commits(rig):
    answers = iter([False, False, False, True])
    rig.slot_check = lambda slot: (rig.calls.append(("slot_check", slot)), next(answers))[1]
    res = rig.run()
    assert res.ok and res.problems == []
    assert [c for c in rig.calls if c[0] == "slot_check"] == [("slot_check", 3)] * 4
    assert rig.ft.t > 0   # it waited on the injected clock, not the wall


def test_slot_check_that_never_turns_true_fails_at_the_deadline(rig):
    rig.slot_ok = False
    res = rig.run(deadline_s=5.0)
    assert not res.ok and any("slot 3 is not reusable" in p for p in res.problems)
    assert 5.0 <= rig.ft.t < 60   # polled to the given deadline, then stopped


@pytest.mark.parametrize("exc", [KeyboardInterrupt, SystemExit])
def test_base_exception_still_runs_slot_checks_and_records_why_then_reraises(rig, exc):
    rig.render_exc = exc()
    rig.revoke_exc = RuntimeError("revoke refused")
    rig.slot_ok = False
    with pytest.raises(exc):
        rig.run()
    assert ("slot_check", 3) in rig.calls
    lines = result_lines(rig)
    assert lines[1].startswith("RESULT: FAIL")
    assert any("interrupted" in x and exc.__name__ in x for x in lines[2:])
    assert any("revoke refused" in x for x in lines[2:])      # the cleanup notes are on disk
    assert any("slot 3 is not reusable" in x for x in lines[2:])


def test_a_failed_final_write_does_not_replace_a_live_base_exception(rig, monkeypatch):
    real = driver._write_durable

    def full_disk(path, text, *, append, **kw):
        if append:
            raise OSError(28, "No space left on device")
        return real(path, text, append=append, **kw)

    monkeypatch.setattr(driver, "_write_durable", full_disk)
    rig.render_exc = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt) as ei:
        rig.run()
    assert any("RESULT.txt" in n and "No space left" in n for n in ei.value.__notes__)


def test_a_failed_final_write_with_no_live_exception_is_loud(rig, monkeypatch):
    real = driver._write_durable

    def full_disk(path, text, *, append, **kw):
        if append:
            raise OSError(28, "No space left on device")
        return real(path, text, append=append, **kw)

    monkeypatch.setattr(driver, "_write_durable", full_disk)
    with pytest.raises(OSError, match="No space left"):
        rig.run()


def test_a_zero_byte_write_is_an_error_not_an_endless_loop(tmp_path, monkeypatch):
    calls = {"n": 0}

    def zero(fd, data):
        calls["n"] += 1
        if calls["n"] > 50:
            raise AssertionError("looped on a zero-byte write")
        return 0

    monkeypatch.setattr(driver.os, "write", zero)
    with pytest.raises(OSError, match="zero"):
        driver._write_durable(tmp_path / "x.txt", "abc", append=False)


@pytest.mark.parametrize("existing", ["cleanup.jsonl", "RESULT.txt"])
def test_a_non_fresh_evidence_dir_is_refused_and_left_untouched(rig, existing):
    (rig.dir / existing).write_text("a previous run\n")
    with pytest.raises(FileExistsError):
        rig.run()
    assert (rig.dir / existing).read_text() == "a previous run\n"
    assert not rig.calls   # nothing ran: no alloc, no REVOKE of the old run's sessions
    assert sorted(x.name for x in rig.dir.iterdir()) == [existing]


def test_a_journal_read_failure_is_a_problem_and_falls_back_to_the_planned_slots(rig, monkeypatch):
    def unreadable(path):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(driver, "journaled_slots", unreadable)
    res = rig.run((SlotPlan(1, "La"), SlotPlan(2, "Lb")))   # returns, does not raise
    assert not res.ok
    assert any("journal" in p and "Input/output error" in p for p in res.problems)
    assert [c for c in rig.calls if c[0] == "slot_check"] == [("slot_check", 1), ("slot_check", 2)]
    assert result_lines(rig)[1].startswith("RESULT: FAIL")


def test_an_interrupt_inside_the_slot_check_itself_is_recorded_and_reraised(rig):
    def ctrl_c(slot):
        raise KeyboardInterrupt()

    rig.slot_check = ctrl_c
    with pytest.raises(KeyboardInterrupt):
        rig.run()
    lines = result_lines(rig)
    assert lines[1].startswith("RESULT: FAIL")
    assert any("interrupted" in x and "KeyboardInterrupt" in x for x in lines[2:])


# -- L3 land11 r1 fix round (1105-49 fix11): each test below was first run RED against bb583d1e -------------------------

@pytest.mark.parametrize("bad", [None, 123, "", b"sess", ["sess-3"]])
def test_a_mint_that_returns_no_session_id_string_fails_the_run_and_journals_nothing_as_a_session(rig, bad):
    rig.mint = lambda slot, lease: (rig.calls.append(("mint", slot, lease)), bad)[1]
    res = rig.run()
    assert not res.ok
    assert any(p.startswith("body: ") and "mint" in p for p in res.problems), res.problems
    assert result_lines(rig)[-2].startswith("RESULT: FAIL")
    assert not any("session" in e for e in journal(rig))      # a non-id is never journalled as the session, nor rendered
    assert not any(c[0] == "render" for c in rig.calls)


def test_the_result_file_write_fsyncs_the_evidence_directory_as_the_journal_append_does(tmp_path, monkeypatch):
    import os
    import stat
    kinds: list[str] = []
    real = os.fsync

    def recording(fd):                         # a recording proxy: what KIND of thing each fsync was handed
        kinds.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return real(fd)

    monkeypatch.setattr(driver.os, "fsync", recording)
    driver._write_durable(tmp_path / "RESULT.txt", "x\n", append=False, exclusive=True)
    assert kinds == ["file", "dir"]
    kinds.clear()
    driver._write_durable(tmp_path / "RESULT.txt", "y\n", append=True)
    assert kinds == ["file", "dir"]
