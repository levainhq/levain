"""The autonomic L3 r2 fixes (seat 1009+21, input: project_memory/1009-21_VAGUS/vagus_r2_out.txt).

Each test below was run first on the unfixed tree (0e0fd58) and failed there; each names the finding it
closes. The admission of an approved effect now asks the gate whether that decision is authority NOW
(``EfferentGate._authorize_admission``), outside the write transaction:

  1. the silence default (``on-loop``) is re-checked at admission like a person's approval: a decision
     written as ``on-loop`` for a rung nobody may approve unattended does not run (complement 1, codex 2);
  2. the rung the effect needs is derived again at admission, so a decision made below it does not run
     even when the risk inputs (the fence) did not change, and the reopened outcome names that rung
     (codex 1, codex 5, complement 6);
  3. a verifier that cannot run at admission holds the effect and keeps the approval (complement 2);
  4. the allowed-signers file is a trust anchor: one the operator's uid owns is refused (complement 4,
     ruled (a) by the desk 2026-10-09).
"""
from __future__ import annotations

from pathlib import Path

import levain.autonomic.confirm as confirm_mod
import levain.autonomic.gate as gate_mod
from levain.autonomic import Posture
from levain.autonomic.policy import risk_floor
from tests.autonomic_confirm_keys import SIGNER, confirm_signers, sign, signed_yes
from tests.test_autonomic_journal_wiring import HIGH, World
from tests.test_autonomic_l3_fixes import _before_admit, _hold_of


def test_an_on_loop_decision_on_a_confirm_hold_does_not_run(tmp_path, test_only_confined_executor):
    # complement 1 / codex 2: ``by="on-loop"`` skipped the signature at admission, and an already-decided
    # hold skipped the unattended check, so a row written as the silence default fired a CONFIRM effect
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("o1")
    hold_id = _hold_of(w, w.open_pending_id())["hold_id"]
    with w.journal.db.write() as conn:
        conn.execute("UPDATE holds SET decided = 1, decided_by = 'on-loop', decided_posture = 'CONFIRM' "
                     "WHERE hold_id = ?", (hold_id,))
    w.dispatch("o1")
    assert ("link1", "o1-1") not in w.outbox()
    assert w.journal.get_hold(hold_id)["decided"] is None             # reopened for a real decision


def test_a_rung_raised_by_policy_alone_reopens_a_signed_approval_at_that_rung(tmp_path, monkeypatch,
                                                                          test_only_confined_executor):
    # codex 1: the risk inputs did not change, so the fence did not, but the policy now puts them on a
    # higher rung; admission verified the signature at the rung it was given and fired. codex 5: the
    # reopened outcome reported the rung derived before admission.
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("p1")
    pid = w.open_pending_id()
    yes = signed_yes(w.gate, pid)

    def stricter(risk):
        return Posture.CONFIRM_ELEVATED if risk == HIGH else risk_floor(risk)
    _before_admit(w, "link-1", lambda: monkeypatch.setattr(gate_mod, "risk_floor", stricter))
    out = w.gate.resolve(pid, yes, chain_owned=True)
    assert not out.fired and ("link1", "p1-1") not in w.outbox()
    assert out.pending and out.posture is Posture.CONFIRM_ELEVATED
    assert w.journal.get_hold(_hold_of(w, pid)["hold_id"])["decided"] is None


def test_a_verifier_that_cannot_run_at_admission_keeps_the_approval(tmp_path, test_only_confined_executor):
    # complement 2: a verifier timeout or a missing binary read as "does not verify" and wiped a good
    # signed approval
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("v1")
    pid = w.open_pending_id()
    hold_id = _hold_of(w, pid)["hold_id"]
    real = w.gate._ssh_keygen
    _before_admit(w, "link-1", lambda: setattr(w.gate, "_ssh_keygen", Path("/nonexistent/ssh-keygen")))
    w.resolve_open(approve=True)
    assert ("link1", "v1-1") not in w.outbox()
    held = w.journal.get_hold(hold_id)
    assert held["decided"] is True and held["signature"]               # the approval stands
    w.gate._ssh_keygen = real
    w.dispatch("v1")                                                   # delivered again, verifiable now
    assert ("link1", "v1-1") in w.outbox()


def test_an_allowed_signers_file_the_operator_owns_is_refused(tmp_path, monkeypatch, test_only_confined_executor):
    # complement 4: only ``is_file()`` was checked, so the operator's uid could enrol its own key
    message = b"any challenge"
    signature = sign(message)
    assert confirm_mod.verify_signature(message, signature=signature, signer=SIGNER,
                                        allowed_signers=confirm_signers())   # the test seam: uid allowed
    monkeypatch.setattr(confirm_mod, "_SIGNERS_FILE_OWNERS", frozenset({0}), raising=False)   # production
    assert not confirm_mod.verify_signature(message, signature=signature, signer=SIGNER,
                                            allowed_signers=confirm_signers())
    w = World(tmp_path)
    w.mint(chain=True)
    w.dispatch("a1")
    assert not w.resolve_open(approve=True).completed
    assert ("link1", "a1-1") not in w.outbox()
