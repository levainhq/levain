"""The verifier report: the body schema and the envelope check, against FIXTURE envelope bytes built field for field
from the S1 contract's envelope (no S1 code writes one yet). Every name in REFUSALS is reached by one case."""
from __future__ import annotations

import hashlib

import pytest

from levain.contracts.canon import canonical_bytes
from levain.dispatch import DispatchError
from levain.dispatch import verifier_report as vr
from levain.dispatch.verifier_report import REFUSALS, ReportRefused, VerifierRun, body_problem, check_envelope

STEPS = ({"argv": ["python", "-m", "pytest", "-q"], "wall_s": 300}, {"argv": ["ruff", "check"], "wall_s": 60})
ENTRY = {"verifier_id": "pytest-bb", "verifier_artifact_digest": "sha256:" + "b" * 64, "declared_kind": "black-box",
         "driver_template": "pytest_bb", "driver_template_digest": "sha256:" + "c" * 64,
         "steps_digest": "sha256:" + hashlib.sha256(canonical_bytes(list(STEPS))).hexdigest(),
         "driver_input_digest": "sha256:" + "d" * 64}
EXIT = {"exit": 0, "signal": None, "wall_s": 12, "timed_out": False, "truncated_bytes": 0, "lingering_killed": 0}


def vrun(job_id="dj-" + "0" * 32, run=0, entry=None) -> VerifierRun:
    return VerifierRun(job_id=job_id, run=run, session=f"v-{run}", ticket=f"t-{run}", verifier=entry or ENTRY,
                       steps=STEPS, driver_input_digest=ENTRY["driver_input_digest"],
                       input_bundle_digest="sha256:" + "e" * 64)


def body(verdicts=("pass", "pass"), **over) -> dict:
    b = {"format": vr.REPORT_FORMAT, "result": "pass" if all(v == "pass" for v in verdicts) else "fail",
         "steps": [{"step": k, "verdict": v, "note": f"step {k} {v}"} for k, v in enumerate(verdicts)]}
    b.update(over)
    return b


def envelope_map(run: VerifierRun, b=None, **over) -> dict:
    e = {"session": run.session, "ticket": run.ticket, "verifier": dict(run.verifier),
         "driver_input_digest": run.driver_input_digest, "input_bundle_digest": run.input_bundle_digest,
         "steps": [dict(EXIT) for _ in run.steps], "body": body() if b is None else b}
    e.update(over)
    return e


def envelope(run: VerifierRun, b=None, **over) -> tuple[bytes, str]:
    raw = canonical_bytes(envelope_map(run, b, **over))
    return raw, "sha256:" + hashlib.sha256(raw).hexdigest()


def event(run: VerifierRun, *, at: str, **kw) -> dict:
    return {"seq": 1, "at": at, "kind": "state", "session": run.session, "ticket": run.ticket, "generation": 3,
            "state": "RETAINED", **kw}


def refused_name(raw, digest=None, run=None) -> str:
    if digest is None:
        digest = "sha256:" + hashlib.sha256(raw).hexdigest() if isinstance(raw, bytes) else "sha256:" + "0" * 64
    with pytest.raises(ReportRefused) as exc:
        check_envelope(raw, expected=run or vrun(), event_digest=digest)
    assert isinstance(exc.value, DispatchError) and exc.value.name in REFUSALS
    return exc.value.name


def canon(m) -> bytes:
    return canonical_bytes(m)


# --- the happy path ------------------------------------------------------------------------------------------------

def test_a_checked_envelope_returns_the_body_and_the_exit_records():
    run = vrun()
    raw, digest = envelope(run, body(("pass", "fail")))
    report = check_envelope(raw, expected=run, event_digest=digest)
    assert report.digest == digest and report.result == "fail" and len(report.exits) == 2
    assert [(s.step, s.verdict) for s in report.steps] == [(0, "pass"), (1, "fail")]
    assert report.first_failure.step == 1 and report.first_failure.note == "step 1 fail"


def test_a_pass_has_no_first_failure():
    run = vrun()
    raw, digest = envelope(run)
    assert check_envelope(raw, expected=run, event_digest=digest).first_failure is None


# --- one case per refusal name -------------------------------------------------------------------------------------

def _not_canonical() -> bytes:
    raw, _ = envelope(vrun())
    return raw.replace(b'{"body":', b'{ "body":', 1)


def _too_deep() -> bytes:
    deep: object = 1
    for _ in range(40):
        deep = [deep]
    return canon(envelope_map(vrun(), {"x": deep}))


