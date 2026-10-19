#!/usr/bin/env python3
"""Q19 S2: Levain's consent queue, driven by the AgentBoundary conformance suite (jamjet-labs/agentboundary).

Run with a Python that has BOTH ``levain`` and ``agentboundary`` (alpha, pre-release) installed:

    python scripts/agentboundary_suite.py SCENARIO_DIR [SCENARIO_DIR ...] [--results FILE] [--keep]

This file is OUTSIDE the wheel and levain never depends on agentboundary; it imports it only here.

``LevainImplementation.attempt`` does what the suite's ``ReferenceImplementation`` does, with Levain's real
``ConsentQueue`` standing in for the runtime: per scenario a fresh scratch consent dir and a real signed authority log
(real ``ssh-keygen`` ed25519 keys), the request is filed and then, by the FIRST ``setup.policies`` rule whose
``capabilities`` hold the action's capability (none: the reference's implicit deny):

    allow            -> a standing grant answers it (``answer_by_grant``)
    require-approval -> a matching ``setup.approvals`` entry: a signed allow by a test key enrolled for that approver;
                        no entry: the hold lapses (``sweep`` -> expired). In v0.2-alpha mode the approver list and the
                        window are checked exactly as ``ReferenceImplementation`` does (its ``_approval_problem`` is
                        imported and reused): a failing approval is never answered (an unenrolled key's answer is
                        really attempted and really refused for the approver case), the hold lapses.
    deny             -> a signed deny
    escalate         -> ``mark_undecided``

then exports the queue with ``levain.consent.agentboundary`` and takes that request's receipt. Emulation of the
scenario's world, NOT Levain's own values (each is a post-export override, listed in RESULTS.md):

    O1 receipt_id      <- setup.seed_receipt_id (when given)
    O2 clocks          <- setup.now into issued_at and execution.completed_at (when given)
    O3 approval time   <- the matched setup.approvals entry's approved_at, into approval.approved_at (an approval block
                          Levain wrote only; the reference records the approval as given, its time included)
    then receipt_hash is recomputed (Levain's own ``receipt_hash``) and ``action.inject`` is applied by the reference
    runtime's own ``_apply_inject`` (imported, private).

Nothing else is overridden in the STRICT run: the receipt keeps Levain's actor, agent, tool capability, target, policy
name/version and approver id. A second, informational run (BOUND) also rewrites those to the scenario's names, to show
how much of the strict result is naming rather than structure; it is not a conformance claim.

Grading is the suite's own ``agentboundary.cli._run_one`` (private; reused, unsupported by the vendor).
"""
from __future__ import annotations

import argparse
import copy
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import agentboundary
from agentboundary.cli import _run_one                      # private; the suite's own grading
from agentboundary.runtime import (Implementation, RuntimeOutcome, _apply_inject, _approval_problem, _match_policy,
                                   _recordable, V02_ALPHA, IMPLICIT_DENY_POLICY)
from agentboundary.scenarios import load_scenarios_dir

import levain.authority as auth
from levain.authority import ReplayCache
from levain.cockpit.consent import utc_stamp
from levain.cockpit.firejournal import CockpitDB
from levain.consent import ANSWER_NAMESPACE, ConsentQueue, answer_message, answer_payload, authority_source
from levain.consent import agentboundary as ab
from levain.consent.queue import UNDECIDED_GRACE
from levain.consent.receipt_log import head as receipts_head

__all__ = ["LevainImplementation"]


class _Key:
    def __init__(self, directory: Path, name: str) -> None:
        self.path = directory / name
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", str(self.path)],
                       check=True, capture_output=True, timeout=30)
        self.public_key = " ".join((directory / f"{name}.pub").read_text().split()[:2])
        out = subprocess.run(["ssh-keygen", "-l", "-E", "sha256", "-f", str(self.path)], check=True,
                             capture_output=True, text=True).stdout
        self.fp = out.split()[1]

    def sign(self, message: bytes, namespace: str = ANSWER_NAMESPACE) -> str:
        return subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(self.path), "-n", namespace],
                              input=message, capture_output=True, check=True, timeout=30).stdout.decode()


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class _Log:
    """A real signed authority log in a real cockpit journal; ``source`` is what a queue replays from."""

    def __init__(self, directory: Path, keys: dict[str, _Key]) -> None:
        self.path = directory / "cockpit.db"
        self.keys = keys
        with CockpitDB(directory).read():
            pass
        self.source = authority_source(self.path, anchor=None, cache=ReplayCache())

    def link(self, op: dict[str, Any]) -> None:
        import sqlite3
        signer = self.keys["laptop"]
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            auth.sign_and_append(conn, op, signer_fp=signer.fp, sign=lambda m: signer.sign(m, auth.NAMESPACE),
                                 clock=lambda: datetime.now(timezone.utc), cache=ReplayCache(), anchor=None)
        finally:
            conn.close()

    def enrol(self, key: _Key) -> None:
        self.link({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key, "class": "laptop-presence",
                   "custody": "passphrase"})


