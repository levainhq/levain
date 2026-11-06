"""3b-8a S-C: the VM-harness driver around the cleanup journal (S-A) and the uid scan (S-B), against fakes.
No VM, no root, no tart, no socket."""
from __future__ import annotations

import json

import pytest

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
    assert [c[0] for c in rig.calls] == ["alloc", "mint", "slot_check"]   # render never ran, no REVOKE
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