CASES = {
    "report-missing": lambda: refused_name(None),
    "report-digest-mismatch": lambda: refused_name(envelope(vrun())[0], digest=envelope(vrun())[1][:-1] + "0"),
    "report-too-large": lambda: refused_name(canon(envelope_map(vrun(), body(steps=[{"step": 0, "verdict": "pass",
                                                                                      "note": "x" * (2 << 20)}])))),
    "report-not-canonical": lambda: refused_name(_not_canonical()),
    "envelope-members": lambda: refused_name(canon({**envelope_map(vrun()), "format": vr.ENVELOPE_FORMAT_NAME})),
    "envelope-session": lambda: refused_name(canon(envelope_map(vrun(), session="v-other"))),
    "envelope-ticket": lambda: refused_name(canon(envelope_map(vrun(), ticket="t-other"))),
    "envelope-verifier": lambda: refused_name(canon(envelope_map(vrun(), verifier={**ENTRY, "steps_digest":
                                                                                    "sha256:" + "9" * 64}))),
    "envelope-driver-input": lambda: refused_name(canon(envelope_map(vrun(), driver_input_digest="sha256:" + "8" * 64))),
    "envelope-input-bundle": lambda: refused_name(canon(envelope_map(vrun(), input_bundle_digest="sha256:" + "7" * 64))),
    "envelope-steps": lambda: refused_name(canon(envelope_map(vrun(), steps=[dict(EXIT)]))),
    "report-format": lambda: refused_name(canon(envelope_map(vrun(), body(format="levain-dispatch-verifier-report/2")))),
    "report-body": lambda: refused_name(canon(envelope_map(vrun(), body(model="glm-5")))),
    "report-step-count": lambda: refused_name(canon(envelope_map(vrun(), body(("pass",))))),
    "report-result-disagrees": lambda: refused_name(canon(envelope_map(vrun(), body(("pass", "fail"),
                                                                                     result="pass")))),
    "verifier-entry": lambda: refused_name(*envelope(minted(extra="x")), run=minted(extra="x")),
}


def test_every_refusal_name_has_a_case():
    assert set(CASES) == set(REFUSALS)


@pytest.mark.parametrize("name", REFUSALS)
def test_each_refusal_is_reached_by_its_case(name):
    assert CASES[name]() == name


def test_the_digest_is_checked_before_anything_is_parsed():
    assert refused_name(b"not json", digest="sha256:" + "1" * 64) == "report-digest-mismatch"


def test_nesting_too_deep_is_not_canonical():
    assert refused_name(_too_deep()) == "report-not-canonical"


@pytest.mark.parametrize("raw", [b"\xff\xfe", b'{"a":1,"a":2}', b'{"a":1.5}', "﻿{}".encode()])
def test_bytes_the_canonical_parser_refuses_are_not_canonical(raw):
    assert refused_name(raw) == "report-not-canonical"


@pytest.mark.parametrize("field, value", [
    ("exit", True), ("exit", -1), ("signal", "9"), ("wall_s", None), ("timed_out", 0), ("truncated_bytes", -1),
    ("lingering_killed", None)])
def test_an_exit_record_of_the_wrong_type_is_refused(field, value):
    steps = [dict(EXIT), {**EXIT, field: value}]
    assert refused_name(canon(envelope_map(vrun(), steps=steps))) == "envelope-steps"


def test_an_exit_record_with_another_member_is_refused():
    steps = [dict(EXIT), {**EXIT, "rss": 1}]
    assert refused_name(canon(envelope_map(vrun(), steps=steps))) == "envelope-steps"


def test_a_verifier_entry_is_compared_byte_for_byte_never_by_id():
    entry = {**ENTRY, "driver_template_digest": "sha256:" + "6" * 64}
    assert refused_name(canon(envelope_map(vrun(), verifier=entry))) == "envelope-verifier"


# --- the body ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("b, name", [
    ([], "report-body"),
    (body(kind="black-box"), "report-body"),
    (body(result="maybe"), "report-body"),
    (body(steps=[]), "report-body"),
    (body(steps=[{"step": 1, "verdict": "pass", "note": ""}]), "report-body"),
    (body(steps=[{"step": 0, "verdict": "skip", "note": ""}]), "report-body"),
    (body(steps=[{"step": 0, "verdict": "pass", "note": "x" * 513}]), "report-body"),
    (body(steps=[{"step": 0, "verdict": "pass"}]), "report-body"),
    (body(steps=[{"step": True, "verdict": "pass", "note": ""}]), "report-body"),
    (body(steps=[{"step": k, "verdict": "pass", "note": ""} for k in range(17)]), "report-body"),
    (body(("fail", "fail"), result="pass"), "report-result-disagrees"),
    (body(("pass", "pass"), result="fail"), "report-result-disagrees"),
    (body(format="other"), "report-format"),
])
def test_body_problem_names_each_bad_body(b, name):
    assert body_problem(b)[0] == name


