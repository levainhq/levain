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

from levain.cockpit.consent import utc_stamp
from levain.consent import ConsentQueue, ruling_message
from levain.consent import agentboundary as ab
from levain.consent.queue import UNDECIDED_GRACE
from levain.consent.record import RULING_NAMESPACE
from tests.test_consent_queue import Clock, Key, Log, signed

HEX64 = re.compile(r"^[a-f0-9]{64}$")


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
    queue = ConsentQueue(tmp / "consent", authority=log.source, clock=clock)
    ids: dict[str, str] = {}

    def file(name: str, tool: str, tool_input: Any, *, agent: str = "claude_code", cwd: str = str(ws),
             hold: timedelta = timedelta(minutes=5), tier: str = "T2"):
        request = queue.file(agent=agent, agent_session_id=f"sess-{name}", tool_name=tool, tool_input=tool_input,
                             cwd=cwd, hold=hold, tier=tier)
        ids[name] = request.id
        return request

    edit = {"file_path": str(ws / "a.txt"), "old_string": "a", "new_string": "b"}
    request = file("grant", "Edit", edit)
    assert queue.answer_by_grant(request.id, tool_input=edit, posture="default") is not None
    request = file("signed_allow", "Bash", {"command": "touch /tmp/x"})
    answer = signed(queue, request, laptop, "allow")
    clock.now += timedelta(seconds=2)                      # the queue applies the answer after the signer signed it
    queue.answer(request.id, *answer)
    request = file("signed_same", "Bash", {"command": "touch /tmp/same"})
    queue.answer(request.id, *signed(queue, request, laptop, "allow"))     # applied in the stamp it was signed in
    request = file("denied", "Write", {"file_path": "/tmp/y"}, agent="codex")
    answer = signed(queue, request, laptop, "deny")
    clock.now += timedelta(seconds=2)
    queue.answer(request.id, *answer)
    request = file("undecided", "Bash", {"command": "rm -r build"})
    assert queue.mark_undecided(request.id).state == "undecided"
    request = file("ruled_allow", "Bash", {"command": "make"}, tier="T3")
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
                            tool_input={"command": "echo ran"}, event="post")[1] == "ran"      # answered while held
    request = file("ran_unasked", "Bash", {"command": "echo unasked"})
    assert queue.withdraw(request.id)
    assert queue.record_ran(agent="claude_code", agent_session_id="sess-ran_unasked", tool_name="Bash",
                            tool_input={"command": "echo unasked"}, event="post")[1] == "ran_unasked"
    request = file("did_not_run", "Bash", {"command": "echo no"})
    assert queue.withdraw(request.id)
    assert queue.close_unrun(agent="claude_code", agent_session_id="sess-did_not_run", event="stop") == [request.id]
    file("float", "Bash", {"command": "sleep", "seconds": 1.5})          # outside JCS: a json: digest
    file("decomposed", "Bash", {"command": "pwd"}, cwd="/w/café")    # a working directory not in NFC
    request = file("expiring", "Bash", {"command": "sleep 1"}, hold=timedelta(minutes=1))
    clock.now += timedelta(minutes=1) + UNDECIDED_GRACE + timedelta(seconds=1)
    assert queue.sweep() == 1
    file("pending", "Bash", {"command": "still waiting"})
    return queue, ids


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
    assert exported.report.by_state == {"allowed": 4, "denied": 2, "expired": 1, "undecided": 3, "withdrawn": 4}
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
    assert r["provenance"]["execution.status"] == "synthesized"      # permitted, never known to have run
    assert json.loads(approval["context"]) == {"queue": queue.queue_id, "request_id": ids["signed_allow"],
                                               "via": "signature", "verify": "levain consent receipts verify"}


def _signed_at(queue, request_id):
    conn = sqlite3.connect(queue.path)
    try:
        return json.loads(conn.execute("SELECT answer FROM requests WHERE id = ?", (request_id,)).fetchone()[0])["signed_at"]
    finally:
        conn.close()


def test_a_signed_answer_in_the_stamp_it_was_applied_falls_back_to_the_decision_time_and_is_counted(scratch):
    r = _row(scratch, "signed_same", "allowed")
    assert r["approval"]["approved_at"] == r["execution"]["completed_at"]
    assert r["provenance"]["approval.approved_at"] == "synthesized"
    assert scratch[2].report.approved_at_fallback == 1


def test_approved_at_precedes_completed_at_for_every_signed_allow_but_the_counted_fallback(scratch):
    _, ids, exported = scratch
    seen = 0
    for r in exported.receipts:
        if "approval" in r and r["approval"]["approver"]["id"].startswith("ssh-key:"):
            seen += 1
            if r["provenance"]["approval.approved_at"] == "inferred":
                assert r["approval"]["approved_at"] < r["execution"]["completed_at"]
    assert seen == 3 + 0           # signed_allow, signed_same, ruled_allow


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
    assert ("claude-code.native-permissions", "unknown") in scratch[2].policy_store()
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
    queue = ConsentQueue(tmp_path / "consent", authority=log.source)
    queue.sweep()                                                       # opens the store, creating the schema
    exported = ab.export(queue.directory)
    assert exported.receipts == [] and exported.policy_store() == set()
    assert exported.report.receipts == 0 and exported.report.held_unsettled == 0 and exported.report.pending_skipped == 0


def _late_world(tmp_path):
    """A bare queue holding one request withdrawn at T0, whose PostToolUse comes later."""
    (tmp_path / "keys").mkdir()
    (tmp_path / "auth").mkdir()
    log = Log(tmp_path / "auth", {"laptop": Key(tmp_path / "keys", "laptop")})
    clock = Clock(datetime.now(timezone.utc))
    queue = ConsentQueue(tmp_path / "consent", authority=log.source, clock=clock)
    request = queue.file(agent="claude_code", agent_session_id="s-late", tool_name="Bash", tool_input={"command": "late"},
                         cwd=str(tmp_path), hold=timedelta(minutes=5), tier="T2")
    assert queue.withdraw(request.id)
    t0 = clock.now
    return queue, clock, t0


def _ran_late(queue):
    return queue.record_ran(agent="claude_code", agent_session_id="s-late", tool_name="Bash",
                            tool_input={"command": "late"}, event="post")


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
    queue, clock, t0 = _late_world(tmp_path)
    request = queue.file(agent="claude_code", agent_session_id="s-after", tool_name="Bash", tool_input={"command": "z"},
                         cwd=str(tmp_path), hold=timedelta(minutes=5), tier="T2")
    assert queue.mark_undecided(request.id).state == "undecided"        # a terminal row AFTER the unsettled one
    held = ab.export(queue.directory, now=t0 + timedelta(minutes=1))
    assert held.report.held_unsettled == 2
    assert held.report.receipts == 0 and held.receipts == []
    assert ab.export(queue.directory, now=t0 + ab.SETTLE * 2).report.receipts == 2
    assert all("s-after" not in r["actor"]["id"] for r in held.receipts)


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
