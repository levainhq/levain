"""Q19 S1: the consent queue's receipts exported as AgentBoundary v0.2-alpha Action Receipts
(:mod:`levain.consent.agentboundary`).

The queue is built through its real API (real ssh-keygen keys, a real signed authority log holding a standing grant),
so each receipt row the exporter maps is one the production paths write. The spec's own validator is NOT imported
here (agentboundary is alpha and never a levain dependency); ``build_scratch`` is shared with the S1 run script,
which feeds the same export to agentboundary's validator, conformance checker and verifier in a separate venv.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from levain.cockpit.consent import parse_stamp, utc_stamp
from levain.consent import ConsentQueue, ruling_message
from levain.consent import agentboundary as ab
from levain.consent.ledger import Ledger
from levain.consent.queue import UNDECIDED_GRACE
from levain.consent.record import RULING_NAMESPACE
from tests.test_consent_queue import Clock, Key, Log, signed
from tests._h4 import h4_home

HEX64 = re.compile(r"^[a-f0-9]{64}$")
REAL_UTCNOW = ab._utcnow


@pytest.fixture(autouse=True, scope="module")
def _far_future_real_clock():
    """The exporter refuses an export clock ahead of the real one, and these tests export 'days later' on queues whose
    own clock is real time: the real clock the exporter compares with is moved out of the way (the refusal itself is
    tested with the real one)."""
    patch = pytest.MonkeyPatch()
    patch.setattr(ab, "_utcnow", lambda: datetime.now(timezone.utc) + timedelta(days=60))
    yield
    patch.undo()


def build_scratch(tmp: Path) -> tuple[ConsentQueue, dict[str, str]]:
    """A queue over a signed authority log (one enrolled laptop key, one standing grant ``g1`` for ``Edit`` in a
    workspace) holding a request in every state the exporter maps; returns it and ``name -> request id``."""
    keys_dir = tmp / "keys"
    keys_dir.mkdir()
    laptop = Key(keys_dir, "laptop")
    (tmp / "auth").mkdir()
    log = Log(tmp / "auth", {"laptop": laptop})
    log.enrol("laptop", "laptop-presence")
    ws = Path(os.path.realpath(tmp)) / "proj"
    ws.mkdir()
    log.link({"op": "create_grant", "grant": {
        "id": "g1", "kind": "consent", "agent": "claude_code", "tools": ["Edit"], "workspace": str(ws),
        "postures": ["default"], "expires_at": utc_stamp(datetime.now(timezone.utc) + timedelta(days=1))}})
    clock = Clock(datetime.now(timezone.utc))
    queue = ConsentQueue(tmp / "consent", authority=log.source, clock=clock, home=h4_home())
    ids: dict[str, str] = {}

    calls: dict[str, tuple[str, Any, str, str | None, str | None]] = {}     # name -> what the agent's hook reports

    def file(name: str, tool: str, tool_input: Any, *, agent: str = "claude_code", cwd: str = str(ws),
             hold: timedelta = timedelta(minutes=5), tier: str = "T2", use_id: str | None = None,
             ends: str | None = None):
        """File a request; ``ends`` (``post`` / ``post_failure``) files the ledger row of its call at the end."""
        request = queue.file(agent=agent, agent_session_id=f"sess-{name}", tool_name=tool, tool_input=tool_input,
                             cwd=cwd, hold=hold, tier=tier, event_id=use_id)
        ids[name] = request.id
        if ends is not None:
            calls[name] = (tool, tool_input, cwd, use_id, ends)
        return request

    edit = {"file_path": str(ws / "a.txt"), "old_string": "a", "new_string": "b"}
    request = file("grant", "Edit", edit, use_id="tu-grant", ends="post")
    assert queue.answer_by_grant(request.id, tool_input=edit, posture="default") is not None
    request = file("signed_allow", "Bash", {"command": "touch /tmp/x"}, ends="post")
    answer = signed(queue, request, laptop, "allow")
    clock.now += timedelta(seconds=2)                      # the queue applies the answer after the signer signed it
    queue.answer(request.id, *answer)
    request = file("signed_same", "Bash", {"command": "touch /tmp/same"}, ends="post")
    queue.answer(request.id, *signed(queue, request, laptop, "allow"))     # applied in the stamp it was signed in
    request = file("failed", "Bash", {"command": "false"}, use_id="tu-failed", ends="post_failure")
    queue.answer(request.id, *signed(queue, request, laptop, "allow"))
    request = file("unrecorded", "Bash", {"command": "never ended"})        # permitted; no ledger row ever came
    queue.answer(request.id, *signed(queue, request, laptop, "allow"))
    request = file("denied", "Write", {"file_path": "/tmp/y"}, agent="codex")
    answer = signed(queue, request, laptop, "deny")
    clock.now += timedelta(seconds=2)
    queue.answer(request.id, *answer)
    request = file("undecided", "Bash", {"command": "rm -r build"})
    assert queue.mark_undecided(request.id).state == "undecided"
    request = file("ruled_allow", "Bash", {"command": "make"}, tier="T3", ends="post")
    assert queue.mark_undecided(request.id).gap == 1
    payload = queue.ruling_payload(request.id, "allow", laptop.fp)
    clock.now += timedelta(seconds=2)
    queue.answer_late(request.id, payload, laptop.sign(ruling_message(payload), RULING_NAMESPACE))
    request = file("ruled_deny", "Bash", {"command": "make clean"})
    assert queue.mark_undecided(request.id).gap == 1
    payload = queue.ruling_payload(request.id, "deny", laptop.fp)
    clock.now += timedelta(seconds=2)
    queue.answer_late(request.id, payload, laptop.sign(ruling_message(payload), RULING_NAMESPACE))
    request = file("withdrawn", "Bash", {"command": "ls"})
    assert queue.withdraw(request.id)
    file("ran", "Bash", {"command": "echo ran"})
    clock.now += timedelta(seconds=2)                      # the operator answers the dialog after it opened
    assert queue.record_ran(agent="claude_code", agent_session_id="sess-ran", tool_name="Bash",
                            tool_input={"command": "echo ran"}, event="PostToolUse")[1] == "ran"      # answered while held
    request = file("ran_unasked", "Bash", {"command": "echo unasked"})
    assert queue.withdraw(request.id)
    assert queue.record_ran(agent="claude_code", agent_session_id="sess-ran_unasked", tool_name="Bash",
                            tool_input={"command": "echo unasked"}, event="PostToolUse")[1] == "ran_unasked"
    request = file("did_not_run", "Bash", {"command": "echo no"})
    assert queue.withdraw(request.id)
    assert queue.close_unrun(agent="claude_code", agent_session_id="sess-did_not_run", event="stop") == [request.id]
    file("float", "Bash", {"command": "sleep", "seconds": 1.5})          # outside JCS: a json: digest
    file("decomposed", "Bash", {"command": "pwd"}, cwd="/w/café")    # a working directory not in NFC
    request = file("expiring", "Bash", {"command": "sleep 1"}, hold=timedelta(minutes=1))
    clock.now += timedelta(minutes=1) + UNDECIDED_GRACE + timedelta(seconds=1)
    assert queue.sweep() == 1
    file("pending", "Bash", {"command": "still waiting"})
    file_ledger_rows(queue, ids, calls)
    return queue, ids


def file_ledger_rows(queue: ConsentQueue, ids: dict[str, str], calls: dict[str, tuple], *,
                     after: timedelta = timedelta(seconds=3)) -> None:
    """The ledger rows the agent's PostToolUse / PostToolUseFailure hooks file for the calls that ended, the way
    ``levain.consent.ledger.hook_record`` files them (agent, session key, tool, input, cwd, phase, tool-use id):
    ``after`` past each request's decision."""
    ledger = Ledger(queue.directory)
    conn = sqlite3.connect(queue.path)
    try:
        decided = {name: conn.execute("SELECT decided_at FROM requests WHERE id = ?", (ids[name],)).fetchone()[0]
                   for name in calls}
    finally:
        conn.close()
    for name, (tool, tool_input, cwd, use_id, phase) in calls.items():
        ledger.record_call(agent="claude_code", agent_session_id=f"sess-{name}", tool_name=tool, tool_input=tool_input,
                           cwd=cwd, phase=phase, tool_use_id=use_id,
                           received_at=parse_stamp(decided[name]) + after)


