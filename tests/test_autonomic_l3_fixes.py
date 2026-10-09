"""The autonomic L3 fixes (seat 1008+13, input: project_memory/1008-13_REVIEWS/vagus_L3_out.txt).

Ruling (Phill, 2026-10-08): "a delivered 'yes' is never authority". Each test below was run first on the
unfixed tree (f0f2da2) and failed there; each names the finding it closes.

  1. a person's approval is re-verified at the effect's admission (the point of use), over a challenge
     rebuilt for the CURRENT risk fence: an unsigned approval, or one signed under another fence, does
     not run, and its hold reopens for a new signed decision (codex 1, codex 2, complement 5);
  2. the fence is a digest of the risk inputs themselves, read again at admission (codex 4, complement 4);
  3. a binding's effect is refused up front, before a hold, a decision or a one-shot's claim, because no
     executor can prove confinement yet (codex 3, codex 5, complement 1, complement 8);
  4. ``ssh-keygen`` is an absolute, root-owned binary, never found through PATH (codex 6, complement 3);
  5. a manual pending's signature is verified against the record actually claimed (complement 2);
  6. one signature is good for one store and one decision (complement 6);
  7. ``verify_signature`` never raises (codex 7).
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import dataclasses
from pathlib import Path

import levain.autonomic.confirm as confirm_mod
import levain.autonomic.gate as gate_mod
from levain.autonomic import (
    ActionRisk, BindingStatus, ConfirmDecision, EfferentGate, GateReceiptStore,
    PendingActionStore, Posture, RiskClass, RunJournal, RunRef, binding_invocation,
)
from levain.autonomic.journal import EffectStatus
from tests.autonomic_confirm_keys import SIGNER, confirm_signers, sign, signed_yes
from tests.autonomic_test_only_confinement import assume_confined_for_this_test
from tests.test_autonomic_journal_wiring import (
    FINANCIAL, HIGH, LOW, OutboxExecutor, World, _one_shot, _single_builder,
)

# a risk whose floor is the same rung as HIGH's (CONFIRM) but whose content is not HIGH's
HIGH_SAME_RUNG = ActionRisk(cls=RiskClass.MEDIUM, reversible=False, external=True, financial=False)


def _before_admit(w: World, effect_id: str, change) -> None:
    """Run ``change`` once, after the rung of ``effect_id`` was decided and before it is admitted."""
    real, once = w.journal.effect, []

    def effect(run_id, eid, **kw):
        if eid == effect_id and not once:
            once.append(1)
            change()
        return real(run_id, eid, **kw)
    w.journal.effect = effect


def _effect(j: RunJournal, run_id: str, effect_id: str, **kw):
    """``RunJournal.effect`` under a fence that reads back unchanged. (The fail-first run of this file on
    f0f2da2 passed that tree's ``risk_revision=0`` here instead.)"""
    return j.effect(run_id, effect_id, fence="f", fence_now=lambda conn: "f", **kw)


def _hold_of(w: World, pending_id: str) -> dict:
    h = w.journal.find_pending(pending_id)
    assert h is not None
    return h


# --- 1. the signed hold, re-verified at admission ----------------------------------------------------

def test_an_unsigned_approval_left_by_an_earlier_version_reopens_instead_of_running(tmp_path, test_only_confined_executor):
    # codex 1: on e3917e1 a human CONFIRM wrote decided=1 with no signature. Its effect had not run; the
    # store format did not change, so after an upgrade the re-delivered event fired it unsigned.
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("u1")
    pid = w.open_pending_id()
    hold_id = _hold_of(w, pid)["hold_id"]
    with w.journal.db.write() as conn:   # what the earlier version wrote for a delivered "yes"
        conn.execute("UPDATE holds SET decided = 1, decided_by = 'human', decided_posture = 'CONFIRM' "
                     "WHERE hold_id = ?", (hold_id,))
    out = w.dispatch("u1")
    assert ("link1", "u1-1") not in w.outbox()
    assert w.journal.get_hold(hold_id)["decided"] is None             # reopened, not approved-unrun
    assert w.journal.approved_unrun() == []
    assert out.chain.paused and not out.chain.reason.startswith("approved_not_yet_run")
    assert pid in [p.pending_id for p in w.gate.open_pendings()]
    assert w.resolve_open(approve=True).completed                     # a new signed decision runs it
    assert ("link1", "u1-1") in w.outbox()


def test_journal_decide_without_a_signature_is_not_an_admissible_human_approval(tmp_path):
    # complement 5: RunJournal.decide is public; a "yes" written through it ran with no signature
    j = RunJournal(tmp_path / "store")
    j.start("r1", binding_id="b1")
    ran: list[int] = []
    held = _effect(j, "r1", "pay", digest="p", fn=lambda: ran.append(1), needs_decision=True)
    assert held.status is EffectStatus.HELD
    j.decide(held.hold_id, approve=True, digest="p", by="human")
    out = _effect(j, "r1", "pay", digest="p", fn=lambda: ran.append(1), needs_decision=True)
    assert ran == [] and out.status is not EffectStatus.DONE
    assert j.get_hold(held.hold_id)["decided"] is None


def test_a_signature_under_an_earlier_fence_reopens_the_hold(tmp_path, test_only_confined_executor):
    # codex 2: the signature named one classification; the effect was admitted under another (here the
    # rung did not rise, so no rung check notices). The hold reopens for a signature over the new one.
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("s1")
    pid = w.open_pending_id()
    yes = signed_yes(w.gate, pid)
    _before_admit(w, "link-1", lambda: w.tool_risk.__setitem__(1, HIGH_SAME_RUNG))
    out = w.resolve_open_as(yes)
    assert ("link1", "s1-1") not in w.outbox()
    assert out.paused and out.pending_id == pid
    assert w.journal.get_hold(_hold_of(w, pid)["hold_id"])["decided"] is None
    assert w.journal.approved_unrun() == []
    w.journal.effect = RunJournal.effect.__get__(w.journal)
    assert w.resolve_open_as(yes).paused                              # the old signature: still open
    assert w.resolve_open(approve=True).completed                     # signed over the current fence
    assert ("link1", "s1-1") in w.outbox()


def test_a_risk_rising_after_a_signed_confirm_reopens_it_at_the_raised_rung(tmp_path, test_only_confined_executor):
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("s2")
    pid = w.open_pending_id()
    _before_admit(w, "link-1", lambda: w.tool_risk.__setitem__(1, FINANCIAL))
    out = w.resolve_open(approve=True)
    assert out.paused and ("link1", "s2-1") not in w.outbox()
    w.journal.effect = RunJournal.effect.__get__(w.journal)
    assert b'"rung":"CONFIRM_ELEVATED"' in w.gate.confirm_challenge(pid)


# --- 2. the content-derived fence ---------------------------------------------------------------------

def test_a_reclassification_made_without_any_call_is_still_fenced(tmp_path, test_only_confined_executor):
    # codex 4 / complement 4: the revision counter moved only if the editor called revise_risk; a change
    # made any other way was admitted at the old rung. The fence is now the risk inputs themselves.
    w = World(tmp_path)
    w.mint(chain=False)
    _before_admit(w, "link-0", lambda: w.tool_risk.__setitem__(0, HIGH))
    first = w.dispatch("f1").outcome
    assert not first.fired and first.held and w.outbox() == []
    w.journal.effect = RunJournal.effect.__get__(w.journal)
    again = w.dispatch("f1").outcome                                  # decided again under the new risk
    assert again.pending and again.posture is Posture.CONFIRM and w.outbox() == []


def test_risk_inputs_that_cannot_be_read_at_admission_hold_the_effect(tmp_path, test_only_confined_executor):
    w = World(tmp_path)
    w.mint(chain=False)
    _before_admit(w, "link-0", lambda: w.tool_risk.pop(0))
    out = w.dispatch("f2").outcome
    assert out.held and not out.fired and not out.refused and w.outbox() == []
    w.tool_risk[0] = LOW
    w.journal.effect = RunJournal.effect.__get__(w.journal)
    assert w.dispatch("f2").outcome.fired and w.outbox() == [("link0", "f2-0")]


def test_an_unrelated_reclassification_does_not_disturb_an_effect(tmp_path, test_only_confined_executor):
    # the fence is this effect's own risk inputs: another tool's change is not a reason to stop it
    w = World(tmp_path)
    w.mint(chain=False)
    _before_admit(w, "link-0", lambda: w.tool_risk.__setitem__(1, FINANCIAL))
    assert w.dispatch("f3").outcome.fired


# --- 3. no binding effect until an executor can prove confinement ------------------------------------

class _DeclaresConfined(OutboxExecutor):
    confined = True   # a declaration is not proof: the gate no longer reads it


def test_a_binding_is_refused_before_its_one_shot_is_claimed_or_its_run_admitted(tmp_path):
    w = World(tmp_path)
    w.gate._executor = _DeclaresConfined(tmp_path / "outbox.jsonl")
    b = _one_shot(w)
    [d] = w.dispatcher.dispatch({"type": "email", "id": "c1", "fields": {"from": "a@x.example"}})
    assert d.outcome.refused and d.outcome.reason == "executor_not_confined" and w.outbox() == []
    assert w.store.get(b.binding_id).status is BindingStatus.ACTIVE   # the one-shot was not spent
    assert w.store.claimed_run(b.binding_id) is None
    with w.journal.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_a_confirm_class_binding_opens_no_hold_while_no_executor_is_confined(tmp_path):
    w = World(tmp_path)
    w.mint(chain=True)
    d = w.dispatch("c2")
    assert d.outcome.refused and d.outcome.reason == "executor_not_confined"
    assert w.gate.open_pendings() == [] and w.outbox() == []
    with w.journal.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0


def test_the_gate_refuses_a_journaled_request_up_front(tmp_path):
    w = World(tmp_path)
    b = w.store.get(w.mint(chain=False).binding_id)
    event = {"type": "email", "id": "g1", "fields": {"from": "a@x.example"}}
    req = dataclasses.replace(_single_builder(b, event), risk=LOW, authority=binding_invocation(b, hops=0),
                              ratified_posture=b.posture, run=RunRef("run-g1", "link-0"))
    out = w.gate.gate(req)
    assert out.refused and out.reason == "executor_not_confined"
    with w.journal.db.read() as conn:
        assert conn.execute("SELECT COUNT(*) FROM holds").fetchone()[0] == 0


def test_a_signed_yes_writes_no_decision_while_no_executor_is_confined(tmp_path, monkeypatch):
    # complement 1 / codex 5: the signed approval was written, then the fire found the executor
    # unconfined and the hold sat approved-unrun, its grant spent
    class Unconfined(OutboxExecutor):
        confined = False
    assume_confined_for_this_test(monkeypatch)
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("c3")                                                   # opened while confinement held
    monkeypatch.undo()
    w.gate._executor = Unconfined(tmp_path / "outbox.jsonl")
    pid = w.open_pending_id()
    out = w.gate.resolve(pid, signed_yes(w.gate, pid), chain_owned=True)
    assert out.refused and out.reason == "executor_not_confined"
    assert _hold_of(w, pid)["decided"] is None and w.journal.approved_unrun() == []
    assert ("link1", "c3-1") not in w.outbox()


def test_no_test_double_or_production_executor_declares_confinement():
    src = Path(gate_mod.__file__).read_text()
    assert '"confined"' not in src and "'confined'" not in src


# --- 4. ssh-keygen is not found through PATH --------------------------------------------------------

_FAKE_SIG = "-----BEGIN SSH SIGNATURE-----\nAAAA\n-----END SSH SIGNATURE-----\n"


def test_a_planted_ssh_keygen_on_path_cannot_forge_a_verdict(tmp_path, monkeypatch):
    fake = tmp_path / "bin" / "ssh-keygen"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\necho \'Good "levain-confirm" signature for operator with ED25519 key SHA256:x\'\nexit 0\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ.get('PATH', '')}")
    assert subprocess.run(["ssh-keygen"], capture_output=True).stdout.startswith(b"Good")   # it is planted
    assert confirm_mod.verify_signature(b"msg", signature=_FAKE_SIG, signer=SIGNER,
                                        allowed_signers=confirm_signers()) is False


def test_an_ssh_keygen_the_user_owns_is_refused_even_for_a_real_signature(tmp_path):
    msg = b"levain test message"
    sig = sign(msg)
    assert confirm_mod.verify_signature(msg, signature=sig, signer=SIGNER, allowed_signers=confirm_signers())
    mine = tmp_path / "ssh-keygen"
    shutil.copyfile(confirm_mod.DEFAULT_SSH_KEYGEN, mine)
    mine.chmod(0o755)
    assert confirm_mod.verify_signature(msg, signature=sig, signer=SIGNER, allowed_signers=confirm_signers(),
                                        ssh_keygen=mine) is False
    assert "root" in (confirm_mod.ssh_keygen_problem(mine) or "")


def test_a_group_or_world_writable_ssh_keygen_is_refused(monkeypatch):
    real_stat = os.lstat

    def writable(path, *a, **k):
        st = real_stat(path, *a, **k)
        return os.stat_result((st.st_mode | stat.S_IWOTH,) + tuple(st)[1:4] + (0,) + tuple(st)[5:])
    monkeypatch.setattr(confirm_mod.os, "lstat", writable)
    assert "writable" in (confirm_mod.ssh_keygen_problem(confirm_mod.DEFAULT_SSH_KEYGEN) or "")


# --- 5. the manual pending: verified against the record claimed ---------------------------------------

class _Recorder:
    name = "rec"

    def __init__(self) -> None:
        self.calls: list = []

    def execute(self, action_name, payload, *, context_id):
        from levain.autonomic import ExecutionResult
        self.calls.append(action_name)
        return ExecutionResult(ok=True, detail="ok")


def test_a_manual_yes_is_verified_against_the_record_actually_claimed(tmp_path):
    from tests.test_autonomic_transport import MANIFEST, RecordingTransport, fixed_clock, make_email_request

    class Slippery(PendingActionStore):
        def get(self, pending_id):   # the record is not there when looked up, and is there when claimed
            return None
    ex = _Recorder()
    gate = EfferentGate(confirm_signers=confirm_signers(), manifest=MANIFEST,
                        store=GateReceiptStore(tmp_path / "r.jsonl"), executor=ex, clock=fixed_clock,
                        transport=RecordingTransport(), pending_store=Slippery(tmp_path / "p.json"))
    proposed = gate.gate(make_email_request())
    assert proposed.pending
    out = gate.resolve(proposed.pending_id, ConfirmDecision(approved=True, by="human"))
    assert not out.fired and out.reason == "integrity:unverified_signature" and ex.calls == []


# --- 6. one signature, one store, one decision -------------------------------------------------------

def test_a_signature_from_one_store_is_not_authority_in_another(tmp_path, test_only_confined_executor):
    a, b = World(tmp_path / "a"), World(tmp_path / "b")
    for w in (a, b):
        w.mint(chain=True)
        w.dispatch("x1")
    pa, pb = a.open_pending_id(), b.open_pending_id()
    assert pa == pb                                                   # the same event, the same pending
    assert a.gate.confirm_challenge(pa) != b.gate.confirm_challenge(pb)
    out = b.resolve_open_as(signed_yes(a.gate, pa))
    assert out.paused and ("link1", "x1-1") not in b.outbox()


# --- 7. verify_signature never raises ----------------------------------------------------------------

def test_verify_signature_returns_false_when_no_temporary_file_can_be_made(monkeypatch):
    monkeypatch.setattr(confirm_mod.tempfile, "mkstemp", lambda *a, **k: (_ for _ in ()).throw(OSError("ro")))
    assert confirm_mod.verify_signature(b"m", signature=_FAKE_SIG, signer=SIGNER,
                                        allowed_signers=confirm_signers()) is False