class NotExpressible(Exception):
    """Levain's semantics cannot express what the scenario's rule asks (reported as such, never forced)."""

    def __init__(self, why: str) -> None:
        super().__init__("NOT EXPRESSIBLE: " + why)


class LevainImplementation(Implementation):
    """Levain's real ConsentQueue, exported through ``levain.consent.agentboundary``, as an AgentBoundary
    Implementation. ``bind_names=True`` is the informational BOUND run (see the module docstring)."""

    def __init__(self, keydir: Path, workdir: Path, *, bind_names: bool = False, fallback_signed_allow: bool = False, levain_version: str = "0.0.0-suite",
                 keys: dict[str, _Key] | None = None) -> None:
        self.keydir, self.workdir, self.bind_names, self.levain_version = keydir, workdir, bind_names, levain_version
        self.fallback_signed_allow = fallback_signed_allow
        self._keys: dict[str, _Key] = keys if keys is not None else {
            "laptop": _Key(keydir, "laptop"), "stranger": _Key(keydir, "stranger")}
        self._n = 0
        self.trace: dict[str, str] = {}      # scenario action id -> how the queue ended (for RESULTS.md)

    def _approver_key(self, approver_id: str) -> _Key:
        name = "approver-" + "".join(c if c.isalnum() else "_" for c in approver_id)
        if name not in self._keys:
            self._keys[name] = _Key(self.keydir, name)
        return self._keys[name]

    @staticmethod
    def _agent(action: dict[str, Any]) -> str:
        framework = str(action.get("agent", {}).get("framework", "")).lower().replace("-", "_")
        return "codex" if framework == "codex" else "claude_code"

    def attempt(self, action: dict[str, Any], *, setup: dict[str, Any]) -> RuntimeOutcome:
        self._n += 1
        home = self.workdir / f"s{self._n:03d}"
        home.mkdir()
        arguments = action.get("arguments", {})
        capability = action.get("tool", {}).get("capability", "")
        emit_v02 = setup.get("receipt_version") == V02_ALPHA
        stamp = setup.get("now")

        policy = _match_policy(setup.get("policies", []), capability) or IMPLICIT_DENY_POLICY
        approval = next((a for a in setup.get("approvals", []) if a.get("capability") == capability), None)
        rule = policy["rule"]

        # --- the scratch world: a signed authority log, the approver's enrolled key, one standing grant if needed
        agent = self._agent(action)
        tool = action["tool"]["name"]
        ws = home / "ws"
        ws.mkdir()
        cwd = os.path.realpath(ws)
        approver_id = ((approval or {}).get("approver") or {}).get("id") if _recordable(approval) else None
        akey = self._approver_key(approver_id) if approver_id else None
        keys = dict(self._keys)
        (home / "auth").mkdir()
        log = _Log(home / "auth", keys)
        log.link({"op": "enrol", "fingerprint": keys["laptop"].fp, "public_key": keys["laptop"].public_key,
                  "class": "laptop-presence", "custody": "passphrase"})
        if akey is not None and akey.fp != keys["laptop"].fp:
            log.enrol(akey)
        clock = _Clock(datetime.now(timezone.utc))
        by_grant = rule == "allow" and not self.fallback_signed_allow
        if by_grant:
            try:
                log.link({"op": "create_grant", "grant": {
                    "id": "g1", "kind": "consent", "agent": agent, "tools": [tool], "workspace": cwd,
                    "postures": ["default"], "expires_at": utc_stamp(clock.now + timedelta(days=1))}})
            except Exception as exc:                         # Levain refuses to grant an unbounded tool
                raise NotExpressible(f"a standing grant cannot cover tool {tool!r}: {exc}") from None
        queue = ConsentQueue(home / "consent", authority=log.source, clock=clock)
        request = queue.file(agent=agent, agent_session_id="suite", tool_name=tool, tool_input=arguments, cwd=cwd,
                             hold=timedelta(minutes=5))

        def sign(key: _Key, decision: str):
            payload = answer_payload(queue_id=queue.queue_id, request=request, decision=decision, signer_fp=key.fp,
                                     signed_at=utc_stamp(clock.now), receipts_head=receipts_head(queue))
            return payload, key.sign(answer_message(payload))

        def lapse() -> None:
            clock.now += timedelta(minutes=5) + UNDECIDED_GRACE + timedelta(seconds=1)
            assert queue.sweep() == 1

        note = ""
        if by_grant:
            if queue.answer_by_grant(request.id, tool_input=arguments, posture="default") is None:
                raise NotExpressible("the standing grant does not cover this request's arguments")
            decision, note = "allow", "grant"
        elif rule == "allow":
            queue.answer(request.id, *sign(keys["laptop"], "allow"))
            decision, note = "allow", "FALLBACK signed allow (a grant cannot express this)"
        elif rule == "deny":
            queue.answer(request.id, *sign(keys["laptop"], "deny"))
            decision, note = "deny", "signed deny"
        elif rule == "escalate":
            assert queue.mark_undecided(request.id).state == "undecided"
            decision, note = "escalate", "undecided"
        elif rule == "require-approval":
            problem = (_approval_problem(policy, approval, stamp or _now())
                       if emit_v02 and approval is not None else None)
            if approval is None or not _recordable(approval):
                lapse()
                decision, note = "require-approval", "no approval: expired"
            elif problem is not None:
                if problem["reason_code"] == "approver-not-authorized":
                    from levain.consent import AnswerRefused
                    try:
                        queue.answer(request.id, *sign(keys["stranger"], "allow"))
                        raise AssertionError("an unenrolled key's answer was accepted")
                    except AnswerRefused:
                        pass
                lapse()
                decision, note = "deny", f"approval refused ({problem['reason_code']}): expired"
            else:
                queue.answer(request.id, *sign(akey, "allow"))
                decision, note = "allow", "signed allow"
        else:                                               # an unknown rule: the reference denies, so a signed deny
            queue.answer(request.id, *sign(keys["laptop"], "deny"))
            decision, note = "deny", "unknown rule: signed deny"
        self.trace[f"{self._n:03d}"] = note

        exported = ab.export(queue.directory, levain_version=self.levain_version)
        assert len(exported.receipts) == 1, f"expected one terminal receipt, got {len(exported.receipts)}"
        receipt = copy.deepcopy(exported.receipts[0])

        # --- scenario-world overrides (see the module docstring); strict run: O1 O2 O3 only
        if setup.get("seed_receipt_id"):
            receipt["receipt_id"] = setup["seed_receipt_id"]
        if stamp:
            receipt["issued_at"] = stamp
            receipt["execution"]["completed_at"] = stamp
        if "approval" in receipt and approval is not None and isinstance(approval.get("approved_at"), str):
            receipt["approval"]["approved_at"] = approval["approved_at"]
        if self.bind_names:
            receipt["actor"] = copy.deepcopy(action["actor"])
            receipt["agent"] = copy.deepcopy(action["agent"])
            receipt["tool"] = copy.deepcopy(action["tool"])
            receipt["target"] = copy.deepcopy(action["target"])
            if policy is not IMPLICIT_DENY_POLICY:
                receipt["policy"]["name"], receipt["policy"]["version"] = policy["name"], policy["version"]
            else:
                receipt["policy"]["name"], receipt["policy"]["version"] = "implicit.deny", "0"
            if "approval" in receipt and approval is not None and approver_id:
                receipt["approval"]["approver"]["id"] = approver_id
        if self.bind_names:
            _reprovenance(receipt)           # names the scenario supplied are not Levain-observed: synthesized
        receipt["receipt_hash"] = ab.receipt_hash(receipt)
        for op in action.get("inject", []):
            receipt = _apply_inject(receipt, op)
            if op.get("op") == "mutate_field" and op.get("path") == "prior_receipt":
                # O4: an injected chain link stands for a Levain that chained: its provenance entries (inferred, like
                # the exporter's own) and score follow, and the hash is recomputed when the scenario asks for it
                _reprovenance(receipt, default="inferred")
                if op.get("recompute_hash"):
                    receipt.pop("receipt_hash", None)
                    receipt["receipt_hash"] = ab.receipt_hash(receipt)
        return RuntimeOutcome(decision=decision, receipt=receipt, arguments=arguments)