def later(queue, days=2):
    """An export clock past SETTLE for everything the scratch queue holds."""
    return queue._clock.now + timedelta(days=days)


@pytest.fixture(scope="module")
def scratch(tmp_path_factory):
    queue, ids = build_scratch(tmp_path_factory.mktemp("ab"))
    return queue, ids, ab.export(queue.directory, now=later(queue))


def _by_request(queue, exported):
    conn = sqlite3.connect(queue.path)
    try:
        seq = {(r[0], r[1]): r[2] for r in conn.execute("SELECT request_id, state, seq FROM receipts")}
    finally:
        conn.close()
    qid = queue.queue_id
    return {(rid, state): next(r for r in exported.receipts
                               if r["execution"]["result_ref"] == f"levain-receipt:{qid}:{n}")
            for (rid, state), n in seq.items() if state != "pending"}


def _row(scratch, name, state):
    queue, ids, exported = scratch
    return _by_request(queue, exported)[(ids[name], state)]


def test_every_terminal_row_is_one_receipt_in_seq_order_and_pending_is_not_exported(scratch):
    queue, ids, exported = scratch
    conn = sqlite3.connect(queue.path)
    try:
        rows = conn.execute("SELECT seq, state FROM receipts ORDER BY seq").fetchall()
    finally:
        conn.close()
    terminal = [(seq, state) for seq, state in rows if state != "pending"]
    qid = queue.queue_id
    assert [r["execution"]["result_ref"] for r in exported.receipts] == [f"levain-receipt:{qid}:{s}" for s, _ in terminal]
    # an undecided request that was ruled has two rows, so two receipts
    assert len([1 for r in terminal if r[1] == "undecided"]) == 3
    assert exported.report.receipts == len(terminal) == len(exported.receipts)
    assert exported.report.by_state == {"allowed": 6, "denied": 2, "expired": 1, "undecided": 3, "withdrawn": 4}
    assert exported.report.pending_skipped == len(rows) - len(terminal)


def test_ids_are_deterministic_and_the_chain_links_each_receipt_to_the_one_before(scratch):
    queue, ids, exported = scratch
    again = ab.export(queue.directory, now=later(queue))
    assert again.receipts == exported.receipts and again.report == exported.report
    assert json.dumps(again.receipts, sort_keys=True) == json.dumps(exported.receipts, sort_keys=True)
    qid = queue.queue_id
    assert ab.LEVAIN_AB_NS == uuid.uuid5(uuid.NAMESPACE_URL, ab._NS_NAME)
    for r in exported.receipts:
        seq = r["execution"]["result_ref"].rsplit(":", 1)[1]
        assert r["receipt_id"] == str(uuid.uuid5(ab.LEVAIN_AB_NS, f"{qid}:{seq}"))
    assert "prior_receipt" not in exported.receipts[0]
    for before, after in zip(exported.receipts, exported.receipts[1:]):
        assert after["prior_receipt"] == {"receipt_id": before["receipt_id"], "receipt_hash": before["receipt_hash"]}
    assert len({r["receipt_id"] for r in exported.receipts}) == len(exported.receipts)


def test_receipt_hash_is_the_jcs_sha256_of_the_receipt_without_it_and_the_score_is_in_it(scratch):
    _, _, exported = scratch
    for r in exported.receipts:
        assert HEX64.match(r["receipt_hash"]) and ab.receipt_hash(r) == r["receipt_hash"]
        score = r["completeness_score"]
        assert 0 <= score <= 1 and score == round(score, 3)
        tampered = dict(r, completeness_score=0.999 if score != 0.999 else 0.998)
        assert ab.receipt_hash(tampered) != r["receipt_hash"]
        assert ab.receipt_hash(dict(r, tool={**r["tool"], "name": r["tool"]["name"] + "x"})) != r["receipt_hash"]


@pytest.mark.parametrize("milli,text", [(1000, "1"), (800, "0.8"), (0, "0"), (5, "0.005"), (123, "0.123"),
                                        (500, "0.5"), (999, "0.999")])
def test_the_score_is_written_as_its_exact_es6_text(milli, text):
    assert str(ab._Fraction(milli)) == text and float(text) == milli / 1000


