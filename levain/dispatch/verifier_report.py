"""levain.dispatch.verifier_report — the verifier report's BODY schema and the check of its root-authored ENVELOPE.

A verifier session's driver writes the BODY, ``levain-dispatch-verifier-report/1`` (canonical JSON,
:mod:`levain.contracts.canon`), exactly ``{"format", "result", "steps": [{"step", "verdict", "note"}]}``: one entry per
ticket step, ``step`` 0..n-1 in order, ``verdict`` and ``result`` ``pass``/``fail``, ``note`` the driver's one-line
reason (at most :data:`NOTE_MAX` characters, shown to the human through :mod:`levain.unshowable` like every
agent-sourced string). ``result`` is ``pass`` exactly when every step is: a body whose result disagrees with its steps
is REFUSED, read neither as a pass (a driver bug must not land) nor as the verifier's fail. The body names no kind, no
verifier and no model: the declared kind is the supervisor's, from the activated verifier entry.

S1's root carries that body opaque inside the ENVELOPE file ``L/export/dispatch/<session>/verifier-report.json``
(``levain-verifier-envelope/1``: ``{session, ticket, verifier, driver_input_digest, input_bundle_digest, steps, body}``,
``steps`` the ``.exit`` records root wrote, ``verifier`` the resolved entry dispatch named at mint). The session's
terminal ``state`` event carries the digest of the ENVELOPE file's bytes. :func:`check_envelope` takes those bytes, the
event's digest and the :class:`VerifierRun` the supervisor minted, and returns the :class:`VerifierReport` or raises
:class:`ReportRefused` with one name of :data:`REFUSALS`. The envelope has no ``format`` member;
:data:`ENVELOPE_FORMAT_NAME` is documentation only.

Known edge: no S1 code writes the envelope yet, so this is checked against fixture bytes that follow the contract's
envelope field for field; the ``.exit`` member types are this module's reading of the contract, which names the
members but not their types. Stdlib only.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from levain.contracts import ContractError
from levain.contracts.canon import canonical_bytes, parse
from levain.dispatch import DispatchError

__all__ = [
    "ENVELOPE_FORMAT_NAME", "EXIT_KEYS", "MAX_ENVELOPE_BYTES", "MAX_REPORT_BYTES", "MAX_STEPS", "NOTE_MAX",
    "REFUSALS", "REPORT_FORMAT", "VERIFIER_KEYS", "ReportRefused", "StepVerdict", "VerifierReport", "VerifierRun",
    "body_problem", "check_envelope",
]

REPORT_FORMAT = "levain-dispatch-verifier-report/1"
ENVELOPE_FORMAT_NAME = "levain-verifier-envelope/1"
MAX_STEPS = 16
# mirrors S1's report cap (the contract's V21, on the BODY); S1 exposes no constant yet, and the test that pins the
# two equal is written once it does
MAX_REPORT_BYTES = 1 << 20
# the envelope is the body plus the resolved entry and one small .exit record per step: the body's cap and room for
# the rest, so an envelope is refused on its size before it is parsed
MAX_ENVELOPE_BYTES = MAX_REPORT_BYTES + (64 << 10)
NOTE_MAX = 512
VERIFIER_KEYS = frozenset({"verifier_id", "verifier_artifact_digest", "declared_kind", "driver_template",
                           "driver_template_digest", "steps_digest", "driver_input_digest"})
EXIT_KEYS = frozenset({"exit", "signal", "wall_s", "timed_out", "truncated_bytes", "lingering_killed"})
_ENVELOPE_KEYS = frozenset({"session", "ticket", "verifier", "driver_input_digest", "input_bundle_digest", "steps",
                            "body"})
_BODY_KEYS = frozenset({"format", "result", "steps"})
_STEP_KEYS = frozenset({"step", "verdict", "note"})
_VERDICTS = ("pass", "fail")

# every name check_envelope refuses with, closed: no envelope bytes to read; the event's digest is not the file's; the
# file is over the cap; it does not parse or is not canonical; its members are not the envelope's; session, ticket,
# verifier entry, driver input or input bundle is not the one minted; its .exit records are not one per ticket step of
# the right shape; the body's format, members, step list or count is wrong; its result disagrees with its steps
REFUSALS = (
    "report-missing", "report-digest-mismatch", "report-too-large", "report-not-canonical", "envelope-members",
    "envelope-session", "envelope-ticket", "envelope-verifier", "envelope-driver-input", "envelope-input-bundle",
    "envelope-steps",
    "report-format", "report-body", "report-step-count", "report-result-disagrees",
)


class ReportRefused(DispatchError):
    """A verifier envelope or body that is not read either way; ``name`` is one of :data:`REFUSALS`."""

    def __init__(self, name: str, detail: str = "") -> None:
        if name not in REFUSALS:
            raise ValueError(f"{name!r} is not a verifier report refusal")
        self.name = name
        super().__init__(f"{name}: {detail}" if detail else name)


@dataclass(frozen=True)
class VerifierRun:
    """What the supervisor minted for ONE verifier session of ONE run of a job: ``verifier`` is the resolved entry
    (:data:`VERIFIER_KEYS`), ``steps`` the ticket's ``verifier_steps``. Not persisted here; the writer's argument."""
    job_id: str
    run: int
    session: str
    ticket: str
    verifier: Mapping[str, Any]
    steps: Sequence[Mapping[str, Any]]
    driver_input_digest: str
    input_bundle_digest: str


@dataclass(frozen=True)
class StepVerdict:
    step: int
    verdict: str
    note: str


@dataclass(frozen=True)
class VerifierReport:
    """A checked envelope: its digest, the body's ``result`` and step verdicts, and root's ``.exit`` records."""
    digest: str
    result: str
    steps: tuple[StepVerdict, ...]
    exits: tuple[Mapping[str, Any], ...]

    @property
    def first_failure(self) -> StepVerdict | None:
        return next((s for s in self.steps if s.verdict == "fail"), None)


def _int(value: Any, *, nullable: bool = False) -> bool:
    # bool is an int to Python; a JSON true is never a count
    return (nullable and value is None) or (type(value) is int and value >= 0)


def _exit_ok(rec: Any) -> bool:
    return (isinstance(rec, dict) and set(rec) == EXIT_KEYS and _int(rec["exit"], nullable=True)
            and _int(rec["signal"], nullable=True) and _int(rec["wall_s"]) and type(rec["timed_out"]) is bool
            and _int(rec["truncated_bytes"]) and _int(rec["lingering_killed"]))


def body_problem(body: Any) -> tuple[str, str] | None:
    """``(refusal name, why)`` when ``body`` is not a ``levain-dispatch-verifier-report/1`` body, else ``None``. The
    step count against the ticket is the envelope's check, not this one."""
    if not isinstance(body, dict) or set(body) != _BODY_KEYS:
        return "report-body", "the body's members are not format, result, steps"
    if body["format"] != REPORT_FORMAT:
        return "report-format", f"the body's format is not {REPORT_FORMAT}"
    if body["result"] not in _VERDICTS:
        return "report-body", "the body's result is not pass or fail"
    steps = body["steps"]
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        return "report-body", f"the body's steps are a list of 1 to {MAX_STEPS}"
    for k, s in enumerate(steps):
        if not isinstance(s, dict) or set(s) != _STEP_KEYS:
            return "report-body", f"step {k}'s members are not step, verdict, note"
        if type(s["step"]) is not int or s["step"] != k:
            return "report-body", f"step {k} is numbered out of order"
        if s["verdict"] not in _VERDICTS:
            return "report-body", f"step {k}'s verdict is not pass or fail"
        if not isinstance(s["note"], str) or len(s["note"]) > NOTE_MAX:
            return "report-body", f"step {k}'s note is not text of at most {NOTE_MAX} characters"
    if (body["result"] == "pass") != all(s["verdict"] == "pass" for s in steps):
        return "report-result-disagrees", f"the body says {body['result']} and its steps say otherwise"
    return None