def test_a_note_of_the_cap_is_kept():
    assert body_problem(body(steps=[{"step": 0, "verdict": "pass", "note": "x" * vr.NOTE_MAX}])) is None


def test_the_report_cap_mirrors_the_contracts_one_mebibyte():
    assert vr.MAX_REPORT_BYTES == 1 << 20 and vr.MAX_ENVELOPE_BYTES > vr.MAX_REPORT_BYTES


def test_a_refusal_outside_the_closed_tuple_cannot_be_raised():
    with pytest.raises(ValueError):
        ReportRefused("report-made-up")


def test_a_body_over_its_cap_inside_an_envelope_under_its_own_is_too_large():
    b = body(steps=[{"step": 0, "verdict": "pass", "note": "x" * vr.MAX_REPORT_BYTES}])
    raw = canon(envelope_map(vrun(), b))
    assert len(raw) <= vr.MAX_ENVELOPE_BYTES
    assert refused_name(raw) == "report-too-large"


# --- the minted run itself (L3 r1 F1, F5) --------------------------------------------------------------------------

def steps_digest(steps) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes([dict(s) for s in steps])).hexdigest()


def minted(steps=STEPS, *, digest=None, **over) -> VerifierRun:
    """A run whose entry is ENTRY with ``over`` applied and its steps_digest recomputed over ``steps`` (unless a
    ``digest`` is given), so a case isolates the one thing it breaks."""
    entry = {**ENTRY, "steps_digest": digest or steps_digest(steps), **over}
    entry = {k: v for k, v in entry.items() if v is not ...}
    return VerifierRun(job_id="dj-" + "0" * 32, run=0, session="v-0", ticket="t-0", verifier=entry, steps=steps,
                       driver_input_digest=ENTRY["driver_input_digest"], input_bundle_digest="sha256:" + "e" * 64)


@pytest.mark.parametrize("run", [
    minted(driver_template=...),
    minted(extra="x"),
    minted(verifier_id="Pytest_BB"),
    minted(verifier_id="x" * 49),
    minted(driver_template="pytest-bb"),
    minted(declared_kind="white-box"),
    minted(verifier_artifact_digest="sha256:" + "B" * 64),
    minted(driver_template_digest="sha256:" + "c" * 63),
    minted(digest="sha256:" + "9" * 64),
    minted(driver_input_digest="sha256:" + "8" * 64),
    minted(()),
    minted(tuple({"argv": ["true"], "wall_s": 1} for _ in range(17))),
    minted(({"argv": ["true"], "wall_s": 1, "env": {}},)),
    minted(({"argv": [], "wall_s": 1},)),
    minted(({"argv": ["x"] * 65, "wall_s": 1},)),
    minted(({"argv": ["a\x00b"], "wall_s": 1},)),
    minted(({"argv": ["x" * 4097], "wall_s": 1},)),
    minted(({"argv": [1], "wall_s": 1},)),
    minted(({"argv": ["true"], "wall_s": True},)),
    minted(({"argv": ["true"], "wall_s": 0},)),
], ids=lambda r: str(r.verifier.get("verifier_id")) + "/" + str(len(r.steps)))
def test_a_minted_run_that_is_not_the_contracts_entry_and_steps_is_refused(run):
    raw, digest = envelope(run)
    with pytest.raises(ReportRefused) as exc:
        check_envelope(raw, expected=run, event_digest=digest)
    assert exc.value.name == "verifier-entry"
    assert vr.run_problem(run) is not None


def test_a_minted_run_of_the_contracts_shape_has_no_problem():
    assert vr.run_problem(vrun()) is None and vr.run_problem(minted(({"argv": ["x" * 4096], "wall_s": 900},))) is None


@pytest.mark.parametrize("note", ["passed\nFAIL: forged", "a\x00b", "ok ‮deliaf", "a\tb"])
def test_a_note_that_cannot_show_as_itself_is_refused(note):
    assert (body_problem(body(steps=[{"step": 0, "verdict": "pass", "note": note}])) or (None,))[0] == "report-body"