def test_completeness_score_follows_the_spec_formula_over_the_provenance(scratch):
    _, _, exported = scratch
    weights = {"observed": 2, "inferred": 1, "synthesized": 0}
    for r in exported.receipts:
        applicable = list(ab._REQUIRED_PATHS) + [p for p in ab._CONDITIONAL_PATHS if ab._present(r, p)]
        assert set(r["provenance"]) == set(applicable)          # an entry for every applicable path, none extra
        total = sum(weights[r["provenance"][p]] for p in applicable)
        assert r["completeness_score"] == (total * 1000 // (2 * len(applicable))) / 1000


def test_a_grant_allow_is_allow_success_with_no_approval_and_no_refusal(scratch):
    r = _row(scratch, "grant", "allowed")
    assert r["policy"]["decision"] == "allow" and r["execution"]["status"] == "success"
    assert "approval" not in r and "refusal" not in r
    assert r["tool"] == {"name": "Edit", "capability": "claude-code.edit"}
    assert r["actor"] == {"type": "agent", "id": "agent:claude_code:session/sess-grant"}
    assert r["policy"]["name"] == "levain.consent.T2" and r["policy"]["version"] == "unknown"
    assert r["target"]["system"] == "levain-workspace" and r["target"]["environment"] == "dev"
    assert r["agent"] == {"framework": "claude-code", "framework_version": "unknown", "model": "unknown"}
    assert r["provenance"]["agent.model"] == "synthesized" and r["provenance"]["tool.name"] == "observed"


def test_a_signed_allow_requires_approval_and_carries_the_operators_approval(scratch):
    queue, ids, _ = scratch
    r = _row(scratch, "signed_allow", "allowed")
    assert r["policy"]["decision"] == "require-approval" and r["execution"]["status"] == "success"
    assert "refusal" not in r
    approval = r["approval"]
    assert approval["approver"]["id"].startswith("ssh-key:SHA256:") and approval["approver"]["role"] == "operator"
    assert approval["approved_at"] < r["execution"]["completed_at"] == r["issued_at"]
    assert approval["approved_at"] == _signed_at(queue, ids["signed_allow"])
    assert r["provenance"]["approval.approved_at"] == "inferred"
    assert r["provenance"]["execution.status"] == "inferred"      # the ledger saw the call end
    assert json.loads(approval["context"]) == {"queue": queue.queue_id, "request_id": ids["signed_allow"],
                                               "via": "signature", "verify": "levain consent receipts verify"}


def _decided_at(queue, request_id):
    conn = sqlite3.connect(queue.path)
    try:
        return conn.execute("SELECT decided_at FROM requests WHERE id = ?", (request_id,)).fetchone()[0]
    finally:
        conn.close()


def _signed_at(queue, request_id):
    conn = sqlite3.connect(queue.path)
    try:
        return json.loads(conn.execute("SELECT answer FROM requests WHERE id = ?", (request_id,)).fetchone()[0])["signed_at"]
    finally:
        conn.close()


def test_a_signed_answer_in_the_stamp_it_was_applied_falls_back_to_the_decision_time_and_is_counted(scratch):
    r = _row(scratch, "signed_same", "allowed")
    assert r["approval"]["approved_at"] == _decided_at(scratch[0], scratch[1]["signed_same"]) < r["execution"]["completed_at"]
    assert r["provenance"]["approval.approved_at"] == "synthesized"
    assert scratch[2].report.approved_at_fallback == 3          # signed_same, failed, unrecorded: all signed in-stamp


def test_approved_at_precedes_completed_at_for_every_signed_allow_but_the_counted_fallback(scratch):
    _, ids, exported = scratch
    seen = 0
    for r in exported.receipts:
        if "approval" in r and r["approval"]["approver"]["id"].startswith("ssh-key:"):
            seen += 1
            if r["provenance"]["approval.approved_at"] == "inferred":
                assert r["approval"]["approved_at"] < r["execution"]["completed_at"]
    assert seen == 5           # signed_allow, signed_same, ruled_allow, failed, unrecorded


def test_a_denied_request_is_a_refusal_deny_with_no_approval_block(scratch):
    r = _row(scratch, "denied", "denied")
    assert r["policy"]["decision"] == "require-approval" and r["execution"]["status"] == "blocked"
    assert r["refusal"] == {"verdict": "deny", "reason_code": "operator-denied"}
    assert "approval" not in r and not any(p.startswith("approval") for p in r["provenance"])
    assert r["tool"]["capability"] == "codex.write"
    assert r["agent"]["framework"] == "codex"


def test_an_expired_request_is_a_refusal_expired(scratch):
    r = _row(scratch, "expiring", "expired")
    assert r["policy"]["decision"] == "require-approval" and r["execution"]["status"] == "blocked"
    assert r["refusal"] == {"verdict": "expired", "reason_code": "approval-window-elapsed"}
    assert "approval" not in r


def test_undecided_then_ruled_is_two_receipts_the_first_a_refusal_the_second_the_ruling(scratch):
    first = _row(scratch, "ruled_allow", "undecided")
    assert first["execution"]["status"] == "blocked" and "approval" not in first
    assert first["refusal"] == {"verdict": "require-approval", "reason_code": "undecided"}
    second = _row(scratch, "ruled_allow", "allowed")
    assert second["policy"]["decision"] == "require-approval" and second["execution"]["status"] == "success"
    assert json.loads(second["approval"]["context"])["via"] == "ruling" and "refusal" not in second
    assert second["approval"]["approved_at"] < second["execution"]["completed_at"]
    assert second["policy"]["name"] == "levain.consent.T3"
    denied = _row(scratch, "ruled_deny", "denied")
    assert denied["refusal"]["verdict"] == "deny" and "approval" not in denied
    # the undecided receipt keeps its own stamp, not the later ruling's decision time
    assert first["execution"]["completed_at"] == first["issued_at"] and first["provenance"]["execution.completed_at"] == "inferred"
    assert _row(scratch, "undecided", "undecided")["refusal"]["reason_code"] == "undecided"


def test_withdrawn_and_dialog_outcomes(scratch):
    withdrawn = _row(scratch, "withdrawn", "withdrawn")
    assert withdrawn["refusal"] == {"verdict": "require-approval", "reason_code": "withdrawn"}
    assert withdrawn["execution"]["status"] == "blocked"
    did_not_run = _row(scratch, "did_not_run", "withdrawn")
    assert did_not_run["execution"]["status"] == "blocked" and "refusal" not in did_not_run
    assert did_not_run["provenance"]["execution.status"] == "inferred"
    _, _, exported = scratch
    assert (exported.report.dialog_ran, exported.report.dialog_ran_unasked,
            exported.report.dialog_did_not_run) == (1, 1, 1)


def _outcome_time(scratch, name):
    queue, ids, _ = scratch
    conn = sqlite3.connect(queue.path)
    try:
        return conn.execute("SELECT closed_at FROM dialog_outcomes WHERE request_id = ?", (ids[name],)).fetchone()[0]
    finally:
        conn.close()


def test_a_ran_outcome_is_a_success_with_an_unsigned_native_dialog_approval(scratch):
    ran = _row(scratch, "ran", "withdrawn")
    at = _outcome_time(scratch, "ran")
    assert ran["policy"]["decision"] == "require-approval" and "refusal" not in ran
    assert ran["execution"]["status"] == "success" and ran["execution"]["completed_at"] == at
    approval = ran["approval"]
    assert approval["approver"] == {"id": "native-dialog:operator", "role": "operator"}
    conn = sqlite3.connect(scratch[0].path)
    try:
        received = conn.execute("SELECT received_at FROM requests WHERE id = ?", (scratch[1]["ran"],)).fetchone()[0]
    finally:
        conn.close()
    assert approval["approved_at"] == received < at          # no approval precedes the request: a lower bound
    assert approval["approved_at"] < ran["execution"]["completed_at"]
    assert ran["provenance"]["approval.approved_at"] == "synthesized"
    context = json.loads(approval["context"])
    assert context["via"] == "native-dialog" and context["signed"] is False
    assert context["request_id"] == scratch[1]["ran"] and context["queue"] == scratch[0].queue_id
    assert ran["provenance"]["approval.approver.id"] == "inferred"
    assert ran["provenance"]["execution.status"] == "inferred"
    # the spec's conditional (success under require-approval needs an approval) holds, and the schema accepts it
    assert not ab_validate(ran)


def test_a_ran_unasked_outcome_is_an_allow_under_the_agents_own_permissions_with_no_approval(scratch):
    unasked = _row(scratch, "ran_unasked", "withdrawn")
    assert unasked["policy"] == {"name": "claude-code.native-permissions", "version": "unknown", "decision": "allow"}
    assert unasked["execution"]["status"] == "success" and "approval" not in unasked and "refusal" not in unasked
    assert unasked["execution"]["completed_at"] == _outcome_time(scratch, "ran_unasked")
    assert unasked["provenance"]["policy.name"] == "inferred" and unasked["provenance"]["policy.version"] == "synthesized"
    assert ("claude-code.native-permissions", "unknown") in scratch[2].referenced_policies()
    assert not ab_validate(unasked)


def ab_validate(receipt):
    """The spec's own validator; its tests are skipped where agentboundary is not installed (the S1 venv has it)."""
    agentboundary = pytest.importorskip("agentboundary")
    return agentboundary.validate_receipt(receipt)


def test_refusal_and_decision_are_always_consistent_and_approval_follows_the_schema_rule(scratch):
    consistent = {"deny": {"deny", "require-approval", "escalate"}, "require-approval": {"require-approval", "escalate"},
                  "expired": {"require-approval", "escalate"}}
    _, _, exported = scratch
    for r in exported.receipts:
        if "refusal" in r:
            assert r["execution"]["status"] == "blocked"
            assert r["policy"]["decision"] in consistent[r["refusal"]["verdict"]]
        if r["policy"]["decision"] == "require-approval" and r["execution"]["status"] in ("success", "failure"):
            assert "approval" in r
        assert (r["policy"]["decision"], r["execution"]["status"]) != ("allow", "blocked") or "refusal" not in r
        assert r["policy"]["decision"] != "deny"


def test_a_json_digest_on_a_terminal_row_is_counted_and_marked(tmp_path):
    queue, ids = build_scratch(tmp_path)
    assert queue.withdraw(ids["float"])
    exported = ab.export(queue.directory, now=later(queue))
    assert exported.report.synthesized_arguments_hash == 1
    r = next(r for r in exported.receipts if r["actor"]["id"].endswith("sess-float"))
    assert r["provenance"]["arguments_hash"] == "synthesized" and HEX64.match(r["arguments_hash"])
    assert r["arguments_hash"] == _stored_digest(queue, ids["float"])[5:]
    ok = next(r for r in exported.receipts if r["actor"]["id"].endswith("sess-signed_allow"))
    assert ok["provenance"]["arguments_hash"] == "observed"
    assert ok["arguments_hash"] == _stored_digest(queue, ids["signed_allow"])[4:]


def _stored_digest(queue, request_id):
    conn = sqlite3.connect(queue.path)
    try:
        return conn.execute("SELECT tool_input_digest FROM requests WHERE id = ?", (request_id,)).fetchone()[0]
    finally:
        conn.close()


def test_a_decomposed_working_directory_is_normalised_and_hashes_under_both_canonical_forms(tmp_path):
    queue, ids = build_scratch(tmp_path)
    assert queue.withdraw(ids["decomposed"])
    exported = ab.export(queue.directory, now=later(queue))
    r = next(r for r in exported.receipts if r["actor"]["id"].endswith("sess-decomposed"))
    assert r["target"]["resource_id"] == unicodedata.normalize("NFC", "/w/café") == "/w/café"
    assert exported.report.nfc_normalised == 1
    assert ab.receipt_hash(r) == r["receipt_hash"]


def test_export_never_writes_the_store(tmp_path):
    queue, _ = build_scratch(tmp_path)
    before = (queue.path.read_bytes(), os.stat(queue.path).st_mtime_ns)
    ab.export(queue.directory, now=later(queue))
    assert (queue.path.read_bytes(), os.stat(queue.path).st_mtime_ns) == before
    # nothing is created beside the database either
    names = sorted(p.name for p in queue.directory.iterdir())
    ab.export(queue.directory, now=later(queue))
    assert sorted(p.name for p in queue.directory.iterdir()) == names


def test_the_read_connection_refuses_a_write(tmp_path):
    queue, _ = build_scratch(tmp_path)
    conn = ab._open(queue.directory)
    try:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("CREATE TABLE zz (a)")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            conn.execute("DELETE FROM receipts")
    finally:
        conn.close()


def test_policy_version_is_unknown_and_synthesized_on_every_receipt(scratch):
    _, _, exported = scratch
    assert {r["policy"]["version"] for r in exported.receipts} == {"unknown"}
    assert {r["provenance"]["policy.version"] for r in exported.receipts} == {"synthesized"}
    assert not hasattr(ab, "_levain_version")


def test_an_empty_queue_exports_nothing(tmp_path):
    (tmp_path / "keys").mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(tmp_path / "keys", "laptop")})
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, home=h4_home())
    queue.sweep()                                                       # opens the store, creating the schema
    exported = ab.export(queue.directory)
    assert exported.receipts == [] and exported.referenced_policies() == set()
    assert exported.report.receipts == 0 and exported.report.held_unsettled == 0 and exported.report.pending_skipped == 0


