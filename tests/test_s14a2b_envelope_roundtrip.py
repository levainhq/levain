"""S14 A2b: root's envelope bytes pass dispatch's ``check_envelope`` against the event's digest (the one place a test
imports ``levain.dispatch.verifier_report``: root never does), and the projection to the contract's seven keys is what
makes them pass."""
from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from levain.contracts import canon
from levain.dispatch import verifier_report as VR
from levain.slot import records as R
from levain.slot import render as RD
from levain.slot import verifier_session as VS
from tests.test_slot_s14a2a_verifier_steps import S
from tests.test_slot_s2f2_runtime_helper import DRIVER_INPUT_DIGEST
from tests.test_slot_s14a2b_verifier_report import BODY, _report, rig, _start  # noqa: F401 - the rig fixture

STEPS = [{"argv": ["pytest", "-q"], "wall_s": 60}]


def _entry() -> dict:
    """A resolved entry that is consistent with the ticket's steps and driver input, plus the registry's own
    ``verifier_generation`` (the member alloc carries and the contract's envelope does not)."""
    e = {"verifier_id": "pytest-report", "declared_kind": "black-box", "driver_template": "output_compare",
         "driver_template_digest": "sha256:" + hashlib.sha256(RD.read_driver_template("output_compare") or b"").hexdigest(),
         "steps_digest": "sha256:" + hashlib.sha256(canon.canonical_bytes(STEPS)).hexdigest(),
         "driver_input_digest": DRIVER_INPUT_DIGEST}   # the ticket's own fact: root's envelope carries facts[...]
    e["verifier_artifact_digest"] = VR.artifact_digest(e)
    return e


def _consistent_alloc(w) -> VR.VerifierRun:
    a = w.alloc(S)
    entry = _entry()
    facts = dict(a.ticket_facts, verifier=dict(entry, verifier_generation=1))
    (w.L / "alloc" / f"{S}.json").write_bytes(R.alloc_bytes(replace(a, ticket_facts=facts)))
    return VR.VerifierRun(job_id="job-1", run=0, session=S, ticket=a.ticket, verifier=entry, steps=STEPS,
                          driver_input_digest=entry["driver_input_digest"],
                          input_bundle_digest=a.ticket_facts["input_bundle_digest"])


def test_the_seven_keys_are_dispatchs():
    assert set(VS.ENVELOPE_VERIFIER_KEYS) == VR.VERIFIER_KEYS and len(VS.ENVELOPE_VERIFIER_KEYS) == 7


def test_roots_bytes_pass_check_envelope(rig):
    w, la, table = rig
    run = _consistent_alloc(w)
    code, line = _start(w)
    assert line["ok"], w.last_err
    raw = _report(w)
    digest = w.state(S).fields["verifier_report_digest"]
    got = VR.check_envelope(raw, expected=run, event_digest=digest)
    assert got.digest == digest and got.result == "pass" and len(got.exits) == 1
    assert "verifier_generation" not in canon.parse(raw)["verifier"]


def test_mutation_dropping_the_projection_goes_red(rig, monkeypatch):
    """Carry alloc's whole resolved entry (``verifier_generation`` too) and dispatch refuses ``envelope-verifier``."""
    w, la, table = rig
    run = _consistent_alloc(w)
    monkeypatch.setattr(VS, "ENVELOPE_VERIFIER_KEYS", VS.ENVELOPE_VERIFIER_KEYS + ("verifier_generation",))
    code, line = _start(w)
    assert line["ok"], w.last_err
    with pytest.raises(VR.ReportRefused) as e:
        VR.check_envelope(_report(w), expected=run, event_digest=w.state(S).fields["verifier_report_digest"])
    assert e.value.name == "envelope-verifier"