def check_envelope(raw: bytes | None, *, expected: VerifierRun, event_digest: str) -> VerifierReport:
    """The checked envelope ``raw`` for the session ``expected`` names, whose terminal event carried
    ``event_digest``; raises :class:`ReportRefused` at the first check that fails (module docstring)."""
    if not isinstance(raw, bytes):
        raise ReportRefused("report-missing", "no envelope bytes were read")
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if digest != event_digest:
        raise ReportRefused("report-digest-mismatch", "the event's digest is not the envelope file's")
    if len(raw) > MAX_ENVELOPE_BYTES:
        raise ReportRefused("report-too-large", f"the envelope is over {MAX_ENVELOPE_BYTES} bytes")
    try:
        env = parse(raw)
        again = canonical_bytes(env)
    except (ContractError, TypeError, ValueError):
        raise ReportRefused("report-not-canonical", "the envelope does not parse") from None
    if again != raw:
        raise ReportRefused("report-not-canonical", "the envelope is not canonical JSON")
    if not isinstance(env, dict) or set(env) != _ENVELOPE_KEYS:
        raise ReportRefused("envelope-members", "the envelope's members are not the contract's")
    if env["session"] != expected.session:
        raise ReportRefused("envelope-session", "the envelope is another session's")
    if env["ticket"] != expected.ticket:
        raise ReportRefused("envelope-ticket", "the envelope is another ticket's")
    # byte-equal to the entry resolved at mint, never "the same id"
    if not isinstance(env["verifier"], dict) or canonical_bytes(env["verifier"]) != canonical_bytes(
            dict(expected.verifier)):
        raise ReportRefused("envelope-verifier", "the envelope's verifier entry is not the one minted")
    if env["driver_input_digest"] != expected.driver_input_digest:
        raise ReportRefused("envelope-driver-input", "the envelope's driver input is not the one minted")
    if env["input_bundle_digest"] != expected.input_bundle_digest:
        raise ReportRefused("envelope-input-bundle", "the envelope's input bundle is not the one minted")
    exits = env["steps"]
    if not isinstance(exits, list) or len(exits) != len(expected.steps):
        raise ReportRefused("envelope-steps", "the envelope holds another number of steps than the ticket")
    for k, rec in enumerate(exits):
        if not _exit_ok(rec):
            raise ReportRefused("envelope-steps", f"step {k}'s exit record is not the contract's")
    body = env["body"]
    if len(canonical_bytes(body)) > MAX_REPORT_BYTES:
        raise ReportRefused("report-too-large", f"the body is over {MAX_REPORT_BYTES} bytes")
    problem = body_problem(body)
    if problem is not None:
        raise ReportRefused(*problem)
    if len(body["steps"]) != len(exits):
        raise ReportRefused("report-step-count", "the body judges another number of steps than ran")
    return VerifierReport(digest=digest, result=body["result"],
                          steps=tuple(StepVerdict(s["step"], s["verdict"], s["note"]) for s in body["steps"]),
                          exits=tuple(exits))