def _late_world(tmp_path):
    """A bare queue holding one request withdrawn at T0, whose PostToolUse comes later."""
    (tmp_path / "keys").mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(tmp_path / "keys", "laptop")})
    clock = Clock(datetime.now(timezone.utc))
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    request = queue.file(agent="claude_code", agent_session_id="s-late", tool_name="Bash", tool_input={"command": "late"},
                         cwd=str(tmp_path), hold=timedelta(minutes=5), tier="T2")
    assert queue.withdraw(request.id)
    t0 = clock.now
    return queue, clock, t0


def _ran_late(queue):
    return queue.record_ran(agent="claude_code", agent_session_id="s-late", tool_name="Bash",
                            tool_input={"command": "late"}, event="PostToolUse")


def test_a_late_outcome_inside_settle_is_held_back_then_exported_with_an_identical_prefix(tmp_path):
    queue, clock, t0 = _late_world(tmp_path)
    first = ab.export(queue.directory, now=t0 + timedelta(minutes=1))
    assert first.report.held_unsettled == 1
    assert not any(r["actor"]["id"].endswith("session/s-late") for r in first.receipts)
    clock.now = t0 + timedelta(minutes=2)
    assert _ran_late(queue)[1] == "ran"
    second = ab.export(queue.directory, now=t0 + timedelta(minutes=3))
    n = len(first.receipts)
    assert second.receipts[:n] == first.receipts and second.report.held_unsettled == 0
    late = second.receipts[n]
    assert late["actor"]["id"].endswith("session/s-late") and late["execution"]["status"] == "success"
    assert late["approval"]["approved_at"] < late["execution"]["completed_at"]       # withdrawn stamp < PostToolUse


def test_the_export_stops_at_the_first_unsettled_row_and_counts_what_it_held(tmp_path):
    queue, clock, t0 = _late_world(tmp_path)               # s-late: withdrawn at t0, settled by the time below
    clock.now = t0 + ab.SETTLE + timedelta(hours=6)
    young = queue.file(agent="claude_code", agent_session_id="s-young", tool_name="Bash", tool_input={"command": "y"},
                       cwd=str(tmp_path), hold=timedelta(minutes=5), tier="T2")
    assert queue.withdraw(young.id)                                     # the first unsettled row
    request = queue.file(agent="claude_code", agent_session_id="s-after", tool_name="Bash", tool_input={"command": "z"},
                         cwd=str(tmp_path), hold=timedelta(minutes=5), tier="T2")
    assert queue.mark_undecided(request.id).state == "undecided"        # a terminal row AFTER the unsettled one
    held = ab.export(queue.directory, now=clock.now + timedelta(minutes=1))
    assert held.report.held_unsettled == 2
    assert held.report.receipts == 1 and len(held.receipts) == 1        # only the settled row before the stop
    assert held.receipts[0]["actor"]["id"].endswith("session/s-late")
    assert all("s-after" not in r["actor"]["id"] and "s-young" not in r["actor"]["id"] for r in held.receipts)
    later_export = ab.export(queue.directory, now=clock.now + ab.SETTLE * 2)
    assert later_export.report.receipts == 3 and later_export.receipts[0] == held.receipts[0]