def _reprovenance(receipt: dict[str, Any], default: str = "synthesized") -> None:
    """Bring ``provenance`` and ``completeness_score`` in line with the paths the receipt now holds (private
    exporter helpers; a shim override, not Levain's own record)."""
    prov = dict(receipt.get("provenance", {}))
    for path in ab._CONDITIONAL_PATHS:
        if ab._present(receipt, path):
            prov.setdefault(path, default)
        else:
            prov.pop(path, None)
    receipt["provenance"] = dict(sorted(prov.items()))
    receipt["completeness_score"] = ab._score_milli(receipt, prov) / 1000.0


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- diagnosis: a failure line -> which side it belongs to -------------------------------------------------------
_NAMING_FIELDS = ("policy.name", "policy.version", "approval.approver.id", "tool.capability", "agent.", "actor.",
                  "target.", "tool.name", "approval.context")
_NAMING_CODES = {"LEVEL_4_POLICY_DOWNGRADE", "LEVEL_4_UNAUTHORIZED_APPROVER"}


def _diagnose(failures: list[str]) -> str:
    bits: list[str] = []
    for f in failures:
        code = f.split(": ", 1)[-1]
        if f.startswith("unexpected failure code") and code in _NAMING_CODES:
            bits.append(f"{code}: naming (Levain names its own policy `levain.consent.<tier>` and its approver "
                        f"`ssh-key:<fp>`; the scenario's verifier context names others)")
        elif f.startswith("assertion failed") and any(f.split(": ", 1)[1].startswith(p) for p in _NAMING_FIELDS):
            bits.append("assertion on a name Levain does not choose (naming)")
        else:
            bits.append("see line")
    return "; ".join(dict.fromkeys(bits))


