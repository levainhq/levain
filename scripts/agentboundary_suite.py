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
                        imported and reused): a failing approval never moves the request and the hold lapses. For
                        the approver case an unenrolled key's signed answer is really attempted (before the lapse)
                        and really refused; for the window case the enrolled approver's signed answer is really
                        attempted AFTER the lapse and really refused as late. The queue keeps each as a refused
                        answer (``refused_answers``), which the exporter carries as the refusal's evidence.
    deny             -> a signed deny
    escalate         -> ``mark_undecided``

then, when the action executed (a decision of allow: an allow rule or an approved require-approval), files the
authority ledger's ``post`` row for that exact call as the PostToolUse hook does (R11: execution comes from the ledger
row, never from permission), and exports the queue with ``levain.consent.agentboundary`` and takes that request's receipt. Emulation of the
scenario's world, NOT Levain's own values (each is a post-export override, listed in RESULTS.md):

    O1 receipt_id      <- setup.seed_receipt_id (when given)
    O2 clocks          <- setup.now into issued_at and execution.completed_at (when given)
    O5 scratch clock   <- the scratch world's clock (authority log, queue, ledger) is pinned 3 days in the past; the
                          queue is exported at the REAL clock (a future export clock is refused), so a lapsed hold is
                          older than the exporter's 24 h SETTLE and the export does not stop at it
    O3 approval time   <- the matched setup.approvals entry's approved_at, into approval.approved_at (an approval block
                          Levain wrote only; the reference records the approval as given, its time included)
    then receipt_hash is recomputed (Levain's own ``receipt_hash``) and ``action.inject`` is applied by the reference
    runtime's own ``_apply_inject`` (imported, private).

Nothing else is overridden in the STRICT run: the receipt keeps Levain's actor, agent, tool capability, target, policy
name/version and approver id. NAMES is STRICT with an operator's names file handed to the exporter
(``ab.export(names=...)``): the product path, the names set inside the export so the hash covers them, and no
post-export rename. A third, informational run (BOUND) rewrites those names after the export instead (actor.type
included, which a names file may not set), to show how much of the strict result is naming rather than structure; it
is not a conformance claim.

Grading is the suite's own ``agentboundary.cli._run_one`` (private; reused, unsupported by the vendor).
"""
from __future__ import annotations

import argparse
import copy
import json
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
from levain.consent.ledger import Ledger
from levain.consent.queue import UNDECIDED_GRACE
from levain.consent.receipt_log import head as receipts_head

__all__ = ["LevainImplementation"]

SCRATCH_PAST = timedelta(days=3)


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

    def __init__(self, directory: Path, keys: dict[str, _Key], clock) -> None:
        self.path = directory / "cockpit.db"
        self.keys = keys
        self.clock = clock
        with CockpitDB(directory).read():
            pass
        self.source = authority_source(self.path, anchor=None, cache=ReplayCache())

    def link(self, op: dict[str, Any]) -> None:
        import sqlite3
        signer = self.keys["laptop"]
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            auth.sign_and_append(conn, op, signer_fp=signer.fp, sign=lambda m: signer.sign(m, auth.NAMESPACE),
                                 clock=self.clock, cache=ReplayCache(), anchor=None)
        finally:
            conn.close()

    def enrol(self, key: _Key) -> None:
        self.link({"op": "enrol", "fingerprint": key.fp, "public_key": key.public_key, "class": "laptop-presence",
                   "custody": "passphrase"})


class NotExpressible(Exception):
    """Levain's semantics cannot express what the scenario's rule asks (reported as such, never forced)."""

    def __init__(self, why: str) -> None:
        super().__init__("NOT EXPRESSIBLE: " + why)


def _mcp_tool_name(capability: str) -> str | None:
    """An AgentBoundary capability as the MCP tool name an agent would call it by: ``github.merge`` ->
    ``mcp__github__merge`` (``mcp__<server>__<tool>``; the first dotted segment is the server, the rest the tool with
    dots as underscores). ``None`` for a capability with no dot (the scenario's own tool name is used)."""
    server, dot, rest = capability.partition(".")
    return f"mcp__{server}__{rest.replace('.', '_')}" if dot and server and rest else None


def _field_kind(name: str, value: Any) -> str:
    """The consent-mcp field kind (``levain.authority.CONSENT_MCP_FIELD_KINDS``) an argument value fits, by its JSON
    type (``str``, ``int``, ``bool``, ``strs``); ``ValueError`` for a value none fits (float, null, object, a list of
    non-strings), which the caller reports as not expressible."""
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "strs"
    raise ValueError(f"argument {name!r} is a {type(value).__name__} ({str(value)[:60]!r})")


class LevainImplementation(Implementation):
    """Levain's real ConsentQueue, exported through ``levain.consent.agentboundary``, as an AgentBoundary
    Implementation. ``bind_names=True`` is the informational BOUND run (see the module docstring)."""

    def __init__(self, keydir: Path, workdir: Path, *, bind_names: bool = False, fallback_signed_allow: bool = False, levain_version: str = "0.0.0-suite",
                 keys: dict[str, _Key] | None = None, names_file: bool = False) -> None:
        self.keydir, self.workdir, self.bind_names, self.levain_version = keydir, workdir, bind_names, levain_version
        self.names_file = names_file
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
        tool = _mcp_tool_name(capability) or action["tool"]["name"]
        ws = home / "ws"
        ws.mkdir()
        cwd = os.path.realpath(ws)
        approver_id = ((approval or {}).get("approver") or {}).get("id") if _recordable(approval) else None
        akey = self._approver_key(approver_id) if approver_id else None
        keys = dict(self._keys)
        (home / "auth").mkdir()
        # O5: the scratch world's clock is pinned SCRATCH_PAST in the past (the authority log, the queue and the
        # ledger all read it), so a lapsed hold (expired / withdrawn) is older than the exporter's 24 h SETTLE when
        # the queue is exported at the real clock; a future export clock is refused (R11), so the world goes back
        clock = _Clock(datetime.now(timezone.utc) - SCRATCH_PAST)
        log = _Log(home / "auth", keys, clock)
        log.link({"op": "enrol", "fingerprint": keys["laptop"].fp, "public_key": keys["laptop"].public_key,
                  "class": "laptop-presence", "custody": "passphrase"})
        if akey is not None and akey.fp != keys["laptop"].fp:
            log.enrol(akey)
        by_grant = rule == "allow" and not self.fallback_signed_allow
        if by_grant:
            try:
                fields = {k: _field_kind(k, v) for k, v in arguments.items()}
            except ValueError as exc:                        # an argument no consent-mcp field kind fits: never forced
                raise NotExpressible(f"no consent-mcp field kind fits the arguments of {tool!r}: {exc}") from None
            try:
                log.link({"op": "create_grant", "grant": {
                    "id": "g1", "kind": "consent-mcp", "agent": agent, "tools": [tool], "workspace": cwd,
                    "postures": ["default"], "expires_at": utc_stamp(clock.now + timedelta(days=1)),
                    "mcp_fields": {tool: fields}}})
            except Exception as exc:                         # the log refused the grant: reported with its words
                raise NotExpressible(f"a consent-mcp grant cannot cover tool {tool!r} with fields {fields}: {exc}") from None
        queue = ConsentQueue(home / "consent", authority=log.source, home=home, clock=clock)
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
                from levain.consent import AnswerRefused
                if problem["reason_code"] == "approver-not-authorized":
                    try:
                        queue.answer(request.id, *sign(keys["stranger"], "allow"))
                        raise AssertionError("an unenrolled key's answer was accepted")
                    except AnswerRefused:
                        pass
                    lapse()
                elif problem["reason_code"] == "approval-window-elapsed":
                    # a stale approval: the authorised approver's answer reaches the queue after the hold lapsed,
                    # and the queue refuses it as late (and keeps it as evidence)
                    lapse()
                    try:
                        queue.answer(request.id, *sign(akey, "allow"))
                        raise AssertionError("an answer after the hold lapsed was accepted")
                    except AnswerRefused:
                        pass
                else:
                    lapse()
                kept = len(queue.refused_answers(request.id))
                decision, note = "deny", f"approval refused ({problem['reason_code']}; kept: {kept}): expired"
            else:
                queue.answer(request.id, *sign(akey, "allow"))
                decision, note = "allow", "signed allow"
        else:                                               # an unknown rule: the reference denies, so a signed deny
            queue.answer(request.id, *sign(keys["laptop"], "deny"))
            decision, note = "deny", "unknown rule: signed deny"
        self.trace[f"{self._n:03d}"] = note

        if decision == "allow":
            # the action EXECUTED (an allow, or an approved require-approval): file the ledger's post row for this exact
            # call as the PostToolUse hook does (ledger.hook_record, phase post); R11 takes execution from it
            Ledger(queue.directory).record_call(agent=agent, agent_session_id="suite", tool_name=tool,
                                                tool_input=arguments, cwd=cwd, phase="post", received_at=clock.now)
            note += " + post row"
            self.trace[f"{self._n:03d}"] = note
        names = None
        if self.names_file:
            # NAMES: the operator's names file, built from the scenario's names and keyed by LEVAIN's own values (read
            # off Levain's own export, as an operator would), handed to the exporter; no name is rewritten after the
            # export. Each authority name is pinned one to one; the approver map names the one key that signed
            stranger = note.startswith("approval refused (approver-not-authorized")
            levains = ab.export(queue.directory).receipts
            assert len(levains) == 1, f"expected one terminal receipt, got {len(levains)}"
            names = ab.load_names(json.dumps(_names_file(
                action, policy, tool, levains[0], approver_id,
                keys["stranger"] if stranger else akey), ensure_ascii=False).encode("utf-8"))
        exported = ab.export(queue.directory, names=names)
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


def _names_file(action: dict[str, Any], policy: dict[str, Any], tool: str, levain: dict[str, Any],
                approver_id: str | None, approver_key: _Key | None) -> dict[str, Any]:
    """The names file an operator running the scenario's world would write: each rule's ``when`` is a value LEVAIN
    wrote (read off ``levain``, Levain's own receipt of the request: its policy name and version, actor id,
    capability and working directory, and the signer ``ssh-key:<fp>``), each ``set`` the scenario's name for it.
    Aliases only: ``actor.type`` and the run's own facts (the agent's framework, model and versions, the tool's name
    and version, the target system and environment) are never set, so a scenario that needs one of them to differ
    from what Levain recorded still fails on it. Every authority name (policy name and version, capability, resource
    id, approver id) is set only by a rule whose ``when`` pins exactly that field to Levain's value (the version: the
    name and the version), so each maps one to one; the approver map names the one key that signed (the enrolled
    approver's, or for an unauthorised approver the stranger's whose refused answer the queue kept), never a second
    key."""
    def strs(block: dict[str, Any], fields: tuple[str, ...], prefix: str) -> dict[str, str]:
        return {f"{prefix}.{f}": block[f] for f in fields if isinstance(block.get(f), str) and block[f]}

    pname, pver = ((policy["name"], str(policy["version"])) if policy is not IMPLICIT_DENY_POLICY
                   else ("implicit.deny", "0"))
    scen_tool, scen_target = action.get("tool", {}), action.get("target", {})
    lname, lver = levain["policy"]["name"], levain["policy"]["version"]
    rules: list[dict[str, Any]] = [
        {"when": {"policy.name": lname}, "set": {"policy.name": pname}},
        {"when": {"policy.name": lname, "policy.version": lver}, "set": {"policy.version": pver}},
        {"when": {"actor.id": levain["actor"]["id"]},
         "set": strs(action.get("actor", {}), ("id", "display_name"), "actor")},
        {"when": {"tool.capability": levain["tool"]["capability"]}, "set": strs(scen_tool, ("capability",), "tool")},
        {"when": {"target.resource_id": levain["target"]["resource_id"]},
         "set": strs(scen_target, ("resource_id",), "target")}]
    if approver_id and approver_key is not None:
        rules.append({"when": {"approval.approver.id": f"ssh-key:{approver_key.fp}"},
                      "set": {"approval.approver.id": approver_id}})
    return {"format": ab.NAMES_FORMAT, "rules": [r for r in rules if r["set"]]}


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


def _write_results(path: Path, strict, fallback, bound, strict_trace, fb_trace, names) -> None:
    n = len(strict)
    cnt = lambda rs: sum(r["passed"] for r in rs)                       # noqa: E731
    ne = sum(_kind(r) == "N/E" for r in strict)
    names_fail = [r for r in names if not r["passed"]]
    out = [f"# Levain through the AgentBoundary conformance suite ({n} scenarios)", "",
           f"Generated by `scripts/agentboundary_suite.py` on {_now()}; agentboundary {agentboundary.__version__}.", "",
           f"- **STRICT: {cnt(strict)} / {n} pass** (26 / 54 before S-R2, the refused-answer export; 27 / 54 before "
           f"R11) ({ne} of the failures are NOT EXPRESSIBLE: a consent-mcp grant "
           f"cannot declare what the scenario's arguments are, reported with Levain's own refusal, not forced). An `allow` is "
           f"answered by a consent-mcp standing grant on the MCP tool the capability names. Levain keeps its own actor, "
           f"agent, capability, target, policy name/version and approver id; only receipt_id, clocks and the approval "
           f"time are the scenario's.",
           f"- **FALLBACK (informational): {cnt(fallback)} / {n}** (28 / 54 before S-R2). As STRICT, but an `allow` rule a standing grant "
           f"cannot express is answered by a signed allow instead (receipt: require-approval / success with an approval "
           f"block). It exercises the exporter on the scenarios STRICT could not reach; it is not Levain's allow.",
           f"- **BOUND (informational): {cnt(bound)} / {n}** (42 / 54 before S-R2). FALLBACK plus a rewrite of actor, agent, tool, target, "
           f"policy name/version and approver id to the scenario's names. Not a conformance claim: it shows how much "
           f"of the result is naming rather than structure.",
           f"- **NAMES (the product path): {cnt(names)} / {n}** (42 / 54 before names fix r2, which made the run's own "
           f"facts unsettable: the agent's framework, model and versions, the tool's name and version, the target "
           f"system and environment; 35-staging-environment-validates moved to FAIL because Levain recorded `dev` and "
           f"a names file no longer rewrites where an action ran. 42 / 54 before names fix r1 too). STRICT (an "
           f"`allow` is a standing grant) with an operator's names file handed to the exporter (`export(names=...)`, "
           f"the CLI's `--names`): each rule keyed by a value Levain wrote (read off Levain's own receipt: its policy "
           f"name and version, actor id, capability, cwd, the signer's `ssh-key:<fp>`) sets the scenario's alias for "
           f"it, and the exporter renames before provenance, score and hash; with a names file every `receipt_id` is "
           f"derived with the digest of the file's validated rules, so it never reuses an unrenamed receipt's id. Every authority name "
           f"(policy name and version, capability, resource id, approver id) is set only by a rule whose `when` pins "
           f"exactly that field to Levain's value (the version: name and version), and the approver map names only "
           f"the key that signed. NO name is rewritten after the export (only O1-O3 below, as in STRICT). A names "
           f"file may set aliases only, never facts, so `actor.type` stays `agent` and `target.environment` `dev`.", "",
           "## NAMES: what still fails, by reason", "",
           "Every NAMES failure, with the reviewed reason. None is naming: the names file closed every naming "
           "failure STRICT had.", ""]
    for r in names_fail:
        out.append(f"- **{r['scenario']}**: {NAMES_REASONS.get(r['scenario'], 'not reviewed: ' + '; '.join(r['failures']))}")
    out += ["",
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
           "- O5 the scratch world's clock (authority log, queue, ledger) is pinned 3 days in the past and the queue is "
           "exported at the real clock (a future export clock is refused, R11). Without it a lapsed hold (expired, "
           "withdrawn) is younger than the exporter's 24 h SETTLE and the export stops there. The ledger `post` row "
           "(below) is stamped by that clock.",
           "- R11 evidence: when the action executed (allow, or an approved require-approval) the shim files the "
           "ledger's `post` row for that exact call (same agent, session, tool, input digest) as the PostToolUse hook "
           "does (`Ledger.record_call(phase=\"post\")`); the exporter takes `execution.status = success` from it. "
           "Failure scenarios are not driven (no scenario has an executed action that failed).",
           "- After O1-O3 `receipt_hash` is recomputed (Levain's `receipt_hash`), then `action.inject` is applied by "
           "the reference runtime's own `_apply_inject` (imported, private).",
           "- Emulated as `ReferenceImplementation` does: first matching policy by capability, else implicit deny; in "
           "v0.2-alpha mode the approver list and window (`_approval_problem`, imported). A refused approval never "
           "moves the request; the hold lapses (`sweep`) and Levain writes `expired`. The unauthorized-approver case "
           "really attempts a signed answer from an unenrolled key before the lapse, the window case really attempts "
           "the enrolled approver's signed answer after it; the queue really refuses each and keeps it in "
           "`refused_answers`, which the exporter carries as the refusal's approval evidence (S-R2). O3 applies to "
           "that approval block as to any other.",
           "- The request's cwd is a scratch workspace, `tool_name` is `action.tool.name`, `tool_input` is "
           "`action.arguments`; the agent is claude_code (codex when `agent.framework` is codex). The tool is named as an agent "
           "would call an MCP tool: capability `github.merge` -> `mcp__github__merge` (first dotted segment the server, the "
           "rest with dots as underscores), the scenario's own `tool.name` only for a capability with no dot. An `allow` "
           "rule is answered by a `consent-mcp` standing grant (`levain.authority.CONSENT_MCP_GRANT_KIND`) whose "
           "`mcp_fields` declares each argument by its JSON type (str, int, bool, strs); an argument no kind fits, or a "
           "refusal by the authority log, makes the scenario NOT EXPRESSIBLE with that exact refusal.",
           "- STRICT keeps Levain's: `actor.id` (`agent:<agent>:session/suite`), `agent.*` (`claude-code` / "
           "`unknown`), `tool.capability` (`claude-code.<tool>`), `target.*`, `policy.name` (`levain.consent.<tier>`), "
           "`approver.id` (`ssh-key:<fp>`). Every Levain receipt is v0.2-alpha, including for the v0.1 scenarios.", "",
           "## What Levain cannot express, by design", "",
           "Only true semantic gaps (not naming, not shim emulation):", "",
           "- **No static policy deny.** A deny is a human's signed answer: `policy.decision` `require-approval`, refusal "
           "`deny` / `operator-denied`, never `policy.decision = deny` / `policy-deny`. Scenarios 04, 16, 41 (16 because "
           "LEVEL_4_DENY_EXECUTED keys on `policy.decision == deny`).",
           "- **No escalate.** The nearest state is `undecided` (policy.decision `require-approval`, refusal "
           "`require-approval` / `undecided`). Scenarios 05, 45.",
           "- **Closed by S-R2: a refused or stale answer now leaves approval evidence.** The queue keeps a refused "
           "answer that verified under the key it names (`refused_answers`); the exporter carries it on the expired "
           "row as the approval block (`deny` / `approver-not-authorized` for an unauthorised key, `expired` / "
           "`approval-window-elapsed` for an authorised one too late). Scenarios 43, 44 now fail in STRICT on naming "
           "only and pass in BOUND.",
           "- **A missing approval is a lapse, not `approval-missing`.** Queue-state choice: verdict `expired` / "
           "`approval-window-elapsed`. Scenario 42 (also an exporter mapping the product could change).",
           "- **A consent-mcp grant cannot declare every input.** No kind for a float, null or object argument "
           "(scenarios 20, 39), and a grant must declare 1 to 32 fields, so a no-argument MCP tool cannot be granted "
           "(34). These requests go to the human.",
           "- **Hook actors are agents, and there is no environment concept.** `actor.type` is `agent` (19, 36) and "
           "`target.environment` is the fixed `dev` (35).", "",
           "## Remaining STRICT FAILs, by category", "",
           "- **Naming (Levain names its own policy `levain.consent.<tier>` and approver `ssh-key:<fp>`; the scenario's "
           "verifier context knows others; BOUND shows the structure is accepted):** 01, 02, 17, 18, 25, 28, 29, 31, 32, "
           "38, 43, 44, 48, 53, plus the naming half of 19, 35, 36, 41, 42, 45. The NAMES run is where these close: "
           "see the NAMES section above for what is left.",
           "- **Shim emulation gap:** none left that is known. 49 is a suite assumption (the runner's "
           "`replace_in_payload` needs the emitted receipt to echo `action.target`; Levain's resource id is the cwd).",
           "- **Exporter mapping:** 35 (`target.environment`), 42 (`approval-missing` vs `expired`).",
           "- **Real Levain semantic mismatch:** the list above.", "",
           "## Matrix", "", "| scenario | strict | fallback | names | bound | queue ended as (strict / fallback) |",
           "|---|---|---|---|---|---|"]
    for i, (a, f, b, m) in enumerate(zip(strict, fallback, bound, names)):
        k = f"{i + 1:03d}"
        out.append(f"| {a['scenario']} | {_kind(a)} | {_kind(f)} | {_kind(m)} | {_kind(b)} | "
                   f"{strict_trace.get(k, 'n/a')} / {fb_trace.get(k, '')} |")
    out += ["", "## Failures, with diagnosis", "",
            "Strict failures are listed with their fallback / bound outcome. N/E scenarios share one cause and are "
            "listed once below.", ""]
    ne_names = [a["scenario"] for a in strict if _kind(a) == "N/E"]
    if ne_names:
        out += ["### NOT EXPRESSIBLE in STRICT", "",
                "A consent-mcp grant declares each input field of the MCP tool by a kind (path, paths, str, strs, int, "
                "bool; 1 to 32 fields). The arguments of these scenarios fit none (a float or object value, or no "
                "argument at all), so the request would go to the human: a real gap in what a grant can declare, "
                "reported with Levain's refusal and not forced. Scenarios: " + ", ".join(ne_names) + ".", ""]
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
    '43-refusal-approval-expired': "NAMING only (S-R2). The enrolled approver's answer reaches the queue after the hold lapsed, is refused as late and kept; the exporter carries it as the approval block of the `expired` / `approval-window-elapsed` receipt, `approved_at` the scenario's (O3). The one remaining failure is LEVEL_4_POLICY_DOWNGRADE (`levain.consent.<tier>` is not in the scenario's policy store); BOUND passes.",
    '44-refusal-unauthorized-approver': "NAMING only (S-R2). The shim's unenrolled key really signs an allow and Levain really refuses it (AnswerRefused) and keeps it; the exporter exports the expired row as `deny` / `approver-not-authorized` with the refused answer as the approval block (approver `ssh-key:<fp>`, no operator role: an unenrolled key; context `enrolled: false`). Remaining: LEVEL_4_POLICY_DOWNGRADE and `approval.approver.id` (`ssh-key:<fp>` vs `user:intern@acme`), both naming; BOUND passes.", '47-refusal-contradicts-policy': 'NOT EXPRESSIBLE in STRICT (needs `policy.decision = allow`). Even in FALLBACK the fault cannot be planted: with policy.decision `require-approval`, every refusal verdict is consistent (`_REFUSAL_CONSISTENT_DECISIONS`), so LEVEL_2_REFUSAL_DECISION_MISMATCH cannot fire. Root cause is the same as the N/E set: no no-human allow.',
    '46-refusal-claims-execution': "EXPORTER CHANGE (R11, was PASS at R10). A human deny now exports with NO approval block (a denial is not an approval); the scenario injects execution.status = success, and a v0.2-alpha receipt claiming success must carry an approval, so the schema rejects it before LEVEL_2_REFUSAL_EXECUTED can fire. Not a shim artefact.", '01-merge-allow': "NAMING (answered by a consent-mcp grant on `mcp__<server>__<tool>`). `policy.decision = allow` holds; only `policy.name` (`levain.consent.T2` vs the scenario's) differs. FALLBACK/BOUND answer by a signed allow, whose decision is `require-approval`, so they fail on that instead (informational).", '20-numeric-arguments-validate': "NOT EXPRESSIBLE: a float argument has no consent-mcp field kind (str, int, bool, strs, path, paths).", '34-empty-arguments-validate': "NOT EXPRESSIBLE: a consent-mcp grant must declare 1 to 32 fields, so a no-argument MCP tool cannot be granted.", '39-nested-arguments-canonical': "NOT EXPRESSIBLE: an object argument has no consent-mcp field kind.", '28-honest-completeness-passes': "NAMING in STRICT (LEVEL_4_POLICY_DOWNGRADE only; the grant-allow receipt has no approval block, so the completeness score now matches). FALLBACK ARTIFACT otherwise: its signed allow adds an approval block the injected provenance map does not list.", '49-envelope-payload-tampered': "SUITE ASSUMPTION. The runner's `replace_in_payload` finds `pull/14` (the scenario's `target.resource_id`) in the emitted receipt and raises if it is absent. Levain's `resource_id` is the request's cwd, so STRICT/FALLBACK crash in the suite's grading (`GradingError`). BOUND restores the target and passes. The suite assumes an implementation echoes `action.target`.", '19-actor-human-validates': "FALLBACK: naming (policy downgrade) and `actor.type` `agent` where the scenario's action is a human actor. Levain's hook actors are always agents (a semantic gap, by design).", '36-dev-environment-validates': "FALLBACK: `actor.type` agent vs the scenario's human actor (naming).", '35-staging-environment-validates': "FALLBACK: `target.environment` is Levain's fixed `dev` (synthesized), the scenario's is `staging` (naming)."}


# scenario name -> the reviewed reason it still fails in NAMES (filled in after reading the run, as DIAGNOSES)
_NO_STATIC_DENY = ("by design: no static policy deny. A deny is a human's signed answer (policy.decision "
                   "`require-approval`, refusal `deny` / `operator-denied`); `policy.decision` is a fact, never a name.")
_NO_ESCALATE = ("by design: no escalate. The nearest state is `undecided` (policy.decision `require-approval`); "
                "`policy.decision` is a fact.")
_ACTOR_TYPE = ("by design: `actor.type` is a FACT (what kind of actor acted) and a names file may not set it; Levain's "
               "hook actors are agents, the scenario's actor is human.")
_NOT_EXPRESSIBLE = "NOT EXPRESSIBLE (as STRICT): a consent-mcp grant cannot declare these arguments, so no allow exists."
NAMES_REASONS: dict[str, str] = {
    "04-merge-deny": _NO_STATIC_DENY, "41-refusal-policy-deny": _NO_STATIC_DENY + " Also `operator-denied` is "
    "not `policy-deny` (a refusal reason is a fact).",
    "16-deny-with-execution-success": _NO_STATIC_DENY + " The injected success then fails the schema (a "
    "require-approval success needs an approval block) before LEVEL_4_DENY_EXECUTED can fire.",
    "05-refund-escalate": _NO_ESCALATE, "45-refusal-escalation-unresolved": _NO_ESCALATE,
    "19-actor-human-validates": _ACTOR_TYPE, "36-dev-environment-validates": _ACTOR_TYPE,
    "35-staging-environment-validates": "by design (names fix r2): `target.environment` is a FACT (the spec: it MUST "
    "be prod, staging or dev, where the action ran) and a names file may not set it; Levain records `dev` "
    "(synthesized: the hook carries no environment), the scenario's action ran in `staging`. Passed at r1, when a "
    "names file could rewrite it.",
    "20-numeric-arguments-validate": _NOT_EXPRESSIBLE + " (a float argument)",
    "34-empty-arguments-validate": _NOT_EXPRESSIBLE + " (a grant declares 1 to 32 fields; this tool has none)",
    "39-nested-arguments-canonical": _NOT_EXPRESSIBLE + " (an object argument)",
    "42-refusal-approval-missing": "exporter mapping / queue-state choice: a missing approval is a lapse, exported "
    "`expired` / `approval-window-elapsed`, not `require-approval` / `approval-missing` (refusal fields are facts).",
    "46-refusal-claims-execution": "by design (R11): a human deny has NO approval block, so the injected "
    "`execution.status = success` fails the schema before LEVEL_2_REFUSAL_EXECUTED can fire.",
}


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
        names_impl = LevainImplementation(keydir, _mk(tmp / "names"), names_file=True, keys=strict_impl._keys)
        names = _run(scenarios, names_impl)
        print("STRICT\n" + _matrix(strict) + "\n\nFALLBACK (informational)\n" + _matrix(fallback)
              + "\n\nNAMES (the product path: STRICT plus an operator's names file)\n" + _matrix(names)
              + "\n\nBOUND (informational)\n" + _matrix(bound))
        if ns.results:
            _write_results(ns.results, strict, fallback, bound, strict_impl.trace, fb_impl.trace, names)
    finally:
        if not ns.keep:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0 if all(r["passed"] for r in strict) else 1


def _mk(p: Path) -> Path:
    p.mkdir(parents=True)
    return p


if __name__ == "__main__":
    sys.exit(main())