def test_a_row_older_than_settle_exports_by_state_and_a_later_outcome_changes_nothing(tmp_path):
    queue, clock, t0 = _late_world(tmp_path)
    now = t0 + ab.SETTLE + timedelta(seconds=1)
    first = ab.export(queue.directory, now=now)
    assert first.report.held_unsettled == 0
    late = first.receipts[-1]
    assert late["actor"]["id"].endswith("session/s-late")
    assert late["execution"]["status"] == "blocked" and late["refusal"]["reason_code"] == "withdrawn"
    clock.now = now                                                     # the PostToolUse lands past the window
    assert _ran_late(queue)[1] == "ran"
    second = ab.export(queue.directory, now=now + timedelta(minutes=1))
    assert second.receipts == first.receipts
    # inside the window the same outcome counts
    (tmp_path / "b").mkdir()
    q2, c2, t2 = _late_world(tmp_path / "b")
    c2.now = t2 + ab.SETTLE - timedelta(seconds=1)
    assert _ran_late(q2)[1] == "ran"
    counted = ab.export(q2.directory, now=t2 + ab.SETTLE + timedelta(minutes=1)).receipts[-1]
    assert counted["execution"]["status"] == "success"


def test_an_aware_clock_is_required(tmp_path):
    queue, _ = build_scratch(tmp_path)
    with pytest.raises(ab.ExportError, match="aware"):
        ab.export(queue.directory, now=datetime(2026, 1, 1))


def test_a_store_that_is_not_a_queue_is_refused(tmp_path):
    with pytest.raises(ab.ExportError):
        ab.export(tmp_path)                                             # no database at all
    (tmp_path / "queue.db").write_bytes(b"")
    with pytest.raises(ab.ExportError):
        ab.export(tmp_path)                                             # an empty one
    conn = sqlite3.connect(tmp_path / "queue.db")
    conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    conn.execute("CREATE TABLE requests (id TEXT)")
    conn.execute("CREATE TABLE receipts (seq INTEGER)")
    conn.execute("INSERT INTO meta VALUES ('format', 'levain-consent-queue/3')")
    conn.commit()
    conn.close()
    with pytest.raises(ab.ExportError, match="format"):
        ab.export(tmp_path)


def test_a_receipt_row_whose_request_is_missing_is_refused_not_dropped(tmp_path):
    queue, ids = build_scratch(tmp_path)
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DROP TRIGGER requests_kept")
        conn.execute("DELETE FROM requests WHERE id = ?", (ids["withdrawn"],))
    finally:
        conn.close()
    with pytest.raises(ab.ExportError, match="does not hold"):
        ab.export(queue.directory, now=later(queue))


def test_an_integer_past_the_safe_range_cannot_be_hashed():
    with pytest.raises(ab.ExportError):
        ab.receipt_hash({"n": 2 ** 60})


# --- R11: execution comes from evidence -------------------------------------------------------------------------
def _stamps_ordered(r):
    return parse_stamp(r["execution"]["completed_at"]) <= parse_stamp(r["issued_at"])


def test_an_allowed_request_takes_its_execution_from_the_ledger_row_of_its_call(scratch):
    queue, ids, exported = scratch
    grant = _row(scratch, "grant", "allowed")
    assert grant["execution"]["status"] == "success" and "error_code" not in grant["execution"]
    assert grant["execution"]["completed_at"] == grant["issued_at"] > _decided_at(queue, ids["grant"])
    assert grant["provenance"]["issued_at"] == "inferred" and grant["provenance"]["execution.status"] == "inferred"
    failed = _row(scratch, "failed", "allowed")
    assert failed["execution"]["status"] == "failure" and failed["execution"]["error_code"] == "tool-failed"
    assert failed["policy"]["decision"] == "require-approval" and "approval" in failed and "refusal" not in failed
    assert failed["provenance"]["execution.error_code"] == "inferred"
    unrecorded = _row(scratch, "unrecorded", "allowed")
    assert unrecorded["execution"]["status"] == "blocked" and unrecorded["execution"]["error_code"] == "execution-unrecorded"
    assert "refusal" not in unrecorded and "approval" in unrecorded
    assert unrecorded["provenance"]["execution.status"] == "synthesized"
    assert unrecorded["provenance"]["execution.error_code"] == "synthesized"
    assert unrecorded["issued_at"] == unrecorded["execution"]["completed_at"] == _decided_at(queue, ids["unrecorded"])
    assert (exported.report.execution_failed, exported.report.execution_unrecorded, exported.report.ledger_found) == (1, 1, True)
    for r in (grant, failed, unrecorded):
        assert not ab_validate(r)


def test_completed_at_never_follows_issued_at(scratch):
    for r in scratch[2].receipts:
        assert _stamps_ordered(r), r["execution"]["result_ref"]


def test_a_store_with_no_ledger_has_no_evidence(tmp_path):
    queue, ids = build_scratch(tmp_path)
    for p in queue.directory.glob("ledger.db*"):
        p.unlink()
    exported = ab.export(queue.directory, now=later(queue))
    assert exported.report.ledger_found is False and exported.report.execution_unrecorded == 6
    coded = [r for r in exported.receipts if "error_code" in r["execution"]]
    assert len(coded) == 6 and {r["execution"]["error_code"] for r in coded} == {"execution-unrecorded"}
    assert all(r["execution"]["status"] == "blocked" and "refusal" not in r for r in coded)
    assert not any(not _stamps_ordered(r) for r in exported.receipts)


def _allow_world(tmp_path, use_ids=(None,)):
    """A bare queue holding one signed-allow request per entry of ``use_ids`` (the same call each time, a minute
    apart); returns the queue, its clock, T0 and the requests."""
    (tmp_path / "keys").mkdir()
    (tmp_path / "auth").mkdir()
    laptop = Key(tmp_path / "keys", "laptop")
    log = Log(tmp_path / "auth", {"laptop": laptop})
    log.enrol("laptop", "laptop-presence")
    clock = Clock(datetime.now(timezone.utc))
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home())
    t0, requests = clock.now, []
    for use_id in use_ids:
        request = queue.file(agent="claude_code", agent_session_id="s-run", tool_name="Bash",
                             tool_input={"command": "same"}, cwd=str(tmp_path), hold=timedelta(minutes=5),
                             tier="T2", event_id=use_id)
        queue.answer(request.id, *signed(queue, request, laptop, "allow"))
        requests.append(request)
        clock.now += timedelta(minutes=1)
    return queue, clock, t0, requests


def _post(queue, at, *, phase="post", use_id=None, tool="Bash", command="same", session="s-run"):
    Ledger(queue.directory).record_call(agent="claude_code", agent_session_id=session, tool_name=tool,
                                        tool_input={"command": command}, cwd=str(queue.directory.parent), phase=phase,
                                        tool_use_id=use_id, received_at=at)


def test_an_allowed_request_with_no_evidence_is_held_until_settle_then_exports_unrecorded(tmp_path):
    queue, clock, t0, _ = _allow_world(tmp_path)
    young = ab.export(queue.directory, now=t0 + timedelta(hours=1))
    assert young.report.held_unsettled == 1 and young.receipts == []
    old = ab.export(queue.directory, now=t0 + ab.SETTLE + timedelta(minutes=5))
    assert old.report.held_unsettled == 0 and old.report.execution_unrecorded == 1
    assert old.receipts[0]["execution"]["status"] == "blocked"
    assert not ab_validate(old.receipts[0])