def _run(scenarios, impl) -> list[dict[str, Any]]:
    out = []
    for s in scenarios:
        try:
            out.append(_run_one(s, impl))
        except Exception as exc:        # the suite's own grading raised outside its per-scenario guard
            out.append({"scenario": s.name, "passed": False, "failures": [f"GradingError: {exc}"], "duration_ms": 0})
    return out


def _matrix(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        lines.append(f"  {'PASS' if r['passed'] else 'FAIL'}  {r['scenario']}")
        for f in r["failures"]:
            lines.append(f"        . {f}")
    p = sum(r["passed"] for r in results)
    lines.append(f"  {p} passed . {len(results) - p} failed . {len(results)} scenarios")
    return "\n".join(lines)


def _kind(r: dict[str, Any]) -> str:
    if r["passed"]:
        return "PASS"
    return "N/E" if any("NOT EXPRESSIBLE" in f for f in r["failures"]) else "FAIL"


def _write_results(path: Path, strict, fallback, bound, strict_trace, fb_trace) -> None:
    n = len(strict)
    cnt = lambda rs: sum(r["passed"] for r in rs)                       # noqa: E731
    ne = sum(_kind(r) == "N/E" for r in strict)
    out = [f"# Levain through the AgentBoundary conformance suite ({n} scenarios)", "",
           f"Generated by `scripts/agentboundary_suite.py` on {_now()}; agentboundary {agentboundary.__version__}.", "",
           f"- **STRICT: {cnt(strict)} / {n} pass** ({ne} of the failures are NOT EXPRESSIBLE: Levain's semantics "
           f"cannot do what the scenario's rule asks, reported as such, not forced). Levain keeps its own actor, "
           f"agent, capability, target, policy name/version and approver id; only receipt_id, clocks and the approval "
           f"time are the scenario's.",
           f"- **FALLBACK (informational): {cnt(fallback)} / {n}.** As STRICT, but an `allow` rule a standing grant "
           f"cannot express is answered by a signed allow instead (receipt: require-approval / success with an approval "
           f"block). It exercises the exporter on the scenarios STRICT could not reach; it is not Levain's allow.",
           f"- **BOUND (informational): {cnt(bound)} / {n}.** FALLBACK plus a rewrite of actor, agent, tool, target, "
           f"policy name/version and approver id to the scenario's names. Not a conformance claim: it shows how much "
           f"of the result is naming rather than structure.", "",
           "## What the shim is, and its honest limit", "",
           "AgentBoundary's policy language (capability rules, approver lists, windows) is EMULATED by the shim; Levain "
           "has tiers and grants, not capability rules. What the suite tests is that Levain's real queue, through the "
           "exporter, emits receipts the spec accepts. The vendor CLI only runs `reference`, so the shim is driven "
           "from Python and graded by the suite's own private `agentboundary.cli._run_one` (unsupported by the vendor).",
           "", "## Overrides applied (the shim emulating the scenario's world, not Levain's values)", "",
           "- O1 `receipt_id` <- `setup.seed_receipt_id` when the scenario pins it.",
           "- O2 `issued_at` and `execution.completed_at` <- `setup.now` when the scenario pins it.",
           "- O3 `approval.approved_at` <- the matched `setup.approvals` entry's `approved_at`, on a receipt Levain "
           "wrote an approval block for. The reference records the approval as given, its time included; Levain stamps "
           "the moment of its own signed answer.",
           "- O4 an injected `prior_receipt` (scenarios 29, 30, 32) stands for a Levain that chained: its two provenance "
           "entries (`inferred`, as the exporter's own) and the score follow it, and the hash is recomputed when the "
           "scenario asks. Without it the injected path would be in no provenance map and the score would mismatch "
           "(a shim artefact, since the reference emits no provenance). BOUND also marks every scenario-supplied "
           "optional path `synthesized` and recomputes the score.",
           "- After O1-O3 `receipt_hash` is recomputed (Levain's `receipt_hash`), then `action.inject` is applied by "
           "the reference runtime's own `_apply_inject` (imported, private).",
           "- Emulated as `ReferenceImplementation` does: first matching policy by capability, else implicit deny; in "
           "v0.2-alpha mode the approver list and window (`_approval_problem`, imported). A refused approval is never "
           "answered; the hold lapses (`sweep`) and Levain writes `expired`. The unauthorized-approver case really "
           "attempts a signed answer from an unenrolled key and the queue really refuses it.",
           "- The request's cwd is a scratch workspace, `tool_name` is `action.tool.name`, `tool_input` is "
           "`action.arguments`; the agent is claude_code (codex when `agent.framework` is codex).",
           "- STRICT keeps Levain's: `actor.id` (`agent:<agent>:session/suite`), `agent.*` (`claude-code` / "
           "`unknown`), `tool.capability` (`claude-code.<tool>`), `target.*`, `policy.name` (`levain.consent.<tier>`), "
           "`approver.id` (`ssh-key:<fp>`). Every Levain receipt is v0.2-alpha, including for the v0.1 scenarios.", "",
           "## Matrix", "", "| scenario | strict | fallback | bound | queue ended as (strict / fallback) |",
           "|---|---|---|---|---|"]
    for i, (a, f, b) in enumerate(zip(strict, fallback, bound)):
        k = f"{i + 1:03d}"
        out.append(f"| {a['scenario']} | {_kind(a)} | {_kind(f)} | {_kind(b)} | "
                   f"{strict_trace.get(k, 'n/a')} / {fb_trace.get(k, '')} |")
    out += ["", "## Failures, with diagnosis", "",
            "Strict failures are listed with their fallback / bound outcome. N/E scenarios share one cause and are "
            "listed once below.", ""]
    ne_names = [a["scenario"] for a in strict if _kind(a) == "N/E"]
    if ne_names:
        out += ["### NOT EXPRESSIBLE in STRICT", "",
                "Levain's standing grant (`answer_by_grant`) can only cover a tool whose request Levain bounds to a "
                "workspace (Edit, Write, Read, ... or an MCP read tool), and only for arguments that fit (`file_path` "
                "inside the workspace). An arbitrary-capability `allow` (github.merge, stripe.refund, ...) has no Levain "
                "meaning: nothing in Levain lets a tool like that run with no human. That is a real semantic mismatch, "
                "not a shim gap. Scenarios: " + ", ".join(ne_names) + ".", ""]
    for a, f, b in zip(strict, fallback, bound):
        if a["passed"] or _kind(a) == "N/E":
            continue
        out.append(f"### {a['scenario']}  (fallback {_kind(f)}, bound {_kind(b)})")
        for line in a["failures"]:
            out.append(f"- {line}")
        out.append(f"- diagnosis (auto): {_diagnose(a['failures'])}")
        note = DIAGNOSES.get(a["scenario"])
        if note:
            out.append(f"- diagnosis (reviewed): {note}")
        out.append("")
    out += ["## Failures in the informational runs that STRICT did not have (N/E in STRICT)", ""]
    for a, f, b in zip(strict, fallback, bound):
        if _kind(a) != "N/E" or f["passed"]:
            continue
        out.append(f"### {a['scenario']}  (fallback {_kind(f)}, bound {_kind(b)})")
        out += [f"- fallback: {x}" for x in f["failures"]]
        if not b["passed"]:
            out += [f"- bound: {x}" for x in b["failures"]]
        note = DIAGNOSES.get(a["scenario"])
        if note:
            out.append(f"- diagnosis (reviewed): {note}")
        out.append("")
    path.write_text("\n".join(out) + "\n")


# scenario name -> reviewed diagnosis, filled in after reading the run (kept here so RESULTS.md regenerates whole)
DIAGNOSES: dict[str, str] = {
    '02-mutation-require-approval': "NAMING. Levain's receipt names its own policy (`levain.consent.<tier>`) and approver (`ssh-key:<fp>`), so the verifier context the scenario supplies (policy store, approver list) does not know them. BOUND (names rewritten) passes, so the structure is accepted; the gap is the mapping of Levain's vocabulary onto a deployer's policy names, which the spec leaves to the operator.", '17-approval-inside-window': "NAMING. Levain's receipt names its own policy (`levain.consent.<tier>`) and approver (`ssh-key:<fp>`), so the verifier context the scenario supplies (policy store, approver list) does not know them. BOUND (names rewritten) passes, so the structure is accepted; the gap is the mapping of Levain's vocabulary onto a deployer's policy names, which the spec leaves to the operator.", '38-approval-without-context': "NAMING. Levain's receipt names its own policy (`levain.consent.<tier>`) and approver (`ssh-key:<fp>`), so the verifier context the scenario supplies (policy store, approver list) does not know them. BOUND (names rewritten) passes, so the structure is accepted; the gap is the mapping of Levain's vocabulary onto a deployer's policy names, which the spec leaves to the operator. (Levain always writes an approval.context, a pointer to its evidence; the scenario's context is optional.)", '53-signed-statement-refusal': "NAMING. Levain's receipt names its own policy (`levain.consent.<tier>`) and approver (`ssh-key:<fp>`), so the verifier context the scenario supplies (policy store, approver list) does not know them. BOUND (names rewritten) passes, so the structure is accepted; the gap is the mapping of Levain's vocabulary onto a deployer's policy names, which the spec leaves to the operator.", '04-merge-deny': "REAL SEMANTIC MISMATCH. The scenario's `deny` is a static policy rule. Levain has no rule that denies on its own: a deny is a human's signed answer (policy.decision `require-approval`, refusal `deny` / `operator-denied`, per R5). The receipt is valid; it just does not claim `policy.decision = deny`.", '41-refusal-policy-deny': 'REAL SEMANTIC MISMATCH, same as 04: a human deny is not a policy deny (policy.decision `require-approval`, reason `operator-denied`, not `policy-deny`). Plus naming (policy downgrade).',
    '16-deny-with-execution-success': "REAL SEMANTIC MISMATCH, same as 04: the scenario corrupts a `deny` receipt to show success and expects LEVEL_4_DENY_EXECUTED, which keys on `policy.decision == deny`. Levain's human-deny receipt says `require-approval`, so a verifier's deny check cannot fire.", '05-refund-escalate': "REAL SEMANTIC MISMATCH. Levain has no `escalate`. The nearest state is `undecided` (nobody answered; the agent's own dialog may take over), which the exporter maps to policy.decision `require-approval` with refusal `require-approval` / `undecided` (R5).", '45-refusal-escalation-unresolved': 'REAL SEMANTIC MISMATCH, same as 05: the refusal verdict (`require-approval`) matches, policy.decision is `require-approval` not `escalate`. Plus naming (policy downgrade).',
    '42-refusal-approval-missing': 'EXPORTER MAPPING / queue-state choice. Per the brief a missing approval makes the hold lapse, which Levain records as `expired` (refusal `expired` / `approval-window-elapsed`); the scenario wants verdict `require-approval` / `approval-missing`. A request the hook marks `undecided` would carry verdict `require-approval` (reason `undecided`, still not `approval-missing`); not run here. Plus naming (policy downgrade).',
    '43-refusal-approval-expired': 'REAL SEMANTIC MISMATCH. verdict `expired` and reason `approval-window-elapsed` match. Levain never records the stale approval: an approval too old is never answered, so there is no approval block to carry as evidence. Plus naming (policy downgrade).',
    '44-refusal-unauthorized-approver': "REAL SEMANTIC MISMATCH. The shim really attempted the unenrolled key's signed answer and Levain really refused it (AnswerRefused); a refused answer writes no receipt, so the request lapses as `expired` with no approval evidence. The scenario wants `deny` / `approver-not-authorized` with the approval recorded. A candidate Levain feature (a receipt for a refused answer), a scope call for Phill. Plus naming (policy downgrade).", '47-refusal-contradicts-policy': 'NOT EXPRESSIBLE in STRICT (needs `policy.decision = allow`). Even in FALLBACK the fault cannot be planted: with policy.decision `require-approval`, every refusal verdict is consistent (`_REFUSAL_CONSISTENT_DECISIONS`), so LEVEL_2_REFUSAL_DECISION_MISMATCH cannot fire. Root cause is the same as the N/E set: no no-human allow.',
    '01-merge-allow': "NOT EXPRESSIBLE: an `allow` with no human has no Levain meaning for a tool a grant cannot cover; FALLBACK/BOUND's signed allow says `require-approval`, so the scenario's `policy.decision = allow` assertion cannot hold.", '28-honest-completeness-passes': "FALLBACK ARTIFACT. The scenario injects its own provenance map (17 required paths, score 1.0); FALLBACK's signed allow adds an approval block whose paths the injected map does not list, so the recomputed score differs. A grant-allow receipt (no approval block) would not have this; N/E in STRICT.", '49-envelope-payload-tampered': "SUITE ASSUMPTION. The runner's `replace_in_payload` finds `pull/14` (the scenario's `target.resource_id`) in the emitted receipt and raises if it is absent. Levain's `resource_id` is the request's cwd, so STRICT/FALLBACK crash in the suite's grading (`GradingError`). BOUND restores the target and passes. The suite assumes an implementation echoes `action.target`.", '19-actor-human-validates': "FALLBACK: naming (policy downgrade) and `actor.type` `agent` where the scenario's action is a human actor. Levain's hook actors are always agents; N/E in STRICT.", '36-dev-environment-validates': "FALLBACK: `actor.type` agent vs the scenario's human actor (naming). N/E in STRICT.", '35-staging-environment-validates': "FALLBACK: `target.environment` is Levain's fixed `dev` (synthesized), the scenario's is `staging` (naming). N/E in STRICT."}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("dirs", nargs="+", type=Path)
    ap.add_argument("--results", type=Path, default=None)
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ns = ap.parse_args(argv)
    scenarios = []
    for d in ns.dirs:
        scenarios += load_scenarios_dir(d)
    scenarios.sort(key=lambda s: s.name)
    tmp = Path(tempfile.mkdtemp(prefix="ab-suite-"))
    try:
        keydir = tmp / "keys"
        keydir.mkdir()
        strict_impl = LevainImplementation(keydir, _mk(tmp / "strict"))
        strict = _run(scenarios, strict_impl)
        fb_impl = LevainImplementation(keydir, _mk(tmp / "fallback"), fallback_signed_allow=True,
                                       keys=strict_impl._keys)
        fallback = _run(scenarios, fb_impl)
        bound_impl = LevainImplementation(keydir, _mk(tmp / "bound"), bind_names=True, fallback_signed_allow=True,
                                          keys=strict_impl._keys)
        bound = _run(scenarios, bound_impl)
        print("STRICT\n" + _matrix(strict) + "\n\nFALLBACK (informational)\n" + _matrix(fallback)
              + "\n\nBOUND (informational)\n" + _matrix(bound))
        if ns.results:
            _write_results(ns.results, strict, fallback, bound, strict_impl.trace, fb_impl.trace)
    finally:
        if not ns.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(r["passed"] for r in strict) else 1


def _mk(p: Path) -> Path:
    p.mkdir(parents=True)
    return p


if __name__ == "__main__":
    sys.exit(main())