def test_evidence_inside_the_window_counts_and_the_exported_prefix_never_changes(tmp_path):
    queue, clock, t0, _ = _allow_world(tmp_path)
    assert ab.export(queue.directory, now=t0 + timedelta(minutes=1)).receipts == []
    _post(queue, t0 + timedelta(minutes=2))
    first = ab.export(queue.directory, now=t0 + timedelta(minutes=3))
    assert first.receipts[0]["execution"]["status"] == "success"
    assert ab.export(queue.directory, now=t0 + ab.SETTLE * 3).receipts == first.receipts


def test_evidence_past_the_window_changes_nothing(tmp_path):
    queue, clock, t0, _ = _allow_world(tmp_path)
    now = t0 + ab.SETTLE + timedelta(minutes=5)
    before = ab.export(queue.directory, now=now)
    _post(queue, now - timedelta(minutes=1))                    # lands past SETTLE from the terminal row
    assert ab.export(queue.directory, now=now + timedelta(minutes=1)).receipts == before.receipts
    assert before.receipts[0]["execution"]["error_code"] == "execution-unrecorded"


@pytest.mark.parametrize("post", [
    dict(at=timedelta(minutes=-1)),                              # before the decision: another call
    dict(at=timedelta(minutes=2), use_id="tu-other"),            # both carry an id, and they differ
    dict(at=timedelta(minutes=2), command="different"),          # another input digest
    dict(at=timedelta(minutes=2), tool="Edit"),
    dict(at=timedelta(minutes=2), session="s-other"),
])
def test_a_ledger_row_of_another_call_is_not_this_requests_evidence(tmp_path, post):
    queue, clock, t0, _ = _allow_world(tmp_path, use_ids=("tu-1",))
    post = dict(post)
    _post(queue, t0 + post.pop("at"), **post)
    exported = ab.export(queue.directory, now=t0 + ab.SETTLE + timedelta(hours=1))
    assert exported.receipts[0]["execution"]["error_code"] == "execution-unrecorded"


def test_an_equal_tool_use_id_pairs_and_a_row_serves_one_request(tmp_path):
    queue, clock, t0, _ = _allow_world(tmp_path, use_ids=("tu-1", "tu-2"))
    _post(queue, t0 + timedelta(minutes=3), use_id="tu-2")       # the second request's call, not the first's
    exported = ab.export(queue.directory, now=t0 + ab.SETTLE + timedelta(hours=1))
    first, second = exported.receipts
    assert first["execution"]["error_code"] == "execution-unrecorded" and second["execution"]["status"] == "success"
    # without ids, one ledger row serves the earliest request only
    (tmp_path / "b").mkdir()
    q2, c2, t2, _ = _allow_world(tmp_path / "b", use_ids=(None, None))
    _post(q2, t2 + timedelta(minutes=3))
    one, two = ab.export(q2.directory, now=t2 + ab.SETTLE + timedelta(hours=1)).receipts
    assert one["execution"]["status"] == "success" and two["execution"]["error_code"] == "execution-unrecorded"


def test_a_failure_event_closes_a_dialog_run_as_a_failure_and_a_non_tool_event_is_refused(tmp_path):
    queue, clock, t0 = _late_world(tmp_path)
    clock.now = t0 + timedelta(minutes=2)
    assert queue.record_ran(agent="claude_code", agent_session_id="s-late", tool_name="Bash",
                            tool_input={"command": "late"}, event="PostToolUseFailure")[1] == "ran"
    r = ab.export(queue.directory, now=t0 + timedelta(minutes=3)).receipts[-1]
    assert r["execution"]["status"] == "failure" and r["execution"]["error_code"] == "tool-failed"
    assert r["policy"]["decision"] == "require-approval" and "approval" in r and "refusal" not in r
    assert r["issued_at"] == r["execution"]["completed_at"] > r["approval"]["approved_at"]      # issued: the later stamp
    assert r["provenance"]["issued_at"] == "inferred" and _stamps_ordered(r)
    assert not ab_validate(r)
    (tmp_path / "b").mkdir()
    q2, c2, t2 = _late_world(tmp_path / "b")
    c2.now = t2 + timedelta(minutes=2)
    assert q2.record_ran(agent="claude_code", agent_session_id="s-late", tool_name="Bash",
                         tool_input={"command": "late"}, event="Stop")[1] == "ran"
    with pytest.raises(ab.ExportError, match="not a tool event"):
        ab.export(q2.directory, now=t2 + timedelta(minutes=3))


def test_an_export_clock_ahead_of_the_real_one_is_refused(tmp_path, monkeypatch):
    queue, _ = build_scratch(tmp_path)
    monkeypatch.setattr(ab, "_utcnow", REAL_UTCNOW)
    with pytest.raises(ab.ExportError, match="ahead of the real clock"):
        ab.export(queue.directory, now=datetime.now(timezone.utc) + timedelta(minutes=1))
    ab.export(queue.directory, now=datetime.now(timezone.utc) + timedelta(seconds=2))      # within the slack


# --- R11: provenance, fail-closed values, the score --------------------------------------------------------------
OBSERVED_ALWAYS = {"issued_at": "observed", "tool.name": "observed", "target.resource_id": "observed"}
COMPOSED = {"actor.id": "inferred", "agent.framework": "inferred", "policy.name": "inferred"}


def test_the_provenance_of_composed_values_is_inferred_for_every_row_kind(scratch):
    """Pinned per kind (the formula's own self-consistency is checked elsewhere): a value built from more than one
    column, renamed or prefixed is `inferred`; only a column copied as it is, `observed`."""
    exported = scratch[2]
    for r in exported.receipts:
        p = r["provenance"]
        assert p["actor.id"] == p["agent.framework"] == "inferred", r["execution"]["result_ref"]
        assert p["policy.name"] == "inferred" and p["tool.name"] == "observed" and p["target.resource_id"] == "observed"
        assert p["agent.framework_version"] == p["agent.model"] == p["policy.version"] == "synthesized"
        assert p["target.system"] == p["target.environment"] == "synthesized"
        assert p["tool.capability"] == "inferred" and p["execution.result_ref"] == "inferred"
        assert p["arguments_hash"] in ("observed", "synthesized")
        if "approval" in r:
            assert p["approval.approver.id"] == "inferred" and p["approval.context"] == "inferred"
            assert p["approval.approver.role"] == "synthesized"
    grant = _row(scratch, "grant", "allowed")["provenance"]
    assert grant == {
        "actor.id": "inferred", "actor.type": "inferred", "agent.framework": "inferred",
        "agent.framework_version": "synthesized", "agent.model": "synthesized", "arguments_hash": "observed",
        "execution.completed_at": "inferred", "execution.result_ref": "inferred", "execution.status": "inferred",
        "issued_at": "inferred", "policy.decision": "inferred", "policy.name": "inferred",
        "policy.version": "synthesized",
        "receipt_id": "inferred", "target.environment": "synthesized", "target.resource_id": "observed",
        "target.system": "synthesized", "tool.capability": "inferred", "tool.name": "observed"}
    denied = _row(scratch, "denied", "denied")["provenance"]
    assert denied["issued_at"] == "observed" and denied["refusal.verdict"] == denied["refusal.reason_code"] == "inferred"
    assert "execution.error_code" not in denied and not any(k.startswith("approval") for k in denied)
    ran = _row(scratch, "ran", "withdrawn")["provenance"]
    assert (ran["approval.approver.id"], ran["approval.approved_at"], ran["approval.context"]) == (
        "inferred", "synthesized", "inferred")
    assert _row(scratch, "ran_unasked", "withdrawn")["provenance"]["policy.name"] == "inferred"


def _corrupt(queue, sql, args=()):
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'").fetchall():
            conn.execute(f'DROP TRIGGER "{name}"')
        conn.execute(sql, args)
    finally:
        conn.close()


@pytest.mark.parametrize("sql,arg,match", [
    ("UPDATE requests SET tool_input_digest = 'jcs:' || ? WHERE id = ?", "g" * 64, "64 lowercase hex"),
    ("UPDATE requests SET tool_input_digest = 'jcs:' || ? WHERE id = ?", "A" * 64, "64 lowercase hex"),
    ("UPDATE requests SET cwd = ? WHERE id = ?", "", "empty"),
    ("UPDATE requests SET agent_session_id = ? WHERE id = ?", "", "empty"),
    ("UPDATE requests SET decided_at = ? WHERE id = ?", "garbage", "decision time"),
    ("UPDATE receipts SET created_at = ? WHERE request_id = ?", "garbage", "stamp"),
])
def test_a_value_the_spec_would_reject_is_an_export_error_not_a_receipt(tmp_path, sql, arg, match):
    queue, ids = build_scratch(tmp_path)
    _corrupt(queue, sql, (arg, ids["signed_allow"]))
    with pytest.raises(ab.ExportError, match=match):
        ab.export(queue.directory, now=later(queue))


def test_a_dialog_outcome_time_is_parsed_only_where_it_is_used(tmp_path):
    queue, ids = build_scratch(tmp_path)
    clean = ab.export(queue.directory, now=later(queue))
    _corrupt(queue, "INSERT INTO dialog_outcomes (request_id, outcome, closed_at, event) VALUES (?, 'ran', 'garbage', 'PostToolUse')",
             (ids["denied"],))                                       # a denied row ignores any outcome
    assert ab.export(queue.directory, now=later(queue)).receipts == clean.receipts


@pytest.mark.parametrize("gap", [timedelta(0), ab.SETTLE * 2])  # inside SETTLE, and past it
def test_an_outcome_the_queue_never_writes_is_ignored_and_reaches_no_receipt(tmp_path, gap):
    queue, ids = build_scratch(tmp_path)
    clean = ab.export(queue.directory, now=later(queue))
    conn = sqlite3.connect(queue.path, isolation_level=None)
    try:
        conn.execute("PRAGMA ignore_check_constraints = ON")         # a raw writer, past the queue's CHECK
        decided = conn.execute("SELECT decided_at FROM requests WHERE id = ?", (ids["withdrawn"],)).fetchone()[0]
        conn.execute("INSERT INTO dialog_outcomes (request_id, outcome, closed_at, event) VALUES (?, 'bogus', ?, 'PostToolUse')",
                     (ids["withdrawn"], utc_stamp(parse_stamp(decided) + gap)))
    finally:
        conn.close()
    assert ab.export(queue.directory, now=later(queue)).receipts == clean.receipts


@pytest.mark.parametrize("score", ["0.5", True, [1], float("nan"), float("inf")])
def test_a_score_that_is_not_a_number_is_an_export_error(score):
    with pytest.raises(ab.ExportError, match="not a number"):
        ab.receipt_hash({"completeness_score": score})


def test_receipt_hash_agrees_with_an_independent_rfc_8785_implementation(scratch):
    rfc8785 = pytest.importorskip("rfc8785")
    import hashlib
    for r in scratch[2].receipts:
        body = {k: v for k, v in r.items() if k != "receipt_hash"}
        assert hashlib.sha256(rfc8785.dumps(body)).hexdigest() == r["receipt_hash"], r["execution"]["result_ref"]


# --- S-R2 (1019+45 B5, REFUSED_ANSWER_DESIGN D4): a kept refused answer exported as the evidence of a refusal ------

@pytest.fixture(scope="module")
def rkeys(tmp_path_factory):
    d = tmp_path_factory.mktemp("ab-refused-keys")
    return {n: Key(d, n) for n in ("laptop", "presence", "revoked", "thief", "stranger")}


def _refused_world(tmp_path, rkeys):
    """A queue over a log holding laptop (laptop-presence), presence (presence: does not cover T2), revoked (a plain
    revocation) and thief (revoked as compromised); stranger is never enrolled."""
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", rkeys)
    log.enrol("laptop", "laptop-presence")
    log.enrol("presence", "presence")
    log.enrol("revoked", "display-phone")
    log.revoke("revoked")
    log.enrol("thief", "display-phone")
    since = log.source().key(rkeys["thief"].fp).enrolled_seq
    log.link({"op": "revoke_key", "fingerprint": rkeys["thief"].fp, "compromised_since": since})
    clock = Clock(datetime.now(timezone.utc))
    return ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock, home=h4_home()), clock


HOLD = timedelta(minutes=5)


def _file(queue, command="x"):
    return queue.file(agent="claude_code", agent_session_id="s-r", tool_name="Bash", tool_input={"command": command},
                      cwd="/w", hold=HOLD, tier="T2")


def _attempt(queue, request, key, decision="allow", **override):
    from levain.consent import AnswerRefused
    payload, sig = signed(queue, request, key, decision, **override)
    with pytest.raises(AnswerRefused):
        queue.answer(request.id, payload, sig)
    return payload


def _lapse(queue, clock):
    clock.now += HOLD + UNDECIDED_GRACE + timedelta(seconds=1)
    assert queue.sweep() == 1


def _expired_receipt(queue, now):
    exported = ab.export(queue.directory, now=now)
    [r] = [r for r in exported.receipts if "refusal" in r and r["refusal"]["verdict"] in ("expired", "deny")]
    return r, exported


@pytest.mark.parametrize("key, operator, enrolled, compromised, subreason", [
    ("stranger", False, False, False, "not-enrolled"),
    ("presence", True, True, False, "class"),
    ("revoked", True, True, False, "revoked"),
    ("thief", False, True, True, "compromised"),
])
def test_an_unauthorised_approval_exports_as_deny_with_the_approval_as_evidence(tmp_path, rkeys, key, operator,
                                                                                enrolled, compromised, subreason):
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    payload = _attempt(queue, request, rkeys[key])
    _lapse(queue, clock)
    r, exported = _expired_receipt(queue, clock.now + timedelta(days=2))
    assert r["refusal"] == {"verdict": "deny", "reason_code": "approver-not-authorized"}
    assert r["policy"]["decision"] == "require-approval" and r["execution"]["status"] == "blocked"
    approver = r["approval"]["approver"]
    assert approver["id"] == f"ssh-key:{rkeys[key].fp}"
    # never attribute a thief's or a stranger's signature to the operator: no role at all (the schema's is optional)
    assert ("role" in approver) is operator and approver.get("role", "operator") == "operator"
    assert r["approval"]["approved_at"] == payload["signed_at"]
    context = json.loads(r["approval"]["context"])
    assert (context["refused"], context["via"], context["enrolled"], context["compromised"], context["subreason"]) == \
        (True, "refused-answer", enrolled, compromised, subreason)
    assert context["request_id"] == request.id and context["levain_reason"]
    p = r["provenance"]
    assert (p["approval.approver.id"], p["approval.approved_at"], p["approval.context"]) == ("inferred",) * 3
    assert ("approval.approver.role" in p) is operator
    assert r["completeness_score"] == ab._score_milli(r, p) / 1000.0 and r["receipt_hash"] == ab.receipt_hash(r)
    assert exported.report.refused_not_authorized == 1 and exported.report.refused_window_elapsed == 0
    if _has_ab():
        assert ab_validate(r) == []                 # the spec's own schema (where agentboundary is installed)


def _has_ab() -> bool:
    try:
        import agentboundary  # noqa: F401
    except ImportError:
        return False
    return True


def test_an_authorised_answer_after_the_hold_keeps_expired_and_carries_the_stale_approval(tmp_path, rkeys):
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    _lapse(queue, clock)
    expired_at = utc_stamp(clock.now)
    clock.now += timedelta(seconds=30)
    payload = _attempt(queue, request, rkeys["laptop"])
    [kept] = queue.refused_answers(request.id)
    assert (kept["reason_code"], kept["subreason"], kept["enrolled"]) == ("approval-window-elapsed", "late", True)
    r, exported = _expired_receipt(queue, clock.now + timedelta(days=2))
    assert r["refusal"] == {"verdict": "expired", "reason_code": "approval-window-elapsed"}
    # the action stayed blocked through levain's refusal of the late answer: that refusal is the block's time, so
    # completed_at never precedes issued_at (the spec's LEVEL_4_COMPLETED_BEFORE_ISSUED, run on this receipt 1019+49)
    assert r["execution"]["status"] == "blocked" and r["execution"]["completed_at"] == kept["refused_at"]
    assert r["execution"]["completed_at"] > expired_at and r["provenance"]["execution.completed_at"] == "inferred"
    # his own authorised key, refused only for its timing: named the operator (head ruling 1019+45, built 1019+49)
    assert r["approval"]["approver"] == {"id": f"ssh-key:{rkeys['laptop'].fp}", "role": "operator"}
    assert r["provenance"]["approval.approver.role"] == "synthesized"
    assert r["approval"]["approved_at"] == payload["signed_at"]
    # the receipt is not earlier than the evidence it records
    assert r["issued_at"] == kept["refused_at"] and r["provenance"]["issued_at"] == "inferred"
    assert exported.report.refused_window_elapsed == 1 and exported.report.refused_not_authorized == 0


def test_a_refused_answer_claiming_a_time_after_its_refusal_is_capped_at_the_refusal(tmp_path, rkeys):
    """signed_at is the signer's claim and a refused answer's is not bounded by the queue: an approved_at later than
    the refusal it is the evidence of is capped at the refusal and marked synthesized (as _approved_at does)."""
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    _attempt(queue, request, rkeys["stranger"], signed_at=utc_stamp(clock.now + timedelta(days=30)))
    _lapse(queue, clock)
    r, _ = _expired_receipt(queue, clock.now + timedelta(days=2))
    [kept] = queue.refused_answers(request.id)
    assert r["approval"]["approved_at"] == kept["refused_at"]
    assert r["provenance"]["approval.approved_at"] == "synthesized"
    assert r["approval"]["approved_at"] <= r["issued_at"] <= r["execution"]["completed_at"]


def test_a_refused_deny_is_never_approval_evidence_and_the_row_exports_as_plain_expired(tmp_path, rkeys):
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    _attempt(queue, request, rkeys["stranger"], "deny")
    assert [k["decision"] for k in queue.refused_answers(request.id)] == ["deny"]
    _lapse(queue, clock)
    r, exported = _expired_receipt(queue, clock.now + timedelta(days=2))
    assert r["refusal"] == {"verdict": "expired", "reason_code": "approval-window-elapsed"}
    assert "approval" not in r and not any(k.startswith("approval") for k in r["provenance"])
    assert exported.report.refused_not_authorized == exported.report.refused_window_elapsed == 0


def test_the_latest_refused_allow_in_the_window_is_the_one_exported(tmp_path, rkeys):
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    _attempt(queue, request, rkeys["stranger"])
    clock.now += timedelta(seconds=10)
    _attempt(queue, request, rkeys["presence"])
    clock.now += timedelta(seconds=10)
    _attempt(queue, request, rkeys["revoked"], "deny")                  # later, but a deny: passed over
    _lapse(queue, clock)
    r, _ = _expired_receipt(queue, clock.now + timedelta(days=2))
    assert r["approval"]["approver"]["id"] == f"ssh-key:{rkeys['presence'].fp}"


def test_a_refusal_kept_after_the_row_was_exported_never_changes_the_receipt(tmp_path, rkeys):
    """R10 SETTLE: an expired row with no outcome is held until it is SETTLE old; a refused answer inside the window
    is in the export, and one kept after an export took the row (even with the writer's clock the full slack behind
    the export clock) changes nothing."""
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    _lapse(queue, clock)
    created = clock.now
    held = ab.export(queue.directory, now=created + timedelta(minutes=1))
    assert held.receipts == [] and held.report.held_unsettled == 1
    now = created + ab.SETTLE                                          # the first moment the row is exported
    first = ab.export(queue.directory, now=now)
    [r] = first.receipts
    assert r["refusal"] == {"verdict": "expired", "reason_code": "approval-window-elapsed"} and "approval" not in r
    clock.now = now - ab._CLOCK_SLACK
    _attempt(queue, request, rkeys["stranger"])
    assert len(queue.refused_answers(request.id)) == 1                 # kept, as evidence, but past the window
    assert ab.export(queue.directory, now=now + timedelta(hours=1)).receipts == first.receipts
    # the same refusal one second inside the window, while the row is still held, is the receipt's evidence
    (tmp_path / "b").mkdir()
    q2, c2 = _refused_world(tmp_path / "b", rkeys)
    r2 = _file(q2)
    _lapse(q2, c2)
    created2 = c2.now
    c2.now = created2 + ab.SETTLE - ab._CLOCK_SLACK - timedelta(seconds=1)
    _attempt(q2, r2, rkeys["stranger"])
    [counted] = ab.export(q2.directory, now=created2 + ab.SETTLE).receipts
    assert counted["refusal"] == {"verdict": "deny", "reason_code": "approver-not-authorized"}


def test_a_malformed_refused_row_is_passed_over_never_an_export_error(tmp_path, rkeys):
    queue, clock = _refused_world(tmp_path, rkeys)
    request = _file(queue)
    _lapse(queue, clock)
    conn = sqlite3.connect(queue.path)
    try:
        with conn:
            conn.execute("INSERT INTO refused_answers (request_id, kind, refused_at, reason_code, subreason, enrolled, "
                         "detail, decision, signer_fp, signed_at, record, signature) VALUES "
                         "(?, 'answer', ?, 'approver-not-authorized', 'not-enrolled', 0, 'd', 'allow', 'SHA256:x', "
                         "'not a time', '{}', 's')", (request.id, utc_stamp(clock.now)))
    finally:
        conn.close()
    r, _ = _expired_receipt(queue, clock.now + timedelta(days=2))
    assert r["refusal"]["verdict"] == "expired" and "approval" not in r


def test_the_scratch_queue_has_no_refused_answers_and_exports_as_before(scratch):
    assert scratch[2].report.refused_not_authorized == scratch[2].report.refused_window_elapsed == 0
